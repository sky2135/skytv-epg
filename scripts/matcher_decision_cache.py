"""Private, authenticated memoization of provisional deterministic decisions.

These files are not approvals. They omit source hashes and matcher identity;
each hit is rebound to the current catalog and still passes the current-run
catalog ambiguity and programme gates. Never publish the plaintext directory.
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
import math
import os
import re
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from skytv_epg_auto_match_v1 import (
    CatalogSnapshot,
    MatchProposal,
    MatcherIdentity,
    SAFE_DUMMY_METHODS,
    SAFE_REAL_METHODS,
)


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 50_000
_FIELDS = frozenset(
    field.name for field in fields(MatchProposal)
    if field.name not in {"matcher_identity", "catalog_source_sha256"}
)
_BOOL_FIELDS = frozenset({"route_explicit", "eligible_for_finalization"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Cache dependency keys must be text")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=lambda item: _encoded(item))
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Unsupported cache dependency type")


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def dependency_sha256(value: Any) -> str:
    return hashlib.sha256(_encoded(_plain(value))).hexdigest()


def rules_sha256(paths: Iterable[Path]) -> str:
    """Hash actual adapter/knowledge code, not just declared version labels."""
    records = []
    for path in paths:
        path = Path(path)
        records.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    return dependency_sha256(records)


def _environment_key() -> bytes | None:
    explicit = os.environ.get("SKYTV_MATCH_CACHE_KEY", "")
    if explicit:
        secret = explicit.encode("utf-8")
    else:
        try:
            raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
            if not raw or len(raw) > 256 * 1024:
                return None
            if not raw.startswith("{"):
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            account = json.loads(raw)
            private_key = account.get("private_key", "")
            if not isinstance(private_key, str) or not private_key:
                return None
            secret = private_key.encode("utf-8")
        except (ValueError, AttributeError, UnicodeError):
            return None
    if len(secret) < 16:
        return None
    return hashlib.sha256(b"SKYTV matcher decision HMAC v1\0" + secret).digest()


def _safe_directory(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Private matcher cache cannot use symlinks")


class DecisionCache:
    """A miss or cache I/O failure always falls back to normal matching."""

    def __init__(self, directory: Path | None, key: bytes | None):
        self.directory = Path(directory) if directory is not None else None
        self.key = key
        self.stats = {"enabled": int(self.directory is not None and key is not None),
                      "hits": 0, "misses": 0, "invalidations": 0,
                      "rejectedFiles": 0, "savedEntries": 0, "writeErrors": 0}
        self._scopes: list[DecisionScope] = []

    @classmethod
    def from_environment(cls) -> "DecisionCache":
        directory = os.environ.get("SKYTV_MATCH_CACHE_DIR", "").strip()
        return cls(Path(directory) if directory else None, _environment_key())

    def scope(self, server_id: str, dependency: str) -> "DecisionScope":
        if not re.fullmatch(r"server_[1-9][0-9]*", server_id) or not _HASH.fullmatch(dependency):
            raise ValueError("Invalid private matcher cache scope")
        scope = DecisionScope(self, server_id, dependency)
        self._scopes.append(scope)
        return scope

    def save(self) -> None:
        for scope in self._scopes:
            scope.save()


class DecisionScope:
    def __init__(self, owner: DecisionCache, server_id: str, dependency: str):
        self.owner, self.server_id, self.dependency = owner, server_id, dependency
        self.entries: dict[str, dict[str, Any]] = {}
        self.path = (owner.directory / (hashlib.sha256(server_id.encode()).hexdigest() + ".json")
                     if owner.directory is not None else None)
        if not owner.stats["enabled"]:
            return
        try:
            assert self.path is not None
            _safe_directory(self.path.parent)
            if self.path.is_symlink():
                raise ValueError("Cache file is a symlink")
            if not self.path.exists():
                return
            if not self.path.is_file() or self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Cache file is oversized")
            raw = self.path.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Cache file is oversized")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
                raise ValueError("Cache envelope is invalid")
            signature = envelope["signature"]
            payload = envelope["payload"]
            if not isinstance(signature, str) or not _HASH.fullmatch(signature):
                raise ValueError("Cache signature is invalid")
            expected = hmac.new(owner.key, _encoded(payload), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Cache authentication failed")
            if not isinstance(payload, dict) or set(payload) != {
                "schemaVersion", "serverId", "dependencySha256", "entries"
            } or type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != SCHEMA_VERSION:
                raise ValueError("Cache schema is invalid")
            if payload["serverId"] != server_id or payload["dependencySha256"] != dependency:
                owner.stats["invalidations"] += 1
                return
            entries = payload["entries"]
            if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
                raise ValueError("Cache entry collection is invalid")
            # Validate the entire authenticated file before accepting any hit.
            for stream_id, record in entries.items():
                self._validate_record(stream_id, record)
            self.entries = entries
        except (OSError, ValueError, TypeError, RecursionError, AssertionError):
            owner.stats["rejectedFiles"] += 1

    def _validate_record(self, stream_id: str, record: Any) -> None:
        if not isinstance(stream_id, str) or not stream_id or len(stream_id) > 120:
            raise ValueError("Invalid cached stream ID")
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise ValueError("Invalid cached proposal schema")
        for name, value in record.items():
            if name in _BOOL_FIELDS:
                if type(value) is not bool:
                    raise ValueError("Invalid cached flag")
            elif name == "route_plan":
                if not isinstance(value, list) or len(value) > 32 or any(
                    not isinstance(item, str) or len(item) > 100 for item in value
                ):
                    raise ValueError("Invalid cached route")
            elif not isinstance(value, str) or len(value) > 4_096:
                raise ValueError("Invalid cached text")
        if record["server_id"] != self.server_id or record["stream_id"] != stream_id:
            raise ValueError("Invalid cached identity")
        if record["matcher_action"] not in {"AUTO_EPGSHARE", "AUTO_DUMMY", "REVIEW", "IGNORE"}:
            raise ValueError("Invalid cached action")
        if record["eligible_for_finalization"]:
            allowed = (SAFE_REAL_METHODS if record["matcher_action"] == "AUTO_EPGSHARE"
                       else SAFE_DUMMY_METHODS if record["matcher_action"] == "AUTO_DUMMY" else ())
            if record["match_method"] not in allowed or not record["target_epg_id"] or record["second_epg_id"]:
                raise ValueError("Invalid cached deterministic result")
            if record["matcher_action"] == "AUTO_EPGSHARE":
                if (record["target_source"] != "epgshare01"
                        or record["target_feed"] != "ALL_SOURCES1"
                        or not record["route_explicit"] or len(record["route_plan"]) != 1
                        or record["explicit_market"] in {"", "ALL", "UNKNOWN", "AMBIGUOUS"}):
                    raise ValueError("Invalid cached real target")
            elif (record["target_source"] != "dummy"
                  or record["target_feed"] != "DUMMY_CHANNELS"):
                raise ValueError("Invalid cached dummy target")

    def get(self, stream_id: str, *, channel_name: str, category_name: str,
            identity: MatcherIdentity, catalog: CatalogSnapshot) -> MatchProposal | None:
        record = self.entries.get(stream_id)
        if record is None or not identity.is_expected or not catalog.usable or (
            record["channel_name"] != channel_name or record["category_name"] != category_name
        ):
            self.owner.stats["misses"] += 1
            return None
        if record["eligible_for_finalization"] and not catalog.contains_unambiguous_exact(record["target_epg_id"]):
            self.owner.stats["misses"] += 1
            return None
        if record["eligible_for_finalization"] and catalog.target_kinds(record["target_epg_id"]) != frozenset({
            "real" if record["matcher_action"] == "AUTO_EPGSHARE" else "dummy"
        }):
            self.owner.stats["misses"] += 1
            return None
        current = dict(record)
        current["route_plan"] = tuple(current["route_plan"])
        proposal = MatchProposal(**current, matcher_identity=identity,
                                 catalog_source_sha256=catalog.source_sha256)
        self.owner.stats["hits"] += 1
        return proposal

    def put(self, proposal: MatchProposal) -> None:
        if not self.owner.stats["enabled"] or proposal.decision_reason.startswith("Matcher failed closed"):
            return
        if len(self.entries) >= MAX_ENTRIES and proposal.stream_id not in self.entries:
            return
        record = {name: _plain(getattr(proposal, name)) for name in _FIELDS}
        try:
            self._validate_record(proposal.stream_id, record)
        except (ValueError, TypeError):
            return
        self.entries[proposal.stream_id] = record

    def save(self) -> None:
        if not self.owner.stats["enabled"]:
            return
        temporary = None
        try:
            assert self.path is not None
            _safe_directory(self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise ValueError("Cache file is a symlink")
            payload = {"schemaVersion": SCHEMA_VERSION, "serverId": self.server_id,
                       "dependencySha256": self.dependency, "entries": self.entries}
            signature = hmac.new(self.owner.key, _encoded(payload), hashlib.sha256).hexdigest()
            raw = _encoded({"payload": payload, "signature": signature})
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Cache output exceeds its limit")
            temporary = self.path.with_suffix(".tmp.json")
            if temporary.is_symlink():
                raise ValueError("Cache temporary file is a symlink")
            with temporary.open("wb") as handle:
                handle.write(raw)
            temporary.replace(self.path)
            self.owner.stats["savedEntries"] += len(self.entries)
        except (OSError, ValueError, TypeError, AssertionError):
            self.owner.stats["writeErrors"] += 1
        finally:
            if temporary is not None and not temporary.is_symlink():
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
