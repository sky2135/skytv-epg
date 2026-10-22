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

from vod_reviewed_corrections import correction_for
from vod_poster_index import POSTER_POLICY, MAX_IMAGES, IMAGE_PATH as POSTER_PATH, official_paths, poster_identity, shared_index


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
STATUSES = {"matched_id", "matched_search", "matched_poster", "no_match", "unavailable"}
IDENTITY_POLICY = 2
MATCH_STRATEGY = 3
MAX_SEARCH_PAGES = 8
MAX_ALIAS_CANDIDATES = 3
DIAGNOSTIC_REASONS = frozenset({
    "matched_id", "matched_search", "matched_alias", "matched_poster", "ambiguous_title_year",
    "no_search_results", "title_mismatch", "year_mismatch", "incomplete_search",
    "alias_candidate_limit", "unavailable", "missing_identity",
})
# Only provider decoration is removed. Articles, subtitles, sequels and numbers
# remain identity-bearing ("Doctor" must never become "Doctor Strange").
# Locale labels are recognized only in the observed spaced-dash/pipe format.
# An acronym followed by a colon can be part of the film's actual title.
PROVIDER_LOCALES = "AR|BN|CS|DA|DE|EL|EN|ES|FA|FI|FR|GU|HE|HI|HU|ID|IT|JA|KM|KN|KO|LO|ML|MR|MS|NE|NL|NO|PA|PL|PT|RO|RU|SI|SK|SO|SV|SW|TA|TE|TH|TL|TR|UK|UR|VI|ZH"
PROVIDER_PREFIX = re.compile(rf"^(?:(?:{PROVIDER_LOCALES})\s+[-|]\s+|(?:4K|FHD|UHD|HD|SD)\s*[-|:]\s+)")
PROVIDER_SUFFIX = re.compile(r"\s*(?:\[(?:4K|FHD|UHD|HD|SD)\]|\((?:4K|FHD|UHD|HD|SD|HINDI|MARATHI|TAMIL|TELUGU|ENGLISH|PUNJABI|DUBBED)\))\s*$", re.I)


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
    title = PROVIDER_PREFIX.sub("", title)
    for _ in range(3):
        title = PROVIDER_SUFFIX.sub("", title).strip()
    trailing_year = re.search(r"(?:\s*[\[(]((?:19|20)\d{2})[\])]|\s+-\s+((?:19|20)\d{2}))\s*$", title)
    year = None
    for value in (item.get("year"), item.get("releaseDate"), item.get("release_date"), item.get("first_air_date")):
        match = re.match(r"^((?:19|20)\d{2})(?:$|-\d{2})", str(value or ""))
        if match:
            year = match.group(1)
            break
    if trailing_year:
        # The independently displayed year wins over possibly polluted detail
        # metadata supplied alongside an incorrect TMDB identifier.
        year = trailing_year.group(1) or trailing_year.group(2)
        title = title[:trailing_year.start()].strip()
    return title, year


def _image(path: object, size: str) -> str | None:
    return f"https://image.tmdb.org/t/p/{size}{path}" if isinstance(path, str) and IMAGE_PATH.fullmatch(path) else None


def _enrichment_identity(item: Mapping[str, Any], kind: str) -> tuple[str, str | None, str, str | None]:
    identifier = _identifier(item.get("tmdbId") or item.get("tmdb_id") or item.get("tmdb"))
    title, year = _title_and_year(item)
    correction = _reviewed(item, kind)
    if correction:
        title = correction.get("searchTitle") or title
        year = correction.get("year") or year
        identifier = _identifier(correction["metadata"].get("tmdbId")) if correction["rejectIdentity"] else identifier
    key = f"{kind}:identity-v{IDENTITY_POLICY}:id:{identifier or ''}:title:{_normalized_title(title)}:year:{year or ''}"
    evidence = _identity_evidence(item, kind)
    if evidence:
        # Evidence is asset-specific. It must never resolve another row that
        # happens to share this normalized title/year or a recycled provider ID.
        key += f":record:{evidence['contentId']}:evidence:{evidence['fingerprint']}:{evidence['digest']}"
    poster = poster_identity(item)
    if poster:
        key += f":poster-v{POSTER_POLICY}:" + hashlib.sha256(poster.encode()).hexdigest()
    return key, identifier, title, year


def _identity_evidence(item: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    raw = item.get("_identityEvidence")
    if not isinstance(raw, Mapping):
        return None
    content_id = item.get("contentId")
    fingerprint = raw.get("fingerprint")
    if (not isinstance(content_id, str) or raw.get("contentId") != content_id
            or not re.fullmatch(rf"server_[1-9][0-9]?:{kind}:[0-9]{{1,10}}", content_id)
            or ("id" in item and str(item["id"]) != content_id.rsplit(":", 1)[1])
            or not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)):
        return None

    def names(value: object) -> list[str]:
        result = set()
        for person in value[:40] if isinstance(value, list) else []:
            name = person.get("name") if isinstance(person, Mapping) else person
            if isinstance(name, str) and len(name) <= 100 and "://" not in name:
                normalized = _normalized_title(name)
                if normalized:
                    result.add(normalized)
        return sorted(result)

    imdb = raw.get("imdbId")
    imdb = imdb if isinstance(imdb, str) and re.fullmatch(r"tt[0-9]{7,10}", imdb) else None
    directors, cast = names(raw.get("directors")), names(raw.get("cast"))
    if not imdb and not (directors and len(cast) >= 2):
        return None
    runtime = _number(raw.get("runtimeMinutes"), 10_000)
    runtime = round(runtime, 2) if runtime is not None and runtime >= 1 else None
    facts = {"imdbId": imdb, "directors": directors, "cast": cast,
             "displayTitle": _text(item.get("title") or item.get("name")),
             "runtimeMinutes": runtime}
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**facts, "contentId": content_id, "fingerprint": fingerprint, "digest": digest}


def _evidence_matches(details: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    external = details.get("external_ids")
    # A supplied external ID is a constraint, never an optional score bonus.
    if evidence["imdbId"]:
        identifiers = [details.get("imdb_id")]
        if isinstance(external, Mapping):
            identifiers.append(external.get("imdb_id"))
        present = [value for value in identifiers if value not in (None, "")]
        return bool(present) and all(value == evidence["imdbId"] for value in present)
    credits = details.get("credits")
    if not isinstance(credits, Mapping):
        return False
    crew, cast = credits.get("crew"), credits.get("cast")
    directors = {_normalized_title(person.get("name")) for person in crew[:1000]
                 if isinstance(person, Mapping) and person.get("job") == "Director"} if isinstance(crew, list) else set()
    actors = {_normalized_title(person.get("name")) for person in cast[:100]
              if isinstance(person, Mapping)} if isinstance(cast, list) else set()
    if not (directors.intersection(evidence["directors"]) and len(actors.intersection(evidence["cast"])) >= 2):
        return False
    runtime = _integer(details.get("runtime"), 1, 10_000)
    expected_runtime = evidence.get("runtimeMinutes")
    return not (runtime and expected_runtime and abs(runtime - expected_runtime) > 5)


def _match_diagnostic(value: object = None, *, cache_hit: bool = False) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    reason = raw.get("reason")
    result = {"reason": reason if isinstance(reason, str) and reason in DIAGNOSTIC_REASONS else "unavailable", "cacheHit": cache_hit,
              "candidateIds": []}
    identifiers = raw.get("candidateIds")
    if isinstance(identifiers, list):
        result["candidateIds"] = list(dict.fromkeys(identifier for value in identifiers[:100]
                                                   if (identifier := _identifier(value))))
    for field in ("searchPages", "candidateCount", "exactMatchCount", "aliasCandidatesChecked"):
        result[field] = _integer(raw.get(field), 0, 100_000) or 0
    result["evidenceMatched"] = raw.get("evidenceMatched") is True
    result["searchComplete"] = raw.get("searchComplete") is True
    if raw.get("posterPolicy") == POSTER_POLICY or result["reason"] == "matched_poster":
        result["posterPolicy"] = POSTER_POLICY
    return result


def _reviewed(item: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    content_id = str(item.get("contentId") or "")
    server_id = str(item.get("_serverId") or (content_id.split(":", 1)[0] if ":" in content_id else ""))
    return correction_for(server_id, kind, item) if server_id else None


def _detail_identity(details: Mapping[str, Any], kind: str) -> dict[str, Any]:
    names = [details.get("title"), details.get("original_title")] if kind == "movie" else [details.get("name"), details.get("original_name")]
    alternatives = details.get("alternative_titles")
    alternatives = alternatives.get("titles" if kind == "movie" else "results") if isinstance(alternatives, dict) else None
    if isinstance(alternatives, list):
        names.extend(alias.get("title") for alias in alternatives[:100] if isinstance(alias, dict))
    release = _date(details.get("release_date" if kind == "movie" else "first_air_date"))
    return {"names": sorted({_normalized_title(name) for name in names if _normalized_title(name)}),
            "year": release[:4] if release else None,
            "originalTitle": _normalized_title(details.get("original_title" if kind == "movie" else "original_name")),
            "tmdbId": _identifier(details.get("id"))}


def _identity_matches(proof: Mapping[str, Any], title: str, year: str | None) -> bool:
    names = proof.get("names")
    if not isinstance(names, list) or len(names) > 102 or any(
            not isinstance(name, str) or not name or name != _normalized_title(name) for name in names):
        return False
    return bool(_normalized_title(title) and _normalized_title(title) in names
                and (year is None or proof.get("year") == year))


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
        "adult": None, "rejectedIdentity": False,
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
    if not isinstance(data, dict) or set(data) not in (set(_empty(None)), set(_empty(None)) - {"rejectedIdentity"}) or data.get("matchStatus") not in STATUSES:
        return None
    identifier = _identifier(data.get("tmdbId"))
    if data["matchStatus"].startswith("matched_") and identifier is None:
        return None
    result = _empty(identifier, data["matchStatus"])
    result["rejectedIdentity"] = data.get("rejectedIdentity") is True
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
        self.last_match_diagnostic = _match_diagnostic()
        self.poster_index = shared_index(self.cache_dir)
        self._poster_cache_state = {}

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

    def _identity_cache(self, key: str, item: Mapping[str, Any], kind: str,
                        identifier: str | None, title: str, year: str | None) -> tuple[dict[str, Any] | None, bool, bool]:
        """Read only caches corroborated against the current display identity.

        Legacy positive entries can be reused without requests when their own
        original title and year independently agree. Rejected legacy entries
        never become stale fallbacks. New entries retain bounded alias proof so
        translated original titles can be reused safely after an API check.
        """
        value, fresh = self._read_cache(key)
        poster = poster_identity(item)
        if poster:
            self._poster_cache_state[key] = (False, False, None)
        if value is None and poster_identity(item):
            # Adopt old verified positives without refreshing the whole library.
            # Old misses lack this new evidence and must not mask the new rule.
            previous, previous_fresh = self._read_cache(key.rsplit(f":poster-v{POSTER_POLICY}:", 1)[0])
            metadata = _cached_metadata(previous.get("metadata")) if isinstance(previous, dict) else None
            if metadata and metadata["matchStatus"].startswith("matched_") and metadata["matchStatus"] != "matched_poster":
                value, fresh = previous, previous_fresh
        evidence = _identity_evidence(item, kind)
        correction = _reviewed(item, kind)
        rejected_ids = correction.get("rejectedTmdbIds", []) if correction else []
        if isinstance(value, dict) and value.get("identityPolicy") == IDENTITY_POLICY:
            metadata = _cached_metadata(value.get("metadata"))
            proof = value.get("identity")
            if metadata is not None:
                if metadata["tmdbId"] in rejected_ids:
                    return None, False, True
                if not metadata["matchStatus"].startswith("matched_"):
                    if value.get("matchStrategy") != MATCH_STRATEGY:
                        return None, False, False
                    attempted = proof.get("posterCandidate") if isinstance(proof, dict) else None
                    if poster:
                        self._poster_cache_state[key] = (False, fresh, _identifier(attempted))
                    return metadata, fresh, metadata["rejectedIdentity"]
                if isinstance(proof, dict) and _identity_matches(proof, title, year) and (
                    proof.get("tmdbId") == metadata["tmdbId"]
                    and (identifier is None or metadata["tmdbId"] == identifier)
                    and proof.get("originalTitle") == _normalized_title(metadata["originalTitle"])
                    and proof.get("year") == ((metadata["releaseDate"] or "")[:4] or None)
                    and (metadata["matchStatus"] != "matched_poster" or (
                        proof.get("posterPolicy") == POSTER_POLICY and proof.get("posterPath") == poster
                        and proof.get("mediaKind") == kind and not self.poster_index.collided(poster)))
                    and (not evidence or (proof.get("evidenceDigest") == evidence["digest"]
                                          and proof.get("evidenceFingerprint") == evidence["fingerprint"]
                                          and proof.get("contentId") == evidence["contentId"]))
                ):
                    self._remember_poster(kind, metadata, proof)
                    if metadata["matchStatus"] == "matched_poster" and self.poster_index.collided(poster):
                        return None, False, True
                    if poster:
                        self._poster_cache_state[key] = (True, fresh, None)
                    return metadata, fresh, metadata["rejectedIdentity"]
            return None, False, True
        if evidence:
            return None, False, False
        # Only pre-policy entries use these keys. Never read them as unbound
        # payloads at the new key, and never migrate misses or unvalidated IDs.
        raw_identifier = _identifier(item.get("tmdbId") or item.get("tmdb_id") or item.get("tmdb"))
        legacy_key = f"{kind}:id:{raw_identifier}" if raw_identifier else f"{kind}:title:{_normalized_title(title)}:year:{year or ''}"
        legacy, legacy_fresh = self._read_cache(legacy_key)
        metadata = _cached_metadata(legacy)
        if metadata is None or not metadata["matchStatus"].startswith("matched_"):
            return None, False, False
        if correction and correction["rejectIdentity"]:
            return None, False, True
        valid = bool(year and _normalized_title(title)
                     and _normalized_title(metadata["originalTitle"]) == _normalized_title(title)
                     and (metadata["releaseDate"] or "")[:4] == year
                     and (identifier is None or metadata["tmdbId"] == identifier))
        if valid and metadata["matchStatus"] != "matched_poster":
            self._remember_poster(kind, metadata, {"names": [_normalized_title(title)], "year": year})
            if poster:
                self._poster_cache_state[key] = (True, legacy_fresh, None)
            return metadata, legacy_fresh, False
        return None, False, True

    def _remember_poster(self, kind, metadata, proof):
        path = poster_identity({"artwork": {"posterUrl": metadata.get("posterUrl")}})
        paths = {path} if path else set()
        additional = proof.get("posterPaths")
        if proof.get("mediaKind") == kind and isinstance(additional, list) and len(additional) <= MAX_IMAGES + 1:
            paths.update(value for value in additional if isinstance(value, str) and POSTER_PATH.fullmatch(value))
        if metadata.get("tmdbId"):
            self.poster_index.add(kind, metadata["tmdbId"], paths, proof)

    def warm_poster_index(self, items, kind):
        """Seed once per prepared group from current identity-verified caches."""
        self._kind(kind)
        seeded = 0
        for item in items:
            key, identifier, title, year = _enrichment_identity(item, kind)
            metadata, _fresh, _rejected = self._identity_cache(key, item, kind, identifier, title, year)
            seeded += bool(metadata and metadata["matchStatus"].startswith("matched_"))
        return seeded

    def poster_candidate(self, item, kind):
        """Cheap index candidate lookup; never performs HTTP or reads files."""
        path = poster_identity(item)
        if not path:
            return None
        _key, identifier, title, year = _enrichment_identity(item, kind)
        candidate = self.poster_index.lookup(path, kind, _normalized_title(title), year)
        correction = _reviewed(item, kind)
        rejected = correction.get("rejectedTmdbIds", []) if correction else []
        return candidate if candidate and candidate not in rejected and (identifier is None or candidate == identifier) else None

    def poster_retry_candidate(self, item, kind):
        """Prioritize missing identities, using the existing priority-read memo."""
        candidate = self.poster_candidate(item, kind)
        if candidate:
            key = _enrichment_identity(item, kind)[0]
            matched, fresh, attempted = self._poster_cache_state.get(key, (False, False, None))
            if not matched and not (fresh and attempted == candidate):
                return candidate
        return None

    def _write_identity_cache(self, key: str, metadata: dict[str, Any], ttl: int,
                              proof: Mapping[str, Any] | None = None) -> None:
        self._write_cache(key, {"identityPolicy": IDENTITY_POLICY,
                                "matchStrategy": MATCH_STRATEGY,
                                "matchDiagnostic": _match_diagnostic(self.last_match_diagnostic),
                                "identity": dict(proof or {}), "metadata": metadata}, ttl)

    def enrichment_priority(self, item: Mapping[str, Any], kind: str) -> int:
        """Prioritize missing titles, then stale matches/misses, then fresh cache.

        Reading priority does not consume HTTP requests or mutate statistics.
        This prevents refreshing early catalog entries from starving new titles.
        """
        self._kind(kind)
        key, identifier, title, year = _enrichment_identity(item, kind)
        cached, fresh, _rejected = self._identity_cache(key, item, kind, identifier, title, year)
        if _reviewed(item, kind) and (cached is None or not fresh):
            return -1
        if cached is None:
            return 0
        if fresh:
            return 3
        return 1 if cached["matchStatus"].startswith("matched_") else 2

    def enrich(self, item: Mapping[str, Any], kind: str) -> dict[str, Any]:
        api_kind = self._kind(kind)
        self.last_match_diagnostic = _match_diagnostic()
        key, identifier, title, year = _enrichment_identity(item, kind)
        evidence = _identity_evidence(item, kind)
        correction = _reviewed(item, kind)
        rejected_ids = correction.get("rejectedTmdbIds", []) if correction else []
        requests_before = self.stats["requests"]
        cached, fresh, rejected = self._identity_cache(key, item, kind, identifier, title, year)
        rejected = rejected or bool(correction and correction["rejectIdentity"])
        poster_attempt = None
        if cached is None:
            fresh = False
        if fresh and cached and not cached["matchStatus"].startswith("matched_") and self.poster_retry_candidate(item, kind):
            fresh = False
        if fresh:
            self.stats["cacheHits"] += 1
            stored, _fresh = self._read_cache(key)
            diagnostic = stored.get("matchDiagnostic") if isinstance(stored, Mapping) else None
            self.last_match_diagnostic = _match_diagnostic(diagnostic, cache_hit=True)
            if not diagnostic:
                self.last_match_diagnostic["reason"] = cached["matchStatus"] if cached["matchStatus"].startswith("matched_") else "unavailable"
            return cached
        self.stats["cacheMisses"] += 1

        def unavailable(reason: str = "unavailable") -> dict[str, Any]:
            self.last_match_diagnostic["reason"] = reason
            if cached is not None:
                self.stats["staleFallbacks"] += 1
            result = cached if cached is not None else _empty(None if rejected else identifier)
            result["rejectedIdentity"] = rejected
            # Record attempted failures without storing HTTP responses or errors.
            # Untouched titles remain uncached so the next run prioritizes them.
            # A failed refresh retains every previously successful metadata field.
            if self.stats["requests"] > requests_before:
                previous, _fresh = self._read_cache(key)
                proof = previous.get("identity") if isinstance(previous, dict) else None
                if cached is not None and cached["matchStatus"].startswith("matched_") and not proof:
                    proof = {"names": [_normalized_title(cached["originalTitle"])],
                             "originalTitle": _normalized_title(cached["originalTitle"]),
                             "year": (cached["releaseDate"] or "")[:4], "tmdbId": cached["tmdbId"]}
                self._write_identity_cache(key, result, MISS_TTL, proof)
            return result

        def no_match(reason: str = "title_mismatch") -> dict[str, Any]:
            self.last_match_diagnostic["reason"] = reason
            result = _empty(None, "no_match")
            result["rejectedIdentity"] = rejected
            self.stats["noMatches"] += 1
            proof = {"posterCandidate": poster_attempt} if poster_attempt else None
            self._write_identity_cache(key, result, MISS_TTL, proof)
            return result

        if not (self.token or self.api_key):
            return unavailable()
        # A provider ID alone does not identify the displayed asset. Original
        # title is deliberately never an identity fallback: it may be polluted.
        if not title or "://" in title or not _normalized_title(title):
            rejected = bool(identifier) or rejected
            return no_match("missing_identity")
        match_status = "matched_id" if identifier else "matched_search"
        details = None
        alias_match = False
        poster = poster_identity(item)
        poster_available = bool(poster and not self.poster_index.collided(poster))
        poster_assisted = False
        if evidence and evidence["imdbId"]:
            # Bound external identity avoids choosing among same-name works or
            # an incomplete title search. It does not relax display title/year
            # checks, supplied IDs, reviewed rejections, or the global budget.
            if not year:
                return no_match("missing_identity")
            state, found = self._request(f"/find/{evidence['imdbId']}", {"external_source": "imdb_id"})
            if state != "ok" or found is None:
                return unavailable()
            results = found.get("movie_results" if kind == "movie" else "tv_results")
            if (not isinstance(results, list) or len(results) > 100
                    or any(not isinstance(candidate, dict)
                           or type(candidate.get("id")) is not int or not _identifier(candidate["id"])
                           or candidate.get("media_type", api_kind) != api_kind for candidate in results)):
                self.stats["errors"] += 1
                return unavailable()
            found_ids = {_identifier(candidate["id"]) for candidate in results}
            self.last_match_diagnostic.update(candidateCount=len(found_ids),
                                              candidateIds=sorted(found_ids, key=int))
            if len(found_ids) > 1:
                return unavailable("ambiguous_title_year")
            if found_ids:
                found_id = next(iter(found_ids))
                if found_id in rejected_ids:
                    rejected = True
                    return no_match("title_mismatch")
                if identifier is not None and found_id != identifier:
                    rejected = True
                    return unavailable()
                identifier = found_id
                state, details = self._request(f"/{api_kind}/{identifier}",
                                               {"append_to_response": "credits,videos,alternative_titles,external_ids"})
                if state != "ok" or not details or _identifier(details.get("id")) != identifier:
                    if state == "ok":
                        self.stats["errors"] += 1
                    rejected = True
                    return unavailable()
        if not identifier and poster_available:
            candidate = self.poster_candidate(item, kind)
            if candidate:
                identifier = candidate
                poster_assisted = True
                poster_attempt = candidate
        if not identifier:
            # A year is necessary to distinguish remakes, reboots and common titles.
            if not title or not year or "://" in title or not _normalized_title(title):
                return no_match("missing_identity")
            matches: set[str] = set()
            candidates: dict[str, tuple[tuple[str, ...], str | None]] = {}
            eligible: set[str] = set()
            same_title_other_year = False
            total_pages, total_results, observed_results = None, None, 0
            for page in range(1, MAX_SEARCH_PAGES + 1):
                params = {"query": title, "page": page, "include_adult": "true", "year" if kind == "movie" else "first_air_date_year": year}
                state, search = self._request(f"/search/{api_kind}", params)
                if state != "ok" or search is None:
                    return unavailable("incomplete_search")
                results = search.get("results")
                page_count = _integer(search.get("total_pages", 1), 0, 100_000)
                result_count = _integer(search.get("total_results"), 0, 10_000_000)
                if (not isinstance(results, list) or len(results) > 100 or page_count is None
                        or _integer(search.get("page", page), 1, 100_000) != page
                        or (page_count == 0 and results)
                        or (page > 1 and (page_count != total_pages or not results))
                        or (page > 1 and result_count != total_results)):
                    self.stats["errors"] += 1
                    return unavailable("incomplete_search")
                total_pages, total_results = page_count, result_count
                observed_results += len(results)
                self.last_match_diagnostic["searchPages"] = page
                for candidate in results:
                    if not isinstance(candidate, dict):
                        self.stats["errors"] += 1
                        return unavailable("incomplete_search")
                    names = (candidate.get("title"), candidate.get("original_title")) if kind == "movie" else (candidate.get("name"), candidate.get("original_name"))
                    release = _date(candidate.get("release_date" if kind == "movie" else "first_air_date"))
                    candidate_id = _identifier(candidate.get("id"))
                    if not candidate_id:
                        self.stats["errors"] += 1
                        return unavailable("incomplete_search")
                    signature = (tuple(sorted({_normalized_title(name) for name in names if _normalized_title(name)})), release)
                    if candidate_id in candidates and candidates[candidate_id] != signature:
                        return unavailable("incomplete_search")
                    candidates[candidate_id] = signature
                    if candidate_id in rejected_ids:
                        continue
                    same_title = _normalized_title(title) in signature[0]
                    if release is None or release[:4] == year:
                        eligible.add(candidate_id)
                    if same_title and release and release[:4] != year:
                        same_title_other_year = True
                    if same_title and release and release[:4] == year:
                        matches.add(candidate_id)
                self.last_match_diagnostic.update(candidateCount=len(candidates), exactMatchCount=len(matches),
                                                  candidateIds=sorted(matches or eligible or set(candidates), key=int)[:100])
                self.last_match_diagnostic["searchComplete"] = bool(
                    page >= max(1, total_pages) and (total_results is None or observed_results == total_results))
                # Two distinct exact IDs are enough to prove ambiguity. Evidence
                # may resolve a small set, but never a partially inspected set.
                if len(matches) > 1 and ((not evidence and not poster_available) or len(matches) > MAX_ALIAS_CANDIDATES):
                    return no_match("ambiguous_title_year")
                if total_pages > MAX_SEARCH_PAGES:
                    return unavailable("incomplete_search")
                if page >= max(1, total_pages):
                    if total_results is not None and observed_results != total_results:
                        return unavailable("incomplete_search")
                    break
            else:
                return unavailable("incomplete_search")

            if not candidates:
                return no_match("no_search_results")
            if len(matches) == 1 and not evidence:
                identifier = next(iter(matches))
            else:
                # Search may find an official alternate title, while exposing
                # only the primary/original title in its results. All eligible
                # candidates must be checked before accepting such a match.
                check_ids = eligible if evidence else matches or eligible
                if not check_ids:
                    return no_match("year_mismatch" if same_title_other_year else "title_mismatch")
                if len(check_ids) > MAX_ALIAS_CANDIDATES:
                    return no_match("ambiguous_title_year" if len(matches) > 1 else "alias_candidate_limit")
                corroborated = []
                poster_tie = bool(poster_available and len(matches) > 1 and not evidence)
                for candidate_id in sorted(check_ids, key=int):
                    append = "credits,videos,alternative_titles" + (",external_ids" if evidence and evidence["imdbId"] else "")
                    if poster_tie:
                        append += ",images"
                    state, candidate_details = self._request(f"/{api_kind}/{candidate_id}", {"append_to_response": append})
                    if state != "ok" or not candidate_details or _identifier(candidate_details.get("id")) != candidate_id:
                        if state == "ok":
                            self.stats["errors"] += 1
                        return unavailable("incomplete_search")
                    self.last_match_diagnostic["aliasCandidatesChecked"] += 1
                    if (_identity_matches(_detail_identity(candidate_details, kind), title, year)
                            and (not evidence or _evidence_matches(candidate_details, evidence))):
                        corroborated.append((candidate_id, candidate_details))
                        if len(corroborated) > 1 and not poster_tie:
                            return no_match("ambiguous_title_year")
                if poster_tie:
                    with_poster = []
                    for candidate_id, candidate_details in corroborated:
                        paths, complete = official_paths(candidate_details)
                        if not complete:
                            return unavailable("incomplete_search")
                        if poster in paths:
                            with_poster.append((candidate_id, candidate_details))
                    if len(with_poster) != 1:
                        return no_match("ambiguous_title_year")
                    corroborated = with_poster
                    poster_assisted = True
                if not corroborated:
                    return no_match("ambiguous_title_year" if matches else "title_mismatch")
                identifier, details = corroborated[0]
                alias_match = not matches
                self.last_match_diagnostic["evidenceMatched"] = bool(evidence)
        if details is None:
            append = "credits,videos,alternative_titles" + (",external_ids" if evidence and evidence["imdbId"] else "")
            if poster_assisted:
                append += ",images"
            state, details = self._request(f"/{api_kind}/{identifier}", {"append_to_response": append})
            if state == "not_found":
                rejected = True
                return no_match()
            if state != "ok" or not details or _identifier(details.get("id")) != identifier:
                if state == "ok":
                    self.stats["errors"] += 1
                return unavailable()
        proof = _detail_identity(details, kind)
        if not _identity_matches(proof, title, year):
            rejected = True
            return no_match("year_mismatch" if proof.get("year") != year else "title_mismatch")
        if evidence:
            if not _evidence_matches(details, evidence):
                return no_match("title_mismatch")
            self.last_match_diagnostic["evidenceMatched"] = True
            proof.update(contentId=evidence["contentId"], evidenceFingerprint=evidence["fingerprint"], evidenceDigest=evidence["digest"])
        paths, _complete = official_paths(details)
        if poster_assisted:
            if poster not in paths or self.poster_index.collided(poster):
                return no_match("title_mismatch")
            proof.update(posterPath=poster, posterPolicy=POSTER_POLICY)
            match_status = "matched_poster"
        proof.update(mediaKind=kind, posterPaths=sorted(paths))
        result = _metadata(details, kind, match_status)
        self._remember_poster(kind, result, proof)
        result["rejectedIdentity"] = rejected
        self.stats["matches"] += 1
        self.last_match_diagnostic["reason"] = "matched_poster" if poster_assisted else "matched_alias" if alias_match else match_status
        self._write_identity_cache(key, result, DETAIL_TTL, proof)
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
