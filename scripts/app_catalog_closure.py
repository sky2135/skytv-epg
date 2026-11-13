"""Validate and retain complete immutable app-catalogue reference closures.

Local preparation only: the caller owns the new candidate tree. The current
index is never replaced, and no publication or activation action is provided.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile

import build_skytvplayer_catalog as catalog
import build_vod_catalogs as vod
import publication_snapshots as snapshots
import series_resume_lookup as lookup
import series_resume_publication as resume


class ClosureError(ValueError):
    pass


TRANSFER_MEMBER_LIMIT = 20_000
RETENTION_RECORD = "app-closure-retention.json"
MAX_RETENTION_RECORD_BYTES = 65_536


def check_transfer_members(component_paths) -> dict:
    """Count payload, directory and private-bundle wrapper ZIP members."""
    files, folded, directories = set(), set(), {"snapshot", "snapshot/files"}
    for name in component_paths:
        snapshots._relative(name)
        if snapshots._component(name) != "vod":
            raise ClosureError("Transfer payload contains another publication component.")
        key = snapshots._path_key(name)
        if name in files or key in folded:
            raise ClosureError("Transfer payload contains duplicate or ambiguous paths.")
        files.add(name)
        folded.add(key)
        pieces = name.split("/")
        directories.update("snapshot/files/" + "/".join(pieces[:n]) for n in range(1, len(pieces)))
    total = len(files) + len(directories) + 2  # snapshot/snapshot.json and report.json
    if any("snapshot/files/" + name in directories for name in files):
        raise ClosureError("Transfer payload aliases a file and directory.")
    if total > TRANSFER_MEMBER_LIMIT:
        raise ClosureError("The complete prepared ZIP exceeds its transfer member limit.")
    return {"payloadFiles": len(files), "directoryMembers": len(directories), "wrapperFiles": 2,
            "totalMembers": total, "memberLimit": TRANSFER_MEMBER_LIMIT}


def _file(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ClosureError("A catalogue closure file is missing or linked.")
    size = path.stat().st_size
    if size > snapshots.MAX_FILE_BYTES:
        raise ClosureError("A catalogue closure file exceeds the publication limit.")
    digest, received = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            received += len(block)
            if received > snapshots.MAX_FILE_BYTES:
                raise ClosureError("A catalogue closure file exceeds the publication limit.")
            digest.update(block)
    if size != received:
        raise ClosureError("A catalogue closure file changed while being measured.")
    return {"bytes": size, "sha256": digest.hexdigest()}


def _read(directory: Path, reference: dict) -> dict:
    return lookup._read_artifact(directory, reference, compressed_limit=catalog.MAX_JSON_BYTES,
        expanded_limit=catalog.MAX_JSON_BYTES, optional={"providerCategoryIds", "languages"})


def collect_app_closure(app_dir: Path, *, expected_servers=None, index=None) -> dict:
    """Return exact file metadata for one fully validated descriptor graph.

    ``index`` may be a previously accepted parsed descriptor whose hashed files
    are retained in this directory; it does not alter the on-disk index. The
    ordinary index bytes are included only when reading that index directly.
    Live EPG references belong to the separate live publication component.
    """
    try:
        directory = snapshots._directory(lookup._io_path(app_dir), required=True)
        index_path = directory / "index.json"
        if index is None:
            if _file(index_path)["bytes"] > snapshots.MAX_INDEX_BYTES:
                raise ClosureError("The app index exceeds the publication limit.")
            raw = index_path.read_bytes()
            document = lookup._strict_json(raw)
            files = {"index.json": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}
        else:
            document = copy.deepcopy(index)
            raw = lookup._json_bytes(document)
            if len(raw) > snapshots.MAX_INDEX_BYTES:
                raise ClosureError("The app descriptor exceeds the publication limit.")
            files = {}
        if (not isinstance(document, dict) or type(document.get("schemaVersion")) is not int
                or document["schemaVersion"] != 1 or document.get("appName") != "SkyTVPlayer"
                or not isinstance(document.get("servers"), list) or type(document.get("serverCount")) is not int
                or len(document["servers"]) != document["serverCount"] or not document["servers"]):
            raise ClosureError("The app descriptor identity is invalid.")
        observed = [server.get("serverId") for server in document["servers"] if isinstance(server, dict)]
        selected = tuple(expected_servers) if expected_servers is not None else tuple(observed)
        if len(observed) != len(document["servers"]) or len(set(observed)) != len(observed) or sorted(observed) != sorted(selected):
            raise ClosureError("The app descriptor server set is invalid.")
        ordinary_validation = catalog.validate(directory, expected_servers=selected, index=document)
        lookup_validation = resume.validate_optional_graphs(directory, expected_servers=selected, index=document)

        def read(reference):
            value = _read(directory, reference)
            name = reference["file"]
            metadata = {"bytes": reference["compressedBytes"], "sha256": reference["sha256"]}
            if name in files and files[name] != metadata:
                raise ClosureError("One app artifact has conflicting reference bytes.")
            files[name] = metadata
            return value

        lookup_count = 0
        for server in document["servers"]:
            for kind in ("movies", "series"):
                manifest = read(server[kind])
                for reference in manifest["shards"]:
                    shard = read(reference)
                    if kind == "series":
                        for parent in shard["items"]:
                            for episode_reference in parent["episodeFiles"]:
                                read(episode_reference)
            read(server["home"])
            if "seriesResumeLookup" in server:
                reference = server["seriesResumeLookup"]
                root = read(reference)
                for table in ("seriesBuckets", "episodeBuckets"):
                    for reference in root[table].values():
                        if reference.get("empty") is not True:
                            read(reference)
                lookup_count += 1
        return {"files": files, "indexSha256": hashlib.sha256(raw).hexdigest(),
            "validation": {"app": ordinary_validation, "lookup": lookup_validation},
            "metrics": {"serverCount": len(selected), "lookupServerCount": lookup_count,
                "closureFileCount": len(files), "closureBytes": sum(row["bytes"] for row in files.values())}}
    except ClosureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, vod.CatalogBuildError) as error:
        raise ClosureError("The complete app reference closure could not be validated.") from error


def _entry_count(root: Path) -> int:
    total, stack = 0, [root]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                total += 1
                if total > snapshots.MAX_TREE_ENTRIES:
                    raise ClosureError("The retained component tree exceeds its entry limit.")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    return total


def _hashed_name(name: str, metadata: dict) -> bool:
    """Recognize public artifact names and bind their prefix to actual bytes."""
    if not catalog.ARTIFACT_NAME.fullmatch(name):
        return False
    snapshots._relative("skytvplayer/" + name)
    prefix = name.split(".")[-3 if name.endswith(".gz") else -2]
    if prefix != metadata["sha256"][:16]:
        raise ClosureError("A retained app artifact does not match its filename checksum.")
    return True


def _projection(index: dict) -> dict:
    """Only catalogue ownership facts bind a retention generation, never EPG/stats."""
    document = {key: copy.deepcopy(index[key]) for key in ("schemaVersion", "appName", "generatedAt", "serverCount")}
    if "seriesMetadataVersion" in index:
        document["seriesMetadataVersion"] = index["seriesMetadataVersion"]
    fields = ("serverId", "movieCount", "seriesCount", "movies", "series", "home")
    document["servers"] = []
    for server in sorted(index["servers"], key=lambda value: value["serverId"]):
        entry = {key: copy.deepcopy(server[key]) for key in fields}
        if "seriesResumeLookup" in server:
            entry["seriesResumeLookup"] = copy.deepcopy(server["seriesResumeLookup"])
        document["servers"].append(entry)
    return document


def _projection_sha(index: dict) -> str:
    return hashlib.sha256(lookup._json_bytes(_projection(index))).hexdigest()


def _record(directory: Path):
    path = directory / RETENTION_RECORD
    if not path.exists() and not path.is_symlink():
        return None
    if _file(path)["bytes"] > MAX_RETENTION_RECORD_BYTES:
        raise ClosureError("The bounded retention record exceeds its byte limit.")
    record = lookup._strict_json(path.read_bytes())
    fields = {"schemaVersion", "kind", "anchor", "remainingFollowupGenerations", "currentProjectionSha256"}
    if (set(record) != fields or type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1
            or record["kind"] != "skytv-app-closure-retention-v1"
            or type(record["remainingFollowupGenerations"]) is not int
            or record["remainingFollowupGenerations"] not in (0, 1)
            or not isinstance(record["currentProjectionSha256"], str)
            or not snapshots.SHA256.fullmatch(record["currentProjectionSha256"])):
        raise ClosureError("The bounded retention record is invalid.")
    anchor = record["anchor"]
    if anchor is None:
        if record["remainingFollowupGenerations"] != 0:
            raise ClosureError("An empty retention anchor has a nonempty budget.")
    elif not isinstance(anchor, dict) or lookup._json_bytes(_projection(anchor)) != lookup._json_bytes(anchor):
        raise ClosureError("The retention anchor is not a compact catalogue descriptor.")
    return record


def validate_retention_owner(app_dir: Path, index=None):
    """Read-only shape/owner check; an optional graph may be withdrawn later.

    The caller independently validates and pins ordinary inputs. This check
    deliberately does not open an optional graph which compensation may remove.
    """
    try:
        directory = snapshots._directory(lookup._io_path(app_dir), required=True)
        record = _record(directory)
        if record is None:
            return None
        if index is None:
            path = directory / "index.json"
            if _file(path)["bytes"] > snapshots.MAX_INDEX_BYTES:
                raise ClosureError("The retention owner index exceeds its byte limit.")
            document = lookup._strict_json(path.read_bytes())
        else:
            document = copy.deepcopy(index)
            if len(lookup._json_bytes(document)) > snapshots.MAX_INDEX_BYTES:
                raise ClosureError("The retention owner descriptor exceeds its byte limit.")
        identifiers = tuple(server["serverId"] for server in document["servers"])
        resume._index(document, identifiers)
        if record["currentProjectionSha256"] != _projection_sha(document):
            raise ClosureError("The retention record belongs to another catalogue projection.")
        return copy.deepcopy(record)
    except ClosureError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ClosureError("The retention record owner could not be validated.") from error


def _lookup_only_origin(current: dict, origin: dict) -> str:
    """Authorize only the optional-reference delta, retaining every other fact."""
    if len(lookup._json_bytes(origin)) > snapshots.MAX_INDEX_BYTES:
        raise ClosureError("The lookup-change origin exceeds the index byte limit.")
    resume._index(origin, tuple(server["serverId"] for server in current["servers"]))
    versions = []
    for document in (current, origin):
        ordinary = copy.deepcopy(document)
        for server in ordinary["servers"]:
            server.pop("seriesResumeLookup", None)
        versions.append(lookup._json_bytes(ordinary))
    if versions[0] != versions[1]:
        raise ClosureError("The authorized lookup change modified ordinary index facts.")
    return _projection_sha(origin)


def _write_record(path: Path, raw: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".closure-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _measure(candidate: Path, app_files: dict, additional: int, removed: int = 0) -> dict:
    component = {"skytvplayer/" + name: metadata for name, metadata in app_files.items()}
    tree_entries = _entry_count(candidate) + additional - removed + 3
    sibling = candidate.parent / "VOD"
    if sibling.exists():
        for name, path in snapshots._tree(snapshots._directory(sibling, required=True)).items():
            component["VOD/" + name] = _file(path)
        tree_entries += _entry_count(sibling) + 1
    if len(component) > snapshots.MAX_FILES or tree_entries > snapshots.MAX_TREE_ENTRIES:
        raise ClosureError("The retained component exceeds its file or tree-entry limit.")
    names = set()
    for name in component:
        snapshots._relative(name)
        key = snapshots._path_key(name)
        if key in names:
            raise ClosureError("The retained component has ambiguous file names.")
        names.add(key)
    files = [{"path": name, **metadata} for name, metadata in sorted(component.items())]
    total = sum(row["bytes"] for row in files)
    manifest = {"schemaVersion": 1, "component": "vod", "fileCount": len(files), "totalBytes": total, "files": files}
    if snapshots.REQUIRED_FILES["vod"] <= set(component):
        snapshots._validate_manifest(manifest, "vod")
    manifest_bytes = len(snapshots._json_bytes(manifest))
    if manifest_bytes > snapshots.MAX_MANIFEST_BYTES or total + manifest_bytes > snapshots.MAX_TOTAL_BYTES:
        raise ClosureError("The retained component exceeds its manifest or total byte limit.")
    if app_files["index.json"]["bytes"] > snapshots.MAX_INDEX_BYTES:
        raise ClosureError("The retained app index exceeds its byte limit.")
    transfer = check_transfer_members(component)
    return {"retainedAppFiles": len(app_files), "componentFiles": len(files), "componentTreeEntries": tree_entries,
        "componentPayloadBytes": total, "serializedManifestBytes": manifest_bytes,
        "estimatedSnapshotBytes": total + manifest_bytes, "includesSiblingVod": sibling.exists(), "transferMembers": transfer}


def retain_app_closures(candidate_app_dir: Path, last_good_app_dir: Path, *, expected_servers=None,
                        compensation_index=None, lookup_change_source_index=None) -> dict:
    """Keep current + last-good current + at most one bounded compensation anchor.

    Graph validation, collision checks and actual retained-tree cap estimates
    precede every copy. The caller must discard this private candidate if a
    later disk error occurs; no published or last-good tree is ever replaced.
    """
    try:
        candidate = snapshots._directory(lookup._io_path(candidate_app_dir), required=True)
        old = snapshots._directory(lookup._io_path(last_good_app_dir), required=True)
        snapshots._separate(candidate, old)
        current_graph = collect_app_closure(candidate, expected_servers=expected_servers)
        old_graph = collect_app_closure(old, expected_servers=expected_servers)
        actual = {name: _file(path) for name, path in snapshots._tree(candidate).items()}
        original_index = (candidate / "index.json").read_bytes()
        current_index = lookup._strict_json(original_index)
        previous_index = lookup._strict_json((old / "index.json").read_bytes())
        current_sha, previous_sha = _projection_sha(current_index), _projection_sha(previous_index)
        old_record, candidate_record = _record(old), _record(candidate)
        origin_sha, origin_owned = None, False
        if lookup_change_source_index is not None:
            origin_sha = _lookup_only_origin(current_index, lookup_change_source_index)
            if candidate_record is None or candidate_record["currentProjectionSha256"] not in {origin_sha, current_sha}:
                raise ClosureError("The lookup change has no validated original retention owner.")
            origin_owned = candidate_record["currentProjectionSha256"] == origin_sha
        if old_record is None and compensation_index is None:
            unowned_roots = [name for name in snapshots._tree(old)
                if catalog.ARTIFACT_NAME.fullmatch(name) and "_resume_index" in name
                and name not in old_graph["files"]]
            if unowned_roots:
                raise ClosureError("Historical lookup roots require their bounded retention record.")
        if old_record is not None and old_record["currentProjectionSha256"] != previous_sha:
            raise ClosureError("The last-good retention record belongs to another catalogue projection.")
        if (candidate_record is not None and candidate_record["currentProjectionSha256"] != current_sha
                and not origin_owned and candidate_record != old_record):
            raise ClosureError("The candidate retention record has no current or last-good owner.")
        inherited = (candidate_record if candidate_record is not None
                     and (candidate_record["currentProjectionSha256"] == current_sha or origin_owned)
                     else old_record or candidate_record)
        anchor, remaining = None, 0
        if compensation_index is not None and any("seriesResumeLookup" in s for s in compensation_index["servers"]):
            anchor, remaining = _projection(compensation_index), 1
        elif inherited is not None and inherited["anchor"] is not None:
            if inherited["currentProjectionSha256"] == current_sha or origin_owned:
                anchor, remaining = inherited["anchor"], inherited["remainingFollowupGenerations"]
            elif inherited["remainingFollowupGenerations"] == 1:
                anchor, remaining = inherited["anchor"], 0
        record = {"schemaVersion": 1, "kind": "skytv-app-closure-retention-v1", "anchor": anchor,
            "remainingFollowupGenerations": remaining, "currentProjectionSha256": current_sha}
        record_bytes = lookup._json_bytes(record)
        if len(record_bytes) > MAX_RETENTION_RECORD_BYTES:
            raise ClosureError("The bounded compensation anchor exceeds its byte limit.")

        graphs = [(candidate, current_graph), (old, old_graph)]
        if anchor is not None:
            anchor_sha = _projection_sha(anchor)
            if anchor_sha not in {current_sha, previous_sha}:
                try:
                    anchor_graph = collect_app_closure(candidate, expected_servers=expected_servers, index=anchor)
                    graphs.append((candidate, anchor_graph))
                except ClosureError:
                    try:
                        graphs.append((old, collect_app_closure(old, expected_servers=expected_servers, index=anchor)))
                    except ClosureError as error:
                        raise ClosureError("The required compensation anchor closure is incomplete.") from error
        selected, sources = {}, {}
        for directory, graph in graphs:
            for name, metadata in graph["files"].items():
                if name == "index.json":
                    continue
                if not _hashed_name(name, metadata):
                    raise ClosureError("A selected closure artifact is not content-addressed.")
                if name in selected and selected[name] != metadata:
                    raise ClosureError("Selected app closures have conflicting artifact bytes.")
                selected[name], sources[name] = metadata, directory / name
        merged = {name: metadata for name, metadata in actual.items()
                  if not catalog.ARTIFACT_NAME.fullmatch(name) and name != RETENTION_RECORD}
        plans = []
        for name, metadata in selected.items():
            if name in actual:
                if actual[name] != metadata:
                    raise ClosureError("Retained app artifacts have conflicting bytes.")
            else:
                plans.append((name, metadata))
            merged[name] = metadata
        pruned = {name: meta for name, meta in actual.items()
                  if catalog.ARTIFACT_NAME.fullmatch(name) and name not in selected}
        merged[RETENTION_RECORD] = {"bytes": len(record_bytes), "sha256": hashlib.sha256(record_bytes).hexdigest()}
        added_record = int(RETENTION_RECORD not in actual)
        measured = _measure(candidate, merged, len(plans) + added_record, len(pruned))
        for name, metadata in plans:
            snapshots._copy_verified(sources[name], candidate / name, metadata)
        for name, metadata in pruned.items():
            if _file(candidate / name) != metadata:
                raise ClosureError("An unselected private candidate artifact changed before pruning.")
            (candidate / name).unlink()
        _write_record(candidate / RETENTION_RECORD, record_bytes)
        if (candidate / "index.json").read_bytes() != original_index:
            raise ClosureError("The candidate app index changed during retention.")
        return {**measured, "copiedFiles": len(plans), "copiedBytes": sum(meta["bytes"] for _name, meta in plans),
            "retentionGrowthFiles": len(plans), "retentionGrowthBytes": sum(meta["bytes"] for _name, meta in plans),
            "protectedClosureCount": len(graphs), "compensationAnchorRetained": anchor is not None,
            "remainingFollowupGenerations": remaining, "currentProjectionSha256": current_sha,
            "prunedPrivateCandidateFiles": len(pruned), "prunedPrivateCandidateBytes": sum(x["bytes"] for x in pruned.values()),
            "automaticPruning": True, "pruningPolicy": "current_last_good_and_one_compensation_followup_only",
            "candidateClosure": current_graph["metrics"], "lastGoodClosure": old_graph["metrics"],
            "candidateIndexSha256": current_graph["indexSha256"], "lastGoodIndexSha256": old_graph["indexSha256"],
            "candidateIndexUnchanged": True, "publicationActivated": False}
    except ClosureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, vod.CatalogBuildError) as error:
        raise ClosureError("The app closures could not be retained in the private candidate.") from error
