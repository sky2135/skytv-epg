"""Rate-limited, checkpointed verification of a frozen catalog; never publishes."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import vod_tmdb as tmdb
from build_skytvplayer_catalog import _metadata_result_key
from vod_provider_evidence import MovieEvidenceCollector
from vod_verification_checkpoint import Checkpoint
from vod_verification_input import load_component_input
from vod_verification_rate_limit import PacedSession, SharedRateLimiter

REPORT_FIELDS = ('tmdbId', 'matchStatus', 'releaseDate', 'originalTitle',
                 'originalLanguage', 'rejectedIdentity')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def small_metadata(value):
    return {key: value.get(key) for key in REPORT_FIELDS}


def code_identity():
    root = Path(__file__).resolve().parent
    names = ('verify_vod_catalog.py', 'vod_tmdb.py', 'vod_poster_index.py',
             'vod_reviewed_corrections.py', 'vod_provider_evidence.py',
             'build_skytvplayer_catalog.py', 'build_vod_catalogs.py',
             'vod_verification_input.py', 'vod_verification_checkpoint.py',
             'vod_verification_rate_limit.py')
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    hashes['data/vod_reviewed_corrections.json'] = hashlib.sha256(
        (root.parent / 'data/vod_reviewed_corrections.json').read_bytes()).hexdigest()
    return hashlib.sha256(canonical(hashes)).hexdigest()


class VerificationClient(tmdb.TmdbClient):
    """Reuse current positive proof, recheck negatives, and bridge added hints.

    An old ID-free cache is only a candidate. The normal current-source proof
    validator still checks its title, year, ID, poster and bound evidence. A
    current rejected identity must never be replaced with that earlier proof.
    """
    def _identity_cache(self, key, item, kind, identifier, title, year):
        cached, fresh, rejected = super()._identity_cache(key, item, kind, identifier, title, year)
        if cached and cached['matchStatus'].startswith('matched_'):
            return cached, fresh, rejected
        if not cached and not rejected and identifier:
            old_key = key.replace(f':id:{identifier}:title:', ':id::title:', 1)
            candidates = [old_key]
            if tmdb.poster_identity(item):
                candidates.append(old_key.rsplit(f':poster-v{tmdb.POSTER_POLICY}:', 1)[0])
            for candidate in dict.fromkeys(candidates):
                value, valid_fresh, invalid = self._main_identity_cache(
                    candidate, item, kind, identifier, title, year)
                if value and value['matchStatus'].startswith('matched_') and not invalid:
                    self.stats['sourceCacheMigrations'] = self.stats.get('sourceCacheMigrations', 0) + 1
                    return value, valid_fresh, invalid
        # A full verification pass revisits negative outcomes. Transient cached
        # responses must not turn the next attempt into another zero-I/O miss.
        return None, False, rejected


def prepare_records(document, evidence_dir, client):
    records, warmed, candidates, posters = [], [], [], set()
    collector = MovieEvidenceCollector(evidence_dir, max_requests=0, max_seconds=0)
    try:
        for source in document['records']:
            row = copy.deepcopy(source)
            item, kind = row['item'], row['kind']
            poster = tmdb.poster_identity(item)
            if poster:
                posters.add(poster)
            facts = collector.load(item) if kind == 'movie' else None
            if facts:
                item['_identityEvidence'] = facts
            row['identityKey'] = hashlib.sha256(canonical(_metadata_result_key(item, kind, None))).hexdigest()
            key, identifier, title, year = tmdb._enrichment_identity(item, kind)
            cached, fresh, _rejected = client._identity_cache(key, item, kind, identifier, title, year)
            if cached and fresh and cached['matchStatus'].startswith('matched_'):
                candidates.append(row)
            records.append(row)
    finally:
        collector.close()
    # A later source can reveal that an earlier poster is shared by multiple
    # IDs. Decide completed poster matches only after the entire warm scan.
    for row in candidates:
        key, identifier, title, year = tmdb._enrichment_identity(row['item'], row['kind'])
        cached, fresh, _rejected = client._identity_cache(key, row['item'], row['kind'], identifier, title, year)
        if cached and fresh and cached['matchStatus'].startswith('matched_'):
            warmed.append((row['contentId'], small_metadata(cached)))
    client.verification_collision_paths = sorted(path for path in posters if client.poster_index.collided(path))
    return records, warmed


def warm_completed(checkpoint, client):
    for row in checkpoint.completed_records():
        key, identifier, title, year = tmdb._enrichment_identity(row['item'], row['kind'])
        client._identity_cache(key, row['item'], row['kind'], identifier, title, year)


def reopen_collided_posters(checkpoint, client):
    revoked, paths = [], set(checkpoint.get_meta('collidedPosterPaths', []))
    for row in checkpoint.completed_records():
        path = tmdb.poster_identity(row['item'])
        if row['result']['metadata']['matchStatus'] == 'matched_poster' and client.poster_index.collided(path):
            revoked.append(row['contentId'])
            paths.add(path)
    if revoked:
        checkpoint.set_meta('collidedPosterPaths', sorted(paths))
        checkpoint.reopen(revoked, 'poster_collision')
    return len(revoked)


def seed_collision_tombstones(client, paths):
    # These are rejection-only sentinels, never candidates or verified IDs.
    # Reopened matches must not forget a known collision on the next process.
    for path in paths:
        proof = {'names': ['verification collision'], 'year': None}
        client.poster_index.add('movie', None, [path], proof)
        client.poster_index.add('series', None, [path], proof)


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(canonical(value) + b'\n')
    temporary.replace(path)


def run_batch(*, state_dir, test_id, component_dir=None, seed_cache=None,
              source_artifact_id=0, seed_cache_key='', rate=20, workers=8,
              batch_seconds=900, batch_records=25000, client_factory=VerificationClient,
              token='', api_key='', session_factory=PacedSession):
    state_dir = Path(state_dir)
    for source in (component_dir, seed_cache):
        if source is not None:
            left, right = state_dir.resolve(), Path(source).resolve()
            if left == right or left in right.parents or right in left.parents:
                raise ValueError('Verification state and frozen/production inputs must be separate')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', test_id):
        raise ValueError('Invalid verification test identity')
    if not 1 <= workers <= 16 or not 1 <= rate <= 25 or not 1 <= batch_seconds <= 1800 or not 1 <= batch_records <= 1000000:
        raise ValueError('Verification batch configuration is outside its bounds')
    if not (token or api_key):
        raise ValueError('TMDB application credential is unavailable')
    state_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    config = {'testId': test_id, 'sourceArtifactId': source_artifact_id,
              'seedCacheKey': seed_cache_key, 'codeIdentity': code_identity(),
              'requestsPerSecond': rate, 'workers': workers, 'requestQuota': None}
    cache_root = state_dir / 'cache'
    checkpoint = Checkpoint(state_dir / 'checkpoint.sqlite')
    contexts, thread_local, contexts_lock = [], threading.local(), threading.Lock()
    fatal = None
    completed_groups = 0
    limiter = None
    ready = False
    try:
        prior_config = checkpoint.get_meta('verificationConfig')
        if prior_config is not None and prior_config != config:
            raise ValueError('Checkpoint belongs to a different frozen verification configuration')
        if prior_config is None:
            if component_dir is None or seed_cache is None:
                raise ValueError('A first batch requires the validated component and seed cache')
            initializing = checkpoint.get_meta('initializingConfig')
            if initializing is not None and initializing != config:
                raise ValueError('Interrupted initialization belongs to another test')
            checkpoint.set_meta('initializingConfig', config)
            checkpoint.set_meta('firstStartedAt', checkpoint.get_meta('firstStartedAt', time.time()))
            document = load_component_input(Path(component_dir))
            source = Path(seed_cache) / 'tmdb'
            if source.is_dir():
                shutil.copytree(source, cache_root / 'tmdb', dirs_exist_ok=True)
            with client_factory(cache_root / 'tmdb', token=token, api_key=api_key,
                                max_requests=0) as warm_client:
                records, warmed = prepare_records(document, Path(seed_cache) / 'provider-evidence', warm_client)
            identity = {key: value for key, value in document.items() if key != 'records'}
            identity['verificationConfig'] = config
            checkpoint.initialize(records, identity)
            checkpoint.set_meta('sourceIdentity', identity)
            checkpoint.set_meta('collidedPosterPaths', getattr(warm_client, 'verification_collision_paths', []))
            for cid, metadata in warmed:
                checkpoint.record_result(cid, metadata, {'reason': metadata['matchStatus'], 'cacheHit': True},
                                         stats={'cacheHits': 1}, transient=False)
            checkpoint.set_meta('initialVerifiedCacheReuse', len(warmed))
            # This is the completed-initialization marker. A restart before it
            # safely replays initialization and identical committed outcomes.
            checkpoint.set_meta('verificationConfig', config)
            del records, document, warmed
            # Exercise a real cross-run checkpoint early, then use the normal
            # longer batches. This is a time boundary, not an API quota.
            batch_seconds = min(batch_seconds, 120)
        ready = True
        deadline = time.time() + batch_seconds
        limiter = SharedRateLimiter(rate=rate, deadline=deadline,
                                    resume_state=checkpoint.get_meta('rateLimiter'),
                                    persist=lambda value: checkpoint.set_meta('rateLimiter', value))
        prior_auth_counts = dict(limiter.snapshot()['statusCounts'])

        def make_client():
            session = session_factory(limiter)
            client = client_factory(cache_root / 'tmdb', token=token, api_key=api_key,
                                    max_requests=None, session=session,
                                    max_elapsed_seconds=max(0, limiter.remaining_seconds))
            with contexts_lock:
                contexts.append((client, session))
            return client

        warm_client = make_client()
        seed_collision_tombstones(warm_client, checkpoint.get_meta('collidedPosterPaths', []))
        warm_completed(checkpoint, warm_client)
        reopened = reopen_collided_posters(checkpoint, warm_client)

        def verify(group):
            if not hasattr(thread_local, 'client'):
                thread_local.client = make_client()
            client = thread_local.client
            client.reset_elapsed_budget(max(0, limiter.remaining_seconds))
            before_stats = dict(client.stats)
            began = time.monotonic()
            item = copy.deepcopy(group[0]['item'])
            metadata = client.enrich(item, group[0]['kind'])
            diagnostic = dict(client.last_match_diagnostic)
            usage = {key: value - before_stats.get(key, 0) for key, value in client.stats.items()
                     if isinstance(value, (float, int))}
            return group, small_metadata(metadata), diagnostic, usage, time.monotonic() - began

        groups = {}
        for row in checkpoint.pending(batch_records):
            groups.setdefault(row['identityKey'], []).append(row)
        groups = {key: checkpoint.pending_group(key) for key in groups}
        queue = iter(groups.values())
        exhausted = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            active = set()
            while active or not exhausted:
                while not exhausted and len(active) < workers:
                    snapshot = limiter.snapshot()
                    if limiter.last_deferred or limiter.remaining_seconds <= 0:
                        exhausted = True
                        break
                    if any(snapshot['statusCounts'].get(str(code), 0) > prior_auth_counts.get(str(code), 0) for code in (401, 403)):
                        fatal = 'tmdb_authentication_rejected'
                        exhausted = True
                        break
                    group = next(queue, None)
                    if group is None:
                        exhausted = True
                        break
                    active.add(executor.submit(verify, group))
                if not active:
                    break
                finished, active = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                for future in finished:
                    group, metadata, diagnostic, usage, elapsed = future.result()
                    ids = [row['contentId'] for row in group]
                    deferred = limiter.last_deferred
                    if metadata['matchStatus'] == 'unavailable' and deferred:
                        reason = 'rate_limited' if deferred.reason in {'rate_limited', 'rate_limit_cooldown'} else 'batch_deadline'
                        checkpoint.defer(ids, reason, max(time.time(), deferred.retry_at), stats=usage, elapsed=elapsed)
                    elif metadata['matchStatus'] == 'unavailable' and limiter.remaining_seconds <= 0:
                        checkpoint.defer(ids, 'batch_deadline', time.time(), stats=usage, elapsed=elapsed)
                    else:
                        checkpoint.record_result(ids, metadata, diagnostic, stats=usage, elapsed=elapsed)
                    completed_groups += 1
                    if completed_groups % 250 == 0:
                        print(json.dumps({'completedGroupsThisBatch': completed_groups,
                                          'wireRequests': limiter.snapshot()['wireAttempts']}, sort_keys=True), flush=True)
        reopened += reopen_collided_posters(checkpoint, warm_client)
        checkpoint.set_meta('posterCollisionRechecks', checkpoint.get_meta('posterCollisionRechecks', 0) + reopened)
        checkpoint.set_meta('rateLimiter', limiter.snapshot())
        checkpoint.set_meta('batchNumber', checkpoint.get_meta('batchNumber', 0) + 1)
        checkpoint.set_meta('executionSeconds', checkpoint.get_meta('executionSeconds', 0) + time.monotonic() - started)
        report = checkpoint.report()
        report.update(testId=test_id, mode='non_publishing_full_vod_verification',
                      newMatches=report['totals']['newMatches'],
                      unresolved=report['totals']['unresolved'],
                      sourceIdentity=checkpoint.get_meta('sourceIdentity'),
                      batchNumber=checkpoint.get_meta('batchNumber'),
                      batchCompletedGroups=completed_groups,
                      batchDeadlineSeconds=batch_seconds,
                      initialVerifiedCacheReuse=checkpoint.get_meta('initialVerifiedCacheReuse', 0),
                      posterCollisionRechecks=checkpoint.get_meta('posterCollisionRechecks', 0),
                      api=limiter.snapshot(), requestQuota=None,
                      batchElapsedSeconds=round(time.monotonic() - started, 3),
                      executionSeconds=round(checkpoint.get_meta('executionSeconds'), 3),
                      totalWallElapsedSeconds=round(time.time() - checkpoint.get_meta('firstStartedAt'), 3),
                      fatalError=fatal,
                      shouldContinue=report['hasUnfinished'] and fatal is None,
                      productionCatalogWritten=False)
        serialized = canonical(report)
        if any(secret and secret.encode() in serialized for secret in (token, api_key)):
            raise ValueError('Verification report contains a credential')
        atomic_json(state_dir / 'report.json', report)
        return report
    except Exception as error:
        # Once inputs are frozen, preserve every already committed identity
        # even if an unexpected operational error stops this batch. The job
        # can upload this sealed checkpoint, but may not automatically loop.
        if not ready:
            raise
        checkpoint.set_meta('batchNumber', checkpoint.get_meta('batchNumber', 0) + 1)
        checkpoint.set_meta('executionSeconds', checkpoint.get_meta('executionSeconds', 0) + time.monotonic() - started)
        report = checkpoint.report()
        report.update(testId=test_id, mode='non_publishing_full_vod_verification',
                      sourceIdentity=checkpoint.get_meta('sourceIdentity'),
                      batchNumber=checkpoint.get_meta('batchNumber'),
                      newMatches=report['totals']['newMatches'], unresolved=report['totals']['unresolved'],
                      api=limiter.snapshot() if limiter else checkpoint.get_meta('rateLimiter'),
                      requestQuota=None, shouldContinue=False,
                      batchElapsedSeconds=round(time.monotonic() - started, 3),
                      executionSeconds=round(checkpoint.get_meta('executionSeconds'), 3),
                      fatalError='verification_exception_' + type(error).__name__,
                      productionCatalogWritten=False)
        atomic_json(state_dir / 'report.json', report)
        return report
    finally:
        if limiter is not None:
            checkpoint.set_meta('rateLimiter', limiter.snapshot())
        for client, session in contexts:
            client.close()
            session.close()
        checkpoint.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--test-id', required=True)
    parser.add_argument('--component-dir', type=Path)
    parser.add_argument('--seed-cache', type=Path)
    parser.add_argument('--source-artifact-id', type=int, required=True)
    parser.add_argument('--seed-cache-key', required=True)
    parser.add_argument('--rate', type=float, default=20)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--batch-seconds', type=int, default=900)
    parser.add_argument('--batch-records', type=int, default=25000)
    args = parser.parse_args(argv)
    report = run_batch(**vars(args), token=os.environ.get('TMDB_READ_ACCESS_TOKEN', ''),
                       api_key=os.environ.get('TMDB_API_KEY', ''))
    print(json.dumps({key: report.get(key) for key in
                      ('testId', 'batchNumber', 'totals', 'newMatches', 'unresolved', 'hasUnfinished',
                       'api', 'batchElapsedSeconds', 'executionSeconds', 'fatalError')}, sort_keys=True))


if __name__ == '__main__':
    main()
