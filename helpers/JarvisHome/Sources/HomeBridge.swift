// Publishes selected HomeKit accessory characteristics to MQTT:
//   homekit/<home>/<room>/<accessory>/<characteristic>   (retained state)
//   homekit/events                                       (stream of changes)
// Payload: {"home","room","accessory","service","characteristic","value","text","reachable","ts"}
//
// Kept deliberately light on HomeKit: only the characteristic types Jarvis uses
// are subscribed, each value is read from the device once at startup, and after
// that only change notifications are used. The periodic re-publish uses
// HomeKit's cached values and never reads from devices (reads on battery and
// Thread/Bluetooth accessories go over the radio via the home hubs).

import Foundation
import HomeKit

/// Characteristic types Jarvis cares about (security, safety, batteries).
let watchedTypes: Set<String> = [
    HMCharacteristicTypeCurrentLockMechanismState,
    HMCharacteristicTypeContactState,
    HMCharacteristicTypeCurrentDoorState,
    HMCharacteristicTypeLeakDetected,
    HMCharacteristicTypeSmokeDetected,
    HMCharacteristicTypeCarbonMonoxideDetected,
    HMCharacteristicTypeStatusLowBattery,
    // indoor air quality
    HMCharacteristicTypeAirQuality,
    HMCharacteristicTypePM2_5Density,
    HMCharacteristicTypePM10Density,
    HMCharacteristicTypeVolatileOrganicCompoundDensity,
    HMCharacteristicTypeCarbonDioxideLevel,
    HMCharacteristicTypeCarbonDioxideDetected,
]

final class HomeBridge: NSObject, ObservableObject, HMHomeManagerDelegate, HMAccessoryDelegate {
    @Published var status = "Starting…"
    @Published var accessoryCount = 0
    @Published var lastEvent = ""

    private let manager = HMHomeManager()
    private let mqtt: MQTTClient
    private var snapshotTimer: Timer?
    private let iso = ISO8601DateFormatter()
    private var subscribed = Set<UUID>()   // characteristics already set up

    init(mqtt: MQTTClient) {
        self.mqtt = mqtt
        super.init()
        mqtt.onStatus = { [weak self] s in DispatchQueue.main.async { self?.status = s } }
        manager.delegate = self
    }

    func homeManager(_ manager: HMHomeManager, didUpdate status: HMHomeManagerAuthorizationStatus) {
        if !status.contains(.authorized) {
            self.status = "HomeKit access not granted (System Settings > Privacy & Security > Home)"
        }
    }

    func homeManagerDidUpdateHomes(_ manager: HMHomeManager) {
        // Can be called repeatedly; only new characteristics are set up.
        var count = 0, watched = 0
        for home in manager.homes {
            for accessory in home.accessories {
                accessory.delegate = self
                count += 1
                for ch in watchedCharacteristics(accessory) {
                    watched += 1
                    guard !subscribed.contains(ch.uniqueIdentifier) else { continue }
                    subscribed.insert(ch.uniqueIdentifier)
                    if ch.properties.contains(HMCharacteristicPropertySupportsEventNotification) {
                        ch.enableNotification(true) { _ in }
                    }
                    // One read per characteristic, at startup only.
                    if ch.properties.contains(HMCharacteristicPropertyReadable) {
                        ch.readValue { [weak self] error in
                            if error == nil { self?.publish(ch, event: false) }
                        }
                    }
                }
            }
        }
        accessoryCount = count
        status = "Watching \(watched) values on \(count) accessories in \(manager.homes.count) home(s)"
        if snapshotTimer == nil {
            snapshotTimer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { [weak self] _ in
                self?.republishCached()
            }
        }
    }

    private func watchedCharacteristics(_ accessory: HMAccessory) -> [HMCharacteristic] {
        accessory.services
            .filter { $0.serviceType != HMServiceTypeAccessoryInformation }
            .flatMap { $0.characteristics }
            .filter { watchedTypes.contains($0.characteristicType) }
    }

    /// Refresh the retained MQTT values from HomeKit's cache; no device reads.
    private func republishCached() {
        for home in manager.homes {
            for accessory in home.accessories {
                publishReachability(accessory)
                for ch in watchedCharacteristics(accessory) where ch.value != nil {
                    publish(ch, event: false)
                }
            }
        }
    }

    func accessory(_ accessory: HMAccessory, service: HMService, didUpdateValueFor characteristic: HMCharacteristic) {
        guard watchedTypes.contains(characteristic.characteristicType) else { return }
        publish(characteristic, event: true)
    }

    func accessoryDidUpdateReachability(_ accessory: HMAccessory) {
        publishReachability(accessory)
    }

    private func publishReachability(_ accessory: HMAccessory) {
        let room = accessory.room?.name ?? "Default Room"
        let home = manager.homes.first { $0.accessories.contains(accessory) }?.name ?? "Home"
        let topic = ["homekit", home, room, accessory.name, "Reachable"].map(clean).joined(separator: "/")
        let body: [String: Any] = ["home": home, "room": room, "accessory": accessory.name,
                                   "characteristic": "Reachable", "value": accessory.isReachable,
                                   "ts": iso.string(from: Date())]
        mqtt.publish(topic, json(body), retain: true)
    }

    private func publish(_ ch: HMCharacteristic, event: Bool) {
        guard let service = ch.service, let accessory = service.accessory, let value = ch.value else { return }
        let room = accessory.room?.name ?? "Default Room"
        let home = manager.homes.first { $0.accessories.contains(accessory) }?.name ?? "Home"
        let name = ch.localizedDescription
        var body: [String: Any] = ["home": home, "room": room, "accessory": accessory.name,
                                   "service": service.name, "characteristic": name,
                                   "value": value, "reachable": accessory.isReachable,
                                   "ts": iso.string(from: Date())]
        if let text = describe(ch.characteristicType, value) { body["text"] = text }
        let topic = ["homekit", home, room, accessory.name, service.name == accessory.name ? name : "\(service.name) \(name)"]
            .map(clean).joined(separator: "/")
        let payload = json(body)
        mqtt.publish(topic, payload, retain: true)
        if event {
            mqtt.publish("homekit/events", payload)
            DispatchQueue.main.async { self.lastEvent = "\(accessory.name): \(name) = \(body["text"] ?? value)" }
        }
    }

    /// Human-readable meaning for common enum/boolean characteristics.
    private func describe(_ type: String, _ value: Any) -> String? {
        let n = (value as? NSNumber)?.intValue ?? -1
        switch type {
        case HMCharacteristicTypeCurrentLockMechanismState, HMCharacteristicTypeTargetLockMechanismState:
            return ["unlocked", "locked", "jammed", "unknown"][safe: n]
        case HMCharacteristicTypeContactState:
            return n == 0 ? "closed" : "open"
        case HMCharacteristicTypeCurrentDoorState, HMCharacteristicTypeTargetDoorState:
            return ["open", "closed", "opening", "closing", "stopped"][safe: n]
        case HMCharacteristicTypeMotionDetected, HMCharacteristicTypeOccupancyDetected:
            return n == 1 ? "detected" : "clear"
        case HMCharacteristicTypeSmokeDetected, HMCharacteristicTypeCarbonMonoxideDetected,
             HMCharacteristicTypeLeakDetected:
            return n == 1 ? "DETECTED" : "clear"
        case HMCharacteristicTypeStatusLowBattery:
            return n == 1 ? "low battery" : "battery ok"
        case HMCharacteristicTypeAirQuality:
            return ["unknown", "excellent", "good", "fair", "inferior", "poor"][safe: n]
        case HMCharacteristicTypeCarbonDioxideDetected:
            return n == 1 ? "abnormal CO2" : "normal CO2"
        case HMCharacteristicTypePM2_5Density, HMCharacteristicTypePM10Density,
             HMCharacteristicTypeVolatileOrganicCompoundDensity:
            if let v = (value as? NSNumber)?.doubleValue { return String(format: "%.0f µg/m³", v) }
            return nil
        case HMCharacteristicTypeCarbonDioxideLevel:
            if let v = (value as? NSNumber)?.doubleValue { return String(format: "%.0f ppm", v) }
            return nil
        case HMCharacteristicTypePowerState:
            return n == 1 ? "on" : "off"
        case HMCharacteristicTypeCurrentPosition, HMCharacteristicTypeBatteryLevel,
             HMCharacteristicTypeCurrentRelativeHumidity:
            return "\(n)%"
        case HMCharacteristicTypeCurrentTemperature:
            if let c = (value as? NSNumber)?.doubleValue { return String(format: "%.0f°F", c * 9 / 5 + 32) }
            return nil
        default:
            return nil
        }
    }

    private func clean(_ s: String) -> String {
        s.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: "#", with: "")
            .replacingOccurrences(of: "+", with: "")
    }

    private func json(_ obj: [String: Any]) -> String {
        let safe = obj.mapValues { v -> Any in
            if v is NSNumber || v is String || v is Bool { return v }
            return String(describing: v)
        }
        let data = (try? JSONSerialization.data(withJSONObject: safe, options: [.sortedKeys])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
