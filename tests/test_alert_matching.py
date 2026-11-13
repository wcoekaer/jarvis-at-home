import datetime as dt
import unittest
from pathlib import Path

import jarvis

ROOT = Path(__file__).resolve().parent.parent


def homekit(accessory, characteristic, text):
    return {"kind": "homekit", "accessory": accessory, "room": "Room",
            "characteristic": characteristic, "text": text}


class HomeKitRuleMatching(unittest.TestCase):
    """Plural filter keys (accessories, rooms, characteristics, texts) must apply.

    Regression: `accessories` was read as "accessorys", so the Starlink outage
    rule matched every open door or gate contact sensor."""

    def setUp(self):
        cfg = jarvis.load_config(ROOT / "config.example.yaml")
        self.alerter = jarvis.Alerter(cfg)
        self.now = dt.datetime(2026, 11, 11, 12, 0)

    def matching(self, ev):
        return [r["name"] for r in self.alerter.rules if self.alerter.matches(r, ev, self.now)]

    def test_open_door_is_not_a_starlink_outage(self):
        self.assertEqual(self.matching(homekit("USL Entry", "Mudroom Entry Contact Sensor State", "open")), [])
        self.assertEqual(self.matching(homekit("Meadow Gate Main Lock", "Contact Sensor State", "open")), [])

    def test_starlink_outage_flag(self):
        self.assertEqual(self.matching(homekit("Starlink Outage", "Contact Sensor State", "open")), ["starlink-outage"])
        self.assertEqual(self.matching(homekit("Starlink Outage", "Contact Sensor State", "closed")), [])


class ReadingRanges(unittest.TestCase):
    def test_wine_room_alerts_once_per_crossing(self):
        cfg = jarvis.load_config(ROOT / "config.example.yaml")
        m = jarvis.MqttState(dict(cfg["mqtt"], persist=False, discover=False))
        topic = "/unifi/protect/AABBCC000010/temperature"
        states = [[e["text"] for e in m.check_ranges(topic, c)] for c in ("19.7", "19.0", "17.5", "9.5", "11.0")]
        self.assertEqual(states, [["high"], [], ["ok"], ["low"], ["ok"]])


class UnifiAlarmTimestamps(unittest.TestCase):
    """UniFi sensors keep sending the time of their LAST tampering/alarm.
    Regression: an old, cleared tampering re-alerted (urgently) every time it was re-seen."""

    def test_only_fresh_timestamps_alert(self):
        import json
        cfg = jarvis.load_config(ROOT / "config.example.yaml")
        u = jarvis.UnifiTracker(cfg["mqtt"]["unifi"])
        now = dt.datetime(2026, 11, 11, 15, 0)
        msg = lambda when: json.dumps({"header": {"modelKey": "sensor", "id": "s1"},
                                       "payload": {"tamperingDetectedAt": int(when.timestamp() * 1000)}})
        topic = "/unifi/protect/x/telemetry"
        old, fresh = now - dt.timedelta(hours=5), now - dt.timedelta(seconds=30)
        self.assertEqual(u.handle(topic, msg(old), now), [])
        self.assertEqual(u.handle(topic, msg(old), now), [])
        self.assertEqual([e["kind"] for e in u.handle(topic, msg(fresh), now)], ["tamper"])
        self.assertEqual(u.handle(topic, msg(fresh), now), [])
        self.assertTrue(any("tampering" in l for l in u.summary(now)))
        self.assertFalse(any("tampering" in l for l in u.summary(now + dt.timedelta(hours=2))))


class TextNumbers(unittest.TestCase):
    """Regression: a sensor publishing its reading as plain text ("19.74", Celsius)
    skipped the C -> F conversion, so briefings called a 67 F wine room "19 degrees"."""

    def test_text_numbers_are_converted(self):
        spec = {"scale": 1.8, "offset": 32, "decimals": 0, "unit": "F"}
        self.assertEqual(jarvis.format_value("19.74", spec), "68 F")
        self.assertEqual(jarvis.format_value(19.74, spec), "68 F")
        self.assertEqual(jarvis.format_value("online", {}), "online")


class RainNowcastAlerts(unittest.TestCase):
    def payload(self, now, rain_from_min=None, raining=False):
        base = int(now.timestamp())
        minutely = [{"dt": base + 60 * i, "precipitation": (0.8 if (raining or (rain_from_min is not None and i >= rain_from_min)) else 0)}
                    for i in range(61)]
        return {"current": {"weather": [{"description": "clouds"}], **({"rain": {"1h": 0.5}} if raining else {})},
                "minutely": minutely}

    def test_announces_rain_once(self):
        import feeds
        now = dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.timezone.utc)
        f = feeds.RainNowcast({}, (0.0, 0.0)); f.state = {}; f.primed = True
        events = []; f.listeners.append(events.append)
        f.evaluate(self.payload(now, rain_from_min=12), now)
        self.assertEqual([e["minutes"] for e in events], ["12"])
        f.evaluate(self.payload(now + dt.timedelta(minutes=10), rain_from_min=2), now + dt.timedelta(minutes=10))
        self.assertEqual(len(events), 1)                       # no repeat within 2 hours
        f.evaluate(self.payload(now, raining=True), now)
        self.assertEqual(len(events), 1)                       # already raining: nothing new
        g = feeds.RainNowcast({}, (0.0, 0.0)); g.state = {}; g.primed = True
        g.listeners.append(events.append)
        g.evaluate(self.payload(now), now)
        self.assertEqual(len(events), 1)                       # dry hour
        self.assertIn("No rain expected", g.lines(now)[0])


if __name__ == "__main__":
    unittest.main()
