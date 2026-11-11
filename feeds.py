"""External feeds for Jarvis: weather alerts, earthquakes, wildfires, outdoor
air quality, flight restrictions and low aircraft near home.

Each feed polls on its own interval, keeps a little state on disk (so restarts
don't re-announce old items), emits real-time events to listeners (the
Alerter) and offers summary lines for the briefing prompt.

Sources and their terms:
  - NWS api.weather.gov: public; requests carry a User-Agent identifying the app.
  - USGS earthquake GeoJSON feeds: public domain, updated every minute.
  - NIFC WFIGS current incidents (ArcGIS): public; includes CAL FIRE incidents.
    (CAL FIRE's own incident API blocks automated clients.)
  - IQAir AirVisual (free Community plan, IQAIR_API_KEY): 10,000 calls/month,
    5/minute; credited as IQAir when spoken.
  - OSP airspace server (AIRSPACE_URL / AIRSPACE_API_KEY): NOTAMs and live targets.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("jarvis")

USER_AGENT = "jarvis-home-briefings/1.0 (personal, non-commercial)"
STATE_DIR = Path(os.path.expanduser("~/Library/Caches/jarvis/feeds"))


def miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(a))


def bearing_word(lat1: float, lng1: float, lat2: float, lng2: float) -> str:
    y = math.sin(math.radians(lng2 - lng1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lng2 - lng1)))
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][int((deg + 22.5) // 45) % 8]


def parse_time(value) -> Optional[dt.datetime]:
    """ISO string or epoch milliseconds -> aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_hm(t: Optional[dt.datetime]) -> str:
    return t.astimezone().strftime("%-I:%M %p") if t else "?"


class Feed:
    """Base class: interval polling, persistent state, listeners."""

    name = "feed"

    def __init__(self, cfg: dict, home: Tuple[float, float]):
        self.cfg = cfg
        self.home = home
        self.interval = float(cfg.get("poll_seconds", 300))
        self.listeners: List = []
        self.path = STATE_DIR / (self.name + ".json")
        try:
            self.state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.primed = bool(self.state.get("primed"))
        self.ok_at = dt.datetime.fromtimestamp(self.state['ok_at']) if self.state.get('ok_at') else None
        self.journal = None

    def save(self) -> None:
        self.state["primed"] = self.primed
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    def emit(self, ev: dict) -> None:
        if not self.primed:
            return  # first poll after install: record, don't announce
        log.info("event: %s", ev)
        for listener in self.listeners:
            listener(ev)

    def distance(self, lat: float, lng: float) -> float:
        return miles_between(self.home[0], self.home[1], lat, lng)

    def direction(self, lat: float, lng: float) -> str:
        return bearing_word(self.home[0], self.home[1], lat, lng)

    async def poll(self, session) -> None:
        raise NotImplementedError

    def lines(self, now: dt.datetime) -> List[str]:
        return []

    async def get_json(self, session, url: str, **kw):
        headers = dict(kw.pop("headers", {}), **{"User-Agent": USER_AGENT})
        async with session.get(url, headers=headers, **kw) as r:
            if r.status != 200:
                raise RuntimeError("%s HTTP %s" % (self.name, r.status))
            return json.loads(await r.text())

    async def run(self) -> None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            while True:
                try:
                    await self.poll(session)
                    self.primed = True
                    self.ok_at = dt.datetime.now()
                    self.state['ok_at'] = self.ok_at.timestamp()
                    if self.journal:
                        self.journal.health(self.name, 'ok', 'last poll succeeded', self.interval * 3)
                    self.save()
                except Exception as e:  # never log URLs: some carry API keys
                    log.warning("%s poll failed: %s", self.name, str(e)[:120] if "key=" not in str(e) else type(e).__name__)
                    if self.journal:
                        self.journal.health(self.name, 'error', type(e).__name__, self.interval * 3)
                await asyncio.sleep(self.interval)


# --------------------------------------------------------------------------- NWS


class WeatherAlerts(Feed):
    """Active NWS watches/warnings/advisories for the house's exact point."""

    name = "weather_alerts"

    async def poll(self, session) -> None:
        url = "https://api.weather.gov/alerts/active?point=%.4f,%.4f" % self.home
        data = await self.get_json(session, url, headers={"Accept": "application/geo+json"})
        active = {}
        for f in data.get("features", []):
            p = f.get("properties") or {}
            if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
                continue
            # Updates reference the original; key by event + onset so an update isn't "new".
            key = "%s|%s" % (p.get("event"), (p.get("onset") or p.get("effective") or "")[:13])
            active[key] = {"event": p.get("event", "Weather alert"), "severity": p.get("severity", "Unknown"),
                           "headline": p.get("headline", ""), "ends": p.get("ends") or p.get("expires"),
                           "instruction": (p.get("instruction") or "")[:300]}
        known = self.state.get("active", {})
        for key, a in active.items():
            if key not in known:
                ends = parse_time(a["ends"])
                self.emit({"kind": "weather_alert", "event": a["event"], "severity": a["severity"],
                           "until": ends.astimezone().strftime("%A %-I %p") if ends else "further notice"})
        self.state["active"] = active

    def lines(self, now):
        out = []
        for a in self.state.get("active", {}).values():
            ends = parse_time(a["ends"])
            if ends and ends.timestamp() <= now.timestamp():
                continue
            out.append("- %s (%s) until %s" % (a["event"], a["severity"],
                                               ends.astimezone().strftime("%a %-I %p") if ends else "further notice"))
        return out


# --------------------------------------------------------------------------- USGS


def spoken_place(place: str) -> str:
    """USGS '4 km NW of Portola Valley, CA' -> 'near Portola Valley'."""
    import re

    near = re.match(r"^\d+(\.\d+)? km [NSEW]{1,3} of ", place or "")
    place = re.sub(r", (CA|California)$", "", (place or "")[near.end() if near else 0:])
    return ("near " + place if near else place) or "location unknown"


class Earthquakes(Feed):
    """USGS real-time feed; announce quakes by magnitude/distance tiers."""

    name = "earthquakes"

    def __init__(self, cfg, home):
        super().__init__(cfg, home)
        # (min magnitude, max miles): small and close, or big and far
        self.tiers = [tuple(t) for t in cfg.get("tiers", [[3.0, 50], [4.5, 150], [6.0, 500]])]

    def notable(self, mag: float, miles: float) -> bool:
        return any(mag >= m and miles <= d for m, d in self.tiers)

    async def poll(self, session) -> None:
        feed = "all_hour" if self.primed else "all_day"
        data = await self.get_json(session, "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/%s.geojson" % feed)
        seen: Dict[str, dict] = self.state.get("seen", {})
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if p.get("type") != "earthquake" or p.get("mag") is None or len(g) < 2:
                continue
            miles = self.distance(g[1], g[0])
            if not self.notable(float(p["mag"]), miles):
                continue
            qid = f.get("id")
            if qid in seen:
                continue
            q = {"mag": round(float(p["mag"]), 1), "place": spoken_place(p.get("place", "")), "miles": round(miles),
                 "dir": self.direction(g[1], g[0]), "time": p.get("time"), "felt": p.get("felt")}
            seen[qid] = q
            self.emit({"kind": "earthquake", "magnitude": "%.1f" % q["mag"], "distance": "%d miles" % q["miles"],
                       "direction": q["dir"], "place": q["place"], "miles": miles, "mag": q["mag"]})
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).timestamp() * 1000
        self.state["seen"] = {k: v for k, v in seen.items() if (v.get("time") or 0) >= cutoff}

    def recent(self, since: dt.datetime) -> List[dict]:
        ts = since.timestamp() * 1000
        return sorted((q for q in self.state.get("seen", {}).values() if (q.get("time") or 0) >= ts),
                      key=lambda q: q["time"])

    def lines(self, now):
        return ["- M%.1f %s, %d miles %s of home, at %s" % (q["mag"], q["place"], q["miles"], q["dir"],
                                                           local_hm(parse_time(q["time"])))
                for q in self.recent(now - dt.timedelta(hours=24))]


# --------------------------------------------------------------------------- wildfires


def acres_text(acres) -> str:
    if acres is None:
        return "size unknown"
    if acres < 1:
        return "under an acre"
    return "{:,.0f} acres".format(acres)


class Wildfires(Feed):
    """Current wildfire incidents near home from NIFC WFIGS (includes CAL FIRE)."""

    name = "wildfires"
    URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
           "WFIGS_Incident_Locations_Current/FeatureServer/0/query")

    async def poll(self, session) -> None:
        radius = float(self.cfg.get("watch_miles", 60))
        params = {"where": "IncidentTypeCategory='WF'", "geometry": "%f,%f" % (self.home[1], self.home[0]),
                  "geometryType": "esriGeometryPoint", "inSR": "4326", "distance": str(radius),
                  "units": "esriSRUnit_StatuteMile", "spatialRel": "esriSpatialRelIntersects",
                  "outFields": "IncidentName,IncidentSize,PercentContained,FireDiscoveryDateTime,POOCounty,IrwinID",
                  "f": "geojson"}
        data = await self.get_json(session, self.URL, params=params)
        alert_miles = float(self.cfg.get("alert_miles", 25))
        fires = {}
        known = self.state.get("fires", {})
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if len(g) < 2:
                continue
            fid = p.get("IrwinID") or p.get("IncidentName")
            miles = self.distance(g[1], g[0])
            fire = {"name": (p.get("IncidentName") or "Unnamed").title() + " Fire", "acres": p.get("IncidentSize"),
                    "contained": p.get("PercentContained"), "county": p.get("POOCounty"), "miles": round(miles),
                    "dir": self.direction(g[1], g[0]), "discovered": p.get("FireDiscoveryDateTime")}
            fires[fid] = fire
            if fid not in known and miles <= alert_miles:
                self.emit({"kind": "wildfire", "fire": fire["name"], "distance": "%d miles" % fire["miles"],
                           "direction": fire["dir"], "county": fire.get("county") or "", "miles": miles,
                           "acres": acres_text(fire["acres"])})
        self.state["fires"] = fires

    def lines(self, now):
        out = []
        for fire in sorted(self.state.get("fires", {}).values(), key=lambda f: f["miles"]):
            acres = acres_text(fire.get("acres"))
            cont = "%d%% contained" % fire["contained"] if fire.get("contained") is not None else "containment unknown"
            out.append("- %s, %s County: %s, %s, %d miles %s" % (fire["name"], fire.get("county") or "?", acres,
                                                                cont, fire["miles"], fire["dir"]))
        return out


# --------------------------------------------------------------------------- outdoor air (IQAir)


AQI_BANDS = [(50, "good"), (100, "moderate"), (150, "unhealthy for sensitive groups"),
             (200, "unhealthy"), (300, "very unhealthy"), (10_000, "hazardous")]
POLLUTANTS = {"p2": "PM2.5", "p1": "PM10", "o3": "ozone", "n2": "nitrogen dioxide", "s2": "sulfur dioxide",
              "co": "carbon monoxide"}


def aqi_band(aqi: int) -> str:
    return next(name for top, name in AQI_BANDS if aqi <= top)


class OutdoorAir(Feed):
    """US AQI for the nearest IQAir city; announces threshold crossings."""

    name = "outdoor_air"

    async def poll(self, session) -> None:
        key = os.environ.get("IQAIR_API_KEY")
        if not key:
            return
        data = await self.get_json(session, "https://api.airvisual.com/v2/nearest_city",
                                   params={"lat": "%.4f" % self.home[0], "lon": "%.4f" % self.home[1], "key": key})
        d = data.get("data") or {}
        pol = (d.get("current") or {}).get("pollution") or {}
        if pol.get("aqius") is None:
            raise ValueError('IQAir response has no air quality observation')
        aqi = int(pol["aqius"])
        self.state["current"] = {"aqi": aqi, "band": aqi_band(aqi), "main": POLLUTANTS.get(pol.get("mainus"), pol.get("mainus")),
                                 "city": d.get("city"), "ts": pol.get("ts")}
        # announce each threshold once per crossing; re-arm 10 points below
        fired = self.state.setdefault("fired", [])
        for th in sorted(self.cfg.get("thresholds", [100, 150]), reverse=True):
            if aqi > th and th not in fired:
                fired.append(th)
                self.emit({"kind": "air_quality", "aqi": str(aqi), "band": aqi_band(aqi), "threshold": th,
                           "pollutant": self.state["current"]["main"] or "particulates"})
                break
        self.state["fired"] = [th for th in fired if aqi > th - 10]

    def lines(self, now):
        c = self.state.get("current")
        if not c:
            return []
        return ["- Outdoor air (IQAir, %s): US AQI %d, %s, mainly %s" % (c["city"], c["aqi"], c["band"], c["main"])]


# --------------------------------------------------------------------------- airspace: TFRs / NOTAMs


def polygon_contains(lng: float, lat: float, ring: List[List[float]]) -> bool:
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class FlightRestrictions(Feed):
    """TFRs and drone-relevant NOTAMs within a radius of home (airspace server)."""

    name = "flight_restrictions"
    TYPES = {"tfr", "uas", "sua", "disaster"}

    async def poll(self, session) -> None:
        url = (os.environ.get("AIRSPACE_URL") or "").rstrip("/")
        key = os.environ.get("AIRSPACE_API_KEY")
        if not url or not key:
            return
        data = await self.get_json(session, url + "/api/notams", params={"status": "active", "limit": "1000"},
                                   headers={"X-API-Key": key})
        radius = float(self.cfg.get("watch_miles", 10))
        now = dt.datetime.now(dt.timezone.utc)
        found = {}
        for n in data.get("notams", []):
            subject = n.get("subject") or ""
            relevant = (n.get("notam_type") in self.TYPES or n.get("affects_uas")
                        or "TEMPORARY FLIGHT RESTRICTION" in subject.upper())
            geom = n.get("geometry") or {}
            if not relevant or geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            rings = geom["coordinates"] if geom["type"] == "Polygon" else [p[0] for p in geom["coordinates"]]
            inside = any(polygon_contains(self.home[1], self.home[0], r) for r in rings)
            nearest = min(self.distance(pt[1], pt[0]) for r in rings for pt in r)
            if not inside and nearest > radius:
                continue
            start, until = parse_time(n.get("effective_from")), parse_time(n.get("effective_until"))
            if (start and start > now + dt.timedelta(hours=24)) or (until and until < now):
                continue
            found[n["notam_id"]] = {"type": n.get("notam_type"), "uas": bool(n.get("affects_uas")),
                                    "inside": inside, "miles": round(nearest, 1),
                                    "subject": " ".join(subject.split())[:220],
                                    "from": n.get("effective_from"), "until": n.get("effective_until"),
                                    "floor_m": n.get("floor_m"), "ceiling_m": n.get("ceiling_m")}
        known = self.state.get("active", {})
        for nid, f in found.items():
            if nid not in known:
                where = "over the house" if f["inside"] else "%.0f miles from home" % f["miles"]
                self.emit({"kind": "flight_restriction", "where": where,
                           "restriction": "temporary flight restriction" if f["type"] == "tfr" or "TEMPORARY FLIGHT" in f["subject"].upper()
                           else "airspace notice affecting drones" if f["uas"] else "airspace restriction",
                           "until": local_hm(parse_time(f["until"])) if f["until"] else "further notice",
                           "subject": f["subject"]})
        self.state["active"] = found

    def lines(self, now):
        out = []
        for f in self.state.get("active", {}).values():
            where = "covers the house" if f["inside"] else "%.0f mi away" % f["miles"]
            until = parse_time(f["until"])
            out.append("- %s%s (%s, until %s): %s" % (f["type"].upper(), ", affects drones" if f["uas"] else "",
                                                      where, until.astimezone().strftime("%a %-I:%M %p") if until else "further notice",
                                                      f["subject"][:160]))
        return out


# --------------------------------------------------------------------------- airspace: low aircraft


class LowAircraft(Feed):
    """Manned aircraft low and close to the house (ADS-B/UAT via the airspace
    server); notes helicopters that linger (circling) nearby."""

    name = "low_aircraft"

    async def poll(self, session) -> None:
        url = (os.environ.get("AIRSPACE_URL") or "").rstrip("/")
        key = os.environ.get("AIRSPACE_API_KEY")
        if not url or not key:
            return
        data = await self.get_json(session, url + "/api/targets", headers={"X-API-Key": key})
        radius = float(self.cfg.get("radius_miles", 2))
        ceiling = float(self.cfg.get("max_altitude_ft_agl", 1500))
        ground = float(self.cfg.get("home_elevation_ft", 820))
        linger = float(self.cfg.get("linger_minutes", 5))
        now = dt.datetime.now()
        tracks: Dict[str, dict] = self.state.get("tracks", {})
        seen_now = set()
        for t in data.get("targets", []):
            proto = (t.get("source_protocol") or "").lower()
            if "remote" in proto or t.get("latitude") is None or t.get("altitude_ft") is None:
                continue  # drones are handled by the airspace drone monitor
            miles = self.distance(t["latitude"], t["longitude"])
            agl = float(t["altitude_ft"]) - ground
            if miles > radius or agl > ceiling:
                continue
            vid = str(t.get("vehicle_id"))
            seen_now.add(vid)
            vtype = (t.get("vehicle_type") or "aircraft").lower()
            heli = "heli" in vtype or "rotor" in vtype
            label = t.get("callsign") or ("helicopter" if heli else "aircraft")
            tr = tracks.get(vid) or {"first": now.isoformat(), "announced": None, "loiter_announced": None}
            tr.update(last=now.isoformat(), label=label, heli=heli, miles=round(miles, 1), agl=int(agl),
                      dir=self.direction(t["latitude"], t["longitude"]))
            tracks[vid] = tr
            kind_word = "helicopter" if heli else "aircraft"
            if not tr["announced"]:
                tr["announced"] = now.isoformat()
                self.state.setdefault("history", []).append(
                    {"at": now.isoformat(), "label": label, "heli": heli, "agl": int(agl),
                     "miles": round(miles, 1), "dir": tr["dir"]})
                self.emit({"kind": "low_aircraft", "aircraft": kind_word, "callsign": str(t.get("callsign") or ""),
                           "altitude": "%d feet" % max(0, round(agl, -2)), "distance": "%.1f miles" % miles,
                           "direction": tr["dir"], "miles": miles})
            elif (heli and not tr["loiter_announced"]
                  and now - dt.datetime.fromisoformat(tr["first"]) >= dt.timedelta(minutes=linger)):
                tr["loiter_announced"] = now.isoformat()
                self.emit({"kind": "aircraft_loiter", "aircraft": kind_word, "callsign": str(t.get("callsign") or ""),
                           "minutes": str(int(linger)), "direction": tr["dir"]})
        # forget aircraft that left 30+ minutes ago (a later return is a new sighting)
        cutoff = now - dt.timedelta(minutes=30)
        self.state["tracks"] = {k: v for k, v in tracks.items()
                                if k in seen_now or dt.datetime.fromisoformat(v["last"]) > cutoff}
        self.state["history"] = [h for h in self.state.get("history", [])
                                 if h["at"] >= (now - dt.timedelta(days=8)).isoformat()]

    def lines(self, now):
        since = (now - dt.timedelta(hours=2)).isoformat()
        return ["- %s: %s, about %d ft above ground, within %s miles" % (
                    local_hm(dt.datetime.fromisoformat(h["at"])),
                    "helicopter" if h["heli"] else "aircraft " + h["label"], h["agl"], self.cfg.get("radius_miles", 2))
                for h in self.state.get("history", []) if h["at"] >= since]


class SolarWeather(Feed):
    """NWS hourly cloud cover intervals for the optional energy planner."""
    name = 'solar_weather'

    async def poll(self, session):
        url = self.state.get('grid_url')
        if not url:
            point = await self.get_json(session, 'https://api.weather.gov/points/%.4f,%.4f' % self.home)
            url = point['properties']['forecastGridData']
        if not url.startswith('https://api.weather.gov/'):
            raise ValueError('unexpected NWS grid URL')
        grid = await self.get_json(session, url)
        hours = []
        for item in grid['properties']['skyCover']['values']:
            start, duration = item['validTime'].split('/')
            match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', duration)
            at = parse_time(start)
            value = item.get('value')
            if not match or not at or value is None:
                continue
            d, h, m, s = [int(v or 0) for v in match.groups()]
            end = at.timestamp() + d * 86400 + h * 3600 + m * 60 + s
            if end > time.time():
                hours.append({'start': at.timestamp(), 'end': end, 'cloud': max(0, min(100, float(value)))})
        if not hours:
            raise ValueError('NWS returned no usable cloud forecast')
        self.state.update(grid_url=url, hours=hours)


FEEDS = {"weather_alerts": WeatherAlerts, "earthquakes": Earthquakes, "wildfires": Wildfires,
         "outdoor_air": OutdoorAir, "flight_restrictions": FlightRestrictions, "low_aircraft": LowAircraft}
FEEDS['solar_weather'] = SolarWeather


def build_feeds(cfg: dict) -> Dict[str, Feed]:
    fcfg = cfg.get("feeds") or {}
    if not fcfg or fcfg.get('enabled') is False:
        return {}
    home = fcfg.get("home")
    if not home:
        raise ValueError("config: set feeds.home: {lat: ..., lng: ...}")
    home_t = (float(home["lat"]), float(home["lng"]))
    out = {}
    for name, cls in FEEDS.items():
        c = fcfg.get(name) or {}
        if name == 'solar_weather' and not c.get('enabled', False):
            continue
        if name == 'outdoor_air' and not os.environ.get('IQAIR_API_KEY'):
            continue
        if name in ('flight_restrictions', 'low_aircraft') and not (os.environ.get('AIRSPACE_URL') and os.environ.get('AIRSPACE_API_KEY')):
            continue
        if c.get("enabled", True):
            out[name] = cls(c, home_t)
    return out
