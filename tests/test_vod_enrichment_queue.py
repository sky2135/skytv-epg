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

    def test_priority_cohort_fits_window_while_all_six_groups_keep_their_turn(self):
        rows = [record(server='server_3', identifier=str(number)) for number in range(1, 1001)]
        rows += [record(server=f'server_{server}', kind=kind)
                 for server in (1, 2, 3) for kind in ('movie', 'series')
                 if (server, kind) != (3, 'movie')]
        self.queue.reconcile(rows, 'policy', {}, now=10)
        priorities = [f'server_3:movie:{number}' for number in range(518, 1001)]
        selected = self.queue.pending(488, now=10, priority_content_ids=priorities)
        self.assertEqual(len({(row['serverId'], row['kind']) for row in selected[:6]}), 6)
        self.assertTrue(set(priorities).issubset({row['contentId'] for row in selected}))
        self.assertEqual(self.queue.pending(100, now=10), self.queue.pending(100, now=10, priority_content_ids=[]))

    def test_priority_cannot_force_done_future_retry_or_removed_rows(self):
        done, retry, ordinary, removed = [record(identifier=str(number)) for number in range(1, 5)]
        self.queue.reconcile([done, retry, ordinary, removed], 'policy', {}, now=10)
        self.queue.cache_validated([(done['contentId'], {'matchStatus': 'matched_id', 'tmdbId': '7'})], now=20)
        self.queue.record_result([retry['contentId']], {'matchStatus': 'unavailable'}, {'reason': 'unavailable'}, now=20, defer_until=500)
        self.queue.reconcile([done, retry, ordinary], 'policy', {}, now=25)
        self.queue.set_meta('rateLimiter', {'cooldownUntil': 999})
        before = self.queue.records()
        selected = self.queue.pending(10, now=30, priority_content_ids=[row['contentId'] for row in (done, retry, removed)])
        self.assertEqual([row['contentId'] for row in selected], [ordinary['contentId']])
        self.assertEqual(self.queue.records(), before)
        self.assertEqual(self.queue.get_meta('rateLimiter'), {'cooldownUntil': 999})

    def test_three_thousand_record_window_covers_cohort_with_six_large_due_groups(self):
        rows = [record(server=f'server_{server}', kind=kind, identifier=str(number))
                for server in (1, 2, 3) for kind in ('movie', 'series') for number in range(1, 1001)]
        self.queue.reconcile(rows, 'policy', {}, now=10)
        priorities = [f'server_3:movie:{number}' for number in range(518, 1001)]
        selected = self.queue.pending(3000, now=10, priority_content_ids=priorities)
        self.assertEqual(len(selected), 3000)
        self.assertEqual(len({(row['serverId'], row['kind']) for row in selected[:6]}), 6)
        self.assertTrue(set(priorities).issubset({row['contentId'] for row in selected}))
        self.assertEqual({(server, kind): sum(row['serverId'] == server and row['kind'] == kind for row in selected)
                          for server in ('server_1', 'server_2', 'server_3') for kind in ('movie', 'series')},
                         {(server, kind): 500 for server in ('server_1', 'server_2', 'server_3') for kind in ('movie', 'series')})

    def test_priority_rejects_malformed_duplicate_and_oversized_hints(self):
        bad = [None, 'server_1:movie:1', {}, [1], ['not-an-id'],
               ['server_1:movie:1', 'server_1:movie:1'],
               [f'server_1:movie:{number}' for number in range(501)]]
        for value in bad:
            with self.subTest(value_type=type(value).__name__, length=len(value) if hasattr(value, '__len__') else None):
                with self.assertRaises(ValueError):
                    self.queue.pending(priority_content_ids=value)


if __name__ == '__main__':
    unittest.main()
