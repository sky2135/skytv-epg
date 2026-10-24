"""Read-only, credential-free inspection of two existing production caches."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import vod_tmdb as tmdb
from vod_provider_evidence import MovieEvidenceCollector


def inspect(root, records):
    rows = []
    collector = MovieEvidenceCollector(Path(root) / 'provider-evidence', max_requests=0, max_seconds=0)
    client = tmdb.TmdbClient(Path(root) / 'tmdb', max_requests=0)
    try:
        for original in records:
            item = dict(original)
            evidence = collector.load(item)
            if evidence:
                item['_identityEvidence'] = evidence
            key, identifier, title, year = tmdb._enrichment_identity(item, 'movie')
            path = client._cache_path(key)
            raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            data = raw.get('data', {}) if isinstance(raw, dict) else {}
            cached, fresh, rejected = client._identity_cache(key, item, 'movie', identifier, title, year)
            rows.append({
                'contentId': item['contentId'],
                'sourceTitle': item['title'],
                'sourceTmdbHint': identifier,
                'parsedTitle': title,
                'parsedYear': year,
                'providerEvidencePresent': bool(evidence),
                'physicalCacheKeyHash': hashlib.sha256(key.encode()).hexdigest(),
                'exactEntryPresent': raw is not None,
                'exactEntrySha256': hashlib.sha256(path.read_bytes()).hexdigest() if raw else None,
                'storedStatus': (data.get('metadata') or {}).get('matchStatus'),
                'storedReason': (data.get('matchDiagnostic') or {}).get('reason'),
                'expiresAt': raw.get('expiresAt') if raw else None,
                'validatedCachedStatus': cached.get('matchStatus') if cached else None,
                'validatedCacheFresh': fresh,
                'validatedRejection': rejected,
            })
        if client.stats['requests'] != 0:
            raise ValueError('Read-only inspection performed requests')
        return {'entries': rows, 'requests': client.stats['requests'], 'cacheWriteErrors': client.stats['cacheWriteErrors']}
    finally:
        collector.close()
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = json.loads(args.input.read_text(encoding='utf-8'))
    if {row.get('contentId') for row in records} != {'server_3:movie:954024', 'server_3:movie:955380'} or len(records) != 2:
        raise ValueError('Unexpected diagnostic scope')
    report = {'schemaVersion': 1, 'productionWritten': False, 'networkLookups': 0,
              'before': inspect(args.before, records), 'after': inspect(args.after, records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
