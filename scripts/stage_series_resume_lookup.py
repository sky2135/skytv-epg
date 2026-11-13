#!/usr/bin/env python3
"""Prepare optional resume lookup artifacts locally without activating them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import build_skytvplayer_catalog as catalog
import build_vod_catalogs as vod


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SERVER_ID = re.compile(r"server_[123]\Z")


class StageError(ValueError):
    pass


def _filesystem_path(path: Path) -> Path:
    value = os.path.abspath(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def _snapshot_time(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise StageError("The source snapshot timestamp is invalid.")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StageError("The source snapshot timestamp is invalid.") from exc


def _load_index(path: Path, expected_sha256: str) -> tuple[dict[str, Any], bytes]:
    path = _filesystem_path(path)
    if not SHA256.fullmatch(expected_sha256):
        raise StageError("The source index requires its exact lowercase SHA-256.")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INDEX_BYTES:
        raise StageError("The source index is missing, linked or oversized.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise StageError("The source index SHA-256 does not match.")
    document = vod._strict_json_from_bytes(data, "Resume staging index")
    if (not isinstance(document, dict) or type(document.get("schemaVersion")) is not int
            or document["schemaVersion"] != 1
            or document.get("appName") != "SkyTVPlayer"
            or not isinstance(document.get("servers"), list)):
        raise StageError("The source is not a supported published app index.")
    identifiers = [server.get("serverId") if isinstance(server, dict) else None
                   for server in document["servers"]]
    if (not identifiers
            or any(not isinstance(value, str) or not SERVER_ID.fullmatch(value) for value in identifiers)
            or len(set(identifiers)) != len(identifiers)
            or type(document.get("serverCount")) is not int or document["serverCount"] != len(identifiers)):
        raise StageError("The source server inventory is invalid.")
    _snapshot_time(document.get("generatedAt"))
    return document, data


def stage(index_path: Path, artifact_dirs: Sequence[Path], output_dir: Path, *,
          expected_index_sha256: str, server_ids: Sequence[str] = (),
          max_input_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    """Validate and copy immutable inputs, then emit a separate review bundle.

    The source index and all its original references remain unchanged. The
    resulting descriptor is an investigation report, never a replacement app
    index. There is intentionally no publishing or activation option.
    """
    from series_resume_lookup import validate_series_resume_lookup, write_series_resume_lookup

    if type(max_input_bytes) is not int or not 0 < max_input_bytes <= MAX_INPUT_BYTES:
        raise StageError("The staging input byte ceiling is invalid.")
    if not artifact_dirs:
        raise StageError("At least one published-artifact directory is required.")
    roots = tuple(_filesystem_path(path).resolve() for path in artifact_dirs)
    if len(set(roots)) != len(roots) or any(not path.is_dir() for path in roots):
        raise StageError("Artifact roots must be distinct existing directories.")
    target = _filesystem_path(output_dir).resolve()
    source_index = _filesystem_path(index_path).resolve()
    if (target.exists() or "public" in {part.casefold() for part in target.parts}
            or target == source_index or target in source_index.parents
            or any(target == root or target in root.parents or root in target.parents for root in roots)):
        raise StageError("Use a new private staging directory outside every source directory.")
    document, index_bytes = _load_index(index_path, expected_index_sha256)
    index_time = _snapshot_time(document["generatedAt"])
    selected = set(server_ids) if server_ids else {entry["serverId"] for entry in document["servers"]}
    available = {entry["serverId"] for entry in document["servers"]}
    if (not selected or any(not isinstance(value, str) or not SERVER_ID.fullmatch(value) for value in selected)
            or not selected <= available or len(server_ids) != len(set(server_ids))):
        raise StageError("Requested server selection is invalid.")

    references: dict[str, tuple[Path, dict[str, Any]]] = {}
    missing: dict[str, dict[str, Any]] = {}
    input_bytes = len(index_bytes)

    def read(reference: Any) -> dict[str, Any] | None:
        nonlocal input_bytes
        if not isinstance(reference, dict):
            raise StageError("An input artifact reference is invalid.")
        name = reference.get("file")
        if not isinstance(name, str) or not catalog.ARTIFACT_NAME.fullmatch(name):
            raise StageError("An input artifact file name is invalid.")
        if name in references:
            if references[name][1].get("sha256") != reference.get("sha256"):
                raise StageError("One artifact name has conflicting source checksums.")
            return catalog.read_artifact(references[name][0].parent, reference)
        for root in roots:
            path = root / name
            if not path.exists() and not path.is_symlink():
                continue
            result = catalog.read_artifact(root, reference)
            size = path.stat().st_size
            if input_bytes + size > max_input_bytes:
                raise StageError("The complete input set exceeds the staging byte ceiling.")
            input_bytes += size
            references[name] = (path, reference)
            return result
        missing[name] = {key: reference.get(key) for key in (
            "file", "sha256", "compressedBytes", "uncompressedBytes", "recordCount")}
        return None

    chosen = [entry for entry in document["servers"] if entry["serverId"] in selected]
    for entry in chosen:
        manifest = read(entry.get("series"))
        if manifest is None:
            continue
        if (type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1
                or manifest.get("serverId") != entry["serverId"] or manifest.get("dataset") != "vod-series"
                or not isinstance(manifest.get("shards"), list)):
            raise StageError("An input series manifest has the wrong identity.")
        manifest_time = _snapshot_time(manifest.get("generatedAt"))
        if (manifest_time > index_time or type(entry.get("seriesCount")) is not int
                or type(manifest.get("totalRecords")) is not int
                or entry["seriesCount"] != manifest["totalRecords"]):
            raise StageError("The source index series count or snapshot cutoff does not match.")
        home = read(entry.get("home"))
        if home is not None and (
                type(home.get("schemaVersion")) is not int or home["schemaVersion"] != 1
                or home.get("serverId") != entry["serverId"]
                or _snapshot_time(home.get("generatedAt")) != manifest_time):
            raise StageError("The accepted Home and series snapshots do not match.")
        for reference in manifest["shards"]:
            shard = read(reference)
            if shard is None:
                continue
            if (type(shard.get("schemaVersion")) is not int or shard["schemaVersion"] != 1
                    or shard.get("serverId") != entry["serverId"] or shard.get("dataset") != "vod-series"
                    or shard.get("generatedAt") != manifest["generatedAt"]
                    or not isinstance(shard.get("items"), list)):
                raise StageError("An input parent shard has the wrong identity.")
            for parent in shard["items"]:
                if not isinstance(parent, dict) or not isinstance(parent.get("episodeFiles"), list):
                    raise StageError("An input parent has no valid episode reference list.")
                for episode_ref in parent["episodeFiles"]:
                    read(episode_ref)

    report: dict[str, Any] = {
        "schema": "SKYTV_SERIES_RESUME_LOCAL_STAGE_V1", "readyForPublication": False,
        "sourceIndexSha256": expected_index_sha256, "selectedServers": sorted(selected),
        "inputArtifacts": len(references), "inputCompressedBytes": input_bytes,
        "missingArtifacts": sorted(missing.values(), key=lambda value: value["file"]),
        "sourcePathsUnchanged": True, "publicReferenceActivated": False,
    }
    target.mkdir(parents=True)
    if missing:
        report["status"] = "blocked_missing_checksum_linked_inputs"
        (target / "stage-report.json").write_bytes(catalog.json_bytes(report))
        return report

    inputs = target / "inputs"
    output = target / "artifacts"
    inputs.mkdir()
    output.mkdir()
    (inputs / "source-index.json").write_bytes(index_bytes)
    for name, (path, reference) in references.items():
        data = path.read_bytes()
        if len(data) != reference["compressedBytes"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise StageError("An input changed between verification and staging.")
        with (inputs / name).open("xb") as handle:
            handle.write(data)
    records = []
    report.update(status="inputs_verified_lookup_validation_pending", servers=records)
    (target / "stage-report.json").write_bytes(catalog.json_bytes(report))
    for entry in chosen:
        server_output = output / entry["serverId"]
        try:
            reference = write_series_resume_lookup(inputs, server_output, entry["serverId"], entry["series"])
            metrics = validate_series_resume_lookup(
                server_output, reference, source_dir=inputs, server_id=entry["serverId"], series_ref=entry["series"])
        except (ValueError, OSError):
            report.update(status="invalid_lookup_candidate", failureServer=entry["serverId"])
            (target / "stage-report.json").write_bytes(catalog.json_bytes(report))
            raise
        records.append({"serverId": entry["serverId"], "series": entry["series"],
                        "artifactDirectory": f"artifacts/{entry['serverId']}",
                        "seriesResumeLookup": reference, "metrics": metrics})
    report.update(status="validated_local_stage_only", missingArtifacts=[], servers=records)
    (target / "stage-report.json").write_bytes(catalog.json_bytes(report))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--artifacts-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--server", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        report = stage(args.index, args.artifacts_dir, args.output_dir,
                       expected_index_sha256=args.index_sha256, server_ids=args.server)
    except (OSError, ValueError, vod.CatalogBuildError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "validated_local_stage_only" else 3


if __name__ == "__main__":
    raise SystemExit(main())
