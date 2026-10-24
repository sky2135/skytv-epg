"""Bounded real-API smoke; all catalog inputs, caches and queues stay private."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import component_artifacts as artifacts
import build_vod_catalogs as vod
import enrich_vod_catalog as worker
import vod_tmdb as tmdb
from vod_verification_input import load_component_input
from vod_verification_rate_limit import SharedRateLimiter, PacedSession, VerificationDeferred
from verify_vod_catalog import atomic_json, canonical

ROOT = Path('.build/vod-smoke')
RESULTS = ROOT / 'results'
COMPONENT_ID = 12561635164
COMPONENT_DIGEST = 'sha256:c0e5715a78f8519bc62b61ed1bfce95df004229023c4f35086966653de369f49'
EXPECTED = {'954024': '335977', '955380': '346698'}
EXPECTED_GROUPS = {'server_1:movie': 65632, 'server_1:series': 17687,
                   'server_2:movie': 57070, 'server_2:series': 11118,
                   'server_3:movie': 107579, 'server_3:series': 25201}
MAX_WIRE_ATTEMPTS = 72  # Test-only safety ceiling, not a production quota.
WIRE_LOCK = threading.Lock()
WIRE_ATTEMPTS = 0


class SmokeCheckFailure(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SmokeCheckFailure(message)


class SmokeRateLimiter(SharedRateLimiter):
    def record_attempt(self, route, *, rate_limit_retry=False):
        global WIRE_ATTEMPTS
        with WIRE_LOCK:
            if WIRE_ATTEMPTS >= MAX_WIRE_ATTEMPTS:
                self.last_deferred = VerificationDeferred('smoke_wire_ceiling', time.time() + 3600)
                raise self.last_deferred
            WIRE_ATTEMPTS += 1
        return super().record_attempt(route, rate_limit_retry=rate_limit_retry)


def public_index():
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get('https://sky2135.github.io/skytv-epg/skytvplayer/index.json',
                params={'_': str(time.time_ns())}, timeout=(10, 30), allow_redirects=False)
            response.raise_for_status()
            require(len(response.content) < 2 * 1024 * 1024, 'Public index size bound')
            raw = response.content
        index = json.loads(raw)
        references = {row['serverId'] + ':' + kind: row[plural]
            for row in index['servers'] for kind, plural in (('movie', 'movies'), ('series', 'series'))}
        require(set(references) == set(EXPECTED_GROUPS), 'Public server manifest set')
        for reference in references.values():
            require(re.fullmatch(r'[0-9a-f]{64}', reference['sha256']) is not None, 'Public manifest digest')
        return {'available': True, 'indexSha256': hashlib.sha256(raw).hexdigest(),
                'generatedAt': index['generatedAt'], 'vodManifestDigests': {key: value['sha256'] for key, value in references.items()}}
    except Exception as error:
        return {'available': False, 'errorType': type(error).__name__}


def whitelisted(report):
    names = ('schemaVersion', 'mode', 'codeIdentity', 'policyIdentity', 'reconciliation', 'batchNumber',
        'sourceArtifactId', 'processedThisBatch', 'api', 'requestQuota', 'requestsPerSecond', 'workers',
        'elapsedSeconds', 'fatalError', 'fatalFrames', 'shouldContinue', 'productionCatalogWritten',
        'totals', 'byGroup', 'unresolvedReasons', 'complete', 'hasUnfinished', 'nextAttemptAt', 'providerEvidence')
    result = {key: report[key] for key in names if key in report}
    source = report.get('sourceIdentity') or {}
    result['sourceIdentity'] = {key: source[key] for key in (
        'schemaVersion', 'generatedAt', 'sourceGeneratedAt', 'inputFingerprint', 'matchingPolicies',
        'servers', 'recordCount', 'movieCount', 'seriesCount', 'componentSha256') if key in source}
    return result


def queue_snapshot(path):
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT content_id,attempts,status,state,server_id,kind FROM items WHERE active=1").fetchall()
    return {row['content_id']: dict(row) for row in rows}


def main():
    started = time.monotonic()
    RESULTS.mkdir(parents=True, exist_ok=True)
    before = public_index()
    client = artifacts.GitHubArtifacts()
    artifact = client.get('/actions/artifacts/' + str(COMPONENT_ID))
    require(artifact.get('id') == COMPONENT_ID and artifact.get('digest') == COMPONENT_DIGEST
            and artifact.get('name') == 'vod-snapshot-40095954027-1', 'Unexpected fixed component artifact')
    require(client._validated('vod', artifact, {}, {}), 'Component build provenance')
    component = ROOT / 'component'
    client._restore_artifact(artifact, component)
    document = load_component_input(component)
    actual_groups = {row['serverId'] + ':' + kind: row[count]
                     for row in document['servers'] for kind, count in (('movie', 'movieCount'), ('series', 'seriesCount'))}
    require(actual_groups == EXPECTED_GROUPS and document['recordCount'] == 284287
            and document['movieCount'] == 230281 and document['seriesCount'] == 54006, 'Complete component counts')
    selected = [row for row in document['records'] if row['contentId'] in {'server_3:movie:' + key for key in EXPECTED}]
    require(len(selected) == 2, 'Exact movie source sample')
    token, api_key = os.environ.get('TMDB_READ_ACCESS_TOKEN', ''), os.environ.get('TMDB_API_KEY', '')
    require(bool(token or api_key), 'TMDB credential unavailable')
    seed = ROOT / 'seed'
    probe_limiter = SmokeRateLimiter(rate=5, deadline=time.time() + 90)
    probe_results = []
    with PacedSession(probe_limiter) as session:
        with tmdb.TmdbClient(seed / 'tmdb', token=token, api_key=api_key, max_requests=20,
                            session=session, max_elapsed_seconds=90) as movie_client:
            for row in selected:
                item = row['item']
                result = movie_client.enrich(item, 'movie')
                identifier = str(item['id'])
                require(result.get('matchStatus', '').startswith('matched_') and result.get('tmdbId') == EXPECTED[identifier]
                        and not result.get('rejectedIdentity'), 'Movie identity verification')
                require(tmdb._date(result.get('releaseDate')) is not None, 'Movie exact release date')
                genres = result.get('genres')
                require(isinstance(genres, list) and 0 < len(genres) <= 40
                        and all(isinstance(value, str) and 0 < len(value) <= 100
                                and all(ord(character) >= 32 for character in value) for value in genres), 'Movie genre metadata')
                _genre, normalized_genres = vod.normalize_genres([{'genres': genres}], '')
                require(bool(normalized_genres) and normalized_genres != ['unknown'], 'Movie normalized genre metadata')
                probe_results.append({'contentId': row['contentId'], 'providerId': identifier,
                    'sourceItemSha256': hashlib.sha256(canonical(item)).hexdigest(),
                    'tmdbId': result['tmdbId'], 'matchStatus': result['matchStatus'],
                    'releaseDate': result['releaseDate'], 'genres': normalized_genres,
                    'originalLanguage': result.get('originalLanguage'), 'rejectedIdentity': False})
            probe_stats = dict(movie_client.stats)
    require(len(list((seed / 'tmdb/tmdb-v1').glob('*.json'))) == 2, 'Only two new positive seed envelopes')
    probe_api = probe_limiter.snapshot()
    atomic_json(RESULTS / 'movie-probe.json', {'schemaVersion': 1, 'codeSha': os.environ.get('GITHUB_SHA'),
        'sourceArtifactId': COMPONENT_ID, 'componentSha256': document['componentSha256'], 'movies': probe_results,
        'api': probe_api, 'clientStats': probe_stats, 'productionCatalogWritten': False})
    del document, selected
    state = ROOT / 'state'
    with mock.patch.object(worker, 'SharedRateLimiter', SmokeRateLimiter):
        first = worker.run_batch(state_dir=state, component_dir=component, seed_cache=seed,
            rate=5, workers=2, batch_seconds=60, batch_records=12, token=token, api_key=api_key,
            resolve_provider_details=(), max_provider_detail_requests=0)
        atomic_json(RESULTS / 'batch-1.json', whitelisted(first))
        require(not first['fatalError'] and first['batchNumber'] == 1, 'First resumable batch')
        first_rows = queue_snapshot(state / 'queue.sqlite')
        require(len(first_rows) == 284287, 'Full queue initialization')
        attempted_groups = {row['server_id'] + ':' + row['kind'] for row in first_rows.values() if row['attempts'] > 0}
        require(attempted_groups == set(EXPECTED_GROUPS), 'Fair first batch covers all six datasets')
        done_matched = {cid: row['attempts'] for cid, row in first_rows.items()
                        if row['state'] == 'done' and str(row['status']).startswith('matched_')}
        second = worker.run_batch(state_dir=state, rate=5, workers=2, batch_seconds=60, batch_records=12,
            token=token, api_key=api_key, resolve_provider_details=(), max_provider_detail_requests=0)
        atomic_json(RESULTS / 'batch-2.json', whitelisted(second))
    require(not second['fatalError'] and second['batchNumber'] == 2, 'Second resumable batch')
    reconciliation = second['reconciliation']
    require(reconciliation['added'] == 0 and reconciliation['changed'] == 0 and reconciliation['removed'] == 0
            and reconciliation['retained'] == 284287, 'Continuation preserves queue inventory')
    second_rows = queue_snapshot(state / 'queue.sqlite')
    repeated_positives = sum(second_rows[cid]['attempts'] != attempts for cid, attempts in done_matched.items())
    require(repeated_positives == 0, 'Completed positive cache work was repeated')
    seeds = []
    for identifier in EXPECTED:
        cid = 'server_3:movie:' + identifier
        row = second_rows[cid]
        require(row['state'] == 'done' and str(row['status']).startswith('matched_') and row['attempts'] == 0,
                'Two seed matches reused without requests')
        seeds.append({'contentId': cid, 'status': row['status'], 'attempts': row['attempts']})
    require(first['totals']['total'] == second['totals']['total'] == 284287, 'Stable complete queue count')
    require({key: value['total'] for key, value in second['byGroup'].items()} == EXPECTED_GROUPS, 'Stable six group counts')
    require(WIRE_ATTEMPTS == probe_api['wireAttempts'] + second['api']['wireAttempts'] and WIRE_ATTEMPTS < 80,
            'Authoritative bounded smoke wire accounting')
    after = public_index()
    comparison = {'before': before, 'after': after}
    if before['available'] and after['available']:
        comparison.update(indexBytesUnchanged=before['indexSha256'] == after['indexSha256'],
                          sixVodManifestsUnchanged=before['vodManifestDigests'] == after['vodManifestDigests'])
    summary = {'schemaVersion': 1, 'success': True, 'runId': os.environ.get('GITHUB_RUN_ID'),
        'codeSha': os.environ.get('GITHUB_SHA'), 'sourceArtifactId': COMPONENT_ID,
        'componentCounts': {'total': 284287, 'movies': 230281, 'series': 54006, 'byGroup': EXPECTED_GROUPS},
        'privateProbeSeedEnvelopes': 2, 'seedMatches': seeds, 'completedPositivesRepeated': repeated_positives,
        'firstBatchAttemptedGroups': sorted(attempted_groups),
        'secondBatchReconciliation': reconciliation,
        'probeWireAttempts': probe_api['wireAttempts'], 'workerWireAttemptsCumulative': second['api']['wireAttempts'],
        'totalWireAttempts': WIRE_ATTEMPTS, 'testOnlyWireSafetyCeiling': MAX_WIRE_ATTEMPTS,
        'productionComparison': comparison, 'productionCatalogWritten': False, 'productionCachesWritten': False,
        'elapsedSeconds': round(time.monotonic() - started, 3), 'fullCatalogVerificationComplete': False}
    raw = canonical(summary)
    require(not any(value and value.encode() in raw for value in (token, api_key)), 'Credential-safe summary')
    atomic_json(RESULTS / 'summary.json', summary)
    print(json.dumps({'success': True, 'totalWireAttempts': WIRE_ATTEMPTS, 'fullCatalogVerificationComplete': False,
                      'productionCatalogWritten': False, 'elapsedSeconds': summary['elapsedSeconds']}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        RESULTS.mkdir(parents=True, exist_ok=True)
        allowed = {'ValueError', 'KeyError', 'TypeError', 'OSError', 'AssertionError', 'VerificationDeferred', 'SmokeCheckFailure'}
        failure = {'success': False, 'errorType': type(error).__name__ if type(error).__name__ in allowed else 'UnexpectedError',
            'check': str(error) if isinstance(error, SmokeCheckFailure) else None,
            'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno}
                       for frame in traceback.extract_tb(error.__traceback__)][-8:],
            'totalWireAttempts': WIRE_ATTEMPTS, 'productionCatalogWritten': False}
        atomic_json(RESULTS / 'failure.json', failure)
        print(json.dumps(failure, sort_keys=True))
        raise SystemExit(1) from None
