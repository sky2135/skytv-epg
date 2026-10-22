"""Read-only, no-network cache diagnosis for a bounded set of VOD records.

Inputs are sanitized normalized provider records, never the enriched app rows.
Run in a disposable Actions diagnostic job after restoring both exact caches.
The report contains only public identity facts, hashes, and validation outcomes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import vod_tmdb as current
from vod_provider_evidence import MovieEvidenceCollector


class NoNetworkSession:
    def get(self, *_args, **_kwargs):
        raise RuntimeError('Network access forbidden in cache diagnostic')

    def close(self):
        pass


def load_previous(path):
    spec = importlib.util.spec_from_file_location('vod_tmdb_previous_diagnostic', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_document(client, key):
    path = client._cache_path(key)
    out = {'keyHash': hashlib.sha256(key.encode('utf-8')).hexdigest(),
           'exists': path.is_file()}
    if not out['exists']:
        return out, None
    try:
        out['bytes'] = path.stat().st_size
        if out['bytes'] > current.MAX_CACHE_BYTES:
            out['readIssue'] = 'oversize'
            return out, None
        document = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(document, dict):
            out['readIssue'] = 'not_object'
            return out, None
        expires = document.get('expiresAt')
        valid_expiry = (not isinstance(expires, bool) and isinstance(expires, (float, int))
                        and math.isfinite(expires))
        out.update(version=document.get('version'), validExpiry=valid_expiry)
        if valid_expiry:
            out.update(expiresAt=expires, fresh=expires > client._timestamp(),
                       expiryAgeSeconds=round(client._timestamp() - expires, 3))
        data = document.get('data')
        if not isinstance(data, dict):
            out['readIssue'] = 'missing_data_object'
            return out, None
        # Intentionally exclude general metadata, HTTP responses and source URLs.
        out.update(identityPolicy=data.get('identityPolicy'), matchStrategy=data.get('matchStrategy'))
        raw = data.get('metadata')
        if isinstance(raw, dict):
            out['storedMetadata'] = {k: raw.get(k) for k in
                                     ('tmdbId', 'matchStatus', 'originalTitle', 'releaseDate', 'rejectedIdentity')}
        diagnostic = data.get('matchDiagnostic')
        if isinstance(diagnostic, dict):
            out['storedMatchDiagnostic'] = {k: diagnostic.get(k) for k in
                                            ('reason', 'cacheHit', 'searchComplete', 'searchPages',
                                             'candidateCount', 'candidateIds', 'ignoredWithoutArtwork')}
        proof = data.get('identity')
        if isinstance(proof, dict):
            out['storedProof'] = {k: proof.get(k) for k in
                                  ('names', 'year', 'originalTitle', 'tmdbId', 'mediaKind',
                                   'posterPath', 'posterPolicy', 'undatedPosterPolicy', 'sourceYear',
                                   'contentId', 'evidenceDigest', 'evidenceFingerprint') if k in proof}
        return out, data
    except (OSError, ValueError, TypeError):
        out['readIssue'] = 'unreadable_or_invalid_json'
        return out, None


def validation_checks(module, data, item, kind, identifier, title, year, client):
    checks = {}
    if not isinstance(data, dict):
        return {'dataObject': False}
    checks['identityPolicyMatches'] = data.get('identityPolicy') == module.IDENTITY_POLICY
    metadata = module._cached_metadata(data.get('metadata'))
    checks['metadataShapeAccepted'] = metadata is not None
    if metadata is None:
        return checks
    correction = module._reviewed(item, kind)
    rejected_ids = correction.get('rejectedTmdbIds', []) if correction else []
    checks['notReviewedRejectedId'] = metadata.get('tmdbId') not in rejected_ids
    positive = metadata['matchStatus'].startswith('matched_')
    checks['positive'] = positive
    if not positive:
        checks['negativeStrategyMatches'] = data.get('matchStrategy') == module.MATCH_STRATEGY
        return checks
    proof = data.get('identity')
    checks['proofObject'] = isinstance(proof, dict)
    if not isinstance(proof, dict):
        return checks
    checks['titleAndYearAgree'] = module._identity_matches(proof, title, year)
    if 'undatedPosterPolicy' in proof and hasattr(module, '_undated_poster_identity_matches'):
        checks['undatedTitleAndPosterAgree'] = (
            data['metadata'].get('releaseDate') is None
            and metadata['matchStatus'] == 'matched_poster'
            and module._undated_poster_identity_matches(proof, item, kind, title, year))
    checks['proofIdMatchesMetadata'] = proof.get('tmdbId') == metadata.get('tmdbId')
    checks['suppliedIdAgrees'] = identifier is None or metadata.get('tmdbId') == identifier
    checks['proofOriginalTitleAgrees'] = proof.get('originalTitle') == module._normalized_title(metadata.get('originalTitle'))
    checks['proofYearMatchesMetadataDate'] = proof.get('year') == ((metadata.get('releaseDate') or '')[:4] or None)
    if metadata['matchStatus'] == 'matched_poster':
        poster = module.poster_identity(item)
        checks.update(posterPolicyMatches=proof.get('posterPolicy') == module.POSTER_POLICY,
                      posterPathMatches=proof.get('posterPath') == poster,
                      mediaKindMatches=proof.get('mediaKind') == kind,
                      noKnownPosterCollision=not client.poster_index.collided(poster))
    evidence = module._identity_evidence(item, kind)
    if evidence:
        checks.update(evidenceDigestMatches=proof.get('evidenceDigest') == evidence['digest'],
                      evidenceFingerprintMatches=proof.get('evidenceFingerprint') == evidence['fingerprint'],
                      evidenceContentMatches=proof.get('contentId') == evidence['contentId'])
    return checks


def inspect(module, item, kind, cache_dir, now):
    item = copy.deepcopy(item)
    evidence_collector = MovieEvidenceCollector(Path(cache_dir) / 'provider-evidence',
                                                max_requests=0, max_seconds=0, now=now)
    facts = evidence_collector.load(item) if kind == 'movie' else None
    if facts:
        item['_identityEvidence'] = facts
    evidence_collector.close()
    client = module.TmdbClient(Path(cache_dir) / 'tmdb', token='offline-diagnostic-no-real-token',
                               session=NoNetworkSession(), max_requests=0, now=now)
    suppressed_writes = []
    # Defense in depth: enrich may write a deterministic negative despite no HTTP.
    # Never alter either restored cache, including its expiry or diagnostic data.
    def suppress_write(key, _data, _ttl):
        suppressed_writes.append(hashlib.sha256(key.encode('utf-8')).hexdigest())
    client._write_cache = suppress_write
    key, identifier, title, year = module._enrichment_identity(item, kind)
    evidence = module._identity_evidence(item, kind)
    identity = {'keyHash': hashlib.sha256(key.encode('utf-8')).hexdigest(),
                'title': title, 'year': year, 'suppliedTmdbId': identifier,
                'posterPath': module.poster_identity(item),
                'hasBoundEvidence': evidence is not None,
                'privateEvidenceLoaded': facts is not None}
    if evidence:
        identity.update(evidenceDigest=evidence['digest'], evidenceFingerprint=evidence['fingerprint'])
    candidates = [('main', key)]
    if module.poster_identity(item):
        candidates.append(('withoutPosterSuffix', key.rsplit(f':poster-v{module.POSTER_POLICY}:', 1)[0]))
    raw_id = module._identifier(item.get('tmdbId') or item.get('tmdb_id') or item.get('tmdb'))
    legacy = f'{kind}:id:{raw_id}' if raw_id else f'{kind}:title:{module._normalized_title(title)}:year:{year or ""}'
    candidates.append(('legacy', legacy))
    documents = []
    for label, candidate_key in candidates:
        info, data = cache_document(client, candidate_key)
        info['keyRole'] = label
        info['checks'] = validation_checks(module, data, item, kind, identifier, title, year, client)
        info['failedChecks'] = [name for name, passed in info['checks'].items()
                                if passed is False and name != 'positive']
        documents.append(info)
    cached, fresh, rejected = client._identity_cache(key, item, kind, identifier, title, year)
    accepted = None if cached is None else {k: cached.get(k) for k in
                                           ('tmdbId', 'matchStatus', 'releaseDate', 'rejectedIdentity')}
    result = client.enrich(copy.deepcopy(item), kind)
    if client.stats['requests'] != 0:
        raise RuntimeError('Cache diagnostic attempted an HTTP request')
    output = {'inputIdentity': identity, 'cacheDocuments': documents,
              'acceptedCachedMetadata': accepted, 'acceptedCacheFresh': fresh,
              'identityRejected': rejected,
              'offlineEnrich': {k: result.get(k) for k in
                                ('tmdbId', 'matchStatus', 'releaseDate', 'rejectedIdentity')},
              'offlineDiagnostic': client.last_match_diagnostic,
              'offlineStats': client.stats,
              'suppressedWriteCount': len(suppressed_writes)}
    client.close()
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--before-cache', required=True)
    parser.add_argument('--after-cache', required=True)
    parser.add_argument('--previous-module', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    body = json.loads(Path(args.input).read_text(encoding='utf-8'))
    records = body.get('records') if isinstance(body, dict) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise RuntimeError('Expected 1 to 50 bounded input records')
    previous = load_previous(args.previous_module)
    now = time.time()
    report = {'schemaVersion': 1, 'generatedAtEpoch': now,
              'previousStrategy': previous.MATCH_STRATEGY, 'currentStrategy': current.MATCH_STRATEGY,
              'recordCount': len(records), 'httpRequests': 0, 'cacheWrites': 0, 'records': []}
    for row in records:
        item = copy.deepcopy(row['item'])
        before_item = copy.deepcopy(row.get('beforeItem', item))
        cid = row.get('contentId') or item.get('contentId')
        kind = row.get('kind') or cid.split(':')[1]
        if kind not in ('movie', 'series') or not isinstance(cid, str):
            raise RuntimeError('Invalid record identity')
        for source in (item, before_item):
            if source.get('contentId') != cid:
                raise RuntimeError('Unbound record input')
        report['records'].append({
            'contentId': cid, 'kind': kind,
            'beforeCachePreviousCodeBeforeInput': inspect(previous, before_item, kind, args.before_cache, now),
            'beforeCacheCurrentCodeBeforeInput': inspect(current, before_item, kind, args.before_cache, now),
            'beforeCacheCurrentCodeCurrentInput': inspect(current, item, kind, args.before_cache, now),
            'afterCacheCurrentCodeCurrentInput': inspect(current, item, kind, args.after_cache, now),
        })
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'records': len(records), 'httpRequests': 0, 'cacheWrites': 0,
                      'reportFile': output.name}, ensure_ascii=True))


if __name__ == '__main__':
    main()
