"""Bounded, optional TMDB enrichment for the SkyTVPlayer VOD export.

Only sanitized metadata is cached. Provider credentials, playback URLs, request
URLs, API keys and bearer tokens are never written to disk or included in errors.
``originalLanguage`` describes the work, never a provider's available audio.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
import unicodedata
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

import requests


API_ROOT = "https://api.themoviedb.org/3"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024
DETAIL_TTL = 14 * 24 * 60 * 60
MISS_TTL = TRENDING_TTL = 24 * 60 * 60
MAX_TRENDING_AGE = 7 * 24 * 60 * 60
MAX_RETRY_DELAY = 5.0
MAX_ATTEMPTS = 3
IMAGE_PATH = re.compile(r"/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)\Z", re.I)
YOUTUBE_KEY = re.compile(r"[A-Za-z0-9_-]{11}\Z")
STATUSES = {"matched_id", "matched_search", "no_match", "unavailable"}


def _text(value: object, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    value = " ".join("".join(c if ord(c) >= 32 else " " for c in value).split())
    return value[:limit] or None


def _integer(value: object, minimum: int = 0, maximum: int = 1_000_000_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,10}", value):
        return None
    result = int(value)
    return result if minimum <= result <= maximum else None


def _identifier(value: object) -> str | None:
    result = _integer(value, 1)
    return str(result) if result is not None else None


def _number(value: object, maximum: float = 1_000_000_000) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= maximum else None


def _date(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _normalized_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", _text(value) or "").casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c)).split())


def _title_and_year(item: Mapping[str, Any]) -> tuple[str, str | None]:
    title = _text(item.get("title") or item.get("name")) or ""
    year = None
    for value in (item.get("year"), item.get("releaseDate"), item.get("release_date"), item.get("first_air_date")):
        match = re.match(r"^((?:19|20)\d{2})(?:$|-\d{2})", str(value or ""))
        if match:
            year = match.group(1)
            break
    trailing_year = re.search(r"\s*[\[(]((?:19|20)\d{2})[\])]\s*$", title)
    if trailing_year and (year is None or trailing_year.group(1) == year):
        year = trailing_year.group(1)
        title = title[:trailing_year.start()].strip()
    return title, year


def _image(path: object, size: str) -> str | None:
    return f"https://image.tmdb.org/t/p/{size}{path}" if isinstance(path, str) and IMAGE_PATH.fullmatch(path) else None


def _enrichment_identity(item: Mapping[str, Any], kind: str) -> tuple[str, str | None, str, str | None]:
    identifier = _identifier(item.get("tmdbId") or item.get("tmdb_id") or item.get("tmdb"))
    title, year = _title_and_year(item)
    key = f"{kind}:id:{identifier}" if identifier else f"{kind}:title:{_normalized_title(title)}:year:{year or ''}"
    return key, identifier, title, year


def _strings(values: object, limit: int = 40) -> list[str]:
    result: list[str] = []
    for value in values[:limit] if isinstance(values, list) else []:
        cleaned = _text(value, 100)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _empty(identifier: str | None, status: str = "unavailable") -> dict[str, Any]:
    return {
        "tmdbId": identifier, "matchStatus": status, "overview": None,
        "originalTitle": None, "originalLanguage": None, "releaseDate": None,
        "genres": [], "posterUrl": None, "backdropUrl": None, "rating": None,
        "voteCount": None, "popularity": None, "runtimeMinutes": None,
        "cast": [], "directors": [], "trailerUrl": None, "seasonCount": None,
        "episodeCount": None, "status": None, "lastAirDate": None, "nextEpisode": None,
        "adult": None,
    }


def _metadata(details: Mapping[str, Any], kind: str, match_status: str) -> dict[str, Any]:
    result = _empty(_identifier(details.get("id")), match_status)
    result.update({
        "overview": _text(details.get("overview"), 6000),
        "originalTitle": _text(details.get("original_title" if kind == "movie" else "original_name")),
        "releaseDate": _date(details.get("release_date" if kind == "movie" else "first_air_date")),
        "posterUrl": _image(details.get("poster_path"), "w500"),
        "backdropUrl": _image(details.get("backdrop_path"), "w1280"),
        "rating": _number(details.get("vote_average"), 10),
        "voteCount": _integer(details.get("vote_count")),
        "popularity": _number(details.get("popularity")),
        "status": _text(details.get("status"), 100),
        "adult": details.get("adult") if isinstance(details.get("adult"), bool) else None,
    })
    language = details.get("original_language")
    if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language):
        result["originalLanguage"] = language
    genres = details.get("genres")
    result["genres"] = _strings([genre.get("name") for genre in genres[:40] if isinstance(genre, dict)] if isinstance(genres, list) else [])
    if kind == "movie":
        result["runtimeMinutes"] = _integer(details.get("runtime"), 1, 10_000)
    else:
        runtimes = details.get("episode_run_time")
        if isinstance(runtimes, list):
            result["runtimeMinutes"] = next((value for raw in runtimes[:100] if (value := _integer(raw, 1, 10_000)) is not None), None)
        result["seasonCount"] = _integer(details.get("number_of_seasons"), 0, 100_000)
        result["episodeCount"] = _integer(details.get("number_of_episodes"), 0, 1_000_000)
        result["lastAirDate"] = _date(details.get("last_air_date"))
        episode = details.get("next_episode_to_air")
        if isinstance(episode, dict):
            season = _integer(episode.get("season_number"), 0, 100_000)
            number = _integer(episode.get("episode_number"), 1, 100_000)
            air_date = _date(episode.get("air_date"))
            if season is not None and number is not None and air_date:
                result["nextEpisode"] = {"season": season, "episode": number, "airDate": air_date}
    credits = details.get("credits")
    if isinstance(credits, dict):
        cast = credits.get("cast")
        for person in cast[:20] if isinstance(cast, list) else []:
            if not isinstance(person, dict):
                continue
            identifier, name = _integer(person.get("id"), 1), _text(person.get("name"))
            if identifier and name:
                result["cast"].append({"id": identifier, "name": name, "character": _text(person.get("character"))})
                if len(result["cast"]) == 10:
                    break
        crew = credits.get("crew")
        result["directors"] = _strings([person.get("name") for person in crew[:1000] if isinstance(person, dict) and person.get("job") == "Director"] if isinstance(crew, list) else [], 10)
    videos = details.get("videos")
    videos = videos.get("results") if isinstance(videos, dict) else None
    trailers = [video for video in videos[:100] if isinstance(video, dict) and video.get("site") == "YouTube" and video.get("type") == "Trailer" and isinstance(video.get("key"), str) and YOUTUBE_KEY.fullmatch(video["key"])] if isinstance(videos, list) else []
    trailers.sort(key=lambda video: (video.get("official") is not True, video.get("iso_639_1") != "en"))
    if trailers:
        result["trailerUrl"] = "https://www.youtube.com/watch?v=" + trailers[0]["key"]
    return result


def _cached_metadata(data: object) -> dict[str, Any] | None:
    """Revalidate restored cache files before they enter a public catalog."""
    if not isinstance(data, dict) or set(data) != set(_empty(None)) or data.get("matchStatus") not in STATUSES:
        return None
    identifier = _identifier(data.get("tmdbId"))
    if data["matchStatus"].startswith("matched_") and identifier is None:
        return None
    result = _empty(identifier, data["matchStatus"])
    for field, limit in (("overview", 6000), ("originalTitle", 300), ("status", 100)):
        result[field] = _text(data.get(field), limit)
    for field in ("releaseDate", "lastAirDate"):
        result[field] = _date(data.get(field))
    language = data.get("originalLanguage")
    if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language):
        result["originalLanguage"] = language
    for field in ("genres", "directors"):
        result[field] = _strings(data.get(field), 10 if field == "directors" else 40)
    for field, size in (("posterUrl", "w500"), ("backdropUrl", "w1280")):
        url = data.get(field)
        prefix = f"https://image.tmdb.org/t/p/{size}"
        if isinstance(url, str) and url.startswith(prefix):
            result[field] = _image(url[len(prefix):], size)
    trailer = data.get("trailerUrl")
    prefix = "https://www.youtube.com/watch?v="
    if isinstance(trailer, str) and trailer.startswith(prefix) and YOUTUBE_KEY.fullmatch(trailer[len(prefix):]):
        result["trailerUrl"] = trailer
    result["rating"] = _number(data.get("rating"), 10)
    result["popularity"] = _number(data.get("popularity"))
    result["voteCount"] = _integer(data.get("voteCount"))
    result["runtimeMinutes"] = _integer(data.get("runtimeMinutes"), 1, 10_000)
    result["seasonCount"] = _integer(data.get("seasonCount"), 0, 100_000)
    result["episodeCount"] = _integer(data.get("episodeCount"), 0, 1_000_000)
    result["adult"] = data.get("adult") if isinstance(data.get("adult"), bool) else None
    cast = data.get("cast")
    for person in cast[:10] if isinstance(cast, list) else []:
        if not isinstance(person, dict):
            continue
        person_id, name = _integer(person.get("id"), 1), _text(person.get("name"))
        if person_id and name:
            result["cast"].append({"id": person_id, "name": name, "character": _text(person.get("character"))})
    episode = data.get("nextEpisode")
    if isinstance(episode, dict):
        season = _integer(episode.get("season"), 0, 100_000)
        number = _integer(episode.get("episode"), 1, 100_000)
        air_date = _date(episode.get("airDate"))
        if season is not None and number is not None and air_date:
            result["nextEpisode"] = {"season": season, "episode": number, "airDate": air_date}
    return result


class TmdbClient:
    """Optional TMDB client with a request budget and persistent safe caches.

    Search requires an exact title and a known release year. Ambiguous results
    are left unmatched. Supplied sessions remain owned by their caller.
    """

    def __init__(self, cache_dir: Path, token: str = "", api_key: str = "", max_requests: int = 1500, session: Any = None, now: Any = None, max_elapsed_seconds: float = 300):
        self.cache_dir = Path(cache_dir) / "tmdb-v1"
        self.token = token.strip()
        self.api_key = api_key.strip()
        self.max_requests = max(0, int(max_requests))
        self.reset_elapsed_budget(max_elapsed_seconds)
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._now = now
        self.stats = {key: 0 for key in ("requests", "cacheHits", "cacheMisses", "staleFallbacks", "matches", "noMatches", "errors", "budgetExhausted", "elapsedBudgetExhausted", "retries", "cacheWriteErrors")}

    def __enter__(self) -> "TmdbClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def reset_elapsed_budget(self, seconds: float) -> None:
        """Start a new bounded metadata phase without charging unrelated work."""
        limit = float(seconds)
        if not math.isfinite(limit) or limit < 0:
            raise ValueError("Metadata elapsed budget must be finite and non-negative.")
        self.max_elapsed_seconds = limit
        self._started_at = time.monotonic()

    def _timestamp(self) -> float:
        value = self._now() if callable(self._now) else self._now
        if value is None:
            return time.time()
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
        return float(value)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def _read_cache(self, key: str, max_stale_seconds: int | None = None) -> tuple[Any, bool]:
        try:
            path = self._cache_path(key)
            if path.stat().st_size > MAX_CACHE_BYTES:
                return None, False
            entry = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict) or entry.get("version") != 1 or not isinstance(entry.get("data"), dict):
                return None, False
            expires = entry.get("expiresAt")
            if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
                return None, False
            now = self._timestamp()
            if max_stale_seconds is not None and now - expires > max_stale_seconds:
                return None, False
            return entry["data"], expires > now
        except (OSError, ValueError, TypeError):
            return None, False

    def _write_cache(self, key: str, data: dict[str, Any], ttl: int) -> None:
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            entry = {"version": 1, "expiresAt": self._timestamp() + ttl, "data": data}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(entry, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            temporary.replace(self._cache_path(key))
        except (OSError, ValueError, TypeError):
            self.stats["cacheWriteErrors"] += 1
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _retry_delay(self, value: object, attempt: int) -> float:
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                parsed = parsedate_to_datetime(str(value))
                delay = parsed.timestamp() - self._timestamp()
            except (ValueError, TypeError, OverflowError):
                delay = 0.5 * (2 ** attempt)
        return min(MAX_RETRY_DELAY, max(0.0, delay)) if math.isfinite(delay) else MAX_RETRY_DELAY

    def _request(self, route: str, params: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
        if not (self.token or self.api_key):
            return "disabled", None
        query = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        else:
            query["api_key"] = self.api_key
        for attempt in range(MAX_ATTEMPTS):
            if time.monotonic() - self._started_at >= self.max_elapsed_seconds:
                self.stats["elapsedBudgetExhausted"] += 1
                return "unavailable", None
            if self.stats["requests"] >= self.max_requests:
                self.stats["budgetExhausted"] += 1
                return "unavailable", None
            self.stats["requests"] += 1
            response = None
            retry_delay = None
            try:
                response = self.session.get(API_ROOT + route, params=query, headers=headers, timeout=(5, 15), stream=True, allow_redirects=False)
                code = response.status_code
                if code == 404:
                    return "not_found", None
                if code == 429 or 500 <= code < 600:
                    retry_delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                elif code != 200:
                    self.stats["errors"] += 1
                    return "unavailable", None
                else:
                    length = response.headers.get("Content-Length")
                    if length and str(length).isdigit() and int(length) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeds metadata limit")
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() - self._started_at >= self.max_elapsed_seconds:
                            self.stats["elapsedBudgetExhausted"] += 1
                            return "unavailable", None
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ValueError("Response exceeds metadata limit")
                        chunks.append(chunk)
                    payload = json.loads(b"".join(chunks))
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid metadata response")
                    return "ok", payload
            except requests.RequestException:
                retry_delay = self._retry_delay(None, attempt)
            except (ValueError, TypeError, OverflowError):
                self.stats["errors"] += 1
                return "unavailable", None
            finally:
                if response is not None:
                    response.close()
            if attempt + 1 < MAX_ATTEMPTS:
                self.stats["retries"] += 1
                time.sleep(retry_delay or 0)
        self.stats["errors"] += 1
        return "unavailable", None

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in {"movie", "series"}:
            raise ValueError("Metadata kind must be movie or series.")
        return "movie" if kind == "movie" else "tv"

    def enrichment_priority(self, item: Mapping[str, Any], kind: str) -> int:
        """Prioritize missing titles, then stale matches/misses, then fresh cache.

        Reading priority does not consume HTTP requests or mutate statistics.
        This prevents refreshing early catalog entries from starving new titles.
        """
        self._kind(kind)
        key, _identifier_value, _title_value, _year_value = _enrichment_identity(item, kind)
        cached, fresh = self._read_cache(key)
        cached = _cached_metadata(cached)
        if cached is None:
            return 0
        if fresh:
            return 3
        return 1 if cached["matchStatus"].startswith("matched_") else 2

    def enrich(self, item: Mapping[str, Any], kind: str) -> dict[str, Any]:
        api_kind = self._kind(kind)
        key, identifier, title, year = _enrichment_identity(item, kind)
        requests_before = self.stats["requests"]
        cached, fresh = self._read_cache(key)
        cached = _cached_metadata(cached)
        if cached is None:
            fresh = False
        if fresh:
            self.stats["cacheHits"] += 1
            return cached
        self.stats["cacheMisses"] += 1

        def unavailable() -> dict[str, Any]:
            if cached is not None:
                self.stats["staleFallbacks"] += 1
            result = cached if cached is not None else _empty(identifier)
            # Record attempted failures without storing HTTP responses or errors.
            # Untouched titles remain uncached so the next run prioritizes them.
            # A failed refresh retains every previously successful metadata field.
            if self.stats["requests"] > requests_before:
                self._write_cache(key, result, MISS_TTL)
            return result

        if not (self.token or self.api_key):
            return unavailable()
        match_status = "matched_id" if identifier else "matched_search"
        if not identifier:
            # A year is necessary to distinguish remakes, reboots and common titles.
            if not title or not year or "://" in title or not _normalized_title(title):
                result = _empty(None, "no_match")
                self.stats["noMatches"] += 1
                self._write_cache(key, result, MISS_TTL)
                return result
            params = {"query": title, "page": 1, "include_adult": "true", "year" if kind == "movie" else "first_air_date_year": year}
            state, search = self._request(f"/search/{api_kind}", params)
            if state != "ok" or search is None:
                return unavailable()
            results = search.get("results")
            if not isinstance(results, list):
                self.stats["errors"] += 1
                return unavailable()
            matches: set[str] = set()
            if _integer(search.get("total_pages", 1), 0, 100_000) in {0, 1}:
                for candidate in results[:100]:
                    if not isinstance(candidate, dict):
                        continue
                    names = (candidate.get("title"), candidate.get("original_title")) if kind == "movie" else (candidate.get("name"), candidate.get("original_name"))
                    release = _date(candidate.get("release_date" if kind == "movie" else "first_air_date"))
                    candidate_id = _identifier(candidate.get("id"))
                    if candidate_id and release and release[:4] == year and _normalized_title(title) in {_normalized_title(name) for name in names}:
                        matches.add(candidate_id)
            if len(matches) != 1:
                result = _empty(None, "no_match")
                self.stats["noMatches"] += 1
                self._write_cache(key, result, MISS_TTL)
                return result
            identifier = matches.pop()
        state, details = self._request(f"/{api_kind}/{identifier}", {"append_to_response": "credits,videos"})
        if state == "not_found":
            result = _empty(identifier, "no_match")
            self.stats["noMatches"] += 1
            self._write_cache(key, result, MISS_TTL)
            return result
        if state != "ok" or not details or _identifier(details.get("id")) != identifier:
            if state == "ok":
                self.stats["errors"] += 1
            return unavailable()
        result = _metadata(details, kind, match_status)
        self.stats["matches"] += 1
        self._write_cache(key, result, DETAIL_TTL)
        return result

    def trending(self, kind: str) -> dict[str, int]:
        api_kind = self._kind(kind)
        key = f"{kind}:trending:week"
        cached, fresh = self._read_cache(key, MAX_TRENDING_AGE - TRENDING_TTL)
        if not isinstance(cached, dict) or any(_identifier(k) != k or _integer(v, 1, 500) is None for k, v in cached.items()):
            cached, fresh = None, False
        if fresh:
            self.stats["cacheHits"] += 1
            return cached
        self.stats["cacheMisses"] += 1
        ranking: dict[str, int] = {}
        rank = 0
        for page in range(1, 6):
            state, payload = self._request(f"/trending/{api_kind}/week", {"page": page})
            if state != "ok" or payload is None or not isinstance(payload.get("results"), list):
                if cached is not None:
                    self.stats["staleFallbacks"] += 1
                    return cached
                return ranking
            results = payload["results"]
            for item in results[:100]:
                rank += 1
                identifier = _identifier(item.get("id")) if isinstance(item, dict) else None
                if identifier and identifier not in ranking:
                    ranking[identifier] = rank
            total_pages = _integer(payload.get("total_pages", 1), 0, 1_000_000)
            if not results or total_pages is not None and page >= total_pages:
                break
        self._write_cache(key, ranking, TRENDING_TTL)
        return ranking
