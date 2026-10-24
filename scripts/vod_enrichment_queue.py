"""Rolling, private VOD enrichment work; saved outcomes never confer identity."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from vod_verification_checkpoint import CONTENT_ID, DEFINITIVE, MATCHED


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class EnrichmentQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA journal_mode=DELETE')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.connection.close()
            raise ValueError('Unsupported rolling enrichment queue schema')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items(
                content_id TEXT PRIMARY KEY,server_id TEXT NOT NULL,kind TEXT NOT NULL,
                payload TEXT NOT NULL,source_hash TEXT NOT NULL,identity_key TEXT NOT NULL,
                physical_key TEXT NOT NULL,policy TEXT NOT NULL,active INTEGER NOT NULL,
                state TEXT NOT NULL,first_seen REAL NOT NULL,pending_since REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,last_attempt REAL,next_attempt REAL,
                status TEXT,reason TEXT,metadata TEXT);
            CREATE INDEX IF NOT EXISTS items_due ON items(active,state,next_attempt);
            CREATE INDEX IF NOT EXISTS items_identity ON items(identity_key,active);
            PRAGMA user_version=1;
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise
            else:
                self.connection.execute('COMMIT')

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.transaction():
            self.connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, encoded(value)))

    def reconcile(self, records, policy, source_identity, *, now=None):
        """Atomically refresh inventory; unchanged work retains progress and age."""
        now = time.time() if now is None else now
        seen, added, changed, retained = set(), 0, 0, 0
        with self.transaction():
            for record in records:
                record = dict(record)
                record.pop('queue', None)
                record.pop('result', None)
                cid = record.get('contentId')
                match = CONTENT_ID.fullmatch(cid) if isinstance(cid, str) else None
                if not match or cid in seen:
                    raise ValueError('Rolling inventory has an invalid or duplicate contentId')
                seen.add(cid)
                server, kind, _ = match.groups()
                if record.get('serverId') != server or record.get('kind') != kind:
                    raise ValueError('Rolling inventory identity conflicts')
                payload = encoded(record)
                if len(payload.encode('utf-8')) > 512 * 1024:
                    raise ValueError('Rolling inventory item exceeds its size bound')
                identity = record['identityKey']
                physical = record['physicalKey']
                if not isinstance(identity, str) or not isinstance(physical, str) or not identity or not physical:
                    raise ValueError('Rolling inventory lacks matching identities')
                source_hash = hashlib.sha256(encoded({'item': record['item'], 'kind': kind}).encode()).hexdigest()
                old = self.connection.execute('SELECT * FROM items WHERE content_id=?', (cid,)).fetchone()
                unchanged = old and old['active'] and old['source_hash'] == source_hash and old['identity_key'] == identity and old['policy'] == policy
                if unchanged:
                    if old['payload'] != payload:
                        self.connection.execute('UPDATE items SET payload=? WHERE content_id=?', (payload, cid))
                    retained += 1
                elif old:
                    self.connection.execute('''UPDATE items SET server_id=?,kind=?,payload=?,source_hash=?,
                        identity_key=?,physical_key=?,policy=?,active=1,state='pending',pending_since=?,
                        attempts=0,last_attempt=NULL,next_attempt=NULL,status=NULL,reason=NULL,metadata=NULL
                        WHERE content_id=?''', (server, kind, payload, source_hash, identity, physical, policy, now, cid))
                    changed += 1
                else:
                    self.connection.execute('''INSERT INTO items(content_id,server_id,kind,payload,source_hash,
                        identity_key,physical_key,policy,active,state,first_seen,pending_since)
                        VALUES (?,?,?,?,?,?,?,?,1,'pending',?,?)''',
                        (cid, server, kind, payload, source_hash, identity, physical, policy, now, now))
                    added += 1
            removed = 0
            for row in self.connection.execute('SELECT content_id FROM items WHERE active=1').fetchall():
                if row[0] not in seen:
                    self.connection.execute('UPDATE items SET active=0 WHERE content_id=?', (row[0],))
                    removed += 1
            self.connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('sourceIdentity', encoded(source_identity)))
            self.connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('policyIdentity', encoded(policy)))
        return {'added': added, 'changed': changed, 'retained': retained, 'removed': removed}

    @staticmethod
    def _entry(row):
        payload = json.loads(row['payload'])
        payload['queue'] = {'state': row['state'], 'attempts': row['attempts'], 'nextAttemptAt': row['next_attempt']}
        if row['metadata']:
            payload['result'] = json.loads(row['metadata'])
        return payload

    def records(self):
        with self.lock:
            return [self._entry(row) for row in self.connection.execute('SELECT * FROM items WHERE active=1 ORDER BY content_id')]

    def pending(self, limit=25000, *, now=None):
        if type(limit) is not int or not 1 <= limit <= 1000000:
            raise ValueError('Invalid rolling batch size')
        now = time.time() if now is None else now
        with self.lock:
            rows = self.connection.execute('''SELECT * FROM (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY server_id,kind
                    ORDER BY COALESCE(last_attempt,0),pending_since,content_id) AS group_row
                FROM items WHERE active=1 AND (state='pending' OR next_attempt<=?))
                ORDER BY group_row,server_id,kind LIMIT ?''', (now, limit))
            return [self._entry(row) for row in rows]

    def pending_group(self, identity_key, *, now=None):
        now = time.time() if now is None else now
        with self.lock:
            return [self._entry(row) for row in self.connection.execute('''SELECT * FROM items WHERE active=1
                AND identity_key=? AND (state='pending' OR next_attempt<=?) ORDER BY content_id''', (identity_key, now))]

    def cache_validated(self, warmed, *, now=None, expiry_lookup=None):
        """Only caller-validated current positive cache proof can warm this queue."""
        now = time.time() if now is None else now
        warmed = dict(warmed)
        with self.transaction():
            for row in self.connection.execute('SELECT content_id,status,state,metadata,next_attempt FROM items WHERE active=1').fetchall():
                cid = row['content_id']
                if cid in warmed:
                    metadata = warmed[cid]
                    if metadata.get('matchStatus') not in MATCHED or not metadata.get('tmdbId'):
                        raise ValueError('Cache warming requires validated positive metadata')
                    serialized = encoded(metadata)
                    if (row['state'] == 'done' and row['status'] == metadata['matchStatus']
                            and row['metadata'] == serialized and row['next_attempt'] is not None
                            and row['next_attempt'] > now):
                        continue
                    expires = expiry_lookup(cid) if expiry_lookup else None
                    if not isinstance(expires, (float, int)) or isinstance(expires, bool) or expires <= now:
                        expires = now + 3600
                    self.connection.execute('''UPDATE items SET state='done',status=?,reason='validated_cache',
                        metadata=?,next_attempt=? WHERE content_id=?''',
                        (metadata['matchStatus'], serialized, expires, cid))
                elif row['status'] in MATCHED:
                    self.connection.execute('''UPDATE items SET state='pending',status=NULL,reason='cache_revalidation',
                        metadata=NULL,next_attempt=NULL WHERE content_id=?''', (cid,))

    def record_result(self, ids, metadata, diagnostic, *, now=None, defer_until=None):
        now = time.time() if now is None else now
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('Result requires unique contentIds')
        status = metadata.get('matchStatus', 'unavailable')
        reason = diagnostic.get('reason', status)
        if status not in MATCHED | {'no_match', 'unavailable'}:
            raise ValueError('Invalid rolling metadata status')
        if status in MATCHED and not metadata.get('tmdbId'):
            raise ValueError('Verified result requires an ID')
        with self.transaction():
            rows = [self.connection.execute('SELECT * FROM items WHERE content_id=? AND active=1', (cid,)).fetchone() for cid in ids]
            if any(row is None for row in rows) or len({row['identity_key'] for row in rows}) != 1:
                raise ValueError('Shared results require one active, complete matching identity')
            for row in rows:
                attempts = row['attempts'] + 1
                definitive = status in MATCHED or status == 'no_match' and reason in DEFINITIVE
                if defer_until is not None:
                    state, next_attempt = 'retry', max(now, defer_until)
                elif definitive:
                    state, next_attempt = 'done', now + 86400
                else:
                    state, next_attempt = 'retry', now + min(3600, 30 * 2 ** min(attempts - 1, 7))
                self.connection.execute('''UPDATE items SET state=?,attempts=?,last_attempt=?,next_attempt=?,
                    status=?,reason=?,metadata=? WHERE content_id=?''',
                    (state, attempts, now, next_attempt, status, reason, encoded(metadata), row['content_id']))

    def reopen_poster_collisions(self, client):
        import vod_tmdb as tmdb
        reopened, paths = 0, set(self.get_meta('collidedPosterPaths', []))
        with self.transaction():
            for row in self.connection.execute("SELECT * FROM items WHERE active=1 AND status='matched_poster'").fetchall():
                path = tmdb.poster_identity(json.loads(row['payload'])['item'])
                if client.poster_index.collided(path):
                    paths.add(path)
                    self.connection.execute('''UPDATE items SET state='pending',status=NULL,reason='poster_collision',
                        metadata=NULL,next_attempt=NULL WHERE content_id=?''', (row['content_id'],))
                    reopened += 1
            self.connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('collidedPosterPaths', encoded(sorted(paths))))
        return reopened

    def report(self, *, now=None):
        now = time.time() if now is None else now
        with self.lock:
            totals = Counter(total=0, done=0, pending=0, matched=0, unresolved=0, remaining=0, due=0)
            reasons, groups = Counter(), {}
            next_due = None
            for row in self.connection.execute('SELECT * FROM items WHERE active=1'):
                due = row['state'] == 'pending' or row['next_attempt'] is not None and row['next_attempt'] <= now
                unfinished = row['state'] != 'done' or due
                matched = row['status'] in MATCHED and not unfinished
                unresolved = row['state'] == 'done' and not matched and not unfinished
                values = {'total': 1, 'done': int(not unfinished), 'pending': int(unfinished),
                          'matched': int(matched), 'unresolved': int(unresolved), 'remaining': int(unfinished), 'due': int(due)}
                totals.update(values)
                groups.setdefault(row['server_id'] + ':' + row['kind'], Counter()).update(values)
                if unresolved:
                    reasons[row['reason'] or 'unavailable'] += 1
                if row['next_attempt'] is not None:
                    next_due = row['next_attempt'] if next_due is None else min(next_due, row['next_attempt'])
            return {'totals': dict(totals), 'byGroup': {key: dict(value) for key, value in groups.items()},
                    'unresolvedReasons': dict(reasons), 'complete': totals['remaining'] == 0,
                    'hasUnfinished': totals['remaining'] > 0, 'nextAttemptAt': next_due}

    def close(self):
        self.connection.close()
