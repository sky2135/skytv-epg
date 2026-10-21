#!/usr/bin/env python3
"""Publish sharded movie/TV databases and small Home feeds for SkyTVPlayer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import requests

import build_vod_catalogs as vod
from vod_tmdb import TmdbClient, _date as tmdb_date, _enrichment_identity
from vod_series import enrich_series
from vod_reviewed_corrections import apply_reviewed_correction, correction_for
from vod_resolution_queue import ResolutionQueue
from vod_provider_evidence import MovieEvidenceCollector
from vod_tmdb import MATCH_STRATEGY

SCHEMA_VERSION = 1
MAX_SHARD_RECORDS = 500
MAX_SHARD_BYTES = 512 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
HOME_ROW_LIMIT = 20
NEW_SERIES_WINDOW_DAYS = 30
MAX_INLINE_EPISODE_BYTES = 256 * 1024
SAFE_EXTENSION = re.compile(r"[A-Za-z0-9]{1,8}\Z")
ARTIFACT_NAME = re.compile(r"[a-z0-9_.-]+\.[0-9a-f]{16}\.json(?:\.gz)?\Z")
ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = vod._temporary_path(path.parent, path.name)
    try:
        temporary.write_bytes(json_bytes(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def timestamp(value: Any) -> str | None:
    if value in (None, "", 0, "0") or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if re.fullmatch(r"\d{9,13}", text):
            number = int(text)
            if number > 10**11:
                number /= 1000
            parsed = datetime.fromtimestamp(number, timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        if parsed.year < 1970 or parsed.year > 2100:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value: Any, maximum: float = 1_000_000_000) -> float | None:
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) and 0 <= result <= maximum else None
    except (TypeError, ValueError):
        return None


def load_state(path: Path) -> dict[str, Any]:
    if path.is_file() and path.stat().st_size <= MAX_JSON_BYTES:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("version") == 1 and isinstance(value.get("items"), dict):
                return value
        except (ValueError, AttributeError, OSError):
            pass
    return {"version": 1, "items": {}}


def make_items(server_id: str, kind: str, source: Mapping[str, Any],
               state: dict[str, Any], client: TmdbClient, now: str,
               config: vod.ServerConfig | None = None,
               episodes: Mapping[str, list] | None = None,
               statuses: Mapping[str, str] | None = None,
               evidence: MovieEvidenceCollector | None = None,
               resolution_queue: ResolutionQueue | None = None) -> list[dict[str, Any]]:
    movie = kind == "movie"
    rows_key = "get_vod_streams" if movie else "get_series"
    categories_key = "get_vod_categories" if movie else "get_series_categories"
    details_key = "vod_details" if movie else "series_details"
    host = urlsplit(config.base_url).hostname if config else ""
    normalized, _stats = vod.normalize_catalog(
        source[rows_key], source.get(categories_key, []), source.get(details_key, {}),
        kind=kind, label=f"{server_id} {kind}", blocked_artwork_hostname=host or "")
    raw_rows = {}
    for row in vod.normalize_rows(source[rows_key], rows_key):
        provider_id = vod.normalize_item_id(row.get("stream_id" if movie else "series_id", row.get("id")))
        raw_rows[provider_id] = row
    details = vod.normalize_detail_map(source.get(details_key, {}), kind=kind, label=details_key)
    baseline = not bool(state.get("initialized"))
    result = []
    corrections = {}
    for position, base in enumerate(normalized):
        corrections[base["id"]] = correction_for(server_id, kind, base)
        normalized[position] = apply_reviewed_correction(server_id, kind, base)
        normalized[position]["contentId"] = f"{server_id}:{kind}:{base['id']}"
    priority = getattr(client, "enrichment_priority", lambda _item, _kind: 0)
    normalized.sort(key=lambda item: (priority(item, kind), vod._sort_item_id(item)))
    for base in normalized:
        provider_id = base["id"]
        content_id = f"{server_id}:{kind}:{provider_id}"
        old = state["items"].get(content_id, {})
        row = raw_rows.get(provider_id, {})
        sources = vod._nested_sources(row, details.get(provider_id, {}))
        item = dict(base)
        item["contentId"] = content_id
        item["providerStreamId" if movie else "providerSeriesId"] = provider_id
        item["firstSeenAt"] = old.get("firstSeenAt") or now
        provider_added = timestamp(vod._first_value(sources, ("added", "added_at", "created_at")))
        item["addedAt"] = provider_added or (
            old.get("addedAt") if content_id in state["items"] else (None if baseline else now))
        item["providerLastModified"] = timestamp(row.get("last_modified"))
        extension = vod.clean_text(vod._first_value(sources, ("container_extension",)), 8)
        if movie:
            item["containerExtension"] = extension.lower() if SAFE_EXTENSION.fullmatch(extension) else None
        match_item = item
        if movie and evidence is not None:
            facts = evidence.load(item)
            if facts:
                match_item = dict(item, _identityEvidence=facts)
        metadata = None
        if resolution_queue is not None and resolution_queue.defer_cached_miss(server_id, kind, match_item):
            reader = getattr(client, "_identity_cache", None)
            if callable(reader):
                key, identifier, title, year = _enrichment_identity(match_item, kind)
                cached, _fresh, _rejected = reader(key, match_item, kind, identifier, title, year)
                if cached and cached.get("matchStatus") == "no_match":
                    metadata = dict(cached)
                    client.stats["cacheHits"] = client.stats.get("cacheHits", 0) + 1
                    client.stats["queueDeferred"] = client.stats.get("queueDeferred", 0) + 1
        if metadata is None:
            metadata = client.enrich(match_item, kind)
        correction = corrections.get(provider_id)
        rejected_identity = bool(metadata.get("rejectedIdentity")) or bool(correction and correction.get("rejectIdentity"))
        if rejected_identity:
            # No unrelated provider synopsis, artwork or date may reappear
            # when a disproven TMDB/cache identity cannot be replaced yet.
            item["tmdbId"] = None
            item["releaseDate"] = None
            item["genre"], item["genres"] = "unknown", []
            item["artwork"] = {"posterUrl": None, "backdropUrl": None}
            if item["adult"] is not True:
                item["adult"] = None
        metadata = dict(metadata)
        if correction:
            metadata.update(correction["metadata"])
            item["metadataEvidence"] = {
                "method": "reviewed_correction", "sourceURLs": correction["evidenceURLs"],
            }
        item["metadataMatch"] = metadata.get("matchStatus", "unavailable")
        if correction and not item["metadataMatch"].startswith("matched_"):
            item["metadataMatch"] = "reviewed_correction"
        verified_external = metadata.get("matchStatus", "").startswith("matched_")
        # Unvalidated provider IDs must not join a title to another work's
        # trending/recommendation metadata. Playback continues to use id.
        item["tmdbId"] = metadata.get("tmdbId") if verified_external else (
            correction["metadata"].get("tmdbId") if correction else None)
        for key in ("overview", "originalTitle", "originalLanguage", "rating", "voteCount",
                    "popularity", "runtimeMinutes", "cast", "directors", "trailerUrl",
                    "seasonCount", "episodeCount", "status", "lastAirDate", "nextEpisode"):
            item[key] = metadata.get(key)
        if not rejected_identity:
            item["overview"] = item["overview"] or vod.clean_text(
                vod._first_value(sources, ("plot", "description", "overview")), 4000) or None
            item["rating"] = item["rating"] if item["rating"] is not None else number(
                vod._first_value(sources, ("rating", "vote_average")), 10)
        item["releaseDate"] = metadata.get("releaseDate") or item["releaseDate"]
        if metadata.get("genres"):
            genre, genres = vod.normalize_genres([{"genre": metadata["genres"]}], "")
            if "adult" in item["genres"] and "adult" not in genres:
                genres.append("adult")
            item["genre"], item["genres"] = genre, genres
        item["adult"] = True if item["adult"] is True or metadata.get("adult") is True else (
            item["adult"] if item["adult"] is not None else metadata.get("adult"))
        for name in ("posterUrl", "backdropUrl"):
            item["artwork"][name] = metadata.get(name) or item["artwork"][name]
        next_state = {"firstSeenAt": item["firstSeenAt"], "addedAt": item["addedAt"]}
        if not movie:
            item["episodes"] = list((episodes or {}).get(provider_id, []))
            item["episodesStatus"] = (statuses or {}).get(provider_id, "pending")
            known = old.get("episodeIds", [])
            detected = old.get("episodeDetectedAt")
            current = [episode["id"] for episode in item["episodes"]]
            # An initial inventory establishes a baseline. Only later provider
            # additions prove a change; TMDB air dates never prove availability.
            if item["episodesStatus"] == "available" and old.get("episodesInitialized") and set(current) - set(known):
                detected = now
            dated = [episode.get("addedAt") for episode in item["episodes"] if episode.get("addedAt")]
            item["latestEpisodeAddedAt"] = max([detected] + dated) if detected else max(dated, default=None)
            next_state.update(episodeIds=current if item["episodesStatus"] == "available" else known,
                              episodesInitialized=old.get("episodesInitialized", False) or item["episodesStatus"] == "available",
                              episodeDetectedAt=detected)
        state["items"][content_id] = next_state
        vod.ensure_no_secret_reflection(item, config)
        result.append(item)
    result.sort(key=vod._sort_item_id)
    return result


def backfill_release_dates(sources: Sequence[tuple[str, Mapping[str, Any]]],
                          client: TmdbClient, now: str, *,
                          max_elapsed_seconds: float = 600,
                          queue: ResolutionQueue | None = None,
                          evidence: MovieEvidenceCollector | None = None,
                          report_dir: Path | None = None,
                          max_due: int = 300) -> dict[str, Any]:
    """Warm only verified metadata caches from selected offline movie inputs.

    Publication and normal per-group budgets are deliberately separate. A year
    in a provider title selects work to check; it never supplies a release day.
    """
    seconds = float(max_elapsed_seconds)
    if not math.isfinite(seconds) or not 1 <= seconds <= 900:
        raise vod.CatalogBuildError("Release-date backfill time budget is invalid.")
    year = datetime.fromisoformat(now.replace("Z", "+00:00")).year
    started = time.monotonic()
    getattr(client, "reset_elapsed_budget", lambda _seconds: None)(seconds)
    priority = getattr(client, "enrichment_priority", lambda _item, _kind: 0)
    cache_reader = getattr(client, "_identity_cache", None)
    summaries, candidates = [], []
    active_scope = {}
    selected = [vod.normalize_server_id(server_id) for server_id, _source in sources]
    if not selected or len(selected) != len(set(selected)):
        raise vod.CatalogBuildError("Each backfill server must be selected once.")
    for server_id, source in sources:
        server_id = vod.normalize_server_id(server_id)
        active_scope[server_id] = set()
        normalized, _stats = vod.normalize_catalog(
            source["get_vod_streams"], source.get("get_vod_categories", []), source.get("vod_details", {}),
            kind="movie", label=f"{server_id} release-date backfill", blocked_artwork_hostname="")
        raw_rows = {vod.normalize_item_id(row.get("stream_id", row.get("id"))): row
                    for row in vod.normalize_rows(source["get_vod_streams"], "get_vod_streams")}
        details = vod.normalize_detail_map(source.get("vod_details", {}), kind="movie", label="vod_details")
        summary = dict(serverId=server_id, movieCount=len(normalized), candidateCount=0, processedCount=0,
                       knownCachedDatesBefore=0, matchedDates=0, newlyMatchedDates=0, noMatch=0, unavailable=0)
        summaries.append(summary)
        for base in normalized:
            item = apply_reviewed_correction(server_id, "movie", base)
            item["contentId"] = f"{server_id}:movie:{item['id']}"
            raw = raw_rows.get(item["id"], {})
            item["providerLastModified"] = timestamp(raw.get("last_modified"))
            if evidence is not None:
                facts = evidence.load(item)
                if facts:
                    item["_identityEvidence"] = facts
            if item.get("languages") != ["en"] or tmdb_date(item.get("releaseDate")):
                continue
            key, identifier, title, identity_year = _enrichment_identity(item, "movie")
            if identity_year != str(year):
                continue
            active_scope[server_id].add(item["contentId"])
            known_before = None
            if callable(cache_reader):
                cached, fresh, _rejected = cache_reader(key, item, "movie", identifier, title, identity_year)
                known_before = bool(cached and str(cached.get("matchStatus", "")).startswith("matched_")
                                    and tmdb_date(cached.get("releaseDate")))
                # The normal publisher consumes this fresh proof directly.
                # Do not put thousands of already-resolved titles in the queue.
                if queue is not None and known_before and fresh:
                    summary["knownCachedDatesBefore"] += 1
                    summary["candidateCount"] += 1
                    summary["cachedDatesReused"] = summary.get("cachedDatesReused", 0) + 1
                    # Another publication phase may have resolved an existing
                    # queue row. Reconcile only the identical source/evidence
                    # key; resolved catalogue rows must not create new entries.
                    try:
                        queue_key, _prepared = queue._identity(server_id, "movie", item)
                    except (ValueError, TypeError, KeyError, OverflowError):
                        queue_key = None
                    if queue_key in queue.entries:
                        queue.record_result(server_id, "movie", item, cached,
                            diagnostic={"reason": cached["matchStatus"], "candidateIds": [cached["tmdbId"]]},
                            attempted=False)
                    continue
            provider_sources = vod._nested_sources(raw_rows.get(item["id"], {}), details.get(item["id"], {}))
            added = timestamp(vod._first_value(provider_sources, ("added", "added_at", "created_at")))
            added_epoch = datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp() if added else 0
            candidates.append((priority(item, "movie"), -added_epoch, vod._sort_item_id(item), item, summary, known_before))
            summary["candidateCount"] += 1
            summary["knownCachedDatesBefore"] += known_before is True
    candidates.sort(key=lambda candidate: candidate[:3])
    due_ids = None
    if queue is not None:
        for server_id, content_ids in active_scope.items():
            queue.retain_scope(server_id, "movie", content_ids)
        due = queue.select_due([(entry[4]["serverId"], "movie", entry[3]) for entry in candidates], limit=max_due)
        due_order = {item["contentId"]: position for position, (_server, _kind, item) in enumerate(due)}
        due_ids = set(due_order)
        candidates.sort(key=lambda entry: due_order.get(entry[3]["contentId"], len(due_order)))
    stopped = "complete"
    for _priority, _added, _id, item, summary, known_before in candidates:
        if due_ids is not None and item["contentId"] not in due_ids:
            summary["queueDeferred"] = summary.get("queueDeferred", 0) + 1
            continue
        if time.monotonic() - started >= seconds:
            stopped = "time_budget"
            break
        before = client.stats.get("requests", 0)
        provider_before = evidence.stats["requests"] if evidence is not None else 0
        if before >= client.max_requests:
            stopped = "request_budget"
            break
        metadata = client.enrich(item, "movie")
        diagnostic = dict(getattr(client, "last_match_diagnostic", {}))
        if (evidence is not None and not str(metadata.get("matchStatus", "")).startswith("matched_")
                and diagnostic.get("reason") in {"ambiguous_title_year", "title_mismatch", "year_mismatch", "no_search_results"}
                and time.monotonic() - started < seconds and client.stats.get("requests", 0) < client.max_requests):
            facts = evidence.collect(summary["serverId"], item)
            if facts and facts != item.get("_identityEvidence"):
                item = dict(item, _identityEvidence=facts)
                metadata = client.enrich(item, "movie")
                diagnostic = dict(getattr(client, "last_match_diagnostic", {}))
        if queue is not None:
            queue.record_result(summary["serverId"], "movie", item, metadata,
                diagnostic=diagnostic, attempted=(client.stats.get("requests", 0) > before
                    or evidence is not None and evidence.stats["requests"] > provider_before))
        summary["processedCount"] += 1
        status = str(metadata.get("matchStatus", ""))
        date = tmdb_date(metadata.get("releaseDate"))
        matched = status.startswith("matched_") and date is not None and date[:4] == str(year)
        summary["matchedDates"] += matched
        summary["newlyMatchedDates"] += matched and known_before is False and client.stats.get("requests", 0) > before
        summary["noMatch"] += status == "no_match"
        summary["unavailable"] += status == "unavailable"
    fields = ("candidateCount", "processedCount", "knownCachedDatesBefore", "matchedDates", "newlyMatchedDates")
    stats_fields = ("requests", "cacheHits", "cacheMisses", "staleFallbacks", "matches", "noMatches", "errors",
                    "budgetExhausted", "elapsedBudgetExhausted", "retries", "cacheWriteErrors")
    extra = {}
    if queue is not None:
        queue.save()
        if report_dir is not None:
            extra["resolution"] = queue.write_reports(report_dir)
    if evidence is not None:
        extra["providerEvidence"] = dict(evidence.stats)
    return {"schemaVersion": 1, "mode": "release_date_backfill", "generatedAt": now, "year": year,
            "targetServers": selected, "servers": summaries, **{field: sum(row[field] for row in summaries) for field in fields},
            "maxRequests": client.max_requests, "maxElapsedSeconds": seconds,
            "elapsedSeconds": round(max(0, time.monotonic() - started), 3), "stoppedReason": stopped,
            "stats": {field: client.stats.get(field, 0) for field in stats_fields}, **extra}


def has_series_inventory(item: Mapping[str, Any]) -> bool:
    """Observed provider inventory is evidence of availability, not playback QA."""
    count = item.get("availableEpisodeCount")
    return (item.get("episodesStatus") in {"available", "stale"}
            and type(count) is int and count > 0)


def recent_series_release(item: Mapping[str, Any], now: str) -> bool:
    """A catalog upload/observation never establishes a show's premiere date."""
    value = item.get("releaseDate")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        released = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    return today - timedelta(days=NEW_SERIES_WINDOW_DAYS) <= released <= today


def make_home(server_id: str, movies: list[dict], series: list[dict],
              trends: Mapping[str, Mapping[str, int]], now: str) -> dict[str, Any]:
    recent_cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    def safe(items):
        return [item for item in items if item["adult"] is False
                and ("providerSeriesId" not in item or has_series_inventory(item))]
    def recent(items, key):
        return sorted([item for item in safe(items) if item.get(key) and recent_cutoff <= item[key] <= now],
                      key=lambda item: (item[key], item["contentId"]), reverse=True)
    def trending(items, kind):
        ranks = trends.get(kind, {})
        return sorted([item for item in safe(items) if item.get("tmdbId") in ranks],
                      key=lambda item: (ranks[item["tmdbId"]], item["contentId"]))
    selected: dict[str, dict] = {}
    def row(row_id, title, items=(), source="catalog", **extra):
        items = list(items)[:HOME_ROW_LIMIT]
        for item in items:
            selected[item["contentId"]] = home_preview(item)
        return {"id": row_id, "title": title, "source": source,
                "itemIds": [item["contentId"] for item in items], **extra}
    def by_language(items, prefix):
        languages = sorted({code for item in safe(items) for code in item["languages"] if code not in {"und", "mul"}})
        return {code: row(f"{prefix}_{code}", code,
                          [item for item in items if code in item["languages"]][:12], language=code)
                for code in languages}
    movie_trending = trending(movies, "movie")
    tv_trending = trending(series, "series")
    featured = sorted([item for item in safe(movies) if item["artwork"].get("backdropUrl")],
                      key=lambda item: (-(item.get("popularity") or 0), -(item.get("rating") or 0), item["contentId"]))[:12]
    movie_rows = [row("featured", "Featured", featured),
                  row("continueWatching", "Continue Watching", source="local"),
                  row("trendingInYourLanguages", "Trending in Your Languages", movie_trending,
                      byLanguage=by_language(movie_trending, "trending")),
                  row("newlyAdded", "Newly Added", recent(movies, "addedAt")),
                  row("languages", "Languages", rows=list(by_language(safe(movies), "language").values())),
                  row("browseAll", "Browse All", dataset="vod-movies")]
    series_rows = [row("upNext", "Up Next", source="local"),
                   row("newEpisodes", "New Episodes", recent(series, "latestEpisodeAddedAt")),
                   row("newSeries", "New Series",
                       sorted([item for item in safe(series) if recent_series_release(item, now)],
                              key=lambda item: (item["releaseDate"], item["contentId"]), reverse=True),
                       dateField="releaseDate", windowDays=NEW_SERIES_WINDOW_DAYS,
                       availabilityEvidence="provider_episode_inventory"),
                   row("trendingTv", "Trending TV", tv_trending),
                   row("languages", "Languages", rows=list(by_language(safe(series), "language").values())),
                   row("browseAll", "Browse All", dataset="vod-series")]
    return {"schemaVersion": 1, "serverId": server_id, "generatedAt": now,
            "movieRows": movie_rows, "seriesRows": series_rows, "items": selected,
            "personalization": {"continueWatching": "local_watch_progress", "upNext": "local_watch_progress_and_available_episodes",
                                "languages": "filter_by_local_selected_languages", "hideEmptyRows": True}}


def home_preview(item: Mapping[str, Any]) -> dict:
    keys = ("id", "contentId", "providerStreamId", "providerSeriesId", "title", "category",
            "language", "languages", "genres", "adult", "tmdbId", "artwork", "releaseDate",
            "rating", "containerExtension", "addedAt", "firstSeenAt", "lastAirDate", "status",
            "latestEpisodeAddedAt", "episodesStatus")
    preview = {**{key: item[key] for key in keys if key in item},
               "overview": (item.get("overview") or "")[:500] or None}
    if "providerSeriesId" in item and has_series_inventory(item):
        # Legacy Android readers require the full inventory when the top-level
        # availableEpisodeCount is positive. Home intentionally omits it.
        preview["discoveryAvailability"] = {"evidence": "provider_episode_inventory",
                                             "episodeCount": item["availableEpisodeCount"]}
    return preview


def write_artifact(output: Path, prefix: str, payload: Any, *, compressed=True, count=0) -> dict[str, Any]:
    raw = json_bytes(payload)
    if len(raw) > MAX_JSON_BYTES:
        raise vod.CatalogBuildError("An app artifact exceeds its size limit.")
    data = gzip.compress(raw, mtime=0) if compressed else raw
    return _install_artifact(output, prefix, raw, data, compressed=compressed, count=count)


def _install_artifact(output: Path, prefix: str, raw: bytes, data: bytes, *, compressed=True, count=0) -> dict[str, Any]:
    """Install bytes already encoded and size-checked by the shard writer."""
    if len(raw) > MAX_JSON_BYTES:
        raise vod.CatalogBuildError("An app artifact exceeds its size limit.")
    digest = hashlib.sha256(data).hexdigest()
    name = f"{prefix}.{digest[:16]}.json" + (".gz" if compressed else "")
    path = output / name
    if not path.exists() or path.read_bytes() != data:
        temporary = vod._temporary_path(output, prefix)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"file": name, "sha256": digest, "compressedBytes": len(data),
            "uncompressedBytes": len(raw), "recordCount": count}


def write_dataset(output: Path, server_id: str, kind: str, items: list[dict], now: str) -> dict:
    plural = "movies" if kind == "movie" else "series"
    dataset = f"vod-{plural}"
    items = sorted(items, key=lambda item: (item["category"]["id"] or "", vod._sort_item_id(item)))
    shards = []
    def write_batch(batch, start):
        payload = {"schemaVersion": 1, "serverId": server_id, "dataset": dataset,
                   "generatedAt": now, "recordCount": len(batch), "items": batch}
        raw = json_bytes(payload)
        data = gzip.compress(raw, mtime=0)
        if len(data) > MAX_SHARD_BYTES:
            if len(batch) < 2:
                raise vod.CatalogBuildError("A single title exceeds the app shard size limit.")
            middle = len(batch) // 2
            write_batch(batch[:middle], start)
            write_batch(batch[middle:], start + middle)
        else:
            reference = _install_artifact(output, f"{server_id}_{plural}_{start:06d}", raw, data, count=len(batch))
            reference["providerCategoryIds"] = sorted({item["category"]["id"] for item in batch if item["category"]["id"]})
            reference["languages"] = sorted({language for item in batch for language in item["languages"]})
            shards.append(reference)
    for start in range(0, len(items), MAX_SHARD_RECORDS):
        write_batch(items[start:start + MAX_SHARD_RECORDS], start)
    categories = {item["category"]["id"]: item["category"] for item in items if item["category"]["id"]}
    manifest = {"schemaVersion": 1, "serverId": server_id, "dataset": dataset,
                "generatedAt": now, "totalRecords": len(items),
                "categories": [categories[key] for key in sorted(categories)], "shards": shards}
    return write_artifact(output, f"{server_id}_{plural}_manifest", manifest,
                          compressed=False, count=len(shards))


def write_episode_files(output: Path, server_id: str, series: list[dict], now: str) -> None:
    """Move large episode inventories into separately fetchable bounded shards."""
    for item in series:
        episodes = item["episodes"]
        item["availableEpisodeCount"] = len(episodes)
        item["episodeFiles"] = []
        if not episodes or (len(episodes) <= MAX_SHARD_RECORDS and len(gzip.compress(json_bytes(item), mtime=0)) <= MAX_INLINE_EPISODE_BYTES):
            continue
        series_key = hashlib.sha256(item["id"].encode("utf-8")).hexdigest()[:16]
        def write_batch(batch, start):
            payload = {"schemaVersion": 1, "serverId": server_id, "dataset": "vod-episodes",
                       "providerSeriesId": item["id"], "generatedAt": now,
                       "recordCount": len(batch), "items": batch}
            raw = json_bytes(payload)
            data = gzip.compress(raw, mtime=0)
            if len(data) > MAX_SHARD_BYTES:
                if len(batch) < 2:
                    raise vod.CatalogBuildError("A single episode exceeds the app shard size limit.")
                middle = len(batch) // 2
                write_batch(batch[:middle], start)
                write_batch(batch[middle:], start + middle)
            else:
                reference = _install_artifact(output, f"{server_id}_series_{series_key}_episodes_{start:06d}", raw, data, count=len(batch))
                item["episodeFiles"].append(reference)
        for start in range(0, len(episodes), MAX_SHARD_RECORDS):
            write_batch(episodes[start:start + MAX_SHARD_RECORDS], start)
        # Inventory tracking already ran on the complete list in make_items.
        item["episodes"] = []


def epg_references(epg_dir: Path | None, server_id: str) -> dict:
    result = {}
    if epg_dir:
        for name, suffix in (("programme", "epg"), ("metadata", "metadata"), ("programmeRich", "epg_v2")):
            path = epg_dir / f"{server_id}_{suffix}.json.gz"
            if name == "programmeRich" and not path.is_file():
                continue
            if not path.is_file():
                raise vod.CatalogBuildError(f"{server_id}: app EPG input is missing.")
            result[name] = {"file": f"../EPG/{path.name}", "sha256": vod.sha256_file(path),
                            "compressedBytes": path.stat().st_size}
    return result


def build(output: Path, cache: Path, sources: Sequence[tuple], client: TmdbClient,
          now: str, epg_dir: Path | None = None, fetch_episodes=False,
          max_episode_requests=600, timings: dict | None = None) -> dict:
    build_started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    if len({server_id for server_id, _source, _config in sources}) != len(sources):
        raise vod.CatalogBuildError("Each app server must be selected once.")
    if cache.resolve() == output.resolve() or output.resolve() in cache.resolve().parents:
        raise vod.CatalogBuildError("Private app caches must stay outside the publication directory.")
    reset_time = getattr(client, "reset_elapsed_budget", lambda _seconds: None)
    reset_time(30)
    trends = {kind: client.trending(kind) for kind in ("movie", "series")}
    trend_seconds = time.monotonic() - build_started
    metadata_seconds = 0
    episode_seconds = 0
    servers = []
    states = []
    # Each provider gets an independent session, cache directory and fair quota.
    # Refreshing three independent episode inventories can overlap their I/O;
    # the shared TMDB client and catalogue publication remain sequential.
    episode_elapsed = {}
    def refresh_episodes(entry, quota, deadline=90, previous=None):
        server_id, source, config = entry
        episode_started = time.monotonic()
        session = requests.Session() if fetch_episodes and config else None
        if session:
            session.trust_env = False
        try:
            return enrich_series(source, server_id, cache / "episodes", config,
                                 session, max_requests=quota, now=now, deadline_seconds=deadline, previous=previous)
        finally:
            episode_elapsed[server_id] = episode_elapsed.get(server_id, 0) + time.monotonic() - episode_started
            if session:
                session.close()

    episode_results = None
    if fetch_episodes and len(sources) > 1:
        episode_wall_started = time.monotonic()
        quota, remainder = divmod(max_episode_requests, len(sources))
        with ThreadPoolExecutor(max_workers=min(3, len(sources))) as executor:
            futures = [executor.submit(refresh_episodes, entry, quota + (position < remainder))
                       for position, entry in enumerate(sources)]
            episode_results = [future.result() for future in futures]
        # Keep unused quota useful. A second pass skips this run's freshly
        # checked rows and uses only the provider's remaining active-I/O time.
        unused = max_episode_requests - sum(stats["requests"] for _episodes, stats in episode_results)
        eligible = [position for position, (server_id, _source, config) in enumerate(sources)
                    if config and episode_results[position][1].get("budgetDeferred", 0)
                    and not episode_results[position][1].get("deadlineReached")
                    and episode_elapsed.get(server_id, 90) < 90]
        for index, position in enumerate(eligible):
            if unused <= 0:
                break
            previous = episode_results[position][1]
            quota = unused // (len(eligible) - index)
            if quota <= 0:
                continue
            server_id = sources[position][0]
            episodes, stats = refresh_episodes(sources[position], quota,
                max(0, 90 - episode_elapsed[server_id]), previous=episode_results[position])
            unused -= stats["requests"]
            stats["requests"] += previous["requests"]
            stats["cacheHits"] += previous["cacheHits"]
            episode_results[position] = (episodes, stats)
        episode_seconds = time.monotonic() - episode_wall_started
    remaining = max_episode_requests
    evidence = MovieEvidenceCollector(cache / "provider-evidence",
        configs={server_id: config for server_id, _source, config in sources if config}, max_requests=0, max_seconds=0)
    resolution_queue = ResolutionQueue(cache / "resolution-queue.json", policy_version=MATCH_STRATEGY, now=now)
    tmdb_limit = client.max_requests
    groups_remaining = len(sources) * 2
    for position, (server_id, source, config) in enumerate(sources):
        state_path = cache / f"{server_id}_inventory.json"
        state = load_state(state_path)
        episode_started = time.monotonic()
        episodes, episode_stats = (episode_results[position] if episode_results is not None else
                                  refresh_episodes((server_id, source, config), remaining // (len(sources) - position)))
        if episode_results is None:
            episode_seconds += time.monotonic() - episode_started
        remaining -= episode_stats["requests"]
        catalogs = []
        for kind in ("movie", "series"):
            # Give every server and content type a share of incremental metadata
            # work. Cached titles consume no HTTP budget. Never let server 1's
            # large library prevent later servers from being enriched.
            used = client.stats.get("requests", 0)
            client.max_requests = used + max(0, tmdb_limit - used) // groups_remaining
            reset_time(45)
            metadata_started = time.monotonic()
            catalogs.append(make_items(server_id, kind, source, state, client, now, config,
                                       episodes, episode_stats["statusBySeries"], evidence, resolution_queue))
            metadata_seconds += time.monotonic() - metadata_started
            groups_remaining -= 1
        client.max_requests = tmdb_limit
        movies, series = catalogs
        write_episode_files(output, server_id, series, now)
        live_ids = {item["contentId"] for item in movies + series}
        state["items"] = {key: value for key, value in state["items"].items() if key in live_ids}
        state["initialized"] = True
        home = make_home(server_id, movies, series, trends, now)
        if len(gzip.compress(json_bytes(home), mtime=0)) > MAX_SHARD_BYTES:
            raise vod.CatalogBuildError("App Home feed exceeds its download size limit.")
        entry = {"serverId": server_id, "movieCount": len(movies), "seriesCount": len(series),
                 "movies": write_dataset(output, server_id, "movie", movies, now),
                 "series": write_dataset(output, server_id, "series", series, now),
                 "home": write_artifact(output, f"{server_id}_home", home, count=len(home["items"])),
                 "epg": epg_references(epg_dir, server_id),
                 "episodeRefresh": {
                     **{key: value for key, value in episode_stats.items() if key not in {"statusBySeries", "attemptedSeriesIds"}},
                     "statusCounts": {status: sum(value == status for value in episode_stats["statusBySeries"].values())
                                      for status in ("available", "stale", "pending", "unavailable")}}}
        vod.ensure_no_secret_reflection({"movies": movies, "series": series, "home": home}, config)
        servers.append(entry)
        states.append((state_path, state))
    index = {"schemaVersion": 1, "appName": "SkyTVPlayer", "generatedAt": now,
             "serverCount": len(servers), "servers": servers,
             "metadata": {"provider": "TMDB", "notice": ATTRIBUTION,
                          "url": "https://www.themoviedb.org", "stats": client.stats}}
    # Reject accidental token reflection in any public artifact, including cache
    # contents replayed from a previous run. Do not log the matched value.
    for secret in (os.environ.get("TMDB_READ_ACCESS_TOKEN"), os.environ.get("TMDB_API_KEY")):
        if secret:
            for path in output.glob("*.json*"):
                raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
                if any(token in raw for token in vod._secret_variants(secret)):
                    raise vod.CatalogBuildError("App output reflected a metadata secret.")
    # Validate the candidate index and every shard before switching the release.
    validate(output, epg_dir=epg_dir, expected_servers=[entry["serverId"] for entry in servers], index=index)
    atomic_json(output / "index.json", index)
    for path, state in states:
        atomic_json(path, state)
    if timings is not None:
        total = time.monotonic() - build_started
        timings.update(totalSeconds=round(total, 3), trendingSeconds=round(trend_seconds, 3),
                       episodeSeconds=round(episode_seconds, 3), metadataSeconds=round(metadata_seconds, 3),
                       writeAndValidationSeconds=round(max(0, total - trend_seconds - episode_seconds - metadata_seconds), 3))
    return index


def _github_summary(title, values):
    destination = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not destination:
        return
    try:
        lines = [f"### {title}", "", "| Measure | Value |", "| --- | ---: |"]
        lines.extend(f"| {key} | {value} |" for key, value in values.items()
                     if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_ ]{1,80}", key)
                     and type(value) in (int, float) and math.isfinite(value))
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")
    except OSError:
        print("WARNING: GitHub build summary could not be updated.", file=sys.stderr)


def read_artifact(output: Path, ref: Mapping[str, Any]) -> dict:
    name = ref.get("file", "")
    if not isinstance(name, str) or not ARTIFACT_NAME.fullmatch(name):
        raise vod.CatalogBuildError("App artifact file name is invalid.")
    path = output / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != output.resolve():
        raise vod.CatalogBuildError("App artifact is missing or outside its directory.")
    if path.stat().st_size != ref.get("compressedBytes") or path.stat().st_size > MAX_JSON_BYTES:
        raise vod.CatalogBuildError("App artifact size does not match.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.get("sha256") or name.split(".")[-3 if name.endswith(".gz") else -2] != ref["sha256"][:16]:
        raise vod.CatalogBuildError("App artifact SHA-256 does not match.")
    if name.endswith(".gz"):
        # Use bounded decompression even for corrupt or malicious releases.
        with gzip.open(path, "rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
    else:
        raw = data
    if len(raw) > MAX_JSON_BYTES or len(raw) != ref.get("uncompressedBytes"):
        raise vod.CatalogBuildError("App artifact expanded size does not match.")
    payload = vod._strict_json_from_bytes(raw, "App artifact")
    if not isinstance(payload, dict):
        raise vod.CatalogBuildError("App artifact must be an object.")
    return payload


def validate_episode_inventory(output: Path, server_id: str, item: Mapping[str, Any], generated_at: str) -> None:
    from vod_series import MAX_EPISODES_PER_SERIES, normalize_episodes
    episodes = item.get("episodes")
    files = item.get("episodeFiles")
    count = item.get("availableEpisodeCount")
    if (
        not isinstance(episodes, list) or normalize_episodes({"episodes": episodes}) != episodes
        or not isinstance(files, list) or (episodes and files)
        or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_EPISODES_PER_SERIES
        or len(episodes) > MAX_SHARD_RECORDS
    ):
        raise vod.CatalogBuildError("App episode inventory is invalid.")
    if episodes and len(gzip.compress(json_bytes(item), mtime=0)) > MAX_INLINE_EPISODE_BYTES:
        raise vod.CatalogBuildError("App inline episode inventory exceeds its size limit.")
    total = len(episodes)
    seen_ids: set[str] = set()
    previous_key = None
    for reference in files:
        if not isinstance(reference, dict):
            raise vod.CatalogBuildError("App episode artifact reference is invalid.")
        shard = read_artifact(output, reference)
        rows = shard.get("items")
        if (
            reference["compressedBytes"] > MAX_SHARD_BYTES
            or shard.get("schemaVersion") != 1 or shard.get("serverId") != server_id
            or shard.get("dataset") != "vod-episodes" or shard.get("providerSeriesId") != item["id"]
            or shard.get("generatedAt") != generated_at
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SHARD_RECORDS
            or type(reference.get("recordCount")) is not int or type(shard.get("recordCount")) is not int
            or reference.get("recordCount") != len(rows) or shard.get("recordCount") != len(rows)
            or normalize_episodes({"episodes": rows}) != rows
        ):
            raise vod.CatalogBuildError("App episode shard identity, size, or count is invalid.")
        for episode in rows:
            if episode["id"] in seen_ids:
                raise vod.CatalogBuildError("App episode shards contain duplicate provider IDs.")
            seen_ids.add(episode["id"])
            key = (episode["seasonNumber"], episode["episodeNumber"], episode["id"])
            if previous_key is not None and key <= previous_key:
                raise vod.CatalogBuildError("App episode shards are out of order.")
            previous_key = key
        total += len(rows)
        if total > MAX_EPISODES_PER_SERIES:
            raise vod.CatalogBuildError("App episode inventory exceeds its item limit.")
    if total != count:
        raise vod.CatalogBuildError("App available episode count does not match its inventory.")


def validate_metadata_identity(server_id: str, kind: str, item: Mapping[str, Any]) -> None:
    """Reject known audit regressions before any release pointer is replaced."""
    correction = correction_for(server_id, kind, item)
    reviewed_id = (correction or {}).get("metadata", {}).get("tmdbId")
    matched = str(item.get("metadataMatch", "")).startswith("matched_")
    if item.get("tmdbId") and not matched and item["tmdbId"] != reviewed_id:
        raise vod.CatalogBuildError("App title contains an unverified TMDB identity.")
    if correction is None:
        return
    if item.get("tmdbId") in correction.get("rejectedTmdbIds", []):
        raise vod.CatalogBuildError("App title reintroduced a reviewed wrong-work TMDB identity.")
    if correction.get("title") and item["title"] != correction["title"]:
        raise vod.CatalogBuildError("App title lost its reviewed display-title correction.")
    for field, expected in correction["metadata"].items():
        if field == "genres":
            genre, genres = vod.normalize_genres([{"genre": expected}], "")
            if item["genre"] != genre or set(item["genres"]) - {"adult"} != set(genres) - {"adult"}:
                raise vod.CatalogBuildError("App title lost its reviewed genre correction.")
        elif item.get(field) != expected:
            raise vod.CatalogBuildError("App title lost a reviewed metadata correction.")
    if correction.get("year") and item.get("releaseDate") and item["releaseDate"][:4] != correction["year"]:
        raise vod.CatalogBuildError("App title contradicts its reviewed release year.")
    evidence = item.get("metadataEvidence", {})
    if evidence.get("method") != "reviewed_correction" or evidence.get("sourceURLs") != correction["evidenceURLs"]:
        raise vod.CatalogBuildError("App title lost its reviewed source evidence.")


def validate(output: Path, *, epg_dir=None, expected_servers=vod.DEFAULT_SERVERS, index=None) -> dict:
    if index is None:
        index = vod._read_bounded_json(output / "index.json", 2 * 1024 * 1024, "App index")
    if index.get("schemaVersion") != 1 or index.get("appName") != "SkyTVPlayer":
        raise vod.CatalogBuildError("App index schema is invalid.")
    servers = index.get("servers", [])
    if len(servers) != index.get("serverCount") or sorted(row["serverId"] for row in servers) != sorted(expected_servers):
        raise vod.CatalogBuildError("App index server set is incomplete or duplicated.")
    for server in servers:
        all_items = {}
        for plural in ("movies", "series"):
            manifest = read_artifact(output, server[plural])
            if manifest.get("serverId") != server["serverId"] or manifest.get("dataset") != f"vod-{plural}" or len(manifest["shards"]) != server[plural]["recordCount"]:
                raise vod.CatalogBuildError("App dataset identity does not match.")
            count = 0
            for ref in manifest["shards"]:
                shard = read_artifact(output, ref)
                if ref["compressedBytes"] > MAX_SHARD_BYTES or shard.get("serverId") != server["serverId"] or shard.get("dataset") != f"vod-{plural}":
                    raise vod.CatalogBuildError("App shard identity or size is invalid.")
                items = shard.get("items", [])
                if len(items) > MAX_SHARD_RECORDS or len(items) != ref["recordCount"] or len(items) != shard.get("recordCount"):
                    raise vod.CatalogBuildError("App shard count does not match.")
                if ref.get("providerCategoryIds") != sorted({item["category"]["id"] for item in items if item["category"]["id"]}) or ref.get("languages") != sorted({language for item in items for language in item["languages"]}):
                    raise vod.CatalogBuildError("App shard browse hints do not match its records.")
                for item in items:
                    content_id = item["contentId"]
                    kind = "movie" if plural == "movies" else "series"
                    if content_id in all_items or content_id != f"{server['serverId']}:{kind}:{item['id']}":
                        raise vod.CatalogBuildError("App title identity is invalid or duplicated.")
                    base = {key: item[key] for key in ("id", "title", "category", "language", "languages", "genre", "genres", "releaseDate", "adult", "tmdbId", "artwork")}
                    vod._validate_catalog_item(base, label="App title")
                    validate_metadata_identity(server["serverId"], kind, item)
                    provider_key = "providerStreamId" if kind == "movie" else "providerSeriesId"
                    if item.get(provider_key) != item["id"]:
                        raise vod.CatalogBuildError("App playback identifier does not match its catalog ID.")
                    if kind == "movie":
                        extension = item.get("containerExtension")
                        if extension is not None and (not isinstance(extension, str) or not SAFE_EXTENSION.fullmatch(extension)):
                            raise vod.CatalogBuildError("App movie extension is invalid.")
                    else:
                        if item.get("episodesStatus") not in {"available", "stale", "pending", "unavailable"}:
                            raise vod.CatalogBuildError("App episode status is invalid.")
                        validate_episode_inventory(output, server["serverId"], item, manifest["generatedAt"])
                    all_items[content_id] = item
                count += len(items)
            expected_count = server["movieCount" if plural == "movies" else "seriesCount"]
            if count != manifest["totalRecords"] or count != expected_count:
                raise vod.CatalogBuildError("App dataset total does not match.")
        home = read_artifact(output, server["home"])
        if server["home"]["compressedBytes"] > MAX_SHARD_BYTES or home.get("serverId") != server["serverId"] or len(home["items"]) != server["home"]["recordCount"]:
            raise vod.CatalogBuildError("App Home identity does not match.")
        for content_id, preview in home["items"].items():
            if content_id not in all_items or preview != home_preview(all_items[content_id]):
                raise vod.CatalogBuildError("App Home preview does not match its catalog.")
        def check_rows(rows):
            for row in rows:
                for content_id in row["itemIds"]:
                    if content_id not in home["items"] or home["items"][content_id]["adult"] is not False:
                        raise vod.CatalogBuildError("App Home contains an invalid discovery title.")
                    item = all_items[content_id]
                    if "providerSeriesId" in item and not has_series_inventory(item):
                        raise vod.CatalogBuildError("App Home series lacks provider episode inventory.")
                    if row["id"] == "newSeries" and not recent_series_release(item, home["generatedAt"]):
                        raise vod.CatalogBuildError("App New Series lacks a recent premiere date.")
                check_rows(row.get("rows", []))
                check_rows(row.get("byLanguage", {}).values())
        check_rows(home["movieRows"] + home["seriesRows"])
        if epg_dir is not None and server["epg"] != epg_references(epg_dir, server["serverId"]):
            raise vod.CatalogBuildError("App EPG references do not match this publication.")
    return {"serverCount": len(servers)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("public/skytvplayer"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".build/vod-cache"))
    parser.add_argument("--epg-dir", type=Path)
    parser.add_argument("--server", action="append")
    parser.add_argument("--now-utc", default="")
    parser.add_argument("--fetch-series-details", action="store_true")
    parser.add_argument("--max-series-requests", type=int, default=600)
    parser.add_argument("--max-tmdb-requests", type=int, default=1500)
    parser.add_argument("--backfill-release-dates", action="append", metavar="SERVER",
                        help="Warm current-year English movie metadata caches for selected offline servers; do not publish.")
    parser.add_argument("--backfill-max-seconds", type=float, default=600)
    parser.add_argument("--resolution-report-dir", type=Path)
    parser.add_argument("--resolution-limit", type=int, default=300)
    parser.add_argument("--resolve-provider-details", action="store_true",
                        help="Collect bounded private facts for unresolved selected-server movies only.")
    parser.add_argument("--max-provider-detail-requests", type=int, default=40)
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    # Enrichment is optional. Keep an unresponsive provider from consuming the
    # entire app publication timeout; catalog list ingestion runs separately.
    vod.REQUEST_TIMEOUT = (10, 25)
    selected = [vod.normalize_server_id(value) for value in (args.server or vod.DEFAULT_SERVERS)]
    if len(selected) != len(set(selected)):
        raise vod.CatalogBuildError("Each app server must be selected once.")
    if not math.isfinite(args.backfill_max_seconds) or not 1 <= args.backfill_max_seconds <= 900:
        raise vod.CatalogBuildError("Release-date backfill time budget is invalid.")
    if (not 1 <= args.resolution_limit <= 1000 or not 0 <= args.max_provider_detail_requests <= 200
            or args.resolve_provider_details and not args.backfill_release_dates):
        raise vod.CatalogBuildError("Movie resolution limits or mode are invalid.")
    backfill_servers = [vod.normalize_server_id(value) for value in (args.backfill_release_dates or [])]
    if backfill_servers and (not args.input_dir or args.server or args.fetch_series_details or args.validate_only
                             or len(backfill_servers) != len(set(backfill_servers))):
        raise vod.CatalogBuildError("Release-date backfill requires only offline selected-server inputs.")
    if args.validate_only:
        validate(args.output_dir, epg_dir=args.epg_dir, expected_servers=selected)
        print("SkyTVPlayer publication verified.")
        return 0
    if not 0 <= args.max_series_requests <= 5000 or not 0 <= args.max_tmdb_requests <= 20000:
        raise vod.CatalogBuildError("App enrichment request budgets are invalid.")
    if backfill_servers:
        if args.cache_dir.resolve() == args.output_dir.resolve() or args.output_dir.resolve() in args.cache_dir.resolve().parents:
            raise vod.CatalogBuildError("Private app caches must stay outside the publication directory.")
        offline = [(server_id, vod.load_offline_inputs(args.input_dir, server_id)) for server_id in backfill_servers]
        now = vod.generated_timestamp(args.now_utc)
        report_dir = args.resolution_report_dir or args.cache_dir.parent / "vod-resolution"
        public_root = Path(__file__).resolve().parents[1] / "public"
        if (report_dir.resolve() == args.output_dir.resolve() or args.output_dir.resolve() in report_dir.resolve().parents
                or report_dir.resolve() == public_root.resolve() or public_root.resolve() in report_dir.resolve().parents):
            raise vod.CatalogBuildError("Resolution reports must remain outside the publication directory.")
        configs = {server_id: vod.load_server_config(server_id, allow_insecure_http=args.allow_insecure_http)
                   for server_id in backfill_servers} if args.resolve_provider_details else {}
        secrets = [os.environ.get("TMDB_READ_ACCESS_TOKEN", ""), os.environ.get("TMDB_API_KEY", "")]
        for config in configs.values():
            secrets.extend((config.username, config.password, config.base_url))
        queue = ResolutionQueue(args.cache_dir / "resolution-queue.json", policy_version=MATCH_STRATEGY,
                                now=now, secret_values=secrets)
        evidence = MovieEvidenceCollector(args.cache_dir / "provider-evidence", configs=configs,
            max_requests=args.max_provider_detail_requests if configs else 0,
            max_seconds=min(60, args.backfill_max_seconds), secret_values=secrets)
        client = TmdbClient(args.cache_dir / "tmdb", token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
                            api_key=os.environ.get("TMDB_API_KEY", ""), max_requests=args.max_tmdb_requests,
                            max_elapsed_seconds=args.backfill_max_seconds)
        try:
            report = backfill_release_dates(offline, client, now,
                max_elapsed_seconds=args.backfill_max_seconds, queue=queue, evidence=evidence,
                report_dir=report_dir, max_due=args.resolution_limit)
        finally:
            client.close()
            evidence.close()
        print(json.dumps(report, sort_keys=True))
        _github_summary("Automatic movie metadata resolution", {
            "processed": report["processedCount"], "newlyMatchedDates": report["newlyMatchedDates"],
            "tmdbHttpAttempts": report["stats"]["requests"],
            "providerHttpAttempts": report.get("providerEvidence", {}).get("requests", 0),
            "elapsedSeconds": report["elapsedSeconds"],
            **report.get("resolution", {}).get("reasonCounts", {})})
        return 0
    sources = []
    session = requests.Session()
    session.trust_env = False
    try:
        for server_id in selected:
            config = None
            if args.input_dir:
                source = vod.load_offline_inputs(args.input_dir, server_id)
                # Workflow snapshots still use credentials solely for bounded
                # provider episode inventory refreshes, never for metadata APIs.
                if args.fetch_series_details:
                    config = vod.load_server_config(server_id, allow_insecure_http=args.allow_insecure_http)
            else:
                config = vod.load_server_config(server_id, allow_insecure_http=args.allow_insecure_http)
                source = vod.fetch_live_inputs(session, config, fetch_details=False)
            sources.append((server_id, source, config))
    finally:
        session.close()
    client = TmdbClient(args.cache_dir / "tmdb", token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
                        api_key=os.environ.get("TMDB_API_KEY", ""), max_requests=args.max_tmdb_requests)
    try:
        timings = {}
        index = build(args.output_dir, args.cache_dir, sources, client,
                      vod.generated_timestamp(args.now_utc), args.epg_dir,
                      args.fetch_series_details, args.max_series_requests, timings=timings)
    finally:
        client.close()
    print(f"SkyTVPlayer: published catalogs and Home feeds for {index['serverCount']} servers.")
    print("SkyTVPlayer stage timings: " + json.dumps(timings, sort_keys=True))
    _github_summary("SkyTVPlayer catalogue stage timings", timings)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (vod.CatalogBuildError, OSError, ValueError, KeyError, TypeError):
        # No raw provider or HTTP exception text can reach public build logs.
        print("SkyTVPlayer catalog failed validation; the release was not switched.", file=sys.stderr)
        raise SystemExit(2) from None
