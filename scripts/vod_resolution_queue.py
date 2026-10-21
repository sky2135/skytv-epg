"""Persistent, reason-based scheduling and public VOD resolution reports.

Only corrected display identities, bounded TMDB IDs, hashes and retry state are
retained. This module never contacts a provider or changes published metadata.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence
from urllib.parse import urlsplit

from build_vod_catalogs import _secret_variants
from vod_tmdb import _enrichment_identity

SCHEMA_VERSION = 1
MAX_ENTRIES = 10_000
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_CANDIDATES = 100
DAY = 86_400
MATCHED = {"matched_id", "matched_search", "matched_alias"}
STABLE = {"ambiguous_title_year", "no_search_results", "title_mismatch", "year_mismatch",
          "incomplete_search", "alias_candidate_limit"}
REASONS = MATCHED | STABLE | {"pending", "unavailable", "missing_identity"}
ENTRY_KEYS = {"contentId", "serverId", "kind", "providerId", "title", "queryTitle", "queryYear",
              "sourceFingerprint", "attempts", "failureStreak", "reason", "candidateIds",
              "firstSeenAt", "lastSeenAt", "lastAttemptAt", "nextAttemptAt", "selectionOrder"}


def _epoch(value: Any = None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Queue timestamps require an explicit timezone.")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 4_102_444_800:
        raise ValueError("Queue timestamp is invalid.")
    return int(value)


def _iso(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _number(value: Any, maximum: int = 10**12) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _group(reason: str) -> str:
    return "matched" if reason in MATCHED else "temporary" if reason == "unavailable" else "missing" if reason == "missing_identity" else "stable"


def _delay(reason: str, failures: int) -> int:
    if reason in MATCHED:
        return 14 * DAY
    if reason == "missing_identity":
        return 30 * DAY
    multiplier = 2 ** min(max(0, failures - 1), 3)
    return min(7, multiplier) * DAY if reason == "unavailable" else min(28, 7 * multiplier) * DAY


class ResolutionQueue:
    """A bounded queue whose selection order rotates independently of attempts."""

    def __init__(self, cache_path: Path, *, policy_version: int, now: Any = None,
                 secret_values: Sequence[str] = ()):
        if type(policy_version) is not int or not 1 <= policy_version <= 1_000_000:
            raise ValueError("Queue policy version is invalid.")
        self.cache_path = Path(cache_path)
        self.policy_version = policy_version
        self.now = _epoch(now)
        self._secrets = tuple(value for value in secret_values if isinstance(value, str) and value)
        protected = set(self._secrets)
        for value in self._secrets:
            if "://" in value:
                try:
                    hostname = urlsplit(value).hostname
                    if hostname:
                        protected.add(hostname)
                except ValueError:
                    pass
        self._secret_tokens = {token for value in protected for token in _secret_variants(value)}
        self._secret_tokens.update(html.escape(value, quote=quote).encode("utf-8")
                                   for value in protected for quote in (True, False))
        self.entries: dict[str, dict[str, Any]] = {}
        self._by_content: dict[str, str] = {}
        self._selection_counter = 0
        self.stats = {"loadErrors": 0, "discardedEntries": 0, "invalidInputs": 0,
                      "fingerprintResets": 0, "evictedEntries": 0, "writeErrors": 0}
        self._load()

    def _text(self, value: Any, limit: int = 300) -> str | None:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return None
        text = " ".join(value.split())
        if "://" in text or any(ord(char) < 32 for char in value) or self._reflects_secret(text):
            return None
        return text

    def _reflects_secret(self, value: str) -> bool:
        raw = value.encode("utf-8")
        return any(token in raw for token in self._secret_tokens)

    @staticmethod
    def _assert_safe_path(path: Path) -> None:
        # Check lexical ancestors before resolving: resolve() would hide a link.
        absolute = path.absolute()
        for ancestor in (absolute, *absolute.parents):
            if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
                raise ValueError("Queue paths cannot use symbolic links or junctions.")

    def _key(self, content_id: str, fingerprint: str) -> str:
        return hashlib.sha256(f"{self.policy_version}:{content_id}:{fingerprint}".encode()).hexdigest()

    def _identity(self, server_id: str, kind: str, item: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(item, Mapping) or not isinstance(server_id, str) or not re.fullmatch(r"server_[1-9][0-9]?", server_id) or kind not in {"movie", "series"}:
            raise ValueError("Queue identity is invalid.")
        provider_id = str(item.get("id") or "")
        if not re.fullmatch(r"[0-9]{1,10}", provider_id):
            raise ValueError("Queue provider ID is invalid.")
        content_id = f"{server_id}:{kind}:{provider_id}"
        if item.get("contentId") not in (None, content_id):
            raise ValueError("Queue content ID is inconsistent.")
        title = self._text(item.get("title"))
        if title is None:
            raise ValueError("Queue display title is invalid.")
        prepared = dict(item, contentId=content_id)
        matcher_key, _identifier, query_title, year = _enrichment_identity(prepared, kind)
        query_title = self._text(query_title)
        if query_title is None:
            raise ValueError("Queue query title is invalid.")
        identity_evidence = item.get("_identityEvidence")
        evidence = {"identityEvidenceFingerprint": identity_evidence.get("fingerprint")
                    if isinstance(identity_evidence, Mapping) else None,
                    "providerEvidenceFingerprint": item.get("providerEvidenceFingerprint")}
        encoded_evidence = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded_evidence.encode()) > 64 * 1024:
            raise ValueError("Queue evidence exceeds its bound.")
        source = {"displayTitle": title, "matcherIdentity": matcher_key,
                  "languages": item.get("languages"), "releaseDate": item.get("releaseDate"),
                  "providerEvidenceHash": hashlib.sha256(encoded_evidence.encode()).hexdigest()}
        encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        row = dict(contentId=content_id, serverId=server_id, kind=kind, providerId=provider_id,
                   title=title, queryTitle=query_title, queryYear=year,
                   sourceFingerprint=fingerprint, attempts=0, failureStreak=0, reason="pending",
                   candidateIds=[], firstSeenAt=self.now, lastSeenAt=self.now, lastAttemptAt=None,
                   nextAttemptAt=self.now, selectionOrder=0)
        return self._key(content_id, fingerprint), row

    def _restored(self, key: str, row: Any) -> bool:
        if (not isinstance(row, dict) or set(row) != ENTRY_KEYS
                or not isinstance(row.get("reason"), str) or row["reason"] not in REASONS):
            return False
        if (not isinstance(row.get("serverId"), str) or not re.fullmatch(r"server_[1-9][0-9]?", row["serverId"])
                or row.get("kind") not in {"movie", "series"} or not isinstance(row.get("providerId"), str)
                or not re.fullmatch(r"[0-9]{1,10}", row["providerId"])
                or row.get("contentId") != f"{row['serverId']}:{row['kind']}:{row['providerId']}"
                or not isinstance(row.get("title"), str) or self._text(row["title"]) != row["title"]
                or not isinstance(row.get("queryTitle"), str) or self._text(row["queryTitle"]) != row["queryTitle"]
                or row.get("queryYear") is not None and (not isinstance(row["queryYear"], str) or not re.fullmatch(r"(?:19|20)[0-9]{2}", row["queryYear"]))
                or not isinstance(row.get("sourceFingerprint"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["sourceFingerprint"])
                or key != self._key(row["contentId"], row["sourceFingerprint"])):
            return False
        if (any(not _number(row.get(field), 1_000_000) for field in ("attempts", "failureStreak"))
                or any(not _number(row.get(field), 4_102_444_800) for field in ("firstSeenAt", "lastSeenAt", "nextAttemptAt"))
                or not _number(row.get("selectionOrder"))):
            return False
        last_attempt = row.get("lastAttemptAt")
        if last_attempt is not None and not _number(last_attempt, 4_102_444_800):
            return False
        if row["lastSeenAt"] > self.now + DAY or row["nextAttemptAt"] > max(last_attempt or row["firstSeenAt"], row["lastSeenAt"]) + 30 * DAY:
            return False
        ids = row.get("candidateIds")
        return (isinstance(ids, list) and len(ids) <= MAX_CANDIDATES
                and all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,8}", value) for value in ids)
                and len(ids) == len(set(ids)))

    def _load(self) -> None:
        try:
            if not self.cache_path.exists():
                return
            self._assert_safe_path(self.cache_path)
            if self.cache_path.is_symlink() or self.cache_path.stat().st_size > MAX_CACHE_BYTES:
                raise ValueError("Queue cache exceeds its bound.")
            document = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if (not isinstance(document, dict) or set(document) != {"schemaVersion", "policyVersion", "selectionCounter", "entries"}
                    or type(document["schemaVersion"]) is not int or document["schemaVersion"] != SCHEMA_VERSION or type(document["policyVersion"]) is not int
                    or not _number(document["selectionCounter"])
                    or not isinstance(document["entries"], dict) or len(document["entries"]) > MAX_ENTRIES):
                raise ValueError("Queue cache schema is invalid.")
            if document["policyVersion"] != self.policy_version:
                self.stats["discardedEntries"] += len(document["entries"])
                return
            for key, row in document["entries"].items():
                if not self._restored(key, row) or row["contentId"] in self._by_content:
                    self.stats["discardedEntries"] += 1
                    continue
                self.entries[key] = row
                self._by_content[row["contentId"]] = key
            self._selection_counter = max(document["selectionCounter"], max((row["selectionOrder"] for row in self.entries.values()), default=0))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.entries.clear()
            self._by_content.clear()
            self.stats["loadErrors"] += 1

    def _register(self, server_id: str, kind: str, item: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        key, prepared = self._identity(server_id, kind, item)
        previous_key = self._by_content.get(prepared["contentId"])
        if previous_key is not None and previous_key != key:
            del self.entries[previous_key]
            self.stats["fingerprintResets"] += 1
        if key not in self.entries:
            if len(self.entries) >= MAX_ENTRIES:
                oldest = min(self.entries, key=lambda old: (self.entries[old]["lastSeenAt"], old))
                self._by_content.pop(self.entries[oldest]["contentId"], None)
                del self.entries[oldest]
                self.stats["evictedEntries"] += 1
            self.entries[key] = prepared
            self._by_content[prepared["contentId"]] = key
        self.entries[key]["lastSeenAt"] = self.now
        return key, self.entries[key]

    def select_due(self, items: Sequence[tuple[str, str, Mapping[str, Any]]], *, limit: int = 300) -> list[tuple]:
        if type(limit) is not int or not 0 <= limit <= MAX_ENTRIES:
            raise ValueError("Queue selection limit is invalid.")
        current = {}
        for server_id, kind, item in items:
            try:
                key, row = self._register(server_id, kind, item)
            except (ValueError, TypeError, KeyError, OverflowError):
                self.stats["invalidInputs"] += 1
                continue
            current[key] = (server_id, kind, item)
        due = [key for key in current if key in self.entries and self.entries[key]["nextAttemptAt"] <= self.now]
        input_order = {key: index for index, key in enumerate(current)}
        due.sort(key=lambda key: (self.entries[key]["selectionOrder"], self.entries[key]["nextAttemptAt"],
                                  input_order[key], self.entries[key]["contentId"]))
        selected = due[:limit]
        for key in selected:
            self._selection_counter += 1
            self.entries[key]["selectionOrder"] = self._selection_counter
        return [current[key] for key in selected]

    def retain_scope(self, server_id: str, kind: str, content_ids: Collection[str]) -> int:
        """Prune only this scope using the caller's complete eligible ID set."""
        if (not isinstance(server_id, str) or not re.fullmatch(r"server_[1-9][0-9]?", server_id)
                or kind not in {"movie", "series"} or isinstance(content_ids, (str, bytes))):
            raise ValueError("Queue retention scope is invalid.")
        try:
            keep = set(content_ids)
        except (TypeError, ValueError):
            raise ValueError("Queue retained IDs are invalid.") from None
        pattern = re.compile(rf"{re.escape(server_id)}:{kind}:[0-9]{{1,10}}")
        if any(not isinstance(content_id, str) or not pattern.fullmatch(content_id) for content_id in keep):
            raise ValueError("Queue retained IDs are inconsistent with their scope.")
        removed = [key for key, row in self.entries.items()
                   if row["serverId"] == server_id and row["kind"] == kind and row["contentId"] not in keep]
        for key in removed:
            self._by_content.pop(self.entries[key]["contentId"], None)
            del self.entries[key]
        return len(removed)

    def defer_cached_miss(self, server_id: str, kind: str, item: Mapping[str, Any]) -> bool:
        """Whether an unchanged unresolved identity is still inside its backoff.

        This is a read-only scheduling hint. The caller must separately validate
        that a negative matcher cache exists; absent caches and positive results
        must continue through the normal matcher.
        """
        if not isinstance(item, Mapping):
            return False
        content_id = item.get("contentId")
        if content_id is None:
            content_id = f"{server_id}:{kind}:{item.get('id') or ''}"
        if not isinstance(content_id, str) or content_id not in self._by_content:
            return False
        try:
            key, _prepared = self._identity(server_id, kind, item)
        except (ValueError, TypeError, KeyError, OverflowError):
            return False
        row = self.entries.get(key)
        return bool(row and row["reason"] in STABLE | {"missing_identity", "unavailable"}
                    and row["nextAttemptAt"] > self.now)

    def record_result(self, server_id: str, kind: str, item: Mapping[str, Any], metadata: Mapping[str, Any],
                      diagnostic: Mapping[str, Any] | None = None, *, attempted: bool = True) -> bool:
        _key, row = self._register(server_id, kind, item)
        diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
        reason = diagnostic.get("reason")
        if not isinstance(reason, str) or reason not in REASONS or reason == "pending":
            status = metadata.get("matchStatus")
            reason = status if status in MATCHED else "unavailable" if status == "unavailable" else "missing_identity"
        ids = diagnostic.get("candidateIds", [])
        ids = ids if isinstance(ids, list) else []
        candidates = []
        for value in ids[:MAX_CANDIDATES]:
            value = str(value) if type(value) is int else value
            if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,8}", value) and value not in candidates:
                candidates.append(value)
        row["candidateIds"] = candidates
        previous = row["reason"]
        row["reason"] = reason
        if not attempted:
            if reason in MATCHED:
                row["failureStreak"] = 0
                row["nextAttemptAt"] = self.now + 14 * DAY
            return False
        row["attempts"] += 1
        row["failureStreak"] = 0 if reason in MATCHED else row["failureStreak"] + 1 if _group(previous) == _group(reason) else 1
        row["lastAttemptAt"] = self.now
        row["nextAttemptAt"] = self.now + _delay(reason, row["failureStreak"])
        return True

    def _atomic(self, path: Path, content: str) -> None:
        self._assert_safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False, newline="") as handle:
                temporary = Path(handle.name)
                handle.write(content)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self) -> bool:
        document = {"schemaVersion": SCHEMA_VERSION, "policyVersion": self.policy_version,
                    "selectionCounter": self._selection_counter, "entries": self.entries}
        try:
            serialized = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(serialized.encode()) > MAX_CACHE_BYTES or self._reflects_secret(serialized):
                raise ValueError("Queue cache failed safety validation.")
            self._atomic(self.cache_path, serialized)
            return True
        except (OSError, ValueError):
            self.stats["writeErrors"] += 1
            return False

    def write_reports(self, directory: Path) -> dict[str, Any]:
        directory = Path(directory)
        self._assert_safe_path(directory / "resolution_queue.json")
        self._assert_safe_path(directory / "resolution_queue.csv")
        rows = []
        for row in sorted(self.entries.values(), key=lambda value: value["contentId"]):
            rows.append({key: row[key] for key in
                         ("contentId", "serverId", "kind", "providerId", "title", "queryTitle", "queryYear",
                          "attempts", "reason", "candidateIds")} |
                        {"lastAttemptAt": _iso(row["lastAttemptAt"]), "nextAttemptAt": _iso(row["nextAttemptAt"]),
                         "sourceURLs": [f"https://www.themoviedb.org/{'movie' if row['kind'] == 'movie' else 'tv'}/{value}" for value in row["candidateIds"]]})
        report = {"schemaVersion": SCHEMA_VERSION, "policyVersion": self.policy_version, "generatedAt": _iso(self.now),
                  "recordCount": len(rows), "reasonCounts": dict(Counter(row["reason"] for row in rows)),
                  "interpretation": "Current resolution state. Candidate IDs do not establish a match; uncertain metadata remains unchanged.",
                  "records": rows}
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        columns = ("contentId", "title", "queryTitle", "queryYear", "reason", "attempts", "lastAttemptAt", "nextAttemptAt", "candidateIds", "sourceURLs")
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            values = {}
            for key in columns:
                value = row[key]
                if isinstance(value, list):
                    value = " | ".join(value)
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                values[key] = value
            writer.writerow(values)
        csv_text = buffer.getvalue()
        if self._reflects_secret(serialized) or self._reflects_secret(csv_text):
            raise ValueError("Queue report failed safety validation.")
        self._atomic(directory / "resolution_queue.json", serialized)
        self._atomic(directory / "resolution_queue.csv", csv_text)
        return {"recordCount": len(rows), "reasonCounts": report["reasonCounts"],
                "jsonFile": "resolution_queue.json", "csvFile": "resolution_queue.csv"}

