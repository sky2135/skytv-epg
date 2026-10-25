import tempfile
import unittest
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from vod_enrichment_queue import EnrichmentQueue


def record(server='server_1', kind='movie', identifier='1', title='Example'):
    cid = f'{server}:{kind}:{identifier}'
    return {'contentId': cid, 'serverId': server, 'kind': kind, 'languages': ['en'],
            'item': {'id': identifier, 'title': title}, 'identityKey': cid + ':' + title,
            'physicalKey': cid + ':' + title}


class RollingQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'queue.sqlite'
        self.queue = EnrichmentQueue(self.path)

    def tearDown(self):
        self.queue.close()
        self.directory.cleanup()

    def test_rollover_retains_unchanged_retry_and_reopens_changed_or_reintroduced_asset(self):
        one, two = record(), record(identifier='2')
        self.queue.reconcile([one, two], 'policy', {'generation': 'one'}, now=10)
        self.queue.record_result([one['contentId']], {'matchStatus': 'unavailable'}, {'reason': 'unavailable'}, now=20, defer_until=500)
        self.queue.reconcile([one, record(identifier='2', title='Changed')], 'policy', {'generation': 'two'}, now=30)
        self.assertEqual(self.queue.records()[0]['queue']['nextAttemptAt'], 500)
        self.assertEqual(self.queue.records()[0]['queue']['attempts'], 1)
        self.assertEqual([row['item']['title'] for row in self.queue.pending(now=40)], ['Changed'])
        self.queue.reconcile([one], 'policy', {'generation': 'three'}, now=50)
        self.assertEqual(self.queue.report(now=50)['totals']['total'], 1)
        self.queue.reconcile([one, two], 'policy', {'generation': 'four'}, now=60)
        self.assertEqual([row['contentId'] for row in self.queue.pending(now=60)], [two['contentId']])

    def test_changed_policy_invalidates_done_and_fairness_covers_all_six_groups(self):
        rows = [record(server=f'server_{server}', kind=kind, identifier=str(number))
                for server in (1, 2, 3) for kind in ('movie', 'series') for number in (1, 2)]
        self.queue.reconcile(rows, 'one', {}, now=10)
        first = self.queue.pending(6, now=10)
        self.assertEqual(len({(row['serverId'], row['kind']) for row in first}), 6)
        for row in first:
            self.queue.record_result([row['contentId']], {'matchStatus': 'no_match'}, {'reason': 'no_search_results'}, now=20)
        self.assertEqual(len(self.queue.pending(100, now=21)), 6)
        self.queue.reconcile(rows, 'two', {}, now=30)
        self.assertEqual(len(self.queue.pending(100, now=30)), 12)

    def test_transient_failure_stays_retry_after_more_than_three_attempts(self):
        one = record()
        self.queue.reconcile([one], 'policy', {}, now=10)
        for attempt in range(8):
            self.queue.record_result([one['contentId']], {'matchStatus': 'unavailable'}, {'reason': 'unavailable'}, now=10000 * attempt + 20)
        self.assertEqual(self.queue.records()[0]['queue']['state'], 'retry')
        self.assertEqual(self.queue.report(now=70021)['totals']['unresolved'], 0)

    def test_definitive_miss_is_done_for_one_day_not_forever(self):
        one = record()
        self.queue.reconcile([one], 'policy', {}, now=10)
        self.queue.record_result([one['contentId']], {'matchStatus': 'no_match'}, {'reason': 'title_mismatch'}, now=20)
        report = self.queue.report(now=21)
        self.assertTrue(report['complete'])
        self.assertEqual(report['nextAttemptAt'], 86420)
        self.assertEqual(len(self.queue.pending(now=86421)), 1)

    def test_cache_status_cannot_keep_verified_when_current_proof_disappears(self):
        one = record()
        self.queue.reconcile([one], 'policy', {}, now=10)
        self.queue.cache_validated([(one['contentId'], {'matchStatus': 'matched_id', 'tmdbId': '7'})], now=20)
        self.assertEqual(self.queue.report(now=21)['totals']['matched'], 1)
        self.queue.cache_validated([], now=30)
        self.assertEqual(self.queue.report(now=31)['totals']['pending'], 1)

    def test_transaction_rejects_duplicate_inventory_without_losing_prior_work(self):
        one = record()
        self.queue.reconcile([one], 'policy', {}, now=10)
        with self.assertRaises(ValueError):
            self.queue.reconcile([record(title='Changed'), one], 'policy', {}, now=20)
        self.assertEqual(self.queue.records()[0]['item']['title'], 'Example')

    def test_only_complete_matching_identity_can_share_one_result(self):
        one, two = record(), record(identifier='2')
        self.queue.reconcile([one, two], 'policy', {}, now=10)
        with self.assertRaises(ValueError):
            self.queue.record_result([one['contentId'], two['contentId']], {'matchStatus': 'no_match'}, {'reason': 'title_mismatch'}, now=20)

    def test_revalidating_same_proof_preserves_original_refresh_deadline(self):
        one = record()
        self.queue.reconcile([one], 'policy', {}, now=10)
        warmed = [(one['contentId'], {'matchStatus': 'matched_id', 'tmdbId': '7'})]
        self.queue.cache_validated(warmed, now=20, expiry_lookup=lambda _cid: 500)
        self.queue.cache_validated(warmed, now=30, expiry_lookup=lambda _cid: 700)
        self.assertEqual(self.queue.records()[0]['queue']['nextAttemptAt'], 500)


if __name__ == '__main__':
    unittest.main()
