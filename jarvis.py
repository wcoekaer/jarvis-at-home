#!/usr/bin/env python3
"""Jarvis: scheduled spoken briefings for HomePods.

At each configured time, sends a prompt to Claude (Anthropic) or OpenAI,
converts the answer to speech, and streams it to one or more HomePods over
AirPlay (RAOP) using pyatv. Runs headless under launchd.

Usage:
    jarvis.py run                      # daemon: run the schedule forever
    jarvis.py devices                  # list AirPlay devices on the network
    jarvis.py test <broadcast> [--no-play]   # run one broadcast right now
    jarvis.py say "text" [--homepod NAME ...]  # speak arbitrary text
    jarvis.py next                     # show upcoming broadcasts
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

import feeds as feedmod

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.yaml"
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

log = logging.getLogger("jarvis")

SPEECH_SYSTEM_PROMPT = """\
You are Jarvis, the household's resident AI: a composed, impeccably polite \
British butler of an assistant. Your manner is calm, precise and quietly \
confident, with a dry, understated wit used sparingly. You are never gushing, \
never chatty, and never use exclamation marks. You may occasionally address the \
listener as "sir".

Your words will be converted to speech and played on HomePod speakers, so:
- Write plain spoken prose only: no markdown, no bullet characters, no headings, \
no emoji, no URLs, no citations or source lists.
- Spell things the way they should be spoken (say "62 degrees", "3 PM").
- Use British phrasing and spelling, but Fahrenheit and American units for weather.
- Vary your greetings and turns of phrase from one briefing to the next; never \
open the same way twice in a row.
- Do not mention that you searched the web or describe your process; just deliver \
the briefing.
"""


# --------------------------------------------------------------------------- config


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ (does not override existing vars)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("llm", {})
    cfg.setdefault("tts", {})
    cfg.setdefault("playback", {})
    cfg.setdefault("broadcasts", [])
    default_homepods = cfg.get("homepods") or []
    seen = set()
    for b in cfg["broadcasts"]:
        if cfg.get("prompt"):
            b.setdefault("prompt", cfg["prompt"])
        if "name" not in b or "prompt" not in b or not ("times" in b or "every_minutes" in b
                                                         or b.get("on_demand")):
            raise ValueError("each broadcast needs name, times (or every_minutes, or on_demand) "
                             "and prompt: %r" % b)
        if b["name"] in seen:
            raise ValueError("duplicate broadcast name: %s" % b["name"])
        seen.add(b["name"])
        b.setdefault("homepods", default_homepods)
        if not b["homepods"]:
            raise ValueError("broadcast %s has no homepods (set top-level 'homepods')" % b["name"])
        b["times"] = expand_times(b)
        days = b.get("days") or DAY_NAMES
        b["days"] = [DAY_NAMES.index(d.lower()[:3]) for d in days]
    return cfg


def expand_times(b: dict) -> List[dt.time]:
    """Explicit `times`, plus `every_minutes` between `start` and `end` (inclusive)."""
    times = [parse_hhmm(t) for t in b.get("times", [])]
    if b.get("every_minutes"):
        step = int(b["every_minutes"])
        start = parse_hhmm(b.get("start", "00:00"))
        end = parse_hhmm(b.get("end", "23:59"))
        minute = start.hour * 60 + start.minute
        while minute <= end.hour * 60 + end.minute:
            times.append(dt.time(minute // 60, minute % 60))
            minute += step
    return sorted(set(times))


def parse_hhmm(value) -> dt.time:
    # YAML 1.1 parses unquoted 07:30 as an int of minutes (sexagesimal).
    if isinstance(value, int):
        return dt.time(value // 60, value % 60)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m:
        raise ValueError("bad time %r, expected HH:MM" % value)
    return dt.time(int(m.group(1)), int(m.group(2)))


def setup_logging(cfg: dict, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    log_file = cfg.get("log_file")
    if log_file:
        path = Path(os.path.expanduser(log_file))
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("pyatv", "httpx", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- LLM


def part_of_day(now: dt.datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def name_instruction(cfg: dict) -> str:
    """Decide in code (not by the model) whether to use the listener's name,
    so it really does appear only some of the time."""
    name = cfg.get("listener_name")
    if name and random.random() < float(cfg.get("name_probability", 0.35)):
        return "Work the listener's name, %s, naturally into the greeting." % name
    return "Do not use the listener's name this time."


def flourish_instruction(cfg: dict) -> str:
    if random.random() < float(cfg.get("flourish_probability", 0.07)):
        return ("This time, add one brief flourish: a dry aside or a short, genuinely "
                "interesting bit of trivia tied to today's date or the news. One sentence.")
    return "No trivia or asides this time."


def segment_instructions(now: dt.datetime, cfg: dict) -> str:
    """Pick time-of-day specific content from config `segments` (first match wins)."""
    minute = now.hour * 60 + now.minute
    for seg in cfg.get("segments") or []:
        start, end = parse_hhmm(seg["from"]), parse_hhmm(seg["to"])
        if start.hour * 60 + start.minute <= minute <= end.hour * 60 + end.minute:
            return seg["instructions"].strip()
    return (cfg.get("default_segment") or "").strip()


def recent_briefings(now: dt.datetime, cfg: dict) -> str:
    """Earlier briefings from today, so the model can report only what changed."""
    archive = cfg.get("archive_dir")
    if not archive:
        return ""
    adir = Path(os.path.expanduser(archive))
    today = now.strftime("%Y%m%d")
    stamp_now = now.strftime("%Y%m%d-%H%M")
    files = sorted(p for p in adir.glob(today + "-*.txt") if p.name[:13] < stamp_now)
    files = files[-int(cfg.get("memory_count", 3)):]
    parts = []
    for p in files:
        t = dt.datetime.strptime(p.name[:13], "%Y%m%d-%H%M").strftime("%-I:%M %p")
        parts.append("[%s]\n%s" % (t, p.read_text().strip()))
    return "\n\n".join(parts)


def personal_context(now: dt.datetime, cfg: dict) -> str:
    """Calendar, reminders and mail from the JarvisContext helper's snapshot."""
    path = Path(os.path.expanduser(cfg.get("context_file", "~/Library/Caches/jarvis/context.json")))
    try:
        data = json.loads(path.read_text())
        generated = dt.datetime.fromisoformat(data["generated"]).replace(tzinfo=None)
    except (OSError, ValueError, KeyError):
        return ""
    if now - generated > dt.timedelta(minutes=30):
        log.warning("personal context is stale (%s); skipping", data["generated"])
        return ""

    def when(iso: str) -> str:
        t = dt.datetime.fromisoformat(iso).replace(tzinfo=None)
        day = "today" if t.date() == now.date() else "tomorrow"
        return "%s %s" % (day, t.strftime("%-I:%M %p"))

    lines = []
    events = [e for e in data.get("events", [])
              if dt.datetime.fromisoformat(e["end"]).replace(tzinfo=None) > now]
    if events:
        lines.append("Calendar (remaining today and tomorrow):")
        for e in events[:12]:
            if e.get("all_day"):
                lines.append("- all day %s: %s" % (when(e["start"]).split()[0], e["title"]))
            else:
                loc = " (at %s)" % e["location"] if e.get("location") else ""
                lines.append("- %s: %s%s" % (when(e["start"]), e["title"], loc))
    elif "events" in data:
        lines.append("Calendar: nothing scheduled for the rest of today or tomorrow.")
    reminders = data.get("reminders", [])
    if reminders:
        lines.append("Reminders due today or overdue:")
        for r in reminders[:10]:
            lines.append("- %s%s" % (r["title"], " (overdue)" if r.get("overdue") else ""))
    if "mail_unread_inbox" in data:
        lines.append("Unread messages in Mail inbox: %d" % data["mail_unread_inbox"])
    return "\n".join(lines)


class MqttState:
    """Keeps the latest value of each subscribed MQTT topic (vehicles, battery...)."""

    def __init__(self, mcfg: dict):
        self.cfg = mcfg
        self.values: Dict[str, Tuple[str, dt.datetime]] = {}
        self.lock = threading.Lock()
        self.client = None
        # Infrequent topics (vehicles) aren't always retained by the broker, so
        # remember the last values across restarts.
        self.cache = Path(os.path.expanduser(mcfg.get("cache_file", "~/Library/Caches/jarvis/mqtt.json")))
        self.last_save = 0.0
        # Discovery: catalog every topic on the broker so new data sources
        # (e.g. a weather station) can be spotted and added to `topics`.
        self.patterns = sorted({t["topic"] for t in mcfg.get("topics", [])})
        self.catalog_file = Path(os.path.expanduser(
            mcfg.get("catalog_file", "~/Library/Caches/jarvis/mqtt-topics.json")))
        self.catalog: Dict[str, dict] = {}
        try:
            self.catalog = json.loads(self.catalog_file.read_text())
        except (OSError, ValueError):
            pass
        self.listeners: List = []   # called (from the MQTT thread) with real-time events
        ucfg = mcfg.get("unifi") or {}
        self.unifi_topic = ucfg.get("topic", "/unifi/protect/+/telemetry")
        self.unifi_file = self.cache.with_name("unifi.json")
        try:
            ustate = json.loads(self.unifi_file.read_text())
        except (OSError, ValueError):
            ustate = None
        self.unifi = UnifiTracker(ucfg, ustate) if ucfg.get("enabled", True) else None
        icfg = mcfg.get("internet") or {}
        self.internet_topic = icfg.get("topic", "ping_ping")
        self.internet_file = self.cache.with_name("internet.json")
        try:
            istate = json.loads(self.internet_file.read_text())
        except (OSError, ValueError):
            istate = None
        self.internet = InternetMonitor(icfg, istate) if icfg.get("enabled", True) else None
        ecfg = mcfg.get("energy") or {}
        self.energy_topic = ecfg.get("topic", "/tesla/powerpack/state")
        self.energy = EnergyTracker(ecfg, self.cache.with_name("energy.json")) \
            if ecfg.get("enabled", True) else None
        if self.unifi:
            self.unifi.names.update({str(k): v for k, v in (ucfg.get("camera_names") or {}).items()})
        try:
            for topic, (payload, seen) in json.loads(self.cache.read_text()).items():
                self.values[topic] = (payload, dt.datetime.fromisoformat(seen))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if self.cfg.get("persist") is False or time.time() - self.last_save < 60:
            return
        self.last_save = time.time()
        with self.lock:
            data = {t: (v, seen.isoformat()) for t, (v, seen) in self.values.items()}
            catalog = json.loads(json.dumps(self.catalog))
            unifi = json.loads(json.dumps(self.unifi.state())) if self.unifi else {}
            internet = self.internet.to_state() if self.internet else {}
        if self.energy:
            try:
                with self.lock:
                    self.energy.save()
            except OSError as e:
                log.warning("could not save energy state: %s", e)
        for path, obj in ((self.cache, data), (self.catalog_file, catalog), (self.unifi_file, unifi),
                          (self.internet_file, internet)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
                tmp.replace(path)
            except OSError as e:
                log.warning("could not save %s: %s", path, e)

    def _record(self, topic: str, payload: str, now: dt.datetime) -> None:
        entry = self.catalog.get(topic)
        if entry is None:
            entry = self.catalog[topic] = {"first_seen": now.isoformat(timespec="seconds"), "count": 0}
            if self.cfg.get("discover", True):
                log.info("MQTT new topic: %s (%d bytes)", topic, len(payload))
        entry["count"] += 1
        entry["last_seen"] = now.isoformat(timespec="seconds")
        entry["sample"] = payload[:1500]

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=self.cfg.get("client_id", "jarvis-%d" % os.getpid()))
        user = self.cfg.get("username") or os.environ.get("MQTT_USERNAME")
        if user:
            c.username_pw_set(user, self.cfg.get("password") or os.environ.get("MQTT_PASSWORD"))
        if self.cfg.get("tls"):
            c.tls_set()
            if self.cfg.get("tls_insecure"):
                c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 60)
        c.connect_async(self.cfg.get("host") or os.environ["MQTT_HOST"],
                        int(self.cfg.get("port", 1883)), keepalive=60)
        c.loop_start()  # paho's own thread; reconnects automatically
        self.client = c

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        topics = list(self.patterns) + ([self.unifi_topic] if self.unifi else []) + \
            ([self.energy_topic] if self.energy else []) + \
            ([self.internet_topic] if self.internet else []) + \
            [self.cfg.get("say_topic", "jarvis/say"), self.cfg.get("brief_topic", "jarvis/brief"),
             self.cfg.get("homekit_events_topic", "homekit/events")]
        if self.cfg.get("discover", True):
            topics = ["#"] + [t for t in topics if t.startswith("$")]
        log.info("MQTT connected; subscribing to %s", ", ".join(topics))
        for t in topics:
            client.subscribe(t)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        now = dt.datetime.now()
        with self.lock:
            self._record(msg.topic, payload, now)
            events = []
            if self.unifi and topic_matches(self.unifi_topic, msg.topic):
                events = self.unifi.handle(msg.topic, payload, now)
            if msg.topic == self.cfg.get("say_topic", "jarvis/say") and payload:
                # plain text, or {"text": ..., "homepods": [...], "volume": 60, "urgent": false}
                try:
                    req = json.loads(payload)
                    req = req if isinstance(req, dict) else {"text": str(req)}
                except ValueError:
                    req = {"text": payload}
                if req.get("text"):
                    events = events + [dict(req, kind="say")]
            if msg.topic == self.cfg.get("brief_topic", "jarvis/brief"):
                try:
                    req = json.loads(payload) if payload.startswith("{") else {"broadcast": payload or None}
                except ValueError:
                    req = {}
                events = events + [dict(req, kind="brief")]
            if msg.topic == self.cfg.get("homekit_events_topic", "homekit/events"):
                try:
                    hk = json.loads(payload)
                    events = events + [{"kind": "homekit", "accessory": hk.get("accessory", ""),
                                        "room": hk.get("room", ""), "characteristic": hk.get("characteristic", ""),
                                        "text": str(hk.get("text", hk.get("value", "")))}]
                except ValueError:
                    pass
            if self.internet and msg.topic == self.internet_topic:
                events = events + self.internet.update(payload, now)
            if self.energy and msg.topic == self.energy_topic:
                self.energy.update(payload, now)
                events = events + self.energy.check_thresholds(payload)
        for ev in events:
            # HomeKit changes are frequent; the alerter logs the ones it acts on.
            (log.debug if ev["kind"] == "homekit" else log.info)("event: %s", ev)
            for listener in self.listeners:
                listener(ev)
        if not any(topic_matches(p, msg.topic) for p in self.patterns):
            self._save()
            return
        status_key = msg.topic + STATUS_SUFFIX
        try:
            parsed = json.loads(payload)
            is_error = isinstance(parsed, dict) and bool(parsed.get("error")) and not parsed.get("response")
        except ValueError:
            is_error = False
        with self.lock:
            if is_error and msg.topic in self.values:
                # e.g. a sleeping car: keep its last real data, note the status separately
                self.values[status_key] = (payload, now)
            else:
                self.values[msg.topic] = (payload, now)
                if not is_error:
                    self.values.pop(status_key, None)
        self._save()

    def summary(self, now: dt.datetime) -> str:
        with self.lock:
            values = dict(self.values)
        default_age = int(self.cfg.get("max_age_minutes", 720))
        lines = []
        for spec in self.cfg.get("topics", []):
            max_age = dt.timedelta(minutes=int(spec.get("max_age_minutes", default_age)))
            fresh = {t: v for t, (v, seen) in values.items() if now - seen <= max_age}
            matched = sorted(t for t in fresh if topic_matches(spec["topic"], t))
            label = spec.get("label", spec["topic"])
            if "count_value" in spec:
                # e.g. how many of /unifi/protect/+/availability are "offline"
                n = sum(1 for t in matched if fresh[t] == str(spec["count_value"]))
                lines.append("- %s: %d of %d" % (label, n, len(matched)))
                continue
            # Optional filters for busy wildcard sources (e.g. HomeKit): topic must
            # contain one of `include`, and the value must be one of `only_values`.
            if spec.get("include"):
                matched = [t for t in matched if any(s in t for s in spec["include"])]
            if spec.get("exclude"):
                matched = [t for t in matched if not any(s in t for s in spec["exclude"])]
            spec_lines = []
            for topic in matched:
                value = format_value(extract_field(fresh[topic], spec.get("field")), spec)
                if value is None:
                    continue
                if spec.get("only_values") and value not in spec["only_values"]:
                    continue
                name = label
                if spec.get("topic_label_parts"):
                    name = "%s: %s" % (label, " / ".join(topic.split("/")[-int(spec["topic_label_parts"]):]))
                elif len(matched) > 1 or "+" in spec["topic"] or "#" in spec["topic"]:
                    name = "%s [%s]" % (label, topic.replace(STATUS_SUFFIX, "").rsplit("/", 1)[-1])
                spec_lines.append("- %s: %s" % (name, value))
            lines += spec_lines[:int(spec.get("max_lines", 40))]
        if self.unifi:
            # Network/security lines first so a long list elsewhere can't push them out.
            with self.lock:
                cams = []
                for mac, name in sorted(self.unifi.camera_macs.items(), key=lambda kv: kv[1]):
                    avail = values.get("/unifi/protect/%s/availability" % mac)
                    if avail:
                        cams.append("- Camera %s: %s" % (name, "OFFLINE" if avail[0] == "offline" else "online"))
                lines = self.unifi.summary(now) + cams + lines
        if self.internet:
            with self.lock:
                lines = self.internet.summary(now) + lines
        return "\n".join(lines[:int(self.cfg.get("max_lines", 100))])


def format_value(value, spec: dict) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        decimals = int(spec.get("decimals", 1))
        value = round(value * float(spec.get("scale", 1)) + float(spec.get("offset", 0)), decimals)
        if decimals == 0:
            value = int(value)
    elif isinstance(value, (dict, list)):
        value = json.dumps(value)[:300]
    unit = spec.get("unit", "")
    return "%s%s" % (value, (" " + unit) if unit else "")


class UnifiTracker:
    """Builds state from UniFi Protect telemetry (a stream of partial updates
    of many kinds) published on /unifi/protect/<console-mac>/telemetry."""

    def __init__(self, ucfg: dict, state: Optional[dict] = None):
        self.cfg = ucfg
        st = state or {}
        self.wan = st.get("wan", {})          # console -> {port_id: {type, plugged, enabled}}
        self.wan_active = st.get("wan_active", {})  # console -> [port ids]
        self.raid_sync = st.get("raid_sync", {})    # console -> {progress, eta_ms, seen}
        self.storage = st.get("storage", {})        # console -> [problems]
        self.low_batt = st.get("low_batt", {})      # sensor id -> seen
        self.alarms = st.get("alarms", {})          # "id kind" -> iso time
        self.names = st.get("names", {})            # device id -> name
        self.detections = st.get("detections", {})  # event id -> [iso start, types, camera]
        self.wan_history = st.get("wan_history", [])  # [iso, port, state]
        self.daily_counts = st.get("daily_counts", {})  # date -> {camera name: {type: n}}
        # Camera names by MAC (config); events only carry Protect's internal id,
        # so learn MAC -> id whenever a camera record includes its MAC.
        self.camera_macs = {norm_mac(k): v for k, v in (ucfg.get("camera_macs") or {}).items()}
        self.mac_ids = st.get("mac_ids", {})          # MAC -> device id
        for mac, dev in self.mac_ids.items():
            if mac in self.camera_macs:
                self.names[dev] = self.camera_macs[mac]
        # Names set by id in config always win over names UniFi sends or MAC learning.
        self.pinned = {str(k): v for k, v in (ucfg.get("camera_names") or {}).items()}
        self.names.update(self.pinned)

    def state(self) -> dict:
        return {k: getattr(self, k) for k in ("wan", "wan_active", "raid_sync", "storage",
                                              "low_batt", "alarms", "names", "detections", "wan_history",
                                              "daily_counts",
                                              "mac_ids")}

    def wan_states(self) -> Dict[str, Tuple[str, str]]:
        """{console/port: (WAN label, state)} with state online/offline/disabled."""
        out = {}
        ignore = set(self.cfg.get("ignore_wan", []))
        for console, ports in self.wan.items():
            for pid, p in ports.items():
                if p["type"] in ignore or pid in ignore:
                    continue
                if not p["enabled"]:
                    state = "disabled"
                elif not p["plugged"]:
                    # Only link loss counts: the console's wanPorts list doesn't mean
                    # "online" (UniFi showed WAN4 online while absent from it).
                    # Internet reachability comes from the ping monitor instead.
                    state = "offline"
                else:
                    state = "online"
                out["%s/%s" % (console, pid)] = (p["type"], state)
        return out

    def handle(self, topic: str, payload: str, now: dt.datetime) -> List[dict]:
        """Update state; return real-time events (detections, WAN changes, alarms)."""
        events: List[dict] = []
        try:
            msg = json.loads(payload)
        except ValueError:
            return events
        header, body = msg.get("header") or {}, msg.get("payload")
        if not isinstance(body, dict) or "allPermissions" in body:
            return events
        wan_before = self.wan_states()
        console = topic.split("/")[3] if topic.count("/") >= 4 else topic
        dev_id = header.get("id", "")
        model = header.get("modelKey", "")
        if body.get("name") and dev_id and dev_id not in self.pinned:
            self.names[dev_id] = body["name"]
        if body.get("mac") and dev_id:
            mac = norm_mac(body["mac"])
            if self.mac_ids.get(mac) != dev_id:
                self.mac_ids[mac] = dev_id
                if mac in self.camera_macs and dev_id not in self.pinned:
                    self.names[dev_id] = self.camera_macs[mac]
                    log.info("UniFi: learned camera %s = %s (MAC %s)", dev_id, self.camera_macs[mac], mac)

        if model == "nvr":
            ports = body.get("portStatus")
            if ports:
                self.wan[console] = {p["id"]: {"type": p.get("type"), "plugged": p.get("plugged"),
                                               "enabled": p.get("enabled")}
                                     for p in ports if str(p.get("type", "")).startswith("WAN")}
            if "wanPorts" in body and (body["wanPorts"] or console in self.wan):
                self.wan_active[console] = body["wanPorts"]
            if body.get("syncingProgress") is not None:
                self.raid_sync[console] = {"progress": body["syncingProgress"],
                                           "eta_ms": body.get("syncingEstimateMs"),
                                           "seen": now.isoformat()}
            sysinfo = body.get("systemInfo") or {}
            if sysinfo.get("ustorage") or sysinfo.get("storage"):
                problems = []
                for d in (sysinfo.get("ustorage") or {}).get("disks", []):
                    if d.get("state") == "nodisk":  # empty bay
                        continue
                    if d.get("state") not in (None, "normal") or d.get("healthy") not in (None, "good"):
                        problems.append("disk slot %s is %s/%s" % (d.get("slot"), d.get("state"), d.get("healthy")))
                for sp in (sysinfo.get("ustorage") or {}).get("space", []):
                    if sp.get("health") not in (None, "health"):
                        problems.append("%s volume health %s" % (sp.get("space_type"), sp.get("health")))
                cap = (sysinfo.get("storage") or {}).get("capability")
                if cap not in (None, "ok"):
                    problems.append("storage capability %s" % cap)
                level = (sysinfo.get("degradation") or {}).get("level")
                if level:
                    problems.append("console degradation level %s" % level)
                self.storage[console] = problems

        batt = body.get("batteryStatus")
        if isinstance(batt, dict) and dev_id:
            if batt.get("isLow"):
                self.low_batt[dev_id] = now.isoformat()
            else:
                self.low_batt.pop(dev_id, None)
        for kind in ("smokeAlarmTriggeredAt", "coAlarmTriggeredAt", "leakDetectedAt",
                     "alarmTriggeredAt", "tamperingDetectedAt"):
            if body.get(kind) and dev_id:
                key = "%s %s" % (dev_id, kind)
                if key not in self.alarms:
                    events.append({"kind": "alarm", "device": dev_id,
                                   "device_name": self.names.get(dev_id, "a sensor"),
                                   "detail": ALARM_NAMES[kind]})
                self.alarms[key] = now.isoformat()

        types = body.get("smartDetectTypes")
        if model == "event" or body.get("type") in ("smartDetectZone", "ring", "smartDetectLine"):
            if body.get("type") == "ring":
                types = ["doorbell ring"]
            allowed = self.cfg.get("detection_types", ["person"])
            types = [t for t in (types or []) if t in allowed]
            # Cameras without person detection (e.g. older ones on the NVR) only
            # send plain motion events; opt them in by id or name.
            cam_id = body.get("camera") or (self.detections.get(dev_id) or ["", [], ""])[2]
            motion_cams = set(self.cfg.get("motion_cameras") or [])
            if (not types and body.get("type") == "motion" and motion_cams
                    and (cam_id in motion_cams or self.names.get(cam_id) in motion_cams)):
                types = ["motion"]
            if types and dev_id:
                start = body.get("start")
                when = dt.datetime.fromtimestamp(start / 1000) if start else now
                camera = body.get("camera") or (self.detections.get(dev_id) or ["", [], ""])[2]
                known = self.detections.get(dev_id)
                new_types = sorted(set(types) - set(known[1] if known else []))
                self.detections[dev_id] = [when.isoformat(), sorted(set(types) | set(known[1] if known else [])), camera]
                if new_types:
                    day = self.daily_counts.setdefault(when.date().isoformat(), {})
                    cam_counts = day.setdefault(self.names.get(camera) or "camera " + camera[-4:], {})
                    for t in new_types:
                        cam_counts[t] = cam_counts.get(t, 0) + 1
                    keep = (now.date() - dt.timedelta(days=14)).isoformat()
                    self.daily_counts = {d: c for d, c in self.daily_counts.items() if d >= keep}
                # Event updates repeat; only announce types not yet seen for this event.
                if new_types and now - when < dt.timedelta(minutes=2):
                    events.append({"kind": "detection", "camera": camera,
                                   "camera_name": self.names.get(camera, ""), "types": new_types})

        # prune old detections and alarms
        cutoff = (now - dt.timedelta(hours=24)).isoformat()
        self.detections = {k: v for k, v in self.detections.items() if v[0] >= cutoff}
        self.alarms = {k: v for k, v in self.alarms.items() if v >= cutoff}

        for key, (label, state) in self.wan_states().items():
            before = wan_before.get(key)
            if before and before[1] != state and state in ("online", "offline"):
                events.append({"kind": "wan_" + state, "port": label})
                self.wan_history.append([now.isoformat(), label, state])
        self.wan_history = [h for h in self.wan_history if h[0] >= cutoff]
        return events


    def overnight(self, now: dt.datetime, start: dt.time, end: dt.time) -> str:
        """Security recap for the most recent overnight window (e.g. 21:00-07:00)."""
        end_dt = dt.datetime.combine(now.date(), end)
        if end_dt > now:
            end_dt = now
        start_dt = dt.datetime.combine(end_dt.date(), start)
        if start_dt >= end_dt:
            start_dt -= dt.timedelta(days=1)
        lines = []
        by_cam: Dict[str, List[str]] = {}
        for when, types, cam in sorted(self.detections.values()):
            t = dt.datetime.fromisoformat(when)
            if start_dt <= t <= end_dt:
                name = self.names.get(cam) or "camera %s" % cam[-4:]
                by_cam.setdefault(name, []).append("%s (%s)" % (t.strftime("%-I:%M %p"), "/".join(types)))
        for name, items in sorted(by_cam.items()):
            lines.append("- %s: %d detection(s) at %s" % (name, len(items), ", ".join(items[:8])))
        for when, port, state in self.wan_history:
            t = dt.datetime.fromisoformat(when)
            if start_dt <= t <= end_dt:
                lines.append("- Internet %s went %s at %s" % (port, state, t.strftime("%-I:%M %p")))
        for key, when in self.alarms.items():
            t = dt.datetime.fromisoformat(when)
            if start_dt <= t <= end_dt:
                dev, kind = key.split(" ", 1)
                lines.append("- ALARM %s on %s at %s" % (ALARM_NAMES.get(kind, kind), self.names.get(dev, dev[-4:]),
                                                         t.strftime("%-I:%M %p")))
        header = "Overnight %s to %s:" % (start_dt.strftime("%-I:%M %p"), end_dt.strftime("%-I:%M %p"))
        return header + "\n" + ("\n".join(lines) if lines else "- nothing recorded: no people detected, no outages, no alarms")

    def summary(self, now: dt.datetime) -> List[str]:
        lines = []
        for key, (label, state) in sorted(self.wan_states().items(), key=lambda kv: kv[1][0]):
            lines.append("- Internet %s (%s): %s" % (label, key.split("/")[1],
                                                     "OFFLINE" if state == "offline" else state))
        for console, rs in self.raid_sync.items():
            if now - dt.datetime.fromisoformat(rs["seen"]) < dt.timedelta(minutes=30):
                eta = (rs.get("eta_ms") or 0) / 3_600_000
                lines.append("- UniFi recorder %s RAID rebuild in progress: %d%%, about %.0f hours remaining"
                             % (console[-4:], rs["progress"] * 100, eta))
        for console, problems in self.storage.items():
            for prob in problems:
                lines.append("- UniFi recorder %s storage problem: %s" % (console[-4:], prob))
        for dev in self.low_batt:
            lines.append("- UniFi sensor battery low: %s" % self.names.get(dev, "sensor " + dev[-4:]))
        for key, when in self.alarms.items():
            dev, kind = key.split(" ", 1)
            lines.append("- ALARM: %s on %s at %s" % (kind.replace("At", ""), self.names.get(dev, dev[-4:]),
                                                     dt.datetime.fromisoformat(when).strftime("%-I:%M %p")))
        window = now - dt.timedelta(minutes=int(self.cfg.get("detection_window_minutes", 30)))
        counts: Dict[str, int] = {}
        for when, types, cam in self.detections.values():
            if dt.datetime.fromisoformat(when) >= window:
                for t in types:
                    counts[t] = counts.get(t, 0) + 1
        if counts:
            lines.append("- Camera detections in the last %d minutes: %s" % (
                int(self.cfg.get("detection_window_minutes", 30)),
                ", ".join("%s x%d" % (t, n) for t, n in sorted(counts.items()))))
        return lines


class EnergyTracker:
    """Daily energy totals from the Powerpack's lifetime counters (Wh):
    solar produced, battery charged (imported) and discharged (exported).
    The house is off-grid, so house use = solar + discharged - charged."""

    def __init__(self, ecfg: dict, path: Path):
        self.cfg = ecfg
        self.path = path
        try:
            self.state = json.loads(path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.latest: Optional[dict] = None

    def update(self, payload: str, now: dt.datetime) -> None:
        try:
            st = json.loads(payload)
            m = st["meters"]
            load_w = float(m["solar"]["powerW"]) + float(m["battery"]["powerW"])  # off-grid: no grid term
            self.loads = [(t, w) for t, w in getattr(self, "loads", []) if now - t < dt.timedelta(hours=1)]
            self.loads.append((now, max(0.0, load_w)))
            self.remaining_wh = float(st["energyRemainingWh"])
            self.full_wh = float(st["nominalFullPackEnergyWh"])
            self.solar_w = float(m["solar"]["powerW"])
        except (ValueError, KeyError, TypeError):
            pass
        try:
            meters = json.loads(payload)["meters"]
            counters = {"solar": meters["solar"]["energyExportedWh"],
                        "charged": meters["battery"]["energyImportedWh"],
                        "discharged": meters["battery"]["energyExportedWh"]}
        except (ValueError, KeyError, TypeError):
            return
        if not all(isinstance(v, (int, float)) and v > 0 for v in counters.values()):
            return
        today = now.date().isoformat()
        if self.state.get("date") != today:
            # New day: yesterday's totals are the last reading minus its baseline.
            if self.state.get("date") and self.state.get("last"):
                self.state["yesterday"] = dict(self.totals(self.state["start"], self.state["last"]),
                                               date=self.state["date"], since=self.state.get("since"))
                # keep two weeks of daily totals for the weekly recap
                hist = [h for h in self.state.get("history", []) if h.get("date") != self.state["date"]]
                self.state["history"] = (hist + [self.state["yesterday"]])[-14:]
            self.state.update(date=today, start=counters, since=now.isoformat(timespec="minutes"))
        self.state["last"] = counters
        self.latest = counters

    def check_thresholds(self, payload: str) -> List[dict]:
        """Fire once when the Powerpack drops to or below each threshold;
        re-arm after it recovers `rearm` points above it."""
        try:
            level = float(json.loads(payload)["stateOfEnergyPercent"])
        except (ValueError, KeyError, TypeError):
            return []
        events = []
        rearm = float(self.cfg.get("rearm_points", 3))
        fired = self.state.setdefault("thresholds_fired", [])
        for th in sorted(self.cfg.get("thresholds", [50, 30]), reverse=True):
            if level <= th and th not in fired:
                fired.append(th)
                events.append({"kind": "battery_low", "threshold": th, "level": round(level)})
            elif level > th + rearm and th in fired:
                fired.remove(th)
        # Only announce the most severe threshold crossed in one step.
        return events[-1:]

    @staticmethod
    def totals(start: dict, end: dict) -> dict:
        kwh = {k: max(0.0, (end[k] - start[k]) / 1000.0) for k in ("solar", "charged", "discharged")}
        kwh["house"] = max(0.0, kwh["solar"] + kwh["discharged"] - kwh["charged"])
        return kwh

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.path)

    def summary(self, now: dt.datetime) -> str:
        if not self.state.get("last") or self.state.get("date") != now.date().isoformat():
            return self.runway()
        t = self.totals(self.state["start"], self.state["last"])
        since = dt.datetime.fromisoformat(self.state["since"])
        partial = "" if since.hour == 0 and since.minute < 15 else \
            " (measured since %s only)" % since.strftime("%-I:%M %p")
        lines = ["Today so far%s: solar produced %.0f kWh; battery charged %.0f kWh and "
                 "discharged %.0f kWh; house used %.0f kWh." % (partial, t["solar"], t["charged"],
                                                                t["discharged"], t["house"])]
        y = self.state.get("yesterday")
        if y:
            lines.append("Yesterday: solar %.0f kWh, house %.0f kWh." % (y["solar"], y["house"]))
        runway = self.runway()
        if runway:
            lines.append(runway)
        return "\n".join(lines)

    def runway(self) -> str:
        """Hours the Powerpack lasts at the last hour's average house load,
        ignoring any further solar (i.e. as if the sun went down now)."""
        loads = getattr(self, "loads", [])
        if not loads or not getattr(self, "remaining_wh", None):
            return ""
        avg_kw = sum(w for _, w in loads) / len(loads) / 1000.0
        if avg_kw < 0.05:
            return ""
        reserve = float(self.cfg.get("runway_reserve_percent", 20)) / 100.0
        to_reserve = max(0.0, self.remaining_wh / 1000.0 - reserve * self.full_wh / 1000.0)
        return ("Battery runway: %.0f kWh left; at the last hour's average house load of %.1f kW "
                "that is about %.0f hours to %d%% (%.0f hours to empty), not counting solar; "
                "solar right now %.1f kW." % (self.remaining_wh / 1000.0, avg_kw, to_reserve / avg_kw,
                                              reserve * 100, self.remaining_wh / 1000.0 / avg_kw,
                                              getattr(self, "solar_w", 0.0) / 1000.0))


class InternetMonitor:
    """Internet reachability from the telegraf ping data (ping_ping, ~2/s to
    1.1.1.1 from the ping collector host). States: up, degraded, down; 'stale' when the ping
    data itself stops (ping collector problem, not the internet)."""

    def __init__(self, icfg: dict, state: Optional[dict] = None):
        self.cfg = icfg
        self.samples: List[Tuple[float, bool, Optional[float]]] = []  # (ts, ok, rtt ms)
        st = state or {}
        self.state = st.get("state", "up")
        self.down_since: Optional[str] = st.get("down_since")
        self.history: List[list] = st.get("history", [])  # [iso start, iso end or None, kind]
        self.ok_streak = 0
        self.fail_streak_since: Optional[float] = None
        self.last_seen = 0.0

    def to_state(self) -> dict:
        return {"state": self.state, "down_since": self.down_since, "history": self.history}

    def update(self, payload: str, now: dt.datetime) -> List[dict]:
        try:
            f = json.loads(payload)["fields"]
            ok = int(f.get("packets_received", 0)) > 0 and int(f.get("result_code", 0)) == 0
            rtt = float(f["average_response_ms"]) if ok and "average_response_ms" in f else None
        except (ValueError, KeyError, TypeError):
            return []
        t = now.timestamp()
        self.last_seen = t
        self.samples.append((t, ok, rtt))
        window = float(self.cfg.get("degraded_window_seconds", 300))
        self.samples = [x for x in self.samples if t - x[0] <= window]
        events = []
        if ok:
            self.ok_streak += 1
            self.fail_streak_since = None
        else:
            self.ok_streak = 0
            self.fail_streak_since = self.fail_streak_since or t

        down_after = float(self.cfg.get("down_after_seconds", 15))
        if self.state != "down" and self.fail_streak_since and t - self.fail_streak_since >= down_after:
            if self.history and self.history[-1][1] is None:  # close a 'degraded' spell
                self.history[-1][1] = now.isoformat(timespec="seconds")
            self.state = "down"
            self.down_since = dt.datetime.fromtimestamp(self.fail_streak_since).isoformat(timespec="seconds")
            self.history.append([self.down_since, None, "outage"])
            events.append({"kind": "internet_down"})
        elif self.state == "down" and self.ok_streak >= int(self.cfg.get("up_after_successes", 10)):
            start = dt.datetime.fromisoformat(self.down_since) if self.down_since else now
            mins = max(1, round((now - start).total_seconds() / 60))
            if self.history and self.history[-1][1] is None:
                self.history[-1][1] = now.isoformat(timespec="seconds")
            self.state, self.down_since = "up", None
            self.samples = [x for x in self.samples if x[1]]  # don't count the outage as 'degraded'
            events.append({"kind": "internet_up", "detail": "%d minute%s" % (mins, "" if mins == 1 else "s")})
        elif self.state in ("up", "degraded") and len(self.samples) >= 60 and ok:
            # Judge 'degraded' only on a successful sample, so the first seconds of
            # an outage (a run of failures) don't raise 'degraded' just before 'down'.
            loss, rtt = self.stats()
            bad = loss >= float(self.cfg.get("degraded_loss_percent", 10)) or \
                (rtt or 0) >= float(self.cfg.get("degraded_latency_ms", 150))
            if bad and self.state == "up":
                self.state = "degraded"
                self.history.append([now.isoformat(timespec="seconds"), None, "degraded"])
                events.append({"kind": "internet_degraded",
                               "detail": "%.0f percent packet loss, %.0f milliseconds" % (loss, rtt or 0)})
            elif not bad and self.state == "degraded":
                self.state = "up"
                if self.history and self.history[-1][1] is None:
                    self.history[-1][1] = now.isoformat(timespec="seconds")
        cutoff = (now - dt.timedelta(days=2)).isoformat()
        self.history = [h for h in self.history if (h[1] or h[0]) >= cutoff]
        return events

    def stats(self) -> Tuple[float, Optional[float]]:
        if not self.samples:
            return 0.0, None
        loss = 100.0 * sum(1 for _, ok, _ in self.samples if not ok) / len(self.samples)
        rtts = [r for _, ok, r in self.samples if ok and r is not None]
        return loss, (sum(rtts) / len(rtts) if rtts else None)

    def summary(self, now: dt.datetime) -> List[str]:
        if not self.last_seen:
            return []
        if now.timestamp() - self.last_seen > float(self.cfg.get("stale_seconds", 180)):
            return ["- Internet check: NO PING DATA for %d minutes (ping collector problem, "
                    "not necessarily the internet)" % ((now.timestamp() - self.last_seen) // 60)]
        loss, rtt = self.stats()
        state = {"up": "online", "degraded": "DEGRADED", "down": "DOWN"}[self.state]
        line = "- Internet (ping to 1.1.1.1): %s, %.0f%% loss, %s average over 5 minutes" % (
            state, loss, "%.0f ms" % rtt if rtt is not None else "no replies")
        if self.state == "down" and self.down_since:
            line += ", down since %s" % dt.datetime.fromisoformat(self.down_since).strftime("%-I:%M %p")
        today = now.date().isoformat()
        outages = [h for h in self.history if h[2] == "outage" and h[0][:10] == today]
        if outages:
            line += "; %d outage(s) today" % len(outages)
        return [line]

    def recap(self, start: dt.datetime, end: dt.datetime) -> List[str]:
        lines = []
        for s_iso, e_iso, kind in self.history:
            s_t = dt.datetime.fromisoformat(s_iso)
            if start <= s_t <= end:
                e_t = dt.datetime.fromisoformat(e_iso) if e_iso else None
                dur = " for %d minutes" % max(1, round((e_t - s_t).total_seconds() / 60)) if e_t else " (ongoing)"
                lines.append("- Internet %s at %s%s" % ("outage" if kind == "outage" else "degraded",
                                                         s_t.strftime("%-I:%M %p"), dur))
        return lines


def norm_mac(mac: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", str(mac)).upper()


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def extract_field(payload: str, field: Optional[str]):
    """Return the payload, or a dotted field from a JSON payload."""
    if not field:
        return payload[:200] if payload else None
    try:
        value = json.loads(payload)
        for key in field.split("."):
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (ValueError, KeyError, IndexError, TypeError):
        return None


MQTT: Optional[MqttState] = None
ALARM_NAMES = {"smokeAlarmTriggeredAt": "smoke alarm", "coAlarmTriggeredAt": "carbon monoxide alarm",
               "leakDetectedAt": "water leak", "alarmTriggeredAt": "alarm",
               "tamperingDetectedAt": "tampering"}
STATUS_SUFFIX = " (status)"


def render_prompt(template: str, now: dt.datetime, cfg: dict) -> str:
    recent = recent_briefings(now, cfg)
    personal = personal_context(now, cfg)
    home = MQTT.summary(now) if MQTT else ""
    overnight, energy = "", ""
    if MQTT and MQTT.unifi:
        ov = cfg.get("overnight") or {}
        with MQTT.lock:
            overnight = MQTT.unifi.overnight(now, parse_hhmm(ov.get("from", "21:00")),
                                             parse_hhmm(ov.get("to", "07:00")))
            if MQTT.internet:
                end_t = min(now, dt.datetime.combine(now.date(), parse_hhmm(ov.get("to", "07:00"))))
                start_t = dt.datetime.combine(end_t.date(), parse_hhmm(ov.get("from", "21:00")))
                if start_t >= end_t:
                    start_t -= dt.timedelta(days=1)
                extra = MQTT.internet.recap(start_t, end_t)
                if extra:
                    overnight = overnight.replace("- nothing recorded: no people detected, no outages, no alarms",
                                                  "").rstrip() + "\n" + "\n".join(extra)
    if MQTT and MQTT.energy:
        with MQTT.lock:
            energy = MQTT.energy.summary(now)
    drones = ""
    if AIRSPACE:
        drones = AIRSPACE.summary(now, now - dt.timedelta(hours=2))
        ov = cfg.get("overnight") or {}
        end_t = min(now, dt.datetime.combine(now.date(), parse_hhmm(ov.get("to", "07:00"))))
        start_t = dt.datetime.combine(end_t.date(), parse_hhmm(ov.get("from", "21:00")))
        if start_t >= end_t:
            start_t -= dt.timedelta(days=1)
        night = AIRSPACE.sighting_lines(start_t, end_t)
        if night and overnight:
            overnight = overnight.replace("- nothing recorded: no people detected, no outages, no alarms",
                                          "").rstrip() + "\n" + "\n".join(
                "- Drone " + l[2:] for l in night)
    values = {
        "date": now.strftime("%A, %B %-d, %Y"),
        "time": now.strftime("%-I:%M %p"),
        "weekday": now.strftime("%A"),
        "part_of_day": part_of_day(now),
        "location": cfg.get("location", ""),
        "name_instruction": name_instruction(cfg),
        "flourish_instruction": flourish_instruction(cfg),
        "segment_instructions": segment_instructions(now, cfg),
        "recent_briefings": recent or "(none yet today: this is the first update of the day)",
        "personal_context": personal or "(not available)",
        "home_status": home or "(not available)",
        "overnight_security": overnight or "(not available)",
        "energy_today": energy or "(not available)",
        "drones": drones or "(not available)",
        "traffic": (TRAFFIC.summary() if TRAFFIC else "") or "(not available)",
        "hazards": feed_lines(["weather_alerts", "earthquakes", "wildfires", "outdoor_air"], now, {
            "weather_alerts": "National Weather Service alerts for the house",
            "earthquakes": "Notable earthquakes, last 24 hours (USGS)",
            "wildfires": "Wildfires within %s miles (NIFC/CAL FIRE)" % (
                (cfg.get("feeds") or {}).get("wildfires", {}).get("watch_miles", 60)),
            "outdoor_air": "Outdoor air quality"}) or "(not available)",
        "airspace_notices": feed_lines(["flight_restrictions", "low_aircraft"], now, {
            "flight_restrictions": "Flight restrictions near home (TFRs, drone NOTAMs)",
            "low_aircraft": "Low aircraft near the house, last 2 hours"}) or "(not available)",
        "weekly_summary": weekly_summary(now) if "{weekly_summary}" in template else "",
    }
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out


def ask_anthropic(prompt: str, llm: dict) -> str:
    import anthropic

    client = anthropic.Anthropic(max_retries=3)
    tools = []
    if llm.get("web_search", True):
        tool = {"type": "web_search_20260209", "name": "web_search",
                "max_uses": int(llm.get("max_searches", 8))}
        if llm.get("user_location"):
            tool["user_location"] = dict(llm["user_location"], type="approximate")
        tools.append(tool)

    messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(6):  # resume server-tool loops that hit pause_turn
        with client.beta.messages.stream(
            model=llm.get("model", "claude-opus-5-5"),
            max_tokens=int(llm.get("max_tokens", 16000)),
            system=SPEECH_SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": llm.get("effort", "medium")},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": response.content}]

    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request: %s" % (response.stop_details,))
    # With web search, the answer is interleaved with tool blocks; the spoken
    # answer is the text that follows the last tool result.
    blocks = list(response.content)
    last_tool = max((i for i, b in enumerate(blocks)
                     if b.type in ("server_tool_use", "web_search_tool_result")), default=-1)
    text = "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    if not text:
        text = "".join(b.text for b in blocks if b.type == "text").strip()
    return text


def ask_openai(prompt: str, llm: dict) -> str:
    import openai

    client = openai.OpenAI(max_retries=3)
    tools = []
    if llm.get("web_search", True):
        tool = {"type": "web_search"}
        if llm.get("user_location"):
            tool["user_location"] = dict(llm["user_location"], type="approximate")
        tools.append(tool)
    resp = client.responses.create(
        model=llm.get("model", "gpt-5"),
        instructions=SPEECH_SYSTEM_PROMPT,
        input=prompt,
        tools=tools,
    )
    return resp.output_text.strip()


def clean_for_speech(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # markdown links
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>]+", "", text)                       # markdown marks
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)        # bullets
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def generate_text(prompt: str, cfg: dict) -> str:
    llm = cfg["llm"]
    provider = llm.get("provider", "anthropic").lower()
    log.info("asking %s (%s)", provider, llm.get("model", "default"))
    if provider == "anthropic":
        text = ask_anthropic(prompt, llm)
    elif provider == "openai":
        text = ask_openai(prompt, llm)
    else:
        raise ValueError("unknown llm.provider %r" % provider)
    text = clean_for_speech(text)
    if not text:
        raise RuntimeError("LLM returned no text")
    return text


# --------------------------------------------------------------------------- TTS


def split_text(text: str, limit: int) -> List[str]:
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + len(sentence) + 1 > limit:
            chunks.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    if current:
        chunks.append(current)
    return chunks


def tts_say(text: str, out_wav: Path, tts: dict) -> None:
    cmd = ["/usr/bin/say", "-o", str(out_wav), "--file-format=WAVE",
           "--data-format=LEI16@44100"]
    if tts.get("voice"):
        cmd += ["-v", tts["voice"]]
    if tts.get("rate"):
        cmd += ["-r", str(tts["rate"])]
    subprocess.run(cmd, input=text.encode(), check=True)


def tts_openai(text: str, out_wav: Path, tts: dict, workdir: Path) -> None:
    import openai

    client = openai.OpenAI(max_retries=3)
    parts = []
    for i, chunk in enumerate(split_text(text, 4000)):
        part = workdir / ("part%03d.mp3" % i)
        kwargs = dict(model=tts.get("model", "gpt-4o-mini-tts"),
                      voice=tts.get("voice", "onyx"), input=chunk,
                      response_format="mp3")
        if tts.get("instructions"):
            kwargs["instructions"] = tts["instructions"]
        with client.audio.speech.with_streaming_response.create(**kwargs) as r:
            r.stream_to_file(part)
        parts.append(part)
    concat_to_wav(parts, out_wav, workdir, float(tts.get("speed", 1.0)))


def concat_to_wav(parts: List[Path], out_wav: Path, workdir: Path,
                  speed: float = 1.0) -> None:
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    listfile = workdir / "parts.txt"
    listfile.write_text("".join("file '%s'\n" % p for p in parts))
    # atempo changes speed while preserving pitch.
    tempo = ["-filter:a", "atempo=%.3f" % speed] if abs(speed - 1.0) > 0.001 else []
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listfile)] + tempo + ["-ar", "44100", "-ac", "2", str(out_wav)],
                   check=True)


def synthesize(text: str, cfg: dict, workdir: Path, chime: bool = True) -> Path:
    tts = cfg["tts"]
    engine = tts.get("engine", "say").lower()
    out = workdir / "speech.wav"
    log.info("synthesizing %d chars with %s", len(text), engine)
    if engine == "say":
        tts_say(text, out, tts)
    elif engine == "openai":
        try:
            tts_openai(text, out, tts, workdir)
        except Exception:
            # Never go silent because of the cloud voice: fall back to macOS.
            log.exception("OpenAI TTS failed; falling back to macOS say")
            tts_say(text, out, {"voice": tts.get("fallback_voice", "Daniel"),
                                "rate": tts.get("fallback_rate")})
    else:
        raise ValueError("unknown tts.engine %r" % engine)

    chime = cfg["playback"].get("chime") if chime else None
    if chime:
        chime_path = Path(os.path.expanduser(chime))
        if chime_path.exists():
            with_chime = workdir / "speech_chime.wav"
            concat_to_wav([chime_path, out], with_chime, workdir)
            return with_chime
        log.warning("chime file not found: %s", chime_path)
    return out


# --------------------------------------------------------------------------- playback


async def find_devices(names: List[str], timeout: int = 8) -> Dict[str, object]:
    import pyatv

    loop = asyncio.get_running_loop()
    found = {}
    confs = await pyatv.scan(loop, timeout=timeout)
    by_name = {}
    for c in confs:
        by_name.setdefault(c.name.lower(), c)
    for name in names:
        conf = by_name.get(name.lower())
        if conf is None:
            log.error("HomePod %r not found on the network", name)
        else:
            found[name] = conf
    return found


async def play_on(name: str, conf, audio: Path, volume: Optional[float]) -> bool:
    import pyatv

    loop = asyncio.get_running_loop()
    atv = None
    try:
        atv = await pyatv.connect(conf, loop)
        if volume is not None:
            try:
                await atv.audio.set_volume(float(volume))
            except Exception as e:  # not all devices allow volume over RAOP
                log.warning("%s: could not set volume: %s", name, e)
        log.info("%s: streaming %s", name, audio.name)
        await atv.stream.stream_file(str(audio))
        log.info("%s: done", name)
        return True
    except Exception:
        log.exception("%s: playback failed", name)
        return False
    finally:
        if atv is not None:
            atv.close()


async def play(audio: Path, homepods: List[str], volume: Optional[float]) -> bool:
    global PLAY_LOCK
    if PLAY_LOCK is None:
        PLAY_LOCK = asyncio.Lock()
    async with PLAY_LOCK:
        return await _play(audio, homepods, volume)


async def _play(audio: Path, homepods: List[str], volume: Optional[float]) -> bool:
    devices = await find_devices(homepods)
    if not devices:
        return False
    results = await asyncio.gather(*(play_on(n, c, audio, volume) for n, c in devices.items()))
    return all(results) and len(devices) == len(homepods)


# --------------------------------------------------------------------------- broadcast


def prepare_broadcast(b: dict, cfg: dict, when: dt.datetime, workdir: Path,
                      archive_text: bool = True) -> Tuple[str, Path]:
    prompt = render_prompt(b["prompt"], when, cfg)
    log.debug("[%s] prompt:\n%s", b["name"], prompt)
    try:
        text = generate_text(prompt, cfg)
    except Exception:
        log.exception("[%s] LLM request failed", b["name"])
        fallback = cfg.get("failure_message")
        if not fallback:
            raise
        text = fallback
    log.info("[%s] script:\n%s", b["name"], text)
    archive = cfg.get("archive_dir")
    if archive and archive_text:  # dry runs stay out of the briefing memory
        adir = Path(os.path.expanduser(archive))
        adir.mkdir(parents=True, exist_ok=True)
        (adir / ("%s-%s.txt" % (when.strftime("%Y%m%d-%H%M"), b["name"]))).write_text(text + "\n")
    return text, synthesize(text, cfg, workdir)


async def run_broadcast(b: dict, cfg: dict, when: dt.datetime, play_audio: bool = True) -> None:
    lead = int(cfg["playback"].get("prepare_seconds", 180))
    with tempfile.TemporaryDirectory(prefix="jarvis-") as tmp:
        _, audio = await asyncio.to_thread(prepare_broadcast, b, cfg, when, Path(tmp), play_audio)
        if not play_audio:
            keep = BASE_DIR / ("last-%s.wav" % b["name"])
            shutil.copy(audio, keep)
            log.info("[%s] audio saved to %s (not played)", b["name"], keep)
            return
        delay = (when - dt.datetime.now()).total_seconds()
        if 0 < delay <= lead + 60:
            log.info("[%s] ready; waiting %.0fs until %s", b["name"], delay, when.strftime("%H:%M"))
            await asyncio.sleep(delay)
        volume = b.get("volume", cfg["playback"].get("volume"))
        ok = await play(audio, b["homepods"], volume)
        log.info("[%s] broadcast %s", b["name"], "complete" if ok else "had errors")


# --------------------------------------------------------------------------- real-time alerts


PLAY_LOCK: Optional[asyncio.Lock] = None  # one stream at a time: alerts never cut into briefings


class Alerter:
    """Speaks short announcements as soon as matching MQTT events arrive,
    independent of the hourly schedule."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rules = [r for r in cfg.get("alerts", []) if r.get("enabled", True)]
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queue: Optional[asyncio.Queue] = None
        self.last_fired: Dict[str, dt.datetime] = {}
        self.audio_dir = Path(os.path.expanduser("~/Library/Caches/jarvis/alerts"))

    def submit(self, event: dict) -> None:
        """Called from the MQTT thread."""
        if self.loop and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def matches(self, rule: dict, ev: dict, now: dt.datetime) -> bool:
        if rule.get("kind") != ev["kind"]:
            return False
        if rule.get("types") and not set(rule["types"]) & set(ev.get("types", [])):
            return False
        cams = rule.get("cameras")
        if cams and ev.get("camera") not in cams and ev.get("camera_name") not in cams:
            return False
        if rule.get("ports") and ev.get("port") not in rule["ports"]:
            return False
        if "threshold" in rule and ev.get("threshold") != rule["threshold"]:
            return False
        if "max_miles" in rule and (ev.get("miles") is None or ev["miles"] > float(rule["max_miles"])):
            return False
        if rule.get("severities") and ev.get("severity") not in rule["severities"]:
            return False
        for field in ("accessory", "room", "characteristic", "text"):
            wanted = rule.get(field + "s") or rule.get(field)
            if wanted:
                wanted = wanted if isinstance(wanted, list) else [wanted]
                have = ev.get(field, "")
                if field == "text":
                    # State text must match exactly: "DETECTED" (smoke/leak) must
                    # not match a motion sensor's "detected".
                    ok = have in wanted
                else:
                    ok = any(w.lower() in have.lower() for w in wanted)
                if not ok:
                    return False
        hours = rule.get("hours")
        if hours:
            start, end = parse_hhmm(hours["from"]), parse_hhmm(hours["to"])
            t = now.time()
            inside = start <= t <= end if start <= end else (t >= start or t <= end)
            if not inside:
                return False
        return True

    def render(self, text: str, ev: dict) -> str:
        values = {"camera": ev.get("camera_name") or "one of the cameras",
                  "types": " and ".join(ev.get("types", [])), "port": ev.get("port", ""),
                  "device": ev.get("device_name", ""), "detail": ev.get("detail", ""),
                  "level": str(ev.get("level", "")), "threshold": str(ev.get("threshold", "")),
                  "accessory": ev.get("accessory", ""), "room": ev.get("room", ""),
                  "characteristic": ev.get("characteristic", ""), "text": ev.get("text", "")}
        # Any other string field of the event is available too (e.g. drone {model}).
        for k, v in ev.items():
            if isinstance(v, str) and k not in values:
                values[k] = v
        for k, v in values.items():
            text = text.replace("{" + k + "}", v)
        return text

    def audio_for(self, text: str) -> Path:
        return cached_speech(text, self.cfg)

    async def prewarm(self) -> None:
        """Synthesize fixed phrases up front so alerts play within a second or two."""
        texts = set()
        for rule in self.rules:
            for msg in rule.get("messages", []):
                for cam in [c for c in rule.get("cameras", []) if not re.fullmatch(r"[0-9a-f]{24}", c)] or [""]:
                    for port in rule.get("ports") or ["WAN", "WAN2", "WAN3", "WAN4"]:
                        for types in [[t] for t in rule.get("types", [])] or [[]]:
                            ev = {"camera_name": cam, "port": port, "types": types}
                            text = self.render(msg, ev)
                            if "{" not in text:
                                texts.add(text)
        for text in sorted(texts):
            try:
                await asyncio.to_thread(self.audio_for, text)
            except Exception:
                log.exception("could not pre-synthesize alert %r", text)
        log.info("pre-synthesized %d alert phrase(s)", len(texts))

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        await self.prewarm()
        log.info("alerter ready with %d rule(s)", len(self.rules))
        while True:
            ev = await self.queue.get()
            now = dt.datetime.now()
            if ev["kind"] == "say":
                if QUIET and QUIET.active(now) and not ev.get("urgent"):
                    log.info("say suppressed (quiet mode): %r", ev["text"])
                    continue
                homepods = ev.get("homepods") or self.cfg.get("alert_homepods") or self.cfg.get("homepods")
                log.info("say: %r -> %s", ev["text"], ", ".join(homepods))
                asyncio.create_task(self._speak(ev["text"], homepods,
                                                ev.get("volume", self.cfg["playback"].get("volume"))))
                continue
            if ev["kind"] == "brief":
                asyncio.create_task(run_on_demand(self.cfg, ev.get("broadcast") or
                                                  self.cfg["broadcasts"][0]["name"], ev.get("homepods")))
                continue
            for rule in self.rules:
                if not self.matches(rule, ev, now):
                    continue
                name = rule.get("name", rule["kind"])
                cooldown = dt.timedelta(minutes=float(rule.get("cooldown_minutes", 5)))
                last = self.last_fired.get(name)
                if last and now - last < cooldown:
                    log.info("alert %s suppressed (cooldown)", name)
                    break
                if QUIET and QUIET.active(now) and not rule.get("urgent"):
                    log.info("alert %s suppressed (quiet mode)", name)
                    break
                self.last_fired[name] = now
                text = self.render(random.choice(rule["messages"]), ev)
                record_alert(name, ev, text)
                homepods = rule.get("homepods") or self.cfg.get("alert_homepods") or self.cfg.get("homepods")
                volume = rule.get("volume", self.cfg["playback"].get("volume"))
                log.info("alert %s: %r -> %s", name, text, ", ".join(homepods))
                asyncio.create_task(self._speak(text, homepods, volume))
                break  # first matching rule wins

    async def _speak(self, text: str, homepods: List[str], volume) -> None:
        try:
            audio = await asyncio.to_thread(self.audio_for, text)
            await play(audio, homepods, volume)
        except Exception:
            log.exception("alert playback failed")


ALERT_LOG = Path(os.path.expanduser("~/Library/Logs/jarvis/alerts.jsonl"))


def record_alert(rule: str, ev: dict, text: str) -> None:
    """Append every spoken alert to a JSON-lines history (used by the weekly recap)."""
    try:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_LOG, "a") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "rule": rule,
                                "kind": ev.get("kind"), "text": text}) + "\n")
    except OSError as e:
        log.warning("could not record alert: %s", e)


def read_alerts(since: dt.datetime) -> List[dict]:
    out = []
    try:
        with open(ALERT_LOG) as f:
            for line in f:
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if a.get("at", "") >= since.isoformat():
                    out.append(a)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- quiet mode / on demand


class QuietMode:
    """'Hey Siri, turn on Jarvis Quiet': skip scheduled updates and non-urgent
    alerts until switched off or until the configured morning time."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = Path(os.path.expanduser("~/Library/Application Support/jarvis/quiet.json"))
        self.until: Optional[dt.datetime] = None
        self.on_change = None  # callback(bool) to mirror state on the HomeKit switch
        try:
            self.until = dt.datetime.fromisoformat(json.loads(self.path.read_text())["until"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def active(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or dt.datetime.now()
        if self.until and now >= self.until:
            log.info("quiet mode ended")
            self.set(False)
        return self.until is not None

    def set(self, on: bool) -> None:
        if on:
            end = parse_hhmm((self.cfg.get("homekit") or {}).get("quiet_until", "07:00"))
            now = dt.datetime.now()
            until = dt.datetime.combine(now.date(), end)
            if until <= now:
                until += dt.timedelta(days=1)
            self.until = until
            log.info("quiet mode on until %s", until.strftime("%a %H:%M"))
        else:
            self.until = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"until": self.until.isoformat() if self.until else None}))
        except OSError:
            pass
        if self.on_change:
            self.on_change(on)


QUIET: Optional[QuietMode] = None
ON_DEMAND_LOCK: Optional[asyncio.Lock] = None


async def run_on_demand(cfg: dict, bname: str, homepods: Optional[List[str]] = None,
                        acknowledge: bool = True) -> None:
    """Run a briefing right now (Siri switch or jarvis/brief), ignoring quiet mode."""
    global ON_DEMAND_LOCK
    if ON_DEMAND_LOCK is None:
        ON_DEMAND_LOCK = asyncio.Lock()
    if ON_DEMAND_LOCK.locked():
        log.info("on-demand briefing already running; ignoring %s", bname)
        return
    async with ON_DEMAND_LOCK:
        try:
            broadcast = next(b for b in cfg["broadcasts"] if b["name"] == bname)
        except StopIteration:
            log.error("no broadcast named %r", bname)
            return
        hcfg = cfg.get("homekit") or {}
        b = dict(broadcast, homepods=homepods or hcfg.get("homepods") or broadcast["homepods"])
        try:
            acks = hcfg.get("acknowledgements") or []
            if acknowledge and acks:
                ack = await asyncio.to_thread(cached_speech, random.choice(acks), cfg)
                await play(ack, b["homepods"], b.get("volume", cfg["playback"].get("volume")))
            log.info("[%s] on-demand briefing", b["name"])
            await run_broadcast(b, cfg, dt.datetime.now())
        except Exception:
            log.exception("on-demand briefing %s failed", bname)


# --------------------------------------------------------------------------- airspace (drones)


def distance_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def spoken_place(address: Optional[str]) -> str:
    """'100 Main St, Springfield, CA 90000, USA' -> 'Main St, Springfield'."""
    if not address or "+" in address.split(",")[0]:  # plus codes aren't speakable
        return ""
    parts = [p.strip() for p in address.split(",")]
    street = re.sub(r"^\d+[A-Za-z]?\s+", "", parts[0])
    return ", ".join([street] + parts[1:2])


class AirspaceMonitor:
    """Polls the OSP airspace server for drones seen by one Remote ID sensor
    (the house), announcing new sightings and sensor outages."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        acfg = cfg.get("airspace") or {}
        self.acfg = acfg
        self.url = (acfg.get("url") or os.environ.get("AIRSPACE_URL", "")).rstrip("/")
        self.key = os.environ.get("AIRSPACE_API_KEY", "")
        self.sensor_id = acfg["sensor_id"]
        self.ignore_serials = {s.upper() for s in acfg.get("ignore_serials", [])}
        self.ignore_classes = set(acfg.get("ignore_classifications", ["friendly"]))
        self.gap = dt.timedelta(minutes=float(acfg.get("new_sighting_gap_minutes", 15)))
        self.path = Path(os.path.expanduser("~/Library/Caches/jarvis/airspace.json"))
        try:
            st = json.loads(self.path.read_text())
        except (OSError, ValueError):
            st = {}
        self.last_seen: Dict[str, str] = st.get("last_seen", {})   # serial -> iso
        self.sightings: List[dict] = st.get("sightings", [])       # announced sightings (24h)
        self.sensor_state: Optional[str] = st.get("sensor_state")
        self.sensor_info: dict = {}
        self.primed = bool(self.last_seen)
        self.listeners: List = []

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"last_seen": self.last_seen, "sightings": self.sightings,
                                   "sensor_state": self.sensor_state}, indent=1))
        tmp.replace(self.path)

    def ignored(self, d: dict) -> bool:
        return (str(d.get("serial_number", "")).upper() in self.ignore_serials
                or d.get("classification") in self.ignore_classes)

    async def poll_once(self, session) -> List[dict]:
        headers = {"X-API-Key": self.key}
        events: List[dict] = []
        async with session.get(self.url + "/api/sensors/" + self.sensor_id, headers=headers) as r:
            r.raise_for_status()
            self.sensor_info = await r.json()
        state = self.sensor_info.get("status_state")
        if self.sensor_state and state and state != self.sensor_state and state in ("online", "offline"):
            events.append({"kind": "drone_sensor_" + state,
                           "sensor": self.sensor_info.get("display_name", "drone sensor")})
        self.sensor_state = state or self.sensor_state

        async with session.get(self.url + "/api/drones", params={"limit": "500"}, headers=headers) as r:
            r.raise_for_status()
            drones = (await r.json()).get("drones", [])
        now = dt.datetime.now(dt.timezone.utc)
        slat, slng = self.sensor_info.get("latitude"), self.sensor_info.get("longitude")
        for d in drones:
            if d.get("last_sensor") != self.sensor_id or not d.get("last_seen") or self.ignored(d):
                continue
            serial = d["serial_number"]
            seen = dt.datetime.fromisoformat(d["last_seen"].replace("Z", "+00:00"))
            prev = self.last_seen.get(serial)
            self.last_seen[serial] = seen.isoformat()
            if not self.primed:
                continue  # first run: record what's already known without announcing it
            prev_dt = dt.datetime.fromisoformat(prev) if prev else None
            fresh = now - seen < dt.timedelta(minutes=10)
            new_flight = prev_dt is None or seen - prev_dt > self.gap
            if fresh and new_flight:
                miles = None
                if slat is not None and d.get("last_lat") is not None:
                    miles = distance_miles(slat, slng, d["last_lat"], d["last_lng"])
                model = " ".join(x for x in (d.get("manufacturer"), d.get("model")) if x) or "unidentified"
                ev = {"kind": "drone", "serial": serial, "model": model,
                      "distance": ("%.1f miles" % miles) if miles is not None else "an unknown distance",
                      "near": spoken_place(d.get("last_address")) or "the area",
                      "operator_near": spoken_place(d.get("last_op_address")),
                      "classification": d.get("classification", "")}
                events.append(ev)
                self.sightings.append(dict(ev, at=now.isoformat()))
        self.primed = True
        cutoff = (now - dt.timedelta(days=8)).isoformat()  # a week, for the weekly recap
        self.sightings = [s for s in self.sightings if s["at"] >= cutoff]
        self.save()
        return events

    async def run(self) -> None:
        import aiohttp

        interval = float(self.acfg.get("poll_seconds", 60))
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    for ev in await self.poll_once(session):
                        log.info("event: %s", ev)
                        for listener in self.listeners:
                            listener(ev)
                except Exception as e:
                    log.warning("airspace poll failed: %s", e)
                await asyncio.sleep(interval)

    def sighting_lines(self, since: dt.datetime, until: dt.datetime) -> List[str]:
        """Announced sightings between two local naive datetimes."""
        lines = []
        for s in self.sightings:
            t = dt.datetime.fromisoformat(s["at"]).astimezone().replace(tzinfo=None)
            if since <= t <= until:
                lines.append("- %s: %s, about %s away near %s%s" % (
                    t.strftime("%-I:%M %p"), s["model"], s["distance"], s["near"],
                    ("; operator near " + s["operator_near"]) if s.get("operator_near") else ""))
        return lines

    def summary(self, now: dt.datetime, since: dt.datetime) -> str:
        """Sensor status plus drones seen by the home sensor since `since`."""
        name = self.sensor_info.get("display_name", "drone sensor")
        status = "- Sensor %s: %s" % (name, self.sensor_state or "unknown")
        return "\n".join([status] + (self.sighting_lines(since, now) or ["- no drones detected"]))


AIRSPACE: Optional[AirspaceMonitor] = None


# --------------------------------------------------------------------------- traffic (511.org open data)
#
# Data use follows the 511 Data Disseminator Agreement:
#  - rate limit is 60 requests / 3600 s per token: we poll ~15/h, keep a
#    persistent request ledger capped at `max_requests_per_hour` (default 40),
#    honour RateLimit-Remaining and 429 Retry-After;
#  - keep information current (2.a): cleared events are dropped every poll;
#  - acknowledge the source (5.b): spoken output says "data provided by 511.org";
#  - no 511 marks in naming/branding (5.c): this module is just "traffic".
# Responses echo the API key (pagination.next_url), so URLs and bodies are never logged.

# Rough San Mateo County outline (lon, lat) for the work-zone feed, which has no county field.
SAN_MATEO_COUNTY = [
    (-122.515, 37.708), (-122.380, 37.708), (-122.355, 37.615), (-122.255, 37.565),
    (-122.190, 37.505), (-122.115, 37.466), (-122.170, 37.425), (-122.190, 37.400),
    (-122.200, 37.330), (-122.170, 37.280), (-122.250, 37.200), (-122.300, 37.105),
    (-122.395, 37.180), (-122.420, 37.320), (-122.515, 37.500),
]

STREET_WORDS = {"Expy": "Expressway", "Rd": "Road", "Blvd": "Boulevard", "Ave": "Avenue",
                "St": "Street", "Dr": "Drive", "Hwy": "Highway", "Pkwy": "Parkway", "Ct": "Court",
                "Ln": "Lane", "Fwy": "Freeway", "Wy": "Way"}


def point_in_polygon(lon: float, lat: float, poly: List[Tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def first_point(geom: dict) -> Optional[Tuple[float, float]]:
    coords = (geom or {}).get("coordinates")
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    if isinstance(coords, list) and len(coords) >= 2:
        return float(coords[0]), float(coords[1])
    return None


def spoken_headline(headline: str) -> str:
    """'CHP : Traffic Collision on I-280 Northbound at Edgewood Rd (Redwood City). Right lane
    blocked. Expect delays.' -> 'Traffic collision on I-280 northbound at Edgewood Road, in
    Redwood City. Right lane blocked.'"""
    text = re.sub(r"^[A-Za-z .&]+ : ", "", headline or "", flags=re.I).replace(" Expect delays.", "").strip()
    text = text.rstrip(".")
    text = re.sub(r" \(([^)]+)\)", r", in \1", text)
    text = re.sub(r"\b(Northbound|Southbound|Eastbound|Westbound)\b", lambda m: m.group(1).lower(), text)
    for short, full in STREET_WORDS.items():
        text = re.sub(r"\b%s\b" % short, full, text)
    return text[:1].upper() + text[1:]


class TrafficMonitor:
    ACCIDENT_WORDS = ("collision", "crash", "overturn", "accident")

    def __init__(self, cfg: dict):
        tcfg = cfg.get("traffic") or {}
        self.tcfg = tcfg
        self.key = os.environ.get("TRAFFIC_API_KEY", "")
        self.base = tcfg.get("base_url", "https://api.511.org")
        self.county = tcfg.get("county", "San Mateo")
        home = tcfg.get("home") or (cfg.get("feeds") or {}).get("home")
        if not home:
            raise ValueError("config: set traffic.home (or feeds.home): {lat: ..., lng: ...}")
        self.home = (float(home["lat"]), float(home["lng"]))
        self.max_per_hour = int(tcfg.get("max_requests_per_hour", 40))
        self.path = Path(os.path.expanduser("~/Library/Caches/jarvis/traffic.json"))
        try:
            st = json.loads(self.path.read_text())
        except (OSError, ValueError):
            st = {}
        self.requests: List[float] = st.get("requests", [])        # epoch seconds, last hour
        self.blocked_until: float = st.get("blocked_until", 0.0)
        self.seen: Dict[str, str] = st.get("seen", {})             # announced/known ids -> first seen
        self.primed = bool(st.get("primed"))
        self.incidents: List[dict] = []                            # current, San Mateo
        self.closures: List[dict] = []                             # current, San Mateo
        self.updated: Optional[str] = None
        self.listeners: List = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"requests": self.requests, "blocked_until": self.blocked_until,
                                   "seen": self.seen, "primed": self.primed}))
        tmp.replace(self.path)

    def may_request(self) -> bool:
        now = time.time()
        self.requests = [t for t in self.requests if now - t < 3600]
        return now >= self.blocked_until and len(self.requests) < self.max_per_hour

    async def fetch(self, session, path: str, params: dict) -> Optional[dict]:
        if not self.may_request():
            log.info("traffic: request skipped to stay within the rate limit")
            return None
        self.requests.append(time.time())
        self.save()
        params = dict(params, api_key=self.key)
        async with session.get(self.base + path, params=params) as r:
            remaining = r.headers.get("RateLimit-Remaining")
            if r.status == 429:
                wait = float(r.headers.get("Retry-After") or 3600)
                self.blocked_until = time.time() + wait
                log.warning("traffic: rate limited by server; pausing %.0f s", wait)
                return None
            if r.status != 200:
                log.warning("traffic: %s returned HTTP %s", path, r.status)
                return None
            if remaining is not None and int(remaining) <= 5:
                self.blocked_until = time.time() + 3600
                log.warning("traffic: only %s requests left this hour; pausing", remaining)
            return json.loads(await r.text(encoding="utf-8-sig"))

    def distance(self, pt: Optional[Tuple[float, float]]) -> Optional[float]:
        if not pt:
            return None
        return distance_miles(self.home[0], self.home[1], pt[1], pt[0])

    async def poll_events(self, session) -> None:
        data = await self.fetch(session, "/traffic/events",
                                {"format": "json", "status": "ACTIVE", "limit": "500"})
        if data is None:
            return
        events = list(data.get("events", []))
        nxt = (data.get("pagination") or {}).get("next_url")
        if nxt and len(events) >= 500:  # at most one extra page
            more = await self.fetch(session, "/traffic/events",
                                    {"format": "json", "status": "ACTIVE", "limit": "500", "offset": "500"})
            events += (more or {}).get("events", [])
        incidents, closures = [], []
        for e in events:
            if not any(self.county.lower() in (a.get("name") or "").lower() for a in e.get("areas", [])):
                continue
            pt = first_point(e.get("geography"))
            subtypes = " ".join(e.get("event_subtypes") or []).lower()
            item = {"id": "ev:" + str(e.get("id")), "type": e.get("event_type"), "severity": e.get("severity"),
                    "subtypes": e.get("event_subtypes") or [], "text": spoken_headline(e.get("headline", "")),
                    "miles": self.distance(pt), "updated": e.get("updated")}
            item["accident"] = e.get("event_type") == "INCIDENT" and any(w in subtypes for w in self.ACCIDENT_WORDS)
            if e.get("event_type") == "INCIDENT":
                incidents.append(item)
            if any((r.get("state") or "").upper() == "CLOSED" for r in e.get("roads", [])):
                closures.append(dict(item, source="events"))
        self.incidents = incidents
        self._event_closures = closures

    async def poll_workzones(self, session) -> None:
        data = await self.fetch(session, "/traffic/wzdx", {"includeAllDefinedEnums": "true"})
        if data is None:
            return
        now = dt.datetime.now(dt.timezone.utc)
        found = []
        for f in data.get("features", []):
            p = f.get("properties") or {}
            if p.get("vehicle_impact") != "all-lanes-closed":
                continue
            pt = first_point(f.get("geometry"))
            if not pt or not point_in_polygon(pt[0], pt[1], SAN_MATEO_COUNTY):
                continue
            try:
                start = dt.datetime.fromisoformat(p["start_date"].replace("Z", "+00:00"))
                end = dt.datetime.fromisoformat(p["end_date"].replace("Z", "+00:00")) if p.get("end_date") else None
            except (KeyError, ValueError):
                continue
            if start > now or (end and end < now):
                continue  # not in effect right now
            core = p.get("core_details") or {}
            desc = core.get("description") or ""
            if " : " in desc[:30]:
                # Caltrans entries carry a full headline; speak that.
                text = spoken_headline(desc)
            else:
                def name(s: str) -> str:  # keep route names like CA-82 / I-280 as-is
                    return s if re.search(r"\d", s) else s.title()
                roads = " and ".join(name(r) for r in core.get("road_names", [])) or "a road"
                between = ""
                if p.get("beginning_cross_street") and p.get("ending_cross_street"):
                    between = " between %s and %s" % (name(p["beginning_cross_street"]),
                                                       name(p["ending_cross_street"]))
                text = "%s closed%s, %s" % (roads, between, (desc or "work zone").lower())
            fid = f.get("id") or core.get("data_source_id", "") + roads + p.get("start_date", "")
            found.append({"id": "wz:" + str(fid), "type": "CLOSURE", "text": text, "miles": self.distance(pt),
                          "until": end.astimezone().strftime("%b %-d") if end else None, "accident": False})
        self._wz_closures = found

    def diff(self) -> List[dict]:
        """New accidents and closures since the last poll."""
        events = []
        now = dt.datetime.now().isoformat(timespec="minutes")
        current = {}
        for item in self.incidents:
            if item["accident"]:
                current[item["id"]] = ("traffic_accident", item)
        for item in self.closures:
            current[item["id"]] = ("road_closure", item)
        for iid, (kind, item) in current.items():
            if iid not in self.seen:
                self.seen[iid] = now
                if self.primed:
                    events.append({"kind": kind, "text": item["text"], "severity": str(item.get("severity") or ""),
                                   "distance": ("%.0f miles" % item["miles"]) if item.get("miles") is not None else "",
                                   "miles": item.get("miles")})
        # forget cleared items so a recurrence is announced again (keeps data current, 2.a)
        self.seen = {k: v for k, v in self.seen.items() if k in current}
        self.primed = True
        return events

    async def run(self) -> None:
        import aiohttp

        ev_every = float(self.tcfg.get("events_poll_minutes", 5)) * 60
        wz_every = float(self.tcfg.get("closures_poll_minutes", 20)) * 60
        self._event_closures, self._wz_closures = [], []
        last_wz = 0.0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            while True:
                try:
                    await self.poll_events(session)
                    if time.time() - last_wz >= wz_every:
                        await self.poll_workzones(session)
                        last_wz = time.time()
                    self.closures = self._event_closures + self._wz_closures
                    self.updated = dt.datetime.now().strftime("%-I:%M %p")
                    for ev in self.diff():
                        log.info("event: %s", ev)
                        for listener in self.listeners:
                            listener(ev)
                    self.save()
                except Exception as e:  # never log the URL: it carries the API key
                    log.warning("traffic poll failed: %s", type(e).__name__)
                await asyncio.sleep(ev_every)

    def summary(self) -> str:
        if self.updated is None:
            return ""
        def fmt(item):
            d = " (%.0f mi from home)" % item["miles"] if item.get("miles") is not None else ""
            sev = " [%s]" % item["severity"] if item.get("severity") and item["severity"] != "Unknown" else ""
            until = " until %s" % item["until"] if item.get("until") else ""
            return "- %s%s%s%s" % (item["text"], sev, until, d)
        acc = [fmt(i) for i in self.incidents if i["accident"]]
        other = [fmt(i) for i in self.incidents if not i["accident"]]
        clo = [fmt(i) for i in self.closures]
        lines = ["%s County, as of %s (data provided by 511.org):" % (self.county, self.updated)]
        lines += ["Accidents:"] + (acc or ["- none"])
        lines += ["Road closures:"] + (clo or ["- none"])
        if other:
            lines += ["Other incidents (hazards, disabled vehicles, weather):"] + other[:8]
        return "\n".join(lines)


TRAFFIC: Optional[TrafficMonitor] = None
FEEDS: Dict[str, "feedmod.Feed"] = {}


def feed_lines(names: List[str], now: dt.datetime, headers: Dict[str, str]) -> str:
    out = []
    for name in names:
        feed = FEEDS.get(name)
        if not feed:
            continue
        lines = feed.lines(now)
        out.append(headers[name] + ":")
        out += lines or ["- none"]
    return "\n".join(out)


def weekly_summary(now: dt.datetime) -> str:
    """Facts for the Sunday recap: the last 7 days."""
    since = now - dt.timedelta(days=7)
    out = ["Week of %s to %s." % (since.strftime("%b %-d"), now.strftime("%b %-d"))]
    if MQTT and MQTT.energy:
        hist = [h for h in MQTT.energy.state.get("history", []) if h.get("date", "") >= since.date().isoformat()]
        if hist:
            solar = sum(h["solar"] for h in hist)
            house = sum(h["house"] for h in hist)
            best = max(hist, key=lambda h: h["solar"])
            worst = min(hist, key=lambda h: h["solar"])
            out.append("Energy (%d days): solar %.0f kWh total, house used %.0f kWh; best solar day %s (%.0f kWh), "
                       "lowest %s (%.0f kWh)." % (len(hist), solar, house, best["date"], best["solar"],
                                                  worst["date"], worst["solar"]))
    if MQTT and MQTT.unifi:
        totals: Dict[str, int] = {}
        for day, cams in MQTT.unifi.daily_counts.items():
            if day >= since.date().isoformat():
                for cam, types in cams.items():
                    totals[cam] = totals.get(cam, 0) + sum(types.values())
        if totals:
            out.append("Camera detections: " + ", ".join("%s %d" % kv for kv in sorted(totals.items(), key=lambda kv: -kv[1])))
    alerts = read_alerts(since)
    by_kind: Dict[str, List[dict]] = {}
    for a in alerts:
        by_kind.setdefault(a.get("kind") or a["rule"], []).append(a)
    if alerts:
        out.append("Spoken alerts: %d in total." % len(alerts))
        for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            sample = "; ".join(i["text"][:140] for i in items[-3:])
            out.append("- %s: %d (latest: %s)" % (kind, len(items), sample))
    else:
        out.append("Spoken alerts: none.")
    if "earthquakes" in FEEDS:
        quakes = FEEDS["earthquakes"].recent(since)
        if quakes:
            big = max(quakes, key=lambda q: q["mag"])
            out.append("Earthquakes worth noting: %d; largest M%.1f %s." % (len(quakes), big["mag"], big["place"]))
    if AIRSPACE:
        week = [s for s in AIRSPACE.sightings
                if dt.datetime.fromisoformat(s["at"]).astimezone().replace(tzinfo=None) >= since]
        models: Dict[str, int] = {}
        for s in week:
            models[s["model"]] = models.get(s["model"], 0) + 1
        out.append("Drones seen by the house sensor: %d%s." % (
            len(week), (" (" + ", ".join("%s x%d" % kv for kv in models.items()) + ")") if models else ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- HomeKit accessory


async def start_homekit(cfg: dict) -> None:
    """Expose Jarvis to Apple Home as one accessory with several switches:
    momentary briefing switches ("Hey Siri, turn on Jarvis Update") that run a
    briefing immediately and switch themselves off, and a "Jarvis Quiet"
    switch that holds quiet mode."""
    from pyhap.accessory import Accessory
    from pyhap.accessory_driver import AccessoryDriver
    from pyhap.const import CATEGORY_SWITCH

    hcfg = cfg.get("homekit") or {}
    loop = asyncio.get_running_loop()
    switches = hcfg.get("switches") or [{"name": hcfg.get("name", "Jarvis Update"),
                                         "broadcast": hcfg.get("broadcast") or cfg["broadcasts"][0]["name"]}]
    state_dir = Path(os.path.expanduser("~/Library/Application Support/jarvis"))
    state_dir.mkdir(parents=True, exist_ok=True)

    class JarvisAccessory(Accessory):
        category = CATEGORY_SWITCH

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for i, sw in enumerate(switches):
                serv = self.add_preload_service("Switch", chars=["Name", "ConfiguredName"])
                serv.configure_char("Name", value=sw["name"])
                serv.configure_char("ConfiguredName", value=sw["name"])
                if i == 0:
                    self.set_primary_service(serv)
                if sw.get("type") == "quiet":
                    char = serv.configure_char("On", value=bool(QUIET and QUIET.active()),
                                               setter_callback=self._quiet_setter(sw["name"]))
                    if QUIET:
                        QUIET.on_change = lambda on, c=char: loop.call_soon_threadsafe(c.set_value, on)
                else:
                    char = serv.configure_char("On", value=False)
                    char.setter_callback = self._brief_setter(sw, char)

        def _brief_setter(self, sw, char):
            def setter(value):
                if not value:
                    return
                log.info("HomeKit: %s turned on", sw["name"])
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(
                    run_on_demand(cfg, sw["broadcast"], sw.get("homepods"))))
                loop.call_soon_threadsafe(loop.call_later, 2, lambda: char.set_value(False))
            return setter

        def _quiet_setter(self, name):
            def setter(value):
                log.info("HomeKit: %s turned %s", name, "on" if value else "off")
                if QUIET:
                    loop.call_soon_threadsafe(QUIET.set, bool(value))
            return setter

    driver = AccessoryDriver(port=int(hcfg.get("port", 51826)), loop=loop,
                             persist_file=str(state_dir / "homekit.state"),
                             pincode=str(hcfg["pincode"]).encode() if hcfg.get("pincode") else None)
    # The accessory keeps the first switch's name so the existing pairing stays valid.
    driver.add_accessory(JarvisAccessory(driver, switches[0]["name"]))
    await driver.async_start()
    if not driver.state.paired:
        log.info("HomeKit: not paired yet. In the Home app: Add Accessory > More options > "
                 "'%s', setup code %s", switches[0]["name"], driver.state.pincode.decode())
    for text in hcfg.get("acknowledgements") or []:
        try:
            await asyncio.to_thread(cached_speech, text, cfg)
        except Exception:
            log.exception("could not synthesize %r", text)


def cached_speech(text: str, cfg: dict) -> Path:
    """Synthesize a short fixed phrase once and reuse the audio."""
    import hashlib

    tts = cfg["tts"]
    key = hashlib.sha1(("%s|%s|%s|%s" % (text, tts.get("voice"), tts.get("speed"),
                                         tts.get("instructions"))).encode()).hexdigest()[:16]
    adir = Path(os.path.expanduser("~/Library/Caches/jarvis/alerts"))
    path = adir / (key + ".wav")
    if not path.exists():
        adir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="jarvis-tts-") as tmp:
            shutil.copy(synthesize(text, cfg, Path(tmp), chime=False), path)
    return path


def upcoming(cfg: dict, after: dt.datetime, horizon_days: int = 8) -> List[Tuple[dt.datetime, dict]]:
    events = []
    for b in cfg["broadcasts"]:
        if b.get("enabled", True) is False:
            continue
        for offset in range(horizon_days):
            day = (after + dt.timedelta(days=offset)).date()
            if day.weekday() not in b["days"]:
                continue
            for t in b["times"]:
                when = dt.datetime.combine(day, t)
                if when > after:
                    events.append((when, b))
    events.sort(key=lambda e: e[0])
    return events


async def scheduler(cfg: dict) -> None:
    lead = dt.timedelta(seconds=int(cfg["playback"].get("prepare_seconds", 180)))
    # An event is started once its prep window opens. Events up to 2 minutes
    # past due (e.g. after a restart or wake from sleep) still fire; older ones
    # are skipped rather than played late.
    started: set = set()
    tasks: set = set()
    global QUIET
    QUIET = QuietMode(cfg)
    if MQTT and (cfg.get("alerts") or cfg.get("mqtt", {}).get("say_topic")):
        alerter = Alerter(cfg)
        MQTT.listeners.append(alerter.submit)
        alert_task = asyncio.create_task(alerter.run())
        alert_task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                                     log.error("alerter stopped: %r", t.exception()))
    global AIRSPACE
    if (cfg.get("airspace") or {}).get("enabled") and os.environ.get("AIRSPACE_API_KEY"):
        AIRSPACE = AirspaceMonitor(cfg)
        if MQTT and cfg.get("alerts"):
            AIRSPACE.listeners.append(alerter.submit)
        air_task = asyncio.create_task(AIRSPACE.run())
        air_task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                                   log.error("airspace monitor stopped: %r", t.exception()))
    global FEEDS
    FEEDS = feedmod.build_feeds(cfg)
    for feed in FEEDS.values():
        if MQTT and cfg.get("alerts"):
            feed.listeners.append(alerter.submit)
        task = asyncio.create_task(feed.run())
        task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                               log.error("feed stopped: %r", t.exception()))
    log.info("feeds: %s", ", ".join(FEEDS) or "none")
    global TRAFFIC
    if (cfg.get("traffic") or {}).get("enabled") and os.environ.get("TRAFFIC_API_KEY"):
        TRAFFIC = TrafficMonitor(cfg)
        if MQTT and cfg.get("alerts"):
            TRAFFIC.listeners.append(alerter.submit)
        tr_task = asyncio.create_task(TRAFFIC.run())
        tr_task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                                  log.error("traffic monitor stopped: %r", t.exception()))
    if (cfg.get("homekit") or {}).get("enabled"):
        try:
            await start_homekit(cfg)
        except Exception:
            log.exception("HomeKit accessory failed to start")
    log.info("jarvis scheduler started with %d broadcast(s)", len(cfg["broadcasts"]))
    for when, b in upcoming(cfg, dt.datetime.now())[:5]:
        log.info("  next: %s  %s -> %s", when.strftime("%a %Y-%m-%d %H:%M"), b["name"], ", ".join(b["homepods"]))
    while True:
        now = dt.datetime.now()
        for when, b in upcoming(cfg, now - lead, horizon_days=2):
            key = (b["name"], when)
            if key in started:
                continue
            prep_at = when - lead
            if prep_at <= now <= when + dt.timedelta(minutes=2):
                started.add(key)
                if QUIET and QUIET.active(now):
                    log.info("[%s] skipped (quiet mode)", b["name"])
                    continue
                log.info("[%s] preparing broadcast for %s", b["name"], when.strftime("%H:%M"))
                task = asyncio.create_task(safe_run(b, cfg, when))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        cutoff = now - dt.timedelta(days=1)
        started = {k for k in started if k[1] > cutoff}
        # Short sleeps keep us accurate across system sleep/wake and clock changes.
        await asyncio.sleep(15)


async def safe_run(b: dict, cfg: dict, when: dt.datetime) -> None:
    try:
        await run_broadcast(b, cfg, when)
    except Exception:
        log.exception("[%s] broadcast failed", b["name"])


# --------------------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description="Scheduled spoken briefings for HomePods")
    ap.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the scheduler (daemon mode)")
    sub.add_parser("devices", help="list AirPlay devices")
    sub.add_parser("next", help="show upcoming broadcasts")
    t = sub.add_parser("test", help="run a broadcast now")
    t.add_argument("broadcast")
    t.add_argument("--no-play", action="store_true", help="generate audio but do not play it")
    t.add_argument("--homepod", action="append", help="override target HomePod(s)")
    cm = sub.add_parser("cameras", help="list cameras seen in UniFi detections")
    cm.add_argument("--watch", action="store_true", help="print detections live (walk past a camera)")
    s = sub.add_parser("say", help="speak text on HomePods")
    s.add_argument("text")
    s.add_argument("--homepod", action="append")
    args = ap.parse_args()

    config_path = Path(args.config)
    load_env_file(config_path.parent / ".env")
    cfg = load_config(config_path)
    setup_logging(cfg if args.cmd == "run" else {}, args.verbose)

    global MQTT
    if args.cmd in ("run", "test") and (cfg.get("mqtt") or {}).get("enabled"):
        MQTT = MqttState(cfg["mqtt"])
        MQTT.start()
        if args.cmd == "test":
            time.sleep(3)  # collect retained messages before building the prompt

    if args.cmd == "run":
        asyncio.run(scheduler(cfg))
    elif args.cmd == "devices":
        asyncio.run(list_devices())
    elif args.cmd == "next":
        for when, b in upcoming(cfg, dt.datetime.now())[:20]:
            print("%s  %-16s -> %s" % (when.strftime("%a %Y-%m-%d %H:%M"), b["name"], ", ".join(b["homepods"])))
    elif args.cmd == "test":
        matches = [b for b in cfg["broadcasts"] if b["name"] == args.broadcast]
        if not matches:
            print("no broadcast named %r" % args.broadcast, file=sys.stderr)
            return 1
        b = dict(matches[0])
        if args.homepod:
            b["homepods"] = args.homepod
        asyncio.run(run_broadcast(b, cfg, dt.datetime.now(), play_audio=not args.no_play))
    elif args.cmd == "cameras":
        return cameras_cmd(cfg, args.watch)
    elif args.cmd == "say":
        homepods = args.homepod or cfg.get("homepods") or []
        with tempfile.TemporaryDirectory(prefix="jarvis-") as tmp:
            audio = synthesize(args.text, cfg, Path(tmp))
            ok = asyncio.run(play(audio, homepods, cfg["playback"].get("volume")))
        return 0 if ok else 1
    return 0


def cameras_cmd(cfg: dict, watch: bool) -> int:
    ucfg = (cfg.get("mqtt") or {}).get("unifi") or {}
    names = {str(k): v for k, v in (ucfg.get("camera_names") or {}).items()}
    try:
        state = json.loads(Path(os.path.expanduser("~/Library/Caches/jarvis/unifi.json")).read_text())
        names = dict(state.get("names", {}), **names)
        seen: Dict[str, List[str]] = {}
        for when, types, cam in state.get("detections", {}).values():
            if cam:
                seen.setdefault(cam, []).append("%s %s" % (when[11:16], "/".join(types)))
        for cam, items in sorted(seen.items()):
            print("%-26s %-20s last 24h: %s" % (cam, names.get(cam, "(unnamed)"), ", ".join(sorted(items)[-6:])))
    except (OSError, ValueError):
        print("no UniFi state yet (the service writes ~/Library/Caches/jarvis/unifi.json)")
    if not watch:
        return 0
    print("\nwatching detections; Ctrl-C to stop")
    m = MqttState(dict(cfg["mqtt"], discover=False, persist=False))
    m.unifi.names.update(names)
    m.listeners.append(lambda ev: print(dt.datetime.now().strftime("%H:%M:%S"), ev, flush=True))
    m.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


async def list_devices() -> None:
    import pyatv
    from pyatv.const import Protocol

    confs = await pyatv.scan(asyncio.get_running_loop(), timeout=8)
    for c in sorted(confs, key=lambda c: c.name.lower()):
        raop = c.get_service(Protocol.RAOP)
        ok = raop is not None and raop.pairing.name in ("NotNeeded", "Optional")
        print("%-28s %-16s %-22s %s" % (c.name, c.address, c.device_info.model_str,
                                        "streamable" if ok else "needs pairing / unsupported"))


if __name__ == "__main__":
    sys.exit(main())
