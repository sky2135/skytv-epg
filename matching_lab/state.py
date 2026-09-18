"""Bounded local observation ledger for Matching Lab runs and proposals.

The ledger is deliberately optional and has no network or remote-write code.
It stores only fixed decision metadata, content hashes, and reason codes: channel
names, candidate prose, prompts, model responses, and secrets are never stored.

Through this API, rows in the observation tables are insert-once. Replaying an
identical run, proposal, or validation is idempotent; reusing an identity with
different content is treated as an integrity error. Every observation is also
committed to a SHA-256 chain for local consistency checks. The database has no
external signature or anchor and is not proof against direct SQLite tampering.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .models import (
    ProposalRecord,
    RunManifest,
    canonical_json_bytes,
    require_code,
    require_sha256,
    safe_text,
    sha256_bytes,
    sha256_json,
)


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4D4C4447  # "MLDG"
GENESIS_SHA256 = "0" * 64
DEFAULT_MAX_EVENTS = 1_000_000
DEFAULT_MAX_DATABASE_BYTES = 1_073_741_824
MAX_EVENT_PAYLOAD_BYTES = 8_192
MAX_REASON_CODES = 32


class LedgerError(RuntimeError):
    """The local observation ledger rejected an operation."""


class LedgerIntegrityError(LedgerError):
    """Stored state or a replayed identity has inconsistent content."""


class LedgerCapacityError(LedgerError):
    """A configured hard capacity limit was reached."""


def _utc_text(value: object, *, label: str) -> str:
    text = safe_text(value, maximum=40).strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LedgerError(f"{label} must be an ISO-8601 UTC timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise LedgerError(f"{label} must include the UTC timezone.")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_sha256(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    return require_sha256(text, label=label) if text else ""


def _reason_codes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise LedgerError("reason_codes must be a sequence of fixed codes.")
    reasons = tuple(sorted({require_code(value, label="validation reason") for value in values}))
    if len(reasons) > MAX_REASON_CODES:
        raise LedgerError("A validation observation has too many reason codes.")
    return reasons


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    """One sanitized result from a proposal validator or guarded writer.

    ``context_sha256`` can bind a private diagnostic artifact without copying
    it into SQLite. ``current_*_sha256`` records freshness guards observed by
    the validator. There is intentionally no free-form message field.
    """

    proposal_id: str
    observed_at: str
    outcome: str
    reason_codes: tuple[str, ...]
    validator_version: str
    current_row_guard_sha256: str = ""
    current_provider_identity_sha256: str = ""
    context_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposal_id",
            require_sha256(self.proposal_id, label="validation proposal_id"),
        )
        object.__setattr__(
            self, "observed_at", _utc_text(self.observed_at, label="observed_at")
        )
        object.__setattr__(
            self, "outcome", require_code(self.outcome, label="validation outcome")
        )
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        version = safe_text(self.validator_version, maximum=80).strip()
        if not version:
            raise LedgerError("validator_version must not be blank.")
        object.__setattr__(self, "validator_version", version)
        for name, label in (
            ("current_row_guard_sha256", "current row guard"),
            ("current_provider_identity_sha256", "current provider identity"),
            ("context_sha256", "validation context"),
        ):
            object.__setattr__(
                self,
                name,
                _optional_sha256(getattr(self, name), label=label),
            )

    def public_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "observed_at": self.observed_at,
            "outcome": self.outcome,
            "reason_codes": list(self.reason_codes),
            "validator_version": self.validator_version,
            "current_row_guard_sha256": self.current_row_guard_sha256,
            "current_provider_identity_sha256": self.current_provider_identity_sha256,
            "context_sha256": self.context_sha256,
        }

    @property
    def validation_id(self) -> str:
        return sha256_json(self.public_dict())


@dataclass(frozen=True, slots=True)
class LedgerStats:
    schema_version: int
    runs: int
    proposals: int
    validations: int
    events: int
    head_sha256: str


_SCHEMA_V1 = (
    """
    CREATE TABLE ledger_meta (
        key TEXT PRIMARY KEY NOT NULL,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        lab_code_sha256 TEXT NOT NULL,
        proposal_count INTEGER NOT NULL CHECK (proposal_count >= 0),
        input_sha256_json TEXT NOT NULL,
        counts_json TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE proposals (
        proposal_id TEXT PRIMARY KEY NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        server_id TEXT NOT NULL,
        stream_id TEXT NOT NULL,
        decision_state TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        selected_candidate_key TEXT NOT NULL,
        score_ppm INTEGER NOT NULL CHECK (score_ppm BETWEEN 0 AND 1000000),
        margin_ppm INTEGER NOT NULL CHECK (margin_ppm BETWEEN 0 AND 1000000),
        row_guard_sha256 TEXT NOT NULL,
        provider_identity_sha256 TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        lab_code_sha256 TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE validation_observations (
        validation_id TEXT PRIMARY KEY NOT NULL,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        observed_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        validator_version TEXT NOT NULL,
        current_row_guard_sha256 TEXT NOT NULL,
        current_provider_identity_sha256 TEXT NOT NULL,
        context_sha256 TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX validation_by_proposal_time
        ON validation_observations(proposal_id, observed_at, validation_id)
    """,
    """
    CREATE TABLE events (
        sequence INTEGER PRIMARY KEY,
        event_id TEXT UNIQUE NOT NULL,
        event_type TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        previous_chain_sha256 TEXT NOT NULL,
        chain_sha256 TEXT UNIQUE NOT NULL
    )
    """,
    "CREATE INDEX events_by_subject ON events(subject_id, sequence)",
)


class ObservationLedger:
    """A local, bounded, append-only observation ledger.

    The caller owns lifecycle and should use this object as a context manager.
    All mutating methods use ``BEGIN IMMEDIATE`` and full synchronous commits.
    SQLite WAL is enabled to keep readers non-blocking while one writer records
    a result. Capacity limits fail closed; this module never prunes audit data.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
        timeout_seconds: float = 30.0,
    ) -> None:
        if isinstance(max_events, bool) or int(max_events) < 1:
            raise LedgerError("max_events must be a positive integer.")
        if isinstance(max_database_bytes, bool) or int(max_database_bytes) < 1_048_576:
            raise LedgerError("max_database_bytes must be at least 1 MiB.")
        if float(timeout_seconds) <= 0:
            raise LedgerError("timeout_seconds must be positive.")
        requested_path = Path(path).expanduser()
        self.path = (
            requested_path
            if requested_path.is_absolute()
            else Path.cwd() / requested_path
        )
        self.max_events = int(max_events)
        self.max_database_bytes = int(max_database_bytes)
        self._closed = False
        self._prepare_path()
        try:
            self._connection = sqlite3.connect(
                str(self.path),
                timeout=float(timeout_seconds),
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._initialize_schema()
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                raise LedgerError("Unable to restrict ledger file permissions.") from exc
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    def _prepare_path(self) -> None:
        if not self.path.parent.exists() or not self.path.parent.is_dir():
            raise LedgerError("The ledger parent directory does not exist.")
        if self.path.exists():
            mode = self.path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise LedgerError("The ledger path must be a regular non-symlink file.")

    def _configure(self) -> None:
        connection = self._connection
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        journal = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).upper()
        if journal != "WAL":
            raise LedgerError("SQLite refused WAL journal mode.")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        maximum_pages = max(1, self.max_database_bytes // page_size)
        actual_maximum = int(
            connection.execute(f"PRAGMA max_page_count = {maximum_pages}").fetchone()[0]
        )
        if actual_maximum * page_size > self.max_database_bytes:
            raise LedgerError("SQLite database size cap could not be installed.")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise LedgerError("The observation ledger is closed.")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _initialize_schema(self) -> None:
        application_id = int(self._connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id not in (0, APPLICATION_ID):
            raise LedgerIntegrityError("The SQLite file belongs to another application.")
        if version > SCHEMA_VERSION:
            raise LedgerIntegrityError("The ledger schema is newer than this code supports.")
        if version == 0:
            existing = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing:
                raise LedgerIntegrityError("An unversioned SQLite file cannot be adopted.")
            with self._transaction() as connection:
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES ('event_count', '0')"
                )
                connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES ('chain_head_sha256', ?)",
                    (GENESIS_SHA256,),
                )
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        stored = self._connection.execute(
            "SELECT value FROM ledger_meta WHERE key='schema_version'"
        ).fetchone()
        if version != SCHEMA_VERSION or stored is None or int(stored[0]) != SCHEMA_VERSION:
            raise LedgerIntegrityError("Ledger schema version metadata is inconsistent.")

    def __enter__(self) -> "ObservationLedger":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _database_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            try:
                total += candidate.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _ensure_event_capacity(self, connection: sqlite3.Connection) -> None:
        count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        if count >= self.max_events:
            raise LedgerCapacityError("The ledger event limit has been reached.")
        if self._database_size_bytes() >= self.max_database_bytes:
            raise LedgerCapacityError("The ledger byte limit has been reached.")

    @staticmethod
    def _insert_once(
        connection: sqlite3.Connection,
        *,
        table: str,
        identity_column: str,
        identity: str,
        content_column: str,
        content_sha256: str,
        sql: str,
        values: tuple[object, ...],
    ) -> bool:
        cursor = connection.execute(sql, values)
        if cursor.rowcount == 1:
            return True
        row = connection.execute(
            f"SELECT {content_column} FROM {table} WHERE {identity_column} = ?",
            (identity,),
        ).fetchone()
        if row is None or str(row[0]) != content_sha256:
            raise LedgerIntegrityError(
                f"{table} identity was replayed with different content."
            )
        return False

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        observed_at: str,
        subject_id: str,
        data: dict[str, object],
    ) -> str:
        payload = {
            "event_type": require_code(event_type, label="event type"),
            "observed_at": _utc_text(observed_at, label="event observed_at"),
            "subject_id": require_sha256(subject_id, label="event subject_id"),
            "data": data,
        }
        payload_bytes = canonical_json_bytes(payload)
        if len(payload_bytes) > MAX_EVENT_PAYLOAD_BYTES:
            raise LedgerError("The sanitized event payload exceeds its size limit.")
        payload_sha256 = sha256_bytes(payload_bytes)
        event_id = payload_sha256
        existing = connection.execute(
            "SELECT payload_sha256 FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != payload_sha256:
                raise LedgerIntegrityError("An event identity has inconsistent content.")
            return event_id
        self._ensure_event_capacity(connection)
        head = connection.execute(
            "SELECT sequence, chain_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = (int(head[0]) + 1) if head is not None else 1
        previous = str(head[1]) if head is not None else GENESIS_SHA256
        chain_sha256 = sha256_json(
            {
                "event_id": event_id,
                "previous_chain_sha256": previous,
                "sequence": sequence,
            }
        )
        connection.execute(
            """
            INSERT INTO events(
                sequence, event_id, event_type, observed_at, subject_id,
                payload_json, payload_sha256, previous_chain_sha256, chain_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_id,
                payload["event_type"],
                payload["observed_at"],
                payload["subject_id"],
                payload_bytes.decode("utf-8"),
                payload_sha256,
                previous,
                chain_sha256,
            ),
        )
        connection.execute(
            "UPDATE ledger_meta SET value = ? WHERE key = 'event_count'",
            (str(sequence),),
        )
        connection.execute(
            "UPDATE ledger_meta SET value = ? WHERE key = 'chain_head_sha256'",
            (chain_sha256,),
        )
        return event_id

    def record_run(self, manifest: RunManifest) -> str:
        """Insert or verify one run and return its deterministic event ID."""

        manifest_dict = manifest.public_dict()
        manifest_sha256 = sha256_json(manifest_dict)
        generated_at = _utc_text(manifest.generated_at, label="run generated_at")
        expires_at = _utc_text(manifest.expires_at, label="run expires_at")
        inputs_json = canonical_json_bytes(dict(manifest.input_sha256)).decode("utf-8")
        counts_json = canonical_json_bytes(dict(manifest.counts)).decode("utf-8")
        mode = safe_text(manifest.mode, maximum=40).strip()
        if not mode:
            raise LedgerError("Run mode must not be blank.")
        with self._transaction() as connection:
            self._insert_once(
                connection,
                table="runs",
                identity_column="run_id",
                identity=manifest.run_id,
                content_column="manifest_sha256",
                content_sha256=manifest_sha256,
                sql="""
                    INSERT INTO runs(
                        run_id, manifest_sha256, generated_at, expires_at, mode,
                        policy_sha256, lab_code_sha256, proposal_count,
                        input_sha256_json, counts_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO NOTHING
                """,
                values=(
                    manifest.run_id,
                    manifest_sha256,
                    generated_at,
                    expires_at,
                    mode,
                    manifest.policy_sha256,
                    manifest.lab_code_sha256,
                    manifest.proposal_count,
                    inputs_json,
                    counts_json,
                ),
            )
            return self._append_event(
                connection,
                event_type="RUN_OBSERVED",
                observed_at=generated_at,
                subject_id=manifest.run_id,
                data={
                    "manifest_sha256": manifest_sha256,
                    "mode": mode,
                    "proposal_count": manifest.proposal_count,
                },
            )

    def record_proposal(self, proposal: ProposalRecord) -> str:
        """Insert or verify one proposal and return its deterministic event ID."""

        proposal.verify_id()
        content_sha256 = sha256_json(proposal.public_dict())
        created_at = _utc_text(proposal.created_at, label="proposal created_at")
        expires_at = _utc_text(proposal.expires_at, label="proposal expires_at")
        reasons_json = canonical_json_bytes(list(proposal.reason_codes)).decode("utf-8")
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT policy_sha256, lab_code_sha256 FROM runs WHERE run_id = ?",
                (proposal.run_id,),
            ).fetchone()
            if run is None:
                raise LedgerIntegrityError("The proposal run must be recorded first.")
            if str(run[0]) != proposal.policy_sha256 or str(run[1]) != proposal.lab_code_sha256:
                raise LedgerIntegrityError("Proposal evidence does not match its recorded run.")
            self._insert_once(
                connection,
                table="proposals",
                identity_column="proposal_id",
                identity=proposal.proposal_id,
                content_column="content_sha256",
                content_sha256=content_sha256,
                sql="""
                    INSERT INTO proposals(
                        proposal_id, run_id, content_sha256, created_at, expires_at,
                        server_id, stream_id, decision_state, reason_codes_json,
                        selected_candidate_key, score_ppm, margin_ppm,
                        row_guard_sha256, provider_identity_sha256, source_sha256,
                        policy_sha256, lab_code_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(proposal_id) DO NOTHING
                """,
                values=(
                    proposal.proposal_id,
                    proposal.run_id,
                    content_sha256,
                    created_at,
                    expires_at,
                    proposal.server_id,
                    proposal.stream_id,
                    proposal.state.value,
                    reasons_json,
                    proposal.selected_candidate_key,
                    proposal.score_ppm,
                    proposal.margin_ppm,
                    proposal.row_guard_sha256,
                    proposal.provider_identity_sha256,
                    proposal.source_sha256,
                    proposal.policy_sha256,
                    proposal.lab_code_sha256,
                ),
            )
            return self._append_event(
                connection,
                event_type="PROPOSAL_OBSERVED",
                observed_at=created_at,
                subject_id=proposal.proposal_id,
                data={
                    "content_sha256": content_sha256,
                    "decision_state": proposal.state.value,
                    "margin_ppm": proposal.margin_ppm,
                    "run_id": proposal.run_id,
                    "score_ppm": proposal.score_ppm,
                    "selected_candidate_key": proposal.selected_candidate_key,
                },
            )

    def record_validation(self, observation: ValidationObservation) -> str:
        """Append one idempotent, sanitized validation outcome."""

        validation_id = observation.validation_id
        public = observation.public_dict()
        reasons_json = canonical_json_bytes(list(observation.reason_codes)).decode("utf-8")
        with self._transaction() as connection:
            proposal = connection.execute(
                "SELECT run_id FROM proposals WHERE proposal_id = ?",
                (observation.proposal_id,),
            ).fetchone()
            if proposal is None:
                raise LedgerIntegrityError("The validated proposal must be recorded first.")
            self._insert_once(
                connection,
                table="validation_observations",
                identity_column="validation_id",
                identity=validation_id,
                content_column="validation_id",
                content_sha256=validation_id,
                sql="""
                    INSERT INTO validation_observations(
                        validation_id, proposal_id, observed_at, outcome,
                        reason_codes_json, validator_version,
                        current_row_guard_sha256,
                        current_provider_identity_sha256, context_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(validation_id) DO NOTHING
                """,
                values=(
                    validation_id,
                    observation.proposal_id,
                    observation.observed_at,
                    observation.outcome,
                    reasons_json,
                    observation.validator_version,
                    observation.current_row_guard_sha256,
                    observation.current_provider_identity_sha256,
                    observation.context_sha256,
                ),
            )
            return self._append_event(
                connection,
                event_type="VALIDATION_OBSERVED",
                observed_at=observation.observed_at,
                subject_id=observation.proposal_id,
                data={
                    "run_id": str(proposal[0]),
                    "validation_id": validation_id,
                    **public,
                },
            )

    def latest_validation(self, proposal_id: str) -> ValidationObservation | None:
        """Return the latest validation by caller-supplied UTC time, if any."""

        identity = require_sha256(proposal_id, label="proposal_id")
        row = self._connection.execute(
            """
            SELECT proposal_id, observed_at, outcome, reason_codes_json,
                   validator_version, current_row_guard_sha256,
                   current_provider_identity_sha256, context_sha256
            FROM validation_observations
            WHERE proposal_id = ?
            ORDER BY observed_at DESC, validation_id DESC
            LIMIT 1
            """,
            (identity,),
        ).fetchone()
        if row is None:
            return None
        return ValidationObservation(
            proposal_id=str(row[0]),
            observed_at=str(row[1]),
            outcome=str(row[2]),
            reason_codes=tuple(json.loads(str(row[3]))),
            validator_version=str(row[4]),
            current_row_guard_sha256=str(row[5]),
            current_provider_identity_sha256=str(row[6]),
            context_sha256=str(row[7]),
        )

    def stats(self) -> LedgerStats:
        """Return bounded aggregate metadata without exposing private rows."""

        if self._closed:
            raise LedgerError("The observation ledger is closed.")
        counts = {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("runs", "proposals", "validation_observations", "events")
        }
        head = self._connection.execute(
            "SELECT chain_sha256 FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return LedgerStats(
            schema_version=SCHEMA_VERSION,
            runs=counts["runs"],
            proposals=counts["proposals"],
            validations=counts["validation_observations"],
            events=counts["events"],
            head_sha256=str(head[0]) if head is not None else GENESIS_SHA256,
        )

    def verify_chain(self) -> LedgerStats:
        """Recompute every event/content link and return stats on success."""

        if self._closed:
            raise LedgerError("The observation ledger is closed.")
        previous = GENESIS_SHA256
        expected_sequence = 1
        rows = self._connection.execute(
            """
            SELECT sequence, event_id, event_type, observed_at, subject_id,
                   payload_json, payload_sha256, previous_chain_sha256, chain_sha256
            FROM events ORDER BY sequence
            """
        )
        for row in rows:
            sequence = int(row[0])
            if sequence != expected_sequence:
                raise LedgerIntegrityError("The event chain has a sequence gap.")
            try:
                payload_value = json.loads(str(row[5]))
                canonical = canonical_json_bytes(payload_value)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LedgerIntegrityError("An event payload is not valid canonical JSON.") from exc
            if canonical.decode("utf-8") != str(row[5]):
                raise LedgerIntegrityError("An event payload is not canonically encoded.")
            payload_sha256 = sha256_bytes(canonical)
            if payload_sha256 != str(row[6]) or payload_sha256 != str(row[1]):
                raise LedgerIntegrityError("An event payload hash is invalid.")
            if str(row[7]) != previous:
                raise LedgerIntegrityError("An event previous-chain hash is invalid.")
            if payload_value.get("event_type") != str(row[2]):
                raise LedgerIntegrityError("An event type does not match its payload.")
            if payload_value.get("observed_at") != str(row[3]):
                raise LedgerIntegrityError("An event timestamp does not match its payload.")
            if payload_value.get("subject_id") != str(row[4]):
                raise LedgerIntegrityError("An event subject does not match its payload.")
            expected_chain = sha256_json(
                {
                    "event_id": str(row[1]),
                    "previous_chain_sha256": previous,
                    "sequence": sequence,
                }
            )
            if expected_chain != str(row[8]):
                raise LedgerIntegrityError("An event chain hash is invalid.")
            previous = expected_chain
            expected_sequence += 1
        metadata = dict(
            self._connection.execute(
                "SELECT key, value FROM ledger_meta WHERE key IN ('event_count', 'chain_head_sha256')"
            ).fetchall()
        )
        if metadata.get("event_count") != str(expected_sequence - 1):
            raise LedgerIntegrityError("The durable event-count checkpoint is invalid.")
        if metadata.get("chain_head_sha256") != previous:
            raise LedgerIntegrityError("The durable chain-head checkpoint is invalid.")
        return self.stats()


__all__ = (
    "APPLICATION_ID",
    "DEFAULT_MAX_DATABASE_BYTES",
    "DEFAULT_MAX_EVENTS",
    "GENESIS_SHA256",
    "LedgerCapacityError",
    "LedgerError",
    "LedgerIntegrityError",
    "LedgerStats",
    "ObservationLedger",
    "SCHEMA_VERSION",
    "ValidationObservation",
)
