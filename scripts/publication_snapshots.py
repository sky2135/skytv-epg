#!/usr/bin/env python3
"""Package, fetch, and compose independent live and VOD publication snapshots.

Snapshots contain snapshot.json and files/<public-relative-path>. Public
component manifests describe the exact deployed bytes, including the small app
index whose EPG references are refreshed during composition. No catalogue or
guide timestamps are changed when the other component is reused.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import requests

SCHEMA_VERSION = 1
MAX_FILES = 20_000
MAX_TREE_ENTRIES = 2 * MAX_FILES + 64
MAX_DEPTH = 12
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
MANIFEST_DIRECTORY = "component-manifests"
REQUIRED_FILES = {
    "live": {"EPG/index.json", "health.json"},
    "vod": {"VOD/index.json", "skytvplayer/index.json"},
}
SEGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SERVER_ID = re.compile(r"server_[1-9][0-9]*\Z")
WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)


class SnapshotError(ValueError):
    """An incomplete, corrupt, or unsafe component cannot replace a release."""


class SnapshotNotFound(SnapshotError):
    """The deployed component manifest has not been bootstrapped yet."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _directory(path, *, required=False):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise SnapshotError("Snapshot paths cannot use symbolic links or junctions.")
    if path.exists() and not path.is_dir() or required and not path.is_dir():
        raise SnapshotError("A snapshot directory is missing or invalid.")
    return path


def _separate(*paths):
    resolved = [path.resolve() for path in paths]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise SnapshotError("Snapshot source and destination directories must be separate.")


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise SnapshotError("A snapshot file path is invalid.")
    parts = value.split("/")
    if (len(parts) > MAX_DEPTH or any(part in {"", ".", ".."} or not SEGMENT.fullmatch(part)
            or part.endswith(".") or WINDOWS_DEVICE.fullmatch(part) for part in parts)
            or PurePosixPath(value).is_absolute()):
        raise SnapshotError("A snapshot file path is unsafe.")
    return value


def _path_key(value):
    # EPG/ (app JSON) and epg/ (XMLTV) are intentionally distinct public
    # namespaces. Other case-only aliases are unsafe on extraction platforms.
    parts = value.split("/")
    if parts[0].casefold() == "epg" and parts[0] not in {"EPG", "epg"}:
        raise SnapshotError("The EPG namespace has an ambiguous name.")
    return "/".join([parts[0] if parts[0] in {"EPG", "epg"} else parts[0].casefold(),
                     *(part.casefold() for part in parts[1:])])


def _component(path):
    first = path.split("/", 1)[0]
    if first.casefold() == MANIFEST_DIRECTORY.casefold():
        if first != MANIFEST_DIRECTORY:
            raise SnapshotError("Publication manifest directory has an ambiguous name.")
        return None
    if first.casefold() in {"vod", "skytvplayer"}:
        if first not in {"VOD", "skytvplayer"}:
            raise SnapshotError("Publication component directory has an ambiguous name.")
        return "vod"
    return "live"


def _tree(root, *, wrapped=False):
    """Inspect every entry, including empty directories and unexpected links."""
    found, seen, entries = {}, set(), 0
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_TREE_ENTRIES:
                    raise SnapshotError("The snapshot tree exceeds its entry limit.")
                relative = prefix + child.name
                # The on-disk wrapper is outside the public path namespace;
                # preserve EPG/ versus epg/ and the same payload depth limit.
                payload = relative[6:] if wrapped and relative.startswith("files/") else relative
                _relative(payload)
                folded = ("files/" if payload != relative else "") + _path_key(payload)
                if folded in seen:
                    raise SnapshotError("The snapshot contains ambiguous file paths.")
                seen.add(folded)
                path = Path(child.path)
                if child.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise SnapshotError("The snapshot contains a symbolic link or junction.")
                if child.is_dir(follow_symlinks=False):
                    stack.append((path, relative + "/"))
                elif child.is_file(follow_symlinks=False):
                    if len(found) >= 2 * MAX_FILES + 2:
                        raise SnapshotError("The snapshot contains too many files.")
                    found[relative] = path
                else:
                    raise SnapshotError("The snapshot contains a nonregular file.")
    return found


def _copy_verified(source, destination, expected=None):
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("A snapshot artifact is not a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    digest, size = hashlib.sha256(), 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(descriptor, "rb") as origin, destination.open("xb") as target:
        info = os.fstat(origin.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise SnapshotError("A snapshot artifact exceeds its file limit.")
        for chunk in iter(lambda: origin.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_FILE_BYTES or expected is not None and size > expected["bytes"]:
                raise SnapshotError("A snapshot artifact exceeds its declared size.")
            digest.update(chunk)
            target.write(chunk)
    result = {"path": destination.name, "bytes": size, "sha256": digest.hexdigest()}
    if expected is not None and (size != expected["bytes"] or result["sha256"] != expected["sha256"]):
        raise SnapshotError("A snapshot artifact does not match its checksum or size.")
    return result


def _make_manifest(component, files):
    files = sorted(files, key=lambda row: row["path"])
    manifest = {"schemaVersion": SCHEMA_VERSION, "component": component,
                "fileCount": len(files), "totalBytes": sum(row["bytes"] for row in files),
                "files": files}
    return _validate_manifest(manifest, component)


def _validate_manifest(manifest, component):
    if component not in REQUIRED_FILES:
        raise SnapshotError("Unknown publication component.")
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schemaVersion", "component", "fileCount", "totalBytes", "files"}
            or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != SCHEMA_VERSION
            or manifest["component"] != component or not isinstance(manifest["files"], list)
            or type(manifest["fileCount"]) is not int or not 1 <= manifest["fileCount"] <= MAX_FILES
            or len(manifest["files"]) != manifest["fileCount"]
            or type(manifest["totalBytes"]) is not int or not 0 <= manifest["totalBytes"] <= MAX_TOTAL_BYTES):
        raise SnapshotError("The component manifest is invalid or exceeds its limits.")
    previous, names, total = "", set(), 0
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise SnapshotError("A component manifest file entry is invalid.")
        name = _relative(entry["path"])
        key = _path_key(name)
        ancestors = ["/".join(name.split("/")[:depth]) for depth in range(1, len(name.split("/")))]
        if (name <= previous or key in names or any(_path_key(parent) in names for parent in ancestors)
                or _component(name) != component
                or type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES
                or not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"])):
            raise SnapshotError("A component manifest file entry is invalid.")
        previous = name
        names.add(key)
        total += entry["bytes"]
    if total != manifest["totalBytes"] or not REQUIRED_FILES[component] <= {row["path"] for row in manifest["files"]}:
        raise SnapshotError("The component manifest is incomplete or its total is incorrect.")
    return manifest


def _read_manifest(path, component):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise SnapshotError("The component manifest is missing or oversized.")
    return _validate_manifest(json.loads(path.read_bytes()), component)


def _snapshot(root, component):
    root = _directory(root, required=True)
    manifest = _read_manifest(root / "snapshot.json", component)
    tree = _tree(root, wrapped=True)
    expected = {"snapshot.json", *("files/" + entry["path"] for entry in manifest["files"])}
    if set(tree) != expected:
        raise SnapshotError("The snapshot tree does not match its manifest.")
    return root, manifest


def _stage(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{output.name}-snapshot-", dir=output.parent))


def _replace(staging, output):
    """Only validated staging can replace an existing output directory."""
    backup = None
    if output.exists():
        backup = Path(tempfile.mkdtemp(prefix=f".{output.name}-previous-", dir=output.parent))
        backup.rmdir()
        _rename(output, backup)
    try:
        _rename(staging, output)
    except OSError:
        if backup is not None:
            _rename(backup, output)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def _rename(source, destination):
    # Windows scanners can briefly hold a newly closed artifact open. Keep
    # replacement atomic while giving those handles a short bounded grace.
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * 2 ** attempt)


def package(component, source_dir, output_dir):
    """Copy one component from an already validated public tree."""
    source, output = _directory(source_dir, required=True), _directory(output_dir)
    _separate(source, output)
    if component not in REQUIRED_FILES:
        raise SnapshotError("Unknown publication component.")
    staging = _stage(output)
    try:
        files, total = [], 0
        for name, path in sorted(_tree(source).items()):
            if _component(name) != component:
                continue
            entry = _copy_verified(path, staging / "files" / name)
            entry["path"] = name
            files.append(entry)
            total += entry["bytes"]
            if total > MAX_TOTAL_BYTES:
                raise SnapshotError("The component exceeds its total size limit.")
        manifest = _make_manifest(component, files)
        (staging / "snapshot.json").write_bytes(_json_bytes(manifest))
        _replace(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _refresh_epg_references(root):
    path = root / "skytvplayer" / "index.json"
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise SnapshotError("The app index exceeds its size limit.")
    original = path.read_bytes()
    index = json.loads(original)
    if (not isinstance(index, dict) or index.get("schemaVersion") != 1
            or index.get("appName") != "SkyTVPlayer" or not isinstance(index.get("servers"), list)
            or type(index.get("serverCount")) is not int or not index["servers"]
            or len(index["servers"]) != index["serverCount"]):
        raise SnapshotError("The app index identity is invalid.")
    seen, changed = set(), False
    for server in index["servers"]:
        server_id = server.get("serverId") if isinstance(server, dict) else None
        if not isinstance(server_id, str) or not SERVER_ID.fullmatch(server_id) or server_id in seen:
            raise SnapshotError("The app index server identity is invalid.")
        seen.add(server_id)
        references = {}
        for field, suffix in (("programme", "epg"), ("metadata", "metadata"), ("programmeRich", "epg_v2")):
            name = f"{server_id}_{suffix}.json.gz"
            guide = root / "EPG" / name
            if not guide.is_file():
                if field == "programmeRich":
                    continue
                raise SnapshotError("The live snapshot lacks an app EPG artifact.")
            digest = hashlib.sha256()
            with guide.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            references[field] = {"file": f"../EPG/{name}", "sha256": digest.hexdigest(),
                                 "compressedBytes": guide.stat().st_size}
        if server.get("epg") != references:
            server["epg"] = references
            changed = True
    if changed:
        path.write_bytes(_json_bytes(index))
    return changed


def compose(live_dir, vod_dir, output_dir):
    """Combine a fresh component and the last validated other component."""
    live, live_manifest = _snapshot(live_dir, "live")
    vod, vod_manifest = _snapshot(vod_dir, "vod")
    output = _directory(output_dir)
    _separate(live, vod, output)
    staging = _stage(output)
    try:
        manifests = {}
        for source, manifest in ((live, live_manifest), (vod, vod_manifest)):
            files = []
            for entry in manifest["files"]:
                result = _copy_verified(source / "files" / entry["path"], staging / entry["path"], entry)
                result["path"] = entry["path"]
                files.append(result)
            manifests[manifest["component"]] = _make_manifest(manifest["component"], files)
        changed = _refresh_epg_references(staging)
        if changed:
            raw = (staging / "skytvplayer" / "index.json").read_bytes()
            for entry in manifests["vod"]["files"]:
                if entry["path"] == "skytvplayer/index.json":
                    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            manifests["vod"] = _make_manifest("vod", manifests["vod"]["files"])
        manifest_dir = staging / MANIFEST_DIRECTORY
        manifest_dir.mkdir()
        for component, manifest in manifests.items():
            (manifest_dir / f"{component}.json").write_bytes(_json_bytes(manifest))
        _replace(staging, output)
        return {"liveFiles": live_manifest["fileCount"], "vodFiles": vod_manifest["fileCount"],
                "epgReferencesUpdated": changed}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _base_url(value):
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        raise SnapshotError("The publication base URL port is invalid.") from None
    if (parsed.scheme != "https" or not hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port not in (None, 443)
            or hostname.casefold() == "localhost" or hostname.casefold().endswith(".localhost")
            or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname)):
        raise SnapshotError("The publication base URL must use public HTTPS without credentials or queries.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise SnapshotError("The publication base URL cannot be an IP address.")
    if any(part in {".", ".."} for part in parsed.path.split("/")) or "%" in parsed.path or "\\" in parsed.path:
        raise SnapshotError("The publication base URL path is unsafe.")
    return value.rstrip("/") + "/"


def _download(session, url, destination, *, maximum, expected=None, manifest=False):
    with session.get(url, stream=True, timeout=(10, 30), allow_redirects=False,
                     headers={"Accept-Encoding": "identity"}) as response:
        if manifest and response.status_code == 404:
            raise SnapshotNotFound("The deployed component manifest is not available yet.")
        if response.status_code != 200 or urlsplit(response.url) != urlsplit(url):
            raise SnapshotError("A deployed component artifact could not be fetched without a redirect.")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise SnapshotError("A deployed component artifact used an unexpected content encoding.")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > maximum
                or expected is not None and int(length) != expected["bytes"]):
            raise SnapshotError("A deployed component artifact has an invalid declared length.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with destination.open("xb") as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise SnapshotError("A deployed component artifact exceeded its download limit.")
                digest.update(chunk)
                stream.write(chunk)
        if expected is not None and (size != expected["bytes"] or digest.hexdigest() != expected["sha256"]):
            raise SnapshotError("A deployed component artifact failed its checksum or size check.")


def fetch(component, base_url, output_dir):
    """Fetch the deployed manifest and its exact files without provider access."""
    if component not in REQUIRED_FILES:
        raise SnapshotError("Unknown publication component.")
    base, output = _base_url(base_url), _directory(output_dir)
    staging = _stage(output)
    try:
        with requests.Session() as session:
            session.trust_env = False
            _download(session, base + MANIFEST_DIRECTORY + f"/{component}.json", staging / "snapshot.json",
                      maximum=MAX_MANIFEST_BYTES, manifest=True)
        manifest = _read_manifest(staging / "snapshot.json", component)

        def download(entry):
            # A separate session per worker request avoids sharing mutable HTTP
            # cookies/authentication across concurrent connections.
            with requests.Session() as session:
                session.trust_env = False
                _download(session, base + quote(entry["path"], safe="/._-"),
                          staging / "files" / entry["path"], maximum=entry["bytes"], expected=entry)

        with ThreadPoolExecutor(max_workers=4) as executor:
            for _ in executor.map(download, manifest["files"]):
                pass
        _snapshot(staging, component)
        _replace(staging, output)
        return manifest
    except requests.RequestException:
        raise SnapshotError("The deployed component could not be downloaded.") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


package_snapshot = package
fetch_snapshot = fetch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    package_parser = subparsers.add_parser("package")
    package_parser.add_argument("--component", choices=tuple(REQUIRED_FILES), required=True)
    package_parser.add_argument("--source-dir", type=Path, required=True)
    package_parser.add_argument("--output-dir", type=Path, required=True)
    compose_parser = subparsers.add_parser("compose")
    compose_parser.add_argument("--live-dir", type=Path, required=True)
    compose_parser.add_argument("--vod-dir", type=Path, required=True)
    compose_parser.add_argument("--output-dir", type=Path, required=True)
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--component", choices=tuple(REQUIRED_FILES), required=True)
    fetch_parser.add_argument("--base-url", required=True)
    fetch_parser.add_argument("--output-dir", type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    result = {"package": package, "compose": compose, "fetch": fetch}[command](**args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SnapshotNotFound:
        raise SystemExit(3) from None
    except (SnapshotError, OSError, ValueError, TypeError, KeyError):
        print("Publication snapshot failed validation; the existing output was preserved.", file=sys.stderr)
        raise SystemExit(2) from None
