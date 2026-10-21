"""Bounded, incremental provider episode metadata for the SKYTVPlayer catalog.

Only allowlisted metadata reaches the cache. Provider credentials and playback
URLs are deliberately absent; the app resolves an episode ID on its own account.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

try:
    from . import build_vod_catalogs as vod
except ImportError:
    import build_vod_catalogs as vod


CACHE_SCHEMA = 1
CACHE_TTL = timedelta(days=1)
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_EPISODES_PER_SERIES = 50_000
EPISODE_KEYS = {
    "id", "seasonNumber", "episodeNumber", "title", "containerExtension",
    "addedAt", "airDate", "overview", "durationSeconds", "stillUrl",
}
CACHE_KEYS = {
    "schemaVersion", "serverId", "seriesId", "sourceFingerprint", "checkedAt",
    "fetchedAt", "status", "episodes",
}


def _integer(value: Any, *, minimum: int = 0, maximum: int = 100_000) -> int | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,12}", text):
        return None
    number = int(text)
    return number if minimum <= number <= maximum else None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{9,13}(?:\.\d{1,6})?", text):
            epoch = float(text)
            if epoch > 100_000_000_000:
                epoch /= 1000
            parsed = datetime.fromtimestamp(epoch, timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            parsed = parsed.astimezone(timezone.utc)
        if not 1970 <= parsed.year <= 2200:
            return None
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _duration(row: Mapping[str, Any], info: Mapping[str, Any]) -> int | None:
    for source in (info, row):
        for key in ("durationSeconds", "duration_secs", "duration_seconds", "runtime_seconds"):
            result = _integer(source.get(key), maximum=7 * 24 * 3600)
            if result is not None:
                return result
        text = str(source.get("duration", "")).strip()
        parts = text.split(":")
        if len(parts) in {2, 3} and all(re.fullmatch(r"\d{1,4}", part) for part in parts):
            values = [int(part) for part in parts]
            if all(number < 60 for number in values[1:]):
                seconds = sum(number * 60**position for position, number in enumerate(reversed(values)))
                if seconds <= 7 * 24 * 3600:
                    return seconds
    return None


def _episode(row: Mapping[str, Any], season_hint: Any, config: vod.ServerConfig | None) -> dict[str, Any] | None:
    episode_id = vod.normalize_item_id(row.get("id", row.get("stream_id", row.get("episode_id"))))
    season = _integer(row.get("seasonNumber", row.get("season", row.get("season_number", season_hint))))
    number = _integer(row.get("episodeNumber", row.get("episode_num", row.get("episode_number", row.get("episode")))))
    if not episode_id or season is None or number is None:
        return None
    info = row.get("info")
    info = info if isinstance(info, Mapping) else {}
    extension = vod.clean_text(row.get("containerExtension", row.get("container_extension")), 20).casefold()
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        extension = None
    title = vod.clean_text(row.get("title", row.get("name")), 300)
    overview = vod.clean_text(info.get("plot", info.get("overview", row.get("overview", row.get("plot")))), 2000)
    # Descriptive text is not a place for provider URLs or playback instructions.
    title = re.sub(r"https?://\S+", "", title, flags=re.I).strip()
    overview = re.sub(r"https?://\S+", "", overview, flags=re.I).strip()
    hostname = urlsplit(config.base_url).hostname or "" if config is not None else ""
    artwork = info.get("movie_image", info.get("still_path", info.get("cover_big", row.get("stillUrl", row.get("stream_icon")))))
    result = {
        "id": episode_id,
        "seasonNumber": season,
        "episodeNumber": number,
        "title": title or f"Episode {number}",
        "containerExtension": extension,
        "addedAt": _timestamp(row.get("addedAt", row.get("added", row.get("added_at")))),
        "airDate": vod.normalize_release_date([{"air_date": row.get("airDate")}, row, info]),
        "overview": overview or None,
        "durationSeconds": _duration(row, info),
        "stillUrl": vod.normalize_artwork_url(artwork, tmdb_size="w500", blocked_hostname=hostname),
    }
    vod.ensure_no_secret_reflection(result, config)
    if hostname and hostname.casefold() in json.dumps(result, ensure_ascii=False).casefold():
        raise vod.CatalogBuildError("Episode metadata contains a private provider hostname.")
    return result


def normalize_episodes(detail: Any, config: vod.ServerConfig | None = None) -> list[dict[str, Any]]:
    """Normalize standard Xtream series details, preserving season/episode zero."""
    if not isinstance(detail, Mapping):
        return []
    payload = detail.get("episodes")
    groups = payload.items() if isinstance(payload, Mapping) else [(None, payload)]
    episodes: dict[str, dict[str, Any]] = {}
    seen_rows = 0
    for season_hint, rows in groups:
        if not isinstance(rows, list):
            continue
        for row in rows:
            seen_rows += 1
            if seen_rows > MAX_EPISODES_PER_SERIES:
                raise vod.CatalogBuildError("Series episode metadata exceeds its item limit.")
            if not isinstance(row, Mapping):
                continue
            item = _episode(row, season_hint, config)
            if item is None:
                continue
            previous = episodes.get(item["id"])
            if previous is not None and previous != item:
                raise vod.CatalogBuildError("Series contains conflicting episode IDs.")
            episodes[item["id"]] = item
    return sorted(episodes.values(), key=lambda row: (row["seasonNumber"], row["episodeNumber"], row["id"]))


def _fingerprint(row: Mapping[str, Any]) -> str:
    value = row.get("last_modified", row.get("lastModified", row.get("modified", "")))
    return hashlib.sha256(vod.clean_text(value, 120).encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, server_id: str, series_id: str) -> Path:
    folder = cache_dir / "series" / server_id
    for path in (cache_dir, cache_dir / "series", folder):
        if path.is_symlink():
            raise vod.CatalogBuildError("The episode cache cannot contain symbolic links.")
    return folder / (hashlib.sha256(series_id.encode("utf-8")).hexdigest() + ".json")


def _load_cache(path: Path, server_id: str, series_id: str, config: vod.ServerConfig | None) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict) or set(value) != CACHE_KEYS
            or value.get("schemaVersion") != CACHE_SCHEMA
            or value.get("serverId") != server_id or value.get("seriesId") != series_id
            or value.get("status") not in {"available", "stale", "unavailable"}
            or not isinstance(value.get("checkedAt"), str)
            or _timestamp(value.get("checkedAt")) != value.get("checkedAt")
            or (value.get("fetchedAt") is not None and (
                not isinstance(value["fetchedAt"], str)
                or _timestamp(value["fetchedAt"]) != value["fetchedAt"]
            ))
            or not isinstance(value.get("sourceFingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sourceFingerprint"])
            or not isinstance(value.get("episodes"), list)
            or any(not isinstance(row, dict) or set(row) != EPISODE_KEYS for row in value["episodes"])
        ):
            return None
        episodes = normalize_episodes({"episodes": value["episodes"]}, config)
        if episodes != value["episodes"]:
            return None
        if value["status"] in {"available", "stale"} and not episodes:
            return None
        return value
    except (OSError, ValueError, TypeError, vod.CatalogBuildError):
        return None


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise vod.CatalogBuildError("The episode cache cannot contain symbolic links.")
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_CACHE_BYTES:
        # The optional cache has a tighter bound than the sharded app export.
        # Preserve this run's normalized inventory even when it cannot be cached.
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".episodes-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enrich_series(
    source: dict[str, Any],
    server_id: str,
    cache_dir: Path,
    config: vod.ServerConfig | None = None,
    session: requests.Session | None = None,
    max_requests: int = 600,
    now: str = "",
    deadline_seconds: float = 90,
    previous: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Return episodes and per-series availability using a bounded request budget.

    Missing inventories precede old inventories. Failed attempts are timestamped,
    so the same failing titles cannot consume every later run's request budget.
    Cached episodes survive transient errors with an explicit ``stale`` status.
    A supplemental pass over the same source can retain its predecessor's
    validated inventories and attempts even when an optional cache was too big.
    """
    server_id = vod.normalize_server_id(server_id)
    if config is not None and vod.normalize_server_id(config.server_id) != server_id:
        raise vod.CatalogBuildError("The episode provider configuration has the wrong server ID.")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
        raise vod.CatalogBuildError("The series request limit must be a non-negative integer.")
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or not math.isfinite(deadline_seconds) or deadline_seconds < 0:
        raise vod.CatalogBuildError("The series time limit must be a finite non-negative number.")
    started = time.monotonic()
    timestamp = vod.generated_timestamp(now)
    current = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    rows = vod.normalize_rows(source.get("get_series", []), f"{server_id} series")
    details = vod.normalize_detail_map(source.get("series_details", {}), kind="series", label=f"{server_id} series details")
    prior_episodes, prior_statuses, prior_attempted = {}, {}, set()
    if (isinstance(previous, tuple) and len(previous) == 2
            and isinstance(previous[0], Mapping) and isinstance(previous[1], Mapping)):
        prior_episodes = previous[0]
        statuses_value = previous[1].get("statusBySeries")
        prior_statuses = statuses_value if isinstance(statuses_value, Mapping) else {}
        attempted_value = previous[1].get("attemptedSeriesIds")
        if isinstance(attempted_value, list):
            # A real first pass cannot attempt more series than its source has.
            # Ignore excess/foreign identifiers rather than expanding work.
            current_ids = {vod.normalize_item_id(row.get("series_id", row.get("id"))) for row in rows}
            prior_attempted = {value for value in attempted_value[:len(rows)]
                               if isinstance(value, str) and value in current_ids}
    result: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, str] = {}
    queue: list[tuple[int, str, str, dict[str, Any] | None, Path, str]] = []
    requests_used = 0
    cache_hits = 0
    deadline_reached = False
    attempted_series_ids = []
    for row in rows:
        series_id = vod.normalize_item_id(row.get("series_id", row.get("id")))
        if not series_id or series_id in statuses:
            continue
        prior_status = prior_statuses.get(series_id)
        if (isinstance(prior_status, str) and prior_status in {"available", "stale", "unavailable"}
                and (prior_status == "available" or series_id in prior_attempted)):
            prior_rows = prior_episodes.get(series_id)
            if isinstance(prior_rows, list) and len(prior_rows) <= MAX_EPISODES_PER_SERIES:
                try:
                    normalized = normalize_episodes({"episodes": prior_rows}, config)
                except (ValueError, TypeError, vod.CatalogBuildError):
                    normalized = None
                if (normalized is not None and normalized == prior_rows
                        and (bool(normalized) if prior_status in {"available", "stale"} else not normalized)):
                    result[series_id] = normalized
                    statuses[series_id] = prior_status
                    continue
        path = _cache_path(Path(cache_dir), server_id, series_id)
        fingerprint = _fingerprint(row)
        cached = _load_cache(path, server_id, series_id, config)
        episodes = list(cached["episodes"]) if cached is not None else []
        result[series_id] = episodes
        if series_id in details:
            normalized = normalize_episodes(details[series_id], config)
            if normalized:
                result[series_id] = normalized
                statuses[series_id] = "available"
                _write_cache(path, {"schemaVersion": CACHE_SCHEMA, "serverId": server_id, "seriesId": series_id, "sourceFingerprint": fingerprint, "checkedAt": timestamp, "fetchedAt": timestamp, "status": "available", "episodes": normalized})
                continue
        checked_at = cached["checkedAt"] if cached is not None else ""
        age = current - datetime.fromisoformat(checked_at.replace("Z", "+00:00")) if checked_at else None
        fresh = cached is not None and cached["sourceFingerprint"] == fingerprint and age is not None and timedelta(0) <= age < CACHE_TTL
        if fresh:
            statuses[series_id] = cached["status"]
            cache_hits += 1
            continue
        statuses[series_id] = "stale" if episodes else "pending"
        # First attempts come before retries; among fetched rows the oldest wins.
        queue.append((0 if cached is None else 1, checked_at, series_id, cached, path, fingerprint))
    if session is not None and config is not None:
        budget = vod.DownloadBudget()
        for _, _, series_id, cached, path, fingerprint in sorted(queue, key=lambda entry: entry[:3]):
            if requests_used >= max_requests:
                break
            if time.monotonic() - started >= deadline_seconds:
                deadline_reached = True
                break
            requests_used += 1
            attempted_series_ids.append(series_id)
            episodes: list[dict[str, Any]] = []
            try:
                detail = vod.request_panel_json(session, config, "get_series_info", item_id=series_id, maximum_bytes=vod.MAX_DETAIL_RESPONSE_BYTES, budget=budget)
                episodes = normalize_episodes(detail, config)
            except (vod.CatalogBuildError, requests.RequestException):
                pass
            if episodes:
                result[series_id] = episodes
                statuses[series_id] = "available"
                fetched_at = timestamp
            else:
                statuses[series_id] = "stale" if result[series_id] else "unavailable"
                fetched_at = cached.get("fetchedAt") if cached is not None else None
            _write_cache(path, {"schemaVersion": CACHE_SCHEMA, "serverId": server_id, "seriesId": series_id, "sourceFingerprint": fingerprint, "checkedAt": timestamp, "fetchedAt": fetched_at, "status": statuses[series_id], "episodes": result[series_id]})
    return result, {"requests": requests_used, "cacheHits": cache_hits, "statusBySeries": statuses,
                    "budgetDeferred": len(queue) - requests_used, "deadlineReached": deadline_reached,
                    "attemptedSeriesIds": attempted_series_ids}
