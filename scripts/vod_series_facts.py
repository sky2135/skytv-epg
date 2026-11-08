"""Bounded, rotating fact refresh for already verified series identities only."""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests

from build_vod_catalogs import CatalogBuildError, generated_timestamp
from vod_tmdb import _enrichment_identity, _normalized_title, _title_and_year
import vod_series_language as language
from vod_verification_rate_limit import PacedSession, SharedRateLimiter, VerificationDeferred


MAX_ITEMS = 500_000
MAX_PROGRESS_BYTES = 256 * 1024
MAX_LIMIT = 1000
SECONDS = 180
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATE_KEYS = {"schemaVersion", "cursor", "rateLimiter", "priorityRetryAfter", "lastRunAt"}
_PRIORITY_TITLES = {"dupahiya": "Dupahiya", "stranger things": "Stranger Things", "anupamaa": "Anupamaa"}


def priority_series_title(title: Any) -> str | None:
    """Recognize scheduling editions only; never alter matching/cache identity.

    The same strict terminal language, multi-audio and presentation wrappers
    used by edition classification may surround a canonical example title.
    Ordinary title words, subtitles and numbers remain identity-bearing.
    """
    if not isinstance(title, str) or len(title) > 4096:
        return None
    remaining = unicodedata.normalize("NFKC", title).strip()
    for _ in range(16):
        match = language._BRACKET_END.search(remaining)
        if match:
            label = next(part for part in match.groups() if part is not None)
            if language._label(label) is None and not language._wrappers_only(label):
                break
            remaining = remaining[:match.start()].rstrip(" -|:")
            continue
        match = re.search(r"\s[-|:]\s([^\n]+)$", remaining)
        if match and language._label(match.group(1)) is not None:
            remaining = remaining[:match.start()].rstrip()
            continue
        match = language._BARE_WRAPPER_END.search(remaining)
        if match and language._wrappers_only(match.group(1)):
            remaining = remaining[:match.start()].rstrip()
            continue
        break
    canonical, _year = _title_and_year({"title": remaining})
    return _PRIORITY_TITLES.get(_normalized_title(canonical))


def priority_series_rank(item: Mapping[str, Any]) -> int:
    name = priority_series_title(item.get("canonicalTitle") or item.get("title") or item.get("name"))
    return {"Dupahiya": 0, "Stranger Things": 1, "Anupamaa": 2}.get(name, 3)


def _identity(item: Mapping[str, Any]) -> tuple[str, int]:
    identity, _identifier, title, _year = _enrichment_identity(item, "series")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), priority_series_rank(item)


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Series facts progress cannot contain symbolic links.")
    if not path.exists():
        return {"schemaVersion": 1, "cursor": None, "rateLimiter": None, "priorityRetryAfter": {}, "lastRunAt": None}
    if not path.is_file() or path.stat().st_size > MAX_PROGRESS_BYTES:
        raise ValueError("Invalid series facts progress.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != _STATE_KEYS or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                or (value["cursor"] is not None and not isinstance(value["cursor"], str))
                or (value["cursor"] is not None and not _HASH.fullmatch(value["cursor"]))
                or (value["lastRunAt"] is not None and (not isinstance(value["lastRunAt"], str)
                    or generated_timestamp(value["lastRunAt"]) != value["lastRunAt"]))
                or not isinstance(value["priorityRetryAfter"], dict) or len(value["priorityRetryAfter"]) > MAX_LIMIT):
            raise ValueError
        for key, retry_at in value["priorityRetryAfter"].items():
            if (not _HASH.fullmatch(key) or isinstance(retry_at, bool)
                    or not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at) or retry_at < 0):
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, CatalogBuildError):
        raise ValueError("Invalid series facts progress.") from None


def _save(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Series facts progress cannot contain symbolic links.")
    data = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_PROGRESS_BYTES:
        raise ValueError("Series facts progress exceeds its size limit.")
    descriptor, temporary = tempfile.mkstemp(prefix=".series-facts-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backfill_series_facts(
    items: Iterable[Mapping[str, Any]], client: Any, cache_dir: Path, now: str, *, limit: int = 200,
) -> dict[str, Any]:
    """Warm verified caches without searching, publishing or provider requests.

    Priority cache hits are free against ``limit``. Ordinary identities advance
    a circular hash cursor after every finalized result, including cache misses
    and failures. A deferred request retains that position for the next run.
    The durable shared limiter preserves full server cooldowns across runs.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_LIMIT:
        raise ValueError("Series facts limit must be an integer between zero and 1000.")
    timestamp = generated_timestamp(now)
    unique = {}
    for count, item in enumerate(items, 1):
        if count > MAX_ITEMS:
            raise ValueError("Series facts input exceeds its item limit.")
        if not isinstance(item, Mapping):
            raise ValueError("Series facts input must contain series metadata objects.")
        key, rank = _identity(item)
        unique.setdefault(key, (rank, item))
    path = Path(cache_dir) / "series-facts-progress.json"
    state = _load(path)
    wall = time.time()
    state["lastRunAt"] = timestamp
    state["priorityRetryAfter"] = {key: retry_at for key, retry_at in state["priorityRetryAfter"].items()
                                   if key in unique and retry_at > wall}

    def persist(snapshot):
        state["rateLimiter"] = snapshot
        _save(path, state)

    limiter = SharedRateLimiter(20, deadline=wall + SECONDS, resume_state=state["rateLimiter"], persist=persist)
    before = limiter.snapshot()
    summary = {key: 0 for key in ("attempted", "refreshed", "cacheHits", "unverified", "deferred", "errors", "wireRequests")}
    summary.update(totalIdentities=len(unique), remaining=len(unique), cursor=state["cursor"], authStopped=False)
    priority = sorted((rank, key) for key, (rank, _item) in unique.items() if rank < 3)
    background = sorted(key for key, (rank, _item) in unique.items() if rank == 3)
    position = bisect.bisect_right(background, state["cursor"]) if state["cursor"] is not None else 0
    ordered = [(key, True) for _rank, key in priority if key not in state["priorityRetryAfter"]]
    ordered += [(key, False) for key in background[position:] + background[:position]]
    original_session = client.session
    paced = PacedSession(limiter, session=original_session)
    charged = 0
    free_priority_hits = 0
    processed = set()
    # Initial save also strips any ignored extra limiter state before use.
    persist(limiter.snapshot())
    try:
        client.session = paced
        client.reset_elapsed_budget(SECONDS)
        for key, is_priority in ordered:
            if charged >= limit or limiter.remaining_seconds <= 0:
                break
            request_limit = getattr(client, "max_requests", None)
            used = getattr(client, "stats", {}).get("requests", 0)
            if isinstance(request_limit, int) and isinstance(used, (int, float)) and used >= request_limit:
                summary["deferred"] += 1
                break
            snapshot = limiter.snapshot()
            if snapshot["cooldownUntil"] is not None and snapshot["cooldownUntil"] >= wall + SECONDS:
                summary["deferred"] += 1
                break
            summary["attempted"] += 1
            try:
                metadata = client.refresh_series_facts(unique[key][1])
                status = getattr(client, "last_series_facts_status", "failed")
                if status not in {"available", "cache_hit", "deferred", "failed", "unverified"}:
                    status = "failed"
                if status in {"available", "cache_hit"} and not isinstance(metadata, Mapping):
                    status = "failed"
            except VerificationDeferred:
                status = "deferred"
            except requests.RequestException:
                status = "failed"
            snapshot = limiter.snapshot()
            if limiter.last_deferred is not None:
                status = "deferred"
            if any(snapshot["statusCounts"].get(str(code), 0) > before["statusCounts"].get(str(code), 0) for code in (401, 403)):
                summary["authStopped"] = True
                summary["errors"] += 1
                break
            if status == "deferred":
                summary["deferred"] += 1
                break
            summary[{"available": "refreshed", "cache_hit": "cacheHits", "unverified": "unverified", "failed": "errors"}[status]] += 1
            free_hit = is_priority and status == "cache_hit" and free_priority_hits < MAX_LIMIT
            free_priority_hits += free_hit
            charged += not free_hit
            processed.add(key)
            if is_priority:
                if status in {"failed", "unverified"}:
                    state["priorityRetryAfter"][key] = time.time() + 24 * 3600
                    if len(state["priorityRetryAfter"]) > MAX_LIMIT:
                        # This is only a priority retry hint. The independent
                        # HTTP cooldown is never evicted or shortened.
                        oldest = min(state["priorityRetryAfter"], key=lambda candidate: (state["priorityRetryAfter"][candidate], candidate))
                        state["priorityRetryAfter"].pop(oldest)
                else:
                    state["priorityRetryAfter"].pop(key, None)
            else:
                state["cursor"] = key
            persist(limiter.snapshot())
    finally:
        client.session = original_session
        paced.close()  # The provided session is not owned and remains open.
        persist(limiter.snapshot())
    summary.update(wireRequests=limiter.snapshot()["wireAttempts"] - before["wireAttempts"],
                   remaining=len(unique) - len(processed), cursor=state["cursor"])
    return summary
