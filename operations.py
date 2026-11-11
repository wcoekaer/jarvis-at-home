"""Local operational views and a loopback-only dashboard (standard library)."""
from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shutil
import time
from urllib.parse import urlsplit, parse_qs


def snapshot(store, hours=24):
    health = store.health_rows()
    runtime = store.get('runtime', {})
    conditions = list(store.entries('condition:').values())
    if not any(h['name'] == 'daemon' and h['status'] == 'ok' for h in health):
        for condition in conditions:
            if condition.get('status') != 'disabled':
                condition['status'] = 'unknown'
        runtime['stale'] = True
    forecast = store.get('energy:forecast', {'status': 'unavailable', 'summary': 'Waiting for the daemon and battery telemetry.'})
    if forecast.get('at') and time.time() - forecast['at'] > 900:
        forecast = {'status': 'unavailable', 'summary': 'Energy forecast is stale. Check the daemon and telemetry.'}
    return {'now': time.time(), 'health': health, 'runtime': runtime,
            'energy': forecast,
            'conditions': conditions, 'announcements': store.announcements(time.time() - hours * 3600),
            'events': store.events(time.time() - hours * 3600),
            'commands': store.rows('SELECT id,at,action,status,error FROM commands ORDER BY at DESC LIMIT 10')}


def daemon_running(store):
    return any(h['name'] == 'daemon' and h['status'] == 'ok' for h in store.health_rows())


def enqueue(store, action, data):
    if not isinstance(action, str) or not isinstance(data, dict):
        raise ValueError('action must be a string and data must be an object')
    if not daemon_running(store):
        raise ValueError('Jarvis daemon is not running or its heartbeat is stale')
    if action == 'replay':
        if not store.announcements(aid=data.get('id', '')):
            raise ValueError('unknown announcement')
    elif action in ('ack', 'snooze'):
        if not str(data.get('key', '')).startswith('condition:') or not store.get(data['key']):
            raise ValueError('unknown condition')
        if action == 'snooze' and not 0 < float(data.get('minutes', 60)) <= 10080:
            raise ValueError('snooze must be between 0 and 10080 minutes')
    elif action == 'quiet':
        if not isinstance(data.get('on'), bool):
            raise ValueError('quiet requires an on boolean')
    else:
        raise ValueError('unknown action')
    return store.command(action, data)


def doctor(cfg, store):
    checks = []
    for module in ('yaml', 'openai', 'anthropic', 'pyatv', 'paho.mqtt', 'pyhap', 'aiohttp'):
        try:
            ok = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            ok = False
        checks.append({'name': module, 'status': 'ok' if ok else 'error', 'detail': 'installed' if ok else 'missing dependency'})
    for executable in ('ffmpeg', '/usr/bin/say'):
        checks.append({'name': executable, 'status': 'ok' if shutil.which(executable) else 'error', 'detail': 'executable check'})
    result = store.rows('PRAGMA quick_check')[0]
    checks.append({'name': 'journal', 'status': 'ok' if list(result.values()) == ['ok'] else 'error', 'detail': str(store.path)})
    checks.append({'name': 'daemon', 'status': 'ok' if daemon_running(store) else 'error', 'detail': 'heartbeat within 45 seconds required'})
    checks.extend(h for h in store.health_rows() if h['name'] != 'daemon')
    return checks


def dashboard_server(store, port=8765):
    token = secrets.token_urlsafe(32)
    html = Path(__file__).with_name('dashboard.html').read_text().replace('__TOKEN__', token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # avoid logging personal payloads

        def trusted(self):
            host = self.headers.get('Host', '')
            allowed = {'127.0.0.1:%d' % self.server.server_port, 'localhost:%d' % self.server.server_port}
            return host in allowed

        def reply(self, code, body, content_type='application/json'):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'nonce-%s'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'" % token)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.trusted():
                return self.reply(403, {'error': 'invalid host'})
            url = urlsplit(self.path)
            if url.path == '/':
                return self.reply(200, html, 'text/html')
            if url.path == '/api/status':
                try:
                    hours = max(1, min(2160, float(parse_qs(url.query).get('hours', ['24'])[0])))
                    return self.reply(200, snapshot(store, hours))
                except ValueError:
                    return self.reply(400, {'error': 'invalid hours'})
            self.reply(404, {'error': 'not found'})

        def do_POST(self):
            if not self.trusted() or not secrets.compare_digest(self.headers.get('X-Jarvis-Token', ''), token):
                return self.reply(403, {'error': 'invalid request token'})
            if self.path != '/api/command':
                return self.reply(404, {'error': 'not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('request must be an object')
                cid = enqueue(store, body['action'], body.get('data') or {})
                self.reply(202, {'id': cid, 'status': 'pending'})
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {'error': str(exc)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
