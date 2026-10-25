"""Refresh private VOD metadata in rolling, resumable, rate-limited batches."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import vod_tmdb as tmdb
import build_vod_catalogs as vod
from build_skytvplayer_catalog import _metadata_result_key
from verify_vod_catalog import atomic_json, canonical, prepare_records, seed_collision_tombstones, small_metadata
from vod_enrichment_queue import EnrichmentQueue, validate_priority_content_ids
from vod_verification_input import load_component_input
from vod_verification_rate_limit import PacedSession, SharedRateLimiter, VerificationDeferred
from vod_provider_evidence import MovieEvidenceCollector


class RollingClient(tmdb.TmdbClient):
    """A transient cached failure cannot suppress the queue's earlier retry."""
    def _identity_cache(self, key, item, kind, identifier, title, year):
        cached, fresh, rejected = super()._identity_cache(key, item, kind, identifier, title, year)
        if cached is not None and cached.get('matchStatus') == 'unavailable':
            return None, False, rejected
        return cached, fresh, rejected


def code_identity():
    directory = Path(__file__).resolve().parent
    names = ('enrich_vod_catalog.py', 'vod_enrichment_queue.py', 'vod_tmdb.py',
             'vod_poster_index.py', 'vod_reviewed_corrections.py', 'vod_provider_evidence.py',
             'build_skytvplayer_catalog.py', 'build_vod_catalogs.py', 'vod_verification_input.py',
             'vod_verification_rate_limit.py', 'verify_vod_catalog.py', 'vod_verification_checkpoint.py')
    return hashlib.sha256(canonical({name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names})).hexdigest()


def policy_identity():
    corrections = Path(__file__).resolve().parents[1] / 'data/vod_reviewed_corrections.json'
    return hashlib.sha256(canonical({'workerPolicy': 1, 'codeIdentity': code_identity(),
        'identityPolicy': tmdb.IDENTITY_POLICY, 'matchStrategy': tmdb.MATCH_STRATEGY,
        'corrections': hashlib.sha256(corrections.read_bytes()).hexdigest()})).hexdigest()


def _separate(state_dir, source):
    if source is not None:
        left, right = state_dir.resolve(), Path(source).resolve()
        if left == right or left in right.parents or right in left.parents:
            raise ValueError('Private enrichment state and input must be separate')


def _provider_evidence(records, client, queue, cache_root, source_identity, servers, maximum, insecure,
                       *, collector_factory=MovieEvidenceCollector):
    """Preserve the former bounded Server 1 disambiguation, not a detail sweep."""
    if not servers or maximum == 0:
        return {'requests': 0, '_changed': []}
    configs = {server: vod.load_server_config(server, allow_insecure_http=insecure) for server in servers}
    generation = source_identity['inputFingerprint']
    budget = queue.get_meta('providerEvidenceBudget', {})
    used = budget.get('used', 0) if budget.get('generation') == generation else 0
    remaining = max(0, maximum - used)
    if not remaining:
        return {'requests': 0, 'generationRequests': used, '_changed': []}
    secrets = [part for config in configs.values() for part in (config.base_url, config.username, config.password)]
    collector = collector_factory(cache_root / 'provider-evidence', configs=configs,
                                  max_requests=remaining, max_seconds=60, secret_values=secrets)
    # Reserve the complete remaining allowance before any provider call. A
    # crashed phase may underuse this allowance, but cannot repeat it unbounded.
    queue.set_meta('providerEvidenceBudget', {'generation': generation, 'used': used + remaining})
    try:
        changed = []
        year = str(datetime.now(timezone.utc).year)
        for record in records:
            item = record['item']
            if record['serverId'] not in configs or record['kind'] != 'movie' or item.get('languages') != ['en'] or item.get('releaseDate'):
                continue
            key, identifier, title, source_year = tmdb._enrichment_identity(item, 'movie')
            if source_year != year:
                continue
            cached, fresh, _rejected = client._identity_cache(key, item, 'movie', identifier, title, source_year)
            if not cached or cached.get('matchStatus') != 'no_match':
                continue
            if fresh and client.ignored_without_artwork(item, 'movie'):
                continue
            facts = collector.collect(record['serverId'], item)
            if facts and facts != item.get('_identityEvidence'):
                item['_identityEvidence'] = facts
                changed.append(record['contentId'])
            if collector.stats['requests'] >= remaining:
                break
        stats = dict(collector.stats)
        queue.set_meta('providerEvidenceBudget', {'generation': generation, 'used': used + stats['requests']})
        stats['generationRequests'] = used + stats['requests']
        stats['_changed'] = changed
        return stats
    finally:
        collector.close()


def _trending_cache(client, kind):
    key = f'{kind}:trending:week'
    cached, fresh = client._read_cache(key, tmdb.MAX_TRENDING_AGE - tmdb.TRENDING_TTL)
    valid = (isinstance(cached, dict) and len(cached) <= 500
             and all(tmdb._identifier(identifier) == identifier and type(rank) is int and 1 <= rank <= 500
                     for identifier, rank in cached.items()))
    if not valid:
        return None, False, None
    try:
        path = client._cache_path(key)
        expires = json.loads(path.read_text(encoding='utf-8'))['expiresAt'] if path.stat().st_size <= tmdb.MAX_CACHE_BYTES else None
    except (OSError, ValueError, KeyError, TypeError):
        expires = None
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
        return cached, False, None
    return cached, bool(fresh), expires


def _refresh_trending(client, queue, limiter):
    """Refresh complete trend caches; a partial/stale response is a retry."""
    saved = queue.get_meta('trendingState', {})
    states = {}
    prior_auth = dict(limiter.snapshot()['statusCounts'])
    for kind in ('movie', 'series'):
        now = time.time()
        cached, fresh, expires = _trending_cache(client, kind)
        old = saved.get(kind, {})
        if fresh:
            states[kind] = {'status': 'fresh', 'count': len(cached), 'attempts': 0, 'nextAttemptAt': expires}
            continue
        if old.get('status') == 'retry' and old.get('nextAttemptAt', 0) > now:
            states[kind] = {**old, 'count': len(cached or {})}
            continue
        snapshot = limiter.snapshot()
        attempts = int(old.get('attempts', 0)) + 1
        deferred = None
        if (limiter.remaining_seconds > 0 and not limiter.last_deferred
                and not any(snapshot['statusCounts'].get(str(code), 0) > prior_auth.get(str(code), 0) for code in (401, 403))):
            client.reset_elapsed_budget(max(0, limiter.remaining_seconds))
            try:
                client.trending(kind)
            except VerificationDeferred as error:
                deferred = error
            # The method can return partial ranks or a stale fallback after an
            # unavailable response. Only a newly fresh cache proves completion.
            cached, fresh, expires = _trending_cache(client, kind)
            deferred = deferred or getattr(client.session, 'last_deferred', None)
        if fresh:
            states[kind] = {'status': 'fresh', 'count': len(cached), 'attempts': 0, 'nextAttemptAt': expires}
        else:
            cooldown = limiter.snapshot().get('cooldownUntil') or 0
            retry_at = deferred.retry_at if deferred else now + min(3600, 30 * 2 ** min(attempts - 1, 7))
            states[kind] = {'status': 'retry', 'count': len(cached or {}), 'attempts': attempts,
                            'nextAttemptAt': max(cooldown, retry_at)}
        queue.set_meta('trendingState', {**saved, **states})
    queue.set_meta('trendingState', states)
    return {'byKind': states, 'nextAttemptAt': min(value['nextAttemptAt'] for value in states.values()),
            'hasUnfinished': any(value['status'] != 'fresh' for value in states.values()),
            'due': sum(value['nextAttemptAt'] <= time.time() for value in states.values())}


def run_batch(*, state_dir, component_dir=None, seed_cache=None, rate=20, workers=8,
              batch_seconds=900, batch_records=25000, token='', api_key='',
              client_factory=RollingClient, session_factory=PacedSession,
              resolve_provider_details=(), max_provider_detail_requests=40, allow_insecure_http=False,
              refresh_trending=False, priority_content_ids=()):
    priority_content_ids = validate_priority_content_ids(priority_content_ids)
    state_dir = Path(state_dir)
    public = Path(__file__).resolve().parents[1] / 'public'
    if state_dir.resolve() == public or public in state_dir.resolve().parents:
        raise ValueError('Enrichment state must remain private')
    for source in (component_dir, seed_cache):
        _separate(state_dir, source)
    if (type(workers) is not int or not 1 <= workers <= 16 or not math.isfinite(rate)
            or not 1 <= rate <= 25 or not math.isfinite(batch_seconds)
            or not 1 <= batch_seconds <= 1800 or type(batch_records) is not int
            or not 1 <= batch_records <= 1000000):
        raise ValueError('Invalid rolling enrichment batch configuration')
    if not (token or api_key):
        raise ValueError('TMDB application credential is unavailable')
    if type(refresh_trending) is not bool:
        raise ValueError('Invalid trending refresh configuration')
    if (type(max_provider_detail_requests) is not int or not 0 <= max_provider_detail_requests <= 200
            or len(set(resolve_provider_details)) != len(resolve_provider_details)
            or set(resolve_provider_details) - {'server_1'}):
        raise ValueError('Invalid bounded provider evidence configuration')
    state_dir.mkdir(parents=True, exist_ok=True)
    started, policy = time.monotonic(), policy_identity()
    queue = EnrichmentQueue(state_dir / 'queue.sqlite')
    previous_policy = queue.get_meta('policyIdentity')
    cache_root = state_dir / 'cache'
    contexts, contexts_lock, local = [], threading.Lock(), threading.local()
    limiter, fatal, processed = None, None, 0
    source_identity, reconciliation = queue.get_meta('sourceIdentity'), None
    provider_stats = {'requests': 0}
    fatal_frames = []
    trending = {'byKind': {}, 'nextAttemptAt': None, 'hasUnfinished': False, 'due': 0}
    priority_report = {'requested': len(priority_content_ids), 'active': 0, 'missing': len(priority_content_ids), 'selected': 0}
    try:
        if seed_cache is not None:
            if source_identity is not None:
                raise ValueError('A rolling seed cache may only initialize a new private state')
            for name in ('tmdb', 'provider-evidence'):
                source = Path(seed_cache) / name
                if source.is_dir():
                    shutil.copytree(source, cache_root / name, dirs_exist_ok=True)
            collisions = Path(seed_cache) / 'enrichment-collisions.json'
            if collisions.is_file():
                cache_root.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(collisions, cache_root / collisions.name)
        if component_dir is None:
            if source_identity is None or queue.get_meta('policyIdentity') != policy:
                raise ValueError('A new or changed rolling worker requires a current validated component')
            document = {'records': queue.records()}
        else:
            document = load_component_input(Path(component_dir))
            source_identity = {key: value for key, value in document.items() if key != 'records'}
        with client_factory(cache_root / 'tmdb', token=token, api_key=api_key, max_requests=0) as warm:
            seed_collision_tombstones(warm, queue.get_meta('collidedPosterPaths', []))
            records, warmed = prepare_records(document, cache_root / 'provider-evidence', warm)
            provider_stats = _provider_evidence(records, warm, queue, cache_root, source_identity,
                resolve_provider_details, max_provider_detail_requests, allow_insecure_http)
            changed_facts = set(provider_stats.pop('_changed', []))
            if changed_facts:
                # The newly collected private facts are matching constraints,
                # never permission to carry an earlier positive outcome over.
                warmed = [(cid, metadata) for cid, metadata in warmed if cid not in changed_facts]
                for record in records:
                    if record['contentId'] not in changed_facts:
                        continue
                    key, identifier, title, year = tmdb._enrichment_identity(record['item'], record['kind'])
                    cached, fresh, _rejected = warm._identity_cache(key, record['item'], record['kind'], identifier, title, year)
                    if cached and fresh and cached.get('matchStatus', '').startswith('matched_'):
                        warmed.append((record['contentId'], small_metadata(cached)))
            for record in records:
                record['physicalKey'] = hashlib.sha256(tmdb._enrichment_identity(record['item'], record['kind'])[0].encode()).hexdigest()
                record['identityKey'] = hashlib.sha256(canonical({'matching': _metadata_result_key(record['item'], record['kind'], None), 'policy': policy})).hexdigest()
            # Reconciliation also runs on continuation, retaining identical rows.
            reconciliation = queue.reconcile(records, policy, source_identity)
            physical_by_id = {record['contentId']: record['physicalKey'] for record in records}
            active_priorities = set(priority_content_ids).intersection(physical_by_id)
            priority_report.update(active=len(active_priorities), missing=len(priority_content_ids) - len(active_priorities))

            def expiry(cid):
                path = cache_root / 'tmdb/tmdb-v1' / (physical_by_id[cid] + '.json')
                try:
                    if not path.is_file() or path.stat().st_size > tmdb.MAX_CACHE_BYTES:
                        return None
                    return json.loads(path.read_text(encoding='utf-8')).get('expiresAt')
                except (OSError, ValueError, TypeError):
                    return None

            queue.cache_validated(warmed, expiry_lookup=expiry)
            queue.reopen_poster_collisions(warm)
        del records, document
        deadline = time.time() + batch_seconds
        limiter = SharedRateLimiter(rate=rate, deadline=deadline, resume_state=queue.get_meta('rateLimiter'),
                                   persist=lambda value: queue.set_meta('rateLimiter', value))
        prior_auth = dict(limiter.snapshot()['statusCounts'])
        physical_locks, physical_locks_lock = {}, threading.Lock()

        def make_client():
            session = session_factory(limiter)
            client = client_factory(cache_root / 'tmdb', token=token, api_key=api_key,
                                    max_requests=None, session=session, max_elapsed_seconds=batch_seconds)
            seed_collision_tombstones(client, queue.get_meta('collidedPosterPaths', []))
            with contexts_lock:
                contexts.append((client, session))
            return client

        if refresh_trending:
            trending = _refresh_trending(make_client(), queue, limiter)

        def enrich(group):
            if not hasattr(local, 'client'):
                local.client = make_client()
            client = local.client
            with physical_locks_lock:
                guard = physical_locks.setdefault(group[0]['physicalKey'], threading.Lock())
            with guard:
                client.reset_elapsed_budget(max(0, limiter.remaining_seconds))
                try:
                    metadata = client.enrich(group[0]['item'], group[0]['kind'])
                except VerificationDeferred as deferred:
                    return group, {'matchStatus': 'unavailable'}, {'reason': 'unavailable'}, deferred
                return group, small_metadata(metadata), dict(client.last_match_diagnostic), getattr(client.session, 'last_deferred', None)

        groups = {}
        selected = queue.pending(batch_records, priority_content_ids=priority_content_ids)
        priority_report['selected'] = sum(row['contentId'] in active_priorities for row in selected)
        for row in selected:
            groups.setdefault(row['identityKey'], None)
        work = iter(queue.pending_group(key) for key in groups)
        exhausted = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            active = set()
            while active or not exhausted:
                while not exhausted and len(active) < workers:
                    snapshot = limiter.snapshot()
                    if any(snapshot['statusCounts'].get(str(code), 0) > prior_auth.get(str(code), 0) for code in (401, 403)):
                        fatal, exhausted = 'tmdb_authentication_rejected', True
                        break
                    if limiter.last_deferred or limiter.remaining_seconds <= 0:
                        exhausted = True
                        break
                    group = next(work, None)
                    if group is None:
                        exhausted = True
                        break
                    if group:
                        active.add(executor.submit(enrich, group))
                if not active:
                    break
                finished, active = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                for future in finished:
                    group, metadata, diagnostic, defer = future.result()
                    defer_until = None
                    if metadata['matchStatus'] == 'unavailable' and defer:
                        defer_until = defer.retry_at
                        diagnostic['reason'] = 'rate_limited' if defer.reason.startswith('rate_limit') else 'batch_deadline'
                    elif metadata['matchStatus'] == 'unavailable' and limiter.remaining_seconds <= 0:
                        defer_until = time.time()
                        diagnostic['reason'] = 'batch_deadline'
                    queue.record_result([row['contentId'] for row in group], metadata, diagnostic, defer_until=defer_until)
                    processed += len(group)
        if contexts:
            queue.reopen_poster_collisions(contexts[0][0])
        snapshot = limiter.snapshot()
        if any(snapshot['statusCounts'].get(str(code), 0) > prior_auth.get(str(code), 0) for code in (401, 403)):
            fatal = 'tmdb_authentication_rejected'
    except Exception as error:
        if reconciliation is None:
            queue.close()
            raise
        fatal = 'enrichment_exception_' + type(error).__name__
        allowed = {path.name for path in Path(__file__).resolve().parent.glob('*.py')}
        fatal_frames = [{'file': Path(frame.filename).name, 'line': frame.lineno}
                        for frame in traceback.extract_tb(error.__traceback__)
                        if Path(frame.filename).name in allowed][-8:]
    finally:
        if limiter is not None:
            queue.set_meta('rateLimiter', limiter.snapshot())
        for client, session in contexts:
            client.close()
            session.close()
    try:
        batch_number = queue.get_meta('batchNumber', 0) + 1
        queue.set_meta('batchNumber', batch_number)
        report = queue.report()
        next_attempts = [value for value in (report['nextAttemptAt'], trending['nextAttemptAt']) if value is not None]
        report['nextAttemptAt'] = min(next_attempts) if next_attempts else None
        report['hasUnfinished'] = report['hasUnfinished'] or trending['hasUnfinished']
        report['complete'] = not report['hasUnfinished']
        control_path = state_dir / 'control.json'
        control = json.loads(control_path.read_text(encoding='utf-8')) if control_path.is_file() else {}
        report.update(schemaVersion=1, mode='rolling_vod_enrichment', sourceIdentity=source_identity,
            codeIdentity=code_identity(), policyIdentity=policy, reconciliation=reconciliation,
            policyChanged=previous_policy is not None and previous_policy != policy, priority=priority_report,
            batchNumber=batch_number, sourceArtifactId=control.get('sourceArtifactId'), providerEvidence=provider_stats,
            trending=trending,
            processedThisBatch=processed, api=limiter.snapshot() if limiter else queue.get_meta('rateLimiter'),
            requestQuota=None, requestsPerSecond=rate, workers=workers,
            elapsedSeconds=round(time.monotonic() - started, 3), fatalError=fatal,
            fatalFrames=fatal_frames,
            shouldContinue=report['hasUnfinished'] and fatal is None, productionCatalogWritten=False)
        raw = canonical(report)
        if any(secret and secret.encode() in raw for secret in (token, api_key)):
            raise ValueError('Enrichment report contains a credential')
        atomic_json(state_dir / 'report.json', report)
        return report
    finally:
        queue.close()


def _priority_json(value):
    try:
        if len(value) > 32768:
            raise ValueError
        return validate_priority_content_ids(json.loads(value))
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Priority JSON must contain at most 500 unique valid contentIds') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--component-dir', type=Path)
    parser.add_argument('--seed-cache', type=Path)
    parser.add_argument('--rate', type=float, default=20)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--batch-seconds', type=float, default=900)
    parser.add_argument('--batch-records', type=int, default=25000)
    parser.add_argument('--resolve-provider-details', action='append', default=[])
    parser.add_argument('--max-provider-detail-requests', type=int, default=40)
    parser.add_argument('--allow-insecure-http', action='store_true')
    parser.add_argument('--refresh-trending', action='store_true')
    parser.add_argument('--priority-content-ids-json', dest='priority_content_ids', type=_priority_json, default=(),
                        help='Up to 500 unique contentIds prioritized only within their existing due server/type groups.')
    args = parser.parse_args(argv)
    report = run_batch(**vars(args), token=os.environ.get('TMDB_READ_ACCESS_TOKEN', ''), api_key=os.environ.get('TMDB_API_KEY', ''))
    print(json.dumps(report, sort_keys=True))
    return 2 if report['fatalError'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit('VOD enrichment validation failed; production was not changed.') from None
