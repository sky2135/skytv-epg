#!/usr/bin/env python3
"""Build credential-free, separate movie and series catalogs.

The linear EPG publisher deliberately excludes Xtream VOD rows.  This module
keeps that boundary intact: it reads the provider's VOD actions (or equivalent
offline snapshots), normalizes a small metadata-only schema, and writes two
gzip catalogs per server.  It never publishes playback URLs or credentials.

Catalogs and per-server manifests are content-addressed.  ``index.json`` is
replaced only after every referenced file has been fully written and hashed,
so it is the small, final release pointer consumers should read first.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, urljoin, urlsplit, urlunsplit

import requests


SCHEMA_VERSION = 1
DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
MAX_ITEMS_PER_CATALOG = 250_000
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_DETAIL_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 1024 * 1024 * 1024
MAX_DETAIL_REQUESTS = 50_000
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CATALOG_DECOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_TEXT_LENGTH = 500
MAX_ARTWORK_URL_LENGTH = 2_048
REQUEST_TIMEOUT = (15, 90)
# Use the same application identity as the existing EPG publisher. Some panels
# reject the generic python-requests identity with HTTP 461, even for valid
# accounts. Set this per request so catalog and episode-detail callers agree.
PROVIDER_HEADERS = {
    "User-Agent": "SKYTV-EPG-Builder/2.0",
    "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
}

SERVER_ID_RE = re.compile(r"server_([1-9][0-9]?)\Z")
SAFE_ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")
WHITESPACE_RE = re.compile(r"\s+")
GENRE_SPLIT_RE = re.compile(r"\s*(?:[,;/|]|\s+&\s+)\s*")
LANGUAGE_SPLIT_RE = re.compile(r"\s*(?:[,;/|+]|\s+&\s+)\s*")
ADULT_EVIDENCE_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:adult|xxx|18\+|porn)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?:19|20)\d{2}\Z")
ISO_DATE_RE = re.compile(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")
ISO_MONTH_RE = re.compile(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])\Z")
DMY_DATE_RE = re.compile(r"(0[1-9]|[12]\d|3[01])[-/.](0[1-9]|1[0-2])[-/.]((?:19|20)\d{2})\Z")
ACTION_FILE_NAMES = {
    "get_vod_categories": "vod_categories",
    "get_vod_streams": "vod_streams",
    "get_series_categories": "series_categories",
    "get_series": "series",
}

LANGUAGE_NAMES = {
    "arabic": "ar",
    "bengali": "bn",
    "bangla": "bn",
    "cantonese": "zh",
    "chinese": "zh",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "farsi": "fa",
    "persian": "fa",
    "filipino": "tl",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "gujarati": "gu",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "kannada": "kn",
    "khmer": "km",
    "korean": "ko",
    "lao": "lo",
    "malay": "ms",
    "malayalam": "ml",
    "mandarin": "zh",
    "marathi": "mr",
    "nepali": "ne",
    "norwegian": "no",
    "polish": "pl",
    "portuguese": "pt",
    "punjabi": "pa",
    "romanian": "ro",
    "russian": "ru",
    "sinhalese": "si",
    "slovak": "sk",
    "somali": "so",
    "spanish": "es",
    "swahili": "sw",
    "swedish": "sv",
    "tamil": "ta",
    "telugu": "te",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "vietnamese": "vi",
}
LANGUAGE_CODES = frozenset(LANGUAGE_NAMES.values()) | {"mul", "und"}

KNOWN_GENRES = {
    "action": "action",
    "adventure": "adventure",
    "animation": "animation",
    "anime": "animation",
    "adult": "adult",
    "biography": "biography",
    "comedy": "comedy",
    "crime": "crime",
    "documentary": "documentary",
    "drama": "drama",
    "family": "family",
    "faith": "faith",
    "fantasy": "fantasy",
    "history": "history",
    "horror": "horror",
    "kids": "kids",
    "music": "music",
    "musical": "music",
    "mystery": "mystery",
    "news": "news",
    "reality": "reality",
    "romance": "romance",
    "sci fi": "science_fiction",
    "sci-fi": "science_fiction",
    "science fiction": "science_fiction",
    "sport": "sport",
    "sports": "sport",
    "thriller": "thriller",
    "war": "war",
    "western": "western",
}


class CatalogBuildError(RuntimeError):
    """A safe error whose message must not include provider data or secrets."""


@dataclass(frozen=True)
class ServerConfig:
    server_id: str
    base_url: str
    username: str
    password: str


@dataclass(frozen=True)
class CatalogResult:
    kind: str
    file_name: str
    sha256: str
    compressed_bytes: int
    item_count: int
    input_count: int
    skipped_invalid: int
    completeness: Mapping[str, int]


@dataclass
class DownloadBudget:
    limit: int = MAX_TOTAL_RESPONSE_BYTES
    consumed: int = 0

    def consume(self, amount: int, server_id: str) -> None:
        self.consumed += amount
        if self.consumed > self.limit:
            raise CatalogBuildError(
                f"{server_id}: provider downloads exceed the total size limit."
            )


def clean_text(value: object, maximum: int = MAX_TEXT_LENGTH) -> str:
    """Return printable, normalized provider text with a strict length bound."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(character if ord(character) >= 32 else " " for character in text)
    return WHITESPACE_RE.sub(" ", text).strip()[:maximum]


def normalize_server_id(value: object) -> str:
    server_id = clean_text(value, 30).casefold().replace("-", "_")
    if not SERVER_ID_RE.fullmatch(server_id):
        raise CatalogBuildError("Server IDs must use the server_1 form.")
    return server_id


def normalize_item_id(value: object) -> str:
    if isinstance(value, bool):
        return ""
    text = clean_text(value, 120)
    return text if SAFE_ITEM_ID_RE.fullmatch(text) else ""


def normalize_base_url(value: object, *, allow_insecure_http: bool) -> str:
    raw = clean_text(value, MAX_ARTWORK_URL_LENGTH)
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise CatalogBuildError("A provider base URL is invalid.")
    if parsed.scheme.casefold() == "http" and not allow_insecure_http:
        raise CatalogBuildError(
            "A provider uses HTTP; use HTTPS or explicitly allow insecure HTTP."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CatalogBuildError(
            "A provider base URL must not contain credentials, a query, or a fragment."
        )
    path = re.sub(
        r"/(?:player_api\.php|panel_api\.php|xmltv\.php|get\.php)/?$",
        "",
        parsed.path or "",
        flags=re.I,
    ).rstrip("/")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogBuildError("A provider base URL has an invalid port.") from exc
    host = parsed.hostname.encode("idna").decode("ascii").casefold()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))


def load_server_config(server_id: str, *, allow_insecure_http: bool) -> ServerConfig:
    normalized = normalize_server_id(server_id)
    number = normalized.rsplit("_", 1)[-1]
    prefix = f"SERVER_{number}_"
    base_url = os.environ.get(prefix + "BASE_URL", "")
    username = os.environ.get(prefix + "USERNAME", "").strip()
    password = os.environ.get(prefix + "PASSWORD", "")
    if not base_url.strip() or not username or not password:
        raise CatalogBuildError(
            f"{normalized} is missing its BASE_URL, USERNAME, or PASSWORD setting."
        )
    return ServerConfig(
        server_id=normalized,
        base_url=normalize_base_url(
            base_url, allow_insecure_http=allow_insecure_http
        ),
        username=username,
        password=password,
    )


def _decode_json_bytes(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8-sig")
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBuildError(f"{label} is not valid UTF-8 JSON.") from exc


def read_json_file(path: Path, label: str) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CatalogBuildError(f"Could not read the {label} snapshot.") from exc
    if size > MAX_INPUT_BYTES:
        raise CatalogBuildError(f"The {label} snapshot is too large.")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogBuildError(f"Could not read the {label} snapshot.") from exc
    return _decode_json_bytes(raw, label)


def normalize_rows(payload: Any, label: str) -> list[Mapping[str, Any]]:
    if payload is None or payload is False or payload == {}:
        return []
    rows: Any = payload
    if isinstance(payload, Mapping):
        for key in ("data", "results", "response", "items"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    if not isinstance(rows, list):
        raise CatalogBuildError(f"{label} must contain a JSON list.")
    if len(rows) > MAX_ITEMS_PER_CATALOG:
        raise CatalogBuildError(f"{label} contains too many rows.")
    return [row for row in rows if isinstance(row, Mapping)]


def _auth_rejected(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    user_info = payload.get("user_info")
    if not isinstance(user_info, Mapping):
        return False
    auth = user_info.get("auth")
    return auth in {0, "0", False} or clean_text(user_info.get("status"), 20).casefold() in {
        "disabled",
        "expired",
    }


def request_panel_json(
    session: requests.Session,
    config: ServerConfig,
    action: str,
    *,
    item_id: str = "",
    maximum_bytes: int | None = None,
    budget: DownloadBudget | None = None,
) -> Any:
    if action not in {*ACTION_FILE_NAMES, "get_vod_info", "get_series_info"}:
        raise CatalogBuildError(f"{config.server_id}: unsupported provider action.")
    response_limit = MAX_RESPONSE_BYTES if maximum_bytes is None else maximum_bytes
    endpoint = f"{config.base_url}/player_api.php"
    params = {
        "username": config.username,
        "password": config.password,
        "action": action,
    }
    if item_id:
        params["vod_id" if action == "get_vod_info" else "series_id"] = item_id
    for redirect_count in range(5):
        try:
            response = session.get(
                endpoint,
                params=params,
                headers=dict(PROVIDER_HEADERS),
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise CatalogBuildError(f"{config.server_id}: provider request failed.") from exc
        if not (response.is_redirect or response.is_permanent_redirect):
            break
        try:
            if redirect_count == 4:
                raise CatalogBuildError(f"{config.server_id}: too many provider redirects.")
            endpoint = _safe_panel_redirect(endpoint, response.headers.get("Location", ""), params)
        except (ValueError, CatalogBuildError):
            raise CatalogBuildError(f"{config.server_id}: unsafe provider redirect refused.") from None
        finally:
            response.close()
    try:
        if response.status_code in {401, 403}:
            raise CatalogBuildError(
                f"{config.server_id}: provider credentials were rejected."
            )
        if response.status_code != 200:
            raise CatalogBuildError(
                f"{config.server_id}: {action} provider returned HTTP {response.status_code}."
            )
        header_length = response.headers.get("Content-Length", "")
        if header_length.isdigit() and int(header_length) > response_limit:
            raise CatalogBuildError(
                f"{config.server_id}: provider response is too large."
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=128 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if budget is not None:
                budget.consume(len(chunk), config.server_id)
            if total > response_limit:
                raise CatalogBuildError(
                    f"{config.server_id}: provider response is too large."
                )
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise CatalogBuildError(
            f"{config.server_id}: provider response download failed."
        ) from exc
    finally:
        response.close()
    payload = _decode_json_bytes(b"".join(chunks), f"{config.server_id} provider response")
    if _auth_rejected(payload):
        raise CatalogBuildError(
            f"{config.server_id}: provider credentials were rejected."
        )
    return payload


def _safe_panel_redirect(endpoint: str, location: str, params: Mapping[str, str]) -> str:
    """Permit only a same-host API redirect with the identical original query.

    Credentials are supplied afresh as the original structured parameters. The
    Location header can neither select another action nor rewrite an account.
    """
    if not isinstance(location, str) or not location or len(location) > 8192 or any(ord(character) <= 32 for character in location):
        raise CatalogBuildError("Unsafe provider redirect.")
    original = urlsplit(endpoint)
    target = urlsplit(urljoin(endpoint, location))
    original_port = original.port if original.port is not None else (443 if original.scheme == "https" else 80)
    target_port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
    upgrade = original.scheme == "http" and target.scheme == "https"
    same_port = original_port == target_port or (upgrade and original_port == 80 and target_port == 443)
    if (
        target.scheme not in {"http", "https"}
        or (target.scheme != original.scheme and not upgrade)
        or not target.hostname or target.hostname.casefold() != (original.hostname or "").casefold()
        or not same_port or target.username or target.password or target.fragment
        or target.path != original.path
    ):
        raise CatalogBuildError("Unsafe provider redirect.")
    if target.query:
        query = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if len(query) != len(params) or dict(query) != dict(params):
            raise CatalogBuildError("Unsafe provider redirect.")
    return urlunsplit((target.scheme, target.netloc, target.path, "", ""))


def categories_by_id(payload: Any, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in normalize_rows(payload, label):
        category_id = normalize_item_id(row.get("category_id", row.get("id")))
        name = clean_text(row.get("category_name", row.get("name")), 160)
        if category_id and name and category_id not in result:
            result[category_id] = name
    return result


def normalize_detail_map(
    payload: Any,
    *,
    kind: str,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    if payload in (None, {}, []):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(payload, Mapping):
        for raw_id, detail in payload.items():
            item_id = normalize_item_id(raw_id)
            if item_id and isinstance(detail, Mapping):
                result[item_id] = detail
        if result:
            return result
    if isinstance(payload, list):
        id_keys = ("stream_id", "vod_id", "id") if kind == "movie" else (
            "series_id",
            "id",
        )
        for detail in payload:
            if not isinstance(detail, Mapping):
                continue
            item_id = ""
            for key in id_keys:
                item_id = normalize_item_id(detail.get(key))
                if item_id:
                    break
            if item_id:
                result[item_id] = detail
        return result
    raise CatalogBuildError(f"{label} must map provider IDs to detail objects.")


def load_offline_inputs(input_dir: Path, server_id: str) -> dict[str, Any]:
    bundle_path = input_dir / f"{server_id}_vod_bundle.json"
    if bundle_path.is_file():
        bundle = read_json_file(bundle_path, f"{server_id} VOD bundle")
        if not isinstance(bundle, Mapping):
            raise CatalogBuildError(f"{server_id} VOD bundle must be a JSON object.")
        source = dict(bundle)
    else:
        source: dict[str, Any] = {}
        for action, stem in ACTION_FILE_NAMES.items():
            path = input_dir / f"{server_id}_{stem}.json"
            if path.is_file():
                source[action] = read_json_file(path, f"{server_id} {stem}")
        for key, stem in (
            ("vod_details", "vod_details"),
            ("series_details", "series_details"),
        ):
            path = input_dir / f"{server_id}_{stem}.json"
            if path.is_file():
                source[key] = read_json_file(path, f"{server_id} {stem}")
    for required in ("get_vod_streams", "get_series"):
        if required not in source:
            raise CatalogBuildError(
                f"{server_id} offline input is missing {required}."
            )
    source.setdefault("get_vod_categories", [])
    source.setdefault("get_series_categories", [])
    source.setdefault("vod_details", {})
    source.setdefault("series_details", {})
    return source


def fetch_live_inputs(
    session: requests.Session,
    config: ServerConfig,
    *,
    fetch_details: bool,
) -> dict[str, Any]:
    budget = DownloadBudget()
    source = {
        action: request_panel_json(session, config, action, budget=budget)
        for action in ACTION_FILE_NAMES
    }
    for key in ("get_vod_streams", "get_series"):
        if not normalize_rows(source[key], f"{config.server_id} {key}"):
            raise CatalogBuildError(
                f"{config.server_id}: live {key} returned no usable items; "
                "refusing to replace the existing VOD release."
            )
    source["vod_details"] = {}
    source["series_details"] = {}
    if not fetch_details:
        return source
    detail_request_count = sum(
        len(normalize_rows(source[key], f"{config.server_id} {key}"))
        for key in ("get_vod_streams", "get_series")
    )
    if detail_request_count > MAX_DETAIL_REQUESTS:
        raise CatalogBuildError(
            f"{config.server_id}: detail enrichment exceeds its request limit."
        )
    for kind, action, rows_key, details_key, id_keys in (
        ("movie", "get_vod_info", "get_vod_streams", "vod_details", ("stream_id", "vod_id", "id")),
        ("series", "get_series_info", "get_series", "series_details", ("series_id", "id")),
    ):
        del kind  # The name makes the tuple readable; the action selects the API shape.
        rows = normalize_rows(source[rows_key], f"{config.server_id} {rows_key}")
        details: dict[str, Any] = {}
        for row in rows:
            item_id = ""
            for key in id_keys:
                item_id = normalize_item_id(row.get(key))
                if item_id:
                    break
            if item_id:
                details[item_id] = request_panel_json(
                    session,
                    config,
                    action,
                    item_id=item_id,
                    maximum_bytes=MAX_DETAIL_RESPONSE_BYTES,
                    budget=budget,
                )
        source[details_key] = details
    return source


def _nested_sources(row: Mapping[str, Any], detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = [row, detail]
    for key in ("movie_data", "series_data", "info", "metadata"):
        candidate = detail.get(key)
        if isinstance(candidate, Mapping):
            sources.append(candidate)
    return sources


def _first_value(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Any:
    for source in reversed(sources):
        for key in keys:
            if key in source and source.get(key) not in (None, "", [], {}):
                return source.get(key)
    return None


def _flatten_values(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, Mapping):
        for key in ("iso_639_1", "code", "name", "title", "value"):
            if value.get(key) not in (None, ""):
                yield clean_text(value.get(key), 120)
                return
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_values(item)
        return
    yield clean_text(value, 300)


def normalize_languages(
    sources: Sequence[Mapping[str, Any]], category_name: str
) -> tuple[str, list[str]]:
    raw = _first_value(
        sources,
        ("languages", "language", "lang", "original_language", "spoken_languages"),
    )
    values = list(_flatten_values(raw))
    # A provider category is acceptable evidence only when it explicitly names
    # a language; country/market alone never implies one.
    values.append(category_name)
    output: list[str] = []
    saw_multilingual = False
    for value in values:
        folded = value.casefold().replace("_", "-")
        if "bilingual" in folded or "multilingual" in folded or folded == "multi":
            saw_multilingual = True
        pieces = LANGUAGE_SPLIT_RE.split(folded)
        for piece in pieces:
            token = piece.strip(" .()[]{}")
            code = ""
            if token in LANGUAGE_NAMES:
                code = LANGUAGE_NAMES[token]
            elif token in LANGUAGE_CODES:
                code = token
            else:
                for name, candidate in LANGUAGE_NAMES.items():
                    if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", token):
                        if candidate not in output:
                            output.append(candidate)
            if code and code not in {"und", "mul"} and code not in output:
                output.append(code)
    if len(output) > 1:
        return "mul", output
    if saw_multilingual:
        # "Bilingual" or "multilingual" without at least two named component
        # languages is incomplete evidence. Preserve it as unknown instead of
        # publishing an internally inconsistent mul record.
        return "und", ["und"]
    if output:
        return output[0], output
    return "und", ["und"]


def _genre_slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")[:48]


def normalize_genres(
    sources: Sequence[Mapping[str, Any]], category_name: str
) -> tuple[str, list[str]]:
    raw = _first_value(sources, ("genres", "genre"))
    candidates: list[str] = []
    for value in _flatten_values(raw):
        candidates.extend(part for part in GENRE_SPLIT_RE.split(value) if part)
    folded_category = category_name.casefold()
    for name, canonical in KNOWN_GENRES.items():
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", folded_category):
            candidates.append(canonical)
    output: list[str] = []
    for candidate in candidates:
        folded = clean_text(candidate, 100).casefold()
        canonical = KNOWN_GENRES.get(folded, _genre_slug(folded))
        if canonical in {"", "unknown", "undetermined", "none", "n_a"}:
            continue
        if canonical and canonical not in output:
            output.append(canonical)
        if len(output) == 12:
            break
    return (output[0], output) if output else ("unknown", [])


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    text = clean_text(value, 40).casefold()
    if text in {"1", "true", "yes", "y", "adult", "xxx", "18+"}:
        return True
    if text in {"0", "false", "no", "n", "not adult", "non-adult", "safe"}:
        return False
    return None


def normalize_adult(
    sources: Sequence[Mapping[str, Any]], category_name: str
) -> bool | None:
    explicit = _first_value(
        sources,
        ("adult", "is_adult", "isAdult", "adult_content", "is_18_plus"),
    )
    rating = clean_text(
        _first_value(sources, ("content_rating", "certification", "mpaa_rating")),
        40,
    ).casefold()
    genre_evidence = " ".join(
        _flatten_values(_first_value(sources, ("genres", "genre")))
    )
    if rating in {"adult", "xxx", "18+", "x", "nc-17"} or ADULT_EVIDENCE_RE.search(
        f"{category_name} {genre_evidence}"
    ):
        return True
    parsed = _boolish(explicit)
    if parsed is not None:
        return parsed
    return None


def normalize_tmdb_id(sources: Sequence[Mapping[str, Any]]) -> str | None:
    value = _first_value(sources, ("tmdb_id", "tmdbId", "tmdb"))
    if isinstance(value, Mapping):
        value = value.get("id")
    text = clean_text(value, 30)
    if text.casefold().startswith("tmdb:"):
        text = text.split(":", 1)[1].strip()
    return text if text.isdigit() and 0 < int(text) < 10**12 else None


def normalize_release_date(sources: Sequence[Mapping[str, Any]]) -> str | None:
    value = _first_value(
        sources,
        ("releaseDate", "release_date", "releasedate", "first_air_date", "air_date", "year"),
    )
    text = clean_text(value, 80)
    if not text:
        return None
    match = ISO_DATE_RE.search(text)
    if match:
        rendered = "-".join(match.groups())
        try:
            datetime.strptime(rendered, "%Y-%m-%d")
        except ValueError:
            return None
        return rendered
    if ISO_MONTH_RE.fullmatch(text) or YEAR_RE.fullmatch(text):
        return text
    match = DMY_DATE_RE.fullmatch(text)
    if match:
        day, month, year = match.groups()
        rendered = f"{year}-{month}-{day}"
        try:
            datetime.strptime(rendered, "%Y-%m-%d")
        except ValueError:
            return None
        return rendered
    return None


def _artwork_candidate(value: Any) -> str:
    if isinstance(value, Mapping):
        value = next(
            (value.get(key) for key in ("file_path", "url", "src") if value.get(key)),
            "",
        )
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item), "")
    return clean_text(value, MAX_ARTWORK_URL_LENGTH)


def normalize_artwork_url(
    value: Any,
    *,
    tmdb_size: str,
    blocked_hostname: str = "",
) -> str | None:
    text = _artwork_candidate(value)
    if not text:
        return None
    if text.startswith("/") and not text.startswith("//"):
        text = f"https://image.tmdb.org/t/p/{tmdb_size}{text}"
    parsed = urlsplit(text)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    blocked = blocked_hostname.casefold().strip()
    if blocked and blocked in host:
        # The release safety scan deliberately treats the configured provider
        # hostname as private. Provider-hosted artwork therefore stays null;
        # third-party/CDN or TMDB artwork remains publishable.
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    # Provider query strings can contain expiring signatures or account data;
    # publishing stable, query-free artwork is the conservative contract.
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, "", ""))


def normalize_artwork(
    sources: Sequence[Mapping[str, Any]],
    *,
    blocked_hostname: str = "",
) -> dict[str, str | None]:
    poster = _first_value(
        sources,
        ("poster_path", "poster", "cover_big", "cover", "movie_image", "stream_icon"),
    )
    backdrop = _first_value(
        sources,
        ("backdrop_path", "backdrop", "backdrop_url", "background"),
    )
    return {
        "posterUrl": normalize_artwork_url(
            poster,
            tmdb_size="w500",
            blocked_hostname=blocked_hostname,
        ),
        "backdropUrl": normalize_artwork_url(
            backdrop,
            tmdb_size="original",
            blocked_hostname=blocked_hostname,
        ),
    }


def _sort_item_id(item: Mapping[str, Any]) -> tuple[int, int | str, str]:
    item_id = str(item["id"])
    if item_id.isdigit():
        return (0, int(item_id), str(item["title"]).casefold())
    return (1, item_id.casefold(), str(item["title"]).casefold())


def normalize_catalog(
    rows_payload: Any,
    categories_payload: Any,
    details_payload: Any,
    *,
    kind: str,
    label: str,
    blocked_artwork_hostname: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if kind not in {"movie", "series"}:
        raise ValueError("kind must be movie or series")
    rows = normalize_rows(rows_payload, label)
    categories = categories_by_id(categories_payload, f"{label} categories")
    details = normalize_detail_map(details_payload, kind=kind, label=f"{label} details")
    id_keys = ("stream_id", "vod_id", "id") if kind == "movie" else (
        "series_id",
        "id",
    )
    output: list[dict[str, Any]] = []
    skipped = 0
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = ""
        for key in id_keys:
            item_id = normalize_item_id(row.get(key))
            if item_id:
                break
        title = clean_text(row.get("name", row.get("title")), 300)
        if not item_id or not title:
            skipped += 1
            continue
        category_id = normalize_item_id(row.get("category_id")) or None
        category_name = clean_text(
            row.get("category_name") or categories.get(category_id or "", ""), 160
        )
        detail = details.get(item_id, {})
        sources = _nested_sources(row, detail)
        language, languages = normalize_languages(sources, category_name)
        genre, genres = normalize_genres(sources, category_name)
        item = {
            "id": item_id,
            "title": title,
            "category": {"id": category_id, "name": category_name or None},
            "language": language,
            "languages": languages,
            "genre": genre,
            "genres": genres,
            "releaseDate": normalize_release_date(sources),
            "adult": normalize_adult(sources, category_name),
            "tmdbId": normalize_tmdb_id(sources),
            "artwork": normalize_artwork(
                sources,
                blocked_hostname=blocked_artwork_hostname,
            ),
        }
        previous = seen.get(item_id)
        if previous is not None:
            if previous != item:
                raise CatalogBuildError(
                    f"{label} contains conflicting rows for item ID {item_id}."
                )
            continue
        seen[item_id] = item
        output.append(item)
    output.sort(key=_sort_item_id)
    completeness = {
        "languageKnown": sum(item["language"] != "und" for item in output),
        "genreKnown": sum(item["genre"] != "unknown" for item in output),
        "releaseDateKnown": sum(item["releaseDate"] is not None for item in output),
        "adultKnown": sum(item["adult"] is not None for item in output),
        "tmdbIdKnown": sum(item["tmdbId"] is not None for item in output),
        "artworkKnown": sum(
            bool(item["artwork"]["posterUrl"] or item["artwork"]["backdropUrl"])
            for item in output
        ),
    }
    return output, {
        "inputCount": len(rows),
        "skippedInvalid": skipped,
        "completeness": completeness,
    }


def generated_timestamp(value: str = "") -> str:
    if value:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CatalogBuildError("--now-utc must be an ISO-8601 timestamp.") from exc
        if parsed.tzinfo is None:
            raise CatalogBuildError("--now-utc must include a timezone.")
        parsed = parsed.astimezone(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(payload: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        rendered = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    return rendered.encode("utf-8")


def _secret_variants(secret: str) -> set[bytes]:
    if not secret:
        return set()
    raw_secret = secret.encode("utf-8")
    variants = {
        secret,
        json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret, ensure_ascii=True)[1:-1],
        base64.b64encode(raw_secret).decode("ascii"),
        base64.urlsafe_b64encode(raw_secret).decode("ascii"),
    }
    encoded = secret
    for _ in range(3):
        encoded = quote(encoded, safe="")
        variants.add(encoded)
    variants.add(quote_plus(secret, safe=""))
    fully_percent_encoded = "".join(f"%{byte:02X}" for byte in raw_secret)
    variants.update(
        {
            fully_percent_encoded,
            fully_percent_encoded.casefold(),
            quote(fully_percent_encoded, safe=""),
        }
    )
    return {value.encode("utf-8") for value in variants if value}


def ensure_no_secret_reflection(payload: Any, config: ServerConfig | None) -> None:
    if config is None:
        return
    raw = _json_bytes(payload)
    secrets = [config.password]
    # Very short/common usernames create false positives such as movie titles;
    # longer account identifiers are still protected from provider reflection.
    if len(config.username) >= 6:
        secrets.append(config.username)
    for secret in secrets:
        if any(variant in raw for variant in _secret_variants(secret)):
            raise CatalogBuildError(
                f"{config.server_id}: provider content reflected a configured secret."
            )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json_from_bytes(raw: bytes, label: str) -> Any:
    def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CatalogBuildError(f"{label} contains a duplicate JSON key.")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise CatalogBuildError(f"{label} contains a non-finite JSON number.")

    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except CatalogBuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBuildError(f"{label} is not valid UTF-8 JSON.") from exc


def _read_bounded_json(path: Path, maximum_bytes: int, label: str) -> Any:
    try:
        size = path.stat().st_size
        if size <= 0 or size > maximum_bytes:
            raise CatalogBuildError(f"{label} has an invalid size.")
        raw = path.read_bytes()
    except CatalogBuildError:
        raise
    except OSError as exc:
        raise CatalogBuildError(f"Could not read {label}.") from exc
    return _strict_json_from_bytes(raw, label)


def _safe_referenced_file(
    output_dir: Path,
    file_name: Any,
    *,
    pattern: re.Pattern[str],
    label: str,
) -> Path:
    if not isinstance(file_name, str) or not pattern.fullmatch(file_name):
        raise CatalogBuildError(f"{label} has an invalid file name.")
    if Path(file_name).name != file_name or "/" in file_name or "\\" in file_name:
        raise CatalogBuildError(f"{label} must stay inside the VOD output directory.")
    try:
        root = output_dir.resolve(strict=True)
        candidate = output_dir / file_name
        if candidate.is_symlink():
            raise CatalogBuildError(f"{label} must not be a symbolic link.")
        resolved = candidate.resolve(strict=True)
    except CatalogBuildError:
        raise
    except OSError as exc:
        raise CatalogBuildError(f"{label} is missing or unreadable.") from exc
    if resolved.parent != root or not resolved.is_file():
        raise CatalogBuildError(f"{label} must stay inside the VOD output directory.")
    return resolved


def _required_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogBuildError(f"{label} must be a non-negative integer.")
    if maximum is not None and value > maximum:
        raise CatalogBuildError(f"{label} exceeds its limit.")
    return value


def _required_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CatalogBuildError(f"{label} is not a SHA-256 digest.")
    return value


def _expect_exact_keys(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CatalogBuildError(f"{label} does not match schema version 1.")
    return value


def _read_catalog_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        raw = bytearray()
        with gzip.open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_CATALOG_DECOMPRESSED_BYTES:
                    raise CatalogBuildError(f"{label} expands beyond its size limit.")
    except CatalogBuildError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise CatalogBuildError(f"{label} is not a valid gzip file.") from exc
    payload = _strict_json_from_bytes(bytes(raw), label)
    if not isinstance(payload, Mapping):
        raise CatalogBuildError(f"{label} must contain a JSON object.")
    return payload


def _validate_catalog_item(
    raw_item: Any,
    *,
    label: str,
) -> Mapping[str, Any]:
    item = _expect_exact_keys(
        raw_item,
        {
            "id",
            "title",
            "category",
            "language",
            "languages",
            "genre",
            "genres",
            "releaseDate",
            "adult",
            "tmdbId",
            "artwork",
        },
        label,
    )
    if not isinstance(item["id"], str) or normalize_item_id(item["id"]) != item["id"]:
        raise CatalogBuildError(f"{label} has an invalid ID.")
    if not isinstance(item["title"], str) or not item["title"] or len(item["title"]) > 300:
        raise CatalogBuildError(f"{label} has an invalid title.")
    category = _expect_exact_keys(item["category"], {"id", "name"}, f"{label} category")
    if category["id"] is not None and (
        not isinstance(category["id"], str)
        or normalize_item_id(category["id"]) != category["id"]
    ):
        raise CatalogBuildError(f"{label} has an invalid category ID.")
    if category["name"] is not None and (
        not isinstance(category["name"], str)
        or not category["name"]
        or len(category["name"]) > 160
    ):
        raise CatalogBuildError(f"{label} has an invalid category name.")
    language = item["language"]
    languages = item["languages"]
    if language not in LANGUAGE_CODES or not isinstance(languages, list) or not languages:
        raise CatalogBuildError(f"{label} has invalid language metadata.")
    if any(code not in LANGUAGE_CODES for code in languages) or len(set(languages)) != len(languages):
        raise CatalogBuildError(f"{label} has invalid language metadata.")
    if language == "und" and languages != ["und"]:
        raise CatalogBuildError(f"{label} has inconsistent unknown language metadata.")
    if language not in {"mul", "und"} and languages != [language]:
        raise CatalogBuildError(f"{label} has inconsistent language metadata.")
    if language == "mul" and (
        len(languages) < 2 or any(code in {"mul", "und"} for code in languages)
    ):
        raise CatalogBuildError(f"{label} has inconsistent multilingual metadata.")
    genre = item["genre"]
    genres = item["genres"]
    if not isinstance(genre, str) or not isinstance(genres, list):
        raise CatalogBuildError(f"{label} has invalid genre metadata.")
    if any(not isinstance(value, str) or not value for value in genres):
        raise CatalogBuildError(f"{label} has invalid genre metadata.")
    if len(set(genres)) != len(genres) or len(genres) > 12:
        raise CatalogBuildError(f"{label} has invalid genre metadata.")
    if (genre == "unknown") != (genres == []):
        raise CatalogBuildError(f"{label} has inconsistent genre metadata.")
    if genres and genre != genres[0]:
        raise CatalogBuildError(f"{label} has inconsistent genre metadata.")
    release_date = item["releaseDate"]
    if release_date is not None and (
        not isinstance(release_date, str)
        or normalize_release_date([{"releaseDate": release_date}]) != release_date
    ):
        raise CatalogBuildError(f"{label} has an invalid release date.")
    if item["adult"] is not None and not isinstance(item["adult"], bool):
        raise CatalogBuildError(f"{label} has an invalid adult status.")
    if item["adult"] is not True and (
        "adult" in genres
        or (
            isinstance(category["name"], str)
            and ADULT_EVIDENCE_RE.search(category["name"])
        )
    ):
        raise CatalogBuildError(
            f"{label} must mark strong adult evidence as adult."
        )
    tmdb_id = item["tmdbId"]
    if tmdb_id is not None and (
        not isinstance(tmdb_id, str)
        or not tmdb_id.isdigit()
        or not 0 < int(tmdb_id) < 10**12
    ):
        raise CatalogBuildError(f"{label} has an invalid TMDB ID.")
    artwork = _expect_exact_keys(
        item["artwork"], {"posterUrl", "backdropUrl"}, f"{label} artwork"
    )
    for key, size in (("posterUrl", "w500"), ("backdropUrl", "original")):
        value = artwork[key]
        if value is not None and (
            not isinstance(value, str)
            or normalize_artwork_url(value, tmdb_size=size) != value
        ):
            raise CatalogBuildError(f"{label} has an invalid artwork URL.")
    return item


def _validate_catalog_file(
    path: Path,
    entry: Mapping[str, Any],
    *,
    server_id: str,
    kind: str,
    generated_at: str,
    label: str,
) -> dict[str, int]:
    expected_keys = {
        "schemaVersion",
        "serverId",
        "contentType",
        "generatedAt",
        "itemCount",
        "items",
    }
    payload = _expect_exact_keys(_read_catalog_json(path, label), expected_keys, label)
    if (
        payload["schemaVersion"] != SCHEMA_VERSION
        or payload["serverId"] != server_id
        or payload["contentType"] != kind
        or payload["generatedAt"] != generated_at
    ):
        raise CatalogBuildError(f"{label} identity does not match its manifest.")
    items = payload["items"]
    if not isinstance(items, list):
        raise CatalogBuildError(f"{label} items must be a JSON list.")
    item_count = _required_int(
        payload["itemCount"], f"{label} itemCount", maximum=MAX_ITEMS_PER_CATALOG
    )
    if item_count != len(items) or item_count != entry["itemCount"]:
        raise CatalogBuildError(f"{label} item count does not match its manifest.")
    ids: set[str] = set()
    completeness = {
        "languageKnown": 0,
        "genreKnown": 0,
        "releaseDateKnown": 0,
        "adultKnown": 0,
        "tmdbIdKnown": 0,
        "artworkKnown": 0,
    }
    for position, raw_item in enumerate(items):
        item = _validate_catalog_item(raw_item, label=f"{label} item {position + 1}")
        item_id = str(item["id"])
        if item_id in ids:
            raise CatalogBuildError(f"{label} contains a duplicate item ID.")
        ids.add(item_id)
        completeness["languageKnown"] += item["language"] != "und"
        completeness["genreKnown"] += item["genre"] != "unknown"
        completeness["releaseDateKnown"] += item["releaseDate"] is not None
        completeness["adultKnown"] += item["adult"] is not None
        completeness["tmdbIdKnown"] += item["tmdbId"] is not None
        completeness["artworkKnown"] += bool(
            item["artwork"]["posterUrl"] or item["artwork"]["backdropUrl"]
        )
    if dict(entry["metadataCompleteness"]) != completeness:
        raise CatalogBuildError(f"{label} metadata completeness does not match.")
    return completeness


def _validate_manifest_catalog_entry(
    output_dir: Path,
    raw_entry: Any,
    *,
    server_id: str,
    plural_kind: str,
    generated_at: str,
) -> tuple[Mapping[str, Any], str]:
    label = f"{server_id} {plural_kind} catalog"
    entry = _expect_exact_keys(
        raw_entry,
        {
            "file",
            "sha256",
            "compressedBytes",
            "itemCount",
            "inputCount",
            "skippedInvalid",
            "metadataCompleteness",
        },
        f"{label} manifest entry",
    )
    expected_file = re.compile(
        rf"{re.escape(server_id)}_{plural_kind}\.[0-9a-f]{{16}}\.json\.gz\Z"
    )
    path = _safe_referenced_file(
        output_dir, entry["file"], pattern=expected_file, label=label
    )
    expected_sha = _required_sha256(entry["sha256"], f"{label} SHA-256")
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise CatalogBuildError(f"Could not read {label}.") from exc
    compressed_bytes = _required_int(
        entry["compressedBytes"], f"{label} size", maximum=MAX_INPUT_BYTES
    )
    if compressed_bytes != actual_size:
        raise CatalogBuildError(f"{label} size does not match.")
    try:
        actual_sha = sha256_file(path)
    except OSError as exc:
        raise CatalogBuildError(f"Could not read {label}.") from exc
    if actual_sha != expected_sha or not str(entry["file"]).split(".")[1] == expected_sha[:16]:
        raise CatalogBuildError(f"{label} SHA-256 does not match.")
    item_count = _required_int(
        entry["itemCount"], f"{label} itemCount", maximum=MAX_ITEMS_PER_CATALOG
    )
    input_count = _required_int(
        entry["inputCount"], f"{label} inputCount", maximum=MAX_ITEMS_PER_CATALOG
    )
    skipped = _required_int(
        entry["skippedInvalid"],
        f"{label} skippedInvalid",
        maximum=MAX_ITEMS_PER_CATALOG,
    )
    if item_count > input_count or skipped > input_count or item_count + skipped > input_count:
        raise CatalogBuildError(f"{label} source counts are inconsistent.")
    completeness = _expect_exact_keys(
        entry["metadataCompleteness"],
        {
            "languageKnown",
            "genreKnown",
            "releaseDateKnown",
            "adultKnown",
            "tmdbIdKnown",
            "artworkKnown",
        },
        f"{label} metadata completeness",
    )
    for key, value in completeness.items():
        _required_int(value, f"{label} {key}", maximum=item_count)
    kind = "movie" if plural_kind == "movies" else "series"
    _validate_catalog_file(
        path,
        entry,
        server_id=server_id,
        kind=kind,
        generated_at=generated_at,
        label=label,
    )
    return entry, expected_sha


def validate_publication(
    output_dir: Path,
    *,
    expected_servers: Sequence[str] | None = None,
) -> dict[str, int]:
    """Verify the complete index -> manifest -> gzip catalog trust chain."""
    try:
        root = output_dir.resolve(strict=True)
    except OSError as exc:
        raise CatalogBuildError("The VOD output directory is missing.") from exc
    index_path = root / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise CatalogBuildError("VOD index.json is missing or is not a regular file.")
    index = _expect_exact_keys(
        _read_bounded_json(index_path, MAX_INDEX_BYTES, "VOD index.json"),
        {"schemaVersion", "generatedAt", "serverCount", "servers"},
        "VOD index.json",
    )
    if index["schemaVersion"] != SCHEMA_VERSION:
        raise CatalogBuildError("VOD index.json has an unsupported schema version.")
    generated_at = generated_timestamp(str(index["generatedAt"]))
    if generated_at != index["generatedAt"]:
        raise CatalogBuildError("VOD index.json generatedAt is not canonical UTC.")
    raw_servers = index["servers"]
    if not isinstance(raw_servers, list) or not raw_servers:
        raise CatalogBuildError("VOD index.json must name at least one server.")
    server_count = _required_int(index["serverCount"], "VOD index serverCount")
    if server_count != len(raw_servers):
        raise CatalogBuildError("VOD index serverCount does not match its server list.")
    seen_servers: set[str] = set()
    total_movies = 0
    total_series = 0
    prior_server = ""
    for raw_server in raw_servers:
        server = _expect_exact_keys(
            raw_server,
            {
                "serverId",
                "releaseId",
                "manifestFile",
                "manifestSha256",
                "manifestBytes",
                "movieCount",
                "seriesCount",
            },
            "VOD index server entry",
        )
        server_id = normalize_server_id(server["serverId"])
        if server_id in seen_servers or (prior_server and server_id <= prior_server):
            raise CatalogBuildError("VOD index server entries are duplicate or unsorted.")
        seen_servers.add(server_id)
        prior_server = server_id
        manifest_pattern = re.compile(
            rf"{re.escape(server_id)}_vod_manifest\.[0-9a-f]{{16}}\.json\Z"
        )
        manifest_path = _safe_referenced_file(
            root,
            server["manifestFile"],
            pattern=manifest_pattern,
            label=f"{server_id} VOD manifest",
        )
        manifest_sha = _required_sha256(
            server["manifestSha256"], f"{server_id} manifest SHA-256"
        )
        try:
            actual_manifest_size = manifest_path.stat().st_size
        except OSError as exc:
            raise CatalogBuildError(f"Could not read {server_id} VOD manifest.") from exc
        if (
            _required_int(
                server["manifestBytes"],
                f"{server_id} manifestBytes",
                maximum=MAX_MANIFEST_BYTES,
            )
            != actual_manifest_size
        ):
            raise CatalogBuildError(f"{server_id} VOD manifest size does not match.")
        try:
            actual_manifest_sha = sha256_file(manifest_path)
        except OSError as exc:
            raise CatalogBuildError(f"Could not read {server_id} VOD manifest.") from exc
        if (
            actual_manifest_sha != manifest_sha
            or str(server["manifestFile"]).split(".")[1] != manifest_sha[:16]
        ):
            raise CatalogBuildError(f"{server_id} VOD manifest SHA-256 does not match.")
        manifest = _expect_exact_keys(
            _read_bounded_json(
                manifest_path, MAX_MANIFEST_BYTES, f"{server_id} VOD manifest"
            ),
            {"schemaVersion", "serverId", "generatedAt", "releaseId", "catalogs"},
            f"{server_id} VOD manifest",
        )
        if (
            manifest["schemaVersion"] != SCHEMA_VERSION
            or manifest["serverId"] != server_id
            or manifest["generatedAt"] != generated_at
            or manifest["releaseId"] != server["releaseId"]
        ):
            raise CatalogBuildError(f"{server_id} VOD manifest identity does not match.")
        catalogs = _expect_exact_keys(
            manifest["catalogs"], {"movies", "series"}, f"{server_id} catalogs"
        )
        movie_entry, movie_sha = _validate_manifest_catalog_entry(
            root,
            catalogs["movies"],
            server_id=server_id,
            plural_kind="movies",
            generated_at=generated_at,
        )
        series_entry, series_sha = _validate_manifest_catalog_entry(
            root,
            catalogs["series"],
            server_id=server_id,
            plural_kind="series",
            generated_at=generated_at,
        )
        release_id = hashlib.sha256(
            f"{movie_sha}:{series_sha}".encode("ascii")
        ).hexdigest()[:24]
        if manifest["releaseId"] != release_id:
            raise CatalogBuildError(f"{server_id} releaseId does not match its catalogs.")
        movie_count = _required_int(
            server["movieCount"], f"{server_id} movieCount", maximum=MAX_ITEMS_PER_CATALOG
        )
        series_count = _required_int(
            server["seriesCount"], f"{server_id} seriesCount", maximum=MAX_ITEMS_PER_CATALOG
        )
        if movie_count != movie_entry["itemCount"] or series_count != series_entry["itemCount"]:
            raise CatalogBuildError(f"{server_id} index catalog counts do not match.")
        total_movies += movie_count
        total_series += series_count
    if expected_servers is not None:
        normalized_expected = [normalize_server_id(value) for value in expected_servers]
        if len(set(normalized_expected)) != len(normalized_expected):
            raise CatalogBuildError("Expected VOD servers must be unique.")
        if seen_servers != set(normalized_expected):
            missing = sorted(set(normalized_expected) - seen_servers)
            unexpected = sorted(seen_servers - set(normalized_expected))
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unexpected:
                detail.append(f"unexpected {', '.join(unexpected)}")
            raise CatalogBuildError(
                "VOD index server set does not match the expected release"
                + (f": {'; '.join(detail)}." if detail else ".")
            )
    return {
        "serverCount": server_count,
        "movieCount": total_movies,
        "seriesCount": total_series,
    }


def _temporary_path(output_dir: Path, prefix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{prefix}.", dir=output_dir)
    os.close(descriptor)
    return Path(raw_path)


def _install_content_addressed(
    temporary: Path,
    output_dir: Path,
    stem: str,
    suffix: str,
) -> tuple[str, str, int]:
    digest = sha256_file(temporary)
    name = f"{stem}.{digest[:16]}{suffix}"
    destination = output_dir / name
    if destination.exists():
        if sha256_file(destination) != digest:
            raise CatalogBuildError("A content-addressed output has unexpected bytes.")
        temporary.unlink()
    else:
        os.replace(temporary, destination)
    return name, digest, destination.stat().st_size


def write_gzip_catalog(
    output_dir: Path,
    server_id: str,
    kind: str,
    generated_at: str,
    items: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
) -> CatalogResult:
    plural_kind = "movies" if kind == "movie" else "series"
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "serverId": server_id,
        "contentType": kind,
        "generatedAt": generated_at,
        "itemCount": len(items),
        "items": list(items),
    }
    temporary = _temporary_path(output_dir, f"{server_id}_{plural_kind}")
    try:
        with temporary.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="", fileobj=raw_handle, mode="wb", mtime=0, compresslevel=9
            ) as gzip_handle:
                gzip_handle.write(_json_bytes(payload))
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        name, digest, size = _install_content_addressed(
            temporary, output_dir, f"{server_id}_{plural_kind}", ".json.gz"
        )
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return CatalogResult(
        kind=kind,
        file_name=name,
        sha256=digest,
        compressed_bytes=size,
        item_count=len(items),
        input_count=int(stats["inputCount"]),
        skipped_invalid=int(stats["skippedInvalid"]),
        completeness=dict(stats["completeness"]),
    )


def write_content_addressed_json(
    output_dir: Path, stem: str, payload: Any
) -> tuple[str, str, int]:
    temporary = _temporary_path(output_dir, stem)
    try:
        with temporary.open("wb") as handle:
            handle.write(_json_bytes(payload, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        name, digest, size = _install_content_addressed(
            temporary, output_dir, stem, ".json"
        )
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return name, digest, size


def write_final_index(output_dir: Path, payload: Any) -> None:
    """Atomically replace the one stable release pointer, always last."""
    temporary = _temporary_path(output_dir, "index")
    try:
        with temporary.open("wb") as handle:
            handle.write(_json_bytes(payload, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_dir / "index.json")
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _catalog_manifest_entry(result: CatalogResult) -> dict[str, Any]:
    return {
        "file": result.file_name,
        "sha256": result.sha256,
        "compressedBytes": result.compressed_bytes,
        "itemCount": result.item_count,
        "inputCount": result.input_count,
        "skippedInvalid": result.skipped_invalid,
        "metadataCompleteness": dict(result.completeness),
    }


def publish_server(
    output_dir: Path,
    server_id: str,
    source: Mapping[str, Any],
    *,
    generated_at: str,
    config: ServerConfig | None = None,
) -> dict[str, Any]:
    blocked_artwork_hostname = ""
    if config is not None:
        blocked_artwork_hostname = urlsplit(config.base_url).hostname or ""
    movies, movie_stats = normalize_catalog(
        source["get_vod_streams"],
        source.get("get_vod_categories", []),
        source.get("vod_details", {}),
        kind="movie",
        label=f"{server_id} movies",
        blocked_artwork_hostname=blocked_artwork_hostname,
    )
    series, series_stats = normalize_catalog(
        source["get_series"],
        source.get("get_series_categories", []),
        source.get("series_details", {}),
        kind="series",
        label=f"{server_id} series",
        blocked_artwork_hostname=blocked_artwork_hostname,
    )
    ensure_no_secret_reflection({"movies": movies, "series": series}, config)

    # Data files are deliberately installed before their checksum manifest.
    movie_result = write_gzip_catalog(
        output_dir, server_id, "movie", generated_at, movies, movie_stats
    )
    series_result = write_gzip_catalog(
        output_dir, server_id, "series", generated_at, series, series_stats
    )
    release_id = hashlib.sha256(
        f"{movie_result.sha256}:{series_result.sha256}".encode("ascii")
    ).hexdigest()[:24]
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "serverId": server_id,
        "generatedAt": generated_at,
        "releaseId": release_id,
        "catalogs": {
            "movies": _catalog_manifest_entry(movie_result),
            "series": _catalog_manifest_entry(series_result),
        },
    }
    manifest_name, manifest_sha, manifest_size = write_content_addressed_json(
        output_dir, f"{server_id}_vod_manifest", manifest
    )
    return {
        "serverId": server_id,
        "releaseId": release_id,
        "manifestFile": manifest_name,
        "manifestSha256": manifest_sha,
        "manifestBytes": manifest_size,
        "movieCount": movie_result.item_count,
        "seriesCount": series_result.item_count,
    }


def publish_catalogs(
    output_dir: Path,
    server_sources: Sequence[
        tuple[str, Mapping[str, Any], ServerConfig | None]
    ],
    *,
    generated_at: str,
) -> dict[str, Any]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CatalogBuildError("Could not create the VOD output directory.") from exc
    seen: set[str] = set()
    servers: list[dict[str, Any]] = []
    for raw_server_id, source, config in server_sources:
        server_id = normalize_server_id(raw_server_id)
        if server_id in seen:
            raise CatalogBuildError(f"Duplicate server selection: {server_id}.")
        seen.add(server_id)
        servers.append(
            publish_server(
                output_dir,
                server_id,
                source,
                generated_at=generated_at,
                config=config,
            )
        )
    servers.sort(key=lambda entry: entry["serverId"])
    index = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "serverCount": len(servers),
        "servers": servers,
    }
    # This stable file is the only release switch and must always be last.
    write_final_index(output_dir, index)
    return index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        action="append",
        default=[],
        help="Server ID to publish; repeat as needed (default: server_1..server_3).",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        help="Use credential-free offline JSON snapshots instead of the live panel.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("public/VOD"))
    parser.add_argument(
        "--snapshot-dir", type=Path,
        help="Private temporary provider snapshots for the app exporter; never publish this directory.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Verify an existing public/VOD release without network access.",
    )
    parser.add_argument(
        "--expected-server",
        action="append",
        default=[],
        help=(
            "Server ID required in a validate-only release; repeat for the full "
            "expected set."
        ),
    )
    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="Fetch per-title Xtream details in live mode (can make many requests).",
    )
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--now-utc", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validate_only:
        if (
            args.server
            or args.input_dir is not None
            or args.fetch_details
            or args.allow_insecure_http
            or args.now_utc
            or args.snapshot_dir is not None
        ):
            raise CatalogBuildError(
                "--validate-only cannot be combined with publication input options."
            )
        summary = validate_publication(
            args.output_dir,
            expected_servers=args.expected_server or None,
        )
        print(
            f"Verified {summary['movieCount']:,} movies and "
            f"{summary['seriesCount']:,} series for "
            f"{summary['serverCount']} server(s)."
        )
        return 0
    if args.expected_server:
        raise CatalogBuildError("--expected-server requires --validate-only.")
    selected = args.server or list(DEFAULT_SERVERS)
    normalized_servers = [normalize_server_id(value) for value in selected]
    if len(set(normalized_servers)) != len(normalized_servers):
        raise CatalogBuildError("Each server may be selected only once.")
    if args.input_dir is not None and args.fetch_details:
        raise CatalogBuildError(
            "--fetch-details is for live panel mode; supply offline detail files instead."
        )
    timestamp = generated_timestamp(args.now_utc)
    sources: list[tuple[str, Mapping[str, Any], ServerConfig | None]] = []
    if args.input_dir is not None:
        for server_id in normalized_servers:
            sources.append(
                (server_id, load_offline_inputs(args.input_dir, server_id), None)
            )
    else:
        session = requests.Session()
        session.trust_env = False
        try:
            for server_id in normalized_servers:
                config = load_server_config(
                    server_id, allow_insecure_http=args.allow_insecure_http
                )
                source = fetch_live_inputs(
                    session, config, fetch_details=args.fetch_details
                )
                sources.append((server_id, source, config))
        finally:
            session.close()
    if args.snapshot_dir is not None:
        # Raw provider responses may contain private URLs. Keep them outside
        # the publication tree and use them only within this build job.
        snapshot_dir = args.snapshot_dir.resolve()
        publication_root = args.output_dir.resolve().parent
        if snapshot_dir == publication_root or publication_root in snapshot_dir.parents:
            raise CatalogBuildError("Private snapshots must stay outside the public directory.")
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for server_id, source, _config in sources:
            # Atomic replacement also prevents an existing symlink/hardlink
            # from redirecting a private snapshot write into public output.
            temporary = _temporary_path(snapshot_dir, server_id)
            try:
                temporary.write_bytes(_json_bytes(source))
                os.replace(temporary, snapshot_dir / f"{server_id}_vod_bundle.json")
            finally:
                temporary.unlink(missing_ok=True)
    index = publish_catalogs(
        args.output_dir, sources, generated_at=timestamp
    )
    total_movies = sum(int(row["movieCount"]) for row in index["servers"])
    total_series = sum(int(row["seriesCount"]) for row in index["servers"])
    print(
        f"Published {total_movies:,} movies and {total_series:,} series "
        f"for {len(index['servers'])} server(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CatalogBuildError as exc:
        print(f"VOD catalog build failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
