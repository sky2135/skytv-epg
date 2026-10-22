#!/usr/bin/env python3
"""Audit identity coverage in an existing app catalog without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import build_skytvplayer_catalog as app
import build_vod_catalogs as vod
from vod_reviewed_corrections import correction_for
from vod_tmdb import MATCH_STRATEGY
from vod_poster_index import POSTER_POLICY

LANGUAGES = ("en", "hi", "pa")
VERIFIED_MATCH_STATUSES = frozenset({"matched_id", "matched_search", "matched_alias", "matched_poster"})
MAX_FILES = 20_000
MAX_RECORDS = 1_000_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
STATUS = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
IMDB = re.compile(r"tt[0-9]{7,10}\Z")


def _directory(path, *, required=False):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise vod.CatalogBuildError("Identity audit paths cannot use symbolic links or junctions.")
    if required and not path.is_dir():
        raise vod.CatalogBuildError("The identity audit catalog directory is missing.")
    return path


def _integer(value, label, maximum=MAX_RECORDS):
    if type(value) is not int or not 0 <= value <= maximum:
        raise vod.CatalogBuildError(f"Identity audit {label} is invalid.")
    return value


def _bucket():
    return {"total": 0, "verifiedTmdb": 0, "verifiedImdb": 0, "verifiedEither": 0,
            "missingBoth": 0, "tmdbFieldPresent": 0, "unverifiedTmdbFieldPresent": 0,
            "imdbFieldPresent": 0}


def _add(bucket, *, tmdb_verified, tmdb_present, imdb_present):
    bucket["total"] += 1
    bucket["verifiedTmdb"] += tmdb_verified
    bucket["verifiedEither"] += tmdb_verified
    bucket["missingBoth"] += not tmdb_verified
    bucket["tmdbFieldPresent"] += tmdb_present
    bucket["unverifiedTmdbFieldPresent"] += tmdb_present and not tmdb_verified
    bucket["imdbFieldPresent"] += imdb_present
    # No current app-schema property proves an IMDb identity. A provider field
    # or a generic "verified" flag must never manufacture that evidence.


def _verified_tmdb(server_id, kind, item, identifier):
    if identifier is None:
        return False
    status = item["metadataMatch"]
    if status in VERIFIED_MATCH_STATUSES:
        return True
    if status != "reviewed_correction":
        return False
    correction = correction_for(server_id, kind, item)
    evidence = item.get("metadataEvidence")
    return bool(correction and isinstance(evidence, dict)
                and correction["metadata"].get("tmdbId") == identifier
                and evidence.get("method") == "reviewed_correction"
                and evidence.get("sourceURLs") == correction["evidenceURLs"])


def _imdb_present(item):
    sources = [item]
    for name in ("externalIds", "external_ids"):
        if isinstance(item.get(name), dict):
            sources.append(item[name])
    return any(isinstance(source.get(key), str) and IMDB.fullmatch(source[key])
               for source in sources for key in ("imdbId", "imdb_id", "imdb"))


def _languages(item):
    primary, labels = item.get("language"), item.get("languages")
    if (not isinstance(primary, str) or primary not in vod.LANGUAGE_CODES
            or not isinstance(labels, list) or not labels or len(labels) > len(vod.LANGUAGE_CODES)
            or any(not isinstance(code, str) or code not in vod.LANGUAGE_CODES for code in labels)
            or len(labels) != len(set(labels))):
        raise vod.CatalogBuildError("Identity audit language labels are invalid.")
    if ((primary == "und" and labels != ["und"])
            or (primary not in {"mul", "und"} and labels != [primary])
            or (primary == "mul" and (len(labels) < 2 or any(code in {"und", "mul"} for code in labels)))):
        raise vod.CatalogBuildError("Identity audit language labels are inconsistent.")
    return primary, frozenset(labels)


def audit(catalog_dir, *, expected_servers=vod.DEFAULT_SERVERS):
    """Verify movie/series artifacts and return aggregate facts, never titles.

    Home and episode inventories are deliberately outside this audit. Their
    normal publication validator remains responsible for their own contracts.
    """
    root = _directory(catalog_dir, required=True)
    index_path = root / "index.json"
    if index_path.is_symlink() or not index_path.is_file() or index_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise vod.CatalogBuildError("Identity audit index is missing or oversized.")
    index_raw = index_path.read_bytes()
    index = vod._strict_json_from_bytes(index_raw, "Identity audit index")
    if (not isinstance(index, dict) or type(index.get("schemaVersion")) is not int
            or index["schemaVersion"] != 1 or index.get("appName") != "SkyTVPlayer"
            or not isinstance(index.get("generatedAt"), str)
            or vod.generated_timestamp(index["generatedAt"]) != index["generatedAt"]
            or not isinstance(index.get("servers"), list)
            or type(index.get("serverCount")) is not int
            or index["serverCount"] != len(index["servers"])):
        raise vod.CatalogBuildError("Identity audit index identity is invalid.")
    servers = index["servers"]
    expected = tuple(expected_servers)
    server_ids = [row.get("serverId") if isinstance(row, dict) else None for row in servers]
    if (not expected or len(expected) != len(set(expected)) or not all(isinstance(value, str) for value in server_ids)
            or sorted(server_ids) != sorted(expected)):
        raise vod.CatalogBuildError("Identity audit server set is incomplete or duplicated.")
    for identifier in server_ids:
        vod.normalize_server_id(identifier)

    report = {"schemaVersion": 1, "generatedAt": index["generatedAt"], "matcherStrategyVersion": MATCH_STRATEGY,
              "posterPolicyVersion": POSTER_POLICY,
              "languages": list(LANGUAGES), "serverCount": len(servers), "totalRecords": 0,
              "verifiedDatasetManifests": 0, "verifiedShards": 0,
              "allRecords": {}, "perLanguage": {}, "selectedLanguageUnion": {},
              "unknownLanguage": {}, "multilingual": {}, "metadataMatchCounts": {},
              "selectedLanguageUnionTotal": _bucket(), "allRecordsTotal": _bucket(),
              "verificationPolicy": {
                  "tmdb": "A numeric TMDB ID with matched_id, matched_search, matched_alias or matched_poster status, or the exact allowlisted reviewed ID and evidence.",
                  "imdb": "The current public schema has no independent IMDb verification proof; provider hints are unverified.",
                  "language": "Use published languages[] only. Per-language groups overlap; each union counts a contentId once.",
                  "originalLanguage": "Work origin is not evidence of the provider stream's audio language."}}
    statuses = Counter({"matched_poster": 0})
    seen_ids, seen_files, source_identity = set(), set(), []
    total_bytes = 0

    def read(reference, *, manifest):
        nonlocal total_bytes
        if not isinstance(reference, dict):
            raise vod.CatalogBuildError("Identity audit artifact reference is invalid.")
        name = reference.get("file")
        if not isinstance(name, str) or name in seen_files:
            raise vod.CatalogBuildError("Identity audit artifact is duplicated or invalid.")
        if len(seen_files) >= MAX_FILES:
            raise vod.CatalogBuildError("Identity audit has too many artifacts.")
        count = _integer(reference.get("recordCount"), "artifact count", MAX_FILES if manifest else app.MAX_SHARD_RECORDS)
        compressed = _integer(reference.get("compressedBytes"), "artifact bytes", MAX_MANIFEST_BYTES if manifest else app.MAX_SHARD_BYTES)
        _integer(reference.get("uncompressedBytes"), "expanded bytes", app.MAX_JSON_BYTES)
        if compressed == 0:
            raise vod.CatalogBuildError("Identity audit artifact is empty.")
        total_bytes += compressed
        if total_bytes > MAX_TOTAL_BYTES:
            raise vod.CatalogBuildError("Identity audit exceeds its total size limit.")
        seen_files.add(name)
        return app.read_artifact(root, reference), count

    for server in servers:
        server_id = server["serverId"]
        if any(key not in server for key in ("movieCount", "seriesCount", "movies", "series")):
            raise vod.CatalogBuildError("Identity audit server dataset references are missing.")
        source_identity.append({key: server[key] for key in ("serverId", "movieCount", "seriesCount", "movies", "series")})
        for kind, plural, count_field in (("movie", "movies", "movieCount"), ("series", "series", "seriesCount")):
            group = f"{server_id}/{kind}"
            expected_count = _integer(server.get(count_field), "dataset total")
            manifest, shard_count = read(server.get(plural), manifest=True)
            dataset = f"vod-{plural}"
            if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1
                    or manifest.get("serverId") != server_id or manifest.get("dataset") != dataset
                    or manifest.get("generatedAt") != index["generatedAt"]
                    or _integer(manifest.get("totalRecords"), "manifest total") != expected_count
                    or not isinstance(manifest.get("shards"), list) or len(manifest["shards"]) != shard_count):
                raise vod.CatalogBuildError("Identity audit dataset identity does not match the index.")
            report["verifiedDatasetManifests"] += 1
            for collection in ("allRecords", "selectedLanguageUnion", "unknownLanguage", "multilingual"):
                report[collection][group] = _bucket()
            for language in LANGUAGES:
                report["perLanguage"][group + "/" + language] = _bucket()
            group_statuses = Counter({"matched_poster": 0})
            for reference in manifest["shards"]:
                shard, record_count = read(reference, manifest=False)
                if (type(shard.get("schemaVersion")) is not int or shard["schemaVersion"] != 1
                        or shard.get("serverId") != server_id or shard.get("dataset") != dataset
                        or shard.get("generatedAt") != index["generatedAt"]
                        or not isinstance(shard.get("items"), list) or len(shard["items"]) != record_count
                        or _integer(shard.get("recordCount"), "shard total", app.MAX_SHARD_RECORDS) != record_count):
                    raise vod.CatalogBuildError("Identity audit shard identity does not match its dataset.")
                report["verifiedShards"] += 1
                for item in shard["items"]:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        raise vod.CatalogBuildError("Identity audit record identity is invalid.")
                    content_id = item.get("contentId")
                    if content_id != f"{server_id}:{kind}:{item['id']}" or content_id in seen_ids:
                        raise vod.CatalogBuildError("Identity audit record identity is duplicated or unscoped.")
                    seen_ids.add(content_id)
                    if len(seen_ids) > MAX_RECORDS:
                        raise vod.CatalogBuildError("Identity audit has too many records.")
                    status = item.get("metadataMatch")
                    if not isinstance(status, str) or not STATUS.fullmatch(status):
                        raise vod.CatalogBuildError("Identity audit match status is invalid.")
                    group_statuses[status] += 1
                    statuses[status] += 1
                    primary, labels = _languages(item)
                    identifier = vod.normalize_tmdb_id([item])
                    facts = {"tmdb_verified": _verified_tmdb(server_id, kind, item, identifier),
                             "tmdb_present": identifier is not None, "imdb_present": bool(_imdb_present(item))}
                    _add(report["allRecords"][group], **facts)
                    _add(report["allRecordsTotal"], **facts)
                    if primary == "und":
                        _add(report["unknownLanguage"][group], **facts)
                    elif primary == "mul":
                        _add(report["multilingual"][group], **facts)
                    selected = labels.intersection(LANGUAGES)
                    if selected:
                        _add(report["selectedLanguageUnion"][group], **facts)
                        _add(report["selectedLanguageUnionTotal"], **facts)
                        for language in selected:
                            _add(report["perLanguage"][group + "/" + language], **facts)
            if report["allRecords"][group]["total"] != expected_count:
                raise vod.CatalogBuildError("Identity audit dataset count does not match the index.")
            report["metadataMatchCounts"][group] = dict(sorted(group_statuses.items()))
    if index_path.read_bytes() != index_raw:
        raise vod.CatalogBuildError("The app catalog index changed during its identity audit.")
    report["totalRecords"] = len(seen_ids)
    report["metadataMatchCountsTotal"] = dict(sorted(statuses.items()))
    report["verifiedArtifactBytes"] = total_bytes
    # Composition refreshes live EPG links in index.json. Bind only the VOD
    # identity so this report remains accurate after that independent refresh.
    report["catalogIdentitySha256"] = hashlib.sha256(app.json_bytes({
        "generatedAt": index["generatedAt"], "servers": sorted(source_identity, key=lambda row: row["serverId"])})).hexdigest()
    return report


def write_report(catalog_dir, output=None, *, expected_servers=vod.DEFAULT_SERVERS):
    root = _directory(catalog_dir, required=True)
    output = Path(output) if output is not None else root / "identity_coverage.json"
    _directory(output.parent)
    if (output.is_symlink() or output.resolve() == (root / "index.json").resolve()
            or app.ARTIFACT_NAME.fullmatch(output.name)):
        raise vod.CatalogBuildError("Identity coverage report must not replace an app artifact.")
    report = audit(root, expected_servers=expected_servers)
    app.atomic_json(output, report)
    return report


def github_summary(report, path):
    lines = ["### Published VOD identity coverage", "", f"Catalog generation: `{report['generatedAt']}`", "",
             "| Server / kind | English/Hindi/Punjabi union | Verified TMDB | Missing both verified IDs |",
             "| --- | ---: | ---: | ---: |"]
    for group, values in report["selectedLanguageUnion"].items():
        lines.append(f"| {group} | {values['total']:,} | {values['verifiedTmdb']:,} | {values['missingBoth']:,} |")
    lines += ["", "Language groups use published audio labels; unknown languages remain outside that union.",
              "Provider ID hints are not verified identities. No IMDb proof is currently exported.", ""]
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=Path("public/skytvplayer"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-server", action="append")
    parser.add_argument("--github-summary", action="store_true")
    args = parser.parse_args(argv)
    report = write_report(args.catalog_dir, args.output, expected_servers=args.expected_server or vod.DEFAULT_SERVERS)
    if args.github_summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        github_summary(report, os.environ["GITHUB_STEP_SUMMARY"])
    print(json.dumps({key: report[key] for key in ("generatedAt", "matcherStrategyVersion", "posterPolicyVersion", "totalRecords",
                                                  "selectedLanguageUnionTotal", "metadataMatchCountsTotal")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (vod.CatalogBuildError, OSError, ValueError, KeyError, TypeError):
        print("VOD identity coverage failed validation; its previous report was preserved.", file=sys.stderr)
        raise SystemExit(2) from None
