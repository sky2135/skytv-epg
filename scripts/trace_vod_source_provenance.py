#!/usr/bin/env python3
"""Trace a small movie sample without retaining provider URLs or raw rows.

Run against the private input bundle from the same build. This does not call
TMDB, write caches, or publish a catalog. An optional live mode makes only the
two inventory/category requests needed to reproduce source normalization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

import build_skytvplayer_catalog as app
import build_vod_catalogs as vod
from vod_reviewed_corrections import apply_reviewed_correction

DEFAULT_IDS = ("1194029", "1194031", "954024")
TMDB_FIELDS = ("tmdb_id", "tmdbId", "tmdb")
POSTER_FIELDS = ("poster_path", "poster", "cover_big", "cover", "movie_image", "stream_icon")
MAX_SAMPLE = 10


def digest(value: Any) -> str:
    return hashlib.sha256(app.json_bytes(value)).hexdigest()


def _type(value: Any) -> str:
    return ("null" if value is None else "boolean" if isinstance(value, bool)
            else "object" if isinstance(value, Mapping) else "array" if isinstance(value, list)
            else "string" if isinstance(value, str) else "number" if isinstance(value, (int, float))
            else "unsupported")


def _sources(row, detail):
    result = [("inventory", row), ("detail", detail)]
    for name in ("movie_data", "series_data", "info", "metadata"):
        if isinstance(detail.get(name), Mapping):
            result.append(("detail." + name, detail[name]))
    return result


def _poster_reason(value, normalized, blocked):
    if normalized:
        return "accepted"
    text = vod._artwork_candidate(value)
    if not text:
        return "empty"
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").encode("idna").decode("ascii").casefold()
        if blocked and blocked.casefold().strip() in host:
            return "provider_hostname_blocked"
    except (ValueError, UnicodeError):
        return "invalid_url"
    return "invalid_or_disallowed_url"


def candidate_trace(row, detail, *, kind, blocked=""):
    """Expose precedence and validation decisions, never candidate text/URLs."""
    fields = TMDB_FIELDS if kind == "tmdb" else POSTER_FIELDS
    output, selected = [], False
    for source_name, source in reversed(_sources(row, detail)):
        for field in fields:
            present = field in source
            value = source.get(field)
            nonempty = present and value not in (None, "", [], {})
            entry = {"path": source_name + "." + field, "present": present,
                     "jsonType": _type(value) if present else "absent",
                     "valueSha256": digest(value) if present else None,
                     "selected": bool(nonempty and not selected)}
            selected = selected or nonempty
            if kind == "tmdb":
                normalized = vod.normalize_tmdb_id([{"tmdb_id": value}])
                entry.update(valid=normalized is not None, normalizedId=normalized,
                             reason="accepted" if normalized else "empty" if not nonempty else "invalid_id")
            else:
                try:
                    normalized = vod.normalize_artwork_url(value, tmdb_size="w500", blocked_hostname=blocked)
                except (ValueError, UnicodeError):
                    normalized = None
                entry.update(valid=normalized is not None,
                             reason=_poster_reason(value, normalized, blocked),
                             normalizedSha256=digest(normalized) if normalized else None)
            output.append(entry)
    chosen = next((entry for entry in output if entry["selected"]), None)
    return {"candidates": output, "selectedPath": chosen["path"] if chosen else None,
            "usableCandidatePresent": any(entry["valid"] for entry in output),
            "selectedCandidateValid": bool(chosen and chosen["valid"]),
            "invalidSelectionMasksValidCandidate": bool(chosen and not chosen["valid"]
                                                       and any(entry["valid"] for entry in output))}


def stage_facts(item):
    if item is None:
        return {"present": False}
    artwork = item.get("artwork") or {}
    result = {"present": True, "rowSha256": digest(item),
              "tmdbId": vod.normalize_tmdb_id([item]),
              "posterPresent": bool(artwork.get("posterUrl")),
              "posterSha256": digest(artwork["posterUrl"]) if artwork.get("posterUrl") else None,
              "titleSha256": digest(item.get("title")),
              "providerLastModifiedSha256": digest(item.get("providerLastModified"))}
    status = item.get("metadataMatch")
    if isinstance(status, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", status):
        result["metadataMatch"] = status
    return result


def published_rows(root: Path, server_id: str, wanted: set[str]):
    """Read checksum-bound local app artifacts; never fetch URLs."""
    index_path = root / "index.json"
    if index_path.is_symlink():
        raise vod.CatalogBuildError("Provenance app index cannot be a symbolic link.")
    index = vod._read_bounded_json(index_path, vod.MAX_INDEX_BYTES, "provenance app index")
    servers = [s for s in index.get("servers", []) if s.get("serverId") == server_id]
    if len(servers) != 1:
        raise vod.CatalogBuildError("Provenance app server is missing or duplicated.")
    reference = servers[0]["movies"]
    manifest = app.read_artifact(root, reference)
    if (manifest.get("serverId") != server_id or manifest.get("dataset") != "vod-movies"
            or manifest.get("generatedAt") != index.get("generatedAt")):
        raise vod.CatalogBuildError("Provenance app generation does not match.")
    found = {}
    for reference in manifest.get("shards", []):
        shard = app.read_artifact(root, reference)
        if (shard.get("serverId") != server_id or shard.get("dataset") != "vod-movies"
                or shard.get("generatedAt") != index["generatedAt"]):
            raise vod.CatalogBuildError("Provenance shard generation does not match.")
        for row in shard.get("items", []):
            identifier = str(row.get("id", ""))
            if identifier in wanted:
                if identifier in found or row.get("contentId") != f"{server_id}:movie:{identifier}":
                    raise vod.CatalogBuildError("Provenance app movie identity is invalid.")
                found[identifier] = row
    return found, {"generatedAt": index["generatedAt"], "manifestSha256": servers[0]["movies"]["sha256"]}


def trace(source, server_id, movie_ids, *, config=None, bundle_sha256=None,
          app_catalog_dir=None, run_id="", code_sha=""):
    server_id = vod.normalize_server_id(server_id)
    movie_ids = tuple(dict.fromkeys(str(value) for value in movie_ids))
    if (not movie_ids or len(movie_ids) > MAX_SAMPLE
            or any(not re.fullmatch(r"[0-9]{1,12}", value) for value in movie_ids)):
        raise vod.CatalogBuildError("Provenance sample must contain one to ten numeric movie IDs.")
    rows = vod.normalize_rows(source["get_vod_streams"], "provenance inventory")
    selected = [row for row in rows if next((vod.normalize_item_id(row.get(key))
                for key in ("stream_id", "vod_id", "id") if vod.normalize_item_id(row.get(key))), "") in movie_ids]
    details = vod.normalize_detail_map(source.get("vod_details", {}), kind="movie", label="provenance details")
    subset = dict(source, get_vod_streams=selected,
                  vod_details={key: value for key, value in details.items() if key in movie_ids})
    blocked = (urlsplit(config.base_url).hostname or "") if config else ""
    normalized, _ = vod.normalize_catalog(selected, source.get("get_vod_categories", []), subset["vod_details"],
                                         kind="movie", label="provenance movies", blocked_artwork_hostname=blocked)
    normalized = {item["id"]: item for item in normalized}
    prepared = app._prepare_catalog(server_id, "movie", subset, config)
    prepared_by_id = {item["id"]: item for item in prepared["normalized"]}
    raw_by_id = {next(vod.normalize_item_id(row.get(key)) for key in ("stream_id", "vod_id", "id")
                      if vod.normalize_item_id(row.get(key))): row for row in selected}
    public, public_identity = ({}, None) if app_catalog_dir is None else published_rows(Path(app_catalog_dir), server_id, set(movie_ids))
    records = []
    for identifier in movie_ids:
        raw = raw_by_id.get(identifier)
        matching_raw = prepared["raw_rows"].get(identifier, {})
        item = normalized.get(identifier)
        corrected = apply_reviewed_correction(server_id, "movie", item) if item else None
        matching = app._matching_item(prepared_by_id[identifier], matching_raw, "movie", None) if identifier in prepared_by_id else None
        record = {"providerId": identifier, "contentId": f"{server_id}:movie:{identifier}",
                  "inventoryPresent": raw is not None,
                  "rawMatchingRowPresent": identifier in prepared["raw_rows"],
                  "rawRowSha256": digest(raw) if raw is not None else None,
                  "detailPresent": bool(details.get(identifier)),
                  "stages": {"normalized": stage_facts(item), "correctedSource": stage_facts(corrected),
                             "preparedInput": stage_facts(prepared_by_id.get(identifier)),
                             "matchingInput": stage_facts(matching)}}
        record["matchingIdentitySha256"] = digest(app._metadata_result_key(matching, "movie", None)) if matching else None
        if raw is not None:
            record["tmdb"] = candidate_trace(raw, details.get(identifier, {}), kind="tmdb", blocked=blocked)
            record["poster"] = candidate_trace(raw, details.get(identifier, {}), kind="poster", blocked=blocked)
        if app_catalog_dir is not None:
            record["stages"]["published"] = stage_facts(public.get(identifier))
        records.append(record)
    return {"schemaVersion": 1, "serverId": server_id,
            "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "runId": str(run_id) if re.fullmatch(r"[0-9]{1,20}(?:-[0-9]{1,4})?", str(run_id)) else None,
            "codeSha": code_sha if re.fullmatch(r"[0-9a-f]{40,64}", code_sha) else None,
            "rawBundleSha256": bundle_sha256 or digest(source),
            "providerArtworkHostCheckApplied": bool(config), "privateEvidenceLoaded": False,
            "productionWritten": False, "tmdbRequests": 0,
            "publishedGeneration": public_identity, "records": records,
            "limitations": ["This captures this input only; the unretained Oct24 raw provider response cannot be reconstructed.",
                            "Provider TMDB hints are unverified; the app intentionally publishes only verified identities.",
                            "Private provider-detail evidence caches are not loaded by this source-only trace."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    origin = parser.add_mutually_exclusive_group(required=True)
    origin.add_argument("--input-dir", type=Path)
    origin.add_argument("--live", action="store_true")
    parser.add_argument("--server", default="server_3")
    parser.add_argument("--movie-id", action="append")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--app-catalog-dir", type=Path)
    parser.add_argument("--load-server-config", action="store_true")
    parser.add_argument("--allow-insecure-http", action="store_true")
    args = parser.parse_args(argv)
    try:
        server_id = vod.normalize_server_id(args.server)
        config = vod.load_server_config(server_id, allow_insecure_http=args.allow_insecure_http) if args.live or args.load_server_config else None
        bundle_sha256 = None
        if args.live:
            with requests.Session() as session:
                session.trust_env = False
                budget = vod.DownloadBudget()
                source = {action: vod.request_panel_json(session, config, action, budget=budget)
                          for action in ("get_vod_streams", "get_vod_categories")}
            source.update(get_series=[], get_series_categories=[], vod_details={}, series_details={})
        else:
            source = vod.load_offline_inputs(args.input_dir, server_id)
            bundle = args.input_dir / f"{server_id}_vod_bundle.json"
            if bundle.is_file():
                bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
        report = trace(source, server_id, args.movie_id or DEFAULT_IDS, config=config,
                       bundle_sha256=bundle_sha256, app_catalog_dir=args.app_catalog_dir,
                       run_id=os.environ.get("GITHUB_RUN_ID", ""), code_sha=os.environ.get("GITHUB_SHA", ""))
        app.atomic_json(args.output, report)
        print(json.dumps({"records": len(report["records"]), "inventoryPresent": sum(r["inventoryPresent"] for r in report["records"]),
                          "tmdbRequests": 0, "productionWritten": False}, sort_keys=True))
        return 0
    except (vod.CatalogBuildError, OSError, ValueError, KeyError, TypeError):
        print("Movie provenance trace failed validation; no raw provider values were logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
