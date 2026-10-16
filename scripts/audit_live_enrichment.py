#!/usr/bin/env python3
"""Verify published live guide compatibility and emit safe aggregate quality data.

One bounded JSON file is parsed at a time. V1 schedules are reduced to hashes
before v2 is loaded, so two complete programme trees never coexist in memory.
The public report contains counts/checksums only, without channel names,
programme descriptions, stream URLs or per-stream identifiers.
"""
from __future__ import annotations

import argparse
import gc
import gzip
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from .epg_programme_details import DETAIL_FIELDS, details_json
else:
    from epg_programme_details import DETAIL_FIELDS, details_json


MAX_COMPRESSED_BYTES = 200 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_STREAMS = 250_000
MAX_PROGRAMME_ROWS = 5_000_000
PROGRAMME_FIELDS = DETAIL_FIELDS | {"categories", "description", "subtitle"}
GENRES = {"sports", "religion", "news", "entertainment", "movies", "kids", "documentary", "music", "lifestyle", "education", "shopping", "general", "events", "adult", "unknown"}
LOGO_KINDS = {"missing", "generated_artwork", "person_portrait", "provider_logo", "brand_logo", "source_channel_logo", "approved_override"}
IMAGE_STATUSES = {"missing", "unchecked", "valid_image", "http_error", "network_error", "invalid_image"}
SOURCE_TYPES = {"epgshare", "panel", "synthetic"}


class AuditError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


class _CappedReader(io.RawIOBase):
    def __init__(self, source: Any, limit: int):
        self.source, self.limit, self.count = source, limit, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        block = self.source.read(min(len(buffer), self.limit - self.count + 1))
        self.count += len(block)
        _require(self.count <= self.limit, "Expanded guide exceeds its byte limit")
        buffer[:len(block)] = block
        return len(block)


def _json_file(path: Path) -> dict[str, Any]:
    _require(path.is_file() and path.stat().st_size <= MAX_MANIFEST_BYTES, "Manifest/index is missing or exceeds its byte limit")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    _require(isinstance(payload, dict), "Manifest/index must be a JSON object")
    return payload


def _gzip_json(path: Path) -> dict[str, Any]:
    _require(path.is_file() and path.stat().st_size <= MAX_COMPRESSED_BYTES, "Compressed guide is missing or exceeds its byte limit")
    with gzip.open(path, "rb") as source:
        with io.TextIOWrapper(io.BufferedReader(_CappedReader(source, MAX_EXPANDED_BYTES)), encoding="utf-8") as handle:
            payload = json.load(handle)
    _require(isinstance(payload, dict), "Guide data must be a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(epg_dir: Path, server: str, role: str, generated_at: int) -> tuple[dict[str, Any], dict[str, Any]]:
    stem = {"legacy": "epg", "rich": "epg_v2", "metadata": "metadata"}[role]
    filename = f"{server}_{stem}.json.gz"
    manifest = _json_file(epg_dir / f"{server}_{stem}_manifest.json")
    _require(manifest.get("serverId") == server and manifest.get("generatedAt") == generated_at, f"{role} manifest server/generation mismatch")
    _require(manifest.get("schemaVersion") == (2 if role == "rich" else 1), f"{role} manifest schema mismatch")
    _require(manifest.get("dataFile") == filename, f"{role} manifest filename mismatch")
    path = epg_dir / filename
    _require(path.is_file() and 0 < path.stat().st_size <= MAX_COMPRESSED_BYTES, f"{role} compressed guide missing/oversized")
    digest = _sha256(path)
    _require(manifest.get("dataSha256") == digest, f"{role} compressed guide checksum mismatch")
    _require(type(manifest.get("compressedBytes")) is int and manifest["compressedBytes"] == path.stat().st_size, f"{role} compressed guide size mismatch")
    _require(bool(re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("mappingSha256", "")))), f"{role} manifest mapping checksum invalid")
    return manifest, {"file": filename, "sha256": digest, "compressedBytes": path.stat().st_size}


def _envelope(payload: Mapping[str, Any], server: str, schema: int, generated_at: int) -> None:
    _require(payload.get("schemaVersion") == schema and payload.get("serverId") == server and payload.get("generatedAt") == generated_at, "Guide schema/server/generation mismatch")


def _projection_row(item: Any, *, rich: bool) -> list[Any]:
    if rich:
        _require(isinstance(item, dict), "V2 programme must be an object")
        _require(not set(item) - (PROGRAMME_FIELDS | {"start", "stop", "title", "sourceType"}), "V2 programme contains unsupported fields")
        row = [item.get("start"), item.get("stop"), item.get("title")]
    else:
        _require(isinstance(item, list) and len(item) == 3, "V1 programme must have three values")
        row = item
    _require(type(row[0]) is int and type(row[1]) is int and 0 <= row[0] < row[1] and isinstance(row[2], str) and bool(row[2].strip()), "Programme timing/title is invalid")
    return row


def _schedule_hash(rows: Any, *, rich: bool) -> tuple[str, int]:
    _require(isinstance(rows, list) and len(rows) <= MAX_PROGRAMME_ROWS, "Programme schedule is invalid/oversized")
    digest = hashlib.sha256()
    for item in rows:
        row = _projection_row(item, rich=rich)
        digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest(), len(rows)


def _guide_maps(payload: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, list], dict[str, str]]:
    streams, programmes, logos = (payload.get(key) for key in ("streamToEpg", "programmes", "streamLogos"))
    _require(isinstance(streams, dict) and isinstance(programmes, dict) and isinstance(logos, dict), "Guide mappings must be JSON objects")
    _require(1 <= len(streams) <= MAX_STREAMS and len(programmes) <= MAX_STREAMS and len(logos) <= MAX_STREAMS, "Guide mapping is empty or exceeds row limit")
    _require(all(isinstance(key, str) and isinstance(value, str) for key, value in streams.items()), "Stream schedule mapping is invalid")
    _require(set(logos).issubset(streams) and all(isinstance(value, str) and bool(value) for value in logos.values()), "Stream logo mapping is invalid")
    return streams, programmes, logos


def audit_server(epg_dir: Path, build: Mapping[str, Any], generated_at: int) -> dict[str, Any]:
    server = build.get("serverId")
    _require(isinstance(server, str) and bool(re.fullmatch(r"server_[123]", server)), "Index has an invalid server identity")
    expected_refs = {"dataFile": f"EPG/{server}_epg.json.gz", "manifestFile": f"EPG/{server}_epg_manifest.json", "richDataFile": f"EPG/{server}_epg_v2.json.gz", "richManifestFile": f"EPG/{server}_epg_v2_manifest.json", "metadataFile": f"EPG/{server}_metadata.json.gz"}
    _require(all(build.get(key) == value for key, value in expected_refs.items()) and build.get("richSchemaVersion") == 2, "Index file references/schema are inconsistent")
    manifests, files = {}, {}
    for role in ("legacy", "rich", "metadata"):
        manifests[role], files[role] = _manifest(epg_dir, server, role, generated_at)
    old_manifest, rich_manifest, meta_manifest = (manifests[role] for role in ("legacy", "rich", "metadata"))
    _require(len({manifest["mappingSha256"] for manifest in manifests.values()}) == 1, "Server mapping checksum parity failed")
    _require(build.get("richDataSha256") == files["rich"]["sha256"] and build.get("richCompressedBytes") == files["rich"]["compressedBytes"], "Index rich guide checksum/size mismatch")
    _require(old_manifest.get("metadataFile") == files["metadata"]["file"] and old_manifest.get("metadataSha256") == files["metadata"]["sha256"], "Legacy metadata reference/checksum mismatch")
    _require(old_manifest.get("richDataFile") == files["rich"]["file"] and old_manifest.get("richDataSha256") == files["rich"]["sha256"] and old_manifest.get("richCompressedBytes") == files["rich"]["compressedBytes"], "Legacy rich guide reference/checksum/size mismatch")
    _require(old_manifest.get("richManifestFile") == f"{server}_epg_v2_manifest.json" and old_manifest.get("richSchemaVersion") == 2 and rich_manifest.get("legacyDataFile") == files["legacy"]["file"] and rich_manifest.get("legacyManifestFile") == f"{server}_epg_manifest.json", "Guide manifest counterpart references are inconsistent")
    _require(rich_manifest.get("metadataFile") == files["metadata"]["file"] and rich_manifest.get("metadataSha256") == files["metadata"]["sha256"], "Rich metadata reference/checksum mismatch")
    legacy = _gzip_json(epg_dir / files["legacy"]["file"])
    _envelope(legacy, server, 1, generated_at)
    streams, schedules, logos = _guide_maps(legacy)
    fingerprints = {key: _schedule_hash(rows, rich=False) for key, rows in schedules.items()}
    programme_rows = sum(count for _, count in fingerprints.values())
    _require(programme_rows <= MAX_PROGRAMME_ROWS, "Guide programme count exceeds limit")
    window = (legacy.get("windowStart"), legacy.get("windowEnd"))
    _require(all(type(value) is int for value in window) and window[0] < window[1], "Guide window is invalid")
    del schedules, legacy
    gc.collect()

    rich = _gzip_json(epg_dir / files["rich"]["file"])
    _envelope(rich, server, 2, generated_at)
    _require(rich.get("programmeRowFormat") == "object", "V2 programme row format is invalid")
    rich_streams, rich_schedules, rich_logos = _guide_maps(rich)
    _require(rich_streams == streams and rich_logos == logos and set(rich_schedules) == set(fingerprints), "V1/V2 stream/logo/schedule parity failed")
    _require((rich.get("windowStart"), rich.get("windowEnd")) == window, "V1/V2 guide window parity failed")
    all_fields, real_fields, sources = Counter(), Counter(), Counter()
    schedule_sources = {}
    for key, rows in rich_schedules.items():
        _require(_schedule_hash(rows, rich=True) == fingerprints[key], "V2 programme projection differs from V1")
        row_sources = set()
        for item in rows:
            source = item.get("sourceType")
            _require(source in SOURCE_TYPES, "Programme source type is invalid")
            row_sources.add(source)
            sources[source] += 1
            categories = item.get("categories")
            _require(isinstance(categories, list) and len(categories) <= 64 and all(isinstance(value, str) and len(value) <= 1200 for value in categories), "Programme categories are invalid")
            _require(all(field not in item or isinstance(item[field], str) for field in ("description", "subtitle")), "Programme description/subtitle must be text")
            try:
                details_json({field: item[field] for field in DETAIL_FIELDS if field in item})
            except ValueError as exc:
                raise AuditError("Programme detail schema/privacy validation failed") from exc
            populated = [field for field in PROGRAMME_FIELDS if item.get(field)]
            all_fields.update(populated)
            if source != "synthetic":
                real_fields.update(populated)
        _require(len(row_sources) <= 1, "A schedule mixes programme source types")
        if row_sources:
            schedule_sources[key] = next(iter(row_sources))
    _require(rich_manifest.get("programmeFieldCounts") == dict(all_fields), "V2 manifest programme detail counts differ from data")
    _require(old_manifest.get("programmeDetailFieldCounts") == dict(all_fields), "V1 manifest rich detail counts differ from data")
    del rich_streams, rich_schedules, rich_logos, rich, fingerprints
    rows = item = None
    gc.collect()

    metadata = _gzip_json(epg_dir / files["metadata"]["file"])
    _envelope(metadata, server, 1, generated_at)
    _require(metadata.get("mappingSha256") == meta_manifest["mappingSha256"], "Metadata mapping checksum mismatch")
    rows = metadata.get("streamMetadata")
    _require(isinstance(rows, dict) and set(rows) == set(streams), "Guide/metadata stream identity parity failed")
    counts, kinds, statuses, modes = Counter(), Counter(), Counter(), Counter()
    for stream_id, row in rows.items():
        _require(isinstance(row, dict), "Channel metadata row must be an object")
        counts["channelRows"] += 1
        languages, countries = row.get("languages"), row.get("countries")
        _require(isinstance(languages, list) and all(isinstance(code, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", code) for code in languages), "Channel language tags are invalid")
        _require(row.get("primaryLanguage") in {"und", "mul"} or row.get("primaryLanguage") in languages, "Channel primary language is inconsistent")
        _require(isinstance(countries, list) and all(isinstance(code, str) and re.fullmatch(r"[A-Z]{2}", code) for code in countries), "Channel countries are invalid")
        _require(row.get("genre") in GENRES and row.get("guideMode") in SOURCE_TYPES, "Channel genre/guide mode is invalid")
        counts["knownLanguage"] += any(code not in {"und", "mul"} for code in languages)
        counts["knownCountry"] += bool(countries)
        counts["knownGenre"] += row["genre"] != "unknown"
        counts["logoUrl"] += bool(row.get("logoUrl"))
        counts["metadataEvidence"] += bool(row.get("metadataEvidence"))
        counts["groupHeaderRows"] += row.get("guideFallbackReason") == "providerGroupHeader"
        provenance = row.get("logoProvenance")
        _require(isinstance(provenance, dict) and provenance.get("kind") in LOGO_KINDS and provenance.get("imageStatus") in IMAGE_STATUSES and type(provenance.get("brandVerified")) is bool, "Channel logo provenance is invalid")
        kinds[provenance["kind"]] += 1
        statuses[provenance["imageStatus"]] += 1
        counts["brandVerified"] += provenance["brandVerified"]
        counts["channelLogo"] += bool(row.get("logoUrl")) and provenance["kind"] in {"provider_logo", "brand_logo", "source_channel_logo", "approved_override"}
        _require(row.get("logoUrl", "") == logos.get(stream_id, ""), "Guide/metadata logo parity failed")
        _require(bool(row.get("logoUrl")) == (provenance["kind"] != "missing"), "Logo URL/provenance presence mismatch")
        modes[row["guideMode"]] += 1
        source = schedule_sources.get(streams[stream_id])
        _require(source is None or source == row["guideMode"], "Programme/metadata source parity failed")
    _require(meta_manifest.get("metadataStreams") == len(rows), "Metadata manifest channel count mismatch")
    for manifest in (old_manifest, rich_manifest):
        _require(manifest.get("mappedStreams") == len(streams) and manifest.get("programmeRows") == programme_rows, "Guide manifest stream/programme count mismatch")
        _require((manifest.get("windowStart"), manifest.get("windowEnd")) == window, "Guide manifest window mismatch")
    _require(build.get("mappedStreams") == len(streams) and build.get("programmeRows") == programme_rows, "Index stream/programme count mismatch")
    return {"serverId": server, "verified": True, "files": files, "metadata": dict(sorted(counts.items())), "logoKinds": dict(sorted(kinds.items())), "imageStatuses": dict(sorted(statuses.items())), "guideModes": dict(sorted(modes.items())), "programmeRows": programme_rows, "realProgrammeRows": sources["epgshare"] + sources["panel"], "syntheticProgrammeRows": sources["synthetic"], "programmeSourceTypes": dict(sorted(sources.items())), "realProgrammeFieldCounts": dict(sorted(real_fields.items())), "allProgrammeFieldCounts": dict(sorted(all_fields.items()))}


def audit_public(public_dir: Path | str, output: Path | str | None = None) -> dict[str, Any]:
    public_dir = Path(public_dir)
    index = _json_file(public_dir / "EPG" / "index.json")
    _require(index.get("schemaVersion") == 1 and type(index.get("generatedAt")) is int, "App index schema/generation is invalid")
    builds = index.get("builds")
    _require(isinstance(builds, list) and 1 <= len(builds) <= 3 and all(isinstance(build, dict) for build in builds), "App index builds are invalid")
    _require(len({build.get("serverId") for build in builds}) == len(builds), "App index contains duplicate servers")
    servers = [audit_server(public_dir / "EPG", build, index["generatedAt"]) for build in builds]
    totals: dict[str, Any] = {}
    for field in ("metadata", "logoKinds", "imageStatuses", "guideModes", "programmeSourceTypes", "realProgrammeFieldCounts", "allProgrammeFieldCounts"):
        counter = Counter()
        for server in servers:
            counter.update(server[field])
        totals[field] = dict(sorted(counter.items()))
    for field in ("programmeRows", "realProgrammeRows", "syntheticProgrammeRows"):
        totals[field] = sum(server[field] for server in servers)
    report = {"schemaVersion": 1, "status": "verified", "generatedAt": index["generatedAt"], "auditedAt": datetime.now(timezone.utc).isoformat(), "serverCount": len(servers), "checks": {"compressedChecksumsAndSizes": True, "schemaServerGenerationParity": True, "legacyProjectionEqualsRich": True, "streamMetadataAndLogoParity": True, "richFactsExcludeSyntheticInRealCounts": True}, "totals": totals, "servers": servers}
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-dir", type=Path, default=Path("public"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit_public(args.public_dir, args.output or args.public_dir / "reports" / "live_enrichment.json")
    except (AuditError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        # Diagnostics deliberately omit source data and potentially private text.
        message = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
        print(f"Live enrichment audit failed: {message}", file=sys.stderr)
        return 1
    totals = report["totals"]
    metadata = totals["metadata"]
    print(f"Verified {report['serverCount']} live server(s): {metadata['channelRows']:,} channel rows; "
          f"language {metadata['knownLanguage']:,}, country {metadata['knownCountry']:,}, "
          f"genre {metadata['knownGenre']:,}, channel logos {metadata['channelLogo']:,}; "
          f"{totals['realProgrammeRows']:,} real programme rows, "
          f"{totals['realProgrammeFieldCounts'].get('description', 0):,} with source descriptions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
