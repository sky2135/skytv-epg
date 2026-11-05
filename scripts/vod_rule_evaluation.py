"""Evaluate new VOD rules on a frozen copy of validated production inputs.

The old rolling checkpoint is only a read-only source and comparison baseline.
This module creates a fresh verification checkpoint. It never publishes or
changes production state, and never uses old match metadata as provider hints.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from component_artifacts import GitHubArtifacts
import vod_enrichment_artifacts as artifacts
import vod_rule_sampling as sampling
import verify_vod_catalog as verification
from vod_verification_checkpoint import MATCHED, CONTENT_ID

CONFIG_FILE = 'rule-evaluation.json'
MAX_CONFIG_BYTES = 1024 * 1024


def _separate(*paths):
    resolved = [Path(path).resolve() for path in paths]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1:]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError('Rule evaluation inputs and outputs must be separate')


def restore_baseline(artifact_id, destination, *, source_artifact_id, github=None):
    """Explicitly trust a successful main production producer, not test state."""
    github = github or GitHubArtifacts()
    artifact = github.get(f'/actions/artifacts/{int(artifact_id)}')
    identity = artifacts._artifact_identity(artifact, 'state', False)
    if identity is None:
        raise ValueError('Rule baseline is not a production enrichment checkpoint')
    run_id, attempt = identity
    run = github.get(f'/actions/runs/{run_id}')
    if run.get('run_attempt', 1) != attempt:
        run = github.get(f'/actions/runs/{run_id}/attempts/{attempt}')
    repository = run.get('repository') or {}
    head_repository = run.get('head_repository') or {}
    if (run.get('id') != run_id or run.get('run_attempt', 1) != attempt
            or run.get('path') != artifacts.WORKFLOW or run.get('head_branch') != 'main'
            or run.get('conclusion') != 'success' or not repository.get('id')
            or repository.get('id') != head_repository.get('id')):
        raise ValueError('Rule baseline producer is not trusted successful production code')
    # The normal production extractor verifies every file seal, control/report
    # agreement and SQLite integrity. It does not rewrite its production mode.
    artifacts._restore(github, artifact, Path(destination), kind='state', isolated=False)
    control, report, _metadata = artifacts._state_identity(destination)
    if control['sourceArtifactId'] != source_artifact_id:
        raise ValueError('Rule baseline differs from the requested frozen source')
    return {'artifactId': int(artifact_id), 'digest': artifact['digest'],
            'runId': run_id, 'headSha': run['head_sha'],
            'sourceArtifactId': source_artifact_id, 'sourceIdentity': report['sourceIdentity'],
            'baselineTotals': report['totals'],
            'queueSha256': artifacts.digest(Path(destination) / 'queue.sqlite')}


def baseline_document(directory):
    """Original provider item stays unchanged; previous metadata is observation."""
    directory = Path(directory)
    control, report, _metadata = artifacts._state_identity(directory)
    records = []
    matched = unresolved = 0
    database = directory / 'queue.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.row_factory = sqlite3.Row
        rows = connection.execute('SELECT * FROM items WHERE active=1 ORDER BY content_id')
        for row in rows:
            record = json.loads(row['payload'])
            cid = record.get('contentId')
            if (cid != row['content_id'] or not isinstance(cid, str) or not CONTENT_ID.fullmatch(cid)
                    or record.get('kind') != row['kind'] or record.get('serverId') != row['server_id']
                    or not isinstance(record.get('item'), dict) or record['item'].get('contentId') != cid):
                raise ValueError('Rule baseline contains an invalid original provider record')
            source_hash = hashlib.sha256(json.dumps(
                {'item': record['item'], 'kind': record['kind']}, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if source_hash != row['source_hash']:
                raise ValueError('Rule baseline provider input changed')
            previous = json.loads(row['metadata']) if row['metadata'] else {}
            verified = row['state'] == 'done' and row['status'] in MATCHED
            is_unresolved = row['state'] == 'done' and row['status'] == 'no_match'
            matched += int(verified)
            unresolved += int(is_unresolved)
            record.update(baselineVerified=verified, baselineStatus=row['status'] or 'unavailable',
                          baselineMetadata=previous, baselineReason=row['reason'],
                          baselineUnresolved=is_unresolved)
            # baselineMetadata is deliberately never copied into record.item.
            records.append(record)
    totals = report['totals']
    if len(records) != totals['total'] or matched != totals['matched'] or unresolved != totals['unresolved']:
        raise ValueError('Rule evaluation baseline counts differ from the sealed report')
    identity = copy.deepcopy(report['sourceIdentity'])
    identity.update(records=records, workerBaselineTotals=copy.deepcopy(totals),
                    workerBaselineSourceArtifactId=control['sourceArtifactId'])
    return identity


def _context(config):
    return {key: copy.deepcopy(config[key]) for key in (
        'baselineArtifactId', 'baselineArtifactDigest', 'baselineQueueSha256',
        'sourceArtifactId', 'scope', 'selectionSha256', 'forceRecheckSha256', 'samplingPolicy')}


def prepare(*, baseline_artifact_id, source_artifact_id, destination, state_dir, test_id, scope='smoke', github=None):
    destination, state_dir = Path(destination), Path(state_dir)
    _separate(destination, state_dir)
    if destination.exists() or state_dir.exists():
        raise ValueError('A rule evaluation must start in fresh directories')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', test_id) or scope not in {'smoke', 'full'}:
        raise ValueError('Invalid rule evaluation identity or scope')
    baseline = restore_baseline(baseline_artifact_id, destination,
                                source_artifact_id=source_artifact_id, github=github)
    document = baseline_document(destination)
    selection = sampling.select_evaluation(document['records'], scope=scope)
    selected = selection['selectedContentIds']
    forced = selection['forceRecheckContentIds']
    config = {'schemaVersion': 1, 'testId': test_id, 'scope': scope,
              'baselineArtifactId': baseline_artifact_id,
              'baselineArtifactDigest': baseline['digest'],
              'baselineQueueSha256': baseline['queueSha256'],
              'sourceArtifactId': source_artifact_id,
              'selectedContentIds': selected, 'forceRecheckContentIds': forced,
              'selectionSha256': hashlib.sha256(verification.canonical(selected)).hexdigest(),
              'forceRecheckSha256': hashlib.sha256(verification.canonical(forced)).hexdigest(),
              'samplingPolicy': sampling.SAMPLING_POLICY,
              'baselineTotals': baseline['baselineTotals'], 'selectionSummary': selection['summary']}
    raw = verification.canonical(config)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Rule evaluation selection exceeds checkpoint bounds')
    state_dir.mkdir(parents=True)
    artifacts.atomic_json(state_dir / CONFIG_FILE, config)
    return {'prepared': True, 'testId': test_id, 'scope': scope,
            'baselineTotals': baseline['baselineTotals'], 'selection': selection['summary']}


def run(*, state_dir, test_id, baseline_artifact_id, source_artifact_id, scope,
        component_dir=None, rate=10, workers=8, batch_seconds=900, batch_records=25000,
        token='', api_key='', **kwargs):
    state_dir = Path(state_dir)
    config = artifacts.read_json(state_dir / CONFIG_FILE, MAX_CONFIG_BYTES)
    if (config.get('schemaVersion') != 1 or config.get('testId') != test_id
            or config.get('baselineArtifactId') != baseline_artifact_id
            or config.get('sourceArtifactId') != source_artifact_id or config.get('scope') != scope
            or config.get('samplingPolicy') != sampling.SAMPLING_POLICY
            or config.get('selectionSha256') != hashlib.sha256(verification.canonical(config.get('selectedContentIds'))).hexdigest()
            or config.get('forceRecheckSha256') != hashlib.sha256(verification.canonical(config.get('forceRecheckContentIds'))).hexdigest()):
        raise ValueError('Rule evaluation request differs from its frozen selection')
    forced = set(config['forceRecheckContentIds'])
    selected = None if config['selectedContentIds'] is None else set(config['selectedContentIds'])
    if any(not CONTENT_ID.fullmatch(cid) for cid in forced | (selected or set())):
        raise ValueError('Rule evaluation contains invalid content IDs')

    class EvaluationClient(verification.VerificationClient):
        def _identity_cache(self, key, item, kind, identifier, title, year):
            if item.get('contentId') in forced:
                return None, False, False
            return super()._identity_cache(key, item, kind, identifier, title, year)

    def selector(records, warmed):
        chosen = records if selected is None else [row for row in records if row['contentId'] in selected]
        keep = {row['contentId'] for row in chosen}
        if selected is not None and keep != selected:
            raise ValueError('Frozen rule selection no longer exists in its source')
        return chosen, [(cid, value) for cid, value in warmed if cid in keep and cid not in forced]

    def loader(directory):
        if artifacts.digest(Path(directory) / 'queue.sqlite') != config['baselineQueueSha256']:
            raise ValueError('Frozen rule baseline database changed')
        return baseline_document(directory)

    seed = Path(component_dir) / 'cache' if component_dir is not None else None
    report = verification.run_batch(
        state_dir=state_dir, test_id=test_id, component_dir=component_dir, seed_cache=seed,
        source_artifact_id=source_artifact_id, seed_cache_key='production-artifact-' + str(baseline_artifact_id),
        rate=rate, workers=workers, batch_seconds=batch_seconds, batch_records=batch_records,
        token=token, api_key=api_key, client_factory=EvaluationClient,
        input_loader=loader, record_selector=selector, verification_context=_context(config), **kwargs)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--test-id', required=True)
    parser.add_argument('--baseline-state-artifact-id', type=int, required=True)
    parser.add_argument('--source-artifact-id', type=int, required=True)
    parser.add_argument('--scope', choices=('smoke', 'full'), default='smoke')
    parser.add_argument('--component-dir', type=Path)
    parser.add_argument('--rate', type=float, default=10)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--batch-seconds', type=int, default=900)
    parser.add_argument('--batch-records', type=int, default=25000)
    args = parser.parse_args(argv)
    if args.mode == 'prepare':
        result = prepare(baseline_artifact_id=args.baseline_state_artifact_id,
                         source_artifact_id=args.source_artifact_id, destination=args.component_dir,
                         state_dir=args.state_dir, test_id=args.test_id, scope=args.scope)
    else:
        result = run(baseline_artifact_id=args.baseline_state_artifact_id,
                     source_artifact_id=args.source_artifact_id, state_dir=args.state_dir,
                     test_id=args.test_id, scope=args.scope, component_dir=args.component_dir,
                     rate=args.rate, workers=args.workers, batch_seconds=args.batch_seconds,
                     batch_records=args.batch_records,
                     token=os.environ.get('TMDB_READ_ACCESS_TOKEN', ''), api_key=os.environ.get('TMDB_API_KEY', ''))
    print(json.dumps({key: value for key, value in result.items() if key not in ('sourceIdentity',)}, sort_keys=True))


if __name__ == '__main__':
    main()
