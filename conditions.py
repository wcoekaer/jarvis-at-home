"""Duration rules over observed state. Unknown/stale data never implies recovery."""
from __future__ import annotations

import datetime as dt
import math
import time


def matches_topic(pattern, topic):
    a, b = pattern.split('/'), topic.split('/')
    for i, part in enumerate(a):
        if part == '#':
            return True
        if i >= len(b) or part not in ('+', b[i]):
            return False
    return len(a) == len(b)


def field_value(data, path):
    for part in (path or '').split('.') if path else []:
        data = data.get(part) if isinstance(data, dict) else None
    return data


def validate_rules(rules):
    names = set()
    for rule in rules:
        if not rule.get('name') or rule['name'] in names or not rule.get('topic'):
            raise ValueError('condition rules need a unique name and topic')
        names.add(rule['name'])
        if sum(k in rule for k in ('equals', 'above', 'below')) != 1:
            raise ValueError('condition %s needs exactly one of equals/above/below' % rule['name'])
        for key in ('for_minutes', 'clear_minutes', 'repeat_minutes', 'max_age_minutes', 'hysteresis'):
            value = float(rule.get(key, 0))
            if not math.isfinite(value) or value < 0:
                raise ValueError('condition %s: invalid %s' % (rule['name'], key))
        if not rule.get('message'):
            raise ValueError('condition %s needs a message' % rule['name'])


class ConditionEngine:
    def __init__(self, journal, rules):
        validate_rules(rules)
        self.store = journal
        self.rules = {r['name']: r for r in rules if r.get('enabled', True)}

    def observe(self, topic, data, observed_at, reachable=True):
        source_reachable = reachable
        for name, rule in self.rules.items():
            reachable = source_reachable
            if not matches_topic(rule['topic'], topic):
                continue
            key = 'condition:' + name + ':' + topic
            state = self.store.get(key, {'key': key, 'name': name, 'topic': topic,
                                        'since': None, 'clear_since': None, 'alerted': False,
                                        'last_alert': None, 'active': False, 'acknowledged': False})
            if observed_at < state.get('observed_at', 0):
                continue
            ttl = float(rule.get('max_age_minutes', 45)) * 60
            if observed_at - state.get('observed_at', observed_at) > ttl:
                state['since'] = state['clear_since'] = None
            value = field_value(data, rule.get('field'))
            try:
                if 'equals' in rule:
                    wanted = rule['equals']
                    active = value in wanted if isinstance(wanted, list) else value == wanted
                else:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError('non-finite value')
                    h = float(rule.get('hysteresis', 0)) if state['active'] else 0
                    active = number > float(rule['above']) - h if 'above' in rule else number < float(rule['below']) + h
            except (ValueError, TypeError):
                reachable, active = False, state['active']
            if value is None:
                reachable = False
            state.update(observed_at=observed_at, reachable=reachable, value=value,
                         context=data if isinstance(data, dict) else {})
            if reachable:
                if active:
                    state['since'] = state['since'] if state['since'] is not None else observed_at
                    state['clear_since'] = None
                else:
                    state['clear_since'] = state['clear_since'] if state['clear_since'] is not None else observed_at
                    state['since'] = None
                state['active'] = active
            self.store.put(key, state)

    def tick(self, now=None):
        now = time.time() if now is None else now
        events = []
        for key, state in self.store.entries('condition:').items():
            rule = self.rules.get(state['name'])
            if not rule:
                if state.get('status') != 'disabled':
                    state['status'] = 'disabled'
                    self.store.put(key, state)
                continue
            stale = not state.get('reachable') or not 0 <= now - state['observed_at'] <= float(rule.get('max_age_minutes', 45)) * 60
            if stale:
                state['since'] = state['clear_since'] = None
                state['status'] = 'unknown'
            elif state['active']:
                state['status'] = 'active'
                hours = rule.get('hours')
                clock = dt.datetime.fromtimestamp(now).strftime('%H:%M')
                in_hours = not hours or (hours['from'] <= clock <= hours['to'] if hours['from'] <= hours['to']
                                         else clock >= hours['from'] or clock <= hours['to'])
                due = state['since'] is not None and now - state['since'] >= float(rule.get('for_minutes', 15)) * 60
                repeat = float(rule.get('repeat_minutes', 60)) * 60
                ready = state['last_alert'] is None or (repeat > 0 and now - state['last_alert'] >= repeat)
                if due and ready and in_hours and not state['acknowledged'] and now >= state.get('snoozed_until', 0):
                    state['alerted'], state['last_alert'] = True, now
                    events.append(self.event(rule, state, now, False))
            else:
                state['status'] = 'clearing' if state['alerted'] else 'clear'
                if state['clear_since'] is not None and now - state['clear_since'] >= float(rule.get('clear_minutes', 1)) * 60:
                    if state['alerted'] and rule.get('resolved_message'):
                        events.append(self.event(rule, state, now, True))
                    state.update(status='clear', alerted=False, last_alert=None, acknowledged=False, snoozed_until=0)
            self.store.put(key, state)
        return events

    def event(self, rule, state, now, resolved):
        values = dict(state.get('context') or {}, value=state.get('value'),
                      minutes=int((now - (state['since'] or now)) / 60), topic=state['topic'])
        text = rule['resolved_message' if resolved else 'message']
        for k, v in values.items():
            text = text.replace('{' + k + '}', str(v))
        return {'kind': 'condition_resolved' if resolved else 'condition', 'condition_key': state['key'],
                'rule': rule['name'], 'text': text, 'urgent': bool(rule.get('urgent')), 'homepods': rule.get('homepods'),
                'volume': rule.get('volume')}

    def control(self, key, action, minutes=60, now=None):
        now = time.time() if now is None else now
        state = self.store.get(key)
        if not key.startswith('condition:') or not state or state['name'] not in self.rules:
            raise ValueError('unknown condition key; use jarvis status')
        if action == 'ack':
            state['acknowledged'] = True
        elif action == 'snooze' and 0 < minutes <= 10080:
            state['snoozed_until'] = now + minutes * 60
        else:
            raise ValueError('invalid condition control')
        self.store.put(key, state)
