"""Durable, private checkpoints for a frozen VOD verification test.

This store never publishes a catalog and never interprets a title as an identity.
The caller supplies sanitized, normalized inputs and, when sharing an outcome,
an identity key covering all matching constraints and source evidence. A frozen
input or policy change requires a new checkpoint instead of silently skipping
work completed under different rules. SQLite transactions commit each outcome;
there is no positional cursor which can skip unfinished records after a crash.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 512 * 1024
MAX_META_BYTES = 1024 * 1024
MATCHED = frozenset({"matched_id", "matched_search", "matched_alias", "matched_poster"})
DEFINITIVE = frozenset({"no_match", "missing_identity", "no_search_results", "title_mismatch",
                         "year_mismatch", "ambiguous_title_year", "alias_candidate_limit",
                         "ignored_no_artwork"})
DEFERRED = frozenset({"batch_deadline", "rate_limited", "session_deadline"})
CONTENT_ID = re.compile(r"(server_[1-9][0-9]?):(movie|series):([0-9]{1,10})\Z")
SAFE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,99}\Z")


class CheckpointMismatch(ValueError):
    """The requested test is not the frozen test in this checkpoint."""


def _json(value: Any, limit: int = MAX_RECORD_BYTES) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Checkpoint values must be finite JSON data.") from exc
    if len(result.encode("utf-8")) > limit:
        raise ValueError("Checkpoint JSON exceeds its size limit.")
    return result


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number.")
    return float(value)


def _now(value: float | None = None) -> float:
    return _number(time.time() if value is None else value, "Timestamp")


def _identifier(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get("tmdbId")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value) if re.fullmatch(r"[1-9][0-9]{0,9}", str(value)) else None


def _status(metadata: Mapping[str, Any]) -> str:
    status = metadata.get("matchStatus", "unavailable")
    if not isinstance(status, str) or status not in MATCHED | {"no_match", "unavailable"}:
        raise ValueError("Result metadata has an invalid matchStatus.")
    if status in MATCHED and _identifier(metadata) is None:
        raise ValueError("A matched result requires a valid TMDB ID.")
    return status


def _baseline(record: Mapping[str, Any]) -> tuple[str, bool, dict[str, Any]]:
    baseline = record.get("baseline", {})
    if not isinstance(baseline, Mapping):
        raise ValueError("baseline must be an object.")
    metadata = record.get("baselineMetadata", baseline.get("metadata", baseline))
    if not isinstance(metadata, Mapping):
        raise ValueError("baselineMetadata must be an object.")
    metadata = dict(metadata)
    published = baseline.get("metadataMatch", "unavailable")
    if isinstance(published, Mapping):
        published = published.get("status", "unavailable")
    status = record.get("baselineStatus", baseline.get("status", metadata.get("matchStatus", published)))
    if not isinstance(status, str) or not SAFE_LABEL.fullmatch(status):
        raise ValueError("Baseline status is invalid.")
    verified = record.get("baselineVerified", False)
    if not isinstance(verified, bool):
        raise ValueError("baselineVerified must be a boolean.")
    if verified:
        metadata.setdefault("matchStatus", status)
        if status not in MATCHED or _status(metadata) != status:
            raise ValueError("Verified baseline requires validated matching metadata.")
    elif status in MATCHED and _identifier(metadata) is None:
        # A flag without a verified identifier is not a verified baseline.
        status = "unavailable"
    return status, verified, metadata


class Checkpoint:
    """Thread-safe checkpoint owned by one verification test.

    ``stats`` passed to :meth:`record_result` are deltas, not cumulative client
    counters. Grouped outcomes charge API usage and elapsed time once, while
    recording a durable result for every member. Call ``close`` before copying
    the database as an artifact; DELETE journaling leaves one portable file.
    """

    def __init__(self, path: Path | str, *, max_attempts: int = 3):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self._lock = threading.RLock()
        self._closed = False
        self.connection = sqlite3.connect(self.path, isolation_level=None,
                                          check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.close()
            raise CheckpointMismatch("Unsupported checkpoint schema.")
        with self._transaction():
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    content_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, kind TEXT NOT NULL,
                    languages TEXT NOT NULL, payload TEXT NOT NULL, input_hash TEXT NOT NULL,
                    identity_key TEXT, baseline_status TEXT NOT NULL, baseline_verified INTEGER NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL, status TEXT, reason TEXT, metadata TEXT, diagnostic TEXT,
                    updated_at REAL, transient INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS items_due ON items(state, next_attempt_at, content_id);
                CREATE INDEX IF NOT EXISTS items_identity_due ON items(identity_key, state, next_attempt_at);
                CREATE TABLE IF NOT EXISTS attempt_events (
                    event_id INTEGER PRIMARY KEY, content_ids TEXT NOT NULL,
                    created_at REAL NOT NULL, stats TEXT NOT NULL, elapsed REAL NOT NULL
                );
            """)
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            else:
                if self.connection.in_transaction:
                    self.connection.execute("COMMIT")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def _meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._meta(key, default)

    def _set_meta(self, key: str, value: Any):
        self.connection.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, _json(value, MAX_META_BYTES)))

    def set_meta(self, key: str, value: Any):
        if not isinstance(key, str) or not SAFE_LABEL.fullmatch(key):
            raise ValueError("Checkpoint metadata key is invalid.")
        if key in {"snapshotIdentity", "inputFingerprint", "recordCount", "startedAt", "maxAttempts"}:
            raise ValueError("Frozen checkpoint metadata cannot be changed.")
        with self._transaction():
            self._set_meta(key, value)

    def initialize(self, records: Iterable[Mapping[str, Any]], snapshot_identity: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze inputs atomically, or validate an exact order-independent resume.

        ``baselineVerified`` must only be set after the caller validates a real
        cached proof under this test's policy. A published matched flag alone is
        retained for comparison but does not cause its verification to be skipped.
        """
        if not isinstance(snapshot_identity, Mapping) or not snapshot_identity:
            raise ValueError("A nonempty source/code/policy snapshot identity is required.")
        snapshot = json.loads(_json(dict(snapshot_identity), MAX_META_BYTES))
        with self._transaction():
            existing = self._meta("snapshotIdentity")
            if existing is not None and (existing != snapshot or self._meta("maxAttempts") != self.max_attempts):
                raise CheckpointMismatch("Checkpoint source/code/policy identity changed; start a new test.")
            self.connection.execute("DROP TABLE IF EXISTS temp.incoming")
            self.connection.execute("CREATE TEMP TABLE incoming (content_id TEXT PRIMARY KEY, "
                                    "input_hash TEXT NOT NULL, values_json TEXT NOT NULL)")
            count = 0
            for record in records:
                if not isinstance(record, Mapping):
                    raise ValueError("Each checkpoint input must be an object.")
                payload = dict(record)
                content_id = payload.get("contentId")
                matched = CONTENT_ID.fullmatch(content_id) if isinstance(content_id, str) else None
                if not matched:
                    raise ValueError("Each input requires a stable server:movie|series:providerID contentId.")
                server, kind, _provider_id = matched.groups()
                if payload.get("serverId", server) != server or payload.get("kind", kind) != kind:
                    raise ValueError("Record server/kind conflicts with contentId.")
                item = payload.get("item", {})
                if not isinstance(item, Mapping):
                    raise ValueError("Normalized item must be an object.")
                languages = payload.get("languages", item.get("languages", []))
                if (not isinstance(languages, list) or len(languages) > 100
                        or any(not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", value) for value in languages)):
                    raise ValueError("Record languages must be bounded language tags.")
                status, verified, metadata = _baseline(payload)
                identity = payload.get("identityKey")
                if identity is not None and (not isinstance(identity, str) or not identity or len(identity) > 4096):
                    raise ValueError("identityKey must be a bounded nonempty string.")
                encoded = _json(payload)
                fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                values = [content_id, server, kind, _json(sorted(set(languages))), encoded, fingerprint,
                          identity, status, int(verified), "done" if verified else "pending",
                          status if verified else None, "reused_verified_cache" if verified else None,
                          _json(metadata) if verified else None]
                try:
                    self.connection.execute("INSERT INTO incoming VALUES (?,?,?)",
                                            (content_id, fingerprint, _json(values, 2 * MAX_RECORD_BYTES)))
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Frozen input contains duplicate contentIds.") from exc
                count += 1
            digest = hashlib.sha256()
            for row in self.connection.execute("SELECT content_id,input_hash FROM incoming ORDER BY content_id"):
                digest.update(_json(list(row)).encode("utf-8"))
                digest.update(b"\n")
            fingerprint = digest.hexdigest()
            if existing is not None:
                if self._meta("inputFingerprint") != fingerprint or self._meta("recordCount") != count:
                    raise CheckpointMismatch("Checkpoint normalized inputs changed; start a new test.")
            else:
                for row in self.connection.execute("SELECT values_json FROM incoming ORDER BY content_id"):
                    self.connection.execute("""INSERT INTO items
                        (content_id,server_id,kind,languages,payload,input_hash,identity_key,
                         baseline_status,baseline_verified,state,status,reason,metadata)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", json.loads(row[0]))
                self._set_meta("snapshotIdentity", snapshot)
                self._set_meta("inputFingerprint", fingerprint)
                self._set_meta("recordCount", count)
                self._set_meta("startedAt", _now())
                self._set_meta("maxAttempts", self.max_attempts)
            self.connection.execute("DROP TABLE incoming")
        return {"recordCount": count, "inputFingerprint": fingerprint, "resumed": existing is not None}

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        payload["checkpoint"] = {"state": row["state"], "attempts": row["attempts"],
                                 "nextAttemptAt": row["next_attempt_at"], "status": row["status"],
                                 "reason": row["reason"]}
        return payload

    def pending(self, limit: int = 1000, now: float | None = None, *, include_transient_retry: bool = True) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1_000_000:
            raise ValueError("Pending limit is invalid.")
        timestamp = _now(now)
        with self._lock:
            states = "state='pending' OR (state='retry' AND next_attempt_at<=?)" if include_transient_retry else "state='pending' AND ?>=0"
            rows = self.connection.execute(f"""SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY server_id,kind ORDER BY attempts,content_id) AS group_row
                FROM items WHERE {states}) ORDER BY group_row,server_id,kind LIMIT ?""", (timestamp, limit))
            return [self._entry(row) for row in rows]

    def pending_group(self, identity_key: str, now: float | None = None) -> list[dict[str, Any]]:
        """Expand one selected identity to every currently due equivalent input.

        Selection batching limits discovery, not sharing a completed lookup.
        A future retry remains deferred even when another source with the same
        constraint-complete identity is currently due.
        """
        if not isinstance(identity_key, str) or not identity_key or len(identity_key) > 4096:
            raise ValueError("identity_key must be a bounded nonempty string.")
        timestamp = _now(now)
        with self._lock:
            rows = self.connection.execute("""SELECT * FROM items WHERE identity_key=?
                AND (state='pending' OR (state='retry' AND next_attempt_at<=?))
                ORDER BY attempts,content_id""", (identity_key, timestamp))
            return [self._entry(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def records(self) -> list[dict[str, Any]]:
        """Return the frozen inputs, in stable logical-ID order."""
        with self._lock:
            return [self._entry(row) for row in self.connection.execute("SELECT * FROM items ORDER BY content_id")]

    def completed_records(self, verified_only: bool = True) -> list[dict[str, Any]]:
        """Return saved outcomes with inputs for current-policy cache revalidation.

        This does not itself confer authority on a stored result. The caller
        must still revalidate actual matching proof before seeding a shared
        poster index, exactly as it would for any restored verified cache.
        """
        with self._lock:
            query = "SELECT * FROM items WHERE state='done'"
            parameters = ()
            if verified_only:
                query += " AND status IN (?,?,?,?)"
                parameters = tuple(sorted(MATCHED))
            rows = self.connection.execute(query + " ORDER BY content_id", parameters)
            result = []
            for row in rows:
                payload = self._entry(row)
                payload["result"] = {"metadata": json.loads(row["metadata"]) if row["metadata"] else None,
                                     "diagnostic": json.loads(row["diagnostic"]) if row["diagnostic"] else None,
                                     "transient": bool(row["transient"])}
                result.append(payload)
            return result

    def get(self, content_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM items WHERE content_id=?", (content_id,)).fetchone()
            if row is None:
                return None
            result = self._entry(row)
            result["result"] = {"metadata": json.loads(row["metadata"]) if row["metadata"] else None,
                                "diagnostic": json.loads(row["diagnostic"]) if row["diagnostic"] else None,
                                "transient": bool(row["transient"])}
            return result

    def record_result(self, content_id: str | Sequence[str], metadata: Mapping[str, Any],
                      diagnostic: Mapping[str, Any], stats: Mapping[str, int | float] | None = None,
                      elapsed: float = 0, *, now: float | None = None, transient: bool | None = None,
                      retry_after: float | None = None) -> bool:
        """Commit an outcome and usage together; return False for an exact replay.

        Negative outcomes finish this frozen pass only. Transient outcomes remain
        due until ``max_attempts`` then finish as unresolved transient failures;
        they are never relabelled as definitive non-matches.
        """
        ids = [content_id] if isinstance(content_id, str) else list(content_id)
        if not ids or len(ids) != len(set(ids)) or len(ids) > 1_000_000:
            raise ValueError("Result requires unique checkpoint contentIds.")
        if not isinstance(metadata, Mapping) or not isinstance(diagnostic, Mapping):
            raise ValueError("Result metadata and diagnostic must be objects.")
        status = _status(metadata)
        reason = diagnostic.get("reason", status)
        if not isinstance(reason, str) or not SAFE_LABEL.fullmatch(reason):
            raise ValueError("Result reason must be a safe diagnostic label.")
        if transient is not None and not isinstance(transient, bool):
            raise ValueError("transient must be boolean or None.")
        temporary = status not in MATCHED and (status == "unavailable" or reason not in DEFINITIVE)
        if transient is not None:
            temporary = (transient or status == "unavailable") and status not in MATCHED
        if temporary and status == "no_match":
            # An interrupted/incomplete negative lookup is not a definitive
            # non-match, even if a caller handed us a negative-shaped payload.
            metadata = {**metadata, "matchStatus": "unavailable"}
            status = "unavailable"
        timestamp, elapsed = _now(now), _number(elapsed, "Elapsed time")
        delay = _number(retry_after, "Retry delay") if retry_after is not None else None
        usage = dict(stats or {})
        if any(not isinstance(key, str) or not SAFE_LABEL.fullmatch(key) for key in usage):
            raise ValueError("API statistic names must be safe labels.")
        for key, value in usage.items():
            _number(value, "API statistic")
        if reason in DEFERRED and status not in MATCHED:
            return self.defer(ids, reason, timestamp + (delay if delay is not None else 5),
                              stats=usage, elapsed=elapsed, now=timestamp)
        metadata_json, diagnostic_json = _json(dict(metadata)), _json(dict(diagnostic))
        with self._transaction():
            rows = []
            for identifier in ids:
                row = self.connection.execute("SELECT * FROM items WHERE content_id=?", (identifier,)).fetchone()
                if row is None:
                    raise KeyError("Result contentId is absent from the frozen checkpoint.")
                rows.append(row)
            if len(rows) > 1 and (not rows[0]["identity_key"] or any(row["identity_key"] != rows[0]["identity_key"] for row in rows)):
                raise ValueError("Shared results require one constraint-complete identityKey.")
            done = [row for row in rows if row["state"] == "done"]
            if done:
                if len(done) == len(rows) and all(row["metadata"] == metadata_json and row["diagnostic"] == diagnostic_json for row in done):
                    return False
                raise ValueError("A completed result cannot be overwritten inside a frozen pass.")
            for row in rows:
                attempts = row["attempts"] + 1
                state = "retry" if temporary and attempts < self.max_attempts else "done"
                due = timestamp + (delay if delay is not None else min(300, 5 * 2 ** (attempts - 1))) if state == "retry" else None
                self.connection.execute("""UPDATE items SET state=?,attempts=?,next_attempt_at=?,
                    status=?,reason=?,metadata=?,diagnostic=?,updated_at=?,transient=? WHERE content_id=?""",
                    (state, attempts, due, status, reason, metadata_json, diagnostic_json, timestamp,
                     int(temporary), row["content_id"]))
            self.connection.execute("INSERT INTO attempt_events(content_ids,created_at,stats,elapsed) VALUES (?,?,?,?)",
                                    (_json(ids, MAX_META_BYTES), timestamp, _json(usage), elapsed))
        return True

    def defer(self, content_id: str | Sequence[str], reason: str, retry_at: float,
              *, stats: Mapping[str, int | float] | None = None, elapsed: float = 0,
              now: float | None = None) -> bool:
        """Persist a cooldown or session interruption without spending an attempt."""
        ids = [content_id] if isinstance(content_id, str) else list(content_id)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Deferral requires unique contentIds.")
        if not isinstance(reason, str) or not SAFE_LABEL.fullmatch(reason):
            raise ValueError("Deferral reason must be a safe diagnostic label.")
        retry_at, timestamp = _number(retry_at, "Retry timestamp"), _now(now)
        elapsed = _number(elapsed, "Elapsed time")
        usage = dict(stats or {})
        for key, value in usage.items():
            if not isinstance(key, str) or not SAFE_LABEL.fullmatch(key):
                raise ValueError("API statistic names must be safe labels.")
            _number(value, "API statistic")
        with self._transaction():
            for identifier in ids:
                row = self.connection.execute("SELECT state FROM items WHERE content_id=?", (identifier,)).fetchone()
                if row is None:
                    raise KeyError("Deferred contentId is absent from the frozen checkpoint.")
                if row["state"] == "done":
                    raise ValueError("Completed results cannot be deferred.")
            for identifier in ids:
                self.connection.execute("""UPDATE items SET state='retry',next_attempt_at=?,
                    status='unavailable',reason=?,diagnostic=?,updated_at=?,transient=1 WHERE content_id=?""",
                    (retry_at, reason, _json({"reason": reason}), timestamp, identifier))
            if usage or elapsed:
                self.connection.execute("INSERT INTO attempt_events(content_ids,created_at,stats,elapsed) VALUES (?,?,?,?)",
                                        (_json(ids, MAX_META_BYTES), timestamp, _json(usage), elapsed))
        return True

    def reopen(self, content_id: str | Sequence[str], reason: str = "poster_collision") -> int:
        """Revoke completed poster proof when a later official identity collides.

        Only completed ``matched_poster`` rows can reopen. Ordinary verified
        identities and completed negative outcomes remain immutable. This new
        evidence starts fresh retry accounting while preserving all historical
        attempt events, request totals and elapsed time.
        """
        ids = [content_id] if isinstance(content_id, str) else list(content_id)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Reopening requires unique contentIds.")
        if not isinstance(reason, str) or not SAFE_LABEL.fullmatch(reason):
            raise ValueError("Reopening reason must be a safe diagnostic label.")
        with self._transaction():
            rows = []
            for identifier in ids:
                row = self.connection.execute("SELECT state,status FROM items WHERE content_id=?", (identifier,)).fetchone()
                if row is None:
                    raise KeyError("Reopened contentId is absent from the frozen checkpoint.")
                if row["state"] == "done" and row["status"] == "matched_poster":
                    rows.append(identifier)
            for identifier in rows:
                self.connection.execute("""UPDATE items SET state='pending',attempts=0,
                    next_attempt_at=NULL,status=NULL,reason=?,metadata=NULL,diagnostic=?,
                    updated_at=?,transient=0,baseline_verified=0 WHERE content_id=?""",
                    (reason, _json({"reason": reason, "revokedStatus": "matched_poster"}), _now(), identifier))
            return len(rows)

    def report(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = _now(now)
        with self._lock:
            totals = Counter(total=0, completed=0, remaining=0, matched=0, newMatches=0,
                             previouslyVerified=0, reusedVerifiedCaches=0, unresolved=0,
                             definitiveUnresolved=0, transientUnresolved=0, due=0,
                             retainedVerified=0, lostVerification=0, attempted=0)
            statuses, reasons, servers, kinds, languages = Counter(), Counter(), {}, {}, {}

            def bucket(target, key):
                return target.setdefault(key, Counter(total=0, completed=0, remaining=0,
                                                       matched=0, newMatches=0, unresolved=0))

            earliest = None
            for row in self.connection.execute("""SELECT server_id,kind,languages,state,status,
                    baseline_status,baseline_verified,reason,transient,attempts,next_attempt_at FROM items"""):
                matched = row["state"] == "done" and row["status"] in MATCHED
                old = row["baseline_status"] in MATCHED
                done = row["state"] == "done"
                new = matched and not old
                totals["total"] += 1
                totals["completed" if done else "remaining"] += 1
                totals["previouslyVerified"] += old
                totals["matched"] += matched
                totals["newMatches"] += new
                totals["retainedVerified"] += matched and old
                totals["lostVerification"] += done and old and not matched
                totals["attempted"] += row["attempts"] > 0
                totals["reusedVerifiedCaches"] += row["baseline_verified"]
                unresolved = done and not matched
                totals["unresolved"] += unresolved
                if unresolved:
                    totals["transientUnresolved" if row["transient"] else "definitiveUnresolved"] += 1
                    reasons[row["reason"] or "unavailable"] += 1
                statuses[row["status"] or "pending"] += 1
                if row["state"] == "pending" or (row["state"] == "retry" and row["next_attempt_at"] <= timestamp):
                    totals["due"] += 1
                if row["state"] == "retry":
                    earliest = row["next_attempt_at"] if earliest is None else min(earliest, row["next_attempt_at"])
                targets = [bucket(servers, row["server_id"]), bucket(kinds, row["kind"])]
                tags = json.loads(row["languages"]) or ["unknown"]
                targets.extend(bucket(languages, tag) for tag in tags)
                for values in targets:
                    values["total"] += 1
                    values["completed" if done else "remaining"] += 1
                    values["matched"] += matched
                    values["newMatches"] += new
                    values["unresolved"] += unresolved
            usage, active_elapsed, events = Counter(), 0.0, 0
            for row in self.connection.execute("SELECT stats,elapsed FROM attempt_events"):
                usage.update(json.loads(row["stats"]))
                active_elapsed += row["elapsed"]
                events += 1
            started = self._meta("startedAt")
            return {"schemaVersion": SCHEMA_VERSION, "snapshotIdentity": self._meta("snapshotIdentity"),
                    "inputFingerprint": self._meta("inputFingerprint"), "totals": dict(totals),
                    "complete": totals["remaining"] == 0, "hasUnfinished": totals["remaining"] > 0,
                    "byStatus": dict(statuses),
                    "unresolvedReasons": dict(reasons), "byServer": servers, "byKind": kinds,
                    "byLanguage": languages, "apiUsage": dict(usage), "attemptEvents": events,
                    "activeElapsedSeconds": active_elapsed,
                    "wallElapsedSeconds": max(0.0, timestamp - started) if started is not None else 0.0,
                    "nextAttemptAt": earliest}
