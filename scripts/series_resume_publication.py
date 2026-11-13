#!/usr/bin/env python3
"""Attach and strictly validate optional resume graphs in a private candidate.

Local file IO only; this is not a deployment command. Attachment requires an
exclusively owned candidate directory after its ordinary build has finished.
The sibling lock serializes cooperating attachment calls, not unrelated writers.
SHA rechecks detect source changes but are not a filesystem compare-and-swap.
The caller still owns retention, component sealing and the publication lock.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

import series_resume_lookup as lookup


DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
MAX_INDEX_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SeriesResumePublicationError(ValueError):
    """The optional publication is invalid or its source changed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SeriesResumePublicationError(message)


def _directory(value: Path) -> Path:
    path = lookup._io_path(value).absolute()
    for part in (path, *path.parents):
        _require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(),
                 "Candidate paths must not contain links or junctions.")
    _require(path.is_dir(), "The app candidate directory is missing.")
    return path.resolve(strict=True)


def _file_bytes(path: Path, maximum: int) -> bytes:
    _require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)()
             and path.is_file() and path.stat().st_nlink == 1,
             "A publication input is missing, linked or not a regular file.")
    _require(path.stat().st_size <= maximum, "A publication input exceeds its byte limit.")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    _require(len(data) <= maximum, "A publication input exceeds its byte limit.")
    return data


def _index_bytes(app_dir: Path) -> bytes:
    return _file_bytes(app_dir / "index.json", MAX_INDEX_BYTES)


def _index(document: Any, expected_servers: Sequence[str]) -> dict[str, Any]:
    expected = tuple(expected_servers)
    _require(bool(expected) and len(expected) == len(set(expected))
             and all(isinstance(value, str) and re.fullmatch(r"server_[1-9][0-9]?", value)
                     for value in expected), "Invalid expected server set.")
    _require(isinstance(document, dict) and type(document.get("schemaVersion")) is int
             and document["schemaVersion"] == 1 and document.get("appName") == "SkyTVPlayer"
             and lookup._utc(document.get("generatedAt")) is not None
             and isinstance(document.get("servers"), list)
             and type(document.get("serverCount")) is int
             and document["serverCount"] == len(document["servers"]), "Invalid app index identity.")
    identifiers = [row.get("serverId") if isinstance(row, dict) else None for row in document["servers"]]
    _require(all(isinstance(value, str) for value in identifiers)
             and len(identifiers) == len(expected) and set(identifiers) == set(expected),
             "The app index server set is incomplete or duplicated.")
    return document


def _read_index(app_dir: Path, expected_servers: Sequence[str]) -> tuple[dict, bytes]:
    raw = _index_bytes(app_dir)
    return _index(lookup._strict_json(raw), expected_servers), raw


def _source_context(app_dir: Path, index: Mapping[str, Any], entry: Mapping[str, Any]) -> dict:
    manifest = lookup._read_artifact(app_dir, entry.get("series"),
                                     compressed_limit=64 * 1024 * 1024, expanded_limit=64 * 1024 * 1024)
    cutoff = lookup._utc(manifest.get("generatedAt"))
    _require(type(manifest.get("schemaVersion")) is int and manifest["schemaVersion"] == 1
             and manifest.get("serverId") == entry["serverId"] and manifest.get("dataset") == "vod-series"
             and cutoff is not None and cutoff <= lookup._utc(index["generatedAt"])
             and type(entry.get("seriesCount")) is int and type(manifest.get("totalRecords")) is int
             and entry["seriesCount"] == manifest["totalRecords"],
             "Series identity, count or accepted snapshot cutoff does not match.")
    home = lookup._read_artifact(app_dir, entry.get("home"),
                                 compressed_limit=512 * 1024, expanded_limit=64 * 1024 * 1024)
    _require(type(home.get("schemaVersion")) is int and home["schemaVersion"] == 1
             and home.get("serverId") == entry["serverId"]
             and home.get("generatedAt") == manifest["generatedAt"]
             and isinstance(home.get("items"), dict)
             and len(home["items"]) == entry["home"]["recordCount"],
             "Home and series snapshot ownership does not match.")
    return manifest


def _validate(app_dir: Path, document: dict, expected_servers: Sequence[str], require_lookup: bool,
              graph_dirs: Mapping[str, Path] | None = None) -> dict:
    _index(document, expected_servers)
    _require(type(require_lookup) is bool, "require_lookup must be a boolean.")
    metrics = []
    for entry in document["servers"]:
        if "seriesResumeLookup" not in entry:
            _require(not require_lookup, "An expected server has no resume lookup reference.")
            continue
        _source_context(app_dir, document, entry)
        directory = graph_dirs[entry["serverId"]] if graph_dirs is not None else app_dir
        metrics.append(lookup.validate_series_resume_lookup(
            directory, entry["seriesResumeLookup"], source_dir=app_dir,
            server_id=entry["serverId"], series_ref=entry["series"]))
    return {"serverCount": len(document["servers"]), "lookupServerCount": len(metrics),
            "lookupRequired": require_lookup, "servers": metrics}


def validate_optional_graphs(app_dir: Path, *, expected_servers: Sequence[str] = DEFAULT_SERVERS,
                             index: Mapping[str, Any] | None = None, require_lookup: bool = False) -> dict:
    """Strictly check every present root; absence is supported unless required.

    This supplements ordinary catalogue validation; it does not replace it.
    A supplied in-memory index is validated without modifying it or disk bytes.
    """
    try:
        directory = _directory(app_dir)
        document = _read_index(directory, expected_servers)[0] if index is None else copy.deepcopy(index)
        return _validate(directory, document, expected_servers, require_lookup)
    except SeriesResumePublicationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise SeriesResumePublicationError("Invalid optional resume publication graph.") from exc


@contextmanager
def _attachment_lock(app_dir: Path):
    lock = app_dir.parent / ("." + app_dir.name + ".series-resume-attachment.lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise SeriesResumePublicationError("Another attachment owns the candidate; inspect a stale lock before retrying.") from exc
    state = {"lockCleanupPending": False}
    try:
        yield state
    finally:
        try:
            lock.rmdir()
        except OSError:
            # The transaction may already be committed. Do not report a failed
            # attachment after a successful index switch; surface cleanup state.
            state["lockCleanupPending"] = True


def _source_pins(app_dir: Path, document: dict) -> dict[str, tuple[int, str]]:
    """Pin all ordinary files whose facts define this new lookup generation."""
    pins = {}

    def pin(reference):
        checked = lookup._ref(reference, compressed_limit=64 * 1024 * 1024,
                              expanded_limit=64 * 1024 * 1024,
                              optional={"providerCategoryIds", "languages"})
        identity = (checked["compressedBytes"], checked["sha256"])
        _require(checked["file"] not in pins or pins[checked["file"]] == identity,
                 "Source references disagree about immutable file bytes.")
        pins[checked["file"]] = identity

    for entry in document["servers"]:
        manifest = _source_context(app_dir, document, entry)
        pin(entry["series"])
        pin(entry["home"])
        for reference in manifest["shards"]:
            pin(reference)
            shard = lookup._read_artifact(app_dir, reference, compressed_limit=512 * 1024,
                                          expanded_limit=64 * 1024 * 1024,
                                          optional={"providerCategoryIds", "languages"})
            for parent in shard["items"]:
                for episode_ref in parent["episodeFiles"]:
                    pin(episode_ref)
    _verify_pins(app_dir, pins)
    return pins


def _verify_pins(app_dir: Path, pins: Mapping[str, tuple[int, str]]) -> None:
    for name, (length, digest) in pins.items():
        data = _file_bytes(app_dir / name, length)
        _require(len(data) == length and hashlib.sha256(data).hexdigest() == digest,
                 "An ordinary source artifact changed during attachment.")


def _check_index_pin(app_dir: Path, expected_sha256: str) -> None:
    _require(hashlib.sha256(_index_bytes(app_dir)).hexdigest() == expected_sha256,
             "The app index changed; attachment requires a new reviewed SHA-256 pin.")


def _same_artifact(path: Path, data: bytes) -> None:
    _require(_file_bytes(path, len(data)) == data, "An existing artifact filename has different bytes.")


def _preflight_artifacts(app_dir: Path, staged: Sequence[tuple[Path, str, bytes]]) -> None:
    names = set()
    for _source, name, data in staged:
        _require(name not in names, "Generated artifacts have duplicate filenames.")
        names.add(name)
        destination = app_dir / name
        if destination.exists() or destination.is_symlink():
            _same_artifact(destination, data)


def _install_artifact(app_dir: Path, name: str, data: bytes) -> None:
    destination = app_dir / name
    created = False
    try:
        with destination.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _same_artifact(destination, data)
    except BaseException:
        # Close the handle before cleanup (Windows); only our exclusive new file.
        if created:
            destination.unlink(missing_ok=True)
        raise
    _same_artifact(destination, data)


def _commit_index(app_dir: Path, document: dict, expected_sha256: str, pins: Mapping) -> None:
    raw = lookup._json_bytes(document)
    _require(len(raw) <= MAX_INDEX_BYTES, "The attached app index exceeds its byte limit.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".series-resume-index-", suffix=".tmp",
                                          dir=app_dir, delete=False) as handle:
            temporary = lookup._io_path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_pins(app_dir, pins)
        _check_index_pin(app_dir, expected_sha256)
        os.replace(temporary, app_dir / "index.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def attach(app_dir: Path, *, enabled: bool = False, expected_index_sha256: str,
           expected_servers: Sequence[str] = DEFAULT_SERVERS) -> dict:
    """Attach all server references, explicitly enabled and pinned; index is last.

    Disabled calls are byte-preserving. Failures preserve the accepted index;
    installation failures can leave unadvertised immutable artifacts for retry.
    An interrupted process can leave a stale sibling lock or incomplete immutable
    file, which blocks rather than overwrites on retry and requires local review.
    """
    try:
        _require(type(enabled) is bool and isinstance(expected_index_sha256, str)
                 and _SHA256.fullmatch(expected_index_sha256) is not None,
                 "Attachment requires a boolean flag and exact lowercase index SHA-256.")
        directory = _directory(app_dir)
        document, original = _read_index(directory, expected_servers)
        _require(hashlib.sha256(original).hexdigest() == expected_index_sha256,
                 "The reviewed app index SHA-256 does not match its loaded bytes.")
        _check_index_pin(directory, expected_index_sha256)
        if not enabled:
            return {"enabled": False, "changed": False, "indexSha256": expected_index_sha256,
                    "previousIndexSha256": expected_index_sha256,
                    "serverCount": len(document["servers"]),
                    "lookupServerCount": sum("seriesResumeLookup" in row for row in document["servers"]),
                    "servers": []}
        with _attachment_lock(directory) as cleanup:
            _check_index_pin(directory, expected_index_sha256)
            # Importing the ordinary validator does not construct API clients.
            import build_skytvplayer_catalog as catalog
            try:
                catalog.validate(directory, expected_servers=expected_servers, index=document)
            except catalog.vod.CatalogBuildError as exc:
                raise SeriesResumePublicationError("The ordinary app candidate failed validation.") from exc
            pins = _source_pins(directory, document)
            candidate = copy.deepcopy(document)
            with tempfile.TemporaryDirectory(prefix=".series-resume-publication-", dir=directory.parent) as temporary:
                stage = lookup._io_path(temporary)
                graph_dirs = {}
                for entry in candidate["servers"]:
                    output = stage / entry["serverId"]
                    entry["seriesResumeLookup"] = lookup.write_series_resume_lookup(
                        directory, output, entry["serverId"], entry["series"])
                    graph_dirs[entry["serverId"]] = output
                metrics = _validate(directory, candidate, expected_servers, True, graph_dirs)
                staged = []
                for output in graph_dirs.values():
                    for path in sorted(output.iterdir()):
                        staged.append((path, path.name, _file_bytes(path, 65_536)))
            # Staging cleanup must succeed before any install or index commit.
            _preflight_artifacts(directory, staged)
            _verify_pins(directory, pins)
            _check_index_pin(directory, expected_index_sha256)
            for _source, name, data in staged:
                _install_artifact(directory, name, data)
            changed = lookup._json_bytes(candidate) != lookup._json_bytes(document)
            final = lookup._json_bytes(candidate) if changed else original
            result = {"enabled": True, "changed": changed,
                      "previousIndexSha256": hashlib.sha256(original).hexdigest(),
                      "indexSha256": hashlib.sha256(final).hexdigest(), **metrics}
            if changed:
                _commit_index(directory, candidate, expected_index_sha256, pins)
            else:
                _verify_pins(directory, pins)
                _check_index_pin(directory, expected_index_sha256)
        result.update(cleanup)
        return result
    except SeriesResumePublicationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise SeriesResumePublicationError("Could not attach optional resume publication graphs.") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "attach"):
        sub = commands.add_parser(command)
        sub.add_argument("--app-dir", type=Path, required=True)
        sub.add_argument("--server", action="append")
        if command == "validate":
            sub.add_argument("--require-lookup", action="store_true")
        else:
            sub.add_argument("--enabled", action="store_true")
            sub.add_argument("--index-sha256", required=True)
    args = parser.parse_args(argv)
    expected = args.server or DEFAULT_SERVERS
    try:
        if args.command == "validate":
            result = validate_optional_graphs(args.app_dir, expected_servers=expected,
                                              require_lookup=args.require_lookup)
        else:
            result = attach(args.app_dir, enabled=args.enabled, expected_index_sha256=args.index_sha256,
                            expected_servers=expected)
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
