#!/usr/bin/env python3
"""Create and verify a sealed EPGShare selection spool.

The inventory synchronizer can feed this module directly from
``epg_catalog_stream.stream_catalog_and_programmes_once``.  The production
builder can then reuse the selected SQLite rows without decompressing and
parsing the same multi-gigabyte XMLTV document a second time.

The spool is accepted only as an ephemeral hand-off produced earlier in the
same trusted GitHub Actions job.  It is bound to the raw EPGShare source
SHA-256, records every requested channel key, carries a deterministic
logical-content *integrity* seal, and is copied and validated before the
builder opens it for writes.  The unkeyed integrity seal detects corruption;
it is not authentication and must not be used for an externally supplied DB.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


SPOOL_SCHEMA_VERSION = "1"
SPOOL_WRITER_VERSION = "1.0"
SPOOL_SOURCE_KEY = "epgshare01"
MAX_SPOOL_BYTES = 6 * 1024 * 1024 * 1024
MINIMUM_FREE_BYTES_AFTER_COPY = 512 * 1024 * 1024
MAX_REQUEST_ROWS = 250_000
MAX_PROGRAMME_ROWS = 20_000_000
DEFAULT_MAXIMUM_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024
SQLITE_BATCH_SIZE = 2_000
MAXIMUM_SPOOL_AGE_SECONDS = 6 * 60 * 60
MAXIMUM_CLOCK_SKEW_SECONDS = 5 * 60
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CORE_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "channels": (
            "source_key",
            "channel_key",
            "source_channel_id",
            "display_name",
            "icon_url",
        ),
        "source_channel_ids": ("source_key", "source_channel_id"),
        "programmes": (
            "source_key",
            "channel_key",
            "start_epoch",
            "stop_epoch",
            "title",
            "subtitle",
            "description",
            "categories_json",
            "quality",
        ),
        "epg_spool_requests": (
            "channel_key",
            "request_kind",
            "source_channel_id",
        ),
        "epg_spool_manifest": ("key", "value"),
    }
)

EXPECTED_TABLE_SIGNATURES: Mapping[
    str, tuple[tuple[str, str, int, int, int], ...]
] = MappingProxyType(
    {
        "channels": (
            ("source_key", "TEXT", 1, 1, 0),
            ("channel_key", "TEXT", 1, 2, 0),
            ("source_channel_id", "TEXT", 1, 0, 0),
            ("display_name", "TEXT", 1, 0, 0),
            ("icon_url", "TEXT", 1, 0, 0),
        ),
        "source_channel_ids": (
            ("source_key", "TEXT", 1, 1, 0),
            ("source_channel_id", "TEXT", 1, 2, 0),
        ),
        "programmes": (
            ("source_key", "TEXT", 1, 1, 0),
            ("channel_key", "TEXT", 1, 2, 0),
            ("start_epoch", "INTEGER", 1, 3, 0),
            ("stop_epoch", "INTEGER", 1, 4, 0),
            ("title", "TEXT", 1, 5, 0),
            ("subtitle", "TEXT", 1, 0, 0),
            ("description", "TEXT", 1, 0, 0),
            ("categories_json", "TEXT", 1, 0, 0),
            ("quality", "INTEGER", 1, 0, 0),
        ),
        "epg_spool_requests": (
            ("channel_key", "TEXT", 1, 1, 0),
            ("request_kind", "TEXT", 1, 0, 0),
            ("source_channel_id", "TEXT", 1, 0, 0),
        ),
        "epg_spool_manifest": (
            ("key", "TEXT", 1, 1, 0),
            ("value", "TEXT", 1, 0, 0),
        ),
    }
)


class SpoolError(RuntimeError):
    """A controlled, credential-free selection-spool failure."""


@dataclass(frozen=True)
class VerifiedSpool:
    """A verified builder database plus immutable provenance."""

    connection: sqlite3.Connection
    manifest: Mapping[str, str]
    stats: Mapping[str, int]
    requested_ids: frozenset[str]


def _schema_sql() -> str:
    # Keep the three core tables byte-for-byte compatible with
    # build_epg_streaming.create_database().  The final two tables are private
    # spool provenance and are ignored by normal output queries.
    return """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-32768;
        PRAGMA mmap_size=0;
        CREATE TABLE channels (
            source_key TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            source_channel_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            icon_url TEXT NOT NULL,
            PRIMARY KEY (source_key, channel_key)
        ) WITHOUT ROWID;
        CREATE TABLE source_channel_ids (
            source_key TEXT NOT NULL,
            source_channel_id TEXT NOT NULL,
            PRIMARY KEY (source_key, source_channel_id)
        ) WITHOUT ROWID;
        CREATE TABLE programmes (
            source_key TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            start_epoch INTEGER NOT NULL,
            stop_epoch INTEGER NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL,
            description TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            quality INTEGER NOT NULL,
            PRIMARY KEY (source_key, channel_key, start_epoch, stop_epoch, title)
        ) WITHOUT ROWID;
        CREATE TABLE epg_spool_requests (
            channel_key TEXT NOT NULL PRIMARY KEY,
            request_kind TEXT NOT NULL,
            source_channel_id TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE epg_spool_manifest (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
    """


def _valid_sha256(value: object) -> bool:
    return bool(SHA256_RE.fullmatch(str(value or "").strip().casefold()))


def _clean_identifier(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text.encode("utf-8")) > 1_200:
        raise SpoolError(f"{label} contains an invalid XMLTV ID.")
    if any(ord(character) < 32 for character in text):
        raise SpoolError(f"{label} contains an invalid XMLTV ID.")
    return text


def _clean_text(value: object, *, label: str, maximum_bytes: int = MAX_TEXT_BYTES) -> str:
    text = str(value or "")
    if "\x00" in text or len(text.encode("utf-8")) > int(maximum_bytes):
        raise SpoolError(f"{label} contains invalid or oversized text.")
    return text


def _normalize_ids(values: Iterable[object], *, label: str) -> tuple[str, ...]:
    result: set[str] = set()
    for raw in values:
        result.add(_clean_identifier(raw, label=label))
        if len(result) > MAX_REQUEST_ROWS:
            raise SpoolError(f"{label} contains too many XMLTV IDs.")
    return tuple(sorted(result, key=lambda item: (item.casefold(), item)))


def _stats_dict(value: object) -> dict[str, int]:
    raw: object
    if is_dataclass(value) and not isinstance(value, type):
        raw = asdict(value)
    else:
        raw = value
    if not isinstance(raw, Mapping):
        raise SpoolError("Spool source statistics are invalid.")
    result: dict[str, int] = {}
    for key, item in raw.items():
        name = str(key)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise SpoolError("Spool source statistics contain an invalid field.")
        if isinstance(item, bool):
            raise SpoolError("Spool source statistics contain an invalid value.")
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise SpoolError("Spool source statistics contain an invalid value.") from exc
        if number < 0 or number > (1 << 63) - 1:
            raise SpoolError("Spool source statistics contain an invalid value.")
        result[name] = number
    return dict(sorted(result.items()))


def _digest_field(digest: Any, value: object) -> None:
    if isinstance(value, int):
        kind = b"i"
        payload = str(value).encode("ascii")
    else:
        kind = b"s"
        payload = str(value).encode("utf-8")
    digest.update(kind)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _digest_record(digest: Any, section: str, values: Sequence[object]) -> None:
    _digest_field(digest, section)
    _digest_field(digest, len(values))
    for value in values:
        _digest_field(digest, value)


def _logical_sha256(
    connection: sqlite3.Connection, seal_fields: Mapping[str, str]
) -> str:
    digest = hashlib.sha256()
    for key in sorted(seal_fields):
        _digest_record(digest, "manifest", (key, seal_fields[key]))
    queries = (
        (
            "request",
            "SELECT channel_key, request_kind, source_channel_id "
            "FROM epg_spool_requests ORDER BY channel_key COLLATE BINARY",
        ),
        (
            "channel",
            "SELECT source_key, channel_key, source_channel_id, display_name, icon_url "
            "FROM channels ORDER BY source_key COLLATE BINARY, channel_key COLLATE BINARY",
        ),
        (
            "source_channel_id",
            "SELECT source_key, source_channel_id FROM source_channel_ids "
            "ORDER BY source_key COLLATE BINARY, source_channel_id COLLATE BINARY",
        ),
        (
            "programme",
            "SELECT source_key, channel_key, start_epoch, stop_epoch, title, "
            "subtitle, description, categories_json, quality FROM programmes "
            "ORDER BY source_key COLLATE BINARY, channel_key COLLATE BINARY, "
            "start_epoch, stop_epoch, title COLLATE BINARY",
        ),
    )
    for section, query in queries:
        for row in connection.execute(query):
            _digest_record(digest, section, tuple(row))
    return digest.hexdigest()


class EpgSelectionSpoolWriter:
    """SQLite callback sink with explicit successful sealing."""

    def __init__(self, destination: Path):
        self.destination = Path(destination)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        if self.destination.is_symlink() or self.destination.parent.is_symlink():
            raise SpoolError("The selection-spool destination must not be a symlink.")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.destination.name}.",
            suffix=".partial",
            dir=self.destination.parent,
        )
        os.close(descriptor)
        self._temporary = Path(temporary)
        self._connection = sqlite3.connect(self._temporary)
        self._connection.executescript(_schema_sql())
        self._programme_batch: list[tuple[Any, ...]] = []
        self._source_by_channel: dict[str, str] = {}
        self._sealed = False

    def __enter__(self) -> "EpgSelectionSpoolWriter":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if not self._sealed:
            self.abort()

    def add_channel(self, channel_key: object, channel: object) -> None:
        key = _clean_identifier(channel_key, label="channel sink")
        source_id = _clean_identifier(
            getattr(channel, "epg_id", ""), label="channel sink"
        )
        display_name = _clean_text(
            getattr(channel, "display_name", ""),
            label="channel display name",
            maximum_bytes=8_000,
        )
        icon_url = _clean_text(
            getattr(channel, "icon_url", ""),
            label="channel icon URL",
            maximum_bytes=8_000,
        )
        self._connection.execute(
            "INSERT INTO source_channel_ids VALUES (?, ?)",
            (SPOOL_SOURCE_KEY, source_id),
        )
        self._connection.execute(
            "INSERT INTO channels VALUES (?, ?, ?, ?, ?)",
            (SPOOL_SOURCE_KEY, key, source_id, display_name, icon_url),
        )
        self._source_by_channel[key] = source_id

    def add_programme(self, record: object) -> None:
        channel_key = _clean_identifier(
            getattr(record, "channel_key", ""), label="programme sink"
        )
        source_channel_id = _clean_identifier(
            getattr(record, "source_channel_id", ""), label="programme sink"
        )
        if self._source_by_channel.get(channel_key) != source_channel_id:
            raise SpoolError("Programme sink does not match its selected channel.")
        try:
            start = int(getattr(record, "start_epoch"))
            stop = int(getattr(record, "stop_epoch"))
            quality = int(getattr(record, "quality"))
        except (TypeError, ValueError) as exc:
            raise SpoolError("Programme sink contains invalid numeric data.") from exc
        if start < 0 or stop <= start or quality < 0 or quality > 10_000:
            raise SpoolError("Programme sink contains invalid numeric data.")
        title = _clean_text(
            getattr(record, "title", ""), label="programme title", maximum_bytes=8_000
        )
        if not title:
            raise SpoolError("Programme sink contains a blank title.")
        subtitle = _clean_text(
            getattr(record, "subtitle", ""),
            label="programme subtitle",
            maximum_bytes=8_000,
        )
        description = _clean_text(
            getattr(record, "description", ""),
            label="programme description",
            maximum_bytes=16_000,
        )
        raw_categories = getattr(record, "categories", ())
        if not isinstance(raw_categories, (list, tuple)) or len(raw_categories) > 64:
            raise SpoolError("Programme sink contains invalid categories.")
        categories = [
            _clean_text(item, label="programme category", maximum_bytes=1_200)
            for item in raw_categories
        ]
        categories_json = json.dumps(
            categories, ensure_ascii=False, separators=(",", ":")
        )
        if len(categories_json.encode("utf-8")) > MAX_TEXT_BYTES:
            raise SpoolError("Programme sink contains oversized categories.")
        self._programme_batch.append(
            (
                SPOOL_SOURCE_KEY,
                channel_key,
                start,
                stop,
                title,
                subtitle,
                description,
                categories_json,
                quality,
                source_channel_id,
            )
        )
        if len(self._programme_batch) >= SQLITE_BATCH_SIZE:
            self._flush_programmes()

    def _flush_programmes(self) -> None:
        if not self._programme_batch:
            return
        rows = [row[:-1] for row in self._programme_batch]
        self._connection.executemany(
            """
            INSERT INTO programmes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_key, channel_key, start_epoch, stop_epoch, title
            ) DO UPDATE SET
                subtitle = excluded.subtitle,
                description = excluded.description,
                categories_json = excluded.categories_json,
                quality = excluded.quality
            WHERE (
                excluded.quality,
                excluded.subtitle,
                excluded.description,
                excluded.categories_json
            ) > (
                programmes.quality,
                programmes.subtitle,
                programmes.description,
                programmes.categories_json
            )
            """,
            rows,
        )
        self._programme_batch.clear()
        self._connection.commit()

    def seal(
        self,
        *,
        source_sha256: str,
        source_bytes: int,
        catalog_sha256: str,
        window_start: int,
        created_at_epoch: int,
        fixed_requested_ids: Iterable[object],
        provisional_requested_ids: Iterable[object],
        resolved_source_ids: Mapping[object, object],
        stats: object,
    ) -> Path:
        if self._sealed:
            raise SpoolError("The selection spool is already sealed.")
        source_hash = str(source_sha256 or "").strip().casefold()
        catalog_hash = str(catalog_sha256 or "").strip().casefold()
        if not _valid_sha256(source_hash) or not _valid_sha256(catalog_hash):
            raise SpoolError("The selection spool requires valid source hashes.")
        try:
            source_size = int(source_bytes)
            start = int(window_start)
            created = int(created_at_epoch)
        except (TypeError, ValueError) as exc:
            raise SpoolError("The selection spool has invalid provenance numbers.") from exc
        if source_size < 1 or start < 0 or created < 0:
            raise SpoolError("The selection spool has invalid provenance numbers.")

        fixed = _normalize_ids(fixed_requested_ids, label="fixed requests")
        provisional = _normalize_ids(
            provisional_requested_ids, label="provisional requests"
        )
        fixed_set = set(fixed)
        provisional = tuple(item for item in provisional if item not in fixed_set)
        requested = fixed + provisional
        requested_set = set(requested)
        if len(requested) > MAX_REQUEST_ROWS:
            raise SpoolError("The selection spool contains too many requests.")

        target_to_source: dict[str, str] = {}
        for raw_source, raw_target in resolved_source_ids.items():
            source_id = _clean_identifier(raw_source, label="resolved source IDs")
            target = _clean_identifier(raw_target, label="resolved source IDs")
            if target not in requested_set:
                raise SpoolError("A resolved source ID was not requested.")
            if target in target_to_source and target_to_source[target] != source_id:
                raise SpoolError("One request resolves to more than one source ID.")
            target_to_source[target] = source_id

        for key in fixed:
            self._connection.execute(
                "INSERT INTO epg_spool_requests VALUES (?, ?, ?)",
                (key, "fixed", target_to_source.get(key, "")),
            )
        for key in provisional:
            self._connection.execute(
                "INSERT INTO epg_spool_requests VALUES (?, ?, ?)",
                (key, "provisional", target_to_source.get(key, "")),
            )
        self._flush_programmes()
        self._connection.commit()

        mismatched_channels = int(
            self._connection.execute(
                """
                SELECT COUNT(*) FROM channels AS c
                LEFT JOIN epg_spool_requests AS r ON r.channel_key = c.channel_key
                WHERE r.channel_key IS NULL OR r.source_channel_id != c.source_channel_id
                   OR c.source_key != ?
                """,
                (SPOOL_SOURCE_KEY,),
            ).fetchone()[0]
        )
        missing_resolved_channels = int(
            self._connection.execute(
                """
                SELECT COUNT(*) FROM epg_spool_requests AS r
                LEFT JOIN channels AS c
                  ON c.source_key = ? AND c.channel_key = r.channel_key
                WHERE r.source_channel_id != '' AND c.channel_key IS NULL
                """,
                (SPOOL_SOURCE_KEY,),
            ).fetchone()[0]
        )
        if mismatched_channels or missing_resolved_channels:
            raise SpoolError("The selection spool channel callbacks are incomplete.")

        stats_dict = _stats_dict(stats)
        stored_programmes = int(
            self._connection.execute("SELECT COUNT(*) FROM programmes").fetchone()[0]
        )
        stored_channels = int(
            self._connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        )
        if stored_programmes > MAX_PROGRAMME_ROWS:
            raise SpoolError("The selection spool contains too many programmes.")
        stats_dict["stored_channels"] = stored_channels
        stats_dict["stored_programmes"] = stored_programmes
        stats_json = json.dumps(
            stats_dict, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        seal_fields = {
            "schema_version": SPOOL_SCHEMA_VERSION,
            "writer_version": SPOOL_WRITER_VERSION,
            "trust_scope": "same_job_ephemeral",
            "state": "sealed",
            "source_key": SPOOL_SOURCE_KEY,
            "source_sha256": source_hash,
            "source_bytes": str(source_size),
            "catalog_sha256": catalog_hash,
            "window_start": str(start),
            "created_at_epoch": str(created),
            "fixed_requested_count": str(len(fixed)),
            "provisional_requested_count": str(len(provisional)),
            "requested_count": str(len(requested)),
            "stats_json": stats_json,
        }
        logical_hash = _logical_sha256(self._connection, seal_fields)
        manifest = {**seal_fields, "logical_sha256": logical_hash}
        self._connection.executemany(
            "INSERT INTO epg_spool_manifest VALUES (?, ?)", sorted(manifest.items())
        )
        self._connection.commit()
        self._connection.close()
        os.replace(self._temporary, self.destination)
        self._sealed = True
        return self.destination

    def abort(self) -> None:
        try:
            self._connection.close()
        finally:
            self._temporary.unlink(missing_ok=True)


def _copy_regular_file_no_follow(source: Path, destination: Path) -> None:
    source_path = Path(source)
    try:
        source_lstat = source_path.lstat()
    except OSError as exc:
        raise SpoolError("The selection-spool file is unavailable.") from exc
    if stat.S_ISLNK(source_lstat.st_mode) or not stat.S_ISREG(source_lstat.st_mode):
        raise SpoolError("The selection-spool input must be a regular file.")
    if source_lstat.st_size < 1 or source_lstat.st_size > MAX_SPOOL_BYTES:
        raise SpoolError("The selection-spool input has an invalid size.")

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(destination_path.parent).free
    if source_lstat.st_size > max(0, available - MINIMUM_FREE_BYTES_AFTER_COPY):
        raise SpoolError("There is not enough workspace disk for the selection spool.")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".copy",
        dir=destination_path.parent,
    )
    temporary_path = Path(temporary)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source_path, flags)
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            ) != (
                source_lstat.st_dev,
                source_lstat.st_ino,
                source_lstat.st_size,
            ):
                raise SpoolError("The selection-spool input changed while opening.")
            with os.fdopen(source_fd, "rb", closefd=False) as input_handle, os.fdopen(
                descriptor, "wb", closefd=False
            ) as output_handle:
                shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                output_handle.flush()
            if os.fstat(source_fd).st_size != source_lstat.st_size:
                raise SpoolError("The selection-spool input changed while copying.")
        finally:
            os.close(source_fd)
            os.close(descriptor)
        os.replace(temporary_path, destination_path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _canonical_sql(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _expected_table_definitions() -> dict[str, str]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_schema_sql())
        return {
            str(name): _canonical_sql(sql)
            for name, sql in reference.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        reference.close()


def _table_signature(
    connection: sqlite3.Connection, name: str
) -> tuple[tuple[str, str, int, int, int], ...]:
    escaped = name.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_xinfo("{escaped}")').fetchall()
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3] or 0),
            int(row[5] or 0),
            int(row[6] or 0),
        )
        for row in rows
    )


def _load_manifest(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        "SELECT key, value FROM epg_spool_manifest ORDER BY key COLLATE BINARY"
    ).fetchall()
    manifest = {str(key): str(value) for key, value in rows}
    required = {
        "schema_version",
        "writer_version",
        "trust_scope",
        "state",
        "source_key",
        "source_sha256",
        "source_bytes",
        "catalog_sha256",
        "window_start",
        "created_at_epoch",
        "fixed_requested_count",
        "provisional_requested_count",
        "requested_count",
        "stats_json",
        "logical_sha256",
    }
    if set(manifest) != required:
        raise SpoolError("The selection spool manifest is incomplete or unsupported.")
    return manifest


def _reject_invalid_cell_shapes(connection: sqlite3.Connection) -> None:
    """Reject SQLite affinity/type tricks and oversized values before fetching."""
    checks = (
        """
        SELECT COUNT(*) FROM epg_spool_manifest
        WHERE typeof(key) != 'text' OR typeof(value) != 'text'
           OR length(CAST(key AS BLOB)) NOT BETWEEN 1 AND 64
           OR length(CAST(value AS BLOB)) > 65536
        """,
        """
        SELECT COUNT(*) FROM epg_spool_requests
        WHERE typeof(channel_key) != 'text' OR typeof(request_kind) != 'text'
           OR typeof(source_channel_id) != 'text'
           OR length(CAST(channel_key AS BLOB)) NOT BETWEEN 1 AND 1200
           OR length(CAST(request_kind AS BLOB)) NOT BETWEEN 1 AND 32
           OR length(CAST(source_channel_id AS BLOB)) > 1200
        """,
        """
        SELECT COUNT(*) FROM channels
        WHERE typeof(source_key) != 'text' OR typeof(channel_key) != 'text'
           OR typeof(source_channel_id) != 'text'
           OR typeof(display_name) != 'text' OR typeof(icon_url) != 'text'
           OR length(CAST(source_key AS BLOB)) NOT BETWEEN 1 AND 32
           OR length(CAST(channel_key AS BLOB)) NOT BETWEEN 1 AND 1200
           OR length(CAST(source_channel_id AS BLOB)) NOT BETWEEN 1 AND 1200
           OR length(CAST(display_name AS BLOB)) > 8000
           OR length(CAST(icon_url AS BLOB)) > 8000
        """,
        """
        SELECT COUNT(*) FROM source_channel_ids
        WHERE typeof(source_key) != 'text' OR typeof(source_channel_id) != 'text'
           OR length(CAST(source_key AS BLOB)) NOT BETWEEN 1 AND 32
           OR length(CAST(source_channel_id AS BLOB)) NOT BETWEEN 1 AND 1200
        """,
        """
        SELECT COUNT(*) FROM programmes
        WHERE typeof(source_key) != 'text' OR typeof(channel_key) != 'text'
           OR typeof(start_epoch) != 'integer' OR typeof(stop_epoch) != 'integer'
           OR typeof(title) != 'text' OR typeof(subtitle) != 'text'
           OR typeof(description) != 'text' OR typeof(categories_json) != 'text'
           OR typeof(quality) != 'integer'
           OR length(CAST(source_key AS BLOB)) NOT BETWEEN 1 AND 32
           OR length(CAST(channel_key AS BLOB)) NOT BETWEEN 1 AND 1200
           OR start_epoch NOT BETWEEN 0 AND 253402300799
           OR stop_epoch NOT BETWEEN 1 AND 253402300799
           OR stop_epoch <= start_epoch OR quality NOT BETWEEN 0 AND 10000
           OR length(CAST(title AS BLOB)) NOT BETWEEN 1 AND 8000
           OR length(CAST(subtitle AS BLOB)) > 8000
           OR length(CAST(description AS BLOB)) > 16000
           OR length(CAST(categories_json AS BLOB)) > 32768
        """,
    )
    for query in checks:
        if int(connection.execute(query).fetchone()[0]):
            raise SpoolError("The selection spool contains invalid cell types or sizes.")


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    text = str(value if value is not None else "")
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise SpoolError(f"The selection spool has an invalid {label}.")
    number = int(text)
    if number < minimum or number > maximum:
        raise SpoolError(f"The selection spool has an invalid {label}.")
    return number


def _validate_database_content(
    connection: sqlite3.Connection,
    *,
    manifest: Mapping[str, str],
    expected_source_sha256: str,
    expected_source_bytes: int,
    required_ids: frozenset[str],
    required_window_start: int,
    maximum_expanded_bytes: int,
    consumer_epoch: int,
    maximum_spool_age_seconds: int,
) -> tuple[dict[str, int], frozenset[str]]:
    if manifest["schema_version"] != SPOOL_SCHEMA_VERSION:
        raise SpoolError("The selection spool schema version is unsupported.")
    if (
        manifest["writer_version"] != SPOOL_WRITER_VERSION
        or manifest["trust_scope"] != "same_job_ephemeral"
        or manifest["state"] != "sealed"
        or manifest["source_key"] != SPOOL_SOURCE_KEY
    ):
        raise SpoolError("The selection spool is not a sealed EPGShare spool.")
    if not _valid_sha256(manifest["source_sha256"]) or not _valid_sha256(
        manifest["catalog_sha256"]
    ):
        raise SpoolError("The selection spool contains invalid source hashes.")
    if manifest["source_sha256"] != expected_source_sha256:
        raise SpoolError("The selection spool does not match the EPGShare source file.")
    source_bytes = _bounded_int(
        manifest["source_bytes"],
        label="source size",
        minimum=1,
        maximum=1024 * 1024 * 1024,
    )
    if source_bytes != int(expected_source_bytes):
        raise SpoolError("The selection spool does not match the EPGShare source size.")
    spool_window_start = _bounded_int(
        manifest["window_start"],
        label="window start",
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    if spool_window_start > int(required_window_start):
        raise SpoolError("The selection spool does not cover the requested history window.")
    created_at = _bounded_int(
        manifest["created_at_epoch"],
        label="creation time",
        minimum=0,
        maximum=(1 << 63) - 1,
    )
    age_limit = int(maximum_spool_age_seconds)
    if age_limit < 1 or age_limit > 7 * 86400:
        raise SpoolError("The selection spool age limit is invalid.")
    if (
        created_at > int(consumer_epoch) + MAXIMUM_CLOCK_SKEW_SECONDS
        or created_at < int(consumer_epoch) - age_limit
    ):
        raise SpoolError("The selection spool was not created during this build window.")

    request_rows = connection.execute(
        "SELECT channel_key, request_kind, source_channel_id "
        "FROM epg_spool_requests ORDER BY channel_key COLLATE BINARY"
    ).fetchall()
    if len(request_rows) > MAX_REQUEST_ROWS:
        raise SpoolError("The selection spool contains too many requests.")
    requested: set[str] = set()
    for raw_key, kind, raw_source in request_rows:
        key = _clean_identifier(raw_key, label="spool requests")
        source_id = str(raw_source or "")
        if kind not in {"fixed", "provisional"}:
            raise SpoolError("The selection spool contains an invalid request kind.")
        if source_id:
            _clean_identifier(source_id, label="spool requests")
        requested.add(key)
    if not required_ids.issubset(requested):
        raise SpoolError("The selection spool does not cover every mapped EPGShare ID.")
    # A fixed legacy mapping can legitimately disappear from a newer source;
    # the direct XML parser historically retained that row and reported zero
    # schedule coverage.  Request-history membership proves the one-pass sync
    # considered every active ID.  Newly auto-approved IDs are held to the
    # stronger resolution/programme gate by the synchronizer before its Sheet
    # write.  Below, every *nonblank* resolution is still required to have the
    # matching selected channel row.

    channel_count = _bounded_int(
        connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        label="channel count",
        minimum=0,
        maximum=MAX_REQUEST_ROWS,
    )
    programme_count = _bounded_int(
        connection.execute("SELECT COUNT(*) FROM programmes").fetchone()[0],
        label="programme count",
        minimum=0,
        maximum=MAX_PROGRAMME_ROWS,
    )
    invalid_relations = int(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM channels AS c
               LEFT JOIN epg_spool_requests AS r ON r.channel_key = c.channel_key
               WHERE c.source_key != ? OR r.channel_key IS NULL
                  OR r.source_channel_id != c.source_channel_id)
              +
              (SELECT COUNT(*) FROM source_channel_ids WHERE source_key != ?)
              +
              (SELECT COUNT(*) FROM source_channel_ids AS s
               LEFT JOIN channels AS c
                 ON c.source_key = s.source_key
                AND c.source_channel_id = s.source_channel_id
               WHERE c.channel_key IS NULL)
              +
              (SELECT COUNT(*) FROM programmes AS p
               LEFT JOIN channels AS c
                 ON c.source_key = p.source_key AND c.channel_key = p.channel_key
               WHERE p.source_key != ? OR c.channel_key IS NULL
                  OR p.stop_epoch <= p.start_epoch)
            """,
            (SPOOL_SOURCE_KEY, SPOOL_SOURCE_KEY, SPOOL_SOURCE_KEY),
        ).fetchone()[0]
    )
    if invalid_relations:
        raise SpoolError("The selection spool contains inconsistent selected rows.")

    for row in connection.execute(
        "SELECT source_channel_id, display_name, icon_url FROM channels"
    ):
        _clean_identifier(row[0], label="spool channels")
        _clean_text(row[1], label="spool channel display name", maximum_bytes=8_000)
        _clean_text(row[2], label="spool channel icon URL", maximum_bytes=8_000)
    for row in connection.execute(
        "SELECT title, subtitle, description, categories_json, quality FROM programmes"
    ):
        title = _clean_text(row[0], label="spool programme title", maximum_bytes=8_000)
        if not title:
            raise SpoolError("The selection spool contains a blank programme title.")
        _clean_text(row[1], label="spool programme subtitle", maximum_bytes=8_000)
        _clean_text(row[2], label="spool programme description", maximum_bytes=16_000)
        categories_text = _clean_text(
            row[3], label="spool programme categories", maximum_bytes=MAX_TEXT_BYTES
        )
        try:
            categories = json.loads(categories_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpoolError("The selection spool contains invalid categories.") from exc
        if (
            not isinstance(categories, list)
            or len(categories) > 64
            or any(
                not isinstance(item, str) or len(item.encode("utf-8")) > 1_200
                for item in categories
            )
        ):
            raise SpoolError("The selection spool contains invalid categories.")
        if isinstance(row[4], bool) or not isinstance(row[4], int) or not 0 <= row[4] <= 10_000:
            raise SpoolError("The selection spool contains an invalid quality score.")

    expected_requests = _bounded_int(
        manifest["requested_count"],
        label="request count",
        minimum=0,
        maximum=MAX_REQUEST_ROWS,
    )
    fixed_count = _bounded_int(
        manifest["fixed_requested_count"],
        label="fixed request count",
        minimum=0,
        maximum=MAX_REQUEST_ROWS,
    )
    provisional_count = _bounded_int(
        manifest["provisional_requested_count"],
        label="provisional request count",
        minimum=0,
        maximum=MAX_REQUEST_ROWS,
    )
    if (
        expected_requests != len(request_rows)
        or fixed_count + provisional_count != expected_requests
        or fixed_count != sum(1 for row in request_rows if row[1] == "fixed")
    ):
        raise SpoolError("The selection spool request counts do not match its rows.")

    try:
        raw_stats = json.loads(manifest["stats_json"])
    except json.JSONDecodeError as exc:
        raise SpoolError("The selection spool source statistics are invalid.") from exc
    stats = _stats_dict(raw_stats)
    if stats.get("stored_channels") != channel_count or stats.get(
        "stored_programmes"
    ) != programme_count:
        raise SpoolError("The selection spool row counts do not match its manifest.")
    required_stats = {
        "source_bytes",
        "expanded_bytes",
        "total_elements",
        "channel_elements",
        "unique_channel_ids",
        "duplicate_channel_ids",
        "programme_elements",
        "selected_programmes",
    }
    if not required_stats.issubset(stats):
        raise SpoolError("The selection spool source statistics are incomplete.")
    maximum_expanded = int(maximum_expanded_bytes)
    if maximum_expanded < 1 or maximum_expanded > (1 << 63) - 1:
        raise SpoolError("The selection spool expanded-byte limit is invalid.")
    if (
        stats["source_bytes"] != source_bytes
        or stats["expanded_bytes"] > maximum_expanded
        or stats["total_elements"] > MAX_PROGRAMME_ROWS
        or stats["programme_elements"] > stats["total_elements"]
        or stats["channel_elements"] > stats["total_elements"]
        or stats["channel_elements"]
        != stats["unique_channel_ids"] + stats["duplicate_channel_ids"]
        or stats["selected_programmes"] > stats["programme_elements"]
        or programme_count > stats["selected_programmes"]
        or channel_count > stats["unique_channel_ids"]
    ):
        raise SpoolError("The selection spool source statistics are inconsistent.")

    seal_fields = {
        key: value for key, value in manifest.items() if key != "logical_sha256"
    }
    if not _valid_sha256(manifest["logical_sha256"]) or _logical_sha256(
        connection, seal_fields
    ) != manifest["logical_sha256"]:
        raise SpoolError("The selection spool logical-content seal is invalid.")
    return stats, frozenset(requested)


def copy_and_open_verified_spool(
    *,
    source: Path,
    destination: Path,
    expected_source_sha256: str,
    expected_source_bytes: int,
    required_ids: Iterable[object],
    required_window_start: int,
    maximum_expanded_bytes: int = DEFAULT_MAXIMUM_EXPANDED_BYTES,
    consumer_epoch: int | None = None,
    maximum_spool_age_seconds: int = MAXIMUM_SPOOL_AGE_SECONDS,
) -> VerifiedSpool:
    """Copy a sealed spool, validate it fully, and return a writable database."""
    source_hash = str(expected_source_sha256 or "").strip().casefold()
    if not _valid_sha256(source_hash):
        raise SpoolError("The expected EPGShare source hash is invalid.")
    wanted = frozenset(_normalize_ids(required_ids, label="required IDs"))
    destination_path = Path(destination)
    _copy_regular_file_no_follow(Path(source), destination_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(destination_path)
        # Apply limits before reading any untrusted cell value.  Legitimate
        # spool fields are at most 32 KiB, so 1 MiB leaves generous headroom.
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if quick_check != [("ok",)]:
            raise SpoolError("The selection-spool database failed its integrity check.")

        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        expected_sql = _expected_table_definitions()
        if [(row[0], row[1]) for row in objects] != sorted(
            ("table", name) for name in CORE_TABLE_COLUMNS
        ) or {
            str(name): _canonical_sql(sql) for _kind, name, sql in objects
        } != expected_sql:
            raise SpoolError("The selection spool contains an unsupported database schema.")
        for table, signature in EXPECTED_TABLE_SIGNATURES.items():
            if _table_signature(connection, table) != signature:
                raise SpoolError("The selection spool contains an unsupported database schema.")

        _reject_invalid_cell_shapes(connection)
        manifest = _load_manifest(connection)
        stats, requested = _validate_database_content(
            connection,
            manifest=manifest,
            expected_source_sha256=source_hash,
            expected_source_bytes=int(expected_source_bytes),
            required_ids=wanted,
            required_window_start=int(required_window_start),
            maximum_expanded_bytes=int(maximum_expanded_bytes),
            consumer_epoch=int(consumer_epoch if consumer_epoch is not None else time.time()),
            maximum_spool_age_seconds=int(maximum_spool_age_seconds),
        )
        connection.execute("PRAGMA query_only=OFF")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-32768")
        return VerifiedSpool(
            connection=connection,
            manifest=MappingProxyType(dict(manifest)),
            stats=MappingProxyType(dict(stats)),
            requested_ids=requested,
        )
    except (sqlite3.DatabaseError, OSError) as exc:
        if connection is not None:
            connection.close()
        destination_path.unlink(missing_ok=True)
        raise SpoolError("The selection-spool database is malformed.") from exc
    except Exception:
        if connection is not None:
            connection.close()
        destination_path.unlink(missing_ok=True)
        raise


__all__ = (
    "EpgSelectionSpoolWriter",
    "SPOOL_SCHEMA_VERSION",
    "SpoolError",
    "VerifiedSpool",
    "copy_and_open_verified_spool",
)
