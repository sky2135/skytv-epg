from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import vod_enrichment_artifacts as artifacts
import vod_tmdb as tmdb
from test_vod_tmdb import movie_details


class EnrichmentArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.control = {'schemaVersion': 1, 'sourceArtifactId': 42, 'isolated': False}
        self.report = {'schemaVersion': 1, 'mode': 'rolling_vod_enrichment', 'sourceArtifactId': 42,
            'sourceIdentity': {'inputFingerprint': 'a' * 64}, 'policyIdentity': 'b' * 64,
            'codeIdentity': 'c' * 64, 'batchNumber': 1, 'productionCatalogWritten': False,
            'shouldContinue': True, 'fatalError': None, 'totals': {'total': 1, 'remaining': 1, 'due': 1}, 'api': {}, 'nextAttemptAt': None}
        artifacts.atomic_json(self.state / 'control.json', self.control)
        self.write_report()
        with closing(sqlite3.connect(self.state / 'queue.sqlite')) as connection:
            connection.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            for key in ('sourceIdentity', 'policyIdentity', 'batchNumber'):
                connection.execute('INSERT INTO meta VALUES (?,?)', (key, json.dumps(self.report[key])))
            connection.execute('INSERT INTO meta VALUES (?,?)', ('collidedPosterPaths', '[]'))
            connection.commit()
        self.environment = mock.patch.dict(os.environ, {'GITHUB_REF_NAME': 'main', 'GITHUB_SHA': 'd' * 40,
                                                        'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_report(self):
        artifacts.atomic_json(self.state / 'report.json', self.report)

    def positive(self, *, expires=1_900_000_000, identifier=101):
        details = movie_details(identifier)
        return {'version': 1, 'expiresAt': expires, 'data': {'identityPolicy': tmdb.IDENTITY_POLICY,
            'matchStrategy': tmdb.MATCH_STRATEGY,
            'identity': tmdb._detail_identity(details, 'movie'),
            'metadata': tmdb._metadata(details, 'movie', 'matched_id')}}

    def evidence(self, *, checked=1_700_000_000):
        content_id = 'server_1:movie:77'
        name = hashlib.sha256(content_id.encode()).hexdigest() + '.json'
        return name, {'version': 1, 'contentId': content_id, 'sourceFingerprint': 'f' * 64,
                      'checkedAt': checked, 'facts': {'imdbId': 'tt1234567', 'directors': ['Director'],
                                                    'cast': ['Actor'], 'runtimeMinutes': 122}}

    def zip_tree(self, root, *, extra=None, changed=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    raw = path.read_bytes()
                    relative = path.relative_to(root).as_posix()
                    if changed and relative in changed:
                        raw = changed[relative]
                    entry = tarfile.TarInfo(relative)
                    entry.size = len(raw)
                    archive.addfile(entry, io.BytesIO(raw))
            if extra:
                entry, raw = extra
                archive.addfile(entry, io.BytesIO(raw) if raw is not None else None)
        output = io.BytesIO()
        with ZipFile(output, 'w') as zipped:
            zipped.writestr('cache.tar' if root.name == 'candidate' else 'state.tar', stream.getvalue())
        return output.getvalue()

    def github(self, archive=None, *, kind='state', isolated=False, run_changes=None, rows=None):
        name = artifacts._prefix(kind, isolated) + '123-1'
        artifact = {'id': 456, 'name': name, 'expired': False, 'size_in_bytes': len(archive or b'zip'),
                    'digest': 'sha256:' + hashlib.sha256(archive or b'zip').hexdigest(), 'workflow_run': {'id': 123},
                    'created_at': '2026-01-01T00:00:00Z'}
        run = {'id': 123, 'path': artifacts.WORKFLOW, 'head_branch': 'main',
               'head_sha': 'e' * 40, 'conclusion': 'success', 'run_attempt': 1, **(run_changes or {})}
        github = mock.Mock()
        github.base = 'https://api.github.com/repos/example/repository'
        def get(route):
            if route.startswith('/actions/artifacts?'):
                return {'artifacts': [artifact] if rows is None else rows}
            if route == '/actions/artifacts/456':
                return artifact
            if route == '/actions/runs/123':
                return run
            raise AssertionError(route)
        github.get.side_effect = get
        github.download.side_effect = lambda _artifact, path: path.write_bytes(archive)
        github.session.post.return_value.status_code = 204
        return github, artifact

    def candidate(self):
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('1' * 64 + '.json'), self.positive())
        candidate = self.root / 'candidate'
        artifacts.export_cache(self.state, candidate)
        return candidate

    def test_seal_binds_database_report_control_and_every_cache_byte(self):
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('1' * 64 + '.json'), self.positive())
        manifest = artifacts.seal(self.state)
        self.assertEqual(manifest['sourceArtifactId'], 42)
        self.assertIn('queue.sqlite', manifest['files'])
        self.assertIn('cache/tmdb/tmdb-v1/' + '1' * 64 + '.json', manifest['files'])
        artifacts._validate_files(self.state, manifest)
        self.report['batchNumber'] = 2
        self.write_report()
        with self.assertRaises(ValueError):
            artifacts.seal(self.state)

    def test_restore_accepts_successful_prior_code_revision_and_checks_seal(self):
        artifacts.seal(self.state)
        zipped = self.zip_tree(self.state)
        github, artifact = self.github(zipped)
        self.assertTrue(artifacts._trusted(github, artifact, 'state', False))
        restored = self.root / 'restored'
        artifacts._restore(github, artifact, restored, kind='state', isolated=False)
        self.assertEqual((restored / 'queue.sqlite').read_bytes(), (self.state / 'queue.sqlite').read_bytes())
        self.assertNotEqual(os.environ['GITHUB_SHA'], 'e' * 40)

    def test_isolated_wrong_branch_workflow_attempt_and_missing_digest_are_rejected(self):
        github, artifact = self.github()
        for change in ({'head_branch': 'other'}, {'path': '.github/workflows/verify-vod.yml'},
                       {'conclusion': 'cancelled'}):
            with self.subTest(change=change):
                github, artifact = self.github(run_changes=change)
                self.assertFalse(artifacts._trusted(github, artifact, 'state', False))
        self.assertIsNone(artifacts._artifact_identity({**artifact, 'name': 'vod-enrichment-test-state-123-1'}, 'state', False))
        self.assertIsNone(artifacts._artifact_identity({**artifact, 'digest': None}, 'state', False))

    def test_failed_batch_requires_successful_seal_and_correct_upload_job_steps(self):
        github, artifact = self.github(run_changes={'conclusion': 'failure'})
        original = github.get.side_effect
        steps = [{'name': name, 'conclusion': 'success'} for name in (
            'Seal durable progress and export verified cache candidates',
            'Retain the sealed checkpoint before any continuation')]
        github.get.side_effect = lambda route: {'jobs': [{'name': artifacts.JOB, 'run_attempt': 1, 'steps': steps}]} if route.endswith('/jobs?per_page=100') else original(route)
        self.assertTrue(artifacts._trusted(github, artifact, 'state', False))
        artifact['name'] = artifacts._prefix('cache', False) + '123-1'
        self.assertFalse(artifacts._trusted(github, artifact, 'cache', False))
        steps.append({'name': 'Retain verified candidates for independent publication', 'conclusion': 'success'})
        self.assertTrue(artifacts._trusted(github, artifact, 'cache', False))
        steps[0]['conclusion'] = 'failure'
        self.assertFalse(artifacts._trusted(github, artifact, 'state', False))

    def test_archive_traversal_links_duplicate_and_size_bounds_are_rejected(self):
        artifacts.seal(self.state)
        traversal = tarfile.TarInfo('../escape')
        traversal.size = 1
        link = tarfile.TarInfo('cache/link')
        link.type, link.linkname = tarfile.SYMTYPE, '../escape'
        duplicate = tarfile.TarInfo('report.json')
        duplicate.size = 1
        for number, extra in enumerate(((traversal, b'x'), (link, None), (duplicate, b'x'))):
            with self.subTest(number=number):
                archive = self.root / f'bad{number}.zip'
                archive.write_bytes(self.zip_tree(self.state, extra=extra))
                with self.assertRaises(ValueError):
                    artifacts._extract(archive, self.root / f'bad{number}')
        archive = self.root / 'oversized.zip'
        archive.write_bytes(self.zip_tree(self.state))
        with mock.patch.object(artifacts, 'LIMIT', 1), self.assertRaises(ValueError):
            artifacts._extract(archive, self.root / 'too-large')
        self.assertFalse((self.root / 'escape').exists())

    def test_restore_detects_tampered_cache_before_copying_destination(self):
        artifacts.seal(self.state)
        zipped = self.zip_tree(self.state, changed={'report.json': b'{}'})
        github, artifact = self.github(zipped)
        with self.assertRaises(ValueError):
            artifacts._restore(github, artifact, self.root / 'restored', kind='state', isolated=False)
        self.assertFalse((self.root / 'restored').exists())

    def test_export_keeps_full_positive_envelope_and_sanitized_evidence_only(self):
        positive = self.positive()
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('1' * 64 + '.json'), positive)
        negative = copy.deepcopy(positive)
        negative['data']['metadata'] = tmdb._empty(None, 'no_match')
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('2' * 64 + '.json'), negative)
        name, facts = self.evidence()
        artifacts.atomic_json(self.state / 'cache/provider-evidence' / name, facts)
        result = artifacts.export_cache(self.state, self.root / 'candidate')
        self.assertEqual(result, {'positive': 1, 'trending': 0, 'providerEvidence': 1, 'skipped': 1})
        copied = artifacts.read_json(self.root / 'candidate/cache/tmdb/tmdb-v1' / ('1' * 64 + '.json'))
        self.assertEqual(copied['data']['metadata']['cast'], positive['data']['metadata']['cast'])
        self.assertEqual(copied['data']['metadata']['overview'], positive['data']['metadata']['overview'])

    def test_positive_validation_rejects_mismatch_partial_metadata_and_raw_proof(self):
        for change in ('id', 'small', 'raw', 'diagnostic'):
            with self.subTest(change=change):
                value = self.positive()
                if change == 'id':
                    value['data']['identity']['tmdbId'] = '999'
                elif change == 'small':
                    value['data']['metadata'] = {'tmdbId': '101', 'matchStatus': 'matched_id'}
                elif change == 'raw':
                    value['data']['identity']['rawResponse'] = 'secret'
                else:
                    value['data']['matchDiagnostic'] = {'requestUrl': 'https://private.invalid'}
                self.assertFalse(artifacts._positive(value))

    def test_matched_poster_export_requires_valid_path_and_rejects_known_collision(self):
        value = self.positive()
        value['data']['metadata']['matchStatus'] = 'matched_poster'
        value['data']['identity'].update(mediaKind='movie', posterPolicy=tmdb.POSTER_POLICY, posterPath='/abc123.jpg')
        self.assertTrue(artifacts._positive(value))
        value['data']['identity']['posterPath'] = '../bad'
        self.assertFalse(artifacts._positive(value))
        value['data']['identity']['posterPath'] = '/abc123.jpg'
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('1' * 64 + '.json'), value)
        with closing(sqlite3.connect(self.state / 'queue.sqlite')) as connection:
            connection.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(['/abc123.jpg']), 'collidedPosterPaths'))
            connection.commit()
        result = artifacts.export_cache(self.state, self.root / 'candidate')
        self.assertEqual(result['positive'], 0)
        self.assertEqual(result['skipped'], 1)

    def test_import_preserves_fresh_current_policy_explicit_rejection(self):
        candidate = self.candidate()
        github, _artifact = self.github(self.zip_tree(candidate), kind='cache')
        destination = self.root / 'production-cache'
        path = destination / 'tmdb/tmdb-v1' / ('1' * 64 + '.json')
        rejected = self.positive(expires=2_000_000_000)
        rejected['data']['metadata'] = {**tmdb._empty(None, 'no_match'), 'rejectedIdentity': True}
        artifacts.atomic_json(path, rejected)
        result = artifacts.import_cache(destination, github=github)
        self.assertEqual(result['preserved'], 1)
        self.assertEqual(artifacts.read_json(path), rejected)

    def test_prepare_new_queue_restores_current_component_and_requests_seed(self):
        github, _artifact = self.github(rows=[])
        github._latest_validated.return_value = {'id': 42}
        with mock.patch('enrich_vod_catalog.policy_identity', return_value='b' * 64):
            result = artifacts.prepare(self.root / 'new-state', self.root / 'component', github=github)
        self.assertTrue(result['should_run'])
        self.assertTrue(result['seed_required'])
        self.assertEqual(artifacts.read_json(self.root / 'new-state/control.json')['sourceArtifactId'], 42)
        github._restore_artifact.assert_called_once()

    def test_prepare_idle_uses_small_report_without_large_state_or_component_download(self):
        github, artifact = self.github()
        github._latest_validated.return_value = {'id': 42}
        report = {**self.report, 'totals': {'total': 1, 'due': 0, 'remaining': 0}, 'shouldContinue': False}
        with mock.patch.object(artifacts, '_small_report', return_value=report), \
             mock.patch.object(artifacts, '_restore') as restore, \
             mock.patch('enrich_vod_catalog.policy_identity', return_value='b' * 64):
            result = artifacts.prepare(self.root / 'new-state', self.root / 'component', github=github)
        self.assertFalse(result['should_run'])
        restore.assert_not_called()
        github._restore_artifact.assert_not_called()
        self.assertFalse((self.root / 'new-state').exists())

    def test_prepare_missing_small_report_restores_sealed_state_and_fatal_state_can_recover(self):
        self.report['fatalError'] = 'tmdb_authentication_rejected'
        self.write_report()
        artifacts.seal(self.state)
        github, _artifact = self.github(self.zip_tree(self.state))
        github._latest_validated.return_value = {'id': 42}
        with mock.patch.object(artifacts, '_small_report', return_value=None), \
             mock.patch('enrich_vod_catalog.policy_identity', return_value='b' * 64):
            result = artifacts.prepare(self.root / 'new-state', self.root / 'component', github=github)
        self.assertTrue(result['should_run'])
        self.assertFalse(result['seed_required'])
        self.assertEqual(artifacts.read_json(self.root / 'new-state/control.json')['sourceArtifactId'], 42)

    def test_isolated_continuation_preserves_explicit_source_in_dispatch_and_prepare(self):
        self.control.update(isolated=True, fixedSourceArtifactId=42)
        artifacts.atomic_json(self.state / 'control.json', self.control)
        artifacts.seal(self.state)
        github, _artifact = self.github(self.zip_tree(self.state), isolated=True)
        self.assertTrue(artifacts.continue_work(self.state, 456, isolated=True, github=github)['continued'])
        self.assertEqual(github.session.post.call_args.kwargs['json']['inputs'],
                         {'isolated': 'true', 'source-artifact-id': '42'})
        original = github.get.side_effect
        github.get.side_effect = lambda route: {'id': 42} if route == '/actions/artifacts/42' else original(route)
        github._validated.return_value = True
        with mock.patch('enrich_vod_catalog.policy_identity', return_value='b' * 64):
            result = artifacts.prepare(self.root / 'new-state', self.root / 'component', isolated=True, github=github)
        self.assertTrue(result['should_run'])
        github._latest_validated.assert_not_called()
        self.assertEqual(artifacts.read_json(self.root / 'new-state/control.json')['fixedSourceArtifactId'], 42)

    def test_exact_trending_cache_envelopes_export_import_without_generic_http_cache(self):
        names = sorted(artifacts.TREND_FILES)
        ranking = {'version': 1, 'expiresAt': 1_900_000_000, 'data': {'101': 1, '102': 2}}
        for name in names:
            artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / name, ranking)
        artifacts.atomic_json(self.state / 'cache/tmdb/tmdb-v1' / ('4' * 64 + '.json'), ranking)
        candidate = self.root / 'candidate'
        counts = artifacts.export_cache(self.state, candidate)
        self.assertEqual(counts['trending'], 2)
        self.assertEqual(counts['skipped'], 1)
        github, _artifact = self.github(self.zip_tree(candidate), kind='cache')
        destination = self.root / 'production-cache'
        newer = {**ranking, 'expiresAt': 2_000_000_000, 'data': {'103': 1}}
        artifacts.atomic_json(destination / 'tmdb/tmdb-v1' / names[0], newer)
        result = artifacts.import_cache(destination, github=github)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['preserved'], 1)
        self.assertEqual(artifacts.read_json(destination / 'tmdb/tmdb-v1' / names[0]), newer)
        self.assertEqual(artifacts.read_json(destination / 'tmdb/tmdb-v1' / names[1]), ranking)
        for changed in ({'data': {'0': 1}}, {'data': {'101': True}}, {'data': {'101': 501}},
                        {'data': {'101': 0}}, {'expiresAt': float('inf')}, {'data': {'101': 1}, 'response': 'raw'}):
            with self.subTest(changed=changed):
                self.assertFalse(artifacts._trending({**ranking, **changed}, names[0]))
        self.assertFalse(artifacts._trending(ranking, '4' * 64 + '.json'))

    def test_small_same_run_report_is_bounded_and_validated(self):
        github, state_artifact = self.github()
        stream = io.BytesIO()
        with ZipFile(stream, 'w') as zipped:
            zipped.writestr('report.json', json.dumps(self.report))
        report_bytes = stream.getvalue()
        report_artifact = {**state_artifact, 'id': 457, 'name': 'vod-enrichment-report-123-1',
            'size_in_bytes': len(report_bytes), 'digest': 'sha256:' + hashlib.sha256(report_bytes).hexdigest()}
        original = github.get.side_effect
        github.get.side_effect = lambda route: {'artifacts': [report_artifact]} if route == '/actions/runs/123/artifacts?per_page=100' else original(route)
        github.download.side_effect = lambda _artifact, path: path.write_bytes(report_bytes)
        self.assertEqual(artifacts._small_report(github, state_artifact, False)['batchNumber'], 1)
        with ZipFile(stream := io.BytesIO(), 'w') as zipped:
            zipped.writestr('report.json', json.dumps({**self.report, 'sourceArtifactId': None}))
        report_bytes = stream.getvalue()
        self.assertIsNone(artifacts._small_report(github, state_artifact, False))

    def test_import_preserves_newer_positive_and_unions_collision_tombstones(self):
        candidate = self.candidate()
        manifest = artifacts.read_json(candidate / 'manifest.json')
        manifest['collidedPosterPaths'] = ['/shared.jpg']
        artifacts.atomic_json(candidate / 'manifest.json', manifest)
        github, _artifact = self.github(self.zip_tree(candidate), kind='cache')
        destination = self.root / 'production-cache'
        path = destination / 'tmdb/tmdb-v1' / ('1' * 64 + '.json')
        existing = self.positive(expires=2_000_000_000)
        artifacts.atomic_json(path, existing)
        artifacts.atomic_json(destination / 'enrichment-collisions.json', {'schemaVersion': 1, 'collidedPosterPaths': ['/other.jpg']})
        result = artifacts.import_cache(destination, github=github)
        self.assertEqual(result['preserved'], 1)
        self.assertEqual(artifacts.read_json(path), existing)
        self.assertEqual(artifacts.read_json(destination / 'enrichment-collisions.json')['collidedPosterPaths'], ['/other.jpg', '/shared.jpg'])

    def test_bad_candidate_never_writes_any_destination_and_optional_warns(self):
        candidate = self.candidate()
        artifacts.atomic_json(candidate / 'cache/tmdb/tmdb-v1' / ('2' * 64 + '.json'), {'raw': 'bad'})
        manifest = artifacts.read_json(candidate / 'manifest.json')
        manifest['files'] = artifacts._files(candidate)
        artifacts.atomic_json(candidate / 'manifest.json', manifest)
        github, _artifact = self.github(self.zip_tree(candidate), kind='cache')
        destination = self.root / 'production-cache'
        destination.mkdir()
        keep = destination / 'keep'
        keep.write_bytes(b'original')
        with mock.patch('sys.stderr', new_callable=io.StringIO) as warning:
            result = artifacts.import_cache(destination, github=github, optional=True)
        self.assertEqual(result['warning'], 'candidate_validation_failed')
        self.assertIn('WARNING:', warning.getvalue())
        self.assertEqual(list(destination.iterdir()), [keep])
        self.assertEqual(keep.read_bytes(), b'original')

    def test_missing_candidate_bootstrap_is_normal_and_test_prefix_is_not_production(self):
        github, artifact = self.github(kind='cache', isolated=True)
        self.assertEqual(artifacts.import_cache(self.root / 'empty', github=github),
                         {'found': False, 'imported': 0, 'preserved': 0})
        self.assertFalse((self.root / 'empty').exists())

    def test_truncated_history_does_not_silently_reset_the_durable_queue(self):
        github, _artifact = self.github(rows=[{'id': number} for number in range(100)])
        with mock.patch.object(artifacts.component_artifacts, 'MAX_ARTIFACT_PAGES', 1), self.assertRaises(ValueError):
            artifacts._latest(github, 'state', False)

    def test_latest_state_and_cache_use_upload_time_when_ids_are_not_monotonic(self):
        for kind in ('state', 'cache', 'report'):
            with self.subTest(kind=kind):
                _github, sample = self.github(kind=kind)
                old = {**sample, 'id': 999, 'name': artifacts._prefix(kind, False) + '999-1',
                       'workflow_run': {'id': 999}, 'created_at': '2026-01-01T00:09:00Z'}
                new = {**sample, 'id': 10, 'name': artifacts._prefix(kind, False) + '10-2',
                       'workflow_run': {'id': 10}, 'created_at': '2026-01-01T00:10:00Z'}
                github = mock.Mock()
                def get(route):
                    if route.startswith('/actions/artifacts?'):
                        return {'artifacts': [old, new]}
                    if route == '/actions/runs/10':
                        return {'id': 10, 'run_attempt': 2, 'head_branch': 'main', 'path': artifacts.WORKFLOW, 'conclusion': 'success'}
                    raise AssertionError(route)
                github.get.side_effect = get
                self.assertEqual(artifacts._latest(github, kind, False)['id'], 10)

    def test_invalid_upload_timestamp_never_silently_resumes_older_state(self):
        for value in (None, '', '2026-02-30T00:00:00Z', '2026-01-01T00:00:00+00:00', '2999-01-01T00:00:00Z'):
            with self.subTest(value=value):
                _github, sample = self.github()
                bad = {**sample, 'id': 999, 'created_at': value}
                github, _ = self.github(rows=[sample, bad])
                with self.assertRaises(ValueError):
                    artifacts._latest(github, 'state', False)

    def test_evidence_rejects_raw_urls_wrong_fingerprint_and_wrong_file_identity(self):
        name, evidence = self.evidence()
        self.assertTrue(artifacts._evidence(evidence, name))
        for change in ('url', 'fingerprint', 'name'):
            with self.subTest(change=change):
                value, filename = copy.deepcopy(evidence), name
                if change == 'url':
                    value['facts']['directors'] = ['https://private.invalid']
                elif change == 'fingerprint':
                    value['sourceFingerprint'] = 'not-a-fingerprint'
                else:
                    filename = '0' * 64 + '.json'
                self.assertFalse(artifacts._evidence(value, filename))

    def test_optional_api_failure_does_not_modify_existing_cache(self):
        github = mock.Mock()
        github.get.side_effect = requests.ConnectionError('do not expose private request details')
        destination = self.root / 'production-cache'
        destination.mkdir()
        (destination / 'keep').write_bytes(b'original')
        with mock.patch('sys.stderr', new_callable=io.StringIO) as warning:
            result = artifacts.import_cache(destination, github=github, optional=True)
        self.assertEqual(result['warning'], 'candidate_unavailable')
        self.assertNotIn('private request details', warning.getvalue())
        self.assertEqual((destination / 'keep').read_bytes(), b'original')

    def test_continue_is_immediate_ref_only_dispatch_with_no_resume_artifact(self):
        artifacts.seal(self.state)
        github, _artifact = self.github()
        result = artifacts.continue_work(self.state, 456, github=github)
        self.assertTrue(result['continued'])
        payload = github.session.post.call_args.kwargs['json']
        self.assertEqual(payload, {'ref': 'main', 'inputs': {'isolated': 'false'}})

    def test_due_trending_work_can_continue_when_identity_queue_is_idle(self):
        self.report.update(totals={'total': 1, 'due': 0, 'remaining': 0},
                           trending={'due': 1, 'hasUnfinished': True}, nextAttemptAt=1_700_000_000)
        self.write_report()
        artifacts.seal(self.state)
        github, _artifact = self.github()
        self.assertTrue(artifacts.continue_work(self.state, 456, github=github)['continued'])

    def test_continue_waits_without_api_or_sleep_and_never_continues_fatal(self):
        for report in ({'api': {'cooldownUntil': 2_000_000_000}}, {'fatalError': 'tmdb_authentication_rejected'},
                       {'totals': {'due': 0}, 'nextAttemptAt': 2_000_000_000}):
            with self.subTest(report=report):
                self.report.update(report)
                self.write_report()
                artifacts.seal(self.state)
                github = mock.Mock()
                with mock.patch.object(artifacts.time, 'time', return_value=1_800_000_000):
                    result = artifacts.continue_work(self.state, 456, github=github)
                self.assertFalse(result['continued'])
                github.get.assert_not_called()
                github.session.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
