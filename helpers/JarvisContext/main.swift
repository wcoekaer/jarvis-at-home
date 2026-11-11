// JarvisContext: snapshots the user's calendar, reminders and Mail unread
// count to ~/Library/Caches/jarvis/context.json for the Jarvis daemon.
//
// It runs as a small app bundle in the user's login session (LaunchAgent,
// every few minutes) because macOS only grants Calendar/Reminders access to
// bundled apps that can show a permission prompt; the boot-time daemon can't.

import AppKit
import EventKit
import Foundation

let store = EKEventStore()
let iso = ISO8601DateFormatter()
iso.formatOptions = [.withInternetDateTime]
iso.timeZone = TimeZone.current

func requestAccess(_ type: EKEntityType) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    let done: (Bool, Error?) -> Void = { ok, _ in granted = ok; sem.signal() }
    if type == .event {
        store.requestFullAccessToEvents(completion: done)
    } else {
        store.requestFullAccessToReminders(completion: done)
    }
    // Pump the run loop so a first-run permission prompt can be shown.
    while sem.wait(timeout: .now() + 0.1) == .timedOut {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    return granted
}

func events() -> [[String: Any]]? {
    guard requestAccess(.event) else { return nil }
    let cal = Calendar.current
    let now = Date()
    let end = cal.date(byAdding: .day, value: 2, to: cal.startOfDay(for: now))!  // through tomorrow
    let pred = store.predicateForEvents(withStart: cal.startOfDay(for: now), end: end, calendars: nil)
    return store.events(matching: pred)
        .filter { ev in
            // Skip invitations the user declined.
            let me = ev.attendees?.first(where: { $0.isCurrentUser })
            return me?.participantStatus != .declined
        }
        .sorted { $0.startDate < $1.startDate }
        .map { ev in
            var d: [String: Any] = [
                "title": ev.title ?? "(untitled)",
                "start": iso.string(from: ev.startDate),
                "end": iso.string(from: ev.endDate),
                "all_day": ev.isAllDay,
                "calendar": ev.calendar.title,
            ]
            if let loc = ev.location, !loc.isEmpty { d["location"] = loc }
            return d
        }
}

func reminders() -> [[String: Any]]? {
    guard requestAccess(.reminder) else { return nil }
    let cal = Calendar.current
    let endOfToday = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: Date()))!
    let pred = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: endOfToday, calendars: nil)
    let sem = DispatchSemaphore(value: 0)
    var out: [[String: Any]] = []
    store.fetchReminders(matching: pred) { items in
        for r in items ?? [] {
            var d: [String: Any] = ["title": r.title ?? "(untitled)", "list": r.calendar.title]
            if let comps = r.dueDateComponents, let due = cal.date(from: comps) {
                d["due"] = iso.string(from: due)
                d["overdue"] = due < cal.startOfDay(for: Date())
            }
            out.append(d)
        }
        sem.signal()
    }
    sem.wait()
    return out
}

func mailUnread() -> Int? {
    let running = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.apple.mail" }
    guard running else { return nil }
    var err: NSDictionary?
    let script = NSAppleScript(source: "tell application \"Mail\" to return unread count of inbox")
    let result = script?.executeAndReturnError(&err)
    if err != nil { return nil }
    return result.map { Int($0.int32Value) }
}

var snapshot: [String: Any] = ["generated": iso.string(from: Date())]
if let e = events() { snapshot["events"] = e } else { snapshot["calendar_access"] = "denied" }
if let r = reminders() { snapshot["reminders"] = r } else { snapshot["reminders_access"] = "denied" }
if let m = mailUnread() { snapshot["mail_unread_inbox"] = m }

let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/jarvis")
try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
let data = try JSONSerialization.data(withJSONObject: snapshot, options: [.prettyPrinted, .sortedKeys])
try data.write(to: dir.appendingPathComponent("context.json"), options: .atomic)
print(String(data: data, encoding: .utf8)!)
