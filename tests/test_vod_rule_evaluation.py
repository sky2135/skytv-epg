from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import vod_rule_evaluation as evaluation


def provider_record(number=1, *, kind='movie', server='server_1'):
    cid = f'{server}:{kind}:{number}'
    return {'contentId': cid, 'serverId': server, 'kind': kind,
            'identityKey': hashlib.sha256(cid.encode()).hexdigest(),
            'item': {'contentId': cid, 'title': 'Provider title (2024)',
                     'tmdbId': None, 'releaseDate': None,
                     'artwork': {'posterUrl': 'https://image.tmdb.org/t/p/w500/source.jpg'},
                     'languages': ['en']}}


def source_hash(record):
    return hashlib.sha256(json.dumps({'item': record['item'], 'kind': record['kind']},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class RuleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root / 'baseline'
        self.state = self.root / 'test-state'
        self.control = {'sourceArtifactId': 42, 'isolated': False}
        self.report = {'sourceIdentity': {'inputFingerprint': 'a' * 64, 'recordCount': 3},
                       'totals': {'total': 3, 'matched': 1, 'unresolved': 1, 'remaining': 1}}
        self.rows = [provider_record(1), provider_record(2, server='server_2'),
                     provider_record(3, kind='series', server='server_3')]
        self.old_metadata = {'tmdbId': '987654', 'matchStatus': 'matched_search',
                             'releaseDate': '1990-01-01', 'originalTitle': 'Old inferred title'}

    def make_database(self):
        self.baseline.mkdir()
        with closing(sqlite3.connect(self.baseline / 'queue.sqlite')) as connection:
            connection.execute('CREATE TABLE items(content_id TEXT, server_id TEXT, kind TEXT, '
                'payload TEXT, source_hash TEXT, active INTEGER, state TEXT, status TEXT, '
                'reason TEXT, metadata TEXT)')
            for row, state, status, reason in zip(self.rows, ('done', 'done', 'retry'),
                    ('matched_search', 'no_match', None), ('matched_search', 'title_mismatch', 'incomplete_search')):
                connection.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['contentId'], row['serverId'], row['kind'], json.dumps(row), source_hash(row),
                     1, state, status, reason, json.dumps(self.old_metadata) if status == 'matched_search' else None))
            connection.commit()

    def identity(self):
        return mock.patch.object(evaluation.artifacts, '_state_identity',
                                 return_value=(self.control, self.report, {}))

    def document(self):
        document = copy.deepcopy(self.report['sourceIdentity'])
        document['records'] = copy.deepcopy(self.rows)
        for index, row in enumerate(document['records']):
            row.update(baselineVerified=index == 0,
                       baselineStatus='matched_search' if index == 0 else 'no_match',
                       baselineReason='matched_search' if index == 0 else 'title_mismatch',
                       baselineMetadata=copy.deepcopy(self.old_metadata) if index == 0 else {})
        return document

    def prepare(self, scope='smoke'):
        baseline = {'digest': 'sha256:' + 'b' * 64, 'queueSha256': 'c' * 64,
                    'baselineTotals': copy.deepcopy(self.report['totals'])}
        with mock.patch.object(evaluation, 'restore_baseline', return_value=baseline), \
             mock.patch.object(evaluation, 'baseline_document', return_value=self.document()):
            return evaluation.prepare(baseline_artifact_id=9, source_artifact_id=42,
                destination=self.baseline, state_dir=self.state, test_id='safe-rule-test', scope=scope)

    def run_evaluation(self, **changes):
        arguments = {'state_dir': self.state, 'test_id': 'safe-rule-test',
                     'baseline_artifact_id': 9, 'source_artifact_id': 42,
                     'scope': 'smoke', 'component_dir': self.baseline}
        arguments.update(changes)
        return evaluation.run(**arguments)

    def config(self):
        return evaluation.artifacts.read_json(self.state / evaluation.CONFIG_FILE)

    def write_config(self, value):
        evaluation.artifacts.atomic_json(self.state / evaluation.CONFIG_FILE, value)

    def test_baseline_reads_immutable_and_keeps_original_provider_item_unchanged(self):
        self.make_database()
        original = (self.baseline / 'queue.sqlite').read_bytes()
        connect = sqlite3.connect
        calls = []
        def tracked(*args, **kwargs):
            calls.append((args, kwargs))
            return connect(*args, **kwargs)
        with self.identity(), mock.patch.object(evaluation.sqlite3, 'connect', side_effect=tracked):
            document = evaluation.baseline_document(self.baseline)
        self.assertIn('?mode=ro&immutable=1', calls[0][0][0])
        self.assertTrue(calls[0][1]['uri'])
        self.assertEqual((self.baseline / 'queue.sqlite').read_bytes(), original)
        self.assertEqual([row['item'] for row in document['records']], [row['item'] for row in self.rows])
        first = document['records'][0]
        self.assertIsNone(first['item']['tmdbId'])
        self.assertIsNone(first['item']['releaseDate'])
        self.assertEqual(first['baselineMetadata'], self.old_metadata)
        self.assertEqual([row['baselineVerified'] for row in document['records']], [True, False, False])
        self.assertEqual([row['baselineUnresolved'] for row in document['records']], [False, True, False])
        self.assertEqual(document['workerBaselineSourceArtifactId'], 42)
        self.assertEqual(document['workerBaselineTotals'], self.report['totals'])

    def test_baseline_rejects_changed_provider_bytes_and_mismatched_identity(self):
        self.make_database()
        with closing(sqlite3.connect(self.baseline / 'queue.sqlite')) as connection:
            value = copy.deepcopy(self.rows[0])
            value['item']['title'] = 'Tampered provider title'
            connection.execute('UPDATE items SET payload=? WHERE content_id=?',
                               (json.dumps(value), value['contentId']))
            connection.commit()
        with self.identity(), self.assertRaisesRegex(ValueError, 'provider input changed'):
            evaluation.baseline_document(self.baseline)
        with closing(sqlite3.connect(self.baseline / 'queue.sqlite')) as connection:
            value['item']['contentId'] = 'server_1:movie:999'
            connection.execute('UPDATE items SET payload=?,source_hash=? WHERE content_id=?',
                               (json.dumps(value), source_hash(value), self.rows[0]['contentId']))
            connection.commit()
        with self.identity(), self.assertRaisesRegex(ValueError, 'invalid original provider'):
            evaluation.baseline_document(self.baseline)

    def test_baseline_rejects_each_sealed_count_mismatch_and_does_not_mutate_identity(self):
        self.make_database()
        expected = copy.deepcopy(self.report)
        with self.identity():
            document = evaluation.baseline_document(self.baseline)
        document['workerBaselineTotals']['total'] = 999
        self.assertEqual(self.report, expected)
        for key in ('total', 'matched', 'unresolved'):
            bad = copy.deepcopy(self.report)
            bad['totals'][key] += 1
            with self.subTest(key=key), \
                 mock.patch.object(evaluation.artifacts, '_state_identity', return_value=(self.control, bad, {})), \
                 self.assertRaisesRegex(ValueError, 'counts differ'):
                evaluation.baseline_document(self.baseline)

    def producer(self, *, run_change=None, artifact_change=None):
        artifact = {'id': 9, 'name': evaluation.artifacts._prefix('state', False) + '123-1',
                    'expired': False, 'size_in_bytes': 100, 'digest': 'sha256:' + 'd' * 64,
                    'workflow_run': {'id': 123}}
        artifact.update(artifact_change or {})
        run = {'id': 123, 'run_attempt': 1, 'path': evaluation.artifacts.WORKFLOW,
               'head_branch': 'main', 'conclusion': 'success', 'head_sha': 'e' * 40,
               'repository': {'id': 7}, 'head_repository': {'id': 7}}
        run.update(run_change or {})
        github = mock.Mock()
        github.get.side_effect = lambda route: artifact if route == '/actions/artifacts/9' else run
        return github

    def test_restore_accepts_only_trusted_successful_production_and_frozen_source(self):
        github = self.producer()
        with mock.patch.object(evaluation.artifacts, '_restore') as restore, self.identity(), \
             mock.patch.object(evaluation.artifacts, 'digest', return_value='f' * 64):
            result = evaluation.restore_baseline(9, self.baseline, source_artifact_id=42, github=github)
        self.assertEqual(result['runId'], 123)
        self.assertEqual(result['headSha'], 'e' * 40)
        self.assertEqual(result['queueSha256'], 'f' * 64)
        self.assertEqual(restore.call_args.kwargs, {'kind': 'state', 'isolated': False})
        with mock.patch.object(evaluation.artifacts, '_restore'), self.identity(), \
             self.assertRaisesRegex(ValueError, 'requested frozen source'):
            evaluation.restore_baseline(9, self.baseline, source_artifact_id=99, github=github)

    def test_restore_rejects_test_state_wrong_workflow_failed_fork_and_other_branch(self):
        cases = [({'head_branch': 'feature'}, None), ({'conclusion': 'failure'}, None),
                 ({'path': '.github/workflows/verify-vod.yml'}, None),
                 ({'head_repository': {'id': 8}}, None), ({'repository': {}}, None),
                 (None, {'name': evaluation.artifacts._prefix('state', True) + '123-1'}),
                 (None, {'digest': None}), (None, {'expired': True})]
        for run_change, artifact_change in cases:
            with self.subTest(run=run_change, artifact=artifact_change), \
                 mock.patch.object(evaluation.artifacts, '_restore') as restore, \
                 self.assertRaises(ValueError):
                evaluation.restore_baseline(9, self.baseline, source_artifact_id=42,
                    github=self.producer(run_change=run_change, artifact_change=artifact_change))
            restore.assert_not_called()

    def test_prepare_uses_real_sampler_policy_and_binds_selection(self):
        with mock.patch.object(evaluation.sampling, 'SAMPLING_POLICY', 77), \
             mock.patch.object(evaluation.sampling, 'select_evaluation', wraps=evaluation.sampling.select_evaluation) as select:
            result = self.prepare()
        select.assert_called_once()
        self.assertEqual(select.call_args.kwargs, {'scope': 'smoke'})
        self.assertTrue(result['prepared'])
        config = self.config()
        self.assertEqual(config['samplingPolicy'], 77)
        self.assertEqual(config['selectionSummary']['samplingPolicyVersion'], 77)
        self.assertEqual(set(config['selectedContentIds']), {row['contentId'] for row in self.rows})
        self.assertEqual(config['forceRecheckContentIds'], [self.rows[0]['contentId']])
        self.assertEqual(config['selectionSha256'], hashlib.sha256(
            evaluation.verification.canonical(config['selectedContentIds'])).hexdigest())
        self.assertNotIn('baselineMetadata', json.dumps(config))
        self.assertNotIn('987654', json.dumps(config))

    def test_prepare_full_avoids_an_all_records_id_list(self):
        self.prepare('full')
        self.assertIsNone(self.config()['selectedContentIds'])
        self.assertEqual(self.config()['forceRecheckContentIds'], [self.rows[0]['contentId']])

    def test_prepare_requires_fresh_separate_paths_and_valid_test_scope(self):
        for destination, state in ((self.baseline, self.baseline),
                                   (self.baseline, self.baseline / 'nested'),
                                   (self.baseline / 'nested', self.baseline)):
            with self.subTest(destination=destination, state=state), \
                 mock.patch.object(evaluation, 'restore_baseline') as restore, self.assertRaises(ValueError):
                evaluation.prepare(baseline_artifact_id=9, source_artifact_id=42,
                    destination=destination, state_dir=state, test_id='safe-rule-test')
            restore.assert_not_called()
        self.baseline.mkdir()
        with mock.patch.object(evaluation, 'restore_baseline') as restore, self.assertRaisesRegex(ValueError, 'fresh'):
            evaluation.prepare(baseline_artifact_id=9, source_artifact_id=42,
                destination=self.baseline, state_dir=self.state, test_id='safe-rule-test')
        restore.assert_not_called()
        self.baseline.rmdir()
        for value in ({'test_id': '../production'}, {'test_id': 'bad\nidentity'}, {'scope': 'production'}):
            arguments = {'baseline_artifact_id': 9, 'source_artifact_id': 42,
                         'destination': self.baseline, 'state_dir': self.state, 'test_id': 'safe-rule-test'}
            arguments.update(value)
            with self.subTest(value=value), mock.patch.object(evaluation, 'restore_baseline') as restore, self.assertRaises(ValueError):
                evaluation.prepare(**arguments)
            restore.assert_not_called()

    def test_run_rejects_request_changes_or_config_tamper_before_verification(self):
        self.prepare()
        original = self.config()
        for change in ({'test_id': 'another-test'}, {'scope': 'full'},
                       {'source_artifact_id': 43}, {'baseline_artifact_id': 10}):
            with self.subTest(change=change), mock.patch.object(evaluation.verification, 'run_batch') as batch, \
                 self.assertRaisesRegex(ValueError, 'frozen selection'):
                self.run_evaluation(**change)
            batch.assert_not_called()
        for field, value in (('samplingPolicy', -1), ('selectionSha256', '0' * 64),
                             ('forceRecheckSha256', '0' * 64), ('selectedContentIds', []),
                             ('forceRecheckContentIds', [])):
            config = copy.deepcopy(original)
            config[field] = value
            self.write_config(config)
            with self.subTest(field=field), mock.patch.object(evaluation.verification, 'run_batch') as batch, \
                 self.assertRaisesRegex(ValueError, 'frozen selection'):
                self.run_evaluation()
            batch.assert_not_called()
        self.write_config(original)

    def test_run_rejects_unsafe_content_ids_even_when_list_hash_matches(self):
        self.prepare()
        config = self.config()
        config['selectedContentIds'] = ['server_1:movie:../../production']
        config['selectionSha256'] = hashlib.sha256(evaluation.verification.canonical(config['selectedContentIds'])).hexdigest()
        self.write_config(config)
        with mock.patch.object(evaluation.verification, 'run_batch') as batch, \
             self.assertRaisesRegex(ValueError, 'invalid content IDs'):
            self.run_evaluation()
        batch.assert_not_called()

    def test_run_only_schedules_selected_inputs_and_excludes_forced_warm_results(self):
        self.prepare()
        config = self.config()
        records = copy.deepcopy(self.rows) + [provider_record(100)]
        warmed = [(row['contentId'], {'tmdbId': '123'}) for row in records]
        with mock.patch.object(evaluation.verification, 'run_batch', return_value={'complete': True}) as batch:
            self.assertEqual(self.run_evaluation(rate=7, workers=2), {'complete': True})
        arguments = batch.call_args.kwargs
        chosen, cached = arguments['record_selector'](records, warmed)
        self.assertEqual(chosen, records[:-1])
        self.assertEqual({cid for cid, _ in cached}, set(config['selectedContentIds']) - set(config['forceRecheckContentIds']))
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            arguments['record_selector'](records[1:], warmed)
        self.assertEqual(arguments['rate'], 7)
        self.assertEqual(arguments['workers'], 2)
        self.assertEqual(arguments['seed_cache'], self.baseline / 'cache')
        self.assertEqual(arguments['verification_context']['baselineQueueSha256'], config['baselineQueueSha256'])
        self.assertNotIn('selectedContentIds', arguments['verification_context'])

    def test_forced_control_rechecks_bypass_cache_and_loader_binds_original_database(self):
        self.prepare()
        with mock.patch.object(evaluation.verification, 'run_batch') as batch:
            self.run_evaluation()
        arguments = batch.call_args.kwargs
        client = object.__new__(arguments['client_factory'])
        forced = self.rows[0]['contentId']
        with mock.patch.object(evaluation.verification.VerificationClient, '_identity_cache', return_value=('cached', True, False)) as parent:
            self.assertEqual(client._identity_cache('key', {'contentId': forced}, 'movie', None, 'title', '2024'),
                             (None, False, False))
            parent.assert_not_called()
            self.assertEqual(client._identity_cache('key', {'contentId': self.rows[1]['contentId']}, 'movie', None, 'title', '2024'),
                             ('cached', True, False))
            parent.assert_called_once()
        with mock.patch.object(evaluation.artifacts, 'digest', return_value='wrong'), \
             mock.patch.object(evaluation, 'baseline_document') as loader, \
             self.assertRaisesRegex(ValueError, 'database changed'):
            arguments['input_loader'](self.baseline)
        loader.assert_not_called()
        with mock.patch.object(evaluation.artifacts, 'digest', return_value='c' * 64), \
             mock.patch.object(evaluation, 'baseline_document', return_value=self.document()) as loader:
            self.assertEqual(arguments['input_loader'](self.baseline), self.document())
        loader.assert_called_once_with(self.baseline)


if __name__ == '__main__':
    unittest.main()
