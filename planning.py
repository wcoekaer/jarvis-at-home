"""Conservative, inspectable energy projections; no LLM arithmetic or device control."""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import math
import statistics
import time


def quantile(values, fraction):
    values = sorted(values)
    pos = (len(values) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


class EnergyPlanner:
    def __init__(self, journal, cfg):
        self.store, self.cfg = journal, cfg

    def forecast(self, now=None, clouds=None):
        now = time.time() if now is None else now
        latest = self.store.get('energy:latest')
        if not latest or not 0 <= now - latest['at'] <= float(self.cfg.get('max_age_minutes', 15)) * 60:
            return {'status': 'unavailable', 'summary': 'Energy forecast unavailable: fresh battery telemetry is required.'}
        rows = self.store.rows('SELECT * FROM energy WHERE at>=? AND at<=? ORDER BY at',
                               (now - float(self.cfg.get('history_days', 21)) * 86400, now))
        days = defaultdict(list)
        for row in rows:
            local = dt.datetime.fromtimestamp(row['at'])
            days[(local.date().isoformat(), local.hour)].append(row)
        profiles = defaultdict(list)
        for (_, hour), samples in days.items():
            if len(samples) >= 4:  # at least 20 minutes represented, not one incidental sample
                profiles[hour].append((statistics.mean(r['load_kw'] for r in samples),
                                       statistics.mean(r['solar_kw'] for r in samples)))
        recent = [r['load_kw'] for r in rows if r['at'] >= now - 3600]
        fallback_load = statistics.median(recent) if recent else latest['load_kw']
        cap = latest['capacity_kwh']
        reserve = cap * float(self.cfg.get('reserve_percent', 20)) / 100
        efficiency = float(self.cfg.get('charge_efficiency', .9))
        typical = low = high = latest['remaining_kwh']
        points, solar_total, load_total = [], 0.0, 0.0
        first_reserve = first_worst = now if typical <= reserve else None
        minimum, learned, weather_hours = typical, 0, 0
        horizon = max(1, min(48, int(self.cfg.get('horizon_hours', 24))))
        cursor, end = now, now + horizon * 3600
        while cursor < end:
            stop = min(end, (int(cursor // 3600) + 1) * 3600)
            duration = (stop - cursor) / 3600
            hour = dt.datetime.fromtimestamp(cursor).hour
            profile = profiles.get(hour, [])
            if len(profile) >= int(self.cfg.get('min_days', 3)):
                loads, solar = [p[0] for p in profile], [p[1] for p in profile]
                demand = statistics.median(loads)
                demand_low, demand_high = min(quantile(loads, .2), demand * .85), max(quantile(loads, .8), demand * 1.15)
                production = statistics.median(solar)
                solar_low, solar_high = min(quantile(solar, .2), production * .5), max(quantile(solar, .8), production * 1.3)
                learned += 1
                cloud = next((p['cloud'] for p in clouds or [] if p['start'] <= cursor < p['end']), None)
                if cloud is not None:
                    # An explicitly labelled heuristic, calibrated to this installation's observed output.
                    production = quantile(solar, .9) * (1 - .75 * (cloud / 100) ** 3)
                    solar_low, solar_high = production * .4, max(solar_high, production * 1.4)
                    weather_hours += 1
            else:
                demand = fallback_load
                demand_low, demand_high = demand * .65, demand * 1.5
                production = solar_low = solar_high = 0  # never invent solar while learning
            def step(energy, supply, use):
                net = supply - use
                return min(cap, max(0, energy + duration * (net * efficiency if net > 0 else net)))
            typical = step(typical, production, demand)
            low = step(low, solar_low, demand_high)
            high = step(high, solar_high, demand_low)
            minimum = min(minimum, typical)
            if typical <= reserve and first_reserve is None:
                first_reserve = stop
            if low <= reserve and first_worst is None:
                first_worst = stop
            solar_total += production * duration
            load_total += demand * duration
            points.append({'at': stop, 'percent': round(typical / cap * 100, 1),
                           'low_percent': round(low / cap * 100, 1), 'high_percent': round(high / cap * 100, 1),
                           'load_kw': round(demand, 2), 'solar_kw': round(production, 2)})
            cursor = stop
        basis = 'weather-adjusted historical profile' if weather_hours else 'historical profile' if learned else 'learning: current load, no solar credited'
        confidence = 'low' if learned < len(points) or not weather_hours else 'moderate'
        target_hour = int(self.cfg.get('morning_hour', 7))
        morning = next((p for p in points if dt.datetime.fromtimestamp(p['at']).hour == target_hour), points[-1])
        target = dt.datetime.fromtimestamp(morning['at']).strftime('%a %-I %p')
        action = ('Defer discretionary loads such as vehicle charging; the typical projection reaches reserve.' if first_reserve else
                  'Consider deferring discretionary loads; the lower scenario reaches reserve.' if first_worst else
                  'The model stays above reserve during this horizon; keep monitoring changing loads and weather.')
        summary = ('Energy projection (%s; %s confidence): by %s, about %.0f%% battery '
                   '(scenario range %.0f–%.0f%%). Next %d hours: %.1f kWh solar and %.1f kWh demand. %s '
                   'These are scenarios, not statistical confidence bounds or a guarantee.' %
                   (basis, confidence, target, morning['percent'], morning['low_percent'], morning['high_percent'],
                    horizon, solar_total, load_total, action))
        return {'status': 'ok', 'at': now, 'basis': basis, 'confidence': confidence, 'summary': summary,
                'reserve_percent': reserve / cap * 100, 'reserve_at': first_reserve, 'lower_reserve_at': first_worst,
                'minimum_percent': minimum / cap * 100, 'solar_kwh': round(solar_total, 2),
                'demand_kwh': round(load_total, 2), 'morning': morning, 'points': points}
