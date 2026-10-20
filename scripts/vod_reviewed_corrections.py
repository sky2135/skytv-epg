"""Record-scoped, source-backed corrections from the 2026-10-20 public audit.

These are reviewed exceptions, never fuzzy title matches. Provider audio fields
remain untouched. Evidence and exact record/title guards live beside the rules.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


MAX_CORRECTION_BYTES = 256 * 1024
METADATA_KEYS = {"tmdbId", "originalTitle", "originalLanguage", "releaseDate", "genres", "directors"}


def _bounded_text(value: object, limit: int = 300) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value) <= limit and all(ord(character) >= 32 for character in value))


def _public_evidence_url(value: object) -> bool:
    if not _bounded_text(value, 2048) or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (not value.startswith("https://") or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host)
                or "." not in host or ".." in host
                or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if parsed.query:
            # The reviewed broadcaster promo is a public YouTube watch link.
            # Other query strings could include account tokens or signatures.
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
            return (host in {"www.youtube.com", "youtube.com"} and parsed.path == "/watch"
                    and len(query) == 1 and query[0][0] == "v"
                    and re.fullmatch(r"[A-Za-z0-9_-]{11}", query[0][1]) is not None)
        return True
    except (ValueError, TypeError):
        return False


def _validate_document(document: object) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Fail closed on ambiguous rules, unsupported fields and private evidence."""
    if (not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1
            or set(document) - {"version", "reviewedAt", "scope", "corrections"}
            or not isinstance(document.get("corrections"), list)
            or not 1 <= len(document["corrections"]) <= 1000):
        raise ValueError("Invalid reviewed VOD correction document.")
    result = {}
    for row in document["corrections"]:
        if (not isinstance(row, dict) or set(row) - {"serverId", "kind", "recordId", "displayNames", "rejectIdentity", "rejectedTmdbIds", "metadata", "evidenceURLs", "title", "year", "searchTitle"}
                or not isinstance(row.get("serverId"), str) or not re.fullmatch(r"server_[1-9][0-9]?", row["serverId"])
                or row.get("kind") not in {"movie", "series"}
                or not isinstance(row.get("recordId"), str) or not re.fullmatch(r"[0-9]{1,10}", row["recordId"])
                or not isinstance(row.get("rejectIdentity"), bool)):
            raise ValueError("Invalid reviewed VOD correction scope.")
        titles, evidence, metadata = row.get("displayNames"), row.get("evidenceURLs"), row.get("metadata")
        if (not isinstance(titles, list) or not 1 <= len(titles) <= 10
                or any(not _bounded_text(title) or not _signature(title) or "://" in title for title in titles)
                or not isinstance(evidence, list) or not 1 <= len(evidence) <= 20
                or any(not _public_evidence_url(url) for url in evidence)
                or not isinstance(metadata, dict) or set(metadata) - METADATA_KEYS):
            raise ValueError("Invalid reviewed VOD correction metadata or evidence.")
        for field in ("title", "searchTitle"):
            if field in row and (not _bounded_text(row[field]) or "://" in row[field]):
                raise ValueError("Invalid reviewed VOD correction title.")
        if "year" in row and (not isinstance(row["year"], str) or not re.fullmatch(r"(?:19|20)[0-9]{2}", row["year"])):
            raise ValueError("Invalid reviewed VOD correction year.")
        rejected = row.get("rejectedTmdbIds", [])
        if (not isinstance(rejected, list) or len(rejected) > 10
                or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,8}", value) for value in rejected)
                or (row["rejectIdentity"] and not rejected)):
            raise ValueError("Invalid reviewed VOD rejected identities.")
        for field, value in metadata.items():
            valid = False
            if field == "tmdbId":
                valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,8}", value) and value not in rejected
            elif field == "originalTitle":
                valid = _bounded_text(value) and "://" not in value
            elif field == "originalLanguage":
                valid = isinstance(value, str) and re.fullmatch(r"[a-z]{2,3}", value)
            elif field == "releaseDate":
                try:
                    valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
                except ValueError:
                    pass
            elif field in {"genres", "directors"}:
                valid = isinstance(value, list) and len(value) <= 20 and all(_bounded_text(entry, 100) for entry in value)
            if not valid:
                raise ValueError("Invalid reviewed VOD correction field value.")
        if row.get("year") and metadata.get("releaseDate") and row["year"] != metadata["releaseDate"][:4]:
            raise ValueError("Contradictory reviewed VOD correction year.")
        key = row["serverId"], row["kind"], row["recordId"]
        if key in result:
            raise ValueError("Duplicate reviewed VOD correction scope.")
        result[key] = row
    return result


def _signature(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text
                            if not unicodedata.combining(c)).split())


@lru_cache(maxsize=1)
def _rules() -> dict[tuple[str, str, str], dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "data" / "vod_reviewed_corrections.json"
    if path.stat().st_size > MAX_CORRECTION_BYTES:
        raise ValueError("Reviewed VOD correction document is too large.")
    document = json.loads(path.read_text(encoding="utf-8"))
    return _validate_document(document)


def correction_for(server_id: str, kind: str, item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a copy of a reviewed correction only for its exact record/title."""
    record_id = str(item.get("id") or item.get("providerStreamId") or item.get("providerSeriesId") or "")
    rule = _rules().get((server_id, kind, record_id))
    if rule is None:
        return None
    titles = rule["displayNames"] + ([rule["title"]] if rule.get("title") else [])
    if _signature(item.get("title") or item.get("name")) not in {_signature(title) for title in titles}:
        return None
    # Recycled provider IDs and unrelated works must never inherit a correction.
    return copy.deepcopy({key: rule[key] for key in
                          ("rejectIdentity", "rejectedTmdbIds", "metadata", "evidenceURLs", "title", "year", "searchTitle") if key in rule})


def apply_reviewed_correction(server_id: str, kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """Correct a normalized row without adding internal/public schema fields."""
    result = copy.deepcopy(dict(item))
    correction = correction_for(server_id, kind, item)
    if correction is None:
        return result
    if correction["rejectIdentity"]:
        if result.get("adult") is False:
            result["adult"] = None
        for key in ("tmdbId", "releaseDate", "overview", "originalTitle", "originalLanguage",
                    "rating", "voteCount", "popularity", "runtimeMinutes", "trailerUrl",
                    "seasonCount", "episodeCount", "status", "lastAirDate", "nextEpisode"):
            if key in result:
                result[key] = None
        for key in ("cast", "directors", "genres"):
            if key in result:
                result[key] = []
        if "genre" in result:
            result["genre"] = "unknown"
        if "artwork" in result:
            result["artwork"] = {"posterUrl": None, "backdropUrl": None}
    for key, value in correction["metadata"].items():
        if key in result:
            result[key] = copy.deepcopy(value)
    if correction.get("title"):
        result["title"] = correction["title"]
    # A reviewed year can disprove a provider date without establishing a day.
    # Keep the unknown date empty rather than carrying a contradictory value.
    if (correction.get("year") and result.get("releaseDate")
            and str(result["releaseDate"])[:4] != correction["year"]
            and not correction["metadata"].get("releaseDate")):
        result["releaseDate"] = None
    if correction["metadata"].get("genres") and "genre" in result:
        result["genre"] = correction["metadata"]["genres"][0]
    return result
