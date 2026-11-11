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


if __name__ == "__main__":
    unittest.main()
