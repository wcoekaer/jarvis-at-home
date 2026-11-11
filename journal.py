"""Local, thread-safe journal shared by the daemon, CLI and loopback dashboard."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def timestamp(value, default=None):
    try:
        if isinstance(value, (float, int)):
            result = float(value)
            if not math.isfinite(result):
                return default
            dt.datetime.fromtimestamp(result)  # reject values outside datetime's range
            return result
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return default


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


class Journal:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(at);
            CREATE TABLE IF NOT EXISTS announcements (
                id TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT, name TEXT, text TEXT,
                status TEXT, event_id TEXT, targets TEXT, results TEXT, facts TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS announcements_time ON announcements(at);
            CREATE TABLE IF NOT EXISTS mentioned (
                key TEXT, version TEXT, speaker TEXT, at REAL, PRIMARY KEY(key, version, speaker));
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS energy (
                bucket INTEGER PRIMARY KEY, at REAL, load_kw REAL, solar_kw REAL,
                remaining_kwh REAL, capacity_kwh REAL);
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, at REAL, action TEXT, data TEXT, status TEXT, error TEXT);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.lock, self.db:
            self.db.execute(sql, params)

    def get(self, key, default=None):
        rows = self.rows('SELECT data FROM kv WHERE key=?', (key,))
        return json.loads(rows[0]['data']) if rows else default

    def put(self, key, data):
        self.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(data)))

    def entries(self, prefix):
        return {r['key']: json.loads(r['data']) for r in self.rows(
            'SELECT * FROM kv WHERE substr(key,1,?)=?', (len(prefix), prefix))}

    def health(self, name, status, detail='', ttl=300, now=None):
        now = time.time() if now is None else now
        self.put('health:' + name, {'name': name, 'status': status, 'detail': detail,
                                  'at': now, 'ttl': ttl})

    def health_rows(self, now=None):
        now = time.time() if now is None else now
        out = []
        for row in self.entries('health:').values():
            row['age_seconds'] = max(0, now - row['at'])
            if row['age_seconds'] > row['ttl']:
                row['status'] = 'stale'
            out.append(row)
        return sorted(out, key=lambda r: r['name'])

    def event(self, event, now=None):
        eid = event.get('_event_id') or uuid.uuid4().hex
        data = {k: v for k, v in event.items() if not k.startswith('_')}
        self.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                     (eid, time.time() if now is None else now, event['kind'], json.dumps(data)))
        return eid

    def events(self, since, limit=200):
        rows = self.rows('SELECT * FROM events WHERE at>=? ORDER BY at DESC LIMIT ?', (since, limit))
        for row in rows:
            row['data'] = json.loads(row['data'])
        return rows

    def announcement(self, kind, name, text, targets, event_id=None, facts=None, now=None):
        aid = uuid.uuid4().hex
        self.execute('INSERT INTO announcements VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (aid, time.time() if now is None else now, kind, name, text, 'generated',
                      event_id, json.dumps(targets), '{}', json.dumps(facts or []), None))
        return aid

    def progress(self, aid, status, error=None):
        self.execute('UPDATE announcements SET status=?, error=? WHERE id=?', (status, error, aid))

    def finish(self, aid, results, now=None):
        now = time.time() if now is None else now
        row = self.announcements(aid=aid)[0]
        success = [speaker for speaker, ok in results.items() if ok]
        status = 'delivered' if success and len(success) == len(row['targets']) else 'partial' if success else 'failed'
        with self.lock, self.db:
            self.db.execute('UPDATE announcements SET status=?, results=? WHERE id=?',
                            (status, json.dumps(results), aid))
            for fact in row['facts']:
                for speaker in success:
                    self.db.execute('INSERT OR REPLACE INTO mentioned VALUES (?,?,?,?)',
                                    (fact['key'], fact['version'], speaker, now))
        return status

    def announcements(self, since=0, limit=100, aid=None):
        rows = self.rows('SELECT * FROM announcements WHERE id=?', (aid,)) if aid else self.rows(
            'SELECT * FROM announcements WHERE at>=? ORDER BY at DESC LIMIT ?', (since, limit))
        for row in rows:
            for key in ('targets', 'results', 'facts'):
                row[key] = json.loads(row[key])
        return rows

    def fact_status(self, fact, speakers, since):
        delivered = {r['speaker'] for r in self.rows(
            'SELECT speaker FROM mentioned WHERE key=? AND version=? AND at>=?',
            (fact['key'], fact['version'], since))}
        return bool(speakers) and set(speakers).issubset(delivered)

    def add_energy(self, at, load, solar, remaining, capacity):
        # One observation per five-minute bucket bounds disk writes on busy MQTT feeds.
        self.execute('INSERT OR IGNORE INTO energy VALUES (?,?,?,?,?,?)',
                     (int(at // 300), at, load, solar, remaining, capacity))
        self.put('energy:latest', {'at': at, 'load_kw': load, 'solar_kw': solar,
                                 'remaining_kwh': remaining, 'capacity_kwh': capacity})

    def command(self, action, data=None):
        if action not in ('replay', 'quiet', 'ack', 'snooze'):
            raise ValueError('unsupported command')
        cid = uuid.uuid4().hex
        self.execute('INSERT INTO commands VALUES (?,?,?,?,?,?)',
                     (cid, time.time(), action, json.dumps(data or {}), 'pending', None))
        return cid

    def pending_commands(self):
        with self.lock, self.db:
            rows = self.rows("SELECT * FROM commands WHERE status='pending' ORDER BY at")
            self.db.execute("UPDATE commands SET status='processing' WHERE status='pending'")
        for row in rows:
            row['data'] = json.loads(row['data'])
        return rows

    def command_done(self, cid, error=None):
        self.execute('UPDATE commands SET status=?, error=? WHERE id=?',
                     ('failed' if error else 'done', error, cid))

    def recover(self):
        self.execute("UPDATE announcements SET status='interrupted' WHERE status IN ('generated','queued','playing')")
        self.execute("UPDATE commands SET status='failed', error='daemon restarted' WHERE status='processing'")

    def prune(self, days=90, now=None):
        cutoff = (time.time() if now is None else now) - days * 86400
        with self.lock, self.db:
            for table in ('events', 'announcements', 'mentioned', 'energy', 'commands'):
                self.db.execute('DELETE FROM ' + table + ' WHERE at<?', (cutoff,))


def open_journal(cfg):
    return Journal((cfg.get('memory') or {}).get('database',
                   '~/Library/Application Support/jarvis/journal.sqlite3'))
