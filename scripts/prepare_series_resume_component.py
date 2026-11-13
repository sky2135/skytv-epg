#!/usr/bin/env python3
"""Prepare or materialize a pinned VOD component locally; never publish it.

Inputs and destinations must be exclusively owned while this command runs.
The result has no eligible GitHub artifact name, and the configured production
secret scan remains mandatory after materialization and before any upload.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Sequence

import app_catalog_closure as closure
import build_skytvplayer_catalog as catalog
import build_vod_catalogs as vod
import publication_snapshots as snapshots
import series_resume_publication as resume


DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PreparedComponentError(ValueError):
    pass


def _require(value, message):
    if not value:
        raise PreparedComponentError(message)


def _digest(path: Path, *, maximum=snapshots.MAX_FILE_BYTES) -> dict:
    _require(not path.is_symlink() and path.is_file(), "A component input is missing or linked.")
    _require(path.stat().st_size <= maximum, "A component input exceeds its byte limit.")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            _require(size <= maximum, "A component input exceeds its byte limit.")
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _pin(path: Path, expected: str, *, maximum=snapshots.MAX_INDEX_BYTES) -> str:
    _require(isinstance(expected, str) and SHA256.fullmatch(expected), "An exact lowercase SHA-256 pin is required.")
    snapshots._directory(path.parent, required=True)
    actual = _digest(path, maximum=maximum)["sha256"]
    _require(actual == expected, "The pinned input changed or does not match.")
    return actual


def _new_output(path, *sources) -> Path:
    target = snapshots._directory(path)
    _require(not target.exists(), "Use a NEW private output directory.")
    for source in sources:
        snapshots._separate(target, source)
    return target


def _source_manifest(public: Path) -> dict:
    files = []
    for name, path in sorted(snapshots._tree(public).items()):
        if snapshots._component(name) == "vod":
            metadata = _digest(path)
            address = re.search(r"\.([0-9a-f]{16})\.json(?:\.gz)?\Z", path.name)
            _require(address is None or address[1] == metadata["sha256"][:16],
                     "Stored content-addressed bytes do not match their filename; use a clean ordinary source.")
            if path.suffix == ".gz":
                # Retained/unadvertised files still pass the publication gzip
                # gate. Never hide damaged bytes by removing their reference.
                expanded = 0
                with gzip.open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        expanded += len(chunk)
                        _require(expanded <= snapshots.MAX_FILE_BYTES,
                                 "A stored gzip exceeds the expanded component-file limit.")
            files.append({"path": name, **metadata})
    manifest = snapshots._make_manifest("vod", files)
    _require(len(catalog.json_bytes(manifest)) <= snapshots.MAX_MANIFEST_BYTES,
             "The complete component manifest exceeds its byte limit.")
    closure.check_transfer_members(entry["path"] for entry in files)
    return manifest


def _validate_public(public: Path, expected_servers: Sequence[str], *, require_lookup=False,
                     ordinary_only=False) -> dict:
    ordinary_vod = vod.validate_publication(public / "VOD", expected_servers=expected_servers)
    index = None
    if ordinary_only:
        # Withdrawal never grants invalid optional references any authority.
        index = copy.deepcopy(vod._read_bounded_json(public / "skytvplayer/index.json",
                                                   snapshots.MAX_INDEX_BYTES, "App index"))
        for server in index["servers"]:
            server.pop("seriesResumeLookup", None)
    # The collector already runs the complete ordinary AND strict optional
    # validators. Reuse their returned metrics instead of reparsing the same
    # full movie/series catalogues a second time in this one call.
    closure_result = closure.collect_app_closure(public / "skytvplayer", expected_servers=expected_servers,
                                                index=index)
    validation = closure_result["validation"]
    graphs = dict(validation["lookup"])
    _require(not require_lookup or graphs["lookupServerCount"] == len(expected_servers),
             "An enabled prepared component is missing a server lookup.")
    graphs["lookupRequired"] = require_lookup
    if ordinary_only:
        graphs = {"ignoredForCompensationSourcePreflight": True}
    return {"vod": ordinary_vod, "app": validation["app"], "lookup": graphs,
            "appClosure": closure_result["metrics"]}


def _copy_component(manifest: dict, source: Path, destination: Path) -> None:
    destination.mkdir()
    for entry in manifest["files"]:
        snapshots._copy_verified(source / entry["path"], destination / entry["path"], entry)


def prepare(candidate_public_dir: Path, last_good_app_dir: Path, output_dir: Path, *,
            expected_index_sha256: str, expected_last_good_index_sha256: str,
            expected_servers: Sequence[str] = DEFAULT_SERVERS, enabled: bool = False,
            compensate: bool = False) -> dict:
    """Create a NEW local review bundle containing snapshot/ and report.json.

    Default mode preserves input references without attaching new ones. Explicit
    compensation removes only the optional lookup fields. All original input
    files remain untouched; output is installed only after complete validation.
    """
    try:
        _require(type(enabled) is bool and type(compensate) is bool and not (enabled and compensate),
                 "Attachment and compensation are mutually exclusive boolean modes.")
        source = snapshots._directory(candidate_public_dir, required=True)
        last_good = snapshots._directory(last_good_app_dir, required=True)
        target = _new_output(output_dir, source, last_good)
        index_path, old_index = source / "skytvplayer/index.json", last_good / "index.json"
        _pin(index_path, expected_index_sha256)
        _pin(old_index, expected_last_good_index_sha256)
        lookup_change_source_index = None
        source_retention_record = None
        record_path = source / "skytvplayer" / closure.RETENTION_RECORD
        if record_path.exists() or record_path.is_symlink():
            # Authenticate the copied record against the ORIGINAL pinned input
            # before attachment/withdrawal changes its optional references.
            origin = vod._read_bounded_json(index_path, snapshots.MAX_INDEX_BYTES, "App index")
            source_retention_record = closure.validate_retention_owner(source / "skytvplayer", index=origin)
            if enabled or compensate:
                lookup_change_source_index = origin
        _validate_public(source, expected_servers, ordinary_only=compensate)
        closure.collect_app_closure(last_good, expected_servers=expected_servers)
        source_manifest = _source_manifest(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".resume-component-", dir=target.parent) as temp:
            staging = Path(temp)
            public = staging / "candidate"
            _copy_component(source_manifest, source, public)
            app_dir = public / "skytvplayer"
            attachment = {"enabled": False, "changed": False}
            compensation_index = None
            if enabled:
                attachment = resume.attach(app_dir, enabled=True, expected_index_sha256=expected_index_sha256,
                                           expected_servers=expected_servers)
            elif compensate:
                index = vod._read_bounded_json(app_dir / "index.json", snapshots.MAX_INDEX_BYTES, "App index")
                # Keep only an explicitly validated prior closure through the
                # compensation and its one following distinct app generation.
                # A broken optional source cannot supply an authoritative hold.
                try:
                    resume.validate_optional_graphs(app_dir, index=index, expected_servers=expected_servers)
                    if any("seriesResumeLookup" in server for server in index["servers"]):
                        compensation_index = copy.deepcopy(index)
                except resume.SeriesResumePublicationError:
                    pass
                if (compensation_index is None
                        and not (source_retention_record is not None and source_retention_record["anchor"] is not None)):
                    known = vod._read_bounded_json(last_good / "index.json", snapshots.MAX_INDEX_BYTES, "Last-good app index")
                    if any("seriesResumeLookup" in server for server in known["servers"]):
                        compensation_index = known
                for server in index["servers"]:
                    server.pop("seriesResumeLookup", None)
                catalog.atomic_json(app_dir / "index.json", index)
            origin_options = ({"lookup_change_source_index": lookup_change_source_index}
                              if lookup_change_source_index is not None else {})
            retention = closure.retain_app_closures(app_dir, last_good, expected_servers=expected_servers,
                                                   compensation_index=compensation_index, **origin_options)
            validation = _validate_public(public, expected_servers, require_lookup=enabled)
            final_manifest = _source_manifest(public)
            snapshot = staging / "snapshot"
            sealed = snapshots.package("vod", public, snapshot)
            _require(sealed == final_manifest, "Sealed component differs from the validated candidate.")
            snapshots._snapshot(snapshot, "vod")
            snapshot_sha = _digest(snapshot / "snapshot.json", maximum=snapshots.MAX_MANIFEST_BYTES)["sha256"]
            report = {
                "schema": "SKYTV_SERIES_RESUME_PREPARED_COMPONENT_V1", "readyForPublication": False,
                "publicReferenceActivated": False, "productionSecretScanPerformed": False,
                "mode": "compensate" if compensate else "attach" if enabled else "preserve",
                "sourceIndexSha256": expected_index_sha256,
                "lastGoodIndexSha256": expected_last_good_index_sha256,
                "candidateIndexSha256": _digest(app_dir / "index.json", maximum=snapshots.MAX_INDEX_BYTES)["sha256"],
                "snapshotDirectory": "snapshot", "snapshotManifestSha256": snapshot_sha,
                "componentFiles": sealed["fileCount"], "componentBytes": sealed["totalBytes"],
                "retention": retention, "validation": validation, "attachment": attachment,
                "remainingGates": ["configured_production_secret_scan", "reviewed_current_source",
                                   "paused_and_drained_producers", "reviewed_restorer_selection",
                                   "public_hash_closure_and_ownership_checks", "older_client_and_native_acceptance"],
            }
            (staging / "report.json").write_bytes(catalog.json_bytes(report))
            # The source copy is deliberately not part of the transferable bundle.
            # Remove only this command's owned temporary tree after sealing.
            import shutil
            shutil.rmtree(public)
            _pin(index_path, expected_index_sha256)
            _pin(old_index, expected_last_good_index_sha256)
            _require(not target.exists(), "The private destination changed during preparation.")
            os.rename(staging, target)
        return report
    except PreparedComponentError:
        raise
    except (OSError, ValueError, TypeError, KeyError, vod.CatalogBuildError) as exc:
        raise PreparedComponentError("Could not prepare a validated private VOD component.") from exc


def materialize(bundle_dir: Path, public_output_dir: Path, *, expected_snapshot_sha256: str,
                expected_servers: Sequence[str] = DEFAULT_SERVERS) -> dict:
    """Verify a pinned snapshot independently of its report and copy a NEW tree.

    Materialization performs no configured secret scan and no deployment. The
    ordinary production scan/check/sealing path must still follow it.
    """
    try:
        bundle = snapshots._directory(bundle_dir, required=True)
        snapshot = bundle / "snapshot"
        target = _new_output(public_output_dir, bundle)
        _pin(snapshot / "snapshot.json", expected_snapshot_sha256, maximum=snapshots.MAX_MANIFEST_BYTES)
        snapshot, manifest = snapshots._snapshot(snapshot, "vod")
        source = snapshot / "files"
        for entry in manifest["files"]:
            _require(_digest(source / entry["path"]) == {key: entry[key] for key in ("bytes", "sha256")},
                     "A sealed component file failed its exact checksum or size.")
        validation = _validate_public(source, expected_servers)
        _require(_source_manifest(source) == manifest, "The prepared component closure changed.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".resume-materialize-", dir=target.parent) as temp:
            staging = Path(temp)
            candidate = staging / "public"
            _copy_component(manifest, source, candidate)
            _validate_public(candidate, expected_servers)
            _require(_source_manifest(candidate) == manifest, "Copied component differs from its seal.")
            _pin(snapshot / "snapshot.json", expected_snapshot_sha256, maximum=snapshots.MAX_MANIFEST_BYTES)
            _require(not target.exists(), "The private destination changed during materialization.")
            os.rename(candidate, target)
        return {"schema": "SKYTV_SERIES_RESUME_MATERIALIZED_COMPONENT_V1", "readyForPublication": False,
                "publicReferenceActivated": False, "productionSecretScanPerformed": False,
                "snapshotManifestSha256": expected_snapshot_sha256,
                "componentFiles": manifest["fileCount"], "componentBytes": manifest["totalBytes"],
                "validation": validation}
    except PreparedComponentError:
        raise
    except (OSError, ValueError, TypeError, KeyError, vod.CatalogBuildError) as exc:
        raise PreparedComponentError("Could not materialize the pinned private VOD component.") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--candidate-public-dir", required=True, type=Path)
    prepare_parser.add_argument("--last-good-app-dir", required=True, type=Path)
    prepare_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_parser.add_argument("--index-sha256", required=True)
    prepare_parser.add_argument("--last-good-index-sha256", required=True)
    modes = prepare_parser.add_mutually_exclusive_group()
    modes.add_argument("--enabled", action="store_true")
    modes.add_argument("--compensate", action="store_true")
    materialize_parser = commands.add_parser("materialize")
    materialize_parser.add_argument("--bundle-dir", required=True, type=Path)
    materialize_parser.add_argument("--output-dir", required=True, type=Path)
    materialize_parser.add_argument("--snapshot-sha256", required=True)
    for command in (prepare_parser, materialize_parser):
        command.add_argument("--server", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        servers = args.server or DEFAULT_SERVERS
        if args.command == "prepare":
            report = prepare(args.candidate_public_dir, args.last_good_app_dir, args.output_dir,
                             expected_index_sha256=args.index_sha256,
                             expected_last_good_index_sha256=args.last_good_index_sha256,
                             expected_servers=servers, enabled=args.enabled, compensate=args.compensate)
        else:
            report = materialize(args.bundle_dir, args.output_dir,
                                 expected_snapshot_sha256=args.snapshot_sha256, expected_servers=servers)
    except PreparedComponentError as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
