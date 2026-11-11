// JarvisHome: HomeKit -> MQTT bridge for Jarvis.
// Reads broker settings (MQTT_HOST, MQTT_USERNAME, MQTT_PASSWORD, MQTT_PORT)
// from the Jarvis .env file ($JARVIS_ENV or ~/git/jarvis/.env).

import SwiftUI

func loadEnv() -> [String: String] {
    let path = ProcessInfo.processInfo.environment["JARVIS_ENV"]
        ?? (NSHomeDirectory() as NSString).appendingPathComponent("git/jarvis/.env")
    var env: [String: String] = [:]
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return env }
    for line in text.split(separator: "\n") {
        let l = line.trimmingCharacters(in: .whitespaces)
        guard !l.hasPrefix("#"), let eq = l.firstIndex(of: "=") else { continue }
        let key = l[..<eq].trimmingCharacters(in: .whitespaces)
        var val = l[l.index(after: eq)...].trimmingCharacters(in: .whitespaces)
        val = val.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        env[key] = val
    }
    return env
}

@main
struct JarvisHomeApp: App {
    @StateObject private var bridge: HomeBridge = {
        let env = loadEnv()
        let mqtt = MQTTClient(host: env["MQTT_HOST"] ?? "localhost",
                              port: UInt16(env["MQTT_PORT"] ?? "1883") ?? 1883,
                              username: env["MQTT_USERNAME"], password: env["MQTT_PASSWORD"],
                              clientID: "jarvishome-\(ProcessInfo.processInfo.processIdentifier)")
        mqtt.start()
        return HomeBridge(mqtt: mqtt)
    }()

    var body: some Scene {
        WindowGroup {
            VStack(alignment: .leading, spacing: 8) {
                Text("JarvisHome").font(.headline)
                Text(bridge.status)
                Text("Accessories: \(bridge.accessoryCount)")
                Text(bridge.lastEvent).font(.caption).foregroundStyle(.secondary)
            }
            .padding()
            .frame(minWidth: 360, minHeight: 140, alignment: .topLeading)
        }
    }
}
