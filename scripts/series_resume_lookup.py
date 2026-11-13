"""Standalone, opt-in exact-series resume artifacts from a published snapshot.

This module performs local file IO only. It never imports API clients, changes
the source publication, installs an index reference, or decides account policy.
Unknown classifications and partial inventories are copied without coercion.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class ResumeLookupError(ValueError):
    """The candidate is incomplete, inconsistent, unsupported, or over limit."""


@dataclass(frozen=True)
class LookupLimits:
    root_compressed_bytes: int = 65_536
    root_expanded_bytes: int = 262_144
    bucket_compressed_bytes: int = 65_536
    bucket_expanded_bytes: int = 262_144
    max_parent_rows: int = 2_048
    max_episode_rows: int = 8_192


DEFAULT_LIMITS = LookupLimits()
SLOTS = tuple(f"{value:02x}" for value in range(256))
HASH_ALGORITHM = "sha256-domain-nul-first-byte-v1"
PARENT_FIELDS = (
    "section", "id", "providerSeriesId", "contentId", "title", "category", "adult",
    "genre", "genres", "language", "languages", "originalLanguage",
    "isMultilingual", "languageListComplete", "languageSource", "episodesStatus",
    "availableEpisodeCount", "serverEpisodeCount", "episodeInventoryComplete",
    "episodeInventoryCheckedAt",
)
EPISODE_FIELDS = (
    "providerSeriesId", "providerEpisodeId", "seasonNumber", "episodeNumber",
    "containerExtension", "title", "durationSeconds",
)
_SOURCE_EPISODE_FIELDS = {
    "id", "seasonNumber", "episodeNumber", "title", "containerExtension",
    "addedAt", "airDate", "overview", "durationSeconds", "stillUrl",
}
_REF_FIELDS = {"file", "sha256", "compressedBytes", "uncompressedBytes", "recordCount"}
_ROOT_FIELDS = {
    "schemaVersion", "dataset", "serverId", "seriesManifestSha256", "hashAlgorithm",
    "seriesRecords", "episodeRecords", "seriesBuckets", "episodeBuckets",
}
_CHILD_FIELDS = {"schemaVersion", "dataset", "serverId", "bucket", "recordCount", "items"}
_SOURCE_MAX_BYTES = 64 * 1024 * 1024
_SOURCE_MAX_COMPRESSED = 512 * 1024
_SOURCE_MAX_ROWS = 250_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")
_SERVER = re.compile(r"server_[1-9][0-9]?\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_FILE = re.compile(r"[a-z0-9_.-]+\.[a-f0-9]{16}\.json(?:\.gz)?\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}\Z")
LANGUAGE_CODES = frozenset("ar bn cs da de el en es fa fi fr gu he hi hu id it ja km kn ko lo ml mr ms mul ne nl no pa pl pt ro ru si sk so sv sw ta te th tl tr uk und ur vi zh".split())
_ADULT_EVIDENCE = re.compile(r"(?:^|[^a-z0-9])(?:adult|xxx|18\+|porn)(?:$|[^a-z0-9])", re.I)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ResumeLookupError(message)


def _io_path(value: str | Path) -> Path:
    """Use long-path-capable Windows disk paths without changing wire names.

    Apply before filesystem operations, including safety checks: an ordinary
    MAX_PATH-limited stat may report a real deep file as missing. Both sides of
    resolve/parent comparisons consequently retain the same extended namespace.
    Device namespaces other than absolute disk/UNC paths are not accepted.
    """
    if os.name != "nt":
        return Path(value)
    raw = os.path.normpath(os.path.abspath(os.fspath(value)))
    if raw.startswith("\\\\.\\"):
        raise ResumeLookupError("Device paths are not resume artifact directories.")
    if raw.startswith("\\\\?\\"):
        tail = raw[4:]
        _require(re.match(r"[A-Za-z]:\\", tail) is not None or tail.upper().startswith("UNC\\"),
                 "Unsupported extended artifact directory.")
        if tail.upper().startswith("UNC\\"):
            raw = "\\\\?\\UNC\\" + tail[4:]
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    _require(re.match(r"[A-Za-z]:\\", raw) is not None, "Artifact directory is not an absolute disk path.")
    return Path("\\\\?\\" + raw)


def _keys(value: object, required: set[str], label: str, optional: set[str] = frozenset()) -> dict:
    _require(isinstance(value, dict) and required <= value.keys()
             and not value.keys() - required - optional, f"Invalid {label} fields.")
    return value


def _integer(value: object, maximum: int, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _identity(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _text(value: object, maximum: int, *, nullable: bool = False) -> bool:
    return value is None and nullable or (
        isinstance(value, str) and 0 < len(value) <= maximum and value == value.strip()
        and unicodedata.normalize("NFC", value) == value
        and all(ord(character) >= 32 for character in value)
    )


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
        return result if 1970 <= result.year <= 2200 else None
    except ValueError:
        return None


def _limits(limits: LookupLimits) -> None:
    _require(isinstance(limits, LookupLimits), "Invalid resume lookup limits.")
    for name, ceiling in vars(DEFAULT_LIMITS).items():
        _require(_integer(getattr(limits, name), ceiling, minimum=1), "Invalid resume lookup limits.")


def _identity_bytes(server_id: str, series_id: str, episode_id: str | None = None) -> bytes:
    _require(isinstance(server_id, str) and _SERVER.fullmatch(server_id) is not None,
             "Invalid resume server identity.")
    _require(_identity(series_id) and (episode_id is None or _identity(episode_id)),
             "Invalid resume provider identity.")
    domain = "skytv-resume-series-v1" if episode_id is None else "skytv-resume-episode-v1"
    parts = [domain, server_id, series_id] + ([] if episode_id is None else [episode_id])
    return "\0".join(parts).encode("utf-8")


def series_bucket(server_id: str, series_id: str) -> str:
    return hashlib.sha256(_identity_bytes(server_id, series_id)).hexdigest()[:2]


def episode_bucket(server_id: str, series_id: str, episode_id: str) -> str:
    return hashlib.sha256(_identity_bytes(server_id, series_id, episode_id)).hexdigest()[:2]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate resume JSON key.")
            result[key] = value
        return result
    def constant(_value):
        raise ResumeLookupError("Non-finite resume JSON number.")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ResumeLookupError("Invalid artifact JSON.") from exc
    _require(isinstance(result, dict), "Artifact JSON must be an object.")
    return result


def _ref(reference: object, *, compressed_limit: int, expanded_limit: int,
         optional: set[str] = frozenset()) -> dict:
    reference = _keys(reference, _REF_FIELDS, "artifact reference", optional)
    name, digest = reference["file"], reference["sha256"]
    _require(isinstance(name, str) and _FILE.fullmatch(name) is not None
             and isinstance(digest, str) and _HASH.fullmatch(digest) is not None,
             "Invalid artifact path or checksum.")
    _require(name.split(".")[-3 if name.endswith(".gz") else -2] == digest[:16],
             "Artifact filename checksum mismatch.")
    _require(_integer(reference["compressedBytes"], compressed_limit, minimum=1)
             and _integer(reference["uncompressedBytes"], expanded_limit, minimum=1)
             and _integer(reference["recordCount"], 1_000_000), "Artifact reference exceeds its limits.")
    if not name.endswith(".gz"):
        _require(reference["compressedBytes"] == reference["uncompressedBytes"],
                 "Plain artifact byte counts disagree.")
    return reference


def _read_artifact(directory: Path, reference: Mapping[str, Any], *, compressed_limit: int,
                   expanded_limit: int, optional: set[str] = frozenset()) -> dict:
    reference = _ref(reference, compressed_limit=compressed_limit, expanded_limit=expanded_limit, optional=optional)
    try:
        directory = _io_path(directory).resolve(strict=True)
        path = directory / reference["file"]
        _require(not path.is_symlink() and path.is_file() and path.resolve().parent == directory,
                 "Referenced artifact is missing or outside its directory.")
        _require(path.stat().st_size == reference["compressedBytes"], "Artifact compressed size mismatch.")
        with path.open("rb") as handle:
            data = handle.read(compressed_limit + 1)
        _require(len(data) == reference["compressedBytes"]
                 and hashlib.sha256(data).hexdigest() == reference["sha256"], "Artifact checksum mismatch.")
        if path.name.endswith(".gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
                raw = handle.read(expanded_limit + 1)
        else:
            raw = data
        _require(len(raw) <= expanded_limit and len(raw) == reference["uncompressedBytes"],
                 "Artifact expanded size mismatch.")
        return _strict_json(raw)
    except ResumeLookupError:
        raise
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise ResumeLookupError("Could not read referenced artifact safely.") from exc


def _parent_projection(item: object, server_id: str, generated_at: str) -> dict:
    _require(isinstance(item, dict) and set(PARENT_FIELDS) - {"section"} <= item.keys(),
             "Source parent lacks required safety fields.")
    row = {key: copy.deepcopy(item[key]) for key in PARENT_FIELDS if key != "section"}
    row["section"] = "series"
    identity = row["id"]
    _require(_identity(identity) and row["providerSeriesId"] == identity
             and row["contentId"] == f"{server_id}:series:{identity}", "Source parent identity mismatch.")
    _require(_text(row["title"], 300), "Invalid parent title.")
    category = _keys(row["category"], {"id", "name"}, "parent category")
    _require((category["id"] is None or _identity(category["id"]))
             and _text(category["name"], 160, nullable=True), "Invalid parent category.")
    _require(row["adult"] is None or type(row["adult"]) is bool, "Invalid parent adult classification.")
    genres = row["genres"]
    _require(isinstance(row["genre"], str) and isinstance(genres, list) and len(genres) <= 12
             and all(_text(value, 100) for value in genres) and len(set(genres)) == len(genres)
             and ((row["genre"] == "unknown" and not genres) or (bool(genres) and row["genre"] == genres[0])),
             "Invalid parent genre classification.")
    _require(row["adult"] is True or not ("adult" in genres or isinstance(category["name"], str)
             and _ADULT_EVIDENCE.search(category["name"])), "Source parent contradicts strong adult evidence.")
    language, languages = row["language"], row["languages"]
    _require(isinstance(language, str) and language in LANGUAGE_CODES
             and isinstance(languages, list) and 1 <= len(languages) <= 100
             and all(isinstance(code, str) and code in LANGUAGE_CODES for code in languages)
             and len(set(languages)) == len(languages), "Invalid parent language classification.")
    _require((language == "und" and languages == ["und"])
             or (language == "mul" and len(languages) >= 2 and not {"und", "mul"}.intersection(languages))
             or (language not in {"und", "mul"} and languages == [language]),
             "Inconsistent parent language vector.")
    _require(row["originalLanguage"] is None or isinstance(row["originalLanguage"], str)
             and _LANGUAGE.fullmatch(row["originalLanguage"]) is not None, "Invalid original language.")
    _require(type(row["isMultilingual"]) is bool and type(row["languageListComplete"]) is bool
             and row["languageSource"] in {"provider_audio", "title_suffix", "provider_multilingual", "verified_original", "unknown"}
             and (language != "mul" or row["isMultilingual"])
             and (language != "und" or not row["languageListComplete"])
             and (not row["isMultilingual"] or not row["languageListComplete"] or len(languages) >= 2),
             "Invalid parent language evidence.")
    _require(row["episodesStatus"] in {"available", "stale", "pending", "unavailable"}
             and _integer(row["availableEpisodeCount"], 50_000)
             and (row["serverEpisodeCount"] is None or _integer(row["serverEpisodeCount"], 50_000))
             and type(row["episodeInventoryComplete"]) is bool, "Invalid parent inventory facts.")
    checked = row["episodeInventoryCheckedAt"]
    _require(checked is None or _utc(checked) is not None and _utc(checked) <= _utc(generated_at),
             "Invalid or future parent inventory timestamp.")
    _require((row["serverEpisodeCount"] is None or checked is not None)
             and (not row["episodeInventoryComplete"] or row["serverEpisodeCount"] is not None and checked is not None),
             "Incomplete parent inventory evidence.")
    return row


def _episode_projection(item: object, series_id: str) -> dict:
    item = _keys(item, _SOURCE_EPISODE_FIELDS, "source episode")
    _require(_identity(item["id"]) and _integer(item["seasonNumber"], 100_000)
             and _integer(item["episodeNumber"], 100_000) and _text(item["title"], 300)
             and re.search(r"https?://", item["title"], re.I) is None, "Invalid source episode identity or title.")
    extension, duration = item["containerExtension"], item["durationSeconds"]
    _require(extension is None or isinstance(extension, str) and re.fullmatch(r"[a-z0-9]{1,8}", extension) is not None,
             "Invalid source episode extension.")
    _require(duration is None or _integer(duration, 604_800), "Invalid source episode duration.")
    return {"providerSeriesId": series_id, "providerEpisodeId": item["id"],
            **{key: item[key] for key in EPISODE_FIELDS if key not in {"providerSeriesId", "providerEpisodeId"}}}


def _source_rows(source_dir: Path, server_id: str, series_ref: Mapping[str, Any], limits: LookupLimits):
    _require(isinstance(server_id, str) and _SERVER.fullmatch(server_id) is not None, "Invalid source server.")
    manifest = _read_artifact(source_dir, series_ref, compressed_limit=_SOURCE_MAX_BYTES, expanded_limit=_SOURCE_MAX_BYTES)
    _keys(manifest, {"schemaVersion", "serverId", "dataset", "generatedAt", "totalRecords", "categories", "shards"},
          "source manifest", {"seriesMetadataVersion"})
    _require(type(manifest["schemaVersion"]) is int and manifest["schemaVersion"] == 1
             and manifest["serverId"] == server_id and manifest["dataset"] == "vod-series"
             and _utc(manifest["generatedAt"]) is not None
             and _integer(manifest["totalRecords"], _SOURCE_MAX_ROWS)
             and isinstance(manifest["categories"], list) and isinstance(manifest["shards"], list)
             and len(manifest["shards"]) == series_ref["recordCount"]
             and len(manifest["shards"]) <= manifest["totalRecords"], "Invalid source manifest identity or counts.")
    if "seriesMetadataVersion" in manifest:
        _require(type(manifest["seriesMetadataVersion"]) is int and manifest["seriesMetadataVersion"] == 2,
                 "Unsupported source series metadata version.")
    parents, episodes, seen_files = {}, {}, set()
    bucket_counts = {kind: dict.fromkeys(SLOTS, 0) for kind in ("series", "episodes")}
    bucket_bytes = {kind: dict.fromkeys(SLOTS, 0) for kind in ("series", "episodes")}

    def reserve(kind, slot, row):
        count = bucket_counts[kind][slot]
        maximum = limits.max_parent_rows if kind == "series" else limits.max_episode_rows
        _require(count < maximum, "Source projection exceeds its resume bucket record limit.")
        # Rows and separating commas are a lower bound on the final JSON
        # envelope. Reject obvious overflow before retaining more source rows;
        # final encoded envelope/compression checks remain authoritative.
        size = bucket_bytes[kind][slot] + len(_json_bytes(row)) + int(count > 0)
        _require(size <= limits.bucket_expanded_bytes, "Source projection exceeds its resume bucket byte limit.")
        bucket_counts[kind][slot] = count + 1
        bucket_bytes[kind][slot] = size
    for reference in manifest["shards"]:
        _require(isinstance(reference, dict) and reference.get("file") not in seen_files, "Duplicate source parent shard.")
        seen_files.add(reference.get("file"))
        shard = _read_artifact(source_dir, reference, compressed_limit=_SOURCE_MAX_COMPRESSED,
                               expanded_limit=_SOURCE_MAX_BYTES, optional={"providerCategoryIds", "languages"})
        _keys(shard, {"schemaVersion", "serverId", "dataset", "generatedAt", "recordCount", "items"}, "source parent shard")
        rows = shard["items"]
        _require(type(shard["schemaVersion"]) is int and shard["schemaVersion"] == 1
                 and shard["serverId"] == server_id and shard["dataset"] == "vod-series"
                 and shard["generatedAt"] == manifest["generatedAt"] and isinstance(rows, list)
                 and 1 <= len(rows) <= 500 and type(shard["recordCount"]) is int
                 and shard["recordCount"] == len(rows) == reference["recordCount"], "Invalid source parent shard identity or count.")
        for item in rows:
            parent = _parent_projection(item, server_id, manifest["generatedAt"])
            sid = parent["id"]
            _require(sid not in parents, "Duplicate source parent identity.")
            reserve("series", series_bucket(server_id, sid), parent)
            parents[sid] = parent
            _require(isinstance(item.get("episodes"), list) and isinstance(item.get("episodeFiles"), list)
                     and not (item["episodes"] and item["episodeFiles"]) and len(item["episodes"]) <= 500,
                     "Invalid source inline or external inventory.")
            logical, previous, inventory_count = set(), None, 0

            def retain_episode(episode):
                nonlocal previous, inventory_count
                projected = _episode_projection(episode, sid)
                identity = (sid, projected["providerEpisodeId"])
                _require(identity not in episodes, "Duplicate source episode identity.")
                order = (projected["seasonNumber"], projected["episodeNumber"], projected["providerEpisodeId"])
                _require(previous is None or previous < order, "Source episode inventory is out of order.")
                _require(inventory_count < parent["availableEpisodeCount"] and inventory_count < 50_000,
                         "Source inventory exceeds its declared record count.")
                reserve("episodes", episode_bucket(server_id, *identity), projected)
                previous = order
                logical.add(order[:2])
                inventory_count += 1
                episodes[identity] = projected

            for episode in item["episodes"]:
                retain_episode(episode)
            episode_files = set()
            for episode_ref in item["episodeFiles"]:
                _require(isinstance(episode_ref, dict) and episode_ref.get("file") not in episode_files,
                         "Duplicate source episode shard.")
                episode_files.add(episode_ref.get("file"))
                payload = _read_artifact(source_dir, episode_ref, compressed_limit=_SOURCE_MAX_COMPRESSED, expanded_limit=_SOURCE_MAX_BYTES)
                _keys(payload, {"schemaVersion", "serverId", "dataset", "providerSeriesId", "generatedAt", "recordCount", "items"}, "source episode shard")
                _require(type(payload["schemaVersion"]) is int and payload["schemaVersion"] == 1
                         and payload["serverId"] == server_id and payload["dataset"] == "vod-episodes"
                         and payload["providerSeriesId"] == sid and payload["generatedAt"] == manifest["generatedAt"]
                         and isinstance(payload["items"], list) and 1 <= len(payload["items"]) <= 500
                         and type(payload["recordCount"]) is int
                         and payload["recordCount"] == len(payload["items"]) == episode_ref["recordCount"],
                         "Invalid source episode shard identity or count.")
                for episode in payload["items"]:
                    retain_episode(episode)
            _require(inventory_count == parent["availableEpisodeCount"], "Source inventory record count mismatch.")
            if parent["episodeInventoryComplete"]:
                _require(parent["serverEpisodeCount"] == len(logical), "Complete source logical episode count mismatch.")
        if "providerCategoryIds" in reference:
            _require(reference["providerCategoryIds"] == sorted({row["category"]["id"] for row in rows if row["category"]["id"]}),
                     "Source category hint mismatch.")
        if "languages" in reference:
            _require(reference["languages"] == sorted({code for row in rows for code in row["languages"]}), "Source language hint mismatch.")
    _require(len(parents) == manifest["totalRecords"], "Source parent coverage mismatch.")
    return parents, episodes


def _encoded(prefix: str, payload: dict, count: int, compressed_limit: int, expanded_limit: int):
    raw = _json_bytes(payload)
    _require(len(raw) <= expanded_limit, "Resume artifact exceeds its expanded byte limit.")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as handle:
        handle.write(raw)
    data = buffer.getvalue()
    _require(len(data) <= compressed_limit, "Resume artifact exceeds its compressed byte limit.")
    digest = hashlib.sha256(data).hexdigest()
    reference = {"file": f"{prefix}.{digest[:16]}.json.gz", "sha256": digest,
                 "compressedBytes": len(data), "uncompressedBytes": len(raw), "recordCount": count}
    return reference, data, payload


def _prepare(source_dir: Path, server_id: str, series_ref: Mapping[str, Any], limits: LookupLimits):
    _limits(limits)
    parents, episodes = _source_rows(Path(source_dir), server_id, series_ref, limits)
    grouped = {"series": {slot: [] for slot in SLOTS}, "episodes": {slot: [] for slot in SLOTS}}
    for identity in sorted(parents, key=lambda value: value.encode("ascii")):
        grouped["series"][series_bucket(server_id, identity)].append(parents[identity])
    for identity in sorted(episodes, key=lambda values: tuple(value.encode("ascii") for value in values)):
        grouped["episodes"][episode_bucket(server_id, *identity)].append(episodes[identity])
    root = {"schemaVersion": 1, "dataset": "vod-resume-index", "serverId": server_id,
            "seriesManifestSha256": series_ref["sha256"], "hashAlgorithm": HASH_ALGORITHM,
            "seriesRecords": len(parents), "episodeRecords": len(episodes), "seriesBuckets": {}, "episodeBuckets": {}}
    artifacts = []
    for kind, table in (("series", "seriesBuckets"), ("episodes", "episodeBuckets")):
        for slot, rows in grouped[kind].items():
            _require(len(rows) <= (limits.max_parent_rows if kind == "series" else limits.max_episode_rows),
                     "Resume bucket exceeds its record limit.")
            if not rows:
                root[table][slot] = {"empty": True, "recordCount": 0}
                continue
            payload = {"schemaVersion": 1, "dataset": f"vod-resume-{kind}", "serverId": server_id,
                       "bucket": slot, "recordCount": len(rows), "items": rows}
            artifact = _encoded(f"{server_id}_resume_{kind}_{slot}", payload, len(rows),
                                limits.bucket_compressed_bytes, limits.bucket_expanded_bytes)
            root[table][slot] = artifact[0]
            artifacts.append(artifact)
    root_artifact = _encoded(f"{server_id}_resume_index", root, 512,
                             limits.root_compressed_bytes, limits.root_expanded_bytes)
    artifacts.append(root_artifact)
    metrics = {"serverId": server_id, "seriesManifestSha256": series_ref["sha256"],
               "seriesRecords": len(parents), "episodeRecords": len(episodes),
               "seriesBucketCount": 256, "episodeBucketCount": 256,
               "nonemptySeriesBuckets": sum(bool(rows) for rows in grouped["series"].values()),
               "nonemptyEpisodeBuckets": sum(bool(rows) for rows in grouped["episodes"].values()),
               "artifactCount": len(artifacts),
               "maxCompressedBytes": max(len(data) for _ref_value, data, _payload in artifacts),
               "maxExpandedBytes": max(reference["uncompressedBytes"] for reference, _data, _payload in artifacts),
               "maxParentRows": max(map(len, grouped["series"].values())),
               "maxEpisodeRows": max(map(len, grouped["episodes"].values()))}
    return root_artifact[0], artifacts, metrics


def write_series_resume_lookup(source_dir: Path, staging_dir: Path, server_id: str,
                               series_ref: Mapping[str, Any], *, limits: LookupLimits = DEFAULT_LIMITS) -> dict:
    """Write a NEW staging directory only after complete input/cap validation.

    The caller owns any later integration or publication. Source and staging
    trees must not overlap; an existing destination is never overwritten.
    """
    try:
        source_path = _io_path(source_dir)
        source = source_path.resolve(strict=True)
        target = _io_path(staging_dir).absolute()
        _require(source.is_dir() and not source_path.is_symlink()
                 and not target.exists() and not target.is_symlink(), "Staging destination must be new and source must be a directory.")
        resolved = target.resolve()
        _require(source != resolved and source not in resolved.parents and resolved not in source.parents,
                 "Source and staging directories must not overlap.")
        reference, artifacts, _metrics = _prepare(source, server_id, series_ref, limits)
        # No filesystem mutation precedes this point. Failed input, projection,
        # coverage or sizing validation cannot leave a partial candidate behind.
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".series-resume-", dir=target.parent) as temporary:
            temporary_dir = _io_path(temporary)
            for artifact_ref, data, _payload in artifacts:
                with (temporary_dir / artifact_ref["file"]).open("xb") as handle:
                    handle.write(data)
            _require(not target.exists() and not target.is_symlink(), "Staging destination changed while preparing.")
            os.replace(temporary_dir, target)
        return copy.deepcopy(reference)
    except ResumeLookupError:
        raise
    except (OSError, TypeError, KeyError, ValueError, OverflowError, RecursionError) as exc:
        raise ResumeLookupError("Could not prepare standalone resume candidate.") from exc


def validate_series_resume_lookup(staging_dir: Path, root_ref: Mapping[str, Any], *, source_dir: Path,
                                  server_id: str, series_ref: Mapping[str, Any],
                                  limits: LookupLimits = DEFAULT_LIMITS) -> dict:
    """Deeply validate all output coverage and facts against the source snapshot."""
    try:
        _limits(limits)
        expected_ref, artifacts, metrics = _prepare(Path(source_dir), server_id, series_ref, limits)
        root = _read_artifact(Path(staging_dir), root_ref, compressed_limit=limits.root_compressed_bytes,
                              expanded_limit=limits.root_expanded_bytes)
        _keys(root, _ROOT_FIELDS, "resume root")
        _require(root_ref["file"].endswith(".json.gz") and root_ref["recordCount"] == 512
                 and type(root["schemaVersion"]) is int and root["schemaVersion"] == 1
                 and root["dataset"] == "vod-resume-index" and root["serverId"] == server_id
                 and root["seriesManifestSha256"] == series_ref["sha256"]
                 and root["hashAlgorithm"] == HASH_ALGORITHM, "Resume root identity or revision mismatch.")
        _require(_integer(root["seriesRecords"], limits.max_parent_rows * 256)
                 and root["seriesRecords"] == metrics["seriesRecords"]
                 and _integer(root["episodeRecords"], limits.max_episode_rows * 256)
                 and root["episodeRecords"] == metrics["episodeRecords"], "Resume root coverage count mismatch.")
        _require(isinstance(root["seriesBuckets"], dict) and isinstance(root["episodeBuckets"], dict)
                 and set(root["seriesBuckets"]) == set(SLOTS) and set(root["episodeBuckets"]) == set(SLOTS),
                 "Resume root must contain all bucket slots.")
        expected_root = artifacts[-1][2]
        expected_payloads = {(payload["dataset"], payload["bucket"]): payload
                             for _reference, _data, payload in artifacts[:-1]}
        actual_refs = [root_ref]
        for table, kind in (("seriesBuckets", "series"), ("episodeBuckets", "episodes")):
            _require(isinstance(root[table], dict), "Invalid resume bucket directory.")
            for slot in SLOTS:
                reference = root[table][slot]
                if reference == {"empty": True, "recordCount": 0}:
                    _require(type(reference["empty"]) is bool and type(reference["recordCount"]) is int,
                             "Invalid verified-empty bucket.")
                    _require(expected_root[table][slot] == {"empty": True, "recordCount": 0},
                             "Verified-empty bucket lost source coverage.")
                    continue
                payload = _read_artifact(Path(staging_dir), reference, compressed_limit=limits.bucket_compressed_bytes,
                                         expanded_limit=limits.bucket_expanded_bytes)
                _keys(payload, _CHILD_FIELDS, "resume bucket")
                _require(reference["file"].endswith(".json.gz")
                         and type(payload.get("schemaVersion")) is int and payload["schemaVersion"] == 1
                         and payload.get("dataset") == f"vod-resume-{kind}"
                         and payload.get("serverId") == server_id and payload.get("bucket") == slot
                         and isinstance(payload.get("items"), list) and type(payload.get("recordCount")) is int
                         and payload["recordCount"] == reference["recordCount"] == len(payload["items"])
                         and 1 <= len(payload["items"]) <= (limits.max_parent_rows if kind == "series" else limits.max_episode_rows),
                         "Resume bucket identity or count mismatch.")
                expected = expected_payloads.get((f"vod-resume-{kind}", slot))
                # Compare canonical typed JSON: Python equality alone would
                # incorrectly equate true/1 and false/0 in safety projections.
                _require(expected is not None and _json_bytes(payload) == _json_bytes(expected),
                         "Resume bucket projection or exact coverage mismatch.")
                actual_refs.append(reference)
        metrics["maxCompressedBytes"] = max(reference["compressedBytes"] for reference in actual_refs)
        metrics["maxExpandedBytes"] = max(reference["uncompressedBytes"] for reference in actual_refs)
        return metrics
    except ResumeLookupError:
        raise
    except (OSError, TypeError, KeyError, ValueError, OverflowError, RecursionError) as exc:
        raise ResumeLookupError("Invalid standalone resume candidate.") from exc
