import hashlib
import json
import tempfile
import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import enrich_vod_catalog as worker
from vod_enrichment_queue import EnrichmentQueue
from vod_verification_rate_limit import VerificationDeferred
from vod_verification_rate_limit import PacedSession


class PosterIndex:
    def collided(self, _path):
        return False

    def add(self, *args):
        pass


class Session:
    def __init__(self, limiter):
        self.limiter = limiter
        self.last_deferred = None

    def lookup(self):
        try:
            self.limiter.acquire()
        except VerificationDeferred as error:
            self.last_deferred = error
            raise
        self.limiter.record_attempt('movie_details')
        self.limiter.record_response(200)

    def close(self):
        pass


class Client:
    """A private persistent proof fixture, never a public baseline identity."""
    def __init__(self, cache_dir, *, session=None, **kwargs):
        self.cache_dir, self.session = Path(cache_dir) / 'tmdb-v1', session
        self.poster_index = PosterIndex()
        self.last_match_diagnostic = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def reset_elapsed_budget(self, _seconds):
        pass

    def _path(self, key):
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def _identity_cache(self, key, item, kind, identifier, title, year):
        path = self._path(key)
        if path.is_file():
            return json.loads(path.read_text())['metadata'], True, False
        return None, False, False

    def enrich(self, item, kind):
        key, identifier, title, year = worker.tmdb._enrichment_identity(item, kind)
        cached, _fresh, _rejected = self._identity_cache(key, item, kind, identifier, title, year)
        if cached:
            self.last_match_diagnostic = {'reason': 'matched_id'}
            return cached
        self.session.lookup()
        metadata = {'tmdbId': identifier, 'matchStatus': 'matched_id', 'releaseDate': year + '-01-01',
                    'originalTitle': title, 'originalLanguage': 'en', 'rejectedIdentity': False,
                    'genres': ['Adventure'], 'posterUrl': None}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps({'identityPolicy': worker.tmdb.IDENTITY_POLICY, 'metadata': metadata}))
        self.last_match_diagnostic = {'reason': 'matched_id'}
        return metadata


def document(count=6):
    records = []
    for position in range(count):
        server = f'server_{position % 3 + 1}'
        kind = 'movie' if position < 3 else 'series'
        cid = f'{server}:{kind}:{position + 1}'
        records.append({'contentId': cid, 'serverId': server, 'kind': kind, 'languages': ['en'],
            'baseline': {'metadataMatch': 'matched_id', 'tmdbId': '999'},
            'item': {'id': str(position + 1), 'contentId': cid, 'title': f'Example {position + 1} (2026)',
                     'tmdbId': str(position + 1), 'languages': ['en'], 'releaseDate': None,
                     'artwork': {'posterUrl': None, 'backdropUrl': None}, 'providerLastModified': None}})
    return {'records': records, 'inputFingerprint': 'input-one', 'generatedAt': '2026-10-24T00:00:00Z'}


class RollingWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state, self.component = self.root / 'state', self.root / 'component'

    def tearDown(self):
        self.directory.cleanup()

    def run_batch(self, source, **kwargs):
        with patch.object(worker, 'load_component_input', return_value=source):
            return worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                client_factory=Client, session_factory=Session, rate=25, batch_seconds=5, **kwargs)

    def test_sealed_private_cache_survives_restart_and_public_baseline_is_not_proof(self):
        first = self.run_batch(document(), batch_records=2)
        self.assertEqual(first['totals']['done'], 2)
        self.assertEqual(first['api']['wireAttempts'], 2)
        second = self.run_batch(document(), batch_records=10)
        self.assertTrue(second['complete'])
        self.assertEqual(second['batchNumber'], 2)
        self.assertEqual(second['api']['wireAttempts'], 6)
        third = self.run_batch(document(), batch_records=10, priority_content_ids=[row['contentId'] for row in document()['records']])
        self.assertEqual(third['api']['wireAttempts'], 6)
        self.assertEqual(third['priority']['selected'], 0)
        self.assertFalse(third['productionCatalogWritten'])
        envelopes = list((self.state / 'cache/tmdb/tmdb-v1').glob('*.json'))
        self.assertEqual(len(envelopes), 6)
        self.assertEqual(json.loads(envelopes[0].read_text())['metadata']['genres'], ['Adventure'])
        self.assertNotIn('secret-token', (self.state / 'report.json').read_text())

    def test_new_generation_changes_only_one_identity_without_restarting_everything(self):
        self.run_batch(document(), batch_records=10)
        source = document()
        source['records'][0]['item']['title'] = 'Different Work (2026)'
        source['inputFingerprint'] = 'input-two'
        report = self.run_batch(source, batch_records=10)
        self.assertEqual(report['reconciliation']['changed'], 1)
        self.assertEqual(report['reconciliation']['retained'], 5)
        self.assertEqual(report['api']['wireAttempts'], 7)

    def test_durable_cooldown_defers_every_wire_attempt_on_restart(self):
        self.run_batch(document(1), batch_records=1)
        queue = EnrichmentQueue(self.state / 'queue.sqlite')
        state = queue.get_meta('rateLimiter')
        state['cooldownUntil'] = worker.time.time() + 3600
        queue.set_meta('rateLimiter', state)
        queue.close()
        report = self.run_batch(document(2), batch_records=2, priority_content_ids=[document(2)['records'][1]['contentId']])
        self.assertEqual(report['api']['wireAttempts'], 1)
        self.assertTrue(report['shouldContinue'])
        self.assertGreater(report['nextAttemptAt'], worker.time.time() + 3500)

    def test_priority_changes_selection_not_identity_and_missing_ids_create_no_rows(self):
        source = document()
        target = dict(source['records'][2])
        target['contentId'] = 'server_3:movie:9999'
        target['item'] = dict(target['item'], contentId=target['contentId'], id='9999',
                              title='Requested Work (2026)', tmdbId='9999')
        source['records'].append(target)
        report = self.run_batch(source, batch_records=6,
            priority_content_ids=[target['contentId'], 'server_3:movie:99999999'])
        self.assertEqual(report['priority'], {'requested': 2, 'active': 1, 'missing': 1, 'selected': 1})
        self.assertEqual(report['totals']['total'], 7)
        self.assertEqual(report['api']['wireAttempts'], 6)
        queue = EnrichmentQueue(self.state / 'queue.sqlite')
        try:
            rows = {row['contentId']: row for row in queue.records()}
            self.assertEqual(rows[target['contentId']]['queue']['state'], 'done')
            self.assertEqual(rows[source['records'][2]['contentId']]['queue']['state'], 'pending')
            self.assertEqual(len({(row['serverId'], row['kind']) for row in rows.values() if row['queue']['state'] == 'done'}), 6)
        finally:
            queue.close()

    def test_priority_json_validation_precedes_state_or_network_work(self):
        import argparse
        for value in ('null', '{}', '"server_1:movie:1"', '["bad"]',
                      '["server_1:movie:1","server_1:movie:1"]',
                      json.dumps([f'server_1:movie:{number}' for number in range(501)])):
            with self.subTest(value=value[:30]):
                with self.assertRaises(argparse.ArgumentTypeError):
                    worker._priority_json(value)
        self.assertEqual(worker._priority_json('["server_3:movie:954024"]'), ('server_3:movie:954024',))
        with patch.object(worker, 'load_component_input') as loader:
            with self.assertRaises(ValueError):
                self.run_batch(document(), priority_content_ids=['bad'])
            loader.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_policy_change_requires_new_current_component_on_continuation(self):
        self.run_batch(document(1), batch_records=1)
        with patch.object(worker, 'policy_identity', return_value='changed'):
            with self.assertRaises(ValueError):
                worker.run_batch(state_dir=self.state, token='secret-token', client_factory=Client,
                                 session_factory=Session, batch_seconds=5)

    def test_failure_report_has_safe_frames_without_exception_message(self):
        class BrokenClient(Client):
            def enrich(self, item, kind):
                raise RuntimeError('secret-token https://private.invalid/?password=hidden')
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            report = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                client_factory=BrokenClient, session_factory=Session, batch_seconds=5)
        self.assertEqual(report['fatalError'], 'enrichment_exception_RuntimeError')
        self.assertFalse(report['shouldContinue'])
        self.assertEqual(report['totals']['pending'], 1)
        serialized = json.dumps(report)
        self.assertNotIn('private.invalid', serialized)
        self.assertNotIn('password=hidden', serialized)
        self.assertTrue(any(row['file'] == 'enrich_vod_catalog.py' for row in report['fatalFrames']))

    def test_authentication_failure_in_last_active_request_never_auto_continues(self):
        class UnauthorizedClient(Client):
            def enrich(self, item, kind):
                self.session.limiter.acquire()
                self.session.limiter.record_attempt('movie_details')
                self.session.limiter.record_response(401)
                self.last_match_diagnostic = {'reason': 'unavailable'}
                return {'matchStatus': 'unavailable'}
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            report = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                client_factory=UnauthorizedClient, session_factory=Session, batch_seconds=5)
        self.assertEqual(report['fatalError'], 'tmdb_authentication_rejected')
        self.assertFalse(report['shouldContinue'])
        self.assertEqual(report['api']['statusCounts']['401'], 1)

    def test_provider_detail_budget_is_shared_across_batches_of_one_source(self):
        class MissClient:
            def _identity_cache(self, *args):
                return {'matchStatus': 'no_match'}, True, False

            def ignored_without_artwork(self, *args):
                return False

        class Collector:
            def __init__(self, *args, **kwargs):
                self.stats = {'requests': 0}

            def collect(self, server, item):
                self.stats['requests'] += 1
                return {'contentId': item['contentId'], 'fingerprint': 'private-facts'}

            def close(self):
                pass

        rows = []
        year = str(worker.datetime.now(worker.timezone.utc).year)
        for identifier in range(1, 6):
            item = dict(document(1)['records'][0]['item'], id=str(identifier),
                        contentId=f'server_1:movie:{identifier}', title=f'Candidate {identifier} ({year})')
            rows.append({'contentId': item['contentId'], 'serverId': 'server_1', 'kind': 'movie', 'item': item})
        queue = EnrichmentQueue(self.state / 'queue.sqlite')
        try:
            config = SimpleNamespace(base_url='https://provider.invalid', username='hidden', password='hidden')
            with patch.object(worker.vod, 'load_server_config', return_value=config):
                first = worker._provider_evidence(rows, MissClient(), queue, self.state / 'cache',
                    {'inputFingerprint': 'one'}, ['server_1'], 2, False, collector_factory=Collector)
                second = worker._provider_evidence(rows, MissClient(), queue, self.state / 'cache',
                    {'inputFingerprint': 'one'}, ['server_1'], 2, False, collector_factory=Collector)
            self.assertEqual(first['requests'], 2)
            self.assertEqual(second['requests'], 0)
            self.assertEqual(queue.get_meta('providerEvidenceBudget')['used'], 2)
        finally:
            queue.close()

    def test_real_trending_cache_is_paced_bounded_and_reused_until_actual_expiry(self):
        class Response:
            status_code = 200
            headers = {}

            def __init__(self, identifier):
                self.identifier = identifier

            def iter_content(self, chunk_size):
                yield json.dumps({'results': [{'id': self.identifier}], 'total_pages': 7}).encode()

            def close(self):
                pass

        class Transport:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs['params']['page']))
                return Response(len(self.calls))

        transport = Transport()
        factory = lambda limiter: PacedSession(limiter, transport)
        with patch.object(worker, 'load_component_input', return_value=document(0)):
            first = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, refresh_trending=True, rate=25, batch_seconds=5)
            second = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, refresh_trending=True, rate=25, batch_seconds=5)
        self.assertEqual(len(transport.calls), 10)
        self.assertEqual(first['api']['wireAttempts'], 10)
        self.assertEqual(second['api']['wireAttempts'], 10)
        self.assertTrue(second['complete'])
        self.assertFalse(second['shouldContinue'])
        self.assertEqual(second['trending']['due'], 0)
        self.assertEqual(second['nextAttemptAt'], first['nextAttemptAt'])
        self.assertGreater(first['nextAttemptAt'], worker.time.time() + 86000)
        for kind in ('movie', 'series'):
            self.assertEqual(first['trending']['byKind'][kind]['count'], 5)
            key = hashlib.sha256(f'{kind}:trending:week'.encode()).hexdigest()
            path = self.state / 'cache/tmdb/tmdb-v1' / (key + '.json')
            value = json.loads(path.read_text())
            self.assertEqual(first['trending']['byKind'][kind]['nextAttemptAt'], value['expiresAt'])
            value['expiresAt'] = worker.time.time() - 1
            path.write_text(json.dumps(value))
        with patch.object(worker, 'load_component_input', return_value=document(0)):
            refreshed = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, refresh_trending=True, rate=25, batch_seconds=5)
        self.assertEqual(refreshed['api']['wireAttempts'], 20)
        self.assertEqual(len(transport.calls), 20)

    def test_trending_429_persists_full_cooldown_without_freshening_partial_results(self):
        class Response:
            status_code = 429
            headers = {'Retry-After': '3600'}

            def close(self):
                pass

        class Transport:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                return Response()

        transport = Transport()
        factory = lambda limiter: PacedSession(limiter, transport)
        with patch.object(worker, 'load_component_input', return_value=document(0)):
            first = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, refresh_trending=True, rate=25, batch_seconds=5)
            second = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, refresh_trending=True, rate=25, batch_seconds=5)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(first['api']['wireAttempts'], 1)
        self.assertEqual(first['api']['rateLimitResponses'], 1)
        self.assertEqual(second['api']['wireAttempts'], 1)
        self.assertTrue(second['shouldContinue'])
        self.assertFalse(second['complete'])
        self.assertEqual(second['trending']['due'], 0)
        self.assertGreater(second['nextAttemptAt'], worker.time.time() + 3500)
        self.assertEqual({row['status'] for row in second['trending']['byKind'].values()}, {'retry'})

    def test_real_transient_cache_retries_before_miss_ttl_and_preserves_definitive_rejection(self):
        class Response:
            headers = {}

            def __init__(self, code, payload=None):
                self.status_code, self.payload = code, payload

            def iter_content(self, chunk_size):
                yield json.dumps(self.payload).encode()

            def close(self):
                pass

        class Transport:
            def __init__(self, replies):
                self.replies, self.calls = list(replies), 0

            def get(self, *args, **kwargs):
                self.calls += 1
                if not self.replies:
                    raise AssertionError('Unexpected wire lookup of a definitive cache')
                return self.replies.pop(0)

        details = {'id': 1, 'title': 'Example 1', 'original_title': 'Example 1',
                   'original_language': 'en', 'release_date': '2026-01-01',
                   'genres': [{'id': 18, 'name': 'Drama'}], 'adult': False}
        transport = Transport([Response(503), Response(503), Response(503), Response(200, details)])
        factory = lambda limiter: PacedSession(limiter, transport)
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            first = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, rate=25, batch_seconds=5)
        self.assertEqual(first['api']['wireAttempts'], 3)
        self.assertEqual(first['totals']['pending'], 1)
        item = document(1)['records'][0]['item']
        key, identifier, title, year = worker.tmdb._enrichment_identity(item, 'movie')
        with worker.tmdb.TmdbClient(self.state / 'cache/tmdb', token='secret-token', max_requests=0) as ordinary:
            cached, fresh, _ = ordinary._identity_cache(key, item, 'movie', identifier, title, year)
        self.assertTrue(fresh)
        self.assertEqual(cached['matchStatus'], 'unavailable')
        queue = EnrichmentQueue(self.state / 'queue.sqlite')
        queue.connection.execute('UPDATE items SET next_attempt=?', (worker.time.time() - 1,))
        queue.close()
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            second = worker.run_batch(state_dir=self.state, component_dir=self.component, token='secret-token',
                session_factory=factory, rate=25, batch_seconds=5)
        self.assertEqual(second['api']['wireAttempts'], 4)
        self.assertEqual(second['totals']['matched'], 1)
        self.assertTrue(second['complete'])
        # A genuine negative proof remains reusable even if the scheduling row
        # is due early; bypassing unavailable must not bypass all negatives.
        other_state = self.root / 'definitive'
        rejection = Transport([Response(404)])
        rejection_factory = lambda limiter: PacedSession(limiter, rejection)
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            rejected = worker.run_batch(state_dir=other_state, component_dir=self.component, token='secret-token',
                session_factory=rejection_factory, rate=25, batch_seconds=5)
        self.assertEqual(rejected['totals']['unresolved'], 1)
        queue = EnrichmentQueue(other_state / 'queue.sqlite')
        queue.connection.execute('UPDATE items SET next_attempt=?', (worker.time.time() - 1,))
        queue.close()
        with patch.object(worker, 'load_component_input', return_value=document(1)):
            retained = worker.run_batch(state_dir=other_state, component_dir=self.component, token='secret-token',
                session_factory=rejection_factory, rate=25, batch_seconds=5)
        self.assertEqual(retained['api']['wireAttempts'], 1)
        self.assertEqual(rejection.calls, 1)


if __name__ == '__main__':
    unittest.main()
