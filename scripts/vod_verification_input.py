"""Freeze validated provider catalog inputs for an isolated TMDB verification run.

The normalized VOD catalogs supply matcher inputs. Enriched app identities and
artwork are baseline observations only; using them as provider hints would make
this test verify its own previous output. This module performs no network calls
and never writes to a publication directory.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path

import build_vod_catalogs as vod
import publication_snapshots as snapshots
from vod_reviewed_corrections import apply_reviewed_correction
from vod_tmdb import IDENTITY_POLICY, MATCH_STRATEGY, UNDATED_POSTER_POLICY, UNMATCHED_PASSTHROUGH_POLICY


SERVERS = ("server_1", "server_2", "server_3")
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_ROWS = 250_000
ARTIFACT = re.compile(r"[A-Za-z0-9_-]+\.[0-9a-f]{16}\.json(?:\.gz)?\Z")
BASELINE_FIELDS = ("tmdbId", "metadataMatch", "title", "releaseDate", "originalTitle",
                   "originalLanguage", "metadataEvidence")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Catalog generation is missing.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("Catalog generation is not canonical UTC.") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("Catalog generation is not canonical UTC.")
    return value


def _json(path, *, expected_bytes=None, expected_sha=None, uncompressed_bytes=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 <= path.stat().st_size <= MAX_JSON_BYTES:
        raise ValueError("Verification input is missing, linked or oversized.")
    raw = path.read_bytes()
    if expected_bytes is not None and (type(expected_bytes) is not int or len(raw) != expected_bytes):
        raise ValueError("Verification input byte length does not match.")
    if expected_sha is not None and (not isinstance(expected_sha, str)
            or not snapshots.SHA256.fullmatch(expected_sha)
            or hashlib.sha256(raw).hexdigest() != expected_sha):
        raise ValueError("Verification input SHA-256 does not match.")
    if path.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Decoded verification input exceeds its size bound.")
    if uncompressed_bytes is not None and (type(uncompressed_bytes) is not int
            or len(raw) != uncompressed_bytes):
        raise ValueError("Decoded verification input byte length does not match.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Verification input must contain an object.")
    return value


def _reference(root, reference):
    if (not isinstance(reference, dict) or not isinstance(reference.get("file"), str)
            or not ARTIFACT.fullmatch(reference["file"])):
        raise ValueError("Catalog reference is unsafe.")
    sha = reference.get("sha256")
    if (not isinstance(sha, str) or not snapshots.SHA256.fullmatch(sha)
            or reference["file"].split(".")[1] != sha[:16]
            or type(reference.get("compressedBytes")) is not int
            or not 0 < reference["compressedBytes"] <= MAX_JSON_BYTES):
        raise ValueError("Catalog reference is not content addressed.")
    return _json(root / reference["file"], expected_bytes=reference.get("compressedBytes"),
                 expected_sha=sha, uncompressed_bytes=reference.get("uncompressedBytes"))


def _servers(index):
    rows = index.get("servers")
    if (index.get("schemaVersion") != 1 or index.get("serverCount") != len(SERVERS)
            or not isinstance(rows, list) or len(rows) != len(SERVERS)
            or any(not isinstance(row, dict) for row in rows)
            or {row.get("serverId") for row in rows} != set(SERVERS)):
        raise ValueError("Verification must include all three servers exactly once.")
    _timestamp(index.get("generatedAt"))
    return {row["serverId"]: row for row in rows}


def _identity(payload, *, server_id, generated_at, key, value):
    if (payload.get("schemaVersion") != 1 or payload.get("serverId") != server_id
            or payload.get("generatedAt") != generated_at or payload.get(key) != value):
        raise ValueError("Catalog identity or generation does not match.")


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_ROWS:
        raise ValueError("Catalog record count is invalid.")
    return value


def load_frozen_input(catalog_dir: Path, source_vod_dir: Path):
    """Load the complete app baseline plus normalized provider input trust chains.

    ``catalog_dir`` contains the app index and its referenced manifests/shards;
    ``source_vod_dir`` contains the normalized VOD index/manifests/catalogs from
    the same frozen component. Their generations can differ because source
    normalization precedes app enrichment, but source cannot postdate the app.
    """
    catalog_dir = snapshots._directory(catalog_dir, required=True)
    source_vod_dir = snapshots._directory(source_vod_dir, required=True)
    app_index = _json(catalog_dir / "index.json")
    source_index = _json(source_vod_dir / "index.json")
    app_servers, source_servers = _servers(app_index), _servers(source_index)
    app_time, source_time = app_index["generatedAt"], source_index["generatedAt"]
    if source_time > app_time:
        raise ValueError("Provider source generation postdates the app baseline.")
    # Reuse the production normalized-source schema, completeness, count,
    # reviewed-correction, release-ID and content-hash validation unchanged.
    try:
        vod.validate_publication(source_vod_dir, expected_servers=SERVERS)
    except vod.CatalogBuildError as exc:
        raise ValueError(str(exc)) from exc
    records, counts, input_hashes = [], [], []
    for server_id in SERVERS:
        app_server, source_server = app_servers[server_id], source_servers[server_id]
        source_manifest = _json(source_vod_dir / source_server["manifestFile"],
            expected_bytes=source_server["manifestBytes"], expected_sha=source_server["manifestSha256"])
        summary = {"serverId": server_id}
        for plural, kind in (("movies", "movie"), ("series", "series")):
            expected_count = _count(app_server.get("movieCount" if kind == "movie" else "seriesCount"))
            source_ref = source_manifest["catalogs"][plural]
            source_payload = _reference(source_vod_dir, source_ref)
            _identity(source_payload, server_id=server_id, generated_at=source_time,
                      key="contentType", value=kind)
            source_rows = source_payload["items"]
            if expected_count != len(source_rows) or expected_count != source_ref["itemCount"]:
                raise ValueError("Provider and app baseline counts differ.")
            source_by_id = {row["id"]: row for row in source_rows}
            if len(source_by_id) != len(source_rows):
                raise ValueError("Provider catalog contains duplicate IDs.")
            app_ref = app_server[plural]
            manifest = _reference(catalog_dir, app_ref)
            _identity(manifest, server_id=server_id, generated_at=app_time,
                      key="dataset", value="vod-" + plural)
            shards = manifest.get("shards")
            if (not isinstance(shards, list) or len(shards) > 20_000
                    or app_ref.get("recordCount") != len(shards)
                    or _count(manifest.get("totalRecords")) != expected_count):
                raise ValueError("App manifest counts do not match its index.")
            input_hashes.extend([source_ref["sha256"], app_ref["sha256"]])
            seen_ids, seen_shards = set(), set()
            for shard_ref in shards:
                if shard_ref.get("file") in seen_shards:
                    raise ValueError("App manifest contains duplicate shards.")
                seen_shards.add(shard_ref.get("file"))
                payload = _reference(catalog_dir, shard_ref)
                _identity(payload, server_id=server_id, generated_at=app_time,
                          key="dataset", value="vod-" + plural)
                rows = payload.get("items")
                if (not isinstance(rows, list) or len(rows) != _count(payload.get("recordCount"))
                        or len(rows) != _count(shard_ref.get("recordCount"))):
                    raise ValueError("App shard counts do not match its manifest.")
                input_hashes.append(shard_ref["sha256"])
                for app in rows:
                    if not isinstance(app, dict):
                        raise ValueError("App catalog contains an invalid record.")
                    record_id = app.get("id")
                    if not isinstance(record_id, str) or record_id not in source_by_id or record_id in seen_ids:
                        raise ValueError("App IDs are duplicated or differ from provider source.")
                    seen_ids.add(record_id)
                    content_id = f"{server_id}:{kind}:{record_id}"
                    if (app.get("contentId") != content_id
                            or app.get("providerStreamId" if kind == "movie" else "providerSeriesId") != record_id):
                        raise ValueError("App record provider identity does not match.")
                    languages = app.get("languages")
                    if (not isinstance(languages, list) or not languages
                            or any(not isinstance(code, str) or not re.fullmatch(r"[a-z]{2,3}", code)
                                   for code in languages)):
                        raise ValueError("App record languages are invalid.")
                    item = apply_reviewed_correction(server_id, kind, source_by_id[record_id])
                    item.update(contentId=content_id, providerLastModified=app.get("providerLastModified"))
                    item["providerStreamId" if kind == "movie" else "providerSeriesId"] = record_id
                    records.append({"contentId": content_id, "kind": kind, "item": item,
                        "baseline": {key: copy.deepcopy(app.get(key)) for key in BASELINE_FIELDS},
                        "serverId": server_id, "languages": list(languages)})
            if seen_ids != set(source_by_id):
                raise ValueError("App shards do not cover the full provider catalog.")
            summary["movieCount" if kind == "movie" else "seriesCount"] = expected_count
        counts.append(summary)
    policies = {"inputSchema": 1, "identityPolicy": IDENTITY_POLICY, "matchStrategy": MATCH_STRATEGY,
                "undatedPosterPolicy": UNDATED_POSTER_POLICY,
                "unmatchedPassthroughPolicy": UNMATCHED_PASSTHROUGH_POLICY,
                "reviewedCorrectionsSha256": hashlib.sha256((Path(__file__).resolve().parents[1]
                    / "data/vod_reviewed_corrections.json").read_bytes()).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps({"policies": policies,
        "appGeneratedAt": app_time, "sourceGeneratedAt": source_time,
        "appIndexSha256": hashlib.sha256((catalog_dir / "index.json").read_bytes()).hexdigest(),
        "sourceIndexSha256": hashlib.sha256((source_vod_dir / "index.json").read_bytes()).hexdigest(),
        "references": input_hashes}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schemaVersion": 1, "generatedAt": app_time, "sourceGeneratedAt": source_time,
            "inputFingerprint": fingerprint, "matchingPolicies": policies,
            "servers": counts, "recordCount": len(records),
            "movieCount": sum(row["movieCount"] for row in counts),
            "seriesCount": sum(row["seriesCount"] for row in counts), "records": records}


def load_component_input(component_dir: Path):
    """Validate every file in a frozen VOD component, then return test inputs."""
    component_dir, manifest = snapshots._snapshot(component_dir, "vod")
    for entry in manifest["files"]:
        path = component_dir / "files" / entry["path"]
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise ValueError("Frozen component file checksum or size does not match.")
    result = load_frozen_input(component_dir / "files/skytvplayer", component_dir / "files/VOD")
    result["componentSha256"] = hashlib.sha256((component_dir / "snapshot.json").read_bytes()).hexdigest()
    return result
