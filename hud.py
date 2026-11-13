"""Jarvis HUD: a wall display of the house's state.

build_status(j, now) turns everything the running daemon knows (module `j`,
the jarvis module) into one JSON snapshot. serve() publishes it on the local
network: GET / (the display), GET /status.json, and a WebSocket /ws that
pushes a new snapshot whenever something changes. A copy is also written to
~/Library/Caches/jarvis/hud-status.json once a minute (used to feed the
remote view).

Access: if HUD_TOKEN is set in .env, every request needs ?token=<HUD_TOKEN>
(the page remembers it in a cookie).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("jarvis")

HERE = Path(__file__).resolve().parent
PAGE = HERE / "hud" / "index.html"
STATUS_FILE = Path(os.path.expanduser("~/Library/Caches/jarvis/hud-status.json"))


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # one broken section must never blank the display
        log.debug("hud section failed: %s", e)
        return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def readings(j, now: dt.datetime) -> Dict[str, list]:
    """label -> [(name, raw value, formatted)] for every configured MQTT topic spec."""
    m = j.MQTT
    out: Dict[str, list] = {}
    if not m:
        return out
    with m.lock:
        values = dict(m.values)
    default_age = int(m.cfg.get("max_age_minutes", 720))
    for spec in m.cfg.get("topics", []):
        max_age = dt.timedelta(minutes=int(spec.get("max_age_minutes", default_age)))
        topics = sorted(t for t, (_, seen) in values.items()
                        if now - seen <= max_age and j.topic_matches(spec["topic"], t))
        if spec.get("include"):
            topics = [t for t in topics if any(s in t for s in spec["include"])]
        if spec.get("exclude"):
            topics = [t for t in topics if not any(s in t for s in spec["exclude"])]
        label = spec.get("label", spec["topic"])
        if "count_value" in spec:
            n = sum(1 for t in topics if values[t][0] == str(spec["count_value"]))
            out.setdefault(label, []).append((label, n, "%d of %d" % (n, len(topics))))
            continue
        for t in topics:
            raw = j.extract_field(values[t][0], spec.get("field"))
            if raw is None:
                continue
            fmt = j.format_value(raw, spec)
            raw = j.numeric(raw)  # numbers sent as text, e.g. "19.74"
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                # same unit conversion as the briefing text (e.g. W -> kW, C -> F)
                raw = raw * float(spec.get("scale", 1)) + float(spec.get("offset", 0))
            if spec.get("only_values") and fmt not in spec["only_values"]:
                continue
            parts = int(spec.get("topic_label_parts") or 0)
            name = " / ".join(t.split("/")[-parts:]) if parts else label
            out.setdefault(label, []).append((name, raw, fmt))
    return out


def first(r: Dict[str, list], label: str):
    v = r.get(label)
    return v[0][1] if v else None


def build_status(j, cfg: dict, now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now()
    r = _safe(lambda: readings(j, now), {})
    s: dict = {"v": 1, "generated": now.isoformat(timespec="seconds"),
               "location": cfg.get("location", "")}

    # --- voice -------------------------------------------------------------
    def voice():
        lock = getattr(j, "PLAY_LOCK", None)
        out = {"speaking": bool(lock and lock.locked()),
               "quiet": bool(getattr(j, "QUIET", None) and j.QUIET.active(now))}
        nxt = [(w, b) for w, b in j.upcoming(cfg, now) if not b.get("on_demand")]
        if nxt:
            out["next"] = {"at": nxt[0][0].strftime("%-I:%M %p"), "name": nxt[0][1]["name"]}
        archive = cfg.get("archive_dir")
        if archive:
            files = sorted(Path(os.path.expanduser(archive)).glob("*.txt"))
            if files:
                import re
                f = files[-1]  # names: YYYYMMDD-HHMM[SS]-<broadcast>.txt
                m = re.match(r"(\d{8})-(\d{4})(\d{2})?-(.+)$", f.stem)
                if m:
                    at = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
                    out["last_briefing"] = {"at": at.strftime("%-I:%M %p"), "name": m.group(4),
                                            "text": f.read_text().strip()[:900]}
        alerts = j.read_alerts(now - dt.timedelta(hours=24))
        out["alerts"] = [{"at": dt.datetime.fromisoformat(a["at"]).strftime("%-I:%M %p"),
                          "kind": a.get("kind") or a.get("rule"), "text": a["text"][:220]}
                         for a in alerts[-10:]][::-1]
        return out
    s["voice"] = _safe(voice, {})

    # --- energy ------------------------------------------------------------
    def energy():
        e = getattr(j.MQTT, "energy", None) if j.MQTT else None
        out = {"soc": _num(first(r, "Powerpack charge")),
               "solar_kw": _num(first(r, "Solar production")),
               "house_kw": _num(first(r, "House load")),
               "battery_kw": _num(first(r, "Battery power (negative = charging)")),
               "charging": first(r, "Powerpack charging") in (True, "True", "true")}
        if e is not None:
            rem, full = getattr(e, "remaining_wh", None), getattr(e, "full_wh", None)
            if rem:
                out["remaining_kwh"] = round(rem / 1000.0)
            if out["soc"] is None and rem and full:
                out["soc"] = round(100.0 * rem / full)
            if out["solar_kw"] is None and getattr(e, "solar_w", None) is not None:
                out["solar_kw"] = round(e.solar_w / 1000.0, 1)
            loads = getattr(e, "loads", [])
            if loads and rem and full:
                avg = sum(w for _, w in loads) / len(loads) / 1000.0
                if avg > 0.05:
                    reserve = float(e.cfg.get("runway_reserve_percent", 20)) / 100.0
                    out["runway_h"] = round(max(0.0, rem / 1000.0 - reserve * full / 1000.0) / avg)
                    out["avg_load_kw"] = round(avg, 1)
            st = e.state
            if st.get("last") and st.get("date") == now.date().isoformat():
                t = e.totals(st["start"], st["last"])
                out["today"] = {"solar": round(t["solar"]), "house": round(t["house"])}
            if st.get("yesterday"):
                out["yesterday"] = {"solar": round(st["yesterday"]["solar"]),
                                    "house": round(st["yesterday"]["house"])}
        return out
    s["energy"] = _safe(energy, {})

    # --- weather, air, hazards --------------------------------------------
    def weather():
        out = {"temp": first(r, "Home weather station temperature"), "feels": first(r, "Feels like"),
               "humidity": first(r, "Humidity"), "wind": first(r, "Wind"), "gust": first(r, "Wind gusts"),
               "rain": first(r, "Rain today"), "uv": first(r, "UV index")}
        out = {k: (round(_num(v)) if k in ("temp", "feels", "humidity", "wind", "gust") and _num(v) is not None
                   else v) for k, v in out.items()}
        wine_t = _num(first(r, "Wine room temperature"))
        if wine_t is not None:
            spec = next((x for x in (j.MQTT.cfg.get("topics", []) if j.MQTT else [])
                         if x.get("label") == "Wine room temperature"), {})
            out["wine"] = {"temp": round(wine_t), "humidity": _num(first(r, "Wine room humidity")),
                           "low": spec.get("alert_below"), "high": spec.get("alert_above")}
        feeds = getattr(j, "FEEDS", {}) or {}
        air = feeds.get("outdoor_air")
        if air and air.state.get("current"):
            c = air.state["current"]
            out["aqi"] = {"value": c["aqi"], "band": c["band"], "main": c.get("main")}
        fc = feeds.get("forecast")
        if fc:
            out["forecast"] = [{"name": x["name"], "temp": x["temp"], "day": x["day"], "short": x["short"],
                                "rain": x.get("rain")} for x in fc.state.get("periods", [])[:4]]
        wa = feeds.get("weather_alerts")
        out["alerts"] = [{"event": a["event"], "severity": a["severity"]}
                         for a in (wa.state.get("active", {}).values() if wa else [])]
        return out
    s["weather"] = _safe(weather, {})

    def hazards():
        feeds = getattr(j, "FEEDS", {}) or {}
        out = {"quakes": [], "fires": []}
        q = feeds.get("earthquakes")
        if q:
            for e in q.recent(now - dt.timedelta(hours=24))[-5:][::-1]:
                out["quakes"].append({"mag": e["mag"], "place": e["place"], "miles": e["miles"], "dir": e["dir"]})
        w = feeds.get("wildfires")
        if w:
            for f in sorted(w.state.get("fires", {}).values(), key=lambda f: f["miles"])[:5]:
                out["fires"].append({"name": f["name"], "miles": f["miles"], "dir": f["dir"],
                                     "acres": f.get("acres"), "contained": f.get("contained")})
        return out
    s["hazards"] = _safe(hazards, {})

    # --- network -------------------------------------------------------------
    def network():
        out = {}
        inet = getattr(j.MQTT, "internet", None) if j.MQTT else None
        if inet and inet.last_seen:
            stale = now.timestamp() - inet.last_seen > float(inet.cfg.get("stale_seconds", 180))
            loss, rtt = inet.stats()
            out["internet"] = "stale" if stale else inet.state
            out["loss"] = round(loss, 1)
            out["rtt_ms"] = round(rtt) if rtt is not None else None
            today = now.date().isoformat()
            out["outages_today"] = sum(1 for h in inet.history if h[2] == "outage" and h[0][:10] == today)
        u = getattr(j.MQTT, "unifi", None) if j.MQTT else None
        if u:
            with j.MQTT.lock:
                out["wan"] = [{"label": label, "state": state}
                              for _, (label, state) in sorted(u.wan_states().items(), key=lambda kv: kv[1][0])]
        flags = [(n, f) for n, _, f in r.get("HomeKit sensor", []) if "starlink" in n.lower()]
        out["starlink"] = [{"name": n.split(" / ")[-2] if " / " in n else n, "on": f == "open"} for n, f in flags]
        return out
    s["network"] = _safe(network, {})

    # --- security --------------------------------------------------------------
    def security():
        out = {"detections": [], "locks": [], "open": [], "low_batteries": [], "alarms": [], "safety": []}
        u = getattr(j.MQTT, "unifi", None) if j.MQTT else None
        if u:
            with j.MQTT.lock:
                cutoff = now - dt.timedelta(hours=12)
                dets = sorted(((dt.datetime.fromisoformat(w), types, cam) for w, types, cam in u.detections.values()
                               if dt.datetime.fromisoformat(w) >= cutoff), reverse=True)[:10]
                out["detections"] = [{"at": t.strftime("%-I:%M %p"), "camera": u.names.get(cam) or "camera " + cam[-4:],
                                      "types": types} for t, types, cam in dets]
                # Same rule as the briefings: the sensor keeps reporting the time of its
                # LAST occurrence, so only show ones that happened recently.
                show = dt.timedelta(minutes=float(u.cfg.get("alarm_show_minutes", 60)))
                names = getattr(j, "ALARM_NAMES", {})
                for k, v in u.alarms.items():
                    t = dt.datetime.fromisoformat(v)
                    if now - t > show:
                        continue
                    dev, kind = k.split(" ", 1)
                    tamper = kind == "tamperingDetectedAt"
                    out["alarms"].append({"what": "Sensor tampering" if tamper else names.get(kind, kind),
                                          "device": u.names.get(dev, "sensor"), "at": t.strftime("%-I:%M %p"),
                                          "level": "warn" if tamper else "crit"})
                raid = [{"progress": round(rs["progress"] * 100), "eta_h": round((rs.get("eta_ms") or 0) / 3.6e6)}
                        for rs in u.raid_sync.values()
                        if now - dt.datetime.fromisoformat(rs["seen"]) < dt.timedelta(minutes=30)]
                if raid:
                    out["raid"] = raid[0]
        off = r.get("UniFi Protect devices offline")
        if off:
            out["devices_offline"] = off[0][2]
        for name, _, f in r.get("HomeKit lock", []):
            out["locks"].append({"name": name.split(" / ")[0], "state": f})
        for name, _, f in r.get("HomeKit sensor", []):
            if f == "open" and "starlink" not in name.lower():
                out["open"].append(name.split(" / ")[-2] if name.count(" / ") >= 1 else name)
        for name, _, f in r.get("HomeKit low battery", []):
            out["low_batteries"].append(name.split(" / ")[0])
        for name, _, f in r.get("HomeKit SAFETY", []):
            out["safety"].append(name)
        return out
    s["security"] = _safe(security, {})

    # --- indoor air ---------------------------------------------------------------
    def indoor():
        items = []
        for name, raw, f in r.get("Indoor air", []):
            parts = name.split(" / ")
            items.append({"room": parts[0], "what": parts[-1].replace(" Density", "").replace("Carbon dioxide Level", "CO2"),
                          "value": f})
        return items[:12]
    s["indoor_air"] = _safe(indoor, [])

    # --- airspace ----------------------------------------------------------------------
    def airspace():
        out = {"drones": [], "aircraft": [], "tfrs": []}
        a = getattr(j, "AIRSPACE", None)
        if a:
            out["sensor"] = a.sensor_state
            cutoff = now - dt.timedelta(hours=24)
            for x in a.sightings[::-1]:
                t = dt.datetime.fromisoformat(x["at"]).astimezone().replace(tzinfo=None)
                if t >= cutoff:
                    out["drones"].append({"at": t.strftime("%-I:%M %p"), "model": x["model"],
                                          "distance": x["distance"], "near": x["near"],
                                          "miles": x.get("miles"), "dir": x.get("dir")})
            out["drones"] = out["drones"][:6]
        feeds = getattr(j, "FEEDS", {}) or {}
        la = feeds.get("low_aircraft")
        if la:
            since = (now - dt.timedelta(hours=2)).isoformat()
            out["aircraft"] = [{"at": dt.datetime.fromisoformat(h["at"]).strftime("%-I:%M %p"),
                                "what": "helicopter" if h["heli"] else h["label"], "agl": h["agl"],
                                "miles": h.get("miles"), "dir": h.get("dir")}
                               for h in la.state.get("history", []) if h["at"] >= since][-5:][::-1]
        fr = feeds.get("flight_restrictions")
        if fr:
            out["tfrs"] = [{"type": f["type"].upper(), "inside": f["inside"], "miles": f["miles"], "uas": f["uas"]}
                           for f in fr.state.get("active", {}).values()][:4]
        return out
    s["airspace"] = _safe(airspace, {})

    # --- traffic -------------------------------------------------------------------------
    def traffic():
        t = getattr(j, "TRAFFIC", None)
        if not t or t.updated is None:
            return {}
        def item(i):
            return {"text": i["text"][:160], "miles": round(i["miles"]) if i.get("miles") is not None else None,
                    "severity": i.get("severity")}
        acc = sorted((i for i in t.incidents if i["accident"]), key=lambda i: i.get("miles") or 999)
        clo = sorted(t.closures, key=lambda i: i.get("miles") or 999)
        return {"updated": t.updated, "accidents": [item(i) for i in acc[:5]],
                "closures": [item(i) for i in clo[:5]], "credit": "Data provided by 511.org"}
    s["traffic"] = _safe(traffic, {})

    # --- vehicle -----------------------------------------------------------------------
    def vehicle():
        out = {"name": first(r, "Vehicle name"), "battery": _num(first(r, "Vehicle battery")),
               "range": _num(first(r, "Vehicle range")), "charging": first(r, "Vehicle charging state"),
               "locked": first(r, "Vehicle locked"), "status": first(r, "Vehicle status")}
        return {k: v for k, v in out.items() if v is not None}
    s["vehicle"] = _safe(vehicle, {})
    return s


def encrypt_status(status: dict, key_b64: str) -> str:
    """AES-256-GCM; the page decrypts with the key from its #fragment."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    def b64d(x: str) -> bytes:
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))

    def b64e(x: bytes) -> str:
        return base64.urlsafe_b64encode(x).decode().rstrip("=")

    nonce = os.urandom(12)
    ct = AESGCM(b64d(key_b64)).encrypt(nonce, json.dumps(status, default=str).encode(), None)
    return json.dumps({"v": 1, "iv": b64e(nonce), "ct": b64e(ct)})


def ssh_put(rcfg: dict, name: str, data: bytes) -> None:
    """Write `data` to <path>/<name> on the remote host atomically (tmp + mv)."""
    import shlex
    import subprocess

    path = rcfg["path"].rstrip("/") + "/" + name
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR"]
    if rcfg.get("identity"):
        cmd += ["-i", os.path.expanduser(rcfg["identity"])]
    if rcfg.get("user"):
        cmd += ["-l", rcfg["user"]]
    q = shlex.quote(path)
    cmd += [rcfg["host"], "cat > %s.tmp && chmod 644 %s.tmp && mv %s.tmp %s" % (q, q, q, q)]
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ssh put %s failed: %s" % (name, r.stderr.decode(errors="replace").strip()[:200]))


def push_remote(rcfg: dict, status: dict, page_hash: Dict[str, str]) -> None:
    """Upload the encrypted snapshot, and the page itself when it changed."""
    key = os.environ.get("HUD_REMOTE_KEY", "")
    if not key:
        raise RuntimeError("HUD_REMOTE_KEY not set in .env")
    html = PAGE.read_bytes()
    digest = hashlib.sha1(html).hexdigest()
    if page_hash.get("page") != digest:
        ssh_put(rcfg, "index.html", html)
        ssh_put(rcfg, "version.txt", digest.encode())   # open pages reload when this changes
        page_hash["page"] = digest
    ssh_put(rcfg, "status.enc", encrypt_status(status, key).encode())


def write_status_file(status: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    tmp.replace(STATUS_FILE)


# --------------------------------------------------------------------------- live state

HISTORY_FILE = STATUS_FILE.with_name("hud-history.json")
AUDIO_DIR = STATUS_FILE.with_name("hud-audio")
LIVE: dict = {"clients": set(), "audio": None, "cfg": None, "page_hash": {}}


def _load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return []


HISTORY: list = _load_history()   # [[iso, soc, solar_kw, house_kw], ...] every 5 min, 24 h


def sample_history(status: dict, now: dt.datetime) -> None:
    e = status.get("energy") or {}
    if e.get("soc") is None:
        return
    if HISTORY and (now - dt.datetime.fromisoformat(HISTORY[-1][0])).total_seconds() < 290:
        return
    HISTORY.append([now.isoformat(timespec="minutes"), round(e["soc"], 1),
                    round(e.get("solar_kw") or 0, 2), round(e.get("house_kw") or 0, 2)])
    cutoff = (now - dt.timedelta(hours=24)).isoformat()
    HISTORY[:] = [h for h in HISTORY if h[0] >= cutoff]
    try:
        HISTORY_FILE.write_text(json.dumps(HISTORY))
    except OSError:
        pass


def _augment(status: dict) -> dict:
    status.setdefault("energy", {})["history"] = HISTORY
    if LIVE["audio"]:
        status.setdefault("voice", {})["audio"] = {k: LIVE["audio"][k] for k in ("id", "name", "at")}
    return status


async def _broadcast(msg: dict) -> None:
    payload = json.dumps(msg, default=str)
    for ws in list(LIVE["clients"]):
        try:
            await ws.send_str(payload)
        except Exception:
            LIVE["clients"].discard(ws)


async def announce(j, cfg: dict, wav: Path, name: str, text: str) -> None:
    """A briefing is starting on the HomePods: hand its audio to the displays.
    Called just before playback; converts quickly, then pushes in the background."""
    import subprocess
    import shutil

    hcfg = cfg.get("hud") or {}
    plays = hcfg.get("play_broadcasts")
    if not hcfg.get("enabled") or (plays is not None and name not in plays):
        return
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    aid = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = AUDIO_DIR / "latest.m4a"
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    await asyncio.to_thread(subprocess.run, [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac",
                                             "-b:a", "64k", "-ac", "1", str(out)], check=True)
    LIVE["audio"] = {"id": aid, "name": name, "at": dt.datetime.now().strftime("%-I:%M %p"), "path": out}
    await _broadcast({"type": "audio", "id": aid, "name": name})

    rcfg = hcfg.get("remote") or {}
    if rcfg.get("enabled") and os.environ.get("HUD_REMOTE_KEY"):
        async def push():
            try:
                data = out.read_bytes()
                await asyncio.to_thread(ssh_put, rcfg, "audio.enc", encrypt_bytes(data, os.environ["HUD_REMOTE_KEY"]).encode())
                status = _augment(build_status(j, cfg))
                await asyncio.to_thread(push_remote, rcfg, status, LIVE["page_hash"])
            except Exception as e:
                log.warning("HUD audio push failed: %s", e)
        asyncio.create_task(push())


def encrypt_bytes(data: bytes, key_b64: str) -> str:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    d = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    e = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
    nonce = os.urandom(12)
    return json.dumps({"v": 1, "iv": e(nonce), "ct": e(AESGCM(d(key_b64)).encrypt(nonce, data, None))})


async def serve(j, cfg: dict) -> None:
    """Local HUD server, status file and remote push."""
    from aiohttp import web, WSMsgType

    hcfg = cfg.get("hud") or {}
    LIVE["cfg"] = cfg
    token = os.environ.get("HUD_TOKEN", "")
    clients = LIVE["clients"]
    current = {"status": _augment(build_status(j, cfg)), "hash": ""}

    def authorized(request) -> bool:
        if not token:
            return True
        return request.query.get("token") == token or request.cookies.get("jarvis_hud") == token

    async def index(request):
        if not authorized(request):
            return web.Response(status=401, text="Add ?token=... to the address (HUD_TOKEN in .env).")
        html = PAGE.read_text().replace("<!--JARVIS_SOURCE-->",
                                        '<script>window.JARVIS_SOURCE = "ws";</script>')
        resp = web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})
        if token and request.query.get("token") == token:
            resp.set_cookie("jarvis_hud", token, max_age=365 * 86400, httponly=True, samesite="Strict")
        return resp

    async def status_json(request):
        if not authorized(request):
            return web.Response(status=401)
        return web.json_response(current["status"], dumps=lambda o: json.dumps(o, default=str))

    async def audio(request):
        if not authorized(request) or not LIVE["audio"]:
            return web.Response(status=404)
        return web.FileResponse(LIVE["audio"]["path"], headers={"Cache-Control": "no-store"})

    async def ws_handler(request):
        if not authorized(request):
            return web.Response(status=401)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        clients.add(ws)
        await ws.send_str(json.dumps(current["status"], default=str))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/status.json", status_json)
    app.router.add_get("/audio/latest.m4a", audio)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host, port = hcfg.get("bind", "0.0.0.0"), int(hcfg.get("port", 8790))
    await web.TCPSite(runner, host, port).start()
    log.info("HUD at http://<this-mac>:%d/%s", port, "?token=…" if token else "")

    last_file = 0.0
    page_mtime = PAGE.stat().st_mtime
    every = float(hcfg.get("push_seconds", 3))
    while True:
        try:
            now = dt.datetime.now()
            status = build_status(j, cfg, now)
            sample_history(status, now)
            status = _augment(status)
            # compare without the timestamp so idle seconds don't count as changes
            digest = hashlib.sha1(json.dumps({k: v for k, v in status.items() if k != "generated"},
                                             sort_keys=True, default=str).encode()).hexdigest()
            current["status"] = status
            if digest != current["hash"]:
                current["hash"] = digest
                await _broadcast(status)
            mtime = PAGE.stat().st_mtime
            if mtime != page_mtime:          # page edited: tell open displays to reload
                page_mtime = mtime
                await _broadcast({"type": "reload"})
            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_file >= float(hcfg.get("file_seconds", 60)):
                last_file = loop_now
                await asyncio.to_thread(write_status_file, status)
                rcfg = hcfg.get("remote") or {}
                if rcfg.get("enabled"):
                    try:
                        await asyncio.to_thread(push_remote, rcfg, status, LIVE["page_hash"])
                    except Exception as e:
                        log.warning("HUD remote push failed: %s", e)
        except Exception as e:
            log.warning("HUD update failed: %s", e)
        await asyncio.sleep(every)
