// Minimal MQTT 3.1.1 publisher (QoS 0, optional retain) over TCP using
// Network.framework. Reconnects automatically and keeps the session alive.

import Foundation
import Network

final class MQTTClient {
    private let host: String
    private let port: UInt16
    private let username: String?
    private let password: String?
    private let clientID: String
    private let queue = DispatchQueue(label: "mqtt")
    private var connection: NWConnection?
    private var connected = false
    private var pingTimer: DispatchSourceTimer?
    var onStatus: ((String) -> Void)?

    init(host: String, port: UInt16 = 1883, username: String?, password: String?, clientID: String) {
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.clientID = clientID
    }

    func start() { queue.async { self.connect() } }

    private func connect() {
        connected = false
        let conn = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: .tcp)
        connection = conn
        conn.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.sendConnect()
                self.receive()
            case .failed(let err), .waiting(let err):
                self.onStatus?("MQTT error: \(err)")
                self.scheduleReconnect()
            case .cancelled:
                break
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    private func scheduleReconnect() {
        connected = false
        pingTimer?.cancel()
        connection?.cancel()
        connection = nil
        queue.asyncAfter(deadline: .now() + 10) { [weak self] in
            if self?.connection == nil { self?.connect() }
        }
    }

    private func receive() {
        connection?.receive(minimumIncompleteLength: 2, maximumLength: 4096) { [weak self] data, _, done, error in
            guard let self else { return }
            if let data, data.count >= 4, data[0] == 0x20 {  // CONNACK
                if data[3] == 0 {
                    self.connected = true
                    self.onStatus?("MQTT connected to \(self.host)")
                    self.startPing()
                } else {
                    self.onStatus?("MQTT refused (code \(data[3]))")
                }
            }
            if done || error != nil {
                self.scheduleReconnect()
            } else {
                self.receive()
            }
        }
    }

    private func startPing() {
        pingTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 30, repeating: 30)
        t.setEventHandler { [weak self] in self?.connection?.send(content: Data([0xC0, 0x00]), completion: .idempotent) }
        t.resume()
        pingTimer = t
    }

    private func sendConnect() {
        var vh = Data()
        vh.append(mqttString("MQTT"))
        vh.append(0x04)  // protocol level 3.1.1
        var flags: UInt8 = 0x02  // clean session
        if username != nil { flags |= 0x80 }
        if password != nil { flags |= 0x40 }
        vh.append(flags)
        vh.append(contentsOf: [0x00, 0x3C])  // keepalive 60s
        var payload = mqttString(clientID)
        if let u = username { payload.append(mqttString(u)) }
        if let p = password { payload.append(mqttString(p)) }
        connection?.send(content: packet(0x10, vh + payload), completion: .idempotent)
    }

    func publish(_ topic: String, _ message: String, retain: Bool = false) {
        queue.async {
            guard self.connected else { return }
            let body = self.mqttString(topic) + Data(message.utf8)
            self.connection?.send(content: self.packet(retain ? 0x31 : 0x30, body), completion: .idempotent)
        }
    }

    private func mqttString(_ s: String) -> Data {
        let bytes = Data(s.utf8)
        return Data([UInt8(bytes.count >> 8), UInt8(bytes.count & 0xFF)]) + bytes
    }

    private func packet(_ header: UInt8, _ body: Data) -> Data {
        var out = Data([header])
        var len = body.count
        repeat {
            var byte = UInt8(len % 128)
            len /= 128
            if len > 0 { byte |= 0x80 }
            out.append(byte)
        } while len > 0
        return out + body
    }
}
