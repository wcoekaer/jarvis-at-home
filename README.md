# Jarvis at home

A spoken home assistant for macOS and Apple HomePods, in the style of a calm British AI
butler. It runs on a Mac as a background service and:

- **Scheduled briefings.** On a schedule (e.g. hourly 07:00–21:00), it writes a short update
  with an LLM (with web search), converts it to speech and streams it to chosen HomePods:
  time and greeting, weather from your own station, news, calendar, traffic, energy, home
  and security status. It reports only what changed since the previous update.
- **Real-time alerts.** It also speaks when something happens: a person on a camera, the
  internet down, a smoke, CO or leak alarm, the home battery crossing 50% or 30%, a drone near
  the house, an accident or road closure, a weather warning, an earthquake, a wildfire, poor
  outdoor air, a new flight restriction, a low or circling helicopter.
- **Siri.** "Hey Siri, turn on Jarvis Update" runs a briefing on demand. Other switches
  run a security sweep, an energy report or quiet mode.
- **Special briefings.** There's a 7:00 overnight security recap, a 21:00 energy summary,
  a 22:00 security sweep and a Sunday weekly recap.

```
                         ┌──────────── jarvis.py (launchd daemon) ─────────────┐
 MQTT broker ──────────► │ MqttState: Powerpack, UniFi Protect, ping, weather, │
  (your home data,       │   Tesla, HomeKit bridge, stock …  + topic discovery │
   HomeKit bridge)       │ feeds.py: NWS, USGS, NIFC fires, IQAir, TFRs, ADS-B  │
 Airspace server ──────► │ AirspaceMonitor (drones)   TrafficMonitor (511.org) │
 511.org, NWS, USGS … ─► │                                                     │
 JarvisContext app ────► │ context.json: calendar, reminders, mail             │
                         │        │ events                    │ schedule       │
                         │        ▼                           ▼                │
                         │    Alerter ──────────────► LLM (OpenAI/Anthropic)   │
                         │        │  phrases               + web search        │
                         │        ▼                           │                │
                         │   TTS (OpenAI gpt-4o-mini-tts / macOS say)          │
                         │        ▼                                            │
                         │   AirPlay / RAOP via pyatv ──────► HomePods         │
                         │ HAP-python accessory ◄──── "Hey Siri, turn on …"    │
                         └─────────────────────────────────────────────────────┘
```

Everything except the core (LLM + TTS + HomePods) is optional. Leave a section disabled, or
leave its key out of `.env`, and Jarvis simply doesn't use it.

---

## 1. Requirements

| Need | Notes |
|---|---|
| A Mac that stays on | Runs as a LaunchDaemon (starts at boot, no login needed). Developed on macOS 27, Apple silicon. |
| Homebrew | `python3` (3.9+; developed on 3.14), `ffmpeg`, and `xcodegen` for the HomeKit bridge |
| HomePods | Home app → Home Settings → Speakers & TV → *Allow Speaker & TV Access*: "Everyone on the same network". `./jarvis devices` must show them as `streamable`. |
| OpenAI API key | Briefing text (`gpt-5.5`) and voice (`gpt-4o-mini-tts`). Anthropic Claude is supported for the text instead (`llm.provider: anthropic`). |
| Optional | An MQTT broker with your home data; Xcode and an Apple developer account (HomeKit bridge); free keys for 511.org and IQAir; access to an OSP airspace server |

```bash
brew install python ffmpeg xcodegen
```

## 2. Install

```bash
git clone https://github.com/wcoekaer/jarvis-at-home.git ~/git/jarvis
cd ~/git/jarvis
cp .env.example .env && chmod 600 .env        # fill in keys (section 3)
cp config.example.yaml config.yaml            # fill in every "<--" value (section 4)

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # install.sh also does this
./jarvis devices                              # list HomePods; use these exact names in config
./jarvis test hourly --no-play                # write a briefing -> last-hourly.wav (no playback)
./jarvis test hourly --homepod Kitchen        # write and play one now
./jarvis say "Testing, one two." --homepod Kitchen

./install.sh daemon                           # start at boot (asks for sudo; run in a real terminal)
./install.sh context                          # calendar / reminders / mail helper (optional)
./install.sh homekit-bridge                   # Apple Home -> MQTT bridge (optional, section 7)
```

After changing `config.yaml` or the code: `./install.sh restart`.

## 3. Secrets (`.env`)

All keys live in `.env` (git-ignored, mode 600). They're read by the daemon, `install.sh` and
the bridge app. launchd services don't see your shell environment, which is why they live in
a file. `.env.example` lists them all.

| Variable | Used for |
|---|---|
| `OPENAI_API_KEY` | briefing text and voice |
| `ANTHROPIC_API_KEY` | only if `llm.provider: anthropic` |
| `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD` (`MQTT_PORT`) | your broker |
| `AIRSPACE_URL`, `AIRSPACE_API_KEY` | OSP airspace server (drones, TFRs, aircraft) |
| `TRAFFIC_API_KEY` | 511.org open-data token |
| `IQAIR_API_KEY` | IQAir AirVisual (free Community plan) |
| `JARVIS_TEAM_ID`, `JARVIS_BUNDLE_PREFIX` | code signing for the JarvisHome bridge |

## 4. Configuration (`config.yaml`)

Start from `config.example.yaml`. It's annotated throughout; the main sections are:

| Section | What it controls |
|---|---|
| `homepods`, `location`, `listener_name`, `name_probability` | where it plays, where you are, how often it uses your name |
| `llm`, `tts`, `playback` | model, effort, web search; voice (`echo`, British butler instructions, speed 1.1); volume and prep time |
| `prompt`, `segments`, `default_segment` | the briefing prompt and time-of-day extras (07:00 day ahead and overnight recap, midday markets, late-afternoon traffic, 21:00 energy and wind-down) |
| `broadcasts` | schedules: `every_minutes`+`start`/`end` or `times`; `days`; per-broadcast `homepods`/`prompt`; `on_demand: true` for Siri-only |
| `mqtt.topics` | which MQTT values go into the briefing (`field` = dotted JSON path, `scale`/`offset`/`decimals`/`unit`, `include`/`exclude`/`only_values`, `count_value`, `max_age_minutes`) |
| `mqtt.unifi`, `mqtt.energy`, `mqtt.internet` | UniFi Protect tracker, Powerpack daily totals and battery thresholds, ping-based internet monitor |
| `mqtt.say_topic`, `mqtt.brief_topic` | let other systems make Jarvis speak or run a briefing (section 9) |
| `mqtt.discover` | subscribe to everything and keep a topic catalog (how new feeds get noticed) |
| `airspace`, `traffic`, `feeds` | drones, 511 traffic, weather/quake/fire/air/TFR/aircraft feeds |
| `homekit` | the Siri accessory: switches, setup code, acknowledgements, quiet mode |
| `alerts`, `alert_homepods` | real-time alert rules (section 6) |

Prompt placeholders: `{date} {time} {weekday} {part_of_day} {location} {name_instruction}
{flourish_instruction} {segment_instructions} {recent_briefings} {personal_context}
{home_status} {overnight_security} {energy_today} {traffic} {drones} {hazards}
{airspace_notices} {weekly_summary}`.

## 5. Data sources

| Source | How | Notes and terms |
|---|---|---|
| **MQTT broker** | `mqtt.topics`, wildcards OK | Latest value per topic kept in memory and cached to disk. Examples: Tesla Powerpack (`/tesla/powerpack/state`, `teslaapi/energy/live_status`), Tesla vehicle (`teslaapi/vehicle_data_<name>`), WeatherFlow Tempest (`http_weatherflow`, hourly), telegraf ping (`ping_ping`), a stock price, UniFi Protect (`/unifi/protect/<console>/telemetry`) |
| **UniFi Protect** (via MQTT) | `mqtt.unifi` | WAN link state, recorder RAID/disk health, sensor batteries and smoke/CO/leak alarms, person detections (cameras with smart detection) and motion (`motion_cameras`) |
| **Apple Home** | JarvisHome bridge (section 7) | locks, doors, contact sensors, leak/smoke/CO, low batteries, indoor air quality (AQI, PM2.5/PM10, VOC, CO₂) |
| **Calendar, Reminders, Mail** | JarvisContext helper (section 8) | snapshot every 5 min while you're logged in |
| **OSP airspace server** | `airspace`, `feeds.flight_restrictions`, `feeds.low_aircraft` | Drones seen by *one* Remote ID sensor (`/api/drones`, filtered by `last_sensor`), with your own drone excluded by serial; NOTAM/TFR polygons near home; live ADS-B/UAT targets |
| **511.org traffic** | `traffic` | Accidents and full road closures in one county (incidents API + WZDx work zones). **Limit 60 requests/hour per token**: Jarvis polls about 15/h, caps at 40/h (persisted), honours `RateLimit-Remaining`/429. Per the 511 Data Disseminator Agreement §5.b, spoken output says *"Data provided by 511.org"*; no 511 marks in naming. Responses echo the token, so URLs and bodies are never logged. |
| **National Weather Service** | `feeds.weather_alerts` | `api.weather.gov/alerts/active?point=lat,lon`, public. Moderate+ alerts are announced; minor ones appear in briefings. |
| **USGS earthquakes** | `feeds.earthquakes` | real-time GeoJSON; `tiers` = (min magnitude, max miles) |
| **Wildfires** | `feeds.wildfires` | NIFC WFIGS current incidents (public ArcGIS, includes CAL FIRE incidents). CAL FIRE's own API blocks automated clients. |
| **IQAir** | `feeds.outdoor_air` | free Community plan: 10,000 calls/month; polled every 30 min (~1,450/month); credited as IQAir when spoken |
| **Web search** | via the LLM | news, forecast, markets, PG&E outages/PSPS, fire weather (backstop) |

### Internet monitoring

Internet reachability comes from telegraf ping data on MQTT (`ping_ping`: about two single
pings a second to 1.1.1.1 from another machine on the network), configured under
`mqtt.internet`:

- **Down:** no replies for 15 seconds. **Back up:** 10 replies in a row; Jarvis says how long
  the outage lasted.
- **Degraded:** 10% or more packet loss, or 150 ms or more average latency, over 5 minutes.
- **No ping data:** if the ping data stops for 3 minutes, Jarvis reports that as a
  ping-collector problem, not an internet outage.

A gateway WAN port counts as offline only when its link drops (cable or modem). The ping goes
out through whichever WAN is working, so it shows whether the house has internet, not which
WAN link is up.

## 6. Real-time alerts

Each rule in `alerts` matches an event `kind`, picks one of its `messages` at random and
speaks it on `alert_homepods` (or the rule's `homepods`/`volume`). Optional filters:
`types`, `cameras`, `ports`, `threshold`, `severities`, `max_miles`, `hours`, and
`accessory`/`room`/`characteristic`/`text` for HomeKit events. `cooldown_minutes` applies
per rule. Phrases without per-event placeholders are synthesized once and cached
(`~/Library/Caches/jarvis/alerts/`), so they play within a couple of seconds. Alerts never
cut into a briefing: playback is serialized. Every spoken alert is logged to
`~/Library/Logs/jarvis/alerts.jsonl`, which feeds the weekly recap. Rules marked
`urgent: true` (smoke, CO, leak, battery at 30%) play even in quiet mode.

| kind | from | placeholders |
|---|---|---|
| `detection` | UniFi camera (person / motion) | `{camera} {types}` |
| `wan_offline`, `wan_online` | UniFi gateway | `{port}` |
| `alarm` | UniFi sensor | `{device} {detail}` |
| `battery_low` | Powerpack (50/30 %) | `{level} {threshold}` |
| `homekit` | HomeKit bridge | `{accessory} {room} {characteristic} {text}` |
| `drone`, `drone_sensor_offline/online` | airspace server | `{model} {distance} {near} {sensor}` |
| `traffic_accident`, `road_closure` | 511.org | `{text} {distance} {severity}` |
| `weather_alert` | NWS | `{event} {severity} {until}` |
| `earthquake` | USGS | `{magnitude} {distance} {direction} {place}` |
| `wildfire` | NIFC | `{fire} {distance} {direction} {acres} {county}` |
| `air_quality` | IQAir | `{aqi} {band} {pollutant}` |
| `flight_restriction` | airspace server | `{restriction} {where} {until}` |
| `low_aircraft`, `aircraft_loiter` | airspace server | `{aircraft} {altitude} {distance} {direction} {minutes}` |

Each feed's first poll after install records what already exists without announcing it, so a
fresh install doesn't produce a burst of stale alerts.

### Camera names

UniFi detection events carry only Protect's internal device id. Map ids to names in
`mqtt.unifi.camera_names`. To find an id, open the camera in the Protect web UI; the URL ends
in `/sidepanel/device/<id>/manage`. Alternatively, run `./jarvis cameras --watch` and walk
past the camera. Names in `camera_names` always win over names UniFi sends and over the
MAC-based learning in `camera_macs`. Cameras without person detection (often older ones on
an NVR) send only motion; list them in `motion_cameras` and use a `types: [motion]` rule.

## 7. Siri and Apple Home

### "Hey Siri, turn on Jarvis …" (the HomeKit accessory)

The daemon advertises a HomeKit accessory (HAP-python). Add it once: Home app → Add Accessory
→ More options → **Jarvis Update** → enter `homekit.pincode`. Its switches each have their
own Siri name:

| Say "Hey Siri, turn on…" | Does |
|---|---|
| Jarvis Update | the regular briefing, right now |
| Jarvis Security | the security sweep: unlocked locks, open doors and gates, internet, cameras |
| Jarvis Energy | Powerpack, solar, house load, battery runway, tomorrow's solar outlook |
| Jarvis Quiet | no scheduled updates or non-urgent alerts until switched off or `quiet_until` |

A briefing switch plays a short acknowledgement, runs its broadcast and turns itself off.
Pairing state is kept in `~/Library/Application Support/jarvis/homekit.state`; deleting it
means re-pairing.

**Battery runway** (energy briefing and 21:00 update): the energy remaining divided by the
last hour's average house load, to 20% and to empty, ignoring further solar.

### JarvisHome: Apple Home → MQTT bridge

`helpers/JarvisHome` is a small Mac Catalyst app (HomeKit is only available to signed apps
with the HomeKit entitlement). It publishes selected characteristics to
`homekit/<home>/<room>/<accessory>/<characteristic>` (retained) and every change to
`homekit/events`. It's deliberately light on HomeKit: it watches only the types Jarvis uses,
reads each value from the device once at startup, then relies on change notifications. The
30-minute refresh uses HomeKit's cache, never device reads. Polling every characteristic
(the first version did) generated thousands of reads an hour and made HomeKit unstable.

```bash
# .env: JARVIS_TEAM_ID (Xcode > Settings > Accounts > your team) and JARVIS_BUNDLE_PREFIX
./install.sh homekit-bridge
```

- **Xcode account:** Xcode must be signed in to that team. The bundle id is
  `$JARVIS_BUNDLE_PREFIX.jarvishome`, and automatic signing creates the Mac Catalyst
  provisioning profile.
- **Keychain:** the installer unlocks the login keychain for `codesign`; over SSH the
  keychain starts locked (`errSecInternalComponent`). If macOS wants an "allow access" click,
  do it via Screen Sharing.
- **Home access:** on first launch, approve *"JarvisHome would like to access your home data"*
  on the Mac's screen, then press Enter in the installer.
- **Launching:** launchd starts the app through `open -W`. A Catalyst app exec'd directly by
  launchd aborts with `_mainSceneIdentifier should have been set`.

## 8. Calendar, Reminders, Mail (JarvisContext)

macOS grants Calendar and Reminders access only to an app bundle that can show a permission
prompt, which the boot-time daemon can't. `helpers/JarvisContext` is a tiny Swift app, run
every 5 minutes in your login session by `./install.sh context`. It writes
`~/Library/Caches/jarvis/context.json` (events through tomorrow, reminders due or overdue,
Mail unread count). Data older than 30 minutes is ignored. Rebuilding changes its ad-hoc
signature, so macOS asks for permission again.

## 9. Make Jarvis speak from anything on MQTT

    mosquitto_pub -t jarvis/say -m "Dinner is ready"
    mosquitto_pub -t jarvis/say -m '{"text": "The gate is open", "homepods": ["Kitchen"], "urgent": true}'
    mosquitto_pub -t jarvis/brief -m energy      # run a briefing now (default: the regular one)

Messages on `jarvis/say` respect quiet mode unless `"urgent": true` is set.

## 10. Services, files, operations

| | |
|---|---|
| `com.jarvis.homepod` | the daemon (`/Library/LaunchDaemons`, or a LaunchAgent with `./install.sh agent`) |
| `com.jarvis.context` | JarvisContext, every 5 min (LaunchAgent) |
| `com.jarvis.homekit` | JarvisHome bridge (LaunchAgent, kept alive) |
| `~/Library/Logs/jarvis/` | `jarvis.log`, `scripts/` (text of every briefing), `alerts.jsonl`, helper logs |
| `~/Library/Caches/jarvis/` | MQTT values and topic catalog, UniFi/energy/airspace/traffic state, `feeds/`, alert audio |

```bash
./install.sh status | restart | uninstall
./jarvis next                      # upcoming broadcasts
./jarvis cameras [--watch]         # cameras seen in detections
tail -f ~/Library/Logs/jarvis/jarvis.log
```

Typical load: the broker carries tens of thousands of messages an hour. Jarvis keeps the
latest values in memory and does real work only when it speaks (about one briefing an hour
plus a handful of alerts a day).

## 11. Privacy

- **Ignored by git:** `.env` (keys) and `config.yaml` (home location, device ids, HomeKit
  setup code). The repo has only `.env.example` and `config.example.yaml`.
- **API keys:** they never appear in logs; services whose responses echo keys are never
  logged.
- **Pairing state:** briefing text, alert history and the HomeKit pairing state stay
  under `~/Library`.

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| HomePod playback fails with `SRPClientSession.__init__() takes 2 positional arguments` | pyatv 0.18 needs `srptools<2` (pinned in requirements) |
| `atvremote` crashes with "no current event loop" on Python 3.14 | use `./jarvis devices`; Jarvis uses pyatv's API directly |
| sudo fails when run from an assistant or `!` command | run `./install.sh daemon` and `restart` in a real terminal |
| Daemon can't find HomePods when run as a LaunchAgent | approve Local Network access for Python, or use `./install.sh daemon` (launchd daemons are exempt) |
| Calendar empty | add the calendar account to this Mac (System Settings → Internet Accounts) |
| JarvisHome crash-loops | it must be launched via `open` (see section 7) |
| JarvisHome build: `No Account for Team` | sign Xcode in to the team in `JARVIS_TEAM_ID` |
| Person alerts never fire for some cameras | they send only motion; see *Camera names* |
| 511 requests skipped | the rate guard is working; see `traffic.max_requests_per_hour` |
| CAL FIRE returns 403 | expected; wildfires come from NIFC WFIGS |
