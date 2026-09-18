#!/usr/bin/env python3
"""Build SKY TV XMLTV, app EPG, and personalization metadata safely.

The production workflow gives this builder an ephemeral CSV snapshot read from
the private Google Sheet, then it parses the EPGShare ALL_SOURCES1 XML exactly
once. Selected programme rows are spooled to
SQLite, so memory usage is bounded by the mapping and one output schedule rather
than by the 1.9+ GiB expanded source document.

The existing SKY TV app EPG remains schema v1.  Personalization data is emitted
as a separate ``server_X_metadata.json.gz`` companion so already-deployed app
parsers cannot be broken by the new metadata fields.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import xml.parsers.expat as expat
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from lxml import etree

if __package__:
    from .epg_selection_spool import SpoolError, copy_and_open_verified_spool
else:
    from epg_selection_spool import SpoolError, copy_and_open_verified_spool


PIPELINE_VERSION = "1.0"
PIPELINE_BUILD_ID = "SKYTV-EPG-V1-2026-09-15"
APP_SCHEMA_VERSION = 1
METADATA_SCHEMA_VERSION = 1
LOGO_POLICY = "SHEET_OR_EXACT_CONFIG_OR_EPGSHARE_SOURCE"
MAPPING_SNAPSHOT_MANIFEST_SCHEMA = "skytv-private-mapping-snapshot-v1"
MAX_MAPPING_SNAPSHOT_MANIFEST_BYTES = 256 * 1024
EFFECTIVE_QUARANTINE_REASON = (
    "Effective snapshot quarantine: OPEN Sync Alerts stream-ID reuse "
    "review; the Google Sheet Mappings row was not changed."
)
DEFAULT_ALL_SOURCE_URL = (
    "https://epgshare01.online/epgshare01/"
    "epg_ripper_ALL_SOURCES1.xml.gz"
)
DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
REJECTED_ACTIONS = frozenset(
    {
        "REVIEW",
        "UNMATCHED",
        "NO_EPG",
        "UNRESOLVED",
        "SKIP",
        "IGNORE",
        "REJECTED",
    }
)
ACTIVE_ACTIONS = frozenset(
    {"KEEP_PANEL", "AUTO_EPGSHARE", "AUTO_DUMMY", "MANUAL", "APPROVED"}
)
ALLOWED_ACTIONS = ACTIVE_ACTIONS | REJECTED_ACTIONS
TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on", "enabled"})
FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", "disabled"})
MAX_MAPPING_BYTES = 50 * 1024 * 1024
MAX_MAPPING_ROWS = 250_000
MAX_SOURCE_COMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_SOURCE_ELEMENTS = 20_000_000
MAX_RECORD_CHILD_ELEMENTS = 10_000
MAX_CATEGORY_VALUES = 64
MAX_XML_PROLOG_BYTES = 2 * 1024 * 1024
SQLITE_BATCH_SIZE = 2_000
PROGRESS_EVERY_ELEMENTS = 250_000
# Per-stream schedules are intentionally day-sized by default. At production
# scale (roughly thirteen thousand placeholder streams), shorter repeated
# blocks add hundreds of thousands of rows without adding real programme facts.
DEFAULT_SYNTHETIC_BLOCK_HOURS = 24
DEFAULT_SYNTHETIC_EVENT_WINDOW_HOURS = 6
DEFAULT_SYNTHETIC_FUTURE_DAYS = 7
MAX_SYNTHETIC_PROGRAMME_ROWS = 2_000_000

SHEET_COLUMNS = (
    "server_id",
    "server_label",
    "region_code",
    "genre",
    "primary_language",
    "stream_id",
    "enabled",
    "channel_name",
    "canonical_name",
    "category_id",
    "category_name",
    "channel_number",
    "country_codes",
    "language_codes",
    "subgenres",
    "sport_codes",
    "religion_codes",
    "audience_codes",
    "content_rating",
    "channel_role",
    "tags",
    "sort_priority",
    "action",
    "source",
    "epg_feed",
    "epg_id",
    "logo_url",
    "metadata_status",
    "metadata_source",
    "metadata_confidence",
    "metadata_locked",
    "reason",
    "notes",
)

REGIONS = frozenset(
    {
        "global",
        "north_america",
        "latin_america",
        "caribbean",
        "europe",
        "middle_east",
        "africa",
        "south_asia",
        "southeast_asia",
        "east_asia",
        "oceania",
        "unknown",
    }
)
GENRES = frozenset(
    {
        "sports",
        "religion",
        "news",
        "entertainment",
        "movies",
        "kids",
        "documentary",
        "music",
        "lifestyle",
        "education",
        "shopping",
        "general",
        "events",
        "adult",
        "unknown",
    }
)
SPORTS = frozenset(
    {
        "cricket",
        "soccer",
        "american_football",
        "canadian_football",
        "rugby_union",
        "rugby_league",
        "field_hockey",
        "ice_hockey",
        "basketball",
        "baseball",
        "tennis",
        "golf",
        "motorsport",
        "boxing",
        "mma",
        "wrestling",
        "kabaddi",
        "badminton",
        "athletics",
        "cycling",
        "multi_sport",
        "other_sport",
        "unknown",
    }
)
RELIGIONS = frozenset(
    {
        "sikhism",
        "hinduism",
        "islam",
        "christianity",
        "buddhism",
        "jainism",
        "judaism",
        "multi_faith",
        "general_spirituality",
        "unknown",
    }
)
AUDIENCES = frozenset({"general", "family", "kids", "teens", "adults"})
CONTENT_RATINGS = frozenset(
    {"general", "parental_guidance", "mature", "adult", "unknown"}
)
CHANNEL_ROLES = frozenset(
    {"linear", "event", "ppv", "timeshift", "radio", "virtual", "unknown"}
)
METADATA_STATUSES = frozenset({"approved", "auto", "review", "unknown"})
METADATA_SOURCES = frozenset(
    {
        "manual",
        "registry",
        "epgshare",
        "provider_category",
        "channel_name",
        "default",
    }
)
TOKEN_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
SERVER_RE = re.compile(r"^server_(\d+)$")
INVALID_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)


class BuildError(RuntimeError):
    """A safe production error that never includes credentials."""


class _XmlRootReached(RuntimeError):
    """Private control-flow signal used to stop the bounded prolog parser."""


@dataclass
class ChannelMetadata:
    region: str
    countries: list[str]
    primary_language: str
    languages: list[str]
    genre: str
    subgenres: list[str]
    sports: list[str]
    religions: list[str]
    audiences: list[str]
    content_rating: str
    channel_role: str
    tags: list[str]
    status: str
    source: str
    confidence: float
    locked: bool


@dataclass
class MappingRow:
    row_number: int
    server_id: str
    server_label: str
    stream_id: str
    enabled: bool
    channel_name: str
    canonical_name: str
    category_id: str
    category_name: str
    channel_number: str
    action: str
    requested_source: str
    effective_source: str
    epg_feed: str
    epg_id: str
    logo_url: str
    sort_priority: int
    reason: str
    notes: str
    metadata: ChannelMetadata

    @property
    def source_key(self) -> str:
        if self.effective_source == "panel":
            return f"panel:{self.server_id}"
        if self.requested_source == "dummy":
            # A generic EPGShare dummy ID is shared by thousands of unrelated
            # channels.  Give every mapped stream its own deterministic source
            # namespace so a useful channel-derived title cannot bleed into a
            # different stream which happens to use the same placeholder ID.
            return f"synthetic:{self.synthetic_identity}"
        return "epgshare01"

    @property
    def synthetic_identity(self) -> str:
        identity = f"{self.server_id}\0{self.stream_id}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    @property
    def schedule_key(self) -> str:
        if self.requested_source == "dummy":
            return f"DUMMY_CHANNELS::{self.synthetic_identity}"
        elif self.effective_source == "panel" or self.source_policy_blocked:
            prefix = "PANEL"
        else:
            prefix = self.epg_feed or "ALL_SOURCES1"
        return f"{prefix}::{self.epg_id}"

    @property
    def source_policy_blocked(self) -> bool:
        """Keep a native panel ID out of Server 1's EPGShare namespace.

        Native provider IDs are not interchangeable with EPGShare IDs.  Even
        when the same spelling happens to exist in both catalogs, treating it
        as an automatic match can attach an unrelated schedule.  A reviewer
        enables the row by selecting an EPGShare ID and changing its source.
        """
        return self.server_id == "server_1" and self.requested_source == "panel"

    @property
    def runtime_eligible(self) -> bool:
        return (
            self.enabled
            and self.action.upper() not in REJECTED_ACTIONS
            and bool(self.epg_id)
            and not self.source_policy_blocked
        )


@dataclass
class SourceStats:
    source_key: str
    compressed_bytes: int = 0
    expanded_bytes: int = 0
    total_elements: int = 0
    channel_elements: int = 0
    unique_channel_ids: int = 0
    duplicate_channel_ids: int = 0
    programme_elements: int = 0
    selected_channels: int = 0
    selected_programmes: int = 0
    duplicate_programmes: int = 0
    invalid_start: int = 0
    synthesized_stop: int = 0
    blank_title: int = 0
    outside_window: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "sourceKey": self.source_key,
            "compressedBytes": self.compressed_bytes,
            "expandedBytes": self.expanded_bytes,
            "totalElements": self.total_elements,
            "channelElements": self.channel_elements,
            "uniqueChannelIds": self.unique_channel_ids,
            "duplicateChannelIds": self.duplicate_channel_ids,
            "programmeElements": self.programme_elements,
            "selectedChannels": self.selected_channels,
            "selectedProgrammes": self.selected_programmes,
            "duplicateProgrammes": self.duplicate_programmes,
            "invalidStartRows": self.invalid_start,
            "synthesizedStopRows": self.synthesized_stop,
            "blankTitleRows": self.blank_title,
            "outsideWindowRows": self.outside_window,
        }


class LimitedReader:
    """Count expanded bytes and stop unexpected decompression growth."""

    def __init__(
        self, raw: BinaryIO, maximum: int, *, close_raw: bool = True
    ) -> None:
        self.raw = raw
        self.maximum = int(maximum)
        self.count = 0
        self.close_raw = bool(close_raw)

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.count += len(data)
        if self.count > self.maximum:
            raise BuildError(
                f"Expanded XML exceeded the configured {self.maximum:,}-byte limit."
            )
        return data

    def close(self) -> None:
        if self.close_raw:
            self.raw.close()

    def __enter__(self) -> "LimitedReader":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def utc_iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def clean_text(value: object, maximum: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return INVALID_XML_RE.sub("", text)[:maximum]


def clean_identifier(value: object, maximum: int = 300) -> str:
    """Clean an opaque XMLTV identifier without changing internal spacing.

    Some real provider IDs contain consecutive spaces. Those spaces are part
    of the identifier, so the display-text normalizer must not be used here.
    XML attribute whitespace is normalized one character at a time, matching
    XML parsing rules, while consecutive normal spaces remain consecutive.
    """
    text = str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return INVALID_XML_RE.sub("", text).strip()[:maximum]


def unescape_spreadsheet_text(value: object) -> str:
    """Undo the standard spreadsheet literal-text escape when reading CSV."""
    text = str(value or "")
    if len(text) >= 2 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text


def spreadsheet_safe_cell(value: object) -> object:
    """Prevent spreadsheet software from executing report/seed text as code."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_open_file(handle: BinaryIO) -> str:
    """Hash one already-open regular file without changing its position.

    Callers which must bind provenance to later reads should keep ``handle``
    open and pass that same handle to :func:`open_limited_xml_handle`.  Hashing
    a path and opening the path again leaves an atomic-replacement race between
    those two operations.
    """
    position = handle.tell()
    digest = hashlib.sha256()
    try:
        handle.seek(0)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    finally:
        handle.seek(position)
    return digest.hexdigest()


def stream_sort_key(value: object) -> tuple[object, ...]:
    text = str(value or "").strip()
    try:
        return (0, int(text), text)
    except (TypeError, ValueError):
        return (1, text.casefold(), text)


def parse_bool(value: object, *, default: bool, field_name: str) -> bool:
    text = str(value or "").strip().casefold()
    if not text:
        return default
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise BuildError(f"{field_name} must be TRUE or FALSE, not {value!r}.")


def normalize_server_id(value: object) -> str:
    text = clean_text(value, 40).casefold().replace("-", "_")
    text = re.sub(r"\s+", "_", text)
    match = re.fullmatch(r"(?:server_?)?(\d+)", text)
    if match:
        return f"server_{int(match.group(1))}"
    if SERVER_RE.fullmatch(text):
        return text
    raise BuildError(f"Invalid server_id {value!r}; use Server 1 or server_1 form.")


def split_values(value: object, *, lowercase: bool = True) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in str(value or "").split("|"):
        item = clean_text(item, 80)
        if lowercase:
            item = item.casefold().replace(" ", "_").replace("-", "_")
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            values.append(item)
    return values


def validate_token_list(
    values: list[str], allowed: frozenset[str] | None, field_name: str
) -> None:
    invalid = [value for value in values if not TOKEN_RE.fullmatch(value)]
    if allowed is not None:
        invalid.extend(value for value in values if value not in allowed)
    if invalid:
        raise BuildError(
            f"Invalid {field_name} value(s): {', '.join(sorted(set(invalid)))}."
        )
    if "unknown" in values and len(values) > 1:
        raise BuildError(f"{field_name}: unknown cannot coexist with a specific value.")


def infer_region_country(
    text: str, epg_id: str, epg_feed: str = ""
) -> tuple[str, list[str], float]:
    folded = text.casefold()
    structured_india = re.search(
        r"(?m)^\s*in(?:\s*[|:/]|\s+-\s+)", folded
    ) or re.search(
        r"(?m)^\s*IN-(?:PUNJ|HIN|ENG|TAM|TEL|MAL|KAN|MAR|GUJ|BENG|URD|SPORTS?)\b",
        text,
    )
    if structured_india:
        return "south_asia", ["IN"], 0.78
    rules = (
        (r"\b(?:pakistan|pakistani)\b", "south_asia", "PK"),
        (r"\b(?:india|indian)\b", "south_asia", "IN"),
        (r"\b(?:bangladesh|bangladeshi)\b", "south_asia", "BD"),
        (r"\b(?:sri lanka|sri lankan)\b", "south_asia", "LK"),
        (r"\b(?:nepal|nepali)\b", "south_asia", "NP"),
        (r"\bcanada|canadian\b", "north_america", "CA"),
        (r"\b(?:usa|u\.s\.|united states)\b", "north_america", "US"),
        (r"\b(?:uk|british|united kingdom)\b", "europe", "GB"),
        (r"\b(?:australia|australian)\b", "oceania", "AU"),
        (r"\b(?:new zealand)\b", "oceania", "NZ"),
        (r"\b(?:uae|united arab emirates|emirati)\b", "middle_east", "AE"),
        (r"\bmiddle east(?:ern)?\b", "middle_east", ""),
        (r"\bcaribbean\b", "caribbean", ""),
        (r"\b(?:latin america|latino)\b", "latin_america", ""),
        (r"\bafrica(?:n)?\b", "africa", ""),
        (r"\bsouth asia(?:n)?\b", "south_asia", ""),
        (r"\bsoutheast asia(?:n)?\b", "southeast_asia", ""),
        (r"\beast asia(?:n)?\b", "east_asia", ""),
        (r"\boceania\b", "oceania", ""),
        (r"\beurope(?:an)?\b", "europe", ""),
    )
    for pattern, region, country in rules:
        if re.search(pattern, folded, re.I):
            return region, [country] if country else [], 0.78
    suffix = epg_id.casefold().rsplit(".", 1)[-1]
    country_by_suffix = {
        "in": ("south_asia", "IN"),
        "pk": ("south_asia", "PK"),
        "bd": ("south_asia", "BD"),
        "lk": ("south_asia", "LK"),
        "np": ("south_asia", "NP"),
        "ca": ("north_america", "CA"),
        "ca2": ("north_america", "CA"),
        "us": ("north_america", "US"),
        "us2": ("north_america", "US"),
        "us_locals1": ("north_america", "US"),
        "uk": ("europe", "GB"),
        "gb": ("europe", "GB"),
        "ae": ("middle_east", "AE"),
        "au": ("oceania", "AU"),
        "nz": ("oceania", "NZ"),
    }
    if suffix in country_by_suffix:
        region, country = country_by_suffix[suffix]
        return region, [country], 0.88
    feed_key = clean_text(epg_feed, 80).upper()
    feed_defaults = {
        "CA2": ("north_america", "CA"),
        "IN1": ("south_asia", "IN"),
        "IN2": ("south_asia", "IN"),
        "IN4": ("south_asia", "IN"),
        "UK1": ("europe", "GB"),
        "US2": ("north_america", "US"),
        "US_LOCALS1": ("north_america", "US"),
        "US_SPORTS1": ("north_america", "US"),
        "FANDUEL1": ("north_america", "US"),
    }
    if feed_key in feed_defaults:
        region, country = feed_defaults[feed_key]
        return region, [country], 0.80
    return "unknown", [], 0.45


LANGUAGE_RULES: tuple[tuple[str, str], ...] = (
    (r"\b(?:punjabi|panjabi|punjab|punj|pjb|gurbani|kirtan)\b|(?:^|[\s|:/-])pb(?:[\s|:/-]|$)", "pa"),
    (r"\bhindi\b|(?:^|[\s|:/-])hin(?:[\s|:/-]|$)", "hi"),
    (r"\burdu\b", "ur"),
    (r"\btamil\b", "ta"),
    (r"\btelugu\b", "te"),
    (r"\b(?:bengali|bangla)\b", "bn"),
    (r"\bgujarati\b|(?:^|[\s|:/-])gjr(?:[\s|:/-]|$)", "gu"),
    (r"\bmarathi\b", "mr"),
    (r"\bmalayalam\b", "ml"),
    (r"\bkannada\b", "kn"),
    (r"\bbhojpuri\b", "bho"),
    (r"\b(?:odia|oriya)\b", "or"),
    (r"\bassamese\b", "as"),
    (r"\bnepali\b", "ne"),
    (r"\b(?:sinhala|sinhalese)\b", "si"),
    (r"\benglish\b|(?:^|[\s|:/-])eng(?:[\s|:/-]|$)", "en"),
    (r"\bfrench\b", "fr"),
    (r"\bspanish\b", "es"),
    (r"\barabic\b", "ar"),
    (r"\b(?:mandarin|cantonese|chinese)\b", "zh"),
    (r"\bjapanese\b", "ja"),
    (r"\bkorean\b", "ko"),
    (r"\bportuguese\b", "pt"),
    (r"\bgerman\b", "de"),
    (r"\bitalian\b", "it"),
)


def infer_language(text: str) -> tuple[str, list[str], float]:
    found: list[str] = []
    for pattern, code in LANGUAGE_RULES:
        if re.search(pattern, text, re.I) and code not in found:
            found.append(code)
    if not found:
        return "und", ["und"], 0.45
    return (found[0] if len(found) == 1 else "mul"), found, 0.82


# Deliberately narrow parental-safety skeleton.  It is not used for channel
# identity or general metadata normalization.  Keys are lowercase because the
# security normalizer casefolds before translating, covering capitals too.
_ADULT_CONFUSABLE_SKELETON = str.maketrans(
    {
        "α": "a",  # Greek alpha
        "а": "a",  # Cyrillic a (for example: Аdult)
        "ϲ": "c",  # Greek lunate sigma
        "σ": "c",  # NFKC/casefold result of Greek lunate sigma
        "с": "c",  # Cyrillic es (for example: erotiс)
        "ԁ": "d",  # Cyrillic Komi de (for example: Aԁult)
        "ε": "e",  # Greek epsilon
        "е": "e",  # Cyrillic ie (for example: еrotic)
        "ι": "i",  # Greek iota
        "і": "i",  # Ukrainian i (for example: erotіc)
        "ӏ": "l",  # Cyrillic palochka (for example: aduӏt)
        "ο": "o",  # Greek omicron (for example: Pοrn)
        "о": "o",  # Cyrillic o (for example: Pоrn)
        "ρ": "p",  # Greek rho
        "р": "p",  # Cyrillic er (for example: рorn)
        "ѕ": "s",  # Cyrillic dze
        "τ": "t",  # Greek tau
        "т": "t",  # Cyrillic te
        "υ": "u",  # Greek upsilon (for example: adυlt)
        "χ": "x",  # Greek chi (for example: ΧΧΧ)
        "х": "x",  # Cyrillic ha (for example: ххх)
        "×": "x",  # multiplication sign (for example: ×××)
        "у": "y",  # Cyrillic u (for example: plaуboy)
    }
)


def _security_normalize_text(value: object) -> str:
    """Fold Unicode disguises before applying parental-safety rules."""

    normalized = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKC", str(value or ""))
    )
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if unicodedata.combining(character) or category in {"Cf", "Cs"}:
            continue
        if category == "Cc":
            # Newlines delimit the independently supplied evidence fields.
            # Preserve that boundary while dropping all other controls.
            if character.isspace():
                characters.append(" ")
            continue
        characters.append(character)
    folded = "".join(characters).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def infer_genre(text: str) -> tuple[str, list[str], float]:
    folded = _security_normalize_text(text)
    adult_folded = folded.translate(_ADULT_CONFUSABLE_SKELETON)
    # Adult content must win before movie/lifestyle keywords so parental
    # exclusion cannot be bypassed by a label such as "PORNO MOVIES". Adult
    # Swim and the "adult contemporary" music format are explicit non-adult
    # meanings of the word.
    strong_adult_pattern = (
        r"\b(?:for\s+adults?|xxx|playboy|brazzers|"
        r"porn(?:o|hub|ography|ographic)?|erotic(?:a|ism)?|onlyfans|hustler|"
        r"red\s*light|sextreme)\b|"
        r"(?:^|\W)18\s*\+(?:\W|$)"
    )
    safe_adult_phrases = (
        r"\badult[\s._-]+(?:swim|contemporary|alternative|education)\b|"
        r"\b(?:pop|music)[\s._-]+adult\b"
    )
    bare_adult_evidence = re.sub(
        safe_adult_phrases, " ", adult_folded, flags=re.I
    )
    if re.search(strong_adult_pattern, adult_folded, re.I) or re.search(
        r"\badults?\b", bare_adult_evidence, re.I
    ):
        return "adult", [], 0.97
    rules: tuple[tuple[str, str, list[str], float], ...] = (
        (r"\b(?:religion|religious|faith|gurbani|gurkirpa|gurkibani|kirtan|simran|khalsa|khalsaal|sikh|sikhism|guru|gurdwara|gurudwara|bhakti|aastha|astha|amritwani|shraddha|krishna|krsna|sadhna|satsang|sanskar|vedic|hindu|hinduism|islam|islamic|muslim|quran|masjid|iqra|ahlebait|madani|hidayat|takbeer|christian|christianity|catholic|church|bible|gospel|worship|jinvani|aadinath)\b|\b(?:tbn|3abn)\b", "religion", ["worship"], 0.88),
        (r"\b(?:sports?|sportowe|desporto|eurosport|fightbox|sporting tv|cricket|cric|willow|football|soccer|nfl|nba|nhl|mlb|tennis|golf|racing|boxing|mma|wrestling|kabaddi|badminton|espn|dazn)\b", "sports", ["linear_sports"], 0.86),
        (r"\b(?:news|current affairs|weather|cnn|msnbc|newsmax)\b", "news", [], 0.86),
        (r"\b(?:kids?|children|cartoon|nickelodeon|disney junior|baby)\b", "kids", [], 0.86),
        (r"\b(?:movies?|cinema|film|bollywood|hollywood|cineplex)\b", "movies", [], 0.84),
        (r"\b(?:documentary|documentaries|history|science|nature|discovery|nat geo)\b", "documentary", [], 0.82),
        (r"\b(?:music|radio|mtv|stingray|songs?|singers?)\b", "music", [], 0.78),
        (r"\b(?:shopping|shop|qvc)\b", "shopping", [], 0.80),
        (r"\b(?:education|educational|learning)\b", "education", [], 0.80),
        (r"\b(?:lifestyle|food|travel|cooking|fashion|home|hgtv)\b", "lifestyle", [], 0.74),
        (r"\b(?:events?|ppv|pay per view)\b", "events", [], 0.72),
        (r"\b(?:entertainment|drama|comedy|series|general)\b", "entertainment", ["general_entertainment"], 0.70),
    )
    for pattern, genre, subgenres, confidence in rules:
        if re.search(pattern, folded, re.I):
            return genre, subgenres, confidence
    return "unknown", [], 0.45


def infer_sports(text: str, genre: str) -> list[str]:
    if genre != "sports":
        return []
    folded = text.casefold()
    rules = (
        (r"\b(?:cricket|willow|ipl|psl|bbl|wpl|t20|odi|the ashes|cric)\b", "cricket"),
        (r"\b(?:soccer|football|epl|uefa|fifa|la liga|bundesliga)\b", "soccer"),
        (r"\b(?:nfl|american football)\b", "american_football"),
        (r"\b(?:nhl|ice hockey)\b", "ice_hockey"),
        (r"\b(?:nba|basketball)\b", "basketball"),
        (r"\b(?:mlb|baseball)\b", "baseball"),
        (r"\btennis\b", "tennis"),
        (r"\bgolf\b", "golf"),
        (r"\b(?:formula ?1|f1|motogp|racing|motorsport)\b", "motorsport"),
        (r"\bboxing\b", "boxing"),
        (r"\b(?:mma|ufc)\b", "mma"),
        (r"\bwrestling\b", "wrestling"),
        (r"\bkabaddi\b", "kabaddi"),
        (r"\bbadminton\b", "badminton"),
    )
    found = [code for pattern, code in rules if re.search(pattern, folded, re.I)]
    return list(dict.fromkeys(found)) or ["multi_sport"]


def infer_religions(text: str, genre: str) -> list[str]:
    if genre != "religion":
        return []
    folded = text.casefold()
    rules = (
        (r"\b(?:gurbani|gurkirpa|gurkibani|kirtan|simran|khalsa|khalsaal|sikh|guru|nanak|gurdwara|gurudwara)\b", "sikhism"),
        (r"\b(?:bhakti|aastha|astha|amritwani|shraddha|krishna|krsna|sadhna|satsang|sanskar|vedic|hindu|mandir|temple)\b", "hinduism"),
        (r"\b(?:islam|islamic|muslim|quran|masjid|makkah|madinah|madina|iqra|ahlebait|madani|hidayat|takbeer)\b", "islam"),
        (r"\b(?:christian|catholic|church|bible|gospel|jesus|tbn|3abn|revelation)\b", "christianity"),
        (r"\b(?:buddha|buddhist|buddhism)\b", "buddhism"),
        (r"\b(?:jain|jainism|jinvani|aadinath)\b", "jainism"),
        (r"\b(?:jewish|judaism|torah)\b", "judaism"),
    )
    found = [code for pattern, code in rules if re.search(pattern, folded, re.I)]
    return list(dict.fromkeys(found)) or ["general_spirituality"]


def infer_role(text: str) -> str:
    folded = text.casefold()
    if re.search(r"\b(?:radio|fm|am)\b", folded):
        return "radio"
    if re.search(r"\b(?:ppv|pay per view)\b", folded):
        return "ppv"
    if re.search(r"\b(?:event|stream|feed|slot)\s*\d+\b|\bvs\.?\b", folded):
        return "event"
    if re.search(r"\b(?:\+1|plus one|timeshift)\b", folded):
        return "timeshift"
    if re.search(r"\b24\s*[/x.-]\s*7\b", folded):
        return "virtual"
    return "linear"


def build_metadata(raw: Mapping[str, str], row_number: int) -> ChannelMetadata:
    locked = parse_bool(
        raw.get("metadata_locked", ""), default=False,
        field_name=f"row {row_number} metadata_locked",
    )
    dummy_mapping = (
        clean_text(raw.get("source", ""), 40).casefold() == "dummy"
        or clean_text(raw.get("epg_feed", ""), 80).casefold()
        == "dummy_channels"
    )
    evidence_fields = ["category_name", "channel_name", "canonical_name"]
    base_evidence = "\n".join(
        clean_text(raw.get(name, ""), 300)
        for name in evidence_fields
    )
    epg_id_evidence = clean_text(raw.get("epg_id", ""), 300)
    classification_evidence = "\n".join(
        value for value in (base_evidence, epg_id_evidence) if value
    )
    identity_evidence = "\n".join(
        clean_text(raw.get(name, ""), 300)
        for name in ("channel_name", "canonical_name", "epg_id")
    )
    location_evidence = base_evidence if dummy_mapping else classification_evidence
    explicit_fields = any(
        clean_text(raw.get(name, ""), 200)
        for name in (
            "region_code", "genre", "primary_language", "country_codes",
            "language_codes", "subgenres", "sport_codes", "religion_codes",
            "audience_codes", "content_rating", "channel_role", "tags",
        )
    )

    inferred_region, inferred_countries, region_confidence = infer_region_country(
        location_evidence,
        "" if dummy_mapping else clean_text(raw.get("epg_id", ""), 240),
        "" if dummy_mapping else clean_text(raw.get("epg_feed", ""), 80),
    )
    inferred_primary, inferred_languages, language_confidence = infer_language(
        classification_evidence
    )
    inferred_genre, inferred_subgenres, genre_confidence = infer_genre(
        classification_evidence
    )

    region = clean_text(raw.get("region_code", ""), 40).casefold() or (
        "unknown" if locked else inferred_region
    )
    genre = clean_text(raw.get("genre", ""), 40).casefold() or (
        "unknown" if locked else inferred_genre
    )
    if inferred_genre == "adult":
        # Strong adult evidence is a parental-safety boundary and cannot be
        # weakened by a contradictory Sheet genre cell.
        genre = "adult"
    countries = split_values(raw.get("country_codes", ""), lowercase=False)
    countries = [value.upper() for value in countries]
    if not countries and not locked:
        countries = inferred_countries
    languages = split_values(raw.get("language_codes", ""), lowercase=False)
    if not languages and not locked:
        languages = inferred_languages
    primary_language = clean_text(raw.get("primary_language", ""), 24) or (
        "und" if locked else inferred_primary
    )
    subgenres = split_values(raw.get("subgenres", ""))
    if not subgenres and not locked:
        subgenres = inferred_subgenres
    sports = split_values(raw.get("sport_codes", ""))
    if not sports and not locked:
        sports = infer_sports(identity_evidence, genre)
        if sports == ["multi_sport"]:
            sports = infer_sports(classification_evidence, genre)
    religions = split_values(raw.get("religion_codes", ""))
    if not religions and not locked:
        religions = infer_religions(identity_evidence, genre)
        if religions == ["general_spirituality"]:
            religions = infer_religions(classification_evidence, genre)
    audiences = split_values(raw.get("audience_codes", ""))
    if genre == "adult":
        audiences = ["adults"]
    elif not audiences:
        audiences = ["kids"] if genre == "kids" else ["general"]
    content_rating = clean_text(raw.get("content_rating", ""), 40).casefold()
    if genre == "adult":
        # Parental exclusion is a safety boundary, not an editable suggestion.
        content_rating = "adult"
    elif not content_rating:
        content_rating = "general"
    channel_role = clean_text(raw.get("channel_role", ""), 40).casefold()
    if not channel_role:
        channel_role = "unknown" if locked else infer_role(classification_evidence)
    tags = split_values(raw.get("tags", ""))

    if region not in REGIONS:
        raise BuildError(f"Row {row_number}: invalid region_code {region!r}.")
    if genre not in GENRES:
        raise BuildError(f"Row {row_number}: invalid genre {genre!r}.")
    if any(not re.fullmatch(r"[A-Z]{2}", value) for value in countries):
        raise BuildError(f"Row {row_number}: country_codes must be ISO alpha-2 codes.")
    if any(not LANGUAGE_RE.fullmatch(value) for value in languages):
        raise BuildError(f"Row {row_number}: invalid BCP-47 language_codes value.")
    if not LANGUAGE_RE.fullmatch(primary_language) and primary_language not in {"und", "mul"}:
        raise BuildError(f"Row {row_number}: invalid primary_language.")
    if primary_language not in {"und", "mul"} and primary_language not in languages:
        raise BuildError(
            f"Row {row_number}: primary_language must also occur in language_codes."
        )
    validate_token_list(subgenres, None, f"row {row_number} subgenres")
    validate_token_list(sports, SPORTS, f"row {row_number} sport_codes")
    validate_token_list(religions, RELIGIONS, f"row {row_number} religion_codes")
    validate_token_list(audiences, AUDIENCES, f"row {row_number} audience_codes")
    validate_token_list(tags, None, f"row {row_number} tags")
    if "multi_sport" in sports and len(sports) > 1:
        raise BuildError(f"Row {row_number}: multi_sport cannot coexist with a specific sport.")
    if "multi_faith" in religions and len(religions) > 1:
        raise BuildError(f"Row {row_number}: multi_faith cannot coexist with a specific faith.")
    if sports and genre != "sports":
        raise BuildError(f"Row {row_number}: sport_codes require genre=sports.")
    if religions and genre != "religion":
        raise BuildError(f"Row {row_number}: religion_codes require genre=religion.")
    if content_rating not in CONTENT_RATINGS:
        raise BuildError(f"Row {row_number}: invalid content_rating.")
    if channel_role not in CHANNEL_ROLES:
        raise BuildError(f"Row {row_number}: invalid channel_role.")

    status = clean_text(raw.get("metadata_status", ""), 30).casefold()
    if not status:
        status = "approved" if locked else ("auto" if not explicit_fields else "review")
    source = clean_text(raw.get("metadata_source", ""), 40).casefold()
    if not source:
        source = (
            "manual"
            if explicit_fields
            else (
                "provider_category"
                if clean_text(raw.get("category_name", ""), 200)
                else "channel_name"
            )
        )
    if status not in METADATA_STATUSES:
        raise BuildError(f"Row {row_number}: invalid metadata_status.")
    if source not in METADATA_SOURCES:
        raise BuildError(f"Row {row_number}: invalid metadata_source.")
    confidence_text = clean_text(raw.get("metadata_confidence", ""), 20)
    if confidence_text:
        try:
            confidence = float(confidence_text)
        except ValueError as exc:
            raise BuildError(f"Row {row_number}: invalid metadata_confidence.") from exc
    elif locked:
        confidence = 1.0
    elif explicit_fields:
        confidence = 0.75
    else:
        known_facet_confidences: list[float] = []
        if region != "unknown":
            known_facet_confidences.append(region_confidence)
        if languages and languages != ["und"]:
            known_facet_confidences.append(language_confidence)
        if genre != "unknown":
            known_facet_confidences.append(genre_confidence)
        confidence = min(known_facet_confidences, default=0.45)
    if not 0.0 <= confidence <= 1.0:
        raise BuildError(f"Row {row_number}: metadata_confidence must be 0..1.")

    return ChannelMetadata(
        region=region,
        countries=countries,
        primary_language=primary_language,
        languages=languages,
        genre=genre,
        subgenres=subgenres,
        sports=sports,
        religions=religions,
        audiences=audiences,
        content_rating=content_rating,
        channel_role=channel_role,
        tags=tags,
        status=status,
        source=source,
        confidence=round(confidence, 3),
        locked=locked,
    )


def normalized_header(value: object) -> str:
    text = clean_text(value, 100).casefold()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    aliases = {
        "provider_category": "category_name",
        "display_name": "canonical_name",
        "selected_epg_id": "epg_id",
        "chosen_epg_id": "epg_id",
        "selected_feed": "epg_feed",
        "chosen_feed": "epg_feed",
        "stream_icon": "logo_url",
        "language": "primary_language",
        "languages": "language_codes",
        "region": "region_code",
        "countries": "country_codes",
        "sports": "sport_codes",
        "religions": "religion_codes",
        "audiences": "audience_codes",
    }
    return aliases.get(text, text)


def normalize_requested_source(
    source: str, epg_feed: str, *, row_number: int
) -> str:
    source_key = clean_text(source, 40).casefold()
    feed_key = clean_text(epg_feed, 80).casefold()
    panel_feed = feed_key in {"panel", "server xmltv.php"}
    dummy_feed = feed_key == "dummy_channels"
    aliases = {
        "epgshare": "epgshare01",
        "epgshare01": "epgshare01",
        "panel": "panel",
        "dummy": "dummy",
    }
    if source_key and source_key not in aliases:
        raise BuildError(
            f"Row {row_number}: invalid source {source!r}; use epgshare01, "
            "panel, or dummy."
        )
    normalized = aliases.get(source_key, "")
    inferred = "panel" if panel_feed else ("dummy" if dummy_feed else "epgshare01")
    if normalized and (
        (panel_feed and normalized != "panel")
        or (dummy_feed and normalized != "dummy")
        or (normalized in {"panel", "dummy"} and feed_key and normalized != inferred)
    ):
        raise BuildError(
            f"Row {row_number}: source and epg_feed describe different source types."
        )
    return normalized or inferred


def parse_mapping_csv(
    content: bytes,
    selected_servers: set[str],
    *,
    require_enabled_servers: bool = True,
) -> list[MappingRow]:
    if len(content) > MAX_MAPPING_BYTES:
        raise BuildError(
            f"Mapping CSV exceeds the configured {MAX_MAPPING_BYTES:,}-byte limit."
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BuildError("Mapping CSV must be UTF-8 encoded.") from exc
    prefix = text.lstrip()[:200].casefold()
    if prefix.startswith("<!doctype html") or prefix.startswith("<html"):
        raise BuildError(
            "Mapping input is HTML instead of CSV. Check the private Sheet sync input."
        )

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        raw_headers = next(reader)
    except StopIteration as exc:
        raise BuildError("Mapping CSV is empty.") from exc
    headers = [normalized_header(value) for value in raw_headers]
    if not headers or any(not header for header in headers):
        raise BuildError("Mapping CSV contains a blank column header.")
    duplicates = sorted(
        header for header, count in Counter(headers).items() if count > 1
    )
    if duplicates:
        raise BuildError(
            "Mapping CSV contains duplicate normalized headers: "
            + ", ".join(duplicates)
        )
    required = {"server_id", "stream_id", "channel_name"}
    missing_headers = sorted(required - set(headers))
    if missing_headers:
        raise BuildError(
            "Mapping CSV is missing required headers: " + ", ".join(missing_headers)
        )

    rows: list[MappingRow] = []
    keys: dict[tuple[str, str], int] = {}
    for row_number, values in enumerate(reader, start=2):
        if row_number > MAX_MAPPING_ROWS + 1:
            raise BuildError(
                f"Mapping CSV exceeds the configured {MAX_MAPPING_ROWS:,}-row limit."
            )
        if not values or not any(str(value).strip() for value in values):
            continue
        if len(values) > len(headers):
            raise BuildError(f"Row {row_number}: more values than headers.")
        values.extend([""] * (len(headers) - len(values)))
        raw = {
            headers[index]: unescape_spreadsheet_text(value)
            for index, value in enumerate(values)
        }

        server_id = normalize_server_id(raw.get("server_id", ""))
        if server_id not in selected_servers:
            continue
        stream_id = clean_identifier(raw.get("stream_id", ""), 120)
        channel_name = clean_identifier(raw.get("channel_name", ""), 300)
        if not stream_id or not channel_name:
            raise BuildError(
                f"Row {row_number}: stream_id and channel_name are required."
            )
        duplicate_key = (server_id, stream_id)
        if duplicate_key in keys:
            raise BuildError(
                f"Rows {keys[duplicate_key]} and {row_number} duplicate "
                f"({server_id}, {stream_id})."
            )
        keys[duplicate_key] = row_number

        action = clean_text(raw.get("action", "APPROVED"), 40).upper() or "APPROVED"
        if action not in ALLOWED_ACTIONS:
            raise BuildError(
                f"Row {row_number}: invalid action {action!r}; use an approved "
                "mapping or review-state action."
            )
        enabled = parse_bool(
            raw.get("enabled", ""),
            default=action not in {"SKIP", "IGNORE", "REJECTED"},
            field_name=f"row {row_number} enabled",
        )
        epg_id = clean_identifier(raw.get("epg_id", ""), 300)
        epg_feed = clean_text(raw.get("epg_feed", ""), 100)
        requested_source = normalize_requested_source(
            raw.get("source", ""), epg_feed, row_number=row_number
        )
        required_source_by_action = {
            "KEEP_PANEL": "panel",
            "AUTO_EPGSHARE": "epgshare01",
            "AUTO_DUMMY": "dummy",
        }
        expected_source = required_source_by_action.get(action)
        if expected_source and requested_source != expected_source:
            raise BuildError(
                f"Row {row_number}: action {action} requires source={expected_source}."
            )
        # This is the non-negotiable Server 1 boundary: its native schedule is
        # never requested or read.  runtime_eligible also quarantines old panel
        # IDs because provider and EPGShare ID namespaces are not equivalent.
        effective_source = (
            "epgshare01"
            if server_id == "server_1" and requested_source == "panel"
            else requested_source
        )
        try:
            sort_priority = int(clean_text(raw.get("sort_priority", ""), 20) or 1000)
        except ValueError as exc:
            raise BuildError(f"Row {row_number}: sort_priority must be an integer.") from exc

        metadata = build_metadata(raw, row_number)
        row = MappingRow(
            row_number=row_number,
            server_id=server_id,
            server_label=clean_text(raw.get("server_label", ""), 80)
            or server_id.replace("_", " ").title(),
            stream_id=stream_id,
            enabled=enabled,
            channel_name=channel_name,
            canonical_name=clean_text(raw.get("canonical_name", ""), 300)
            or channel_name,
            category_id=clean_text(raw.get("category_id", ""), 120),
            category_name=clean_text(raw.get("category_name", ""), 200),
            channel_number=clean_text(raw.get("channel_number", ""), 40),
            action=action,
            requested_source=requested_source,
            effective_source=effective_source,
            epg_feed=epg_feed,
            epg_id=epg_id,
            logo_url=valid_http_url(raw.get("logo_url", "")),
            sort_priority=sort_priority,
            reason=clean_text(raw.get("reason", ""), 500),
            notes=clean_text(raw.get("notes", ""), 500),
            metadata=metadata,
        )
        if row.enabled and action not in REJECTED_ACTIONS and not row.epg_id:
            raise BuildError(
                f"Row {row_number}: enabled {action} row requires epg_id."
            )
        rows.append(row)

    if require_enabled_servers:
        missing_servers = sorted(
            selected_servers - {row.server_id for row in rows if row.enabled}
        )
        if missing_servers:
            raise BuildError(
                "No enabled mapping rows were found for: " + ", ".join(missing_servers)
            )
    return rows


def validate_private_mapping_snapshots(
    authoritative_content: bytes,
    effective_content: bytes,
    selected_servers: set[str],
) -> dict[str, Any]:
    """Prove that the build snapshot is only a quarantined Sheet snapshot.

    The fixed row floors protect the final authoritative Google Sheet read.
    The effective snapshot remains the only input used for publication.  Every
    difference between the two must be the exact, fail-closed OPEN-alert
    quarantine transform; rows may never be added, dropped, reordered, or
    otherwise edited between these private hand-off files.
    """
    authoritative_rows = parse_mapping_csv(
        authoritative_content,
        selected_servers,
        require_enabled_servers=False,
    )
    effective_rows = parse_mapping_csv(
        effective_content,
        selected_servers,
        require_enabled_servers=False,
    )
    authoritative_keys = [
        (row.server_id, row.stream_id) for row in authoritative_rows
    ]
    effective_keys = [(row.server_id, row.stream_id) for row in effective_rows]
    if authoritative_keys != effective_keys:
        raise BuildError(
            "Private mapping snapshot pair has a row identity or order mismatch."
        )

    server_stats: dict[str, dict[str, int]] = {
        server_id: {
            "authoritative_rows": 0,
            "authoritative_runnable_rows": 0,
            "effective_rows": 0,
            "effective_runnable_rows": 0,
            "quarantined_rows": 0,
            "quarantined_authoritative_runnable_rows": 0,
            "quarantined_already_ineligible_rows": 0,
        }
        for server_id in sorted(selected_servers)
    }
    changed_keys: list[list[str]] = []
    for authoritative, effective in zip(
        authoritative_rows, effective_rows, strict=True
    ):
        stats = server_stats[authoritative.server_id]
        stats["authoritative_rows"] += 1
        stats["effective_rows"] += 1
        if authoritative.runtime_eligible:
            stats["authoritative_runnable_rows"] += 1
        if effective.runtime_eligible:
            stats["effective_runnable_rows"] += 1

        if effective == authoritative:
            continue
        expected = replace(
            authoritative,
            enabled=False,
            action="REVIEW",
            reason=EFFECTIVE_QUARANTINE_REASON,
            metadata=replace(authoritative.metadata, status="review"),
        )
        if effective != expected:
            raise BuildError(
                "Private mapping snapshot pair contains a change other than "
                "the exact OPEN-alert quarantine transform at "
                f"{authoritative.server_id} row {authoritative.row_number}."
            )
        if effective.runtime_eligible:
            raise BuildError(
                "An OPEN-alert quarantine row remained runtime eligible."
            )
        stats["quarantined_rows"] += 1
        if authoritative.runtime_eligible:
            stats["quarantined_authoritative_runnable_rows"] += 1
        else:
            stats["quarantined_already_ineligible_rows"] += 1
        changed_keys.append([authoritative.server_id, authoritative.stream_id])

    for server_id, stats in server_stats.items():
        expected_effective = (
            stats["authoritative_runnable_rows"]
            - stats["quarantined_authoritative_runnable_rows"]
        )
        if stats["effective_runnable_rows"] != expected_effective:
            raise BuildError(
                "Private mapping snapshot quarantine accounting failed for "
                f"{server_id}."
            )

    changed_keys.sort(key=lambda item: (item[0], stream_sort_key(item[1])))
    return {
        "authoritative_sha256": hashlib.sha256(authoritative_content).hexdigest(),
        "effective_sha256": hashlib.sha256(effective_content).hexdigest(),
        "quarantine_keys_sha256": hashlib.sha256(
            json_compact(changed_keys).encode("utf-8")
        ).hexdigest(),
        "authoritative_rows": len(authoritative_rows),
        "effective_rows": len(effective_rows),
        "servers": server_stats,
    }


def expected_mapping_snapshot_manifest(
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": MAPPING_SNAPSHOT_MANIFEST_SCHEMA,
        "authoritative_sha256": validation["authoritative_sha256"],
        "effective_sha256": validation["effective_sha256"],
        "quarantine_keys_sha256": validation["quarantine_keys_sha256"],
        "authoritative_rows": validation["authoritative_rows"],
        "effective_rows": validation["effective_rows"],
        "servers": validation["servers"],
    }


def parse_and_validate_mapping_snapshot_manifest(
    content: bytes,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    if len(content) > MAX_MAPPING_SNAPSHOT_MANIFEST_BYTES:
        raise BuildError("Private mapping snapshot manifest is unexpectedly large.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("Private mapping snapshot manifest must be UTF-8 JSON.") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BuildError(
                    "Private mapping snapshot manifest contains duplicate JSON keys."
                )
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise BuildError("Private mapping snapshot manifest is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise BuildError("Private mapping snapshot manifest must be a JSON object.")
    canonical_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    canonical_expected = json.dumps(
        expected_mapping_snapshot_manifest(validation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical_payload != canonical_expected:
        raise BuildError(
            "Private mapping snapshot manifest does not match the authoritative "
            "and effective snapshot files."
        )
    return payload


def canonical_mapping_sha256(rows: Sequence[MappingRow]) -> str:
    stable: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item.server_id, stream_sort_key(item.stream_id))):
        stable.append(
            {
                "server_id": row.server_id,
                "stream_id": row.stream_id,
                "enabled": row.enabled,
                "channel_name": row.channel_name,
                "canonical_name": row.canonical_name,
                "category_id": row.category_id,
                "category_name": row.category_name,
                "channel_number": row.channel_number,
                "action": row.action,
                "requested_source": row.requested_source,
                "effective_source": row.effective_source,
                "epg_feed": row.epg_feed,
                "epg_id": row.epg_id,
                "logo_url": row.logo_url,
                "sort_priority": row.sort_priority,
                "metadata": metadata_dict(row.metadata),
            }
        )
    return hashlib.sha256(json_compact(stable).encode("utf-8")).hexdigest()


def metadata_dict(metadata: ChannelMetadata) -> dict[str, Any]:
    return {
        "region": metadata.region,
        "countries": metadata.countries,
        "primaryLanguage": metadata.primary_language,
        "languages": metadata.languages,
        "genre": metadata.genre,
        "subgenres": metadata.subgenres,
        "sports": metadata.sports,
        "religions": metadata.religions,
        "audiences": metadata.audiences,
        "contentRating": metadata.content_rating,
        "channelRole": metadata.channel_role,
        "tags": metadata.tags,
        "classification": {
            "status": metadata.status,
            "source": metadata.source,
            "confidence": metadata.confidence,
            "locked": metadata.locked,
        },
    }


def valid_http_url(value: object, *, base_url: str = "") -> str:
    text = clean_text(value, 2_000)
    if not text:
        return ""
    if base_url:
        text = urljoin(base_url, text)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    # These URLs are copied into public XML/JSON.  Reject every URL component
    # that can carry private credentials instead of attempting to redact it:
    # redaction can change the resource being referenced and query/fragment
    # values are routinely used for signed or bearer-token URLs.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return text


def public_url_without_query(value: object) -> str:
    """Remove query credentials and fragments from published provenance."""
    parsed = urlparse(str(value or ""))
    return parsed._replace(query="", fragment="").geturl()


def declared_epgshare_file_origin(value: object) -> tuple[str, str]:
    """Validate a caller-declared origin without claiming we fetched it.

    ``--all-source-file`` deliberately bypasses this builder's downloader.  A
    workflow may still tell us where it downloaded that file, but that claim is
    weaker than the final response URL observed by :func:`safe_download`.  Keep
    the accepted declaration narrowly scoped to the same EPGShare HTTPS host
    boundary and return the public URL plus its *declared* host.
    """

    text = str(value or "").strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").casefold()
    allowed = host == "epgshare01.online" or host.endswith(
        ".epgshare01.online"
    )
    if (
        parsed.scheme != "https"
        or not host
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BuildError(
            "--all-source-file-origin-url must be a public EPGShare HTTPS URL "
            "without credentials, a query, or a fragment."
        )
    return parsed.geturl(), host


def safe_download(
    url: str,
    destination: Path,
    *,
    maximum_bytes: int,
    expected_gzip: bool,
    allowed_host_suffixes: Sequence[str],
    attempts: int = 4,
) -> tuple[Path, dict[str, Any]]:
    def approved_url(value: str) -> tuple[str, str]:
        parsed_url = urlparse(str(value).strip())
        host = (parsed_url.hostname or "").casefold()
        allowed = any(
            host == suffix.casefold().lstrip(".")
            or host.endswith("." + suffix.casefold().lstrip("."))
            for suffix in allowed_host_suffixes
        )
        if (
            parsed_url.scheme != "https"
            or not host
            or not allowed
            or parsed_url.username
            or parsed_url.password
        ):
            raise BuildError("Remote input URL uses an unapproved HTTPS host.")
        return str(value), host

    initial_url, _initial_host = approved_url(str(url).strip())
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    session = requests.Session()
    session.headers.update({"User-Agent": f"SKYTV-EPG/{PIPELINE_VERSION}"})
    last_status = "network error"
    try:
        for attempt in range(1, attempts + 1):
            temporary.unlink(missing_ok=True)
            # A failed attempt may have followed one or more redirects. Start
            # every retry from the originally approved URL so a transient
            # redirect target cannot become persistent retry state.
            request_url = initial_url
            try:
                for redirect_number in range(6):
                    response = session.get(
                        request_url,
                        stream=True,
                        timeout=(20, 180),
                        allow_redirects=False,
                    )
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("Location", "").strip()
                    next_url = urljoin(response.url, location)
                    response.close()
                    if not location:
                        raise BuildError("Remote input returned an empty redirect.")
                    request_url, _redirect_host = approved_url(next_url)
                else:
                    raise BuildError("Remote input exceeded the redirect limit.")
                last_status = f"HTTP {response.status_code}"
                if response.status_code in {429, 500, 502, 503, 504}:
                    response.close()
                    raise requests.RequestException(last_status)
                if response.status_code != 200:
                    response.close()
                    raise BuildError(f"Remote input returned {last_status}.")
                final = urlparse(response.url)
                final_host = (final.hostname or "").casefold()
                approved_url(response.url)
                declared = response.headers.get("Content-Length", "").strip()
                content_encoding = response.headers.get(
                    "Content-Encoding", "identity"
                ).strip().casefold()
                if declared.isdigit() and int(declared) > maximum_bytes:
                    response.close()
                    raise BuildError("Remote input exceeds its configured size limit.")

                digest = hashlib.sha256()
                total = 0
                prefix = b""
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if not chunk:
                            continue
                        if len(prefix) < 256:
                            prefix = (prefix + chunk)[:256]
                        total += len(chunk)
                        if total > maximum_bytes:
                            raise BuildError("Remote input exceeds its configured size limit.")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                response.close()
                if (
                    declared.isdigit()
                    and content_encoding in {"", "identity"}
                    and total != int(declared)
                ):
                    raise requests.RequestException("incomplete response body")
                if expected_gzip and prefix[:2] != b"\x1f\x8b":
                    raise BuildError("EPG source is not a gzip file.")
                lowered = prefix.lstrip().lower()
                if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html"):
                    raise BuildError("Remote input returned HTML instead of data.")
                os.replace(temporary, destination)
                return destination, {
                    "sha256": digest.hexdigest(),
                    "bytes": total,
                    "etag": response.headers.get("ETag", ""),
                    "lastModified": response.headers.get("Last-Modified", ""),
                    "finalHost": final_host,
                }
            except BuildError:
                temporary.unlink(missing_ok=True)
                raise
            except (OSError, requests.RequestException):
                temporary.unlink(missing_ok=True)
                if attempt == attempts:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
    finally:
        session.close()
    raise BuildError(f"Remote input download failed after {attempts} attempts ({last_status}).")


def mapping_bytes_from_args(args: argparse.Namespace, work_dir: Path) -> bytes:
    if args.mapping_file:
        path = Path(args.mapping_file).resolve()
        if not path.is_file():
            raise BuildError(f"Mapping file does not exist: {path}")
        if path.stat().st_size > MAX_MAPPING_BYTES:
            raise BuildError("Mapping file exceeds its configured size limit.")
        content = path.read_bytes()
        if len(content) > MAX_MAPPING_BYTES:
            raise BuildError("Mapping file exceeds its configured size limit.")
        return content
    url = str(args.mapping_url or "").strip()
    if not url:
        raise BuildError(
            "Provide --mapping-file (production) or --mapping-url (diagnostic only)."
        )
    path, _details = safe_download(
        url,
        work_dir / "downloads" / "mapping.csv",
        maximum_bytes=MAX_MAPPING_BYTES,
        expected_gzip=False,
        allowed_host_suffixes=("docs.google.com", "googleusercontent.com"),
    )
    return path.read_bytes()


def bounded_private_file_bytes(
    path_value: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    source = Path(path_value)
    if source.is_symlink():
        raise BuildError(f"{label} must not be a symbolic link.")
    path = source.resolve()
    if not path.is_file():
        raise BuildError(f"{label} does not exist: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BuildError(f"{label} could not be inspected.") from exc
    if size > int(maximum_bytes):
        raise BuildError(f"{label} exceeds its configured size limit.")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"{label} could not be read.") from exc
    if len(content) > int(maximum_bytes):
        raise BuildError(f"{label} exceeds its configured size limit.")
    return content


_SYNTHETIC_LANGUAGE_NAMES = (
    "hindi",
    "punjabi",
    "tamil",
    "telugu",
    "malayalam",
    "kannada",
    "marathi",
    "bengali",
    "gujarati",
    "urdu",
    "english",
)
_SYNTHETIC_ACRONYMS = {
    "am": "AM",
    "bbc": "BBC",
    "ca": "CA",
    "cfl": "CFL",
    "chl": "CHL",
    "ctv": "CTV",
    "et": "ET",
    "espn": "ESPN",
    "f1": "F1",
    "hbo": "HBO",
    "itv": "ITV",
    "lhjmq": "LHJMQ",
    "mlb": "MLB",
    "mls": "MLS",
    "nba": "NBA",
    "nfl": "NFL",
    "nhl": "NHL",
    "ohl": "OHL",
    "pga": "PGA",
    "pm": "PM",
    "ppv": "PPV",
    "tv": "TV",
    "tnt": "TNT",
    "tsn": "TSN",
    "uae": "UAE",
    "ufc": "UFC",
    "uk": "UK",
    "us": "US",
    "wsl": "WSL",
    "wwe": "WWE",
}
_SYNTHETIC_GENRE_LABELS = {
    "adult": "Adult",
    "documentary": "Documentary",
    "education": "Education",
    "entertainment": "Entertainment",
    "events": "Live Event",
    "general": "General",
    "kids": "Kids",
    "lifestyle": "Lifestyle",
    "movies": "Movies",
    "music": "Music",
    "news": "News",
    "religion": "Religion",
    "shopping": "Shopping",
    "sports": "Sports",
}


def _smart_synthetic_title(value: object) -> str:
    """Render provider text readably without adding facts not in that text."""

    text = clean_text(value, 180)
    if not text:
        return ""
    titled = text.title()
    for word, replacement in _SYNTHETIC_ACRONYMS.items():
        titled = re.sub(
            rf"(?<![\w]){re.escape(word)}(?![\w])",
            replacement,
            titled,
            flags=re.I,
        )
    for connector in (
        "an", "and", "at", "by", "for", "in", "of", "on", "the", "to",
        "vs", "with",
    ):
        titled = re.sub(
            rf"(?<=\s){connector}(?=\s)", connector, titled, flags=re.I
        )
    return clean_text(titled, 180)


def _clean_synthetic_channel_label(value: object) -> str:
    """Remove provider slot/quality decoration while retaining real event text."""

    text = unicodedata.normalize("NFKC", clean_text(value, 300))
    text = re.sub(r"[#*_]{2,}", " ", text)
    text = re.sub(
        r"\[(?:live[\s_-]*event|event|live|4k|uhd|fhd|hd|sd|hevc|"
        r"[a-z]{2}|s[ᴛt][ᴠv]\+?)\]",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\((?:live[\s_-]*event|event|all\s+day\s+one\s+movie|"
        r"4k|uhd|fhd|hd|sd|hevc)\)",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:[A-Z]{2}\s*)?\(\s*ESPN\+\s*\d+\s*\)\s*[|:\-]*\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:PPV\s*)?(?:LIVE\s*)?EVENT(?:\s*(?:NO\.?|#)?\s*\d+)?"
        r"\s*[|:\-]*\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^24\s*[/x]\s*7\s*[|:\-]*\s*", "", text, flags=re.I)
    text = re.sub(
        r"^(?:US|UK|CA|IN|EN)\s*(?:4K|UHD|FHD|HD|SD)?\s*[|:]\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"[|]+", " ", text)
    text = re.sub(r"(?<=\w)[_\-](?=\w)", " ", text)
    text = re.sub(r"\s+[\-–—]\s+", " ", text)
    text = re.sub(
        r"(?:\s+|^)(?:(?:4K|UHD|FHD|HD|SD|HEVC|H\.?(?:264|265)|"
        r"1080P?|2160P?)\s*)+$",
        "",
        text,
        flags=re.I,
    )
    return clean_text(text.strip(" |:-–—"), 180)


_EASTERN_TIME = ZoneInfo("America/New_York")
_ISO_EVENT_TIME_RE = re.compile(
    r"\(\s*(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
    r"[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"\s*(?P<zone>ET|EST|EDT)?\s*\)",
    re.I,
)
_SHORT_EVENT_TIME_RE = re.compile(
    r"\(\s*(?P<month>\d{1,2})[./](?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>AM|PM)"
    r"\s*(?P<zone>ET|EST|EDT)?\s*\)",
    re.I,
)
_BLANK_EVENT_LABELS = frozenset(
    {
        "",
        "event",
        "event tba",
        "coming soon",
        "live event",
        "ppv",
        "ppv event",
        "tba",
        "to be announced",
    }
)


@dataclass(frozen=True)
class SyntheticEventTitle:
    name: str
    start_epoch: int | None
    time_label: str


def _plausible_event_datetime(
    *,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
    reference_epoch: int,
    zone_hint: str = "",
) -> datetime | None:
    try:
        naive = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
        )
    except ValueError:
        return None
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=_EASTERN_TIME, fold=fold)
        if (
            candidate.astimezone(timezone.utc)
            .astimezone(_EASTERN_TIME)
            .replace(tzinfo=None)
            == naive
            and all(
                existing.utcoffset() != candidate.utcoffset()
                for existing in candidates
            )
        ):
            candidates.append(candidate)
    if not candidates:
        # Reject nonexistent wall-clock times during the spring DST jump.
        return None
    hint = str(zone_hint or "").upper()
    if hint in {"EST", "EDT"}:
        candidates = [value for value in candidates if value.tzname() == hint]
        if not candidates:
            return None
    elif len(candidates) > 1:
        # Generic "ET" cannot disambiguate the repeated fall-back hour.
        return None
    event = candidates[0]
    reference = datetime.fromtimestamp(int(reference_epoch), tz=timezone.utc)
    # Provider placeholder slots commonly carry dates such as 2098-01-01.
    # A PPV name can remain useful, but that implausible time must never be
    # presented to a customer as a real scheduled event.
    if abs(event.astimezone(timezone.utc) - reference) > timedelta(days=550):
        return None
    return event


def _event_time_label(value: datetime) -> str:
    return (
        f"{value.strftime('%b')} {value.day}, "
        f"{value.strftime('%I').lstrip('0')}:{value.strftime('%M %p %Z')}"
    )


def decode_synthetic_event_title(
    row: MappingRow,
    *,
    reference_epoch: int,
) -> SyntheticEventTitle | None:
    """Decode an event name/time only when the approved row supplies them."""

    event_like = (
        row.metadata.genre == "events"
        or row.metadata.channel_role in {"event", "ppv"}
        or "ppv.events" in row.epg_id.casefold()
        or bool(re.search(r"\bESPN\+\s*\d+\b", row.channel_name, re.I))
    )
    if not event_like:
        return None

    raw_text = unicodedata.normalize("NFKC", clean_text(row.channel_name, 300))
    match = _ISO_EVENT_TIME_RE.search(raw_text)
    event_time: datetime | None = None
    supplied_time_label = ""
    if match:
        event_time = _plausible_event_datetime(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=int(match.group("second") or 0),
            reference_epoch=reference_epoch,
            zone_hint=match.group("zone") or "",
        )
        raw_text = raw_text[: match.start()] + " " + raw_text[match.end() :]
    else:
        match = _SHORT_EVENT_TIME_RE.search(raw_text)
        if match:
            hour = int(match.group("hour"))
            month = int(match.group("month"))
            day = int(match.group("day"))
            minute = int(match.group("minute"))
            try:
                # A yearless provider label cannot truthfully be assigned an
                # instant or called "upcoming". Validate its calendar shape,
                # then retain generic ET (or its explicit abbreviation).
                datetime(2000, month, day, hour, minute)
            except ValueError:
                supplied_time_label = ""
            else:
                if 1 <= hour <= 12:
                    supplied_time_label = (
                        f"{datetime(2000, month, day).strftime('%b')} {day}, "
                        f"{hour}:{minute:02d} {match.group('meridiem').upper()} "
                        f"{(match.group('zone') or 'ET').upper()}"
                    )
                    eastern_reference = datetime.fromtimestamp(
                        int(reference_epoch), tz=timezone.utc
                    ).astimezone(_EASTERN_TIME)
                    local_hour = hour % 12
                    if match.group("meridiem").casefold() == "pm":
                        local_hour += 12
                    candidates = [
                        candidate
                        for year in (
                            eastern_reference.year - 1,
                            eastern_reference.year,
                            eastern_reference.year + 1,
                        )
                        if (
                            candidate := _plausible_event_datetime(
                                year=year,
                                month=month,
                                day=day,
                                hour=local_hour,
                                minute=minute,
                                reference_epoch=reference_epoch,
                                zone_hint=match.group("zone") or "ET",
                            )
                        )
                        is not None
                    ]
                    if candidates:
                        nearest = min(
                            candidates,
                            key=lambda value: (
                                abs(
                                    value.astimezone(timezone.utc).timestamp()
                                    - int(reference_epoch)
                                ),
                                0
                                if value.astimezone(timezone.utc).timestamp()
                                >= int(reference_epoch)
                                else 1,
                            ),
                        )
                        if abs(
                            nearest.astimezone(timezone.utc).timestamp()
                            - int(reference_epoch)
                        ) <= 183 * 86400:
                            event_time = nearest
            raw_text = raw_text[: match.start()] + " " + raw_text[match.end() :]

    text = _clean_synthetic_channel_label(raw_text)
    text = re.sub(
        r"^(?:CFL|CHL|MLB|MLS|NBA|NFL|NHL|PPV|UFC|WWE)\s*\d+\s*:\s*",
        "",
        text,
        flags=re.I,
    )
    normalized_name = _security_normalize_text(text)
    if re.search(r"\b(?:no event|no scheduled event|no event scheduled)\b", normalized_name):
        text = "No Event Scheduled"
        event_time = None
        supplied_time_label = ""
    elif re.search(r"\boff air\b", normalized_name):
        text = "Off Air"
        event_time = None
        supplied_time_label = ""
    elif re.search(r"\b(?:coming soon|tba)\b", normalized_name):
        text = ""
    elif raw_text.rstrip().endswith((":", "|")) and re.fullmatch(
        r"(?:CFL|CHL|MLB|MLS|NBA|NFL|NHL|PPV|UFC|WWE)\s*\d+",
        text,
        flags=re.I,
    ):
        text = ""
    name = _smart_synthetic_title(clean_text(text.strip(" |:-–—"), 160))
    if _security_normalize_text(name) in _BLANK_EVENT_LABELS:
        name = "Event To Be Announced"
    if event_time is None:
        return SyntheticEventTitle(
            name=name,
            start_epoch=None,
            time_label=supplied_time_label,
        )
    return SyntheticEventTitle(
        name=name,
        start_epoch=int(event_time.timestamp()),
        time_label=_event_time_label(event_time),
    )


def synthetic_programme_title(
    row: MappingRow,
    *,
    reference_epoch: int | None = None,
) -> str:
    """Create a truthful placeholder title from approved channel metadata.

    The function intentionally does not invent episodes, performers, scores,
    descriptions, dates, or times. Event dates are accepted only from supported
    provider label formats and bounded relative to this build; slot numbers and
    technical decoration are removed.
    """

    if row.metadata.genre == "adult" or row.metadata.content_rating == "adult":
        return "Adult Programming"

    event = decode_synthetic_event_title(
        row,
        reference_epoch=int(reference_epoch or 0),
    ) if reference_epoch is not None else None
    if event is not None:
        if not event.time_label:
            return event.name
        prefix = "Upcoming: " if event.start_epoch and event.start_epoch > int(reference_epoch) else ""
        return clean_text(f"{prefix}{event.name} — {event.time_label}", 180)

    title = _clean_synthetic_channel_label(row.channel_name)
    category = _security_normalize_text(row.category_name)
    is_singer_group = bool(re.search(r"\bsingers?\b", category))
    if is_singer_group:
        language_pattern = "|".join(_SYNTHETIC_LANGUAGE_NAMES)
        artist = re.sub(
            rf"^(?:{language_pattern})\s*[|:\-]*\s*",
            "",
            title,
            flags=re.I,
        )
        artist = re.sub(r"\b(?:singer|songs?)\b\s*$", "", artist, flags=re.I)
        artist = clean_text(artist, 150)
        if artist:
            return clean_text(f"{_smart_synthetic_title(artist)} Songs", 180)

    is_movie = row.metadata.genre == "movies" or "movie.dummy" in row.epg_id.casefold()
    if is_movie and title:
        generic_numbered = re.fullmatch(
            rf"({'|'.join(_SYNTHETIC_LANGUAGE_NAMES)})\s+movies?\s+\d+",
            title,
            flags=re.I,
        )
        if generic_numbered:
            title = f"{generic_numbered.group(1)} Movies"
        else:
            title = re.sub(r"\bmovies?\s+\d+\s*$", "Movies", title, flags=re.I)
        language_prefix = re.match(
            rf"^({'|'.join(_SYNTHETIC_LANGUAGE_NAMES)})\s+(.+)$",
            title,
            flags=re.I,
        )
        if language_prefix and language_prefix.group(2).casefold() not in {
            "movie",
            "movie loop",
            "movies",
            "movies loop",
        }:
            title = language_prefix.group(2)
        title = re.sub(r"\bmovie\s+loop\b", "Movies", title, flags=re.I)
        title = re.sub(
            r"\baction\s+(?:and\s+|&\s*)?adventure\b",
            "Action & Adventure",
            title,
            flags=re.I,
        )

    rendered = _smart_synthetic_title(title)
    if rendered:
        return rendered
    genre_label = _SYNTHETIC_GENRE_LABELS.get(row.metadata.genre, "")
    return f"{genre_label} Programming" if genre_label else "Channel Programming"


def synthetic_programme_categories(row: MappingRow) -> list[str]:
    label = _SYNTHETIC_GENRE_LABELS.get(row.metadata.genre, "")
    return [label] if label else []


@dataclass(frozen=True)
class SyntheticGuideStats:
    schedules: int
    programme_rows: int
    block_hours: int
    event_window_hours: int
    window_start: int
    window_end: int


def insert_synthetic_guides(
    *,
    connection: sqlite3.Connection,
    rows: Sequence[MappingRow],
    window_start: int,
    window_end: int,
    block_hours: int = DEFAULT_SYNTHETIC_BLOCK_HOURS,
    event_window_hours: int = DEFAULT_SYNTHETIC_EVENT_WINDOW_HOURS,
    reference_epoch: int | None = None,
) -> SyntheticGuideStats:
    """Insert deterministic, per-stream guide blocks for approved dummy rows."""

    hours = int(block_hours)
    if hours < 4 or hours > 24:
        raise BuildError("Synthetic guide block hours must be between 4 and 24.")
    event_hours = int(event_window_hours)
    if event_hours < 1 or event_hours > 24:
        raise BuildError("Synthetic event window hours must be between 1 and 24.")
    block_seconds = hours * 3600
    aligned_start = (int(window_start) // block_seconds) * block_seconds
    final_end = int(window_end)
    title_reference_epoch = int(
        reference_epoch if reference_epoch is not None else window_start
    )
    if final_end <= aligned_start:
        raise BuildError("Synthetic guide window must end after it starts.")
    synthetic_rows = sorted(
        (
            row
            for row in rows
            if row.runtime_eligible and row.requested_source == "dummy"
        ),
        key=lambda row: (
            row.server_id,
            stream_sort_key(row.stream_id),
            row.channel_name.casefold(),
            row.channel_name,
        ),
    )
    slots_per_schedule = (
        final_end - aligned_start + block_seconds - 1
    ) // block_seconds
    # Splitting an exact-time event out of the day-aligned grid can add at most
    # two boundaries per stream.
    expected_programmes = len(synthetic_rows) * (slots_per_schedule + 2)
    if expected_programmes > MAX_SYNTHETIC_PROGRAMME_ROWS:
        raise BuildError(
            "Synthetic guide would exceed the configured programme-row limit "
            f"({expected_programmes:,} > {MAX_SYNTHETIC_PROGRAMME_ROWS:,})."
        )
    if not synthetic_rows:
        return SyntheticGuideStats(
            0, 0, hours, event_hours, aligned_start, final_end
        )

    connection.executemany(
        "INSERT INTO channels "
        "(source_key, channel_key, source_channel_id, display_name, icon_url) "
        "VALUES (?, ?, ?, ?, '')",
        (
            (
                row.source_key,
                row.epg_id,
                f"synthetic:{row.synthetic_identity}",
                row.channel_name,
            )
            for row in synthetic_rows
        ),
    )
    programme_batch: list[tuple[Any, ...]] = []
    programme_rows = 0
    insert_sql = (
        "INSERT INTO programmes "
        "(source_key, channel_key, start_epoch, stop_epoch, title, subtitle, "
        "description, categories_json, quality) "
        "VALUES (?, ?, ?, ?, ?, '', '', ?, 1000)"
    )
    for row in synthetic_rows:
        categories_json = json_compact(synthetic_programme_categories(row))
        event = None
        if row.metadata.genre != "adult" and row.metadata.content_rating != "adult":
            event = decode_synthetic_event_title(
                row,
                reference_epoch=title_reference_epoch,
            )
        if event is None:
            regular_title = synthetic_programme_title(
                row,
                reference_epoch=title_reference_epoch,
            )
        elif event.time_label:
            regular_title = clean_text(
                f"{event.name} — {event.time_label}", 180
            )
        else:
            regular_title = event.name
        if event is not None and event.start_epoch is not None:
            event_start = event.start_epoch
            event_end = event_start + event_hours * 3600
        for base_start in range(aligned_start, final_end, block_seconds):
            base_stop = base_start + block_seconds
            split_points = [base_start]
            if event is not None and event.start_epoch is not None:
                if base_start < event_start < base_stop:
                    split_points.append(event_start)
                if base_start < event_end < base_stop:
                    split_points.append(event_end)
            split_points.append(base_stop)
            split_points.sort()
            for start, stop in zip(split_points, split_points[1:]):
                if event is not None and event.start_epoch is not None:
                    event_label = clean_text(
                        f"{event.name} — {event.time_label}", 180
                    )
                    if stop <= event_start:
                        title = clean_text(f"Upcoming: {event_label}", 180)
                    elif start < event_end and stop > event_start:
                        title = event_label
                    else:
                        title = clean_text(
                            f"Event Information: {event_label}", 180
                        )
                else:
                    title = regular_title
                programme_batch.append(
                    (
                        row.source_key,
                        row.epg_id,
                        start,
                        stop,
                        title,
                        categories_json,
                    )
                )
                programme_rows += 1
                if len(programme_batch) >= SQLITE_BATCH_SIZE:
                    connection.executemany(insert_sql, programme_batch)
                    programme_batch.clear()
    if programme_batch:
        connection.executemany(insert_sql, programme_batch)
    connection.commit()
    return SyntheticGuideStats(
        schedules=len(synthetic_rows),
        programme_rows=programme_rows,
        block_hours=hours,
        event_window_hours=event_hours,
        window_start=aligned_start,
        window_end=final_end,
    )


def create_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-32768;
        PRAGMA mmap_size=0;
        CREATE TABLE channels (
            source_key TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            source_channel_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            icon_url TEXT NOT NULL,
            PRIMARY KEY (source_key, channel_key)
        ) WITHOUT ROWID;
        CREATE TABLE source_channel_ids (
            source_key TEXT NOT NULL,
            source_channel_id TEXT NOT NULL,
            PRIMARY KEY (source_key, source_channel_id)
        ) WITHOUT ROWID;
        CREATE TABLE programmes (
            source_key TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            start_epoch INTEGER NOT NULL,
            stop_epoch INTEGER NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL,
            description TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            quality INTEGER NOT NULL,
            PRIMARY KEY (source_key, channel_key, start_epoch, stop_epoch, title)
        ) WITHOUT ROWID;
        """
    )
    return connection


def local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def preferred_child_text(element: etree._Element, name: str) -> str:
    fallback = ""
    for child in element:
        if local_name(child.tag) != name:
            continue
        value = clean_text("".join(child.itertext()), 2_000)
        if not value:
            continue
        language = (child.get("lang") or "").casefold()
        if language in {"en", "eng", "en-us", "en-gb"}:
            return value
        if not fallback:
            fallback = value
    return fallback


def child_texts(element: etree._Element, name: str) -> list[str]:
    result: list[str] = []
    for child in element:
        if local_name(child.tag) != name:
            continue
        value = clean_text("".join(child.itertext()), 300)
        if value and value not in result:
            result.append(value)
            if len(result) >= MAX_CATEGORY_VALUES:
                break
    return result


def parse_xmltv_time(value: str | None) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(
        r"(\d{12}|\d{14})(?:\s+(Z|UTC|GMT|[+-]\d{4}))?", text, re.I
    )
    if match is None:
        return None
    stamp, zone = match.groups()
    fmt = "%Y%m%d%H%M%S" if len(stamp) == 14 else "%Y%m%d%H%M"
    try:
        parsed = datetime.strptime(stamp, fmt)
    except ValueError:
        return None
    if not zone or zone.upper() in {"Z", "UTC", "GMT"}:
        tz = timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        zone_hours = int(zone[1:3])
        zone_minutes = int(zone[3:5])
        if zone_hours > 23 or zone_minutes > 59:
            return None
        offset = timedelta(hours=zone_hours, minutes=zone_minutes)
        tz = timezone(sign * offset)
    return int(parsed.replace(tzinfo=tz).timestamp())


def open_limited_xml(path: Path, maximum_expanded: int) -> LimitedReader:
    raw = Path(path).open("rb")
    try:
        return open_limited_xml_handle(
            raw, maximum_expanded, close_raw_file=True
        )
    except Exception:
        raw.close()
        raise


def open_limited_xml_handle(
    raw: BinaryIO,
    maximum_expanded: int,
    *,
    close_raw_file: bool = False,
) -> LimitedReader:
    """Open XML/gzip data through an already-open, seekable file handle.

    The gzip signature, decompression, and XML reads all use ``raw``.  Keeping
    the default ``close_raw_file=False`` lets a provenance-sensitive caller
    retain and re-check the same descriptor after parsing.
    """
    raw.seek(0)
    magic = raw.read(2)
    raw.seek(0)
    if magic == b"\x1f\x8b":
        source: BinaryIO = gzip.GzipFile(fileobj=raw, mode="rb")
        # GzipFile does not close a caller-supplied file object.  Wrap its
        # close method when ownership of the underlying regular file belongs
        # to this helper's caller.
        if close_raw_file:
            source = _CloseWithRaw(source, raw)
        return LimitedReader(source, maximum_expanded)
    return LimitedReader(
        raw,
        maximum_expanded,
        close_raw=close_raw_file,
    )


class _CloseWithRaw:
    """Close a transform and its caller-owned backing file together."""

    def __init__(self, source: BinaryIO, raw: BinaryIO) -> None:
        self.source = source
        self.raw = raw

    def read(self, size: int = -1) -> bytes:
        return self.source.read(size)

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            self.raw.close()


def release_top_level(element: etree._Element) -> None:
    element.clear()
    parent = element.getparent()
    if parent is not None:
        while element.getprevious() is not None:
            del parent[0]


def _xml_source_label(source_label: str) -> str:
    return clean_text(source_label, 100) or "XMLTV input"


def _safe_xmltv_system_identifier(system_id: str | None) -> bool:
    """Allow only the inert local identifier used by standard XMLTV feeds.

    Neither the Expat preflight nor lxml loads this identifier.  Keeping the
    allowlist exact also prevents a future parser-option change from turning a
    network URL or local-file path into an external fetch.
    """
    return system_id in {None, "xmltv.dtd"}


def _preflight_xml_prolog(
    source: BinaryIO,
    *,
    allow_inert_xmltv_doctype: bool,
    source_label: str,
) -> None:
    """Structurally inspect a bounded XML prolog without loading any DTD.

    A raw byte search both misses alternate encodings and mistakes declaration
    text inside comments for a real DTD.  Expat reports the declaration before
    processing an internal subset, so an unsafe subset can be rejected before
    lxml sees it.  Parsing stops at the first root element.
    """
    label = _xml_source_label(source_label)
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    declared_root = ""

    def start_doctype(
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: int,
    ) -> None:
        nonlocal declared_root
        if not allow_inert_xmltv_doctype:
            raise BuildError(f"{label} contains a forbidden DTD declaration.")
        if name != "tv":
            raise BuildError(
                f"{label} contains a forbidden non-XMLTV DTD declaration."
            )
        if has_internal_subset:
            raise BuildError(f"{label} contains a forbidden DTD internal subset.")
        if public_id is not None or not _safe_xmltv_system_identifier(system_id):
            raise BuildError(
                f"{label} contains a forbidden external DTD identifier."
            )
        declared_root = name

    def entity_declaration(*_arguments: object) -> None:
        raise BuildError(f"{label} contains a forbidden entity declaration.")

    def external_entity(*_arguments: object) -> int:
        raise BuildError(f"{label} attempted to load a forbidden external entity.")

    def root_element(name: str, _attributes: Mapping[str, str]) -> None:
        if declared_root and name != declared_root:
            raise BuildError(f"{label} has a DTD/root-name mismatch.")
        raise _XmlRootReached()

    parser.StartDoctypeDeclHandler = start_doctype
    parser.EntityDeclHandler = entity_declaration
    parser.ExternalEntityRefHandler = external_entity
    parser.StartElementHandler = root_element

    total = 0
    try:
        while total < MAX_XML_PROLOG_BYTES:
            chunk = source.read(min(64 * 1024, MAX_XML_PROLOG_BYTES - total))
            if not chunk:
                parser.Parse(b"", True)
                break
            total += len(chunk)
            parser.Parse(chunk, False)
    except _XmlRootReached:
        return
    except BuildError:
        raise
    except expat.ExpatError as exc:
        raise BuildError(f"{label} is malformed or truncated XMLTV.") from exc
    if total >= MAX_XML_PROLOG_BYTES:
        raise BuildError(
            f"{label} XML prolog exceeds the {MAX_XML_PROLOG_BYTES:,}-byte limit."
        )
    raise BuildError(f"{label} is malformed or truncated XMLTV.")


def reject_unsafe_xml_prefix(
    path: Path,
    *,
    allow_inert_xmltv_doctype: bool = False,
    source_label: str = "XMLTV input",
) -> None:
    """Preflight an XML or gzip source before the full streaming parse."""
    try:
        with open_limited_xml(path, MAX_XML_PROLOG_BYTES) as source:
            _preflight_xml_prolog(
                source,
                allow_inert_xmltv_doctype=allow_inert_xmltv_doctype,
                source_label=source_label,
            )
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise BuildError(
            f"{_xml_source_label(source_label)} is malformed or truncated XMLTV."
        ) from exc


def reject_unsafe_xml_prefix_handle(
    raw: BinaryIO,
    *,
    allow_inert_xmltv_doctype: bool = False,
    source_label: str = "XMLTV input",
) -> None:
    """Apply the structural prolog preflight to an open source descriptor."""
    position = raw.tell()
    try:
        with open_limited_xml_handle(raw, MAX_XML_PROLOG_BYTES) as source:
            _preflight_xml_prolog(
                source,
                allow_inert_xmltv_doctype=allow_inert_xmltv_doctype,
                source_label=source_label,
            )
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise BuildError(
            f"{_xml_source_label(source_label)} is malformed or truncated XMLTV."
        ) from exc
    finally:
        raw.seek(position)


def reject_parsed_doctype(
    element: etree._Element,
    *,
    allow_inert_xmltv_doctype: bool = False,
    source_label: str = "XMLTV input",
) -> None:
    """Cross-check a declaration after lxml has parsed the document root."""
    document = element.getroottree().docinfo
    if not clean_text(document.doctype, 2_000):
        return
    label = _xml_source_label(source_label)
    if not allow_inert_xmltv_doctype:
        raise BuildError(f"{label} contains a forbidden DTD declaration.")
    if (
        document.root_name != "tv"
        or local_name(element.tag) != "tv"
        or document.public_id is not None
        or not _safe_xmltv_system_identifier(document.system_url)
    ):
        raise BuildError(f"{label} contains a forbidden DTD declaration.")
    internal_dtd = document.internalDTD
    if internal_dtd is not None and (
        any(internal_dtd.iterentities()) or any(internal_dtd.iterelements())
    ):
        raise BuildError(f"{label} contains forbidden internal DTD declarations.")


def wanted_target(
    source_channel_id: str,
    wanted_exact: set[str],
    wanted_by_fold: Mapping[str, set[str]],
    source_ids_by_fold: Mapping[str, set[str]],
) -> str:
    if source_channel_id in wanted_exact:
        return source_channel_id
    folded = source_channel_id.casefold()
    wanted = wanted_by_fold.get(folded, set())
    source_variants = source_ids_by_fold.get(folded, set())
    if len(wanted) == 1 and len(source_variants) == 1:
        return next(iter(wanted))
    return ""


def first_channel_icon(element: etree._Element, base_url: str) -> str:
    for child in element:
        if local_name(child.tag) != "icon":
            continue
        value = valid_http_url(child.get("src") or "", base_url=base_url)
        if value:
            return value
    return ""


def programme_quality(
    title: str, subtitle: str, description: str, categories: Sequence[str]
) -> int:
    score = 0
    score += min(len(title), 160)
    score += 40 if subtitle else 0
    score += min(len(description), 400) // 10
    score += min(len(categories), 8) * 10
    return score


def ingest_xmltv_source(
    *,
    connection: sqlite3.Connection,
    path: Path,
    source_key: str,
    wanted_ids: set[str],
    window_start: int,
    source_base_url: str = "",
    allow_source_icons: bool = True,
    allow_inert_xmltv_doctype: bool = False,
    maximum_expanded_bytes: int = MAX_SOURCE_EXPANDED_BYTES,
) -> SourceStats:
    """Strictly parse one XMLTV source and spool only mapped schedules."""
    if allow_inert_xmltv_doctype and source_key not in {
        "panel:server_2",
        "panel:server_3",
    }:
        raise BuildError(
            "The inert XMLTV DOCTYPE allowance is restricted to Server 2/3 "
            "native panel inputs."
        )
    reject_unsafe_xml_prefix(
        path,
        allow_inert_xmltv_doctype=allow_inert_xmltv_doctype,
        source_label=source_key,
    )
    stats = SourceStats(source_key=source_key, compressed_bytes=path.stat().st_size)
    wanted_by_fold: dict[str, set[str]] = defaultdict(set)
    for channel_id in wanted_ids:
        wanted_by_fold[channel_id.casefold()].add(channel_id)
    source_ids_by_fold: dict[str, set[str]] = defaultdict(set)
    actual_to_target: dict[str, str] = {}
    programme_batch: list[tuple[Any, ...]] = []
    initial_programme_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM programmes WHERE source_key = ?", (source_key,)
        ).fetchone()[0]
    )
    root_name = ""
    root_element: etree._Element | None = None
    active_top_level: etree._Element | None = None
    active_child_elements = 0
    programme_phase_started = False
    started = time.monotonic()

    def flush_programmes() -> None:
        if not programme_batch:
            return
        connection.executemany(
            """
            INSERT INTO programmes (
                source_key, channel_key, start_epoch, stop_epoch, title,
                subtitle, description, categories_json, quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_key, channel_key, start_epoch, stop_epoch, title
            ) DO UPDATE SET
                subtitle = excluded.subtitle,
                description = excluded.description,
                categories_json = excluded.categories_json,
                quality = excluded.quality
            WHERE (
                excluded.quality,
                excluded.subtitle,
                excluded.description,
                excluded.categories_json
            ) > (
                programmes.quality,
                programmes.subtitle,
                programmes.description,
                programmes.categories_json
            )
            """,
            programme_batch,
        )
        programme_batch.clear()

    try:
        with open_limited_xml(path, maximum_expanded_bytes) as source:
            context = etree.iterparse(
                source,
                events=("start", "end"),
                recover=False,
                huge_tree=False,
                load_dtd=False,
                no_network=True,
                resolve_entities=False,
                remove_comments=True,
                remove_pis=True,
            )
            for event, element in context:
                if event == "start":
                    if root_element is None:
                        root_element = element
                        root_name = local_name(element.tag)
                        reject_parsed_doctype(
                            element,
                            allow_inert_xmltv_doctype=allow_inert_xmltv_doctype,
                            source_label=source_key,
                        )
                        if root_name != "tv":
                            raise BuildError(
                                f"{source_key} is not an XMLTV <tv> document."
                            )
                    elif element.getparent() is root_element:
                        child_name = local_name(element.tag)
                        if child_name not in {"channel", "programme"}:
                            raise BuildError(
                                f"{source_key} contains unsupported top-level "
                                f"XMLTV element <{child_name}>."
                            )
                        active_top_level = element
                        active_child_elements = 0
                    elif active_top_level is not None:
                        active_child_elements += 1
                        if active_child_elements > MAX_RECORD_CHILD_ELEMENTS:
                            raise BuildError(
                                f"{source_key} XMLTV record exceeds the "
                                "configured child-element limit."
                            )
                    continue
                if root_element is None or element.getparent() is not root_element:
                    continue
                name = local_name(element.tag)
                stats.total_elements += 1
                if stats.total_elements > MAX_SOURCE_ELEMENTS:
                    raise BuildError("XML source exceeds its configured element limit.")

                if name == "channel":
                    stats.channel_elements += 1
                    source_channel_id = clean_identifier(element.get("id") or "", 300)
                    if source_channel_id:
                        inserted = connection.execute(
                            "INSERT OR IGNORE INTO source_channel_ids VALUES (?, ?)",
                            (source_key, source_channel_id),
                        ).rowcount
                        if inserted:
                            stats.unique_channel_ids += 1
                        else:
                            stats.duplicate_channel_ids += 1
                        folded = source_channel_id.casefold()
                        if folded in wanted_by_fold:
                            source_ids_by_fold[folded].add(source_channel_id)
                        wanted_variants = wanted_by_fold.get(folded, set())
                        source_variants = source_ids_by_fold.get(folded, set())
                        if len(wanted_variants) != 1 or len(source_variants) != 1:
                            invalidated = [
                                variant
                                for variant in source_variants
                                if variant not in wanted_ids
                                and variant in actual_to_target
                            ]
                            if invalidated and programme_phase_started:
                                raise BuildError(
                                    f"{source_key} exposed an ambiguous case-only "
                                    "channel ID after programme rows began."
                                )
                            for variant in invalidated:
                                actual_to_target.pop(variant, None)
                        target = wanted_target(
                            source_channel_id,
                            wanted_ids,
                            wanted_by_fold,
                            source_ids_by_fold,
                        )
                        if target:
                            actual_to_target[source_channel_id] = target
                            display_name = preferred_child_text(element, "display-name")
                            icon_url = (
                                first_channel_icon(element, source_base_url)
                                if allow_source_icons
                                else ""
                            )
                            connection.execute(
                                """
                                INSERT INTO channels (
                                    source_key, channel_key, source_channel_id,
                                    display_name, icon_url
                                ) VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(source_key, channel_key) DO UPDATE SET
                                  display_name = CASE
                                    WHEN channels.display_name = '' THEN excluded.display_name
                                    ELSE channels.display_name END,
                                  icon_url = CASE
                                    WHEN channels.icon_url = '' THEN excluded.icon_url
                                    ELSE channels.icon_url END
                                """,
                                (
                                    source_key,
                                    target,
                                    source_channel_id,
                                    display_name,
                                    icon_url,
                                ),
                            )
                            stats.selected_channels += 1
                    release_top_level(element)
                else:
                    programme_phase_started = True
                    stats.programme_elements += 1
                    source_channel_id = clean_identifier(
                        element.get("channel") or "", 300
                    )
                    target = actual_to_target.get(source_channel_id, "")
                    if not target and source_channel_id in wanted_ids:
                        target = source_channel_id
                    if target:
                        start_epoch = parse_xmltv_time(element.get("start"))
                        stop_epoch = parse_xmltv_time(element.get("stop"))
                        title = preferred_child_text(element, "title")
                        if start_epoch is None:
                            stats.invalid_start += 1
                        elif not title:
                            stats.blank_title += 1
                        else:
                            if stop_epoch is None or stop_epoch <= start_epoch:
                                stop_epoch = start_epoch + 3600
                                stats.synthesized_stop += 1
                            if stop_epoch <= window_start:
                                stats.outside_window += 1
                            else:
                                subtitle = preferred_child_text(element, "sub-title")
                                description = preferred_child_text(element, "desc")
                                categories = child_texts(element, "category")
                                programme_batch.append(
                                    (
                                        source_key,
                                        target,
                                        int(start_epoch),
                                        int(stop_epoch),
                                        title,
                                        subtitle,
                                        description,
                                        json_compact(categories),
                                        programme_quality(
                                            title, subtitle, description, categories
                                        ),
                                    )
                                )
                                stats.selected_programmes += 1
                                if len(programme_batch) >= SQLITE_BATCH_SIZE:
                                    flush_programmes()
                                    connection.commit()
                    release_top_level(element)

                active_top_level = None
                active_child_elements = 0
                if stats.total_elements % PROGRESS_EVERY_ELEMENTS == 0:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(
                        f"{source_key}: parsed {stats.total_elements:,} top-level "
                        f"elements, kept {stats.selected_programmes:,} programmes "
                        f"({stats.total_elements / elapsed:,.0f} elements/s)",
                        flush=True,
                    )
            del context
            stats.expanded_bytes = source.count
        flush_programmes()
        connection.commit()
        final_programme_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM programmes WHERE source_key = ?", (source_key,)
            ).fetchone()[0]
        )
        stats.duplicate_programmes = max(
            0,
            stats.selected_programmes
            - max(0, final_programme_rows - initial_programme_rows),
        )
    except (etree.XMLSyntaxError, gzip.BadGzipFile, EOFError, OSError) as exc:
        raise BuildError(f"{source_key} XMLTV input is malformed or truncated.") from exc

    if root_name != "tv":
        raise BuildError(f"{source_key} is not an XMLTV <tv> document.")
    return stats


def panel_credentials(server_id: str) -> tuple[str, str, str]:
    prefix = server_id.upper()
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip().rstrip("/")
    username = os.environ.get(f"{prefix}_USERNAME", "").strip()
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    if not base_url or not username or not password:
        raise BuildError(
            f"{server_id} requests panel EPG, but its BASE_URL/USERNAME/PASSWORD "
            "GitHub secrets are incomplete."
        )
    if len(password) < 4:
        raise BuildError(
            f"{server_id} panel password is too short for safe output scanning."
        )
    parsed = urlparse(base_url)
    allow_insecure_http = parse_bool(
        os.environ.get("ALLOW_INSECURE_PANEL_HTTP", ""),
        default=False,
        field_name="ALLOW_INSECURE_PANEL_HTTP",
    )
    allowed_schemes = {"https", "http"} if allow_insecure_http else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BuildError(
            f"{server_id} panel base URL must be an approved HTTP(S) address "
            "without embedded credentials, query text, or a fragment. HTTPS is "
            "required unless ALLOW_INSECURE_PANEL_HTTP is explicitly TRUE."
        )
    if parsed.scheme == "http":
        print(
            f"WARNING: {server_id} native panel transport is unencrypted because "
            "ALLOW_INSECURE_PANEL_HTTP is TRUE.",
            flush=True,
        )
    return base_url, username, password


def panel_payload_is_non_xmltv(prefix: bytes) -> bool:
    """Recognize common status-200 HTML/JSON panel error responses."""
    lowered = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if lowered.startswith(b"<?xml"):
        declaration_end = lowered.find(b"?>")
        if declaration_end >= 0:
            lowered = lowered[declaration_end + 2 :].lstrip()
    return lowered.startswith((b"<html", b"<!doctype html", b"{", b"["))


def download_panel_xmltv(server_id: str, destination: Path) -> tuple[Path, dict[str, Any]]:
    base_url, username, password = panel_credentials(server_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    session = requests.Session()
    session.headers.update({"User-Agent": f"SKYTV-EPG/{PIPELINE_VERSION}"})
    try:
        for attempt in range(1, 4):
            temporary.unlink(missing_ok=True)
            try:
                response = session.get(
                    f"{base_url}/xmltv.php",
                    params={"username": username, "password": password},
                    stream=True,
                    timeout=(20, 180),
                    allow_redirects=False,
                )
                if response.status_code != 200:
                    response.close()
                    raise requests.RequestException("panel HTTP failure")
                total = 0
                digest = hashlib.sha256()
                prefix = b""
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(4 * 1024 * 1024):
                        if not chunk:
                            continue
                        prefix = (prefix + chunk)[:256]
                        total += len(chunk)
                        if total > MAX_SOURCE_COMPRESSED_BYTES:
                            raise BuildError(f"{server_id} panel XMLTV is too large.")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                response.close()
                if panel_payload_is_non_xmltv(prefix):
                    raise BuildError(f"{server_id} panel returned a non-XML response.")
                os.replace(temporary, destination)
                return destination, {
                    "sha256": digest.hexdigest(),
                    "bytes": total,
                }
            except BuildError:
                temporary.unlink(missing_ok=True)
                raise
            except (OSError, requests.RequestException):
                temporary.unlink(missing_ok=True)
                if attempt == 3:
                    break
                time.sleep(2 ** (attempt - 1))
    finally:
        session.close()
    raise BuildError(f"{server_id} panel XMLTV download failed after 3 attempts.")


@dataclass(frozen=True)
class IconOverride:
    server_id: str
    epg_id: str
    channel_name: str
    icon_url: str
    priority: int


def load_icon_overrides(
    path: Path | None,
    *,
    repository_root: Path,
    staging_public: Path,
    public_base_url: str,
) -> list[IconOverride]:
    if path is None or not Path(path).is_file():
        return []
    result: list[IconOverride] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            try:
                enabled = parse_bool(
                    row.get("enabled", ""),
                    default=True,
                    field_name=f"icon row {row_number} enabled",
                )
            except BuildError as exc:
                raise BuildError(str(exc)) from exc
            if not enabled:
                continue
            server_id = clean_text(row.get("server_id", "*"), 40).casefold() or "*"
            if server_id != "*":
                server_id = normalize_server_id(server_id)
            icon_url = valid_http_url(row.get("icon_url", ""))
            local_file = clean_text(row.get("local_file", ""), 500)
            if local_file:
                relative = Path(local_file)
                if relative.is_absolute() or ".." in relative.parts:
                    raise BuildError(f"Icon row {row_number}: unsafe local_file path.")
                source = (repository_root / "assets" / "logos" / relative).resolve()
                logo_root = (repository_root / "assets" / "logos").resolve()
                if logo_root not in source.parents or not source.is_file():
                    raise BuildError(f"Icon row {row_number}: local_file does not exist.")
                destination = staging_public / "logos" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if public_base_url:
                    icon_url = valid_http_url(
                        f"{public_base_url.rstrip('/')}/logos/{relative.as_posix()}"
                    )
            if not icon_url:
                continue
            try:
                priority = int(clean_text(row.get("priority", ""), 20) or 100)
            except ValueError as exc:
                raise BuildError(f"Icon row {row_number}: priority must be an integer.") from exc
            epg_id = clean_identifier(row.get("epg_id", ""), 300)
            channel_name = clean_identifier(row.get("channel_name", ""), 300)
            if not epg_id and not channel_name:
                raise BuildError(
                    f"Icon row {row_number}: epg_id or channel_name is required."
                )
            result.append(
                IconOverride(
                    server_id=server_id,
                    epg_id=epg_id,
                    channel_name=channel_name,
                    icon_url=icon_url,
                    priority=priority,
                )
            )
    return result


def configured_icon(
    overrides: Sequence[IconOverride], row: MappingRow
) -> str:
    candidates: list[IconOverride] = []
    for item in overrides:
        if item.server_id not in {"*", row.server_id}:
            continue
        id_match = item.epg_id and item.epg_id.casefold() == row.epg_id.casefold()
        name_match = (
            item.channel_name
            and item.channel_name.casefold() == row.channel_name.casefold()
        )
        if id_match or name_match:
            candidates.append(item)
    if not candidates:
        return ""
    chosen = max(
        candidates,
        key=lambda item: (
            item.priority,
            1 if item.server_id == row.server_id else 0,
            1 if item.epg_id else 0,
            item.icon_url,
        ),
    )
    return chosen.icon_url


@dataclass(frozen=True)
class ScheduleStats:
    programme_rows: int
    future_rows: int
    earliest_start: int
    latest_stop: int
    latest_future_stop: int
    conflicting_slots: int
    overlapping_rows: int = 0


def load_schedule_stats(
    connection: sqlite3.Connection, now_epoch: int
) -> dict[tuple[str, str], ScheduleStats]:
    stats: dict[tuple[str, str], ScheduleStats] = {}
    query = """
        SELECT source_key, channel_key,
               COUNT(*) AS programme_rows,
               SUM(CASE WHEN stop_epoch > ? THEN 1 ELSE 0 END) AS future_rows,
               MIN(start_epoch), MAX(stop_epoch),
               MAX(CASE WHEN stop_epoch > ? THEN stop_epoch ELSE 0 END),
               SUM(CASE WHEN title_count > 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN prior_max_stop > start_epoch THEN 1 ELSE 0 END)
        FROM (
            SELECT source_key, channel_key, start_epoch, stop_epoch,
                   title_count,
                   MAX(stop_epoch) OVER (
                     PARTITION BY source_key, channel_key
                     ORDER BY start_epoch, stop_epoch
                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS prior_max_stop
            FROM (
                SELECT source_key, channel_key, start_epoch, stop_epoch,
                       COUNT(*) AS title_count
                FROM programmes
                GROUP BY source_key, channel_key, start_epoch, stop_epoch
            ) AS slots
        )
        GROUP BY source_key, channel_key
    """
    for row in connection.execute(query, (now_epoch, now_epoch)):
        stats[(str(row[0]), str(row[1]))] = ScheduleStats(
            programme_rows=int(row[2] or 0),
            future_rows=int(row[3] or 0),
            earliest_start=int(row[4] or 0),
            latest_stop=int(row[5] or 0),
            latest_future_stop=int(row[6] or 0),
            conflicting_slots=int(row[7] or 0),
            overlapping_rows=int(row[8] or 0),
        )
    return stats


def schedule_quality(stats: ScheduleStats | None, source_key: str) -> tuple[int, ...]:
    if stats is None:
        return (0, 0, 0, 0, 0, 0, 0)
    return (
        1 if stats.future_rows else 0,
        stats.latest_future_stop,
        stats.future_rows,
        stats.programme_rows,
        1 if source_key.startswith("epgshare") else 0,
        -stats.conflicting_slots,
        -stats.overlapping_rows,
    )


def source_channel_info(
    connection: sqlite3.Connection, source_key: str, channel_key: str
) -> tuple[str, str]:
    row = connection.execute(
        "SELECT display_name, icon_url FROM channels "
        "WHERE source_key = ? AND channel_key = ?",
        (source_key, channel_key),
    ).fetchone()
    if row is None:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")


@dataclass
class XmlEntry:
    channel_id: str
    display_names: set[str]
    source_key: str
    channel_key: str
    icon_url: str
    quality: tuple[int, ...]
    entry_type: str
    entry_priority: int


def build_xml_entries(
    *,
    rows: Sequence[MappingRow],
    connection: sqlite3.Connection,
    schedule_stats: Mapping[tuple[str, str], ScheduleStats],
    icon_overrides: Sequence[IconOverride],
) -> tuple[dict[str, XmlEntry], set[str], list[dict[str, str]]]:
    entries: dict[str, XmlEntry] = {}
    streams_with_programmes: set[str] = set()
    conflicts: list[dict[str, str]] = []

    def register(
        channel_id: str,
        display_names: Iterable[str],
        row: MappingRow,
        icon_url: str,
        entry_type: str,
    ) -> None:
        channel_id = clean_identifier(channel_id, 300)
        if not channel_id:
            return
        key = (row.source_key, row.epg_id)
        quality = schedule_quality(schedule_stats.get(key), row.source_key)
        entry_priority = (
            2
            if entry_type in {"canonical_epg_id", "synthetic_stream_id"}
            else (0 if row.requested_source == "dummy" else 1)
        )
        candidate_names = {
            clean_text(value, 300) for value in display_names if clean_text(value, 300)
        }
        current = entries.get(channel_id)
        if current is None:
            entries[channel_id] = XmlEntry(
                channel_id=channel_id,
                display_names=candidate_names or {channel_id},
                source_key=row.source_key,
                channel_key=row.epg_id,
                icon_url=icon_url,
                quality=quality,
                entry_type=entry_type,
                entry_priority=entry_priority,
            )
            return
        if (current.source_key, current.channel_key) == key:
            current.display_names.update(candidate_names)
            if not current.icon_url and icon_url:
                current.icon_url = icon_url
            if entry_priority > current.entry_priority:
                current.entry_priority = entry_priority
                current.entry_type = entry_type
                current.quality = quality
            return
        resolution = "kept_existing"
        if (entry_priority, quality) > (current.entry_priority, current.quality):
            conflicts.append(
                {
                    "channel_id": channel_id,
                    "kept_source": row.source_key,
                    "kept_epg_id": row.epg_id,
                    "discarded_source": current.source_key,
                    "discarded_epg_id": current.channel_key,
                    "resolution": "used_better_schedule",
                }
            )
            current.source_key = row.source_key
            current.channel_key = row.epg_id
            current.display_names = candidate_names or {channel_id}
            current.quality = quality
            current.entry_type = entry_type
            current.entry_priority = entry_priority
            current.icon_url = icon_url
            return
        conflicts.append(
            {
                "channel_id": channel_id,
                "kept_source": current.source_key,
                "kept_epg_id": current.channel_key,
                "discarded_source": row.source_key,
                "discarded_epg_id": row.epg_id,
                "resolution": resolution,
            }
        )

    channel_name_counts = Counter(
        clean_identifier(row.channel_name, 300)
        for row in rows
        if row.runtime_eligible and clean_identifier(row.channel_name, 300)
    )

    # Shared channel-name aliases can point at more than one schedule. Iterate
    # in the same stable identity order used by the mapping hash so equal-
    # quality ties never depend on Google Sheet row order.
    for row in sorted(
        rows,
        key=lambda item: (
            item.server_id,
            stream_sort_key(item.stream_id),
            item.source_key.casefold(),
            item.source_key,
            item.epg_id.casefold(),
            item.epg_id,
        ),
    ):
        if not row.runtime_eligible:
            continue
        key = (row.source_key, row.epg_id)
        stats = schedule_stats.get(key)
        if stats is None or stats.programme_rows == 0:
            continue
        streams_with_programmes.add(row.stream_id)
        source_name, source_icon = source_channel_info(
            connection, row.source_key, row.epg_id
        )
        icon_url = (
            row.logo_url
            or configured_icon(icon_overrides, row)
            or source_icon
        )
        register(
            row.channel_name,
            [row.channel_name, row.canonical_name],
            row,
            icon_url,
            "channel_name",
        )
        if row.requested_source == "dummy" and channel_name_counts[
            clean_identifier(row.channel_name, 300)
        ] > 1:
            # Duplicate provider names cannot represent two per-stream guides
            # as one XMLTV ID. Preserve the shared name alias for existing
            # playlists and add a stable unique ID for each affected stream.
            register(
                row.schedule_key,
                [row.channel_name, row.canonical_name],
                row,
                icon_url,
                "synthetic_stream_id",
            )
        elif row.requested_source != "dummy":
            register(
                row.epg_id,
                [source_name, row.canonical_name, row.channel_name],
                row,
                icon_url,
                "canonical_epg_id",
            )
    return entries, streams_with_programmes, conflicts


def iter_schedule_rows(
    connection: sqlite3.Connection,
    source_key: str,
    channel_key: str,
    *,
    window_start: int,
    window_end: int | None,
) -> Iterator[tuple[int, int, str, str, str, str]]:
    upper_clause = "AND start_epoch < ?" if window_end is not None else ""
    parameters: list[Any] = [source_key, channel_key, int(window_start)]
    if window_end is not None:
        parameters.append(int(window_end))
    query = f"""
        SELECT start_epoch, stop_epoch, title, subtitle, description, categories_json
        FROM (
            SELECT start_epoch, stop_epoch, title, subtitle, description,
                   categories_json, quality,
                   ROW_NUMBER() OVER (
                     PARTITION BY start_epoch, stop_epoch
                     ORDER BY quality DESC, title COLLATE NOCASE, title
                   ) AS choice
            FROM programmes
            WHERE source_key = ? AND channel_key = ? AND stop_epoch > ?
              {upper_clause}
        )
        WHERE choice = 1
        ORDER BY start_epoch, stop_epoch, title COLLATE NOCASE, title
    """
    for row in connection.execute(query, parameters):
        yield (
            int(row[0]), int(row[1]), str(row[2]), str(row[3]),
            str(row[4]), str(row[5]),
        )


def xml_escape_text(value: object) -> str:
    return html.escape(clean_text(value, 10_000), quote=False)


def xml_escape_attr(value: object) -> str:
    return html.escape(clean_text(value, 2_000), quote=True)


def xml_escape_identifier(value: object) -> str:
    return html.escape(clean_identifier(value, 300), quote=True)


def deterministic_gzip_text(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    raw = temporary.open("wb")
    compressed = gzip.GzipFile(
        filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
    )
    text = io.TextIOWrapper(compressed, encoding="utf-8", newline="\n")
    return temporary, raw, compressed, text


def finish_deterministic_gzip(
    destination: Path,
    temporary: Path,
    raw: BinaryIO,
    compressed: gzip.GzipFile,
    text: io.TextIOWrapper,
) -> None:
    text.flush()
    text.detach()
    compressed.close()
    raw.flush()
    os.fsync(raw.fileno())
    raw.close()
    os.replace(temporary, destination)


def write_tivimate_xmltv(
    *,
    destination: Path,
    entries: Mapping[str, XmlEntry],
    connection: sqlite3.Connection,
    window_start: int,
) -> dict[str, Any]:
    temporary, raw, compressed, output = deterministic_gzip_text(destination)
    programme_rows = 0
    try:
        output.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        output.write(
            f'<tv generator-info-name="SKY TV Streaming EPG {PIPELINE_VERSION}">\n'
        )
        for channel_id in sorted(entries, key=lambda value: (value.casefold(), value)):
            entry = entries[channel_id]
            output.write(f'  <channel id="{xml_escape_identifier(channel_id)}">\n')
            if entry.icon_url:
                output.write(f'    <icon src="{xml_escape_attr(entry.icon_url)}"/>\n')
            for display_name in sorted(
                entry.display_names, key=lambda value: (value.casefold(), value)
            ):
                output.write(
                    f"    <display-name>{xml_escape_text(display_name)}</display-name>\n"
                )
            output.write("  </channel>\n")

        for channel_id in sorted(entries, key=lambda value: (value.casefold(), value)):
            entry = entries[channel_id]
            for start, stop, title, subtitle, description, categories_json in iter_schedule_rows(
                connection,
                entry.source_key,
                entry.channel_key,
                window_start=window_start,
                window_end=None,
            ):
                output.write(
                    f'  <programme start="{xmltv_timestamp(start)}" '
                    f'stop="{xmltv_timestamp(stop)}" '
                    f'channel="{xml_escape_identifier(channel_id)}">\n'
                )
                output.write(f"    <title>{xml_escape_text(title)}</title>\n")
                if subtitle:
                    output.write(
                        f"    <sub-title>{xml_escape_text(subtitle)}</sub-title>\n"
                    )
                if description:
                    output.write(f"    <desc>{xml_escape_text(description)}</desc>\n")
                try:
                    categories = json.loads(categories_json)
                except json.JSONDecodeError:
                    categories = []
                for category in categories:
                    output.write(
                        f"    <category>{xml_escape_text(category)}</category>\n"
                    )
                output.write("  </programme>\n")
                programme_rows += 1
        output.write("</tv>\n")
        finish_deterministic_gzip(
            destination, temporary, raw, compressed, output
        )
    except Exception:
        try:
            output.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    return {
        "channels": len(entries),
        "programmeRows": programme_rows,
        "canonicalEpgChannels": sum(
            1 for entry in entries.values() if entry.entry_type == "canonical_epg_id"
        ),
        "channelNameEntries": sum(
            1 for entry in entries.values() if entry.entry_type == "channel_name"
        ),
    }


def xmltv_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000"
    )


def resolve_row_icon(
    row: MappingRow,
    connection: sqlite3.Connection,
    overrides: Sequence[IconOverride],
) -> str:
    _source_name, source_icon = source_channel_info(
        connection, row.source_key, row.epg_id
    )
    return row.logo_url or configured_icon(overrides, row) or source_icon


def write_app_epg(
    *,
    destination: Path,
    rows: Sequence[MappingRow],
    connection: sqlite3.Connection,
    generated_at: int,
    window_start: int,
    window_end: int,
    icon_overrides: Sequence[IconOverride],
) -> dict[str, Any]:
    runtime_rows = sorted(
        (row for row in rows if row.runtime_eligible),
        key=lambda item: stream_sort_key(item.stream_id),
    )
    schedules: dict[str, tuple[str, str]] = {}
    for row in runtime_rows:
        schedules.setdefault(row.schedule_key, (row.source_key, row.epg_id))

    temporary, raw, compressed, output = deterministic_gzip_text(destination)
    programme_count = 0
    schedules_with_programmes = 0
    streams_with_programmes = 0
    available_schedule_keys: set[str] = set()
    try:
        output.write("{")
        scalar_fields = (
            ("schemaVersion", APP_SCHEMA_VERSION),
            ("serverId", rows[0].server_id),
            ("generatedAt", generated_at),
            ("windowStart", window_start),
            ("windowEnd", window_end),
        )
        first_field = True
        for key, value in scalar_fields:
            if not first_field:
                output.write(",")
            first_field = False
            output.write(json_compact(key) + ":" + json_compact(value))

        output.write(',"streamToEpg":{')
        for index, row in enumerate(runtime_rows):
            if index:
                output.write(",")
            output.write(json_compact(row.stream_id))
            output.write(":")
            output.write(json_compact(row.schedule_key))
        output.write("}")

        output.write(',"programmes":{')
        first_schedule = True
        for schedule_key in sorted(schedules, key=lambda value: (value.casefold(), value)):
            source_key, channel_key = schedules[schedule_key]
            iterator = iter_schedule_rows(
                connection,
                source_key,
                channel_key,
                window_start=window_start,
                window_end=window_end,
            )
            first_row = next(iterator, None)
            if first_row is None:
                continue
            if not first_schedule:
                output.write(",")
            first_schedule = False
            available_schedule_keys.add(schedule_key)
            schedules_with_programmes += 1
            output.write(json_compact(schedule_key) + ":[")
            for row_index, item in enumerate(chain((first_row,), iterator)):
                if row_index:
                    output.write(",")
                output.write(json_compact([item[0], item[1], item[2]]))
                programme_count += 1
            output.write("]")
        output.write("}")

        stream_logos: list[tuple[str, str]] = []
        for row in runtime_rows:
            logo = resolve_row_icon(row, connection, icon_overrides)
            if logo:
                stream_logos.append((row.stream_id, logo))
        output.write(',"streamLogos":{')
        for index, (stream_id, logo) in enumerate(stream_logos):
            if index:
                output.write(",")
            output.write(json_compact(stream_id) + ":" + json_compact(logo))
        output.write("}")
        output.write(
            ',"logoAttribution":'
            + json_compact(
                {
                    "mappingOverridesPreferred": True,
                    "source": "EPGShare channel metadata or approved exact override",
                    "policy": LOGO_POLICY,
                    "panelSourceIconsUsed": False,
                }
            )
        )
        output.write("}")
        finish_deterministic_gzip(
            destination, temporary, raw, compressed, output
        )
    except Exception:
        try:
            output.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise

    streams_with_programmes = sum(
        1 for row in runtime_rows if row.schedule_key in available_schedule_keys
    )
    return {
        "mappedStreams": len(runtime_rows),
        "streamsWithProgrammes": streams_with_programmes,
        "coveragePercent": round(
            streams_with_programmes * 100.0 / max(1, len(runtime_rows)), 2
        ),
        "uniqueMappedSchedules": len(schedules),
        "uniqueSchedulesWithProgrammes": schedules_with_programmes,
        "programmeRows": programme_count,
        "logoStreams": len(stream_logos),
    }


def write_metadata_json(
    *,
    destination: Path,
    rows: Sequence[MappingRow],
    connection: sqlite3.Connection,
    generated_at: int,
    mapping_sha256: str,
    icon_overrides: Sequence[IconOverride],
) -> dict[str, Any]:
    # Public metadata follows the same approval boundary as schedule output.
    # `enabled` alone is not approval: REVIEW/ignored rows and Server 1 native
    # panel placeholders must remain private even if a Sheet checkbox is set by
    # mistake.
    published_rows = sorted(
        (row for row in rows if row.runtime_eligible),
        key=lambda item: stream_sort_key(item.stream_id),
    )
    temporary, raw, compressed, output = deterministic_gzip_text(destination)
    eligible_count = 0
    try:
        output.write("{")
        output.write('"schemaVersion":1')
        output.write(',"metadataVersion":1')
        output.write(',"serverId":' + json_compact(rows[0].server_id))
        output.write(',"serverLabel":' + json_compact(rows[0].server_label))
        output.write(',"generatedAt":' + str(int(generated_at)))
        output.write(',"mappingSha256":' + json_compact(mapping_sha256))
        output.write(
            ',"filterSemantics":'
            + json_compact(
                {
                    "dimensions": "AND",
                    "valuesWithinDimension": "OR",
                    "unknownMatchesSpecific": False,
                    "parentalExclusionRunsFirst": True,
                    "minimumRecommendedConfidence": 0.70,
                    "requiredDimensionsMustBeEligible": True,
                }
            )
        )
        output.write(',"streamMetadata":{')
        for index, row in enumerate(published_rows):
            if index:
                output.write(",")
            metadata = metadata_dict(row.metadata)
            eligible_dimensions = personalization_dimensions(row.metadata)
            personalization_eligible = (
                row.metadata.status in {"approved", "auto"}
                and row.metadata.confidence >= 0.70
                and bool(eligible_dimensions)
            )
            if personalization_eligible:
                eligible_count += 1
            payload = {
                "channelName": row.channel_name,
                "canonicalName": row.canonical_name,
                "guideMode": (
                    "synthetic"
                    if row.requested_source == "dummy"
                    else ("panel" if row.effective_source == "panel" else "epgshare")
                ),
                "providerCategory": row.category_name,
                "categoryId": row.category_id,
                "channelNumber": row.channel_number,
                "sortPriority": row.sort_priority,
                "logoUrl": resolve_row_icon(row, connection, icon_overrides),
                "enabled": row.enabled,
                "personalizationEligible": personalization_eligible,
                "personalizationEligibleDimensions": eligible_dimensions,
                **metadata,
            }
            output.write(json_compact(row.stream_id) + ":" + json_compact(payload))
        output.write("}}")
        finish_deterministic_gzip(
            destination, temporary, raw, compressed, output
        )
    except Exception:
        try:
            output.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    return {
        "metadataStreams": len(published_rows),
        "personalizationEligibleStreams": eligible_count,
        "reviewStreams": sum(
            1
            for row in published_rows
            if row.metadata.status in {"review", "unknown"}
            or row.metadata.confidence < 0.70
        ),
    }


def personalization_dimensions(metadata: ChannelMetadata) -> list[str]:
    """Return confidently usable discovery dimensions for one channel.

    This deliberately does not require genre in order to expose a known
    language or region.  Every requested app filter still has to match its own
    concrete value, so a Punjabi-only preference can include a channel whose
    genre awaits review, while Punjabi + religion cannot.
    """
    dimensions: list[str] = []
    if metadata.region != "unknown":
        dimensions.append("region")
    if metadata.countries:
        dimensions.append("countries")
    if any(value not in {"und", "mul"} for value in metadata.languages):
        dimensions.append("languages")
    if metadata.genre != "unknown":
        dimensions.append("genre")
    if metadata.subgenres:
        dimensions.append("subgenres")
    if metadata.sports:
        dimensions.append("sports")
    if metadata.religions:
        dimensions.append("religions")
    if metadata.audiences:
        dimensions.append("audiences")
    if metadata.channel_role != "unknown":
        dimensions.append("channelRole")
    if metadata.tags:
        dimensions.append("tags")
    return dimensions


def validate_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as handle:
            for _chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                pass
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise BuildError(f"Generated gzip failed validation: {path.name}") from exc


def validate_xmltv(path: Path) -> dict[str, int]:
    reject_unsafe_xml_prefix(path)
    channel_ids: set[str] = set()
    programme_channels: set[str] = set()
    duplicate_channels: set[str] = set()
    programmes = 0
    root_name = ""
    try:
        with open_limited_xml(path, MAX_SOURCE_EXPANDED_BYTES) as source:
            context = etree.iterparse(
                source,
                events=("start", "end"),
                recover=False,
                huge_tree=False,
                load_dtd=False,
                no_network=True,
                resolve_entities=False,
                remove_comments=True,
                remove_pis=True,
            )
            for event, element in context:
                if event == "start" and not root_name:
                    root_name = local_name(element.tag)
                    reject_parsed_doctype(element)
                    continue
                if event != "end":
                    continue
                name = local_name(element.tag)
                if name == "channel":
                    channel_id = clean_identifier(element.get("id") or "", 300)
                    if channel_id in channel_ids:
                        duplicate_channels.add(channel_id)
                    channel_ids.add(channel_id)
                    release_top_level(element)
                elif name == "programme":
                    programmes += 1
                    channel_id = clean_identifier(element.get("channel") or "", 300)
                    programme_channels.add(channel_id)
                    start = parse_xmltv_time(element.get("start"))
                    stop = parse_xmltv_time(element.get("stop"))
                    if start is None or stop is None or stop <= start:
                        raise BuildError("Generated XMLTV contains invalid programme times.")
                    if not preferred_child_text(element, "title"):
                        raise BuildError("Generated XMLTV contains a blank programme title.")
                    release_top_level(element)
            del context
    except etree.XMLSyntaxError as exc:
        raise BuildError("Generated XMLTV is malformed.") from exc
    undeclared = programme_channels - channel_ids
    if root_name != "tv" or not channel_ids or not programmes:
        raise BuildError("Generated XMLTV has no usable channel/programme data.")
    if duplicate_channels:
        raise BuildError("Generated XMLTV contains duplicate channel IDs.")
    if undeclared:
        raise BuildError("Generated XMLTV programmes reference undeclared channels.")
    return {
        "channels": len(channel_ids),
        "programmeRows": programmes,
        "programmeChannelIds": len(programme_channels),
        "duplicateChannelIds": 0,
        "undeclaredProgrammeChannels": 0,
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(
            [spreadsheet_safe_cell(value) for value in row]
            for row in rows
        )
    os.replace(temporary, path)


def write_server_reports(
    *,
    report_dir: Path,
    rows: Sequence[MappingRow],
    connection: sqlite3.Connection,
    schedule_stats: Mapping[tuple[str, str], ScheduleStats],
    conflicts: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    missing_rows: list[list[Any]] = []
    for row in rows:
        if not row.runtime_eligible:
            continue
        stats = schedule_stats.get((row.source_key, row.epg_id))
        if stats is None or stats.programme_rows == 0:
            channel_found = connection.execute(
                "SELECT 1 FROM channels WHERE source_key = ? AND channel_key = ?",
                (row.source_key, row.epg_id),
            ).fetchone() is not None
            missing_rows.append(
                [
                    row.stream_id,
                    row.channel_name,
                    row.epg_id,
                    row.requested_source,
                    row.effective_source,
                    "yes" if channel_found else "no",
                    row.action,
                    "No retained programme rows",
                ]
            )
    write_csv(
        report_dir / f"{rows[0].server_id}_missing_epg_ids.csv",
        (
            "stream_id", "channel_name", "epg_id", "requested_source",
            "effective_source", "channel_found", "action", "reason",
        ),
        missing_rows,
    )
    review_rows = [
        row
        for row in rows
        if row.runtime_eligible
        and (
            row.metadata.status in {"review", "unknown"}
            or row.metadata.confidence < 0.70
        )
    ]
    write_csv(
        report_dir / f"{rows[0].server_id}_metadata_review.csv",
        (
            "stream_id", "channel_name", "category_name", "region_code",
            "genre", "primary_language", "sport_codes", "religion_codes",
            "metadata_status", "metadata_confidence",
        ),
        (
            (
                row.stream_id,
                row.channel_name,
                row.category_name,
                row.metadata.region,
                row.metadata.genre,
                row.metadata.primary_language,
                "|".join(row.metadata.sports),
                "|".join(row.metadata.religions),
                row.metadata.status,
                row.metadata.confidence,
            )
            for row in review_rows
        ),
    )
    write_csv(
        report_dir / f"{rows[0].server_id}_xmltv_id_conflicts.csv",
        (
            "channel_id", "kept_source", "kept_epg_id", "discarded_source",
            "discarded_epg_id", "resolution",
        ),
        (
            (
                item.get("channel_id", ""),
                item.get("kept_source", ""),
                item.get("kept_epg_id", ""),
                item.get("discarded_source", ""),
                item.get("discarded_epg_id", ""),
                item.get("resolution", ""),
            )
            for item in conflicts
        ),
    )
    return {
        "missingScheduleRows": len(missing_rows),
        "metadataReviewRows": len(review_rows),
        "xmltvIdConflicts": len(conflicts),
    }


def server_mapping_sha256(rows: Sequence[MappingRow]) -> str:
    return canonical_mapping_sha256(list(rows))


def build_server(
    *,
    server_id: str,
    rows: Sequence[MappingRow],
    connection: sqlite3.Connection,
    schedule_stats: Mapping[tuple[str, str], ScheduleStats],
    staging_public: Path,
    generated_at: int,
    xml_window_start: int,
    app_window_start: int,
    app_window_end: int,
    icon_overrides: Sequence[IconOverride],
    source_provenance: Mapping[str, Any],
    minimum_coverage: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    server_rows = [row for row in rows if row.server_id == server_id]
    if not server_rows:
        raise BuildError(f"No mapping rows found for {server_id}.")
    if server_id == "server_1" and any(
        row.effective_source == "panel" for row in server_rows
    ):
        raise BuildError("Internal policy violation: Server 1 resolved to panel EPG.")

    entries, streams_with_programmes, conflicts = build_xml_entries(
        rows=server_rows,
        connection=connection,
        schedule_stats=schedule_stats,
        icon_overrides=icon_overrides,
    )
    xml_path = staging_public / "epg" / f"{server_id}_tivimate.xml.gz"
    xml_result = write_tivimate_xmltv(
        destination=xml_path,
        entries=entries,
        connection=connection,
        window_start=xml_window_start,
    )
    xml_validation = validate_xmltv(xml_path)
    validate_gzip(xml_path)

    app_path = staging_public / "EPG" / f"{server_id}_epg.json.gz"
    app_result = write_app_epg(
        destination=app_path,
        rows=server_rows,
        connection=connection,
        generated_at=generated_at,
        window_start=app_window_start,
        window_end=app_window_end,
        icon_overrides=icon_overrides,
    )
    validate_gzip(app_path)
    metadata_path = staging_public / "EPG" / f"{server_id}_metadata.json.gz"
    mapping_sha = server_mapping_sha256(server_rows)
    metadata_result = write_metadata_json(
        destination=metadata_path,
        rows=server_rows,
        connection=connection,
        generated_at=generated_at,
        mapping_sha256=mapping_sha,
        icon_overrides=icon_overrides,
    )
    validate_gzip(metadata_path)

    if app_result["coveragePercent"] < float(minimum_coverage):
        raise BuildError(
            f"{server_id} coverage {app_result['coveragePercent']:.2f}% is below "
            f"the configured {float(minimum_coverage):.2f}% gate."
        )

    report_dir = staging_public / "reports" / server_id
    report_counts = write_server_reports(
        report_dir=report_dir,
        rows=server_rows,
        connection=connection,
        schedule_stats=schedule_stats,
        conflicts=conflicts,
    )
    mapped_rows = [row for row in server_rows if row.runtime_eligible]
    quarantined_server_1_panel_rows = sum(
        1
        for row in server_rows
        if row.source_policy_blocked
    )
    conflicting_slots = sum(
        schedule_stats.get((row.source_key, row.epg_id), ScheduleStats(0, 0, 0, 0, 0, 0)).conflicting_slots
        for row in {
            (item.source_key, item.epg_id): item for item in mapped_rows
        }.values()
    )
    overlapping_rows = sum(
        schedule_stats.get(
            (row.source_key, row.epg_id),
            ScheduleStats(0, 0, 0, 0, 0, 0),
        ).overlapping_rows
        for row in {
            (item.source_key, item.epg_id): item for item in mapped_rows
        }.values()
    )
    native_panel_used = any(row.effective_source == "panel" for row in mapped_rows)
    dummy_guide_streams = sum(
        1 for row in mapped_rows if row.requested_source == "dummy"
    )
    source_policy = (
        (
            "EPGSHARE_ALL_PLUS_CHANNEL_DERIVED"
            if dummy_guide_streams
            else "EPGSHARE_ALL_ONLY"
        )
        if server_id == "server_1"
        else (
            "MAPPING_SELECTED_PLUS_CHANNEL_DERIVED"
            if dummy_guide_streams
            else "MAPPING_SELECTED"
        )
    )
    used_source_keys = {row.source_key for row in mapped_rows}
    server_source_provenance = {
        key: value
        for key, value in source_provenance.items()
        if key in used_source_keys
    }
    if "synthetic" in source_provenance and any(
        row.runtime_eligible and row.requested_source == "dummy"
        for row in server_rows
    ):
        server_source_provenance["synthetic"] = source_provenance["synthetic"]
    unique_schedule_keys = {
        (row.source_key, row.epg_id) for row in mapped_rows
    }
    downloaded_schedule_keys = {
        key for key in unique_schedule_keys if not key[0].startswith("synthetic:")
    }
    generated_schedule_keys = unique_schedule_keys - downloaded_schedule_keys
    schedules_with_programmes = sum(
        1
        for key in unique_schedule_keys
        if schedule_stats.get(key) is not None
        and schedule_stats[key].programme_rows > 0
    )
    epgshare_feed_labels = sorted(
        {
            row.epg_feed or "ALL_SOURCES1"
            for row in mapped_rows
            if row.source_key == "epgshare01"
            and row.requested_source != "dummy"
        },
        key=lambda value: (value.casefold(), value),
    )
    refresh_plan = {
        "recommendedRefreshAt": utc_iso(generated_at + 24 * 3600),
        "policy": "DAILY_WORKFLOW_SCHEDULE",
    }
    manifest = {
        "schemaVersion": 2,
        "format": "XMLTV",
        "targetApp": "TiviMate",
        "builderVersion": PIPELINE_VERSION,
        "builderBuildId": PIPELINE_BUILD_ID,
        "serverId": server_id,
        "generatedAt": generated_at,
        "generatedAtIso": utc_iso(generated_at),
        "windowStart": xml_window_start,
        "windowStartIso": utc_iso(xml_window_start),
        "pastDaysRetained": round(
            (generated_at - xml_window_start) / 86400, 3
        ),
        "futurePolicy": "ALL_AVAILABLE_FROM_COMBINED_SOURCE",
        "windowEnd": None,
        "mappingSha256": mapping_sha,
        "sourcePolicy": source_policy,
        "nativePanelXmltvUsed": native_panel_used,
        "logoPolicy": LOGO_POLICY,
        "panelSourceIconsUsed": False,
        "combinedSourceDummyGuideStreams": dummy_guide_streams,
        "syntheticGuidePolicy": (
            server_source_provenance.get("synthetic", {}).get("titlePolicy", "")
        ),
        "syntheticGuideBlockHours": (
            server_source_provenance.get("synthetic", {}).get("blockHours")
        ),
        "syntheticEventWindowHours": (
            server_source_provenance.get("synthetic", {}).get("eventWindowHours")
        ),
        "server1LegacyPanelRowsQuarantined": quarantined_server_1_panel_rows,
        "dataFile": xml_path.name,
        "dataSha256": sha256_file(xml_path),
        "compressedBytes": xml_path.stat().st_size,
        "mappedStreams": len(mapped_rows),
        "mappedStreamsWithProgrammes": len(streams_with_programmes),
        "mappingProgrammeCoveragePercent": round(
            len(streams_with_programmes) * 100.0 / max(1, len(mapped_rows)), 2
        ),
        "xmltvChannels": xml_result["channels"],
        "channelNameEntries": xml_result["channelNameEntries"],
        "canonicalEpgIdEntries": xml_result["canonicalEpgChannels"],
        "programmeRows": xml_result["programmeRows"],
        "catalogStreams": len(server_rows),
        "downloadedUniqueSourceSchedulesRequested": len(downloaded_schedule_keys),
        "generatedSyntheticSchedules": len(generated_schedule_keys),
        "uniqueSourceSchedulesRequested": len(unique_schedule_keys),
        "uniqueSourceSchedulesWithProgrammes": schedules_with_programmes,
        "epgShareFeeds": epgshare_feed_labels,
        "effectiveEpgShareFeeds": epgshare_feed_labels,
        "refreshEpgShareFeeds": epgshare_feed_labels,
        "refreshPlan": refresh_plan,
        "trustedMappingUsed": True,
        "channelsWithIcons": sum(1 for entry in entries.values() if entry.icon_url),
        "iconCoveragePercent": round(
            sum(1 for entry in entries.values() if entry.icon_url)
            * 100.0
            / max(1, len(entries)),
            2,
        ),
        "sameSlotSourceConflicts": conflicting_slots,
        "overlappingProgrammeRows": overlapping_rows,
        "reportCounts": report_counts,
        "windowPolicy": {
            "tivimatePastDays": round((generated_at - xml_window_start) / 86400, 3),
            "tivimateFutureLimit": None,
            "appWindowStart": app_window_start,
            "appWindowEnd": app_window_end,
        },
        "sourceProvenance": server_source_provenance,
        "validation": xml_validation,
    }
    write_json(report_dir / f"{server_id}_tivimate_manifest.json", manifest)
    write_json(
        report_dir / f"{server_id}_validation.json",
        {
            "serverId": server_id,
            "xmltv": xml_validation,
            "app": app_result,
            "metadata": metadata_result,
            "reports": report_counts,
            "server1PolicyEnforced": (
                server_id != "server_1" or not native_panel_used
            ),
        },
    )

    app_manifest = {
        "schemaVersion": APP_SCHEMA_VERSION,
        "serverId": server_id,
        "generatedAt": generated_at,
        "generatedAtIso": utc_iso(generated_at),
        "windowStart": app_window_start,
        "windowEnd": app_window_end,
        "dataFile": app_path.name,
        "dataSha256": sha256_file(app_path),
        "mappingSha256": mapping_sha,
        "compressedBytes": app_path.stat().st_size,
        **app_result,
        "metadataFile": metadata_path.name,
        "metadataSha256": sha256_file(metadata_path),
        **metadata_result,
        "sourcePolicy": source_policy,
        "nativePanelXmltvUsed": native_panel_used,
        "logoPolicy": LOGO_POLICY,
        "panelSourceIconsUsed": False,
        "combinedSourceDummyGuideStreams": dummy_guide_streams,
        "syntheticGuidePolicy": (
            server_source_provenance.get("synthetic", {}).get("titlePolicy", "")
        ),
        "syntheticGuideBlockHours": (
            server_source_provenance.get("synthetic", {}).get("blockHours")
        ),
        "syntheticEventWindowHours": (
            server_source_provenance.get("synthetic", {}).get("eventWindowHours")
        ),
    }
    write_json(
        staging_public / "EPG" / f"{server_id}_epg_manifest.json",
        app_manifest,
    )
    write_json(
        staging_public / "EPG" / f"{server_id}_metadata_manifest.json",
        {
            "schemaVersion": METADATA_SCHEMA_VERSION,
            "serverId": server_id,
            "generatedAt": generated_at,
            "generatedAtIso": utc_iso(generated_at),
            "dataFile": metadata_path.name,
            "dataSha256": sha256_file(metadata_path),
            "compressedBytes": metadata_path.stat().st_size,
            "mappingSha256": mapping_sha,
            "logoPolicy": LOGO_POLICY,
            "panelSourceIconsUsed": False,
            **metadata_result,
        },
    )
    return manifest, app_manifest


def taxonomy_payload() -> dict[str, Any]:
    return {
        "schemaVersion": METADATA_SCHEMA_VERSION,
        "filterSemantics": {
            "dimensions": "AND",
            "valuesWithinDimension": "OR",
            "unknownMatchesSpecific": False,
            "parentalExclusionRunsFirst": True,
            "minimumRecommendedConfidence": 0.70,
            "requiredDimensionsMustBeEligible": True,
            "notes": (
                "Language never implies religion. multi_sport does not satisfy "
                "a specific cricket filter."
            ),
        },
        "eligibleDimensionFields": [
            "region",
            "countries",
            "languages",
            "genre",
            "subgenres",
            "sports",
            "religions",
            "audiences",
            "channelRole",
            "tags",
        ],
        "policyFields": ["contentRating"],
        "controlledValues": {
            "regions": sorted(REGIONS),
            "genres": sorted(GENRES),
            "sports": sorted(SPORTS),
            "religions": sorted(RELIGIONS),
            "audiences": sorted(AUDIENCES),
            "contentRatings": sorted(CONTENT_RATINGS),
            "channelRoles": sorted(CHANNEL_ROLES),
        },
        "exampleRecipes": {
            "punjabi_religious": {
                "languages": ["pa"],
                "genre": ["religion"],
            },
            "punjabi_sikh": {
                "languages": ["pa"],
                "genre": ["religion"],
                "religions": ["sikhism"],
            },
            "cricket_channels": {
                "genre": ["sports"],
                "sports": ["cricket"],
            },
        },
    }


def lexical_absolute_path(path: Path) -> Path:
    """Return an absolute normalized path without following symbolic links."""
    return Path(os.path.abspath(os.fspath(path)))


def reject_symlink_components(path: Path, *, label: str) -> None:
    """Fail closed if an existing component could redirect a destructive path."""
    candidate = lexical_absolute_path(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BuildError(f"The {label} contains a symbolic link: {current}")


def safe_reset_directory(path: Path, *, repository_root: Path) -> None:
    reject_symlink_components(path, label="directory selected for cleanup")
    resolved = lexical_absolute_path(path).resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), repository_root.resolve()}
    if resolved in forbidden or len(resolved.parts) < 4:
        raise BuildError(f"Refusing to clear unsafe directory: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def validate_build_paths(
    *, work_dir: Path, public_dir: Path, repository_root: Path
) -> None:
    """Reject output paths that could erase repository source or each other."""
    repository_root = repository_root.resolve()
    lexical_work = lexical_absolute_path(work_dir)
    lexical_public = lexical_absolute_path(public_dir)
    lexical_staging = lexical_public.with_name(lexical_public.name + ".staging")
    for candidate, label in (
        (lexical_work, "work directory"),
        (lexical_public, "public directory"),
        (lexical_staging, "staging directory"),
    ):
        reject_symlink_components(candidate, label=label)

    work_dir = lexical_work.resolve()
    public_dir = lexical_public.resolve()
    staging = lexical_staging.resolve()

    if work_dir.is_relative_to(repository_root):
        allowed_work = repository_root / ".build" / "work"
        if work_dir != allowed_work and not work_dir.is_relative_to(allowed_work):
            raise BuildError(
                "A work directory inside the repository must be .build/work "
                "or one of its descendants."
            )
    if public_dir.is_relative_to(repository_root):
        if public_dir != repository_root / "public":
            raise BuildError(
                "A public directory inside the repository must be exactly public."
            )
    for candidate, label in (
        (work_dir, "work directory"),
        (public_dir, "public directory"),
        (staging, "staging directory"),
    ):
        if candidate == repository_root or repository_root.is_relative_to(candidate):
            raise BuildError(f"The {label} cannot be the repository or its ancestor.")

    candidates = (work_dir, public_dir, staging)
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if (
                left == right
                or left.is_relative_to(right)
                or right.is_relative_to(left)
            ):
                raise BuildError("Work, public, and staging directories must not overlap.")


def atomic_publish(
    staging: Path, destination: Path, *, repository_root: Path
) -> None:
    reject_symlink_components(staging, label="validated staging directory")
    reject_symlink_components(destination, label="public destination")
    staging = lexical_absolute_path(staging).resolve()
    destination = lexical_absolute_path(destination).resolve()
    repository_root = repository_root.resolve()
    backup = destination.with_name(destination.name + ".previous")
    reject_symlink_components(backup, label="public backup directory")
    if not staging.is_dir():
        raise BuildError("Validated staging directory is missing.")
    if destination in {Path("/").resolve(), Path.home().resolve()}:
        raise BuildError("Refusing to replace an unsafe public directory.")
    if destination.is_relative_to(repository_root):
        if destination != repository_root / "public":
            raise BuildError("Refusing to publish over repository source files.")
    if destination == repository_root or repository_root.is_relative_to(destination):
        raise BuildError("Refusing to publish over the repository or its ancestor.")
    if backup.exists():
        shutil.rmtree(backup)
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(staging, destination)
    except Exception:
        if moved_old and not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def parse_panel_files(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise BuildError("--panel-file must use server_2=/path/to/file form.")
        server_text, path_text = value.split("=", 1)
        server_id = normalize_server_id(server_text)
        if server_id == "server_1":
            raise BuildError("Server 1 panel XMLTV is forbidden by source policy.")
        path = Path(path_text).resolve()
        if not path.is_file():
            raise BuildError(f"Panel XMLTV fixture does not exist for {server_id}.")
        result[server_id] = path
    return result


def parse_server_minimums(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise BuildError(
                "--minimum-server-rows must use server_1=4000 form."
            )
        server_text, minimum_text = value.split("=", 1)
        server_id = normalize_server_id(server_text)
        try:
            minimum = int(minimum_text)
        except ValueError as exc:
            raise BuildError(
                f"Minimum row count for {server_id} must be an integer."
            ) from exc
        if minimum < 1 or minimum > MAX_MAPPING_ROWS:
            raise BuildError(
                f"Minimum row count for {server_id} must be 1..{MAX_MAPPING_ROWS}."
            )
        if server_id in result:
            raise BuildError(f"Duplicate minimum row count for {server_id}.")
        result[server_id] = minimum
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mapping = parser.add_mutually_exclusive_group()
    mapping.add_argument("--mapping-url", default="")
    mapping.add_argument("--mapping-file", type=Path)
    parser.add_argument(
        "--mapping-authoritative-file",
        type=Path,
        help=(
            "Final authoritative private Sheet snapshot before OPEN-alert "
            "quarantine. Required with row floors in production."
        ),
    )
    parser.add_argument(
        "--mapping-snapshot-manifest",
        type=Path,
        help="Hash-bound manifest for the authoritative/effective snapshot pair.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--all-source-url", default=DEFAULT_ALL_SOURCE_URL)
    source.add_argument("--all-source-file", type=Path)
    parser.add_argument(
        "--all-source-file-origin-url",
        default="",
        help=(
            "Caller-declared download origin for --all-source-file. The "
            "builder records this as declared, not as a host it observed."
        ),
    )
    parser.add_argument(
        "--epgshare-spool-file",
        type=Path,
        help=(
            "Reuse a sealed one-pass EPGShare selection spool. This requires "
            "--all-source-file so the source SHA-256 can be verified."
        ),
    )
    parser.add_argument("--public-dir", type=Path, default=Path("public"))
    parser.add_argument("--work-dir", type=Path, default=Path(".build/work"))
    parser.add_argument("--servers", nargs="+", default=list(DEFAULT_SERVERS))
    parser.add_argument(
        "--server-1-policy",
        choices=("epgshare-only",),
        default="epgshare-only",
    )
    parser.add_argument("--icon-config", type=Path, default=Path("config/channel_icons.csv"))
    parser.add_argument("--public-base-url", default=os.environ.get("EPG_PUBLIC_BASE_URL", ""))
    parser.add_argument("--panel-file", action="append", default=[])
    parser.add_argument(
        "--minimum-server-rows",
        action="append",
        default=[],
        metavar="SERVER=COUNT",
        help="Fail if the private mapping snapshot unexpectedly loses runnable server rows.",
    )
    parser.add_argument("--past-days", type=int, default=3)
    parser.add_argument("--app-past-hours", type=int, default=6)
    parser.add_argument("--app-future-hours", type=int, default=72)
    parser.add_argument(
        "--synthetic-block-hours",
        type=int,
        default=DEFAULT_SYNTHETIC_BLOCK_HOURS,
        help=(
            "Duration of channel-derived placeholder guide blocks (4..24, "
            "subject to the bounded programme-row cap; "
            f"default: {DEFAULT_SYNTHETIC_BLOCK_HOURS})."
        ),
    )
    parser.add_argument(
        "--synthetic-future-days",
        type=int,
        default=DEFAULT_SYNTHETIC_FUTURE_DAYS,
        help=(
            "Future coverage for channel-derived placeholder guides (1..14; "
            f"default: {DEFAULT_SYNTHETIC_FUTURE_DAYS})."
        ),
    )
    parser.add_argument(
        "--synthetic-event-window-hours",
        type=int,
        default=DEFAULT_SYNTHETIC_EVENT_WINDOW_HOURS,
        help=(
            "Bounded presentation window around an exact-time synthetic event "
            f"(1..24; default: {DEFAULT_SYNTHETIC_EVENT_WINDOW_HOURS})."
        ),
    )
    parser.add_argument("--minimum-coverage", type=float, default=0.0)
    parser.add_argument("--now-epoch", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-expanded-bytes",
        type=int,
        default=MAX_SOURCE_EXPANDED_BYTES,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def inferred_github_pages_base_url() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    match = re.fullmatch(
        r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
        r"([A-Za-z0-9._-]{1,100})",
        repository,
    )
    if match is None:
        return ""
    owner, name = match.groups()
    origin = f"https://{owner}.github.io"
    if name.casefold() == f"{owner}.github.io".casefold():
        return origin
    return f"{origin}/{name}"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    selected_servers = {normalize_server_id(value) for value in args.servers}
    if not selected_servers:
        raise BuildError("At least one server is required.")
    if args.past_days < 0 or args.app_past_hours < 0 or args.app_future_hours <= 0:
        raise BuildError("Guide windows must be non-negative and future hours positive.")
    if not 4 <= args.synthetic_block_hours <= 24:
        raise BuildError("--synthetic-block-hours must be between 4 and 24.")
    if not 1 <= args.synthetic_future_days <= 14:
        raise BuildError("--synthetic-future-days must be between 1 and 14.")
    if not 1 <= args.synthetic_event_window_hours <= 24:
        raise BuildError("--synthetic-event-window-hours must be between 1 and 24.")
    if not 0.0 <= args.minimum_coverage <= 100.0:
        raise BuildError("--minimum-coverage must be between 0 and 100.")
    if args.epgshare_spool_file and not args.all_source_file:
        raise BuildError(
            "--epgshare-spool-file requires --all-source-file for provenance "
            "verification."
        )
    if args.all_source_file_origin_url and not args.all_source_file:
        raise BuildError(
            "--all-source-file-origin-url requires --all-source-file."
        )

    validate_build_paths(
        work_dir=args.work_dir,
        public_dir=args.public_dir,
        repository_root=repository_root,
    )
    work_dir = lexical_absolute_path(args.work_dir).resolve()
    public_dir = lexical_absolute_path(args.public_dir).resolve()
    staging_public = public_dir.with_name(public_dir.name + ".staging")
    safe_reset_directory(work_dir, repository_root=repository_root)
    safe_reset_directory(staging_public, repository_root=repository_root)
    (staging_public / "epg").mkdir(parents=True, exist_ok=True)
    (staging_public / "EPG").mkdir(parents=True, exist_ok=True)
    (staging_public / "reports").mkdir(parents=True, exist_ok=True)
    (staging_public / ".nojekyll").write_text("", encoding="utf-8")

    generated_at = int(args.now_epoch or datetime.now(timezone.utc).timestamp())
    xml_window_start = generated_at - int(args.past_days) * 86400
    app_window_start = generated_at - int(args.app_past_hours) * 3600
    app_window_end = generated_at + int(args.app_future_hours) * 3600

    mapping_content = mapping_bytes_from_args(args, work_dir)
    rows = parse_mapping_csv(mapping_content, selected_servers)
    minimum_rows = parse_server_minimums(args.minimum_server_rows)
    unknown_minimums = sorted(set(minimum_rows) - selected_servers)
    if unknown_minimums:
        raise BuildError(
            "Row-count gates were provided for unselected servers: "
            + ", ".join(unknown_minimums)
        )
    pair_arguments = (
        args.mapping_authoritative_file is not None,
        args.mapping_snapshot_manifest is not None,
    )
    if any(pair_arguments) and not all(pair_arguments):
        raise BuildError(
            "Use --mapping-authoritative-file and --mapping-snapshot-manifest together."
        )
    if all(pair_arguments) and args.mapping_file is None:
        raise BuildError(
            "Private snapshot-pair validation requires --mapping-file."
        )
    if minimum_rows and not all(pair_arguments):
        raise BuildError(
            "--minimum-server-rows requires the authoritative private snapshot "
            "and its snapshot manifest."
        )

    snapshot_validation: dict[str, Any] | None = None
    if all(pair_arguments):
        authoritative_content = bounded_private_file_bytes(
            args.mapping_authoritative_file,
            label="Authoritative private mapping snapshot",
            maximum_bytes=MAX_MAPPING_BYTES,
        )
        snapshot_validation = validate_private_mapping_snapshots(
            authoritative_content,
            mapping_content,
            selected_servers,
        )
        manifest_content = bounded_private_file_bytes(
            args.mapping_snapshot_manifest,
            label="Private mapping snapshot manifest",
            maximum_bytes=MAX_MAPPING_SNAPSHOT_MANIFEST_BYTES,
        )
        parse_and_validate_mapping_snapshot_manifest(
            manifest_content,
            snapshot_validation,
        )
        for server_id in sorted(selected_servers):
            stats = snapshot_validation["servers"][server_id]
            print(
                f"Snapshot guard {server_id}: "
                f"{stats['effective_runnable_rows']:,} output-eligible; "
                f"{stats['quarantined_authoritative_runnable_rows']:,} safely "
                "excluded by OPEN alerts; "
                f"{stats['authoritative_runnable_rows']:,} integrity rows.",
                flush=True,
            )
        server_row_counts = Counter(
            {
                server_id: stats["authoritative_runnable_rows"]
                for server_id, stats in snapshot_validation["servers"].items()
            }
        )
    else:
        server_row_counts = Counter(
            row.server_id for row in rows if row.runtime_eligible
        )
    below_minimum = [
        f"{server_id}={server_row_counts.get(server_id, 0):,} "
        f"(minimum {minimum:,})"
        for server_id, minimum in sorted(minimum_rows.items())
        if server_row_counts.get(server_id, 0) < minimum
    ]
    if below_minimum:
        raise BuildError(
            "Private authoritative mapping snapshot failed the runnable-row "
            "truncation guard: "
            + "; ".join(below_minimum)
        )
    mapping_sha = canonical_mapping_sha256(rows)
    print(
        f"Loaded {len(rows):,} mapping rows for {len(selected_servers)} server(s); "
        f"canonical SHA-256 {mapping_sha}.",
        flush=True,
    )
    if any(
        row.server_id == "server_1" and row.effective_source == "panel"
        for row in rows
    ):
        raise BuildError("Server 1 panel policy assertion failed.")

    database_path = work_dir / "spool" / "selected_epg.sqlite3"
    connection = create_database(database_path)
    source_provenance: dict[str, Any] = {}
    source_stats: dict[str, SourceStats] = {}
    try:
        wanted_epgshare = {
            row.epg_id
            for row in rows
            if row.runtime_eligible and row.source_key == "epgshare01"
        }
        if args.all_source_file:
            all_source_path = Path(args.all_source_file).resolve()
            if not all_source_path.is_file():
                raise BuildError("--all-source-file does not exist.")
            declared_origin_url = ""
            declared_origin_host = ""
            if args.all_source_file_origin_url:
                declared_origin_url, declared_origin_host = (
                    declared_epgshare_file_origin(
                        args.all_source_file_origin_url
                    )
                )
            source_url = declared_origin_url
            # A declared origin is provenance only.  Do not let an unobserved
            # URL change how relative data inside the provided file is used.
            source_icon_base_url = ""
            source_details = {
                "sha256": sha256_file(all_source_path),
                "bytes": all_source_path.stat().st_size,
                "etag": "",
                "lastModified": "",
                # This builder did not perform the HTTP exchange, so it cannot
                # truthfully name a final redirect host.  The source bytes are
                # still bound to the spool by the SHA-256 above.
                "finalHost": "",
                "finalHostObservedByBuilder": False,
                "inputMode": (
                    "pre-downloaded-file"
                    if declared_origin_url
                    else "local-fixture"
                ),
                "originEvidence": (
                    "caller-declared"
                    if declared_origin_url
                    else "none"
                ),
                "declaredOriginHost": declared_origin_host,
            }
        else:
            source_url = str(args.all_source_url or DEFAULT_ALL_SOURCE_URL).strip()
            source_icon_base_url = urljoin(source_url, ".")
            all_source_path, source_details = safe_download(
                source_url,
                work_dir / "downloads" / "epg_ripper_ALL_SOURCES1.xml.gz",
                maximum_bytes=MAX_SOURCE_COMPRESSED_BYTES,
                expected_gzip=True,
                allowed_host_suffixes=("epgshare01.online",),
            )
            source_details.update(
                {
                    "finalHostObservedByBuilder": True,
                    "inputMode": "builder-download",
                    "originEvidence": "builder-observed",
                    "declaredOriginHost": "",
                }
            )
        spool_manifest: Mapping[str, str] = {}
        if args.epgshare_spool_file:
            print(
                "Validating and reusing the one-pass ALL_SOURCES1 selection spool "
                f"for {len(wanted_epgshare):,} unique IDs.",
                flush=True,
            )
            connection.close()
            try:
                verified_spool = copy_and_open_verified_spool(
                    source=Path(args.epgshare_spool_file).resolve(),
                    destination=database_path,
                    expected_source_sha256=str(source_details["sha256"]),
                    expected_source_bytes=int(source_details["bytes"]),
                    required_ids=wanted_epgshare,
                    required_window_start=xml_window_start,
                    maximum_expanded_bytes=args.max_expanded_bytes,
                    consumer_epoch=generated_at,
                )
            except SpoolError as exc:
                raise BuildError(str(exc)) from exc
            connection = verified_spool.connection
            spool_manifest = verified_spool.manifest
            spool_stats = verified_spool.stats
            selected_programmes = int(spool_stats.get("selected_programmes", 0))
            stored_programmes = int(spool_stats.get("stored_programmes", 0))
            all_stats = SourceStats(
                source_key="epgshare01",
                compressed_bytes=int(source_details["bytes"]),
                expanded_bytes=int(spool_stats.get("expanded_bytes", 0)),
                total_elements=int(spool_stats.get("total_elements", 0)),
                channel_elements=int(spool_stats.get("channel_elements", 0)),
                unique_channel_ids=int(spool_stats.get("unique_channel_ids", 0)),
                duplicate_channel_ids=int(spool_stats.get("duplicate_channel_ids", 0)),
                programme_elements=int(spool_stats.get("programme_elements", 0)),
                selected_channels=int(spool_stats.get("stored_channels", 0)),
                selected_programmes=selected_programmes,
                duplicate_programmes=max(0, selected_programmes - stored_programmes),
                invalid_start=int(spool_stats.get("invalid_start", 0)),
                synthesized_stop=int(spool_stats.get("synthesized_stop", 0)),
                blank_title=int(spool_stats.get("blank_title", 0)),
                outside_window=int(spool_stats.get("outside_window", 0)),
            )
        else:
            print(
                f"Parsing ALL_SOURCES1 once for {len(wanted_epgshare):,} unique IDs.",
                flush=True,
            )
            all_stats = ingest_xmltv_source(
                connection=connection,
                path=all_source_path,
                source_key="epgshare01",
                wanted_ids=wanted_epgshare,
                window_start=xml_window_start,
                source_base_url=source_icon_base_url,
                maximum_expanded_bytes=args.max_expanded_bytes,
            )
        source_stats["epgshare01"] = all_stats
        source_provenance["epgshare01"] = {
            **source_details,
            **all_stats.public_dict(),
            "url": public_url_without_query(source_url) or "local-fixture",
            "selectionSpoolReused": bool(args.epgshare_spool_file),
            **(
                {
                    "selectionSpoolSchemaVersion": spool_manifest[
                        "schema_version"
                    ],
                    "selectionSpoolCatalogSha256": spool_manifest[
                        "catalog_sha256"
                    ],
                    "selectionSpoolLogicalSha256": spool_manifest[
                        "logical_sha256"
                    ],
                }
                if spool_manifest
                else {}
            ),
        }

        panel_files = parse_panel_files(args.panel_file)
        for server_id in sorted(selected_servers):
            if server_id == "server_1":
                continue
            wanted_panel = {
                row.epg_id
                for row in rows
                if row.server_id == server_id
                and row.runtime_eligible
                and row.effective_source == "panel"
            }
            if not wanted_panel:
                continue
            if server_id in panel_files:
                panel_path = panel_files[server_id]
                panel_details = {
                    "sha256": sha256_file(panel_path),
                    "bytes": panel_path.stat().st_size,
                }
            else:
                panel_path, panel_details = download_panel_xmltv(
                    server_id,
                    work_dir / "downloads" / f"{server_id}_panel.xmltv",
                )
            panel_key = f"panel:{server_id}"
            print(f"Parsing native panel XMLTV for {server_id}.", flush=True)
            panel_stats = ingest_xmltv_source(
                connection=connection,
                path=panel_path,
                source_key=panel_key,
                wanted_ids=wanted_panel,
                window_start=xml_window_start,
                # Mapping/config URLs are preferred. Relative panel icons are
                # intentionally ignored, and absolute source icons are disabled,
                # so a private panel origin or query token is never copied into
                # a public manifest or output URL.
                source_base_url="",
                allow_source_icons=False,
                allow_inert_xmltv_doctype=True,
                maximum_expanded_bytes=args.max_expanded_bytes,
            )
            source_stats[panel_key] = panel_stats
            source_provenance[panel_key] = {
                **panel_details,
                **panel_stats.public_dict(),
            }

        synthetic_stats = insert_synthetic_guides(
            connection=connection,
            rows=rows,
            window_start=xml_window_start,
            window_end=max(
                app_window_end,
                generated_at + int(args.synthetic_future_days) * 86400,
            ),
            block_hours=args.synthetic_block_hours,
            event_window_hours=args.synthetic_event_window_hours,
            reference_epoch=generated_at,
        )
        if synthetic_stats.schedules:
            print(
                "Generated channel-derived placeholder guides for "
                f"{synthetic_stats.schedules:,} streams in "
                f"{synthetic_stats.programme_rows:,} "
                f"{synthetic_stats.block_hours}-hour blocks.",
                flush=True,
            )
            source_provenance["synthetic"] = {
                "inputMode": "generated",
                "titlePolicy": "CHANNEL_DERIVED_NO_INVENTED_PROGRAMME_DETAILS",
                "blockHours": synthetic_stats.block_hours,
                "eventWindowHours": synthetic_stats.event_window_hours,
                "futureDays": int(args.synthetic_future_days),
                "windowStart": synthetic_stats.window_start,
                "windowEnd": synthetic_stats.window_end,
            }

        connection.execute(
            "CREATE INDEX IF NOT EXISTS programme_emit_idx ON programmes "
            "(source_key, channel_key, start_epoch, stop_epoch)"
        )
        connection.commit()
        schedule_stats = load_schedule_stats(connection, generated_at)

        requested_public_base = str(args.public_base_url or "").strip()
        public_base_url = valid_http_url(requested_public_base)
        if requested_public_base and not public_base_url:
            raise BuildError(
                "--public-base-url must be an HTTP(S) URL without credentials, "
                "a query, or a fragment."
            )
        public_base_url = public_base_url or inferred_github_pages_base_url()
        icon_overrides = load_icon_overrides(
            args.icon_config.resolve() if args.icon_config else None,
            repository_root=repository_root,
            staging_public=staging_public,
            public_base_url=public_base_url,
        )
        tivimate_builds: list[dict[str, Any]] = []
        app_builds: list[dict[str, Any]] = []
        for server_id in sorted(selected_servers):
            print(f"Writing and validating {server_id} outputs.", flush=True)
            manifest, app_manifest = build_server(
                server_id=server_id,
                rows=rows,
                connection=connection,
                schedule_stats=schedule_stats,
                staging_public=staging_public,
                generated_at=generated_at,
                xml_window_start=xml_window_start,
                app_window_start=app_window_start,
                app_window_end=app_window_end,
                icon_overrides=icon_overrides,
                source_provenance=source_provenance,
                minimum_coverage=args.minimum_coverage,
            )
            tivimate_builds.append(
                {
                    "serverId": server_id,
                    # Legacy index fields remain stable for existing clients.
                    "file": f"epg/{server_id}_tivimate.xml.gz",
                    "sha256": manifest["dataSha256"],
                    "compressedBytes": manifest["compressedBytes"],
                    "dataFile": f"{server_id}_tivimate.xml.gz",
                    "manifestFile": f"../reports/{server_id}/{server_id}_tivimate_manifest.json",
                    "generatedAtIso": manifest["generatedAtIso"],
                    "mappedStreams": manifest["mappedStreams"],
                    "mappedStreamsWithProgrammes": manifest[
                        "mappedStreamsWithProgrammes"
                    ],
                    "coveragePercent": manifest[
                        "mappingProgrammeCoveragePercent"
                    ],
                    "programmeRows": manifest["programmeRows"],
                    "recommendedRefreshAt": manifest["refreshPlan"].get(
                        "recommendedRefreshAt"
                    ),
                    "builderVersion": manifest["builderVersion"],
                    "builderBuildId": manifest["builderBuildId"],
                    "channelsWithIcons": manifest["channelsWithIcons"],
                    "iconCoveragePercent": manifest["iconCoveragePercent"],
                    "sourcePolicy": manifest["sourcePolicy"],
                    "nativePanelXmltvUsed": manifest["nativePanelXmltvUsed"],
                }
            )
            app_builds.append(
                {
                    "serverId": server_id,
                    "dataFile": f"EPG/{app_manifest['dataFile']}",
                    "manifestFile": f"EPG/{server_id}_epg_manifest.json",
                    "metadataFile": f"EPG/{app_manifest['metadataFile']}",
                    "generatedAtIso": app_manifest["generatedAtIso"],
                    "mappedStreams": app_manifest["mappedStreams"],
                    "streamsWithProgrammes": app_manifest[
                        "streamsWithProgrammes"
                    ],
                    "coveragePercent": app_manifest["coveragePercent"],
                    "programmeRows": app_manifest["programmeRows"],
                }
            )

        write_json(staging_public / "EPG" / "taxonomy.json", taxonomy_payload())
        write_json(
            staging_public / "epg" / "index.json",
            {
                "schemaVersion": 1,
                "pipelineVersion": PIPELINE_VERSION,
                "generatedAt": generated_at,
                "generatedAtIso": utc_iso(generated_at),
                "generatedAtUtc": utc_iso(generated_at),
                "targetApp": "TVMeta / TiviMate",
                "matcherPolicy": "APPROVED_MAPPINGS_ONLY_NO_AUTOMATIC_REMATCH",
                "mappingSource": "PRIVATE_GOOGLE_SHEET_EPHEMERAL_SNAPSHOT",
                "mappingSha256": mapping_sha,
                "builds": tivimate_builds,
            },
        )
        write_json(
            staging_public / "EPG" / "index.json",
            {
                "schemaVersion": 1,
                "pipelineVersion": PIPELINE_VERSION,
                "generatedAt": generated_at,
                "generatedAtIso": utc_iso(generated_at),
                "mappingPolicy": "APPROVED_MAPPING_ROWS_ONLY_NO_AUTOMATIC_REMATCH",
                "mappingSource": "PRIVATE_GOOGLE_SHEET_EPHEMERAL_SNAPSHOT",
                "mappingSha256": mapping_sha,
                "taxonomyFile": "EPG/taxonomy.json",
                "builds": app_builds,
            },
        )
        write_json(
            staging_public / "health.json",
            {
                "status": "ok",
                "pipelineVersion": PIPELINE_VERSION,
                "generatedAt": generated_at,
                "generatedAtIso": utc_iso(generated_at),
                "generatedAtUtc": utc_iso(generated_at),
                "serverCount": len(selected_servers),
                "servers": len(selected_servers),
                "mappingSha256": mapping_sha,
            },
        )
        landing = (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<title>SKY TV EPG</title><h1>SKY TV EPG</h1>"
            f"<p>Streaming pipeline {PIPELINE_VERSION}; generated "
            f"{html.escape(str(utc_iso(generated_at)))}</p>"
            '<p><a href="epg/index.json">TiviMate index</a> · '
            '<a href="EPG/index.json">App index</a></p></html>\n'
        )
        (staging_public / "index.html").write_text(landing, encoding="utf-8")

        private_reports = work_dir.parent / "reports"
        private_reports.mkdir(parents=True, exist_ok=True)
        write_json(
            private_reports / "source-provenance.json",
            {
                "generatedAtIso": utc_iso(generated_at),
                "mappingSha256": mapping_sha,
                "sources": source_provenance,
            },
        )
        write_csv(
            private_reports / "combined-source-conflicts.csv",
            (
                "source_key", "epg_id", "programme_slots", "future_slots",
                "latest_stop_utc", "same_start_stop_different_title_slots",
                "overlapping_programme_rows",
            ),
            (
                (
                    source_key,
                    channel_key,
                    stats.programme_rows,
                    stats.future_rows,
                    utc_iso(stats.latest_stop),
                    stats.conflicting_slots,
                    stats.overlapping_rows,
                )
                for (source_key, channel_key), stats in sorted(schedule_stats.items())
                if stats.conflicting_slots or stats.overlapping_rows
            ),
        )
    finally:
        connection.close()

    atomic_publish(
        staging_public,
        public_dir,
        repository_root=repository_root,
    )
    print(
        f"Published {len(selected_servers)} validated server build(s) to {public_dir}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
