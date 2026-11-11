import asyncio
import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import jarvis
import feeds
import operations
from journal import Journal
from conditions import ConditionEngine
from planning import EnergyPlanner


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Journal(Path(self.tmp.name) / 'journal.db')
        self.old = (jarvis.JOURNAL, jarvis.MQTT, jarvis.CONDITIONS, jarvis.PLANNER, jarvis.FEEDS, jarvis.PLAY_LOCK, jarvis.QUIET)
        jarvis.JOURNAL = self.store
        jarvis.MQTT = jarvis.CONDITIONS = jarvis.PLANNER = jarvis.QUIET = None
        jarvis.FEEDS, jarvis.PLAY_LOCK = {}, None
        self.now = dt.datetime(2026, 11, 11, 21).timestamp()

    def tearDown(self):
        (jarvis.JOURNAL, jarvis.MQTT, jarvis.CONDITIONS, jarvis.PLANNER, jarvis.FEEDS, jarvis.PLAY_LOCK, jarvis.QUIET) = self.old
        self.store.close()
        self.tmp.cleanup()

    def rule(self, **kw):
        return dict({'name': 'garage', 'topic': 'home/+/door', 'field': 'text', 'equals': 'open',
                     'for_minutes': 10, 'max_age_minutes': 30, 'clear_minutes': 1, 'repeat_minutes': 60,
                     'message': '{accessory} open for {minutes} minutes', 'resolved_message': '{accessory} closed'}, **kw)

    def test_condition_duration_restart_recovery_and_ack(self):
        engine = ConditionEngine(self.store, [self.rule()])
        engine.observe('home/garage/door', {'text': 'open', 'accessory': 'Garage'}, self.now)
        self.assertEqual(engine.tick(self.now + 599), [])
        engine = ConditionEngine(self.store, [self.rule()])
        events = engine.tick(self.now + 600)
        self.assertEqual(events[0]['text'], 'Garage open for 10 minutes')
        key = events[0]['condition_key']
        engine.control(key, 'ack', now=self.now + 600)
        engine.observe('home/garage/door', {'text': 'open'}, self.now + 3600)
        self.assertEqual(engine.tick(self.now + 4200), [])
        engine.observe('home/garage/door', {'text': 'closed', 'accessory': 'Garage'}, self.now + 4300)
        self.assertEqual(engine.tick(self.now + 4359), [])
        self.assertEqual(engine.tick(self.now + 4360)[0]['kind'], 'condition_resolved')
        self.assertFalse(self.store.get(key)['acknowledged'])
        engine.observe('home/garage/door', {'text': 'open'}, self.now + 4400)
        self.assertEqual(len(engine.tick(self.now + 5000)), 1)

    def test_stale_and_unreachable_do_not_resolve_or_count_duration(self):
        engine = ConditionEngine(self.store, [self.rule()])
        engine.observe('home/garage/door', {'text': 'open'}, self.now)
        self.assertEqual(engine.tick(self.now + 1801), [])
        engine.observe('home/garage/door', {'text': 'open'}, self.now + 1900)
        self.assertEqual(engine.tick(self.now + 2000), [])
        self.assertEqual(len(engine.tick(self.now + 2500)), 1)
        engine.observe('home/garage/door', {'text': 'closed'}, self.now + 2600, reachable=False)
        self.assertEqual(engine.tick(self.now + 2700), [])
        self.assertEqual(next(iter(self.store.entries('condition:').values()))['status'], 'unknown')

    def test_independent_devices_snooze_and_hysteresis(self):
        rule = self.rule()
        engine = ConditionEngine(self.store, [rule])
        for room in ('one', 'two'):
            engine.observe('home/' + room + '/door', {'text': 'open'}, self.now)
        key = 'condition:garage:home/one/door'
        engine.control(key, 'snooze', 15, self.now)
        self.assertEqual(len(engine.tick(self.now + 600)), 1)
        self.assertEqual(len(engine.tick(self.now + 900)), 1)
        numeric = self.rule(name='co2', field='value', above=1200, hysteresis=150, for_minutes=0)
        del numeric['equals']
        engine = ConditionEngine(self.store, [numeric])
        engine.observe('home/office/door', {'value': 1300}, self.now)
        self.assertEqual(len(engine.tick(self.now)), 1)
        engine.observe('home/office/door', {'value': 1150}, self.now + 60)
        self.assertEqual(engine.tick(self.now + 120), [])
        engine.observe('home/office/door', {'value': 1000}, self.now + 130)
        self.assertEqual(engine.tick(self.now + 190)[0]['kind'], 'condition_resolved')

    def test_out_of_order_observation_does_not_close_condition(self):
        engine = ConditionEngine(self.store, [self.rule()])
        engine.observe('home/garage/door', {'text': 'open'}, self.now)
        engine.observe('home/garage/door', {'text': 'closed'}, self.now - 100)
        self.assertEqual(len(engine.tick(self.now + 600)), 1)

    def test_delivery_memory_is_per_speaker_and_success_only(self):
        fact = {'key': 'lock', 'version': 'open'}
        aid = self.store.announcement('briefing', 'hourly', 'Gate open', ['A', 'B'], facts=[fact], now=self.now)
        self.assertFalse(self.store.fact_status(fact, ['A'], self.now - 1))
        self.assertEqual(self.store.finish(aid, {'A': True, 'B': False}, self.now), 'partial')
        self.assertTrue(self.store.fact_status(fact, ['A'], self.now - 1))
        self.assertFalse(self.store.fact_status(fact, ['A', 'B'], self.now - 1))
        self.assertFalse(self.store.fact_status({'key': 'lock', 'version': 'closed'}, ['A'], self.now - 1))
        self.store.recover()
        self.assertEqual(self.store.announcements(aid=aid)[0]['status'], 'partial')

    def test_journal_survives_reopen_and_prunes(self):
        eid = self.store.event({'kind': 'detection', 'camera': 'Garage'}, now=self.now)
        aid = self.store.announcement('alert', 'person', 'Someone is there', ['A'], now=self.now)
        self.store.close()
        self.store = Journal(Path(self.tmp.name) / 'journal.db')
        self.assertEqual(self.store.events(self.now - 1)[0]['id'], eid)
        self.store.recover()
        self.assertEqual(self.store.announcements(aid=aid)[0]['status'], 'interrupted')
        self.store.prune(days=7, now=self.now + 8 * 86400)
        self.assertEqual(self.store.events(0), [])

    def test_energy_learning_staleness_and_reserve(self):
        self.store.add_energy(self.now, 2, 5, 20, 100)
        planner = EnergyPlanner(self.store, {})
        result = planner.forecast(self.now)
        self.assertIn('learning', result['basis'])
        self.assertEqual(result['solar_kwh'], 0)
        self.assertIsNotNone(result['reserve_at'])
        self.assertTrue(all(0 <= p['low_percent'] <= p['percent'] <= p['high_percent'] <= 100 for p in result['points']))
        self.assertEqual(planner.forecast(self.now + 901)['status'], 'unavailable')

    def test_energy_profiles_clouds_and_battery_cap(self):
        for day in range(1, 5):
            start = dt.datetime.fromtimestamp(self.now - day * 86400).replace(hour=0).timestamp()
            for i in range(288):
                at = start + i * 300
                hour = dt.datetime.fromtimestamp(at).hour
                self.store.add_energy(at, 1, 8 if 8 <= hour < 17 else 0, 95, 100)
        self.store.add_energy(self.now, 1, 0, 95, 100)
        planner = EnergyPlanner(self.store, {})
        clear = planner.forecast(self.now, [{'start': self.now, 'end': self.now + 86400, 'cloud': 0}])
        overcast = planner.forecast(self.now, [{'start': self.now, 'end': self.now + 86400, 'cloud': 100}])
        self.assertGreater(clear['solar_kwh'], overcast['solar_kwh'])
        self.assertEqual(clear['confidence'], 'moderate')
        self.assertLessEqual(max(p['percent'] for p in clear['points']), 100)

    def test_mqtt_source_age_and_reachability(self):
        state = jarvis.MqttState({'cache_file': self.tmp.name + '/mqtt.json', 'catalog_file': self.tmp.name + '/topics.json',
             'persist': False, 'discover': False, 'unifi': {'enabled': False}, 'internet': {'enabled': False},
             'energy': {'enabled': False}, 'topics': [{'topic': 'home/lock', 'field': 'text', 'label': 'Lock'}]})
        old = {'text': 'locked', 'ts': dt.datetime.fromtimestamp(self.now - 2 * 86400).isoformat(), 'reachable': False}
        state._on_message(None, None, SimpleNamespace(topic='home/lock', payload=json.dumps(old).encode(), retain=True))
        self.assertNotIn('locked', state.summary(dt.datetime.fromtimestamp(self.now)))
        self.assertIn('UNKNOWN', state.facts(dt.datetime.fromtimestamp(self.now))[0]['text'])
        data, at, _ = state.observation('locked', dt.datetime.now(), retained=True)
        self.assertEqual(at, 0)

    def test_feed_unknown_and_expiry(self):
        feed = object.__new__(feeds.WeatherAlerts)
        feed.state, feed.ok_at, feed.interval = {}, None, 300
        jarvis.FEEDS = {'weather_alerts': feed}
        now = dt.datetime.fromtimestamp(self.now)
        self.assertIn('UNKNOWN', jarvis.feed_lines(['weather_alerts'], now, {'weather_alerts': 'Weather'}))
        feed.ok_at = now
        feed.state = {'active': {'old': {'event': 'Old warning', 'severity': 'Severe', 'ends': (now - dt.timedelta(hours=1)).astimezone().isoformat()}}}
        self.assertNotIn('Old warning', jarvis.feed_lines(['weather_alerts'], now, {'weather_alerts': 'Weather'}))
        self.assertEqual(feeds.build_feeds({}), {})

    def test_structured_output_rejects_invented_ids(self):
        facts = [{'id': 'f1', 'key': 'lock', 'version': 'open'}]
        with patch.object(jarvis, 'ask_openai', return_value='{"speech":"Gate open.","mentioned_fact_ids":["fake"]}'):
            with self.assertRaises(ValueError):
                jarvis.generate_text('prompt', {'llm': {'provider': 'openai'}}, facts)
        with patch.object(jarvis, 'ask_openai', return_value='{"speech":"Gate open.","mentioned_fact_ids":["f1"]}'):
            text, mentioned = jarvis.generate_text('prompt', {'llm': {'provider': 'openai'}}, facts)
            self.assertEqual(text, 'Gate open.')
            self.assertEqual(mentioned, facts)

    def test_partial_playback_and_failure_are_recorded(self):
        aid = self.store.announcement('briefing', 'hourly', 'Hello', ['A', 'B'])
        with patch.object(jarvis, 'find_devices', AsyncMock(return_value={'A': object()})), patch.object(jarvis, 'play_on', AsyncMock(return_value=True)):
            self.assertFalse(asyncio.run(jarvis.deliver(Path('unused.wav'), ['A', 'B'], 50, aid)))
        self.assertEqual(self.store.announcements(aid=aid)[0]['results'], {'A': True, 'B': False})
        self.assertEqual(self.store.announcements(aid=aid)[0]['status'], 'partial')

    def test_dashboard_http_token_host_and_commands(self):
        self.store.health('daemon', 'ok', ttl=45)
        server = operations.dashboard_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % server.server_port
        try:
            html = urlopen(base).read().decode()
            import re
            token = re.search('nonce="([^"]+)"', html)[1]
            self.assertEqual(json.load(urlopen(base + '/api/status'))['health'][0]['name'], 'daemon')
            with self.assertRaises(HTTPError):
                try:
                    urlopen(Request(base, headers={'Host': 'evil.example'}))
                except HTTPError as exc:
                    exc.close()
                    raise
            body = json.dumps({'action': 'quiet', 'data': {'on': True}}).encode()
            with self.assertRaises(HTTPError):
                try:
                    urlopen(Request(base + '/api/command', body))
                except HTTPError as exc:
                    exc.close()
                    raise
            response = json.load(urlopen(Request(base + '/api/command', body, headers={'X-Jarvis-Token': token})))
            self.assertEqual(response['status'], 'pending')
            commands = self.store.pending_commands()
            self.assertEqual(commands[0]['action'], 'quiet')
            self.assertEqual(self.store.pending_commands(), [])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_scheduler_without_mqtt_receives_external_feed_alert(self):
        class FakeFeed:
            def __init__(self):
                self.listeners = []
            async def run(self):
                for listener in self.listeners:
                    listener({'kind': 'weather_alert', 'event': 'Synthetic warning'})
                await asyncio.sleep(100)
        cfg = {'playback': {}, 'tts': {}, 'memory': {}, 'broadcasts': [], 'homepods': ['A'],
               'alerts': [{'name': 'weather', 'kind': 'weather_alert', 'messages': ['Weather alert.']}],
               'context_file': self.tmp.name + '/no-context'}
        jarvis.CONDITIONS = ConditionEngine(self.store, [])
        quiet = SimpleNamespace(active=lambda *a: False)
        async def scenario():
            with patch.object(jarvis, 'QuietMode', return_value=quiet), \
                 patch.object(feeds, 'build_feeds', return_value={'fake': FakeFeed()}), \
                 patch.object(jarvis.Alerter, 'prewarm', AsyncMock()), \
                 patch.object(jarvis.Alerter, '_speak', AsyncMock()) as speak:
                task = asyncio.create_task(jarvis.scheduler(cfg))
                for _ in range(20):
                    if speak.called:
                        break
                    await asyncio.sleep(.005)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(speak.called)
        asyncio.run(scenario())
        self.assertEqual(self.store.events(0)[0]['kind'], 'weather_alert')
        self.assertTrue(operations.daemon_running(self.store))

    def test_synthesis_failure_never_becomes_briefing_memory(self):
        cfg = {'llm': {}, 'tts': {}, 'memory': {}, 'playback': {}, 'archive_dir': self.tmp.name + '/archive'}
        b = {'name': 'hourly', 'homepods': ['A'], 'prompt': 'Hello'}
        with patch.object(jarvis, 'render_prompt', return_value='test'), \
             patch.object(jarvis, 'generate_text', return_value=('Hello', [])), \
             patch.object(jarvis, 'synthesize', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                jarvis.prepare_broadcast(b, cfg, dt.datetime.now(), Path(self.tmp.name))
        self.assertEqual(self.store.announcements()[0]['status'], 'failed')
        self.assertEqual(jarvis.recent_briefings(dt.datetime.now(), cfg), '')
        self.assertFalse(Path(cfg['archive_dir']).exists())

    def test_nws_cloud_intervals(self):
        feed = object.__new__(feeds.SolarWeather)
        feed.state, feed.home = {}, (37, -122)
        start = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        with patch.object(feed, 'get_json', AsyncMock(side_effect=[
            {'properties': {'forecastGridData': 'https://api.weather.gov/gridpoints/TEST/1,1'}},
            {'properties': {'skyCover': {'values': [{'validTime': start.isoformat() + '/PT3H', 'value': 75}]}}}
        ])):
            asyncio.run(feed.poll(None))
        self.assertEqual(feed.state['hours'][0]['end'] - feed.state['hours'][0]['start'], 10800)
        self.assertEqual(feed.state['hours'][0]['cloud'], 75)

    def test_disabled_feeds_and_invalid_intervals(self):
        self.assertEqual(feeds.build_feeds({'feeds': {'enabled': False}}), {})
        with self.assertRaises(ValueError):
            jarvis.expand_times({'every_minutes': -5})


if __name__ == '__main__':
    unittest.main()
