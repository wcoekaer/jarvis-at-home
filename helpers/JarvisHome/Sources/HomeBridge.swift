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

final class HomeBridge: NSObject, ObservableObject, HMHomeManagerDelegate, HMHomeDelegate, HMAccessoryDelegate {
    @Published var status = "Starting…"
    @Published var accessoryCount = 0
    @Published var lastEvent = ""

    private let manager = HMHomeManager()
    private let mqtt: MQTTClient
    private var snapshotTimer: Timer?
    private let iso = ISO8601DateFormatter()
    private var subscribed = Set<UUID>()   // characteristics already set up
    private var observedAt: [UUID: Date] = [:]
    // Last value published per characteristic, and when. HomeKit re-sends
    // unchanged values in bursts (bridge reconnects, hub changes); those must
    // not become events (they'd replay alerts) or flood MQTT.
    private var lastPublished: [UUID: (value: String, at: Date)] = [:]
    private var lastReachable: [UUID: Bool] = [:]
    private let unchangedRepublish: TimeInterval = 600

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
        var count = 0
        for home in manager.homes {
            home.delegate = self  // learn about accessories added, removed, moved, renamed
            for accessory in home.accessories {
                setUp(accessory)
                count += 1
            }
        }
        accessoryCount = count
        status = "Watching \(subscribed.count) values on \(count) accessories in \(manager.homes.count) home(s)"
        if snapshotTimer == nil {
            snapshotTimer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { [weak self] _ in
                self?.republishCached()
            }
        }
        scheduleInventory()
    }

    /// Subscribe to an accessory's watched characteristics and read each once.
    private func setUp(_ accessory: HMAccessory) {
        accessory.delegate = self
        for ch in watchedCharacteristics(accessory) {
            guard !subscribed.contains(ch.uniqueIdentifier) else { continue }
            subscribed.insert(ch.uniqueIdentifier)
            if ch.properties.contains(HMCharacteristicPropertySupportsEventNotification) {
                ch.enableNotification(true) { _ in }
            }
            // One read per characteristic, when it is first seen.
            if ch.properties.contains(HMCharacteristicPropertyReadable) {
                ch.readValue { [weak self] error in
                    if error == nil {
                        self?.observedAt[ch.uniqueIdentifier] = Date()
                        self?.publish(ch, event: false)
                    }
                }
            }
        }
    }

    // MARK: HMHomeDelegate: keep the published picture in step with Home.

    func home(_ home: HMHome, didAdd accessory: HMAccessory) {
        setUp(accessory)
        publishReachability(accessory)
        accessoryCount += 1
        scheduleInventory()
    }

    func home(_ home: HMHome, didRemove accessory: HMAccessory) {
        accessoryCount = max(0, accessoryCount - 1)
        scheduleInventory()  // Jarvis clears the removed accessory's topics
    }

    func home(_ home: HMHome, didUpdate room: HMRoom, for accessory: HMAccessory) {
        republish(accessory)  // topics include the room
        scheduleInventory()
    }

    func accessoryDidUpdateName(_ accessory: HMAccessory) {
        republish(accessory)  // topics include the name
        scheduleInventory()
    }

    private func republish(_ accessory: HMAccessory) {
        publishReachability(accessory)
        for ch in watchedCharacteristics(accessory) where ch.value != nil {
            publish(ch, event: false)
        }
    }

    /// Retained list of every accessory that currently exists, as
    /// "home/room/accessory" (the topic prefix). Jarvis uses it to drop
    /// topics of accessories that were removed, renamed or moved.
    private var inventoryWork: DispatchWorkItem?

    private func scheduleInventory() {
        inventoryWork?.cancel()  // coalesce bursts of changes
        let work = DispatchWorkItem { [weak self] in self?.publishInventory() }
        inventoryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 20, execute: work)
    }

    private func publishInventory() {
        var list: [String] = []
        for home in manager.homes {
            for accessory in home.accessories {
                list.append([home.name, accessory.room?.name ?? "Default Room", accessory.name].map(clean).joined(separator: "/"))
            }
        }
        let body: [String: Any] = ["ts": iso.string(from: Date()), "homes": manager.homes.map { clean($0.name) },
                                   "accessories": list.sorted()]
        mqtt.publish("homekit/inventory", json(body), retain: true)
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
                republish(accessory)
            }
        }
        publishInventory()
    }

    func accessory(_ accessory: HMAccessory, service: HMService, didUpdateValueFor characteristic: HMCharacteristic) {
        guard watchedTypes.contains(characteristic.characteristicType) else { return }
        let id = characteristic.uniqueIdentifier
        observedAt[id] = Date()  // a repeat still proves the device is alive
        if let last = lastPublished[id], last.value == valueKey(characteristic.value) {
            // Unchanged: no event; refresh the retained state only occasionally.
            if Date().timeIntervalSince(last.at) >= unchangedRepublish {
                publish(characteristic, event: false)
            }
            return
        }
        publish(characteristic, event: true)
    }

    func accessoryDidUpdateReachability(_ accessory: HMAccessory) {
        guard lastReachable[accessory.uniqueIdentifier] != accessory.isReachable else { return }
        publishReachability(accessory)
    }

    private func valueKey(_ value: Any?) -> String {
        guard let value else { return "nil" }
        return String(describing: value)
    }

    private func publishReachability(_ accessory: HMAccessory) {
        let room = accessory.room?.name ?? "Default Room"
        let home = manager.homes.first { $0.accessories.contains(accessory) }?.name ?? "Home"
        let topic = ["homekit", home, room, accessory.name, "Reachable"].map(clean).joined(separator: "/")
        let body: [String: Any] = ["home": home, "room": room, "accessory": accessory.name,
                                   "characteristic": "Reachable", "value": accessory.isReachable,
                                   "ts": iso.string(from: Date())]
        mqtt.publish(topic, json(body), retain: true)
        lastReachable[accessory.uniqueIdentifier] = accessory.isReachable
    }

    private func publish(_ ch: HMCharacteristic, event: Bool) {
        guard let service = ch.service, let accessory = service.accessory, let value = ch.value else { return }
        let room = accessory.room?.name ?? "Default Room"
        let home = manager.homes.first { $0.accessories.contains(accessory) }?.name ?? "Home"
        let name = ch.localizedDescription
        var body: [String: Any] = ["home": home, "room": room, "accessory": accessory.name,
                                   "service": service.name, "characteristic": name,
                                   "value": value, "reachable": accessory.isReachable,
                                   "observed_at": iso.string(from: observedAt[ch.uniqueIdentifier] ?? .distantPast),
                                   "ts": iso.string(from: Date())]
        if let text = describe(ch.characteristicType, value) { body["text"] = text }
        let topic = ["homekit", home, room, accessory.name, service.name == accessory.name ? name : "\(service.name) \(name)"]
            .map(clean).joined(separator: "/")
        let payload = json(body)
        mqtt.publish(topic, payload, retain: true)
        lastPublished[ch.uniqueIdentifier] = (valueKey(value), Date())
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
            if v is NSNumber || v is String || v is Bool || v is [String] { return v }
            return String(describing: v)
        }
        let data = (try? JSONSerialization.data(withJSONObject: safe, options: [.sortedKeys])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
