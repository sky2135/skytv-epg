"""Smart Rules v8 contextual channel-to-EPG resolver.

This module is a new decision engine layered over the proven v7 catalog and
builder primitives.  It does not alter the v7 algorithms in place.  Instead it
parses provider names into channel identity, market, language, edition and
technical metadata; uses deterministic exact indexes and approved knowledge;
and invokes selected v7 rules only as verified compatibility resolvers.

Fuzzy similarity is deliberately advisory: a fuzzy-only decision is always
returned as REVIEW, never as an automatic assignment.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from lxml import etree
from rapidfuzz import fuzz, process

SMART_RULES_V8_VERSION = "8.4"
MATCHER_V8_BUILD_ID = "SKYTV-CONTEXTUAL-RULES-8.4-2026-07-19"

TECHNICAL_TOKENS_V8 = {
    "hd", "fhd", "uhd", "sd", "4k", "8k", "hevc", "h265", "h264",
    "1080p", "1080i", "720p", "2160p", "50fps", "60fps", "vip",
    "backup", "raw", "test", "multi", "low", "hq", "lq", "source",
    "digital", "mono", "stereo",
}
OPTIONAL_DESCRIPTOR_TOKENS_V8 = {"tv", "television", "channel", "network", "the"}
MEANINGFUL_DESCRIPTOR_TOKENS_V8 = {
    "gold", "news", "music", "movie", "movies", "cinema", "plus", "extra",
    "xtra", "alternate", "alt", "east", "west", "pacific", "local",
    "kids", "junior", "sports", "sport", "cricket", "religious", "classic",
    "action", "comedy", "family", "mystery", "science", "history",
}
LANGUAGE_MAP_V8 = {
    "pb": "punjabi", "pun": "punjabi", "punj": "punjabi", "punjab": "punjabi",
    "punjabi": "punjabi", "panjabi": "punjabi",
    "my": "malayalam", "mal": "malayalam", "ml": "malayalam", "malayalam": "malayalam",
    "tm": "tamil", "tam": "tamil", "tamil": "tamil",
    "tg": "telugu", "tel": "telugu", "telugu": "telugu",
    "guj": "gujarati", "gujrati": "gujarati", "gujarati": "gujarati",
    "kan": "kannada", "kn": "kannada", "kand": "kannada", "kannada": "kannada",
    "ori": "odia", "od": "odia", "odia": "odia", "odiya": "odia", "oriya": "odia",
    "mar": "marathi", "marathi": "marathi",
    "bn": "bengali", "bangla": "bengali", "bengali": "bengali",
    "hindi": "hindi", "hin": "hindi", "urdu": "urdu",
    "assam": "assamese", "assamese": "assamese",
    "english": "english", "eng": "english", "en": "english",
    "spanish": "spanish", "espanol": "spanish",
    "french": "french", "fr": "french", "arabic": "arabic", "ar": "arabic",
    "pjb": "punjabi", "mly": "malayalam", "gjr": "gujarati",
    "mrt": "marathi", "tlg": "telugu", "bgl": "bengali",
    "bho": "bhojpuri", "bhoj": "bhojpuri",
    "tl": "telugu",
}
SOUTH_ASIAN_LANGUAGES_V8 = {
    "punjabi", "malayalam", "tamil", "telugu", "gujarati", "kannada",
    "odia", "marathi", "bengali", "hindi", "urdu", "assamese",
}

# Short provider/category labels. These labels are metadata only when they are
# isolated as a prefix/segment; the same letters inside a channel brand remain.
WRAPPER_CODES_V8 = {
    "pb", "mal", "tm", "tl", "tg", "guj", "kan", "kn", "ori", "cri", "cric",
    "airtel", "d2h", "ts", "at", "tata", "tata play", "tata sky",
    "dish tv", "videocon d2h", "skytv", "sky", "live",
    "ukhd", "ukfhd", "uksd", "ukuhd", "uk4k", "uk hd", "uk fhd", "uk sd",
    "in news", "in guj", "in gujarati", "in punjabi", "in tamil", "in telugu",
    "in malayalam", "in kannada", "in marathi", "in bangla", "in bengali",
    "ca fr", "us spanish", "us english", "latin", "religious", "ph",
    "eng", "sports", "sport", "pjb", "mly", "gjr", "mrt", "tlg", "bgl", "tam",
    "bho", "bhoj",
}
AMBIGUOUS_WRAPPER_CODES_V8 = {"my", "bd"}
WRAPPER_COMPACT_CODES_V8 = {
    re.sub(r"[^a-z0-9]+", "", value.casefold())
    for value in WRAPPER_CODES_V8 | AMBIGUOUS_WRAPPER_CODES_V8
}
WRAPPER_QUALITY_SUFFIX_RE_V8 = re.compile(r"(?:fhd|uhd|hd|sd|4k|8k)$", re.I)

# Shared lexical canonicalization. These are identity-equivalence rules, not
# channel-specific mappings: they normalize common singular/plural, spelling and
# EPG-abbreviation forms before any candidate is considered.
TOKEN_CANONICAL_V82 = {
    "sport": "sports", "movie": "movies", "film": "movies", "films": "movies",
    "color": "colors", "colour": "colors", "colours": "colors",
    "television": "tv", "sci": "science",
}

# Smart Rules v8.4 uses a provider-taxonomy grammar and token-multiset identity.
# These are linguistic/structural equivalences, not complete channel-name rules.
IDENTITY_TOKEN_CANONICAL_V84 = {
    "punjabi": "punjab", "panjabi": "punjab", "punjab": "punjab",
    "times": "time", "chardikalah": "chardikala", "chardikla": "chardikala",
    "hp": "himachal", "inter": "international",
    "tune": "tunes", "tunes": "tunes",
}

CATEGORY_NAMESPACE_CODES_V84 = {
    "AS", "SA", "AR", "UK", "EU", "NA", "AF", "ES", "AM",
    "ALB", "BLN", "DE", "PT", "FR", "GR", "PL", "TH", "IT", "MT",
}

CATEGORY_PREFIX_CODES_V84 = {
    "english": {"eng"}, "sports": {"sports", "sport", "sp"},
    "punjabi": {"pjb", "pb", "pun", "punj"},
    "malayalam": {"mly", "mal", "ml", "my"},
    "bengali": {"bgl", "bn", "bd"}, "bangla": {"bgl", "bn", "bd"},
    "gujarati": {"gjr", "guj"}, "marathi": {"mrt", "mar"},
    "telugu": {"tlg", "tg", "tl"}, "tamil": {"tam", "tm"},
    "kannada": {"kan", "kn"}, "odia": {"ori", "od"},
    "assam": {"asm"}, "assamese": {"asm"}, "bhojpuri": {"bho", "bhoj"},
}

CATEGORY_DIRECT_MARKETS_V84 = {
    "UK": "UK", "FR": "FR", "ES": "ES", "DE": "DE", "PT": "PT",
    "ALB": "AL", "BLN": "EXYU", "GR": "GR", "PL": "PL",
    "TH": "TH", "IT": "IT", "MT": "MT",
}

CATEGORY_COUNTRY_MARKETS_V84: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(?:usa|united states|america)\b", re.I), "US", "United States"),
    (re.compile(r"\bcanada\b", re.I), "CA", "Canada"),
    (re.compile(r"\bfrance|french\b", re.I), "FR", "France"),
    (re.compile(r"\bbelgium|belgique|belgie\b", re.I), "BE", "Belgium"),
    (re.compile(r"\bhungary|hungaria|hungarian\b", re.I), "HU", "Hungary"),
    (re.compile(r"\bdenmark|danish\b", re.I), "DK", "Denmark"),
    (re.compile(r"\blatvia|latvian\b", re.I), "LV", "Latvia"),
    (re.compile(r"\bswiss|switzerland\b", re.I), "CH", "Switzerland"),
    (re.compile(r"\blithuania|lithuanian\b", re.I), "LT", "Lithuania"),
    (re.compile(r"\bukraine|ukrainian\b", re.I), "UA", "Ukraine"),
    (re.compile(r"\bcyprus|cypriot\b", re.I), "CY", "Cyprus"),
    (re.compile(r"\bvietnam|vietnamese\b", re.I), "VN", "Vietnam"),
    (re.compile(r"\bsouth korea|korea|korean\b", re.I), "KR", "South Korea"),
    (re.compile(r"\bhong ?kong\b", re.I), "HK", "Hong Kong"),
    (re.compile(r"\barmenia|armenian\b", re.I), "AM", "Armenia"),
    (re.compile(r"\bkurdistan|kurdish\b", re.I), "KURD", "Kurdistan"),
    (re.compile(r"\bjapan|japanese\b", re.I), "JP", "Japan"),
    (re.compile(r"\bmexico|mexican\b", re.I), "MX", "Mexico"),
    (re.compile(r"\bcolombia|colombian\b", re.I), "CO", "Colombia"),
    (re.compile(r"\bperu|peruvian\b", re.I), "PE", "Peru"),
    (re.compile(r"\bchile|chilean\b", re.I), "CL", "Chile"),
    (re.compile(r"\becuador|ecuadorian\b", re.I), "EC", "Ecuador"),
    (re.compile(r"\buruguay|uruguayan\b", re.I), "UY", "Uruguay"),
    (re.compile(r"\bserbia|serbian\b", re.I), "RS", "Serbia"),
    (re.compile(r"\bcroatia|croatian\b", re.I), "HR", "Croatia"),
    (re.compile(r"\bbosnia|bosnian\b", re.I), "BA", "Bosnia"),
    (re.compile(r"\bmacedonia|macedonian\b", re.I), "MK", "Macedonia"),
    (re.compile(r"\bslovenia|slovenian\b", re.I), "SI", "Slovenia"),
    (re.compile(r"\bnigeria|nigerian\b", re.I), "NG", "Nigeria"),
    (re.compile(r"\bghana|ghanaian\b", re.I), "GH", "Ghana"),
    (re.compile(r"\bcameroon|cameroonian\b", re.I), "CM", "Cameroon"),
    (re.compile(r"\bsomalia|somali\b", re.I), "SO", "Somalia"),
    (re.compile(r"\bmali|malian\b", re.I), "ML", "Mali"),
)

INDIA_CATEGORY_TOPIC_TOKENS_V84 = {
    "india", "indian", "english", "sports", "sport", "punjabi", "punjab",
    "malayalam", "bangla", "bengali", "marathi", "gujarati", "tamil",
    "telugu", "kannada", "odia", "oriya", "bhojpuri", "assam", "assamese",
}

VIRTUAL_BANK_MARKERS_V84 = {
    "360", "movies", "cinema", "box", "office", "store", "premiere",
    "premium", "play", "ppv", "event", "events", "slot", "slots",
    "feed", "feeds", "multiscreen", "multi",
}
MOVIE_BANK_MARKERS_V84 = {"movies", "cinema", "box", "office", "store", "premiere"}
DECORATIVE_EDGE_CHARS_V84 = "#*=~_─━═•▪▫◆◇"

# A short wrapper may be removed when it is an isolated prefix token. Brand-like
# words such as SKY are intentionally excluded here, although they can still be
# metadata when isolated by a pipe or colon (``SKY | Channel``).
LEADING_TOKEN_WRAPPERS_V82 = {
    "pb", "mal", "tm", "tl", "tg", "guj", "kan", "kn", "ori", "cri", "cric",
    "airtel", "d2h", "ts", "at", "tata", "my", "bd",
    "eng", "sports", "sport", "pjb", "mly", "gjr", "mrt", "tlg", "bgl", "tam",
    "bho", "bhoj",
}

# Geographic/context descriptors that may safely be extra in a provider name
# when a more specific catalog identity is otherwise an exact token subset.
# This powers families such as News18 Punjab/Haryana/Himachal without a row-level
# exception and is used only with uniqueness and brand-safety checks.
REGIONAL_CONTEXT_TOKENS_V82 = {
    "punjab", "punjabi", "haryana", "himachal", "pradesh", "hp", "jammu",
    "kashmir", "gujarat", "gujarati", "rajasthan", "uttar", "uttarakhand",
    "madhya", "chhattisgarh", "bihar", "jharkhand", "assam", "odisha", "odia",
    "bengal", "bangla", "bengali", "maharashtra", "marathi", "karnataka",
    "kannada", "kerala", "malayalam", "tamil", "telugu", "andhra", "telangana",
    "delhi", "mumbai", "india", "indian",
}

# Category-level market ontology. A strong unsupported category is routed to its
# own region and blocked before fuzzy/cross-country matching. The model can be
# extended by adding a feed with the same region code; no matcher rewrite is
# required when coverage is later added.
CATEGORY_MARKET_RULES_V82: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(?:mbc\s+arabic|arabic\s+news|arabic|islamic)\b", re.I), "MENA", "Arabic/Islamic general programming"),
    (re.compile(r"\bisrael(?:i)?\b", re.I), "IL", "Israel"),
    (re.compile(r"\bnorway|norwegian\b", re.I), "NO", "Norway"),
    (re.compile(r"\bfinland|finnish\b", re.I), "FI", "Finland"),
    (re.compile(r"\balgeria|algerian\b", re.I), "DZ", "Algeria"),
    (re.compile(r"\blebanon|lebanese\b", re.I), "LB", "Lebanon"),
    (re.compile(r"\bitaly|italian\b", re.I), "IT", "Italy"),
    (re.compile(r"\biraq|iraqi\b", re.I), "IQ", "Iraq"),
    (re.compile(r"\bqatar|qatari\b", re.I), "QA", "Qatar"),
    (re.compile(r"\begypt|egyptian\b", re.I), "EG", "Egypt"),
    (re.compile(r"\brussia|russian\b", re.I), "RU", "Russia"),
    (re.compile(r"\b(?:ex[ -]?yu|exyu|yugoslav(?:ia|ian)?)\b", re.I), "EXYU", "Ex-Yugoslavia"),
    (re.compile(r"\bromania|romanian\b", re.I), "RO", "Romania"),
    (re.compile(r"\bbrasil|brazil|brazilian\b", re.I), "BR", "Brazil"),
    (re.compile(r"\bnetherlands|dutch\b", re.I), "NL", "Netherlands"),
    (re.compile(r"\bsweden|swedish\b", re.I), "SE", "Sweden"),
    (re.compile(r"\bczech(?:ia| republic)?\b", re.I), "CZ", "Czechia"),
    (re.compile(r"\bbulgaria|bulgarian\b", re.I), "BG", "Bulgaria"),
    (re.compile(r"\bindonesia|indonesian\b", re.I), "ID", "Indonesia"),
    (re.compile(r"\bsri\s+lanka|sri\s+lankan|ceylon\b", re.I), "LK", "Sri Lanka"),
    (re.compile(r"\biran|iranian|persian\b", re.I), "IR", "Iran"),
    (re.compile(r"\bafghanistan|afghan\b", re.I), "AF", "Afghanistan"),
)

MARKET_PREFIX_ALIASES_V82 = {
    "il": "IL", "no": "NO", "fi": "FI", "dz": "DZ", "lb": "LB", "it": "IT",
    "iq": "IQ", "qa": "QA", "eg": "EG", "ru": "RU", "exyu": "EXYU",
    "ex yu": "EXYU", "ro": "RO", "br": "BR", "nl": "NL", "se": "SE",
    "cz": "CZ", "bg": "BG", "id": "ID", "lk": "LK", "sl": "LK", "ir": "IR", "af": "AF",
}

BRACKET_METADATA_WORDS_V82 = {
    "english", "eng", "en", "audio", "multi", "multiaudio", "dubbed", "original",
    "feed", "source", "backup", "raw", "vip", "test",
}

GENERIC_SLOT_TOKENS_V82 = {
    "movie", "movies", "cinema", "film", "films", "sports", "sport", "event", "events",
    "channel", "channels", "stream", "streams", "slot", "slots", "feed", "feeds",
    "live", "ppv", "tv", "network", "playlist", "radio", "music",
}

NUMBER_WORDS_V8 = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
}

PHRASE_REPAIRS_V8: tuple[tuple[str, str], ...] = (
    (r"\bchakde\b", "chak de"),
    (r"\bdisc\s+(science|history|turbo)\b", r"discovery \1"),
    (r"\bdisc\s+sci\b", "discovery science"),
    (r"\bs\s+west\b", "south west"),
    (r"\bn\s+west\b", "north west"),
    (r"\bs\s+east\b", "south east"),
    (r"\bn\s+east\b", "north east"),
    (r"\bfox sport\b", "fox sports"),
    (r"\bnews 18\b", "news18"),
    (r"\bhome and garden television\b", "hgtv"),
    (r"\boprah winfrey network\b", "own"),
    (r"\bnational geographic\b", "nat geo"),
    (r"\bthe discovery channel\b|\bdiscovery channel\b", "discovery"),
    (r"\bsony entertainment television\b", "sony tv"),
    (r"\bfilm rise\b", "filmrise"),
    (r"\bscreen pix\b", "screenpix"),
    (r"\bcine max\b", "cinemax"),
    (r"\bcity tv\b", "citytv"),
    (r"\b3 abn\b", "3abn"),
    (r"\bcp 24\b", "cp24"),
    (r"\bbe in\b", "bein"),
    (r"\bsports net\b", "sportsnet"),
    (r"\bredzone\b", "red zone"),
    (r"\bfan duel\b", "fanduel"),
    (r"\bbally sports(?: network)?\b", "fanduel sports"),
    (r"\bfanduel sports network\b", "fanduel sports"),
    (r"\bb 4 u\b", "b4u"),
    (r"\benter+r 10\b", "enter 10"),
    (r"\b(?:bazar|bazaar)\b", "bajar"),
    (r"\basmitha\b", "asmita"),
    (r"\bchard(?:h)?ikla\b|\bchardikala\b", "chardikala"),
    (r"\baakaash\b", "akash"),
    (r"\bsuvana\b", "suvarna"),
    (r"\bgujrat\b", "gujarat"),
    (r"\bcheannl\b|\bnetwrk\b", "network"),
    (r"\bid investigation discovery\b", "investigation discovery"),
)

MARKET_SEGMENTS_V8 = {
    "us": "US", "usa": "US", "u s": "US", "united states": "US",
    "america": "US", "north america": "NA_DIASPORA",
    "ca": "CA", "can": "CA", "canada": "CA", "toronto": "CA", "to": "CA",
    "uk": "UK", "gb": "UK", "united kingdom": "UK", "britain": "UK",
    "in": "IN", "india": "IN", "inr": "IN",
    "pk": "PK", "pakistan": "PK",
    "bein": "BEIN",
}
MARKET_SEGMENTS_V8.update(MARKET_PREFIX_ALIASES_V82)
MARKET_SEGMENTS_V8.update({
    "fr": "FR", "es": "ES", "de": "DE", "pt": "PT", "it": "IT",
    "gr": "GR", "pl": "PL", "th": "TH", "mt": "MT", "eu": "EU",
    "ar": "MENA", "sa": "LATAM", "af": "AFRICA", "alb": "AL",
    "bln": "EXYU", "jp": "JP", "japan": "JP",
})

CONTENT_GROUPS_V8 = {
    "news": {"news"},
    "music": {"music"},
    "movies": {"movie", "movies", "cinema", "film", "films"},
    "sports": {"sport", "sports", "cricket", "football", "soccer", "tennis", "golf", "racing"},
    "kids": {"kids", "children", "junior", "jr"},
    "religious": {"religious", "religion", "faith", "devotional"},
    "science": {"science"},
}

DEFAULT_APPROVED_ALIASES_V8: tuple[dict[str, Any], ...] = (
    {
        "alias": "ptc chak de", "regions": ("IN",),
        "epg_ids": ("PTC.CHAK.DE.in", "PTC.Chak.De.in", "PTC.NEWS.in", "PTC.News.in"),
        "relationship": "verified_identity_with_news_fallback",
        "note": "Prefer the dedicated PTC Chak De schedule; use PTC News only if the dedicated ID disappears.",
    },
    {
        "alias": "discovery turbo", "regions": ("UK",),
        "epg_ids": ("Discovery.HD.uk",),
        "timeshift_epg_ids": {"1": ("Discovery+1.uk",)},
        "relationship": "verified_schedule_relationship",
        "note": "Provider's Discovery Turbo stream uses the main Discovery UK schedule; the +1 stream preserves the same offset.",
    },
    {
        "alias": "tv9 news", "regions": ("IN",),
        "required_languages": ("gujarati",),
        "epg_ids": ("VIP.News.in",),
        "relationship": "verified_provider_schedule_relationship",
        "note": "Gujarati provider label confirmed to use the VIP News schedule.",
    },
)


@dataclass(frozen=True)
class ChannelContextV8:
    original_name: str
    category_name: str
    core_name: str
    strict_key: str
    relaxed_key: str
    compact_key: str
    edition_key: str
    identity_keys: tuple[str, ...]
    bag_key: str
    context_bag_key: str
    category_namespace: str
    category_topic: str
    category_market: str
    quality: str
    tokens: tuple[str, ...]
    languages: frozenset[str]
    content: frozenset[str]
    numbers: frozenset[str]
    direction: str
    has_plus: bool
    has_extra: bool
    has_alternate: bool
    timeshift: str
    wrapper_tokens: tuple[str, ...]
    explicit_market: str
    route_plan: tuple[str, ...]
    route_reason: str
    route_explicit: bool
    south_asian_context: bool


@dataclass(frozen=True)
class CandidateContextV8:
    candidate: Mapping[str, str]
    strict_key: str
    relaxed_key: str
    compact_key: str
    edition_key: str
    identity_keys: tuple[str, ...]
    bag_key: str
    quality: str
    tokens: tuple[str, ...]
    languages: frozenset[str]
    content: frozenset[str]
    numbers: frozenset[str]
    direction: str
    has_plus: bool
    has_extra: bool
    has_alternate: bool
    timeshift: str
    family_signature: tuple[Any, ...]


@dataclass
class V8PreparedState:
    fingerprint: str = ""
    index_builds: int = 0
    index_reuses: int = 0
    by_region: dict[str, list[CandidateContextV8]] = field(default_factory=dict)
    strict_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    relaxed_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    compact_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    edition_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    identity_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    bag_index: dict[str, dict[str, list[CandidateContextV8]]] = field(default_factory=dict)
    candidate_keys: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduleFingerprintV8:
    """Compact future-schedule signature for one XMLTV channel ID."""

    epg_id: str
    entries: tuple[tuple[int, str], ...]
    programme_count: int
    first_start: int | None
    last_stop: int | None




def _nfkc(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def _ascii_fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", _nfkc(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _basic_words_v8(value: object) -> list[str]:
    text = _ascii_fold(value)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    # Preserve a distinct +1 timeshift token before treating + as the word plus.
    text = re.sub(r"\+\s*(\d+)", r" plusshift\1 ", text)
    text = text.replace("&", " and ").replace("+", " plus ")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).casefold()
    for pattern, replacement in PHRASE_REPAIRS_V8:
        text = re.sub(pattern, replacement, text)
    raw_words = [
        TOKEN_CANONICAL_V82.get(NUMBER_WORDS_V8.get(word, word), NUMBER_WORDS_V8.get(word, word))
        for word in text.split()
    ]
    words: list[str] = []
    i = 0
    while i < len(raw_words):
        if i + 1 < len(raw_words) and raw_words[i].isdigit() and raw_words[i + 1] in {"k", "p", "i", "fps"}:
            words.append(raw_words[i] + raw_words[i + 1])
            i += 2
            continue
        if i + 1 < len(raw_words) and raw_words[i] == "h" and raw_words[i + 1] in {"264", "265"}:
            words.append("h" + raw_words[i + 1])
            i += 2
            continue
        words.append(raw_words[i])
        i += 1
    return words


def _acronym_signature_v82(words: Sequence[str]) -> str:
    """Return a compact brand acronym while preserving meaningful numbers."""
    significant = [
        word for word in words
        if word not in OPTIONAL_DESCRIPTOR_TOKENS_V8
        and word not in {"and", "of", "for"}
        and word not in TECHNICAL_TOKENS_V8
    ]
    letters = "".join(word[0] for word in significant if word and word[0].isalpha())
    numbers = "".join(word for word in significant if word.isdigit())
    return f"{letters}{numbers}" if letters else numbers


def _identity_keys_v82(value: object) -> tuple[str, ...]:
    """Build equivalent canonical keys without discarding meaningful editions.

    Besides strict and optional-descriptor forms, this removes a redundant
    acronym prefix or suffix when it describes the adjacent full brand. For
    example, ``FS1 Fox Sports 1`` and ``Fox Sports 1 (FS1)`` both expose the
    canonical identity ``fox sports 1``.
    """
    strict = _normalize_key_v8(value)
    relaxed = _normalize_key_v8(value, remove_optional=True)
    variants: list[str] = [strict, relaxed]
    words = strict.split()
    if len(words) >= 3:
        max_span = min(3, len(words) - 1)
        for span in range(1, max_span + 1):
            prefix, suffix = words[:span], words[span:]
            if "".join(prefix) == _acronym_signature_v82(suffix):
                variants.extend((" ".join(suffix), " ".join(
                    word for word in suffix if word not in OPTIONAL_DESCRIPTOR_TOKENS_V8
                )))
            prefix2, suffix2 = words[:-span], words[-span:]
            if "".join(suffix2) == _acronym_signature_v82(prefix2):
                variants.extend((" ".join(prefix2), " ".join(
                    word for word in prefix2 if word not in OPTIONAL_DESCRIPTOR_TOKENS_V8
                )))
    return tuple(dict.fromkeys(value for value in variants if value))


def _is_redundant_bracket_metadata_v82(content: str, outside: str, category: str) -> bool:
    words = _basic_words_v8(content)
    if not words:
        return True
    token_set = set(words)
    if token_set <= TECHNICAL_TOKENS_V8:
        return True
    if token_set <= BRACKET_METADATA_WORDS_V82:
        return True
    if all(word in LANGUAGE_MAP_V8 or word in BRACKET_METADATA_WORDS_V82 for word in words):
        return True
    # Parenthetical aliases such as (FS1) are metadata only when their compact
    # form is the acronym of the surrounding full identity.
    outside_words = [
        word for word in _basic_words_v8(outside)
        if word not in TECHNICAL_TOKENS_V8
    ]
    compact = "".join(words)
    if compact and compact == _acronym_signature_v82(outside_words):
        return True
    # Single provider annotation letters are never channel identity.
    if len(words) == 1 and re.fullmatch(r"[a-z]{1,3}\d?", words[0]) and words[0] in {
        "a", "b", "c", "d", "e", "f", "f2", "fl", "h", "l", "r", "s", "x", "cx", "pc"
    }:
        return True
    return False


def _strip_redundant_brackets_v82(raw: str, category: str) -> str:
    """Remove technical/language/acronym brackets while preserving editions."""
    text = str(raw or "")
    pattern = re.compile(r"(\([^()]{1,40}\)|\[[^\[\]]{1,40}\])")
    for _ in range(4):
        changed = False
        pieces: list[str] = []
        cursor = 0
        for match in pattern.finditer(text):
            content = match.group(0)[1:-1].strip()
            outside = f"{text[:match.start()]} {text[match.end():]}"
            if _is_redundant_bracket_metadata_v82(content, outside, category):
                pieces.append(text[cursor:match.start()])
                cursor = match.end()
                changed = True
        if not changed:
            break
        pieces.append(text[cursor:])
        text = " ".join(pieces)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_key_v8(value: object, *, remove_optional: bool = False) -> str:
    words = [w for w in _basic_words_v8(value) if w not in TECHNICAL_TOKENS_V8]
    if remove_optional:
        words = [w for w in words if w not in OPTIONAL_DESCRIPTOR_TOKENS_V8]
    # Collapse immediately repeated provider names, e.g. GAME+ Game Plus.
    if len(words) % 2 == 0 and words[: len(words) // 2] == words[len(words) // 2 :]:
        words = words[: len(words) // 2]
    return " ".join(words)


def _metadata_key_v8(value: object) -> str:
    return _normalize_key_v8(value, remove_optional=False)


def _market_from_segment_v8(segment: str) -> str:
    key = _metadata_key_v8(segment)
    key = re.sub(r"\b(?:hd|fhd|uhd|sd|4k)\b", "", key)
    key = re.sub(r"\s+", " ", key).strip()
    if key in MARKET_SEGMENTS_V8:
        return MARKET_SEGMENTS_V8[key]
    if key in {"us in", "usa in", "us india", "usa india", "us indian", "can us", "usa canada"}:
        return "NA_DIASPORA"
    if key in {"in to", "india to", "in toronto", "india toronto"}:
        return "CA"
    if re.fullmatch(r"uk\s*(?:hd|fhd|uhd|sd|4k)?", key.replace(" ", "")):
        return "UK"
    return ""


def _languages_from_text_v8(value: object, *, category: str = "") -> set[str]:
    words = _basic_words_v8(value)
    result: set[str] = set()
    category_key = _normalize_key_v8(category)
    for word in words:
        if word == "my" and "malaysia" in category_key:
            continue
        if word == "bd" and not re.search(r"\b(?:bangla|bengali|bangladesh)\b", category_key):
            continue
        language = LANGUAGE_MAP_V8.get(word)
        if language:
            result.add(language)
    if re.search(r"\b(?:bangla|bengali|bangladesh)\b", category_key):
        result.add("bengali")
    return result


def _content_from_tokens_v8(tokens: Iterable[str]) -> set[str]:
    token_set = set(tokens)
    result: set[str] = set()
    for label, words in CONTENT_GROUPS_V8.items():
        if token_set & words:
            result.add(label)
    return result


def _identity_bag_key_v84(
    value: object, *, remove_tokens: Iterable[str] = ()
) -> str:
    """Order-insensitive exact identity while preserving token multiplicity."""
    removed = {str(token).casefold() for token in remove_tokens}
    words = []
    for word in _basic_words_v8(value):
        if word in TECHNICAL_TOKENS_V8 or word in OPTIONAL_DESCRIPTOR_TOKENS_V8:
            continue
        word = IDENTITY_TOKEN_CANONICAL_V84.get(word, word)
        if word in removed:
            continue
        words.append(word)
    return " ".join(sorted(words))


LANGUAGE_IDENTITY_TOKENS_V84: dict[str, frozenset[str]] = {
    "punjabi": frozenset({"punjab", "punjabi", "panjabi"}),
    "malayalam": frozenset({"malayalam"}),
    "tamil": frozenset({"tamil"}),
    "telugu": frozenset({"telugu"}),
    "gujarati": frozenset({"gujarati", "gujrat"}),
    "kannada": frozenset({"kannada"}),
    "odia": frozenset({"odia", "oriya", "odiya"}),
    "marathi": frozenset({"marathi"}),
    "bengali": frozenset({"bengali", "bangla"}),
    "hindi": frozenset({"hindi"}),
    "urdu": frozenset({"urdu"}),
    "assamese": frozenset({"assam", "assamese"}),
    "bhojpuri": frozenset({"bhojpuri"}),
    "english": frozenset({"english", "eng", "en"}),
    "french": frozenset({"french", "fr"}),
}

REGIONAL_CANONICAL_TOKENS_V84 = frozenset(
    IDENTITY_TOKEN_CANONICAL_V84.get(token, token)
    for token in REGIONAL_CONTEXT_TOKENS_V82 | {"hp"}
)


def _remove_language_tokens_from_bag_v84(
    bag_key: str, languages: Iterable[str]
) -> str:
    removed: set[str] = set()
    for language in languages:
        removed.update(
            IDENTITY_TOKEN_CANONICAL_V84.get(token, token)
            for token in LANGUAGE_IDENTITY_TOKENS_V84.get(str(language).casefold(), ())
        )
    return " ".join(token for token in bag_key.split() if token not in removed)


def _single_token_orthographic_variant_v84(
    query_key: str, target_key: str
) -> tuple[str, str, float] | None:
    """Return one conservative spelling/transliteration difference.

    The complete token multisets must otherwise be identical. This converts only
    near-exact spelling variants into deterministic matches; it is not a fuzzy
    channel-family resolver.
    """
    q_counter = Counter(query_key.split())
    t_counter = Counter(target_key.split())
    common = q_counter & t_counter
    q_only = list((q_counter - common).elements())
    t_only = list((t_counter - common).elements())
    if len(q_only) != 1 or len(t_only) != 1:
        return None
    q_word, t_word = q_only[0], t_only[0]
    if q_word.isdigit() or t_word.isdigit():
        return None
    if q_word in MEANINGFUL_DESCRIPTOR_TOKENS_V8 or t_word in MEANINGFUL_DESCRIPTOR_TOKENS_V8:
        return None
    common_count = sum(common.values())
    if common_count < 1 or (common_count == 1 and len(next(iter(common.elements()), "")) < 4):
        return None
    if min(len(q_word), len(t_word)) < 5 or abs(len(q_word) - len(t_word)) > 2:
        return None
    if q_word[0] != t_word[0]:
        return None
    ratio = float(fuzz.ratio(q_word, t_word))
    if ratio < 84.0:
        return None
    return q_word, t_word, ratio


def _parse_category_context_v84(category_name: str) -> dict[str, Any]:
    raw = _nfkc(category_name).strip()
    segments = [part.strip() for part in re.split(r"\|+", raw) if part.strip()]
    namespace = ""
    if segments and segments[0].upper() in CATEGORY_NAMESPACE_CODES_V84:
        namespace = segments.pop(0).upper()
    topic = " | ".join(segments).strip() if segments else raw.strip(" |")
    topic_key = _normalize_key_v8(topic)
    topic_tokens = set(topic_key.split())
    languages = _languages_from_text_v8(topic, category=topic)
    content = _content_from_tokens_v8(topic_tokens)

    market = ""
    strength = 0
    reason = ""
    if namespace == "NA":
        if re.search(r"\b(?:usa|us|united states)\b", topic_key):
            market, strength, reason = "US", 3, "provider taxonomy indicates United States"
        elif "canada" in topic_tokens:
            market, strength, reason = "CA", 3, "provider taxonomy indicates Canada"
        else:
            market, strength, reason = "NA", 3, "provider taxonomy indicates unsupported North-American scope"
    elif namespace == "UK":
        market, strength, reason = "UK", 3, "provider taxonomy indicates United Kingdom"
    elif namespace == "AS":
        if languages & SOUTH_ASIAN_LANGUAGES_V8 or topic_tokens & INDIA_CATEGORY_TOPIC_TOKENS_V84:
            market, strength, reason = "IN", 3, "provider taxonomy indicates the India/South-Asian package"
        else:
            for pattern, code, label in CATEGORY_COUNTRY_MARKETS_V84:
                if pattern.search(topic):
                    market, strength, reason = code, 3, f"provider taxonomy indicates {label}"
                    break
            if not market:
                market, strength, reason = "ASIA", 3, "provider taxonomy indicates an unsupported Asian market"
    elif namespace == "AR":
        if re.search(r"\bbein\b", topic_key):
            market, strength, reason = "BEIN", 3, "provider taxonomy indicates the configured beIN catalog"
        else:
            market, strength, reason = "MENA", 3, "provider taxonomy indicates Arabic/MENA programming"
    elif namespace == "EU":
        for pattern, code, label in CATEGORY_COUNTRY_MARKETS_V84:
            if pattern.search(topic):
                market, strength, reason = code, 3, f"provider taxonomy indicates {label}"
                break
        if not market:
            market, strength, reason = "EU", 3, "provider taxonomy indicates an unsupported European market"
    elif namespace == "SA":
        market, strength, reason = "LATAM", 3, "provider taxonomy indicates Caribbean/Latin programming"
    elif namespace == "AM":
        for pattern, code, label in CATEGORY_COUNTRY_MARKETS_V84:
            if pattern.search(topic):
                market, strength, reason = code, 3, f"provider taxonomy indicates {label}"
                break
        if not market:
            market, strength, reason = "LATAM", 3, "provider taxonomy indicates Latin America"
    elif namespace == "AF":
        for pattern, code, label in CATEGORY_COUNTRY_MARKETS_V84:
            if pattern.search(topic):
                market, strength, reason = code, 3, f"provider taxonomy indicates {label}"
                break
        if not market:
            market, strength, reason = "AFRICA", 3, "provider taxonomy indicates an unsupported African market"
    elif namespace in CATEGORY_DIRECT_MARKETS_V84:
        market = CATEGORY_DIRECT_MARKETS_V84[namespace]
        strength = 3
        reason = f"provider taxonomy namespace {namespace} identifies {market}"

    if not market:
        folded = _ascii_fold(raw)
        for pattern, code, label in CATEGORY_MARKET_RULES_V82:
            if pattern.search(folded):
                market, strength, reason = code, 3, f"category indicates {label}"
                break
        if not market:
            for pattern, code, label in CATEGORY_COUNTRY_MARKETS_V84:
                if pattern.search(raw):
                    market, strength, reason = code, 3, f"category indicates {label}"
                    break

    aliases: set[str] = set()
    if namespace:
        aliases.add(namespace.casefold())
    for token in topic_tokens:
        aliases.add(token)
        aliases.update(CATEGORY_PREFIX_CODES_V84.get(token, ()))
        language = LANGUAGE_MAP_V8.get(token)
        if language:
            aliases.update(CATEGORY_PREFIX_CODES_V84.get(language, ()))
    for language in languages:
        aliases.update(CATEGORY_PREFIX_CODES_V84.get(language, ()))
    if "sports" in content:
        aliases.update({"sports", "sport", "sp"})
    if "movies" in content:
        aliases.update({"movies", "movie", "cinema"})
    aliases = {
        _metadata_key_v8(value) for value in aliases
        if value and len(_metadata_key_v8(value).split()) <= 4
    }
    return {
        "original": raw, "namespace": namespace, "topic": topic,
        "topic_key": topic_key, "languages": frozenset(languages),
        "content": frozenset(content), "market": market,
        "strength": strength, "reason": reason,
        "prefix_aliases": frozenset(aliases),
    }


def _leading_structural_prefix_v84(channel_name: str) -> str:
    raw = _nfkc(channel_name).strip()
    if not raw or re.match(r"^[#*=~_─━═]{3,}", raw):
        return ""
    patterns = (
        r"^\s*([^|:]{1,30}?)\s*\|\s*(.+)$",
        r"^\s*([^:]{1,30}?)\s*:\s*(.+)$",
        r"^\s*(.{1,30}?)\s+-\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            return _metadata_key_v8(match.group(1))
    return ""


def _build_inventory_profiles_v84(
    channels: Sequence[Mapping[str, Any]], category_names: Mapping[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Learn repeated category wrappers and virtual banks from the whole lineup."""
    by_category: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for channel in channels:
        category_id = str(channel.get("category_id") or "").strip()
        by_category[category_id].append(channel)

    profiles: dict[str, dict[str, Any]] = {}
    for category_id, items in by_category.items():
        category_name = str(category_names.get(category_id, ""))
        context = _parse_category_context_v84(category_name)
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            name = str(item.get("name") or item.get("channel_name") or item.get("stream_display_name") or "")
            prefix = _leading_structural_prefix_v84(name)
            if prefix:
                counts[prefix] += 1
        learned = set(context["prefix_aliases"])
        total = max(1, len(items))
        for prefix, count in counts.items():
            if count >= 2 and (count / total >= 0.35 or count >= 5):
                learned.add(prefix)
        profiles[category_id] = {
            "category_context": context,
            "learned_prefixes": frozenset(learned),
        }

    # Identify repeated numbered virtual banks. This requires lineup evidence and
    # therefore cannot confuse a single real numbered network with a PPV slot.
    grouped: dict[tuple[str, str], list[tuple[str, int, set[str], set[str]]]] = defaultdict(list)
    for channel in channels:
        stream_id = str(channel.get("stream_id") or channel.get("id") or "").strip()
        category_id = str(channel.get("category_id") or "").strip()
        category_name = str(category_names.get(category_id, ""))
        profile = profiles.get(category_id, {})
        context = profile.get("category_context") or _parse_category_context_v84(category_name)
        core, _wrappers, _market = _extract_channel_core_v8(
            str(channel.get("name") or channel.get("channel_name") or channel.get("stream_display_name") or ""),
            category_name, category_context=context,
            learned_prefixes=profile.get("learned_prefixes", ()),
        )
        words = [word for word in _basic_words_v8(core) if word not in TECHNICAL_TOKENS_V8]
        if len(words) < 2 or not words[-1].isdigit():
            continue
        slot = int(words[-1])
        if not 0 < slot < 1000:
            continue
        base_words = words[:-1]
        base_key = " ".join(base_words)
        if not base_key:
            continue
        grouped[(category_id, base_key)].append((stream_id, slot, set(base_words), set(context["topic_key"].split())))

    signals: dict[str, dict[str, str]] = {}
    for (_category_id, base_key), items in grouped.items():
        slots = {slot for _sid, slot, _base, _topic in items}
        if len(slots) < 3:
            continue
        base_tokens = set(next(iter(items))[2])
        topic_tokens = set(next(iter(items))[3])
        combined = base_tokens | topic_tokens
        has_virtual_marker = bool(combined & VIRTUAL_BANK_MARKERS_V84)
        movie_like = bool(combined & MOVIE_BANK_MARKERS_V84)
        sports_360 = "360" in combined
        category_bank = bool(topic_tokens & {"premium", "play", "store", "movies", "cinema", "ppv", "events", "360"})
        if not has_virtual_marker or not (sports_360 or movie_like or category_bank):
            continue
        role = "movie_bank" if movie_like and not sports_360 else "event_bank"
        for stream_id, _slot, _base, _topic in items:
            if stream_id:
                signals[stream_id] = {
                    "role": role, "base_key": base_key,
                    "distinct_slots": str(len(slots)),
                }
    return profiles, signals


def _is_decorative_heading_v84(channel_name: str) -> bool:
    raw = _nfkc(channel_name).strip()
    if not raw:
        return False
    edge = re.escape(DECORATIVE_EDGE_CHARS_V84)
    if re.match(rf"^[{edge}]{{3,}}", raw) and re.search(rf"[{edge}]{{3,}}$", raw):
        return True
    decoration_count = sum(ch in DECORATIVE_EDGE_CHARS_V84 for ch in raw)
    return decoration_count >= 8 and decoration_count / max(1, len(raw)) >= 0.35


def _semantic_fields_v8(value: object, *, category: str = "") -> dict[str, Any]:
    words = [w for w in _basic_words_v8(value) if w not in TECHNICAL_TOKENS_V8]
    tokens = set(words)
    direction = ""
    if tokens & {"west", "pacific"}:
        direction = "west"
    elif tokens & {"east", "eastern"}:
        direction = "east"
    timeshifts = sorted(w.removeprefix("plusshift") for w in words if w.startswith("plusshift"))
    raw_words = set(_basic_words_v8(value))
    if raw_words & {"4k", "8k", "uhd", "2160p"}:
        quality = "uhd"
    elif raw_words & {"hd", "fhd", "1080p", "1080i", "720p"}:
        quality = "hd"
    elif "sd" in raw_words:
        quality = "sd"
    else:
        quality = ""
    return {
        "tokens": tuple(words),
        "languages": frozenset(_languages_from_text_v8(f"{category} {value}", category=category)),
        "content": frozenset(_content_from_tokens_v8(words)),
        "numbers": frozenset(w for w in words if w.isdigit()),
        "direction": direction,
        "has_plus": "plus" in tokens,
        "has_extra": bool(tokens & {"extra", "xtra"}),
        "has_alternate": bool(tokens & {"alternate", "alt"}),
        "timeshift": timeshifts[0] if timeshifts else "",
        "quality": quality,
    }


def _is_wrapper_segment_v8(segment: str, *, category: str) -> bool:
    key = _metadata_key_v8(segment)
    if not key:
        return True
    if _market_from_segment_v8(segment):
        return True
    if key in WRAPPER_CODES_V8 or key in AMBIGUOUS_WRAPPER_CODES_V8:
        return True

    # Alphanumeric provider codes are commonly punctuated in different ways:
    # D2H, D 2 H, D-2-H.  Compare the isolated metadata segment in compact form
    # rather than adding a full-name exception for every spelling.
    compact = re.sub(r"[^a-z0-9]+", "", key)
    if compact in WRAPPER_COMPACT_CODES_V8:
        return True

    # Attached quality suffixes are metadata when the remaining isolated token is
    # a known wrapper, e.g. PBHD:, MYFHD:, CRIC4K:.  This does not strip quality
    # from ordinary brand names because it is used only on a prefix/pipe segment.
    compact_without_quality = WRAPPER_QUALITY_SUFFIX_RE_V8.sub("", compact)
    if compact_without_quality and compact_without_quality in WRAPPER_COMPACT_CODES_V8:
        return True

    if key in LANGUAGE_MAP_V8 and len(key) <= 10:
        return True
    if re.fullmatch(r"(?:uk|us|usa|ca|in)[ ]?(?:hd|fhd|uhd|sd|4k)", key):
        return True
    if re.fullmatch(r"(?:in|inr) (?:news|gujarati|punjabi|tamil|telugu|malayalam|kannada|marathi|bangla|bengali|odia)", key):
        return True
    return False


def _extract_channel_core_v8(
    channel_name: str, category_name: str, *,
    category_context: Mapping[str, Any] | None = None,
    learned_prefixes: Iterable[str] = (),
) -> tuple[str, list[str], list[tuple[str, int, str]]]:
    category_context = dict(category_context or _parse_category_context_v84(category_name))
    structural_prefixes = {
        _metadata_key_v8(value) for value in (
            *category_context.get("prefix_aliases", ()), *tuple(learned_prefixes)
        ) if _metadata_key_v8(value)
    }

    def is_structural_prefix(segment: str) -> bool:
        key = _metadata_key_v8(segment)
        compact = re.sub(r"[^a-z0-9]+", "", key)
        return (
            _is_wrapper_segment_v8(segment, category=category_name)
            or key in structural_prefixes
            or compact in {re.sub(r"[^a-z0-9]+", "", value) for value in structural_prefixes}
        )

    raw = _nfkc(channel_name).strip()
    raw = re.sub(r"[.\s]+$", "", raw)
    wrappers: list[str] = []
    market_evidence: list[tuple[str, int, str]] = []

    # Special diaspora wrappers used by providers.
    if re.match(r"^\s*US\s*\(\s*(?:IN|INDIA)\s*\)", raw, flags=re.I):
        market_evidence.append(("NA_DIASPORA", 4, "US (IN) diaspora wrapper"))
        raw = re.sub(r"^\s*US\s*\(\s*(?:IN|INDIA)\s*\)\s*", "", raw, flags=re.I)
        wrappers.append("US (IN)")
    if re.match(r"^\s*(?:IN|IT|PK|PB|TM|MAL|MY)?\s*\(\s*(?:TO|TOR|TORONTO)\s*\)", raw, flags=re.I):
        market_evidence.append(("CA", 4, "Toronto diaspora wrapper"))
        m = re.match(r"^\s*([^)]*\))", raw)
        if m:
            wrappers.append(m.group(1).strip())
        raw = re.sub(r"^\s*(?:IN|IT|PK|PB|TM|MAL|MY)?\s*\(\s*(?:TO|TOR|TORONTO)\s*\)\s*", "", raw, flags=re.I)

    # Standalone parenthetical market at the end is strong evidence.
    end_market = re.search(
        r"\s*\(\s*(USA|US|NORTH\s+AMERICA|CANADA|CA|TORONTO|UK|GB|INDIA|IN)\s*\)\s*$",
        raw, flags=re.I,
    )
    if end_market:
        market = _market_from_segment_v8(end_market.group(1))
        market_evidence.append((market, 4, f"explicit suffix ({end_market.group(1)})"))
        wrappers.append(end_market.group(0).strip())
        raw = raw[: end_market.start()].strip()

    # A bare trailing market is also structural when the category is South Asian
    # or the provider separated it with punctuation.  This covers names such as
    # ``PTC Punjabi USA HD`` without treating the brand ``USA Network`` as a
    # market wrapper.
    category_languages = _languages_from_text_v8(category_name, category=category_name)
    category_key = _normalize_key_v8(category_name)
    south_asian_category = bool(category_languages & SOUTH_ASIAN_LANGUAGES_V8) or bool(
        set(category_key.split()) & {"india", "indian", "inr"}
    )
    bare_market = re.search(
        r"(?P<separator>\s+|\s*[-_/]\s*)"
        r"(?P<market>NORTH\s+AMERICA|UNITED\s+STATES|USA|US|CANADA|CA|TORONTO|UK|GB|INDIA|IN)"
        r"(?:\s+(?:HD|FHD|UHD|4K|8K|SD|HEVC|H265|H264))?\s*$",
        raw,
        flags=re.I,
    )
    if bare_market and (
        south_asian_category or bool(re.search(r"[-_/]", bare_market.group("separator")))
    ):
        token = bare_market.group("market")
        market = _market_from_segment_v8(token)
        if market:
            market_evidence.append((market, 4, f"explicit bare suffix {token}"))
            wrappers.append(raw[bare_market.start():].strip())
            raw = raw[: bare_market.start()].strip()

    # Remove bracketed technical, audio/language, and self-describing acronym
    # metadata before parsing wrapper boundaries. Meaningful editions such as
    # East/West/+1 remain part of the identity.
    raw = _strip_redundant_brackets_v82(raw, category_name)

    # Remove trailing technical parentheticals, but never meaningful editions.
    raw = re.sub(r"(?:\s*\((?:HD|FHD|UHD|4K|8K|SD|HEVC|H265|H264|A|B|C|D|E|F|F2|FL|H|L|R|S|X|CX|PC)\))+\s*$", "", raw, flags=re.I)

    # Provider category labels are often repeated before a pipe, colon, or
    # spaced hyphen (ENG -, PJB -, SPORTS -, GJR -). The accepted labels are
    # derived from the category grammar and repeated lineup evidence.
    for _ in range(4):
        match = re.match(
            r"^\s*([^|:]{1,30}?)\s*(?:\||:|\s+-\s+)\s*(.+)$", raw
        )
        if not match or not is_structural_prefix(match.group(1)):
            break
        segment = match.group(1).strip()
        wrappers.append(segment)
        market = _market_from_segment_v8(segment)
        if market:
            market_evidence.append((market, 3, f"structural prefix {segment}"))
        raw = match.group(2).strip()

    # Pipes provide the clearest metadata boundaries.
    if "|" in raw:
        segments = [s.strip() for s in re.split(r"\|+", raw) if s.strip()]
        while len(segments) > 1 and is_structural_prefix(segments[0]):
            seg = segments.pop(0)
            wrappers.append(seg)
            market = _market_from_segment_v8(seg)
            if market:
                market_evidence.append((market, 3, f"leading pipe segment {seg}"))
        while len(segments) > 1 and is_structural_prefix(segments[-1]):
            seg = segments.pop()
            wrappers.append(seg)
            market = _market_from_segment_v8(seg)
            if market:
                market_evidence.append((market, 4, f"trailing pipe segment {seg}"))
        raw = " ".join(segments)

    # Colon prefix is metadata only when it is a known short wrapper/market.
    for _ in range(3):
        m = re.match(r"^\s*([^:]{1,24})\s*:\s*(.+)$", raw)
        if not m or not is_structural_prefix(m.group(1)):
            break
        seg = m.group(1).strip()
        wrappers.append(seg)
        market = _market_from_segment_v8(seg)
        if market:
            market_evidence.append((market, 3, f"colon prefix {seg}"))
        raw = m.group(2).strip()

    # Underscore wrappers such as BD_DHOOM MUSIC and short prefixes followed by
    # a space. Attached hyphen country codes are intentionally weak evidence.
    m = re.match(r"^\s*([A-Z]{2,8})[_]+(.+)$", raw)
    if m and is_structural_prefix(m.group(1)):
        wrappers.append(m.group(1))
        market = _market_from_segment_v8(m.group(1))
        if market:
            market_evidence.append((market, 3, f"underscore prefix {m.group(1)}"))
        raw = m.group(2).strip()

    # Known non-market wrappers may also be separated by a hyphen or slash.
    # Country prefixes keep their deliberately weaker handling below because
    # providers sometimes label imported channels with a misleading country.
    m = re.match(r"^\s*([^-/]{1,24})\s*[-/]\s*(.+)$", raw)
    if m and not _market_from_segment_v8(m.group(1)) and is_structural_prefix(m.group(1)):
        wrappers.append(m.group(1).strip())
        raw = m.group(2).strip()

    m = re.match(r"^\s*(UK)(?:HD|FHD|UHD|SD|4K)\s*[-:/]?\s*(.+)$", raw, flags=re.I)
    if m:
        wrappers.append(m.group(0)[: m.start(2)].strip())
        market_evidence.append(("UK", 3, "UK quality wrapper"))
        raw = m.group(2).strip()

    # Weak attached region prefix; category may override it (e.g. UK-SONY in an
    # Indian category). A colon/pipe prefix was already treated as strong above.
    m = re.match(r"^\s*(US|USA|UK|GB|CA|CANADA|IN|INDIA|PK)\s*[-/]\s*(.+)$", raw, flags=re.I)
    if m:
        wrappers.append(m.group(1))
        market = _market_from_segment_v8(m.group(1))
        market_evidence.append((market, 1, f"weak attached prefix {m.group(1)}-"))
        raw = m.group(2).strip()

    # Known language/provider token followed by whitespace. Only uppercase or
    # category-supported tokens qualify, so a normal brand word is not removed.
    m = re.match(r"^\s*([A-Za-z]{2,8})\s+(.+)$", raw)
    if m:
        token = m.group(1)
        key = token.casefold()
        category_key = _normalize_key_v8(category_name)
        language_hint = LANGUAGE_MAP_V8.get(key, "")
        supported = (
            key in LEADING_TOKEN_WRAPPERS_V82
            or key in MARKET_SEGMENTS_V8
            or (key in LANGUAGE_MAP_V8 and len(key) <= 3)
        )
        uppercase_signal = token.isupper()
        category_tokens = set(category_key.split())
        category_signal = key in category_tokens or bool(language_hint and language_hint in category_tokens)
        if supported and (uppercase_signal or category_signal):
            # Some market-looking words are also real channel brands. Keep them
            # when the following token proves that the phrase is the brand, not a
            # provider routing wrapper.
            protected_brand = (
                (key == "usa" and re.match(r"(?i)^network\b", m.group(2)))
                or (key == "bein" and re.match(r"(?i)^sports\b", m.group(2)))
                # INDIA/IN can be the first word of a real network brand, not
                # a routing prefix.  A separated wrapper such as ``IN -`` has
                # already been removed above, so preserving ``India News ...``
                # here is structural brand protection rather than a row rule.
                or (key in {"in", "india"} and re.match(r"(?i)^news\b", m.group(2)))
            )
            if not protected_brand:
                wrappers.append(token)
                market = _market_from_segment_v8(token)
                if market:
                    market_evidence.append((market, 3, f"leading token {token}"))
                raw = m.group(2).strip()

    # Wrapper removal may expose a bracket alias that could not be recognized
    # while the market prefix was still present (for example USA: ... (FS1)).
    raw = _strip_redundant_brackets_v82(raw, category_name)
    raw = re.sub(r"(?:\s*\((?:HD|FHD|UHD|4K|8K|SD|HEVC|H265|H264)\))+\s*$", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip(" -_:|./")
    return raw, wrappers, market_evidence


def _category_route_v8(category_name: str, languages: set[str]) -> tuple[str, int, str]:
    context = _parse_category_context_v84(category_name)
    if context.get("market"):
        return str(context["market"]), int(context.get("strength", 3)), str(context.get("reason", ""))
    if languages & SOUTH_ASIAN_LANGUAGES_V8:
        return "IN", 2, "category/language indicates India"
    return "", 0, ""




def _choose_route_v8(
    engine: Any,
    channel_name: str,
    category_name: str,
    core_name: str,
    wrappers: list[str],
    market_evidence: list[tuple[str, int, str]],
    languages: set[str],
) -> tuple[str, tuple[str, ...], str, bool, bool]:
    south_asian = bool(languages & SOUTH_ASIAN_LANGUAGES_V8) or any(
        market == "NA_DIASPORA" for market, _strength, _reason in market_evidence
    )
    category_market, category_strength, category_reason = _category_route_v8(category_name, languages)

    # Explicit market evidence wins only when it is stronger than the category.
    chosen_market = ""
    chosen_strength = 0
    chosen_reason = ""
    for market, strength, reason in market_evidence:
        if market and strength > chosen_strength:
            chosen_market, chosen_strength, chosen_reason = market, strength, reason

    if category_market and category_strength > chosen_strength:
        chosen_market, chosen_strength, chosen_reason = category_market, category_strength, category_reason

    route_explicit = bool(chosen_market and chosen_strength >= 3)
    if chosen_market == "NA_DIASPORA" or (chosen_market == "US" and south_asian and chosen_strength >= 3):
        return (
            "NA_DIASPORA", ("US", "CA"),
            f"{chosen_reason}; South-Asian channel uses exact US then Canadian diaspora schedules",
            True, south_asian,
        )
    if chosen_market:
        return chosen_market, (chosen_market,), chosen_reason, route_explicit, south_asian

    # Use the legacy router as a lower-priority fallback for countries/custom
    # sources not represented in the structural policy above.
    try:
        legacy_region, legacy_explicit, legacy_reason = engine.detect_route({
            "channel_name": channel_name,
            "category_name": category_name,
            "panel_epg_id": "",
        })
    except Exception:
        legacy_region, legacy_explicit, legacy_reason = "ALL", False, "no reliable region clue"
    legacy_region = str(legacy_region or "ALL").upper()
    if legacy_region == "US" and south_asian and legacy_explicit:
        return "NA_DIASPORA", ("US", "CA"), f"{legacy_reason}; diaspora exact-search plan", True, south_asian
    return legacy_region, (legacy_region,), legacy_reason, bool(legacy_explicit), south_asian


def parse_channel_context_v8(
    engine: Any, channel_name: str, category_name: str, *,
    category_profile: Mapping[str, Any] | None = None,
) -> ChannelContextV8:
    category_profile = dict(category_profile or {})
    category_context = dict(
        category_profile.get("category_context")
        or _parse_category_context_v84(category_name)
    )
    core_name, wrappers, market_evidence = _extract_channel_core_v8(
        channel_name, category_name, category_context=category_context,
        learned_prefixes=category_profile.get("learned_prefixes", ()),
    )
    strict_key = _normalize_key_v8(core_name)
    relaxed_key = _normalize_key_v8(core_name, remove_optional=True)
    compact_key = re.sub(r"[^a-z0-9]+", "", relaxed_key)
    edition_key = " ".join(
        word for word in strict_key.split()
        if word not in {"east", "eastern", "west", "pacific", "plus", "extra", "xtra", "alternate", "alt"}
        and not word.startswith("plusshift")
    )
    identity_keys = _identity_keys_v82(core_name)
    bag_key = _identity_bag_key_v84(core_name)
    context_remove: set[str] = set()
    topic_tokens = set(str(category_context.get("topic_key", "")).split())
    if category_context.get("market") == "IN" and topic_tokens & {"english", "sports", "sport"}:
        context_remove.update({"english", "eng", "en"})
    context_bag_key = _identity_bag_key_v84(core_name, remove_tokens=context_remove)

    semantics = _semantic_fields_v8(core_name, category=category_name)
    original_semantics = _semantic_fields_v8(channel_name, category=category_name)
    semantics["quality"] = original_semantics["quality"]
    languages = set(semantics["languages"])
    for wrapper in wrappers:
        wrapper_key = _metadata_key_v8(wrapper)
        if wrapper_key == "my" and "malaysia" in _normalize_key_v8(category_name):
            continue
        if wrapper_key == "bd" and not re.search(r"\b(?:bangla|bengali|bangladesh)\b", _normalize_key_v8(category_name)):
            continue
        language = LANGUAGE_MAP_V8.get(wrapper_key)
        if language:
            languages.add(language)
    languages.update(category_context.get("languages", ()))
    explicit_market, route_plan, route_reason, route_explicit, south_asian = _choose_route_v8(
        engine, channel_name, category_name, core_name, wrappers, market_evidence, languages
    )
    return ChannelContextV8(
        original_name=str(channel_name or ""), category_name=str(category_name or ""),
        core_name=core_name, strict_key=strict_key, relaxed_key=relaxed_key,
        compact_key=compact_key, edition_key=edition_key, identity_keys=identity_keys,
        bag_key=bag_key, context_bag_key=context_bag_key,
        category_namespace=str(category_context.get("namespace", "")),
        category_topic=str(category_context.get("topic", "")),
        category_market=str(category_context.get("market", "")),
        quality=semantics["quality"], tokens=semantics["tokens"],
        languages=frozenset(languages), content=semantics["content"],
        numbers=semantics["numbers"], direction=semantics["direction"],
        has_plus=semantics["has_plus"], has_extra=semantics["has_extra"],
        has_alternate=semantics["has_alternate"], timeshift=semantics["timeshift"],
        wrapper_tokens=tuple(wrappers), explicit_market=explicit_market,
        route_plan=route_plan, route_reason=route_reason, route_explicit=route_explicit,
        south_asian_context=south_asian,
    )


# Keep an implementation alias because the self-contained notebook exposes a
# two-argument convenience wrapper under the public name on the same module.
_parse_channel_context_impl_v8 = parse_channel_context_v8


def parse_candidate_context_v8(engine: Any, candidate: Mapping[str, str]) -> CandidateContextV8:
    epg_id = str(candidate.get("epg_id", ""))
    try:
        base = engine.SOURCE_SUFFIX_RE.sub("", epg_id)
    except Exception:
        base = re.sub(r"\.[a-z]{2,}(?:\d+|_locals\d*)?$", "", epg_id, flags=re.I)
    base = base.replace(".", " ").replace("_", " ").replace("/", " ")
    base = re.sub(
        r"\b(?:digital\s+)?(?:mono|stereo)(?:\s+(?:ar|en|fr))?\s*$",
        "", base, flags=re.I,
    ).strip()
    if str(candidate.get("region", "")).upper() == "UK":
        base = re.sub(r"^\s*u\s+and\s+", "", base, flags=re.I)
    strict_key = _normalize_key_v8(base)
    relaxed_key = _normalize_key_v8(base, remove_optional=True)
    compact_key = re.sub(r"[^a-z0-9]+", "", relaxed_key)
    edition_key = " ".join(
        word for word in strict_key.split()
        if word not in {"east", "eastern", "west", "pacific", "plus", "extra", "xtra", "alternate", "alt"}
        and not word.startswith("plusshift")
    )
    identity_keys = _identity_keys_v82(base)
    bag_key = _identity_bag_key_v84(base)
    semantics = _semantic_fields_v8(base)
    family_signature = (
        bag_key or relaxed_key or strict_key, semantics["direction"], semantics["has_plus"],
        semantics["has_extra"], semantics["has_alternate"], semantics["timeshift"],
        tuple(sorted(semantics["numbers"])), tuple(sorted(semantics["languages"])),
        tuple(sorted(semantics["content"])),
    )
    return CandidateContextV8(
        candidate=candidate, strict_key=strict_key, relaxed_key=relaxed_key,
        compact_key=compact_key, edition_key=edition_key, identity_keys=identity_keys,
        bag_key=bag_key, quality=semantics["quality"], tokens=semantics["tokens"],
        languages=semantics["languages"], content=semantics["content"],
        numbers=semantics["numbers"], direction=semantics["direction"],
        has_plus=semantics["has_plus"], has_extra=semantics["has_extra"],
        has_alternate=semantics["has_alternate"], timeshift=semantics["timeshift"],
        family_signature=family_signature,
    )




def _catalog_fingerprint_v8(real_candidates: list[dict[str, str]], dummy_ids: dict[str, str]) -> str:
    digest = hashlib.blake2b(digest_size=20)
    for item in real_candidates:
        digest.update(str(item.get("feed", "")).encode("utf-8")); digest.update(b"\0")
        digest.update(str(item.get("region", "")).encode("utf-8")); digest.update(b"\0")
        digest.update(str(item.get("epg_id", "")).encode("utf-8")); digest.update(b"\xff")
    for key, value in sorted(dummy_ids.items()):
        digest.update(str(key).encode("utf-8")); digest.update(b"\0")
        digest.update(str(value).encode("utf-8")); digest.update(b"\xff")
    return digest.hexdigest()


def _split_multi_value_v8(value: object, *, separators: str = r"[,|;]") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in re.split(separators, value) if part.strip())
    try:
        return tuple(str(part).strip() for part in value if str(part).strip())
    except TypeError:
        text = str(value).strip()
        return (text,) if text else ()


def _timeshift_targets_v82(value: object) -> dict[str, tuple[str, ...]]:
    if not value:
        return {}
    payload = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for raw_shift, raw_ids in payload.items():
        shift = str(raw_shift).strip().removeprefix("+")
        ids = _split_multi_value_v8(raw_ids, separators=r"[|;]")
        if shift and ids:
            result[shift] = ids
    return result


def read_approved_alias_rows_v8(source: object) -> list[dict[str, Any]]:
    """Read approved alias rows from a DataFrame, CSV path, or iterable.

    Expected columns are ``alias``, ``regions``/``region``,
    ``epg_ids``/``epg_id``, ``relationship``, and ``note``.  Additional columns
    are ignored.  The function performs no matching and never reads credentials.
    """
    if source is None:
        return []
    if isinstance(source, pd.DataFrame):
        return [dict(row) for row in source.to_dict("records")]
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"Approved alias file was not found: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if isinstance(source, Mapping):
        return [dict(source)]
    return [dict(row) for row in source]


def build_schedule_fingerprints_v8(
    engine: Any,
    xmltv_path: str | Path,
    epg_ids: Iterable[str],
    *,
    now_epoch: int | None = None,
    horizon_hours: int = 72,
    start_bucket_minutes: int = 5,
) -> dict[str, ScheduleFingerprintV8]:
    """Build future-programme fingerprints for selected IDs from one XMLTV file.

    This is intentionally an opt-in validation facility.  The ordinary matcher
    does not download every large XML feed merely to compare schedules.  A caller
    can run this only for ambiguous IDs, discover equivalent schedule groups,
    and register those groups with :func:`register_schedule_equivalences_v8`.
    """
    path = Path(xmltv_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    wanted = {str(value).casefold(): str(value) for value in epg_ids if str(value).strip()}
    if not wanted:
        return {}
    now = int(now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp())
    horizon = now + max(1, int(horizon_hours)) * 3600
    bucket_seconds = max(1, int(start_bucket_minutes)) * 60
    rows: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)

    with engine.open_maybe_gzip(path) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            if engine.local_name(element.tag) != "programme":
                if engine.local_name(element.tag) == "channel":
                    element.clear()
                    while element.getprevious() is not None:
                        del element.getparent()[0]
                continue
            source_id = (element.get("channel") or "").strip()
            canonical = wanted.get(source_id.casefold())
            if canonical is not None:
                start = engine.parse_xmltv_time(element.get("start"))
                stop = engine.parse_xmltv_time(element.get("stop"))
                title = engine.child_text(element, "title")
                normalized_title = engine.normalize_programme_title_for_quality(title)
                if start is not None and start <= horizon and (stop or start + 3600) >= now and normalized_title:
                    effective_stop = stop if stop is not None and stop > start else start + 3600
                    rows[canonical].append((start // bucket_seconds, normalized_title, start, effective_stop))
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
        del context

    result: dict[str, ScheduleFingerprintV8] = {}
    for canonical, programmes in rows.items():
        programmes.sort(key=lambda item: (item[2], item[3], item[1]))
        deduped = list(dict.fromkeys((bucket, title) for bucket, title, _start, _stop in programmes))
        result[canonical.casefold()] = ScheduleFingerprintV8(
            epg_id=canonical,
            entries=tuple(deduped),
            programme_count=len(deduped),
            first_start=min(item[2] for item in programmes),
            last_stop=max(item[3] for item in programmes),
        )
    for folded, canonical in wanted.items():
        result.setdefault(
            folded,
            ScheduleFingerprintV8(
                epg_id=canonical, entries=(), programme_count=0,
                first_start=None, last_stop=None,
            ),
        )
    return result


# Same-module notebook installation replaces the public convenience name; keep
# the implementation reference stable for its closure.
_build_schedule_fingerprints_impl_v8 = build_schedule_fingerprints_v8


def schedule_similarity_v8(
    left: ScheduleFingerprintV8,
    right: ScheduleFingerprintV8,
    *,
    start_tolerance_buckets: int = 1,
) -> float:
    """Return 0..1 overlap of title/start sequences for two future schedules."""
    if not left.entries or not right.entries:
        return 0.0
    right_by_title: dict[str, list[int]] = defaultdict(list)
    for bucket, title in right.entries:
        right_by_title[title].append(bucket)
    matched = 0
    consumed: set[tuple[str, int]] = set()
    tolerance = max(0, int(start_tolerance_buckets))
    for bucket, title in left.entries:
        options = sorted(right_by_title.get(title, ()), key=lambda other: abs(other - bucket))
        for other in options:
            marker = (title, other)
            if marker in consumed or abs(other - bucket) > tolerance:
                continue
            consumed.add(marker)
            matched += 1
            break
    return matched / max(len(left.entries), len(right.entries))


def discover_schedule_equivalence_groups_v8(
    fingerprints: Mapping[str, ScheduleFingerprintV8],
    *,
    min_programmes: int = 8,
    min_similarity: float = 0.92,
) -> list[list[str]]:
    """Discover connected groups of IDs carrying materially identical schedules."""
    items = [
        fingerprint for fingerprint in fingerprints.values()
        if fingerprint.programme_count >= max(1, int(min_programmes))
    ]
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    threshold = max(0.0, min(1.0, float(min_similarity)))
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if schedule_similarity_v8(items[left], items[right]) >= threshold:
                union(left, right)

    grouped: dict[int, list[str]] = defaultdict(list)
    for index, fingerprint in enumerate(items):
        grouped[find(index)].append(fingerprint.epg_id)
    return [sorted(ids, key=str.casefold) for ids in grouped.values() if len(ids) > 1]


def load_schedule_equivalence_groups_v8(source: object) -> list[list[str]]:
    """Load equivalence groups from JSON, a mapping, or an iterable of groups."""
    if source is None:
        return []
    payload: object
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        payload = source
    if isinstance(payload, Mapping):
        payload = payload.get("groups", payload.get("scheduleEquivalenceGroups", []))
    groups: list[list[str]] = []
    for raw_group in payload:
        if isinstance(raw_group, Mapping):
            raw_group = raw_group.get("epg_ids", raw_group.get("ids", []))
        ids = list(dict.fromkeys(_split_multi_value_v8(raw_group, separators=r"[|;,]")))
        if len(ids) > 1:
            groups.append(ids)
    return groups


class ContextualMatcherV8:
    def __init__(self, engine: Any):
        self.engine = engine
        self.state = V8PreparedState()
        self.approved_aliases: list[dict[str, Any]] = []
        self.schedule_group_by_id: dict[str, str] = {}
        self.register_approved_aliases(DEFAULT_APPROVED_ALIASES_V8)

    def register_approved_aliases(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Register or merge generic approved channel knowledge.

        The key is canonical alias + market set.  Re-registering the same key
        merges target IDs in priority order instead of creating patch chains.
        """
        changed = 0
        for raw in rows:
            alias = _normalize_key_v8(raw.get("alias", ""))
            if not alias:
                continue
            regions = tuple(
                part.upper() for part in _split_multi_value_v8(
                    raw.get("regions", raw.get("region", "ALL"))
                )
            ) or ("ALL",)
            epg_ids = _split_multi_value_v8(
                raw.get("epg_ids", raw.get("epg_id", "")), separators=r"[|;]"
            )
            timeshift_targets = _timeshift_targets_v82(
                raw.get("timeshift_epg_ids", raw.get("timeshift_targets", {}))
            )
            if not epg_ids and not timeshift_targets:
                continue
            normalized_regions = tuple(dict.fromkeys(regions))
            existing = next(
                (
                    item for item in self.approved_aliases
                    if item["alias"] == alias
                    and tuple(item.get("regions", ("ALL",))) == normalized_regions
                ),
                None,
            )
            if existing is None:
                self.approved_aliases.append({
                    "alias": alias,
                    "regions": normalized_regions,
                    "epg_ids": tuple(dict.fromkeys(epg_ids)),
                    "timeshift_epg_ids": timeshift_targets,
                    "required_languages": tuple(
                        value.casefold() for value in _split_multi_value_v8(
                            raw.get("required_languages", raw.get("required_language", ""))
                        )
                    ),
                    "relationship": str(raw.get("relationship", "approved_alias")),
                    "note": str(raw.get("note", "")),
                })
                changed += 1
                continue
            merged = tuple(dict.fromkeys((*existing.get("epg_ids", ()), *epg_ids)))
            if merged != tuple(existing.get("epg_ids", ())):
                existing["epg_ids"] = merged
                changed += 1
            if timeshift_targets:
                old_targets = dict(existing.get("timeshift_epg_ids", {}))
                merged_targets = dict(old_targets)
                for shift, ids in timeshift_targets.items():
                    merged_targets[shift] = tuple(dict.fromkeys((*old_targets.get(shift, ()), *ids)))
                if merged_targets != old_targets:
                    existing["timeshift_epg_ids"] = merged_targets
                    changed += 1
            if raw.get("relationship"):
                existing["relationship"] = str(raw["relationship"])
            if raw.get("note"):
                existing["note"] = str(raw["note"])
        return changed

    def load_approved_aliases(self, source: object) -> int:
        return self.register_approved_aliases(read_approved_alias_rows_v8(source))

    def approved_alias_table(self) -> pd.DataFrame:
        rows = [
            {
                "alias": str(item.get("alias", "")),
                "regions": "|".join(str(value) for value in item.get("regions", ("ALL",))),
                "epg_ids": "|".join(str(value) for value in item.get("epg_ids", ())),
                "timeshift_epg_ids": json.dumps(item.get("timeshift_epg_ids", {}), sort_keys=True),
                "required_languages": "|".join(str(value) for value in item.get("required_languages", ())),
                "relationship": str(item.get("relationship", "approved_alias")),
                "note": str(item.get("note", "")),
            }
            for item in self.approved_aliases
        ]
        return pd.DataFrame(
            rows,
            columns=["alias", "regions", "epg_ids", "timeshift_epg_ids", "required_languages", "relationship", "note"],
        ).drop_duplicates(["alias", "regions"], keep="last")

    def register_schedule_equivalences(self, groups: Iterable[Iterable[str]]) -> int:
        """Register IDs proven equivalent by programme-title/time comparison."""
        changed = 0
        for index, raw_group in enumerate(groups, start=1):
            ids = tuple(dict.fromkeys(str(value).strip() for value in raw_group if str(value).strip()))
            if len(ids) < 2:
                continue
            label = "schedule:" + hashlib.blake2b(
                "|".join(sorted(value.casefold() for value in ids)).encode("utf-8"),
                digest_size=10,
            ).hexdigest()
            for epg_id in ids:
                folded = epg_id.casefold()
                if self.schedule_group_by_id.get(folded) != label:
                    self.schedule_group_by_id[folded] = label
                    changed += 1
        return changed

    def load_schedule_equivalences(self, source: object) -> int:
        return self.register_schedule_equivalences(load_schedule_equivalence_groups_v8(source))

    def export_approved_alias_memory(self, mapping: pd.DataFrame) -> pd.DataFrame:
        """Export generic knowledge from rows a person explicitly approved.

        Stream IDs, server credentials, URLs and panel mappings are intentionally
        excluded.  Only channel identity + market + an EPGShare target are saved.
        """
        if not isinstance(mapping, pd.DataFrame):
            mapping = pd.DataFrame(mapping)
        records: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in mapping.to_dict("records"):
            action = str(raw.get("action", "")).strip().upper()
            source = str(raw.get("source", "")).strip().casefold()
            target_id = str(raw.get("epg_id", "")).strip()
            if action not in {"MANUAL", "APPROVED"} or source != "epgshare" or not target_id:
                continue
            channel_name = str(raw.get("channel_name", "")).strip()
            category_name = str(raw.get("category_name", "")).strip()
            context = _parse_channel_context_impl_v8(self.engine, channel_name, category_name)
            alias = context.strict_key
            if not alias:
                continue
            catalog_item = self.engine.ID_LOOKUP.get(target_id.casefold(), {})
            region = str(catalog_item.get("region") or raw.get("detected_region") or "ALL").upper()
            if region == "NA_DIASPORA":
                region = "US|CA"
            key = (alias, region)
            record = records.setdefault(key, {
                "alias": alias,
                "regions": region,
                "epg_ids": [],
                "relationship": "approved_mapping_memory",
                "note": "Learned from a human-approved EPGShare mapping; revalidated against the current catalog before use.",
            })
            if target_id not in record["epg_ids"]:
                record["epg_ids"].append(target_id)
        rows = []
        for key in sorted(records, key=lambda item: (item[0], item[1])):
            record = records[key]
            rows.append({**record, "epg_ids": "|".join(record["epg_ids"])})
        return pd.DataFrame(rows, columns=["alias", "regions", "epg_ids", "relationship", "note"])

    def prepare(self, real_candidates: list[dict[str, str]], dummy_ids: dict[str, str]) -> None:
        fingerprint = _catalog_fingerprint_v8(real_candidates, dummy_ids)
        if fingerprint == self.state.fingerprint:
            self.state.index_reuses += 1
            return
        self.engine._smart_v7_prepare_matcher(real_candidates, dummy_ids)
        by_region: dict[str, list[CandidateContextV8]] = defaultdict(list)
        for candidate in self.engine.CANDIDATES:
            context = parse_candidate_context_v8(self.engine, candidate)
            by_region[str(candidate.get("region", "ALL")).upper()].append(context)
        all_contexts = [ctx for region, values in by_region.items() if region != "ALL" for ctx in values]
        all_contexts.extend(list(by_region.get("ALL", ())))
        by_region["ALL"] = all_contexts

        strict_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        relaxed_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        compact_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        edition_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        identity_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        bag_index: dict[str, dict[str, list[CandidateContextV8]]] = {}
        candidate_keys: dict[str, list[str]] = {}
        for region, contexts in by_region.items():
            s: dict[str, list[CandidateContextV8]] = defaultdict(list)
            r: dict[str, list[CandidateContextV8]] = defaultdict(list)
            c: dict[str, list[CandidateContextV8]] = defaultdict(list)
            e: dict[str, list[CandidateContextV8]] = defaultdict(list)
            i: dict[str, list[CandidateContextV8]] = defaultdict(list)
            b: dict[str, list[CandidateContextV8]] = defaultdict(list)
            for ctx in contexts:
                if ctx.strict_key:
                    s[ctx.strict_key].append(ctx)
                if ctx.relaxed_key:
                    r[ctx.relaxed_key].append(ctx)
                if ctx.compact_key:
                    c[ctx.compact_key].append(ctx)
                if ctx.edition_key:
                    e[ctx.edition_key].append(ctx)
                if ctx.bag_key:
                    b[ctx.bag_key].append(ctx)
                for identity_key in ctx.identity_keys:
                    i[identity_key].append(ctx)
            strict_index[region] = dict(s)
            relaxed_index[region] = dict(r)
            compact_index[region] = dict(c)
            edition_index[region] = dict(e)
            identity_index[region] = dict(i)
            bag_index[region] = dict(b)
            candidate_keys[region] = list(dict.fromkeys([ctx.strict_key for ctx in contexts if ctx.strict_key]))
        self.state = V8PreparedState(
            fingerprint=fingerprint, index_builds=self.state.index_builds + 1,
            index_reuses=self.state.index_reuses, by_region=dict(by_region),
            strict_index=strict_index, relaxed_index=relaxed_index,
            compact_index=compact_index, edition_index=edition_index,
            identity_index=identity_index, bag_index=bag_index,
            candidate_keys=candidate_keys,
        )


    @staticmethod
    def _compatible(query: ChannelContextV8, target: CandidateContextV8, *, allow_default_east: bool = True) -> bool:
        if query.timeshift != target.timeshift:
            return False
        if query.has_extra != target.has_extra:
            return False
        if query.has_alternate != target.has_alternate:
            return False
        if query.has_plus != target.has_plus:
            return False
        if query.direction != target.direction:
            if not (allow_default_east and query.direction == "east" and target.direction == ""):
                return False
        # Candidate numbers are meaningful even when the query omits them, except
        # for call-sign metadata handled by the legacy local-station resolver.
        if query.numbers and target.numbers and query.numbers != target.numbers:
            return False
        if not query.numbers and target.numbers:
            # Do not map HBO to HBO2, TSN to TSN5, etc.
            return False
        if query.languages and target.languages and query.languages.isdisjoint(target.languages):
            return False
        # Preserve meaningful content/edition words. Knowledge aliases are the
        # only layer allowed to bridge a known rebrand such as Chak De -> News.
        if query.content and target.content and query.content != target.content:
            if query.content.isdisjoint(target.content):
                return False
        query_meaningful = set(query.tokens) & MEANINGFUL_DESCRIPTOR_TOKENS_V8
        target_meaningful = set(target.tokens) & MEANINGFUL_DESCRIPTOR_TOKENS_V8
        if query_meaningful != target_meaningful:
            # East may be omitted by a default feed; already handled above.
            adjusted_q = query_meaningful - {"east"}
            adjusted_t = target_meaningful - {"east"}
            if adjusted_q != adjusted_t:
                return False
        return True

    def _family_key(self, target: CandidateContextV8) -> tuple[Any, ...]:
        schedule_group = self.schedule_group_by_id.get(
            str(target.candidate.get("epg_id", "")).casefold()
        )
        if schedule_group:
            return ("schedule_equivalent", schedule_group)
        return target.family_signature

    def _result_from_group(
        self, query: ChannelContextV8, group: list[CandidateContextV8],
        *, method: str, score: float = 100.0, reason: str = "",
    ) -> dict[str, Any] | None:
        compatible = [ctx for ctx in group if self._compatible(query, ctx)]
        if not compatible:
            return None
        families: dict[tuple[Any, ...], list[CandidateContextV8]] = defaultdict(list)
        for ctx in compatible:
            families[self._family_key(ctx)].append(ctx)
        if len(families) != 1:
            return None
        family = next(iter(families.values()))
        def quality_rank(ctx: CandidateContextV8) -> int:
            if query.quality in {"uhd", "hd"}:
                return {"uhd": 0, "hd": 1, "": 2, "sd": 3}.get(ctx.quality, 4)
            if query.quality == "sd":
                return {"sd": 0, "": 1, "hd": 2, "uhd": 3}.get(ctx.quality, 4)
            return {"": 0, "hd": 1, "uhd": 2, "sd": 3}.get(ctx.quality, 4)

        def identity_rank(ctx: CandidateContextV8) -> tuple[int, float, int]:
            if ctx.strict_key == query.strict_key:
                level = 0
            elif ctx.relaxed_key == query.relaxed_key:
                level = 1
            elif set(ctx.identity_keys).intersection(query.identity_keys):
                level = 2
            elif ctx.bag_key == query.bag_key:
                level = 3
            elif ctx.bag_key == query.context_bag_key:
                level = 4
            else:
                level = 5
            ordered_similarity = float(fuzz.ratio(query.strict_key, ctx.strict_key))
            token_delta = abs(len(query.strict_key.split()) - len(ctx.strict_key.split()))
            return level, -ordered_similarity, token_delta

        best_identity = min(identity_rank(ctx) for ctx in family)
        identity_family = [ctx for ctx in family if identity_rank(ctx) == best_identity]

        # EPG IDs describe a schedule, not the video resolution of the stream.
        # Once the channel identity is established, choose the configured source
        # authority first, then use HD/SD only as a tie-breaker inside that source.
        # This keeps an ``UHD`` label from pulling the schedule to a lower-priority
        # catalog while still selecting an HD ID when the authoritative feed offers
        # both SD and HD variants of the same channel.
        best_source = min(
            self.engine.SOURCE_PRIORITY.get(str(ctx.candidate.get("feed", "")).upper(), 50)
            for ctx in identity_family
        )
        source_family = [
            ctx for ctx in identity_family
            if self.engine.SOURCE_PRIORITY.get(str(ctx.candidate.get("feed", "")).upper(), 50)
            == best_source
        ]
        best_quality = min(quality_rank(ctx) for ctx in source_family)
        quality_family = [ctx for ctx in source_family if quality_rank(ctx) == best_quality]
        candidate_dicts = [dict(ctx.candidate) for ctx in quality_family]
        primary = self.engine.choose_preferred(candidate_dicts, {
            "channel_name": query.original_name, "category_name": query.category_name,
        })
        all_candidate_dicts = [dict(ctx.candidate) for ctx in family]
        primary_id = str(primary.get("epg_id", "")).casefold()
        alternatives = [
            item for item in all_candidate_dicts
            if str(item.get("epg_id", "")).casefold() != primary_id
        ]
        alternate = alternatives[0] if alternatives else None
        return {
            "action": "AUTO_EPGSHARE", "source": "epgshare",
            "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
            "best_score": float(score),
            "second_epg_id": alternate["epg_id"] if alternate else "",
            "second_epg_feed": alternate["feed"] if alternate else "",
            "second_score": float(score) if alternate else "",
            "score_margin": 100.0 if not alternate else 0.0,
            "reason": reason or f"Contextual {method} identity match",
            "match_method": method,
        }

    def _approved_alias_match(self, query: ChannelContextV8) -> dict[str, Any] | None:
        keys = {query.strict_key, query.relaxed_key, query.edition_key, *query.identity_keys}
        for row in self.approved_aliases:
            if _normalize_key_v8(row.get("alias", "")) not in keys:
                continue
            regions = {str(x).upper() for x in row.get("regions", ("ALL",))}
            if "ALL" not in regions and not regions.intersection(query.route_plan):
                continue
            required_languages = {str(value).casefold() for value in row.get("required_languages", ())}
            if required_languages and not required_languages.issubset(set(query.languages)):
                continue
            if query.timeshift:
                targets = tuple(row.get("timeshift_epg_ids", {}).get(query.timeshift, ()))
                if not targets:
                    continue
            else:
                targets = tuple(row.get("epg_ids", ()))
            for epg_id in targets:
                item = self.engine.ID_LOOKUP.get(str(epg_id).casefold())
                if not item:
                    continue
                if query.route_explicit and str(item.get("region", "")).upper() not in query.route_plan:
                    continue
                return {
                    "action": "AUTO_EPGSHARE", "source": "epgshare",
                    "epg_id": item["epg_id"], "epg_feed": item["feed"],
                    "best_score": 100.0, "second_epg_id": "", "second_epg_feed": "",
                    "second_score": "", "score_margin": 100.0,
                    "reason": f"Approved channel knowledge: {row.get('relationship', 'approved alias')}"
                              + (f" — {row.get('note')}" if row.get("note") else ""),
                    "match_method": "approved_knowledge",
                }
        return None

    def _contextual_exact_match(self, query: ChannelContextV8) -> dict[str, Any] | None:
        for region in query.route_plan:
            # Unified identity variants handle redundant acronyms, optional TV/
            # Network labels, punctuation, and common EPG abbreviations.
            for identity_key in query.identity_keys:
                if not identity_key:
                    continue
                if len(identity_key.split()) == 1 and len(identity_key) < 4:
                    continue
                group = self.state.identity_index.get(region, {}).get(identity_key, [])
                if group:
                    result = self._result_from_group(
                        query, group, method="canonical_identity", score=100.0,
                        reason=(
                            "Shared identity normalizer found one catalog family after "
                            "removing technical/bracket/acronym metadata"
                        ),
                    )
                    if result:
                        return result

            for method, key, index, score in (
                ("strict", query.strict_key, self.state.strict_index, 100.0),
                ("edition_aware", query.edition_key, self.state.edition_index, 99.5),
                ("descriptor_relaxed", query.relaxed_key, self.state.relaxed_index, 99.0),
                ("spacing_compact", query.compact_key, self.state.compact_index, 98.0),
            ):
                if not key:
                    continue
                if method == "descriptor_relaxed" and len(key.split()) == 1 and len(key) < 5:
                    continue
                if method == "edition_aware" and len(key.split()) == 1 and len(key) < 3:
                    continue
                if method == "spacing_compact" and len(key) < 5:
                    continue
                group = index.get(region, {}).get(key, [])
                if not group:
                    continue
                result = self._result_from_group(
                    query, group, method=method, score=score,
                    reason=(
                        f"Context parser removed provider/quality metadata and found one exact {method.replace('_', ' ')} "
                        f"channel family in {region}"
                    ),
                )
                if result:
                    return result

            for method, key, score in (
                ("token_multiset", query.bag_key, 98.5),
                ("category_language_default", query.context_bag_key, 98.0),
            ):
                if not key or (method == "category_language_default" and key == query.bag_key):
                    continue
                group = self.state.bag_index.get(region, {}).get(key, [])
                if not group:
                    continue
                result = self._result_from_group(
                    query, group, method=method, score=score,
                    reason=(
                        "The structural identity has the same complete token multiset; "
                        "only token order, regional adjective form, or a category-default language differs"
                    ),
                )
                if result:
                    return result
        return None

    def _contextual_containment_match(self, query: ChannelContextV8) -> dict[str, Any] | None:
        """Resolve a unique already-regional brand whose only provider extras are regions.

        This is deliberately narrower than fuzzy matching. Candidate tokens must
        be an exact subset, the candidate must itself already contain regional
        identity, every extra query token must be a known geographic or language
        context token, and the most-specific candidate family must be unique. It therefore handles regional bundle labels without collapsing
        meaningful subbrands such as Science, Gold, +1, East or channel numbers.
        """
        query_tokens = set(query.relaxed_key.split())
        if len(query_tokens) < 3:
            return None
        matches: list[tuple[tuple[int, int], CandidateContextV8, str]] = []
        for region in query.route_plan:
            for target in self.state.by_region.get(region, []):
                if not self._compatible(query, target):
                    continue
                best: tuple[tuple[int, int], str] | None = None
                for key in target.identity_keys:
                    candidate_tokens = set(key.split())
                    if len(candidate_tokens) < 2 or not candidate_tokens < query_tokens:
                        continue
                    extras = query_tokens - candidate_tokens
                    if not extras or not extras <= REGIONAL_CONTEXT_TOKENS_V82:
                        continue
                    # Containment may extend an already regional identity (for
                    # example Punjab/Haryana + Himachal), but it may not turn a
                    # non-regional base brand into an arbitrary regional edition
                    # such as Fox Sports South or Zee Punjabi News.
                    candidate_regions = candidate_tokens & REGIONAL_CONTEXT_TOKENS_V82
                    if not candidate_regions:
                        continue
                    brand_tokens = (
                        candidate_tokens
                        - REGIONAL_CONTEXT_TOKENS_V82
                        - OPTIONAL_DESCRIPTOR_TOKENS_V8
                        - {"and"}
                    )
                    if not brand_tokens:
                        continue
                    metric = (len(candidate_tokens), -len(extras))
                    if best is None or metric > best[0]:
                        best = (metric, key)
                if best:
                    matches.append((best[0], target, best[1]))
        if not matches:
            return None
        top_metric = max(item[0] for item in matches)
        top = [(target, key) for metric, target, key in matches if metric == top_metric]
        families: dict[tuple[Any, ...], list[CandidateContextV8]] = defaultdict(list)
        for target, _key in top:
            families[self._family_key(target)].append(target)
        if len(families) != 1:
            return None
        group = next(iter(families.values()))
        matched_key = next(key for target, key in top if target in group)
        return self._result_from_group(
            query, group, method="regional_context_containment", score=97.5,
            reason=(
                f"Unique exact catalog identity '{matched_key}' is the most-specific token subset; "
                "the provider's extra words are regional/language context only"
            ),
        )

    def _category_language_equivalence_match(
        self, query: ChannelContextV8
    ) -> dict[str, Any] | None:
        """Treat a category language as context only when one exact family remains.

        A language-specific exact identity always runs first. This fallback is
        therefore used only when a provider repeats the category language in the
        channel name, or the catalog appends it, but no competing language edition
        exists for the same complete brand identity.
        """
        category_context = _parse_category_context_v84(query.category_name)
        category_languages = set(category_context.get("languages", ()))
        if not category_languages:
            return None
        language_words: set[str] = set()
        for language in category_languages:
            language_words.update(
                IDENTITY_TOKEN_CANONICAL_V84.get(token, token)
                for token in LANGUAGE_IDENTITY_TOKENS_V84.get(language, ())
            )

        def language_is_edge_annotation(strict_key: str) -> bool:
            words = [
                IDENTITY_TOKEN_CANONICAL_V84.get(word, word)
                for word in strict_key.split()
            ]
            positions = [
                index for index, word in enumerate(words)
                if word in language_words
            ]
            return not positions or all(
                index in {0, len(words) - 1} for index in positions
            )

        if not language_is_edge_annotation(query.strict_key):
            return None
        query_key = _remove_language_tokens_from_bag_v84(
            query.bag_key, category_languages
        )
        if not query_key or len(query_key.replace(" ", "")) < 4:
            return None
        for region in query.route_plan:
            matches: list[CandidateContextV8] = []
            for target in self.state.by_region.get(region, ()):
                if not self._compatible(query, target):
                    continue
                if not language_is_edge_annotation(target.strict_key):
                    continue
                target_key = _remove_language_tokens_from_bag_v84(
                    target.bag_key, category_languages
                )
                if target_key == query_key:
                    matches.append(target)
            if not matches:
                continue
            families: dict[tuple[Any, ...], list[CandidateContextV8]] = defaultdict(list)
            for target in matches:
                families[self._family_key(target)].append(target)
            if len(families) != 1:
                continue
            return self._result_from_group(
                query, next(iter(families.values())),
                method="category_language_equivalence", score=97.5,
                reason=(
                    "After an exact language-specific search, removing only the "
                    "language already supplied by the category leaves one complete "
                    "catalog identity"
                ),
            )
        return None

    def _regional_catalog_extension_match(
        self, query: ChannelContextV8
    ) -> dict[str, Any] | None:
        """Allow one canonical regional suffix on an already-regional India brand.

        This is the reverse of provider-extra containment. It is restricted to an
        Indian query already carrying at least two region tokens, so generic brands
        can never be expanded to an arbitrary local or directional schedule.
        """
        if "IN" not in query.route_plan:
            return None
        query_tokens = set(query.bag_key.split())
        query_regions = query_tokens & REGIONAL_CANONICAL_TOKENS_V84
        if len(query_regions) < 2:
            return None
        stable = query_tokens - REGIONAL_CANONICAL_TOKENS_V84
        if not stable:
            return None
        matches: list[CandidateContextV8] = []
        for target in self.state.by_region.get("IN", ()):
            if not self._compatible(query, target):
                continue
            target_tokens = set(target.bag_key.split())
            extras = target_tokens - query_tokens
            if not extras or len(extras) > 2:
                continue
            if not query_tokens.issubset(target_tokens):
                continue
            if not extras <= REGIONAL_CANONICAL_TOKENS_V84:
                continue
            if (target_tokens - REGIONAL_CANONICAL_TOKENS_V84) != stable:
                continue
            matches.append(target)
        if not matches:
            return None
        families: dict[tuple[Any, ...], list[CandidateContextV8]] = defaultdict(list)
        for target in matches:
            families[self._family_key(target)].append(target)
        if len(families) != 1:
            return None
        return self._result_from_group(
            query, next(iter(families.values())),
            method="regional_catalog_extension", score=97.0,
            reason=(
                "The provider and catalog share the complete brand and multiple "
                "regional identities; the catalog adds only one canonical region "
                "suffix such as HP/Himachal"
            ),
        )

    def _near_exact_orthographic_match(
        self, query: ChannelContextV8
    ) -> dict[str, Any] | None:
        """Auto-resolve one unique spelling/transliteration variant, never a fuzzy family."""
        if not query.bag_key:
            return None
        for region in query.route_plan:
            candidates: list[tuple[CandidateContextV8, float, str, str]] = []
            for target in self.state.by_region.get(region, ()):
                if not self._compatible(query, target):
                    continue
                variant = _single_token_orthographic_variant_v84(
                    query.bag_key, target.bag_key
                )
                if variant is None:
                    continue
                q_word, t_word, ratio = variant
                candidates.append((target, ratio, q_word, t_word))
            if not candidates:
                continue
            best_ratio = max(item[1] for item in candidates)
            top = [item for item in candidates if item[1] == best_ratio]
            families: dict[tuple[Any, ...], list[CandidateContextV8]] = defaultdict(list)
            for target, _ratio, _qword, _tword in top:
                families[self._family_key(target)].append(target)
            if len(families) != 1:
                continue
            q_word, t_word = top[0][2], top[0][3]
            return self._result_from_group(
                query, next(iter(families.values())),
                method="near_exact_orthography", score=round(best_ratio, 1),
                reason=(
                    f"All identity tokens are exact except one conservative "
                    f"spelling/transliteration variant ({q_word} ↔ {t_word}); "
                    "only one compatible catalog family exists"
                ),
            )
        return None

    def _coverage_block(self, query: ChannelContextV8) -> dict[str, Any] | None:
        if not query.route_explicit or not query.route_plan or query.route_plan == ("ALL",):
            return None
        available = [
            region for region in query.route_plan
            if bool(self.state.by_region.get(region))
        ]
        if available:
            return None
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "",
            "reason": (
                f"No enabled EPGShare catalog covers {'/'.join(query.route_plan)}; "
                f"{query.route_reason}. Cross-country fuzzy matching was suppressed"
            ),
            "match_method": "unsupported_catalog_scope",
        }

    def _synthetic_slot_match(
        self, row: Mapping[str, Any], query: ChannelContextV8
    ) -> dict[str, Any] | None:
        """Detect provider-generated numbered genre banks, not real brands."""
        category = str(row.get("category_name", ""))
        channel = str(row.get("channel_name", ""))
        category_words = set(_basic_words_v8(category))
        channel_words = _basic_words_v8(channel)
        number_words = {word for word in channel_words if word.isdigit()}
        if not number_words:
            return None

        normalized_original = " ".join(channel_words)
        if (
            "360" in category_words
            and re.search(r"\b360\s+\d+\b", normalized_original)
            and set(channel_words) & {"sport", "sports", "eurosport", "eurosports"}
        ):
            return {
                "action": "AUTO_DUMMY", "source": "dummy",
                "epg_id": self.engine.dummy_id("PPV.EVENTS.Dummy.us"),
                "epg_feed": "DUMMY_CHANNELS", "best_score": "",
                "second_epg_id": "", "second_epg_feed": "",
                "second_score": "", "score_margin": "",
                "reason": "Numbered 360 multiscreen sports bank changes with the live event",
                "match_method": "virtual_360_event_bank",
            }

        category_content = _content_from_tokens_v8(category_words)
        query_content = set(query.content)
        is_movies = "movies" in category_content or "movies" in query_content
        is_sports = "sports" in category_content or "sports" in query_content
        if not (is_movies or is_sports):
            return None

        # Require an explicit generic genre-number construction such as
        # MALAYALAM-MOVIES 5, TAMIL CINEMA 12, SPORTS SLOT 7.
        genre_number = re.search(
            r"\b(?:movies|cinema|sports|events?|slots?|streams?|channels?)\s+(?:no\s*)?\d+\b",
            normalized_original,
        )
        if not genre_number:
            return None

        allowed = (
            GENERIC_SLOT_TOKENS_V82
            | set(LANGUAGE_MAP_V8)
            | set(LANGUAGE_MAP_V8.values())
            | REGIONAL_CONTEXT_TOKENS_V82
            | number_words
        )
        distinctive = set(query.tokens) - allowed
        if distinctive:
            return None

        if is_movies:
            epg_id = self.engine.dummy_id("Movie.Dummy.us")
            reason = "Generic numbered movie/cinema bank has no stable linear schedule"
        else:
            epg_id = self.engine.dummy_id("PPV.EVENTS.Dummy.us")
            reason = "Generic numbered sports/event bank changes with each provider event"
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": epg_id, "epg_feed": "DUMMY_CHANNELS",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "", "reason": reason,
            "match_method": "synthetic_numbered_genre_slot",
        }

    def _heading_placeholder_match(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        if not _is_decorative_heading_v84(str(row.get("channel_name", ""))):
            return None
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": self.engine.dummy_id("Blank.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS", "best_score": "",
            "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "",
            "reason": "Decorative category heading/placeholder has no playable linear schedule",
            "match_method": "heading_placeholder",
        }

    def _inventory_bank_match(self, signal: Mapping[str, Any] | None) -> dict[str, Any] | None:
        role = str((signal or {}).get("role", ""))
        if role not in {"movie_bank", "event_bank"}:
            return None
        movie = role == "movie_bank"
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": self.engine.dummy_id("Movie.Dummy.us" if movie else "PPV.EVENTS.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS", "best_score": "",
            "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "",
            "reason": (
                f"Lineup-level numbered {'movie' if movie else 'event'} bank "
                f"({signal.get('distinct_slots', '?')} distinct slots) has no stable one-to-one schedule"
            ),
            "match_method": "inventory_numbered_bank",
        }

    def _legacy_verified_match(self, row: dict[str, Any], query: ChannelContextV8, *, pre_panel: bool) -> dict[str, Any] | None:
        contextual_row = dict(row)
        contextual_row["channel_name"] = query.core_name
        regions = query.route_plan
        if pre_panel:
            # Try the untouched provider name first so legacy rules that rely on a
            # leading route wrapper (for example US beIN) keep their old behavior.
            # Then try the parsed core so wrappers such as PB: or MY: no longer block
            # an otherwise exact rule.
            for candidate_row in (row, contextual_row):
                for region in regions:
                    for resolver in (
                        lambda r, reg=region: self.engine.direct_rule(r, reg),
                        lambda r, reg=region: self.engine.india_language_match_v7(r, reg),
                        lambda r, reg=region: self.engine.fanduel_match(r, reg),
                        lambda r, reg=region: self.engine.league_team_match_v5(r, reg),
                    ):
                        match = resolver(candidate_row)
                        if match:
                            result = dict(match)
                            result.setdefault("match_method", "verified_legacy_rule")
                            result["reason"] = f"Contextual parsing + verified existing rule: {result.get('reason', '')}"
                            # This branch contains only catalog-verified legacy direct/rebrand
                            # knowledge. It may intentionally select a source hosted under a
                            # different catalog region (for example Utsav Gold UK or beIN).
                            # Generic exact/fuzzy matching below still obeys the route plan.
                            return result
            return None

        # Code-first station resolution operates on the original name because
        # local call signs and subchannels can be carried in provider brackets.
        if "US" in regions:
            for resolver in (self.engine.callsign_match_v7, self.engine.pbs_brand_match_v7):
                match = resolver(row)
                if match:
                    result = dict(match)
                    result.setdefault("match_method", "verified_station_identity")
                    return result

        exact_resolvers = (
            self.engine.spectrum_news_match_v5,
            self.engine.canada_market_alias_match_v6,
            self.engine.canada_local_network_match_v5,
            self.engine.directional_exact_match_v7,
            self.engine.alias_exact_match_v7,
            self.engine.strong_exact_identity_match_v6,
            self.engine.exact_alias_variant_match_v5,
            self.engine.safe_brand_descriptor_match_v5,
            self.engine.panel_id_catalog_match_v5,
            self.engine.exact_family_match,
            self.engine.safe_panel_hint,
            self.engine.language_context_match,
        )
        for region in regions:
            for resolver in exact_resolvers:
                try:
                    match = resolver(contextual_row, region)
                except TypeError:
                    continue
                if match:
                    result = dict(match)
                    result.setdefault("match_method", "verified_legacy_exact")
                    result["reason"] = f"Contextual parsing + verified exact matcher: {result.get('reason', '')}"
                    epg_id = str(result.get("epg_id", ""))
                    item = self.engine.ID_LOOKUP.get(epg_id.casefold())
                    if query.route_explicit and item and str(item.get("region", "")).upper() not in query.route_plan:
                        continue
                    return result
        return None

    def _fuzzy_score(self, query: ChannelContextV8, target: CandidateContextV8) -> float:
        if not self._compatible(query, target):
            return -1.0
        wratio = fuzz.WRatio(query.strict_key, target.strict_key)
        token = fuzz.token_set_ratio(query.strict_key, target.strict_key)
        simple = fuzz.ratio(query.strict_key, target.strict_key)
        compact = fuzz.ratio(query.compact_key, target.compact_key) if query.compact_key and target.compact_key else 0.0
        score = 0.35 * wratio + 0.30 * token + 0.20 * simple + 0.15 * compact
        q_tokens = set(query.relaxed_key.split())
        t_tokens = set(target.relaxed_key.split())
        distinctive_q = q_tokens - OPTIONAL_DESCRIPTOR_TOKENS_V8 - {"and"}
        if distinctive_q and distinctive_q.issubset(t_tokens):
            score += 2.0
        if query.relaxed_key and target.relaxed_key and (query.relaxed_key in target.relaxed_key or target.relaxed_key in query.relaxed_key):
            score += 1.0
        return min(100.0, score)

    def _contextual_fuzzy_match(self, query: ChannelContextV8) -> dict[str, Any] | None:
        identity_tokens = set(query.relaxed_key.split()) - OPTIONAL_DESCRIPTOR_TOKENS_V8 - {"and"}
        if len(identity_tokens) == 1 and len(next(iter(identity_tokens))) < 5:
            return None
        considered: dict[tuple[str, str], tuple[CandidateContextV8, float]] = {}
        for region in query.route_plan:
            contexts = self.state.by_region.get(region, [])
            keys = self.state.candidate_keys.get(region, [])
            if not contexts or not keys or not query.strict_key:
                continue
            matches = process.extract(query.strict_key, keys, scorer=fuzz.WRatio, limit=30, score_cutoff=58)
            key_set = {match[0] for match in matches}
            for target in contexts:
                if target.strict_key not in key_set:
                    continue
                score = self._fuzzy_score(query, target)
                if score < 0:
                    continue
                ident = (str(target.candidate.get("feed", "")), str(target.candidate.get("epg_id", "")))
                old = considered.get(ident)
                if old is None or score > old[1]:
                    considered[ident] = (target, score)
        if not considered:
            return None

        ranked = sorted(
            considered.values(),
            key=lambda pair: (-pair[1], self.engine.candidate_preference(dict(pair[0].candidate), {
                "channel_name": query.core_name, "category_name": query.category_name,
            })),
        )
        # Collapse duplicate IDs/families so the margin compares different channel
        # identities, not IN1 versus IN4 copies of the same schedule.
        family_best: list[tuple[CandidateContextV8, float]] = []
        seen_families: set[tuple[Any, ...]] = set()
        for target, score in ranked:
            family_key = self._family_key(target)
            if family_key in seen_families:
                continue
            seen_families.add(family_key)
            family_best.append((target, score))
        best, best_score = family_best[0]
        if best_score < 80.0:
            return None
        second, second_score = family_best[1] if len(family_best) > 1 else (None, 0.0)
        margin = best_score - second_score if second else best_score

        # Fuzzy similarity is advisory only in v8. Automatic assignments require
        # structural exactness, verified aliases, panel data, or station identity.
        action = "REVIEW"
        source = "epgshare_candidate"
        return {
            "action": action, "source": source,
            "epg_id": best.candidate["epg_id"], "epg_feed": best.candidate["feed"],
            "best_score": round(best_score, 1),
            "second_epg_id": second.candidate["epg_id"] if second else "",
            "second_epg_feed": second.candidate["feed"] if second else "",
            "second_score": round(second_score, 1) if second else "",
            "score_margin": round(margin, 1),
            "reason": (
                f"Contextual candidate only; fuzzy similarity never auto-assigns in Smart Rules v{SMART_RULES_V8_VERSION}"
            ),
            "match_method": "contextual_fuzzy",
        }

    def resolve(
        self, row: dict[str, Any], *, panel_is_usable: bool = False,
        category_profile: Mapping[str, Any] | None = None,
        inventory_signal: Mapping[str, Any] | None = None,
    ) -> tuple[ChannelContextV8, dict[str, Any]]:
        query = _parse_channel_context_impl_v8(
            self.engine, str(row.get("channel_name", "")),
            str(row.get("category_name", "")), category_profile=category_profile,
        )

        heading = self._heading_placeholder_match(row)
        if heading:
            return query, heading

        special = self.engine.special_rule(row)
        if special:
            result = dict(special); result.setdefault("match_method", "safety_rule")
            return query, result

        inventory_bank = self._inventory_bank_match(inventory_signal)
        if inventory_bank:
            return query, inventory_bank

        synthetic = self._synthetic_slot_match(row, query)
        if synthetic:
            return query, synthetic

        coverage_block = self._coverage_block(query)
        if coverage_block:
            if panel_is_usable:
                return query, {
                    "action": "KEEP_PANEL", "source": "panel",
                    "epg_id": str(row.get("panel_epg_id", "")), "epg_feed": "server xmltv.php",
                    "best_score": "", "second_epg_id": "", "second_epg_feed": "",
                    "second_score": "", "score_margin": "",
                    "reason": "Panel XMLTV supplies the unsupported market's useful current/future schedule",
                    "match_method": "panel",
                }
            return query, coverage_block

        approved = self._approved_alias_match(query)
        if approved:
            return query, approved
        pre = self._legacy_verified_match(row, query, pre_panel=True)
        if pre:
            return query, pre
        exact = self._contextual_exact_match(query)
        if exact:
            return query, exact
        contained = self._contextual_containment_match(query)
        if contained:
            return query, contained
        language_equivalent = self._category_language_equivalence_match(query)
        if language_equivalent:
            return query, language_equivalent
        regional_extension = self._regional_catalog_extension_match(query)
        if regional_extension:
            return query, regional_extension
        orthographic = self._near_exact_orthographic_match(query)
        if orthographic:
            return query, orthographic

        if panel_is_usable:
            return query, {
                "action": "KEEP_PANEL", "source": "panel",
                "epg_id": str(row.get("panel_epg_id", "")), "epg_feed": "server xmltv.php",
                "best_score": "", "second_epg_id": "", "second_epg_feed": "",
                "second_score": "", "score_margin": "",
                "reason": "Panel XMLTV contains useful current/future programme information",
                "match_method": "panel",
            }

        verified = self._legacy_verified_match(row, query, pre_panel=False)
        if verified:
            return query, verified
        fuzzy = self._contextual_fuzzy_match(query)
        if fuzzy:
            return query, fuzzy

        if query.route_explicit and all(region not in self.state.by_region for region in query.route_plan):
            reason = f"No enabled EPGShare source is configured for {'/'.join(query.route_plan)}"
        elif query.explicit_market == "NA_DIASPORA":
            reason = "Explicit North-American South-Asian edition has no verified exact US or Canadian schedule; India was not used as a fallback"
        else:
            reason = f"No safe contextual match was found in {'/'.join(query.route_plan)}"
        return query, {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "", "reason": reason,
            "match_method": "unmatched",
        }


    def make_report(
        self,
        server_id: str,
        channels: list[dict[str, Any]],
        category_names: dict[str, str],
        panel_quality: dict[str, dict[str, Any]],
        real_candidates: list[dict[str, str]],
        dummy_ids: dict[str, str],
    ) -> pd.DataFrame:
        self.prepare(real_candidates, dummy_ids)
        profiles, signals = _build_inventory_profiles_v84(channels, category_names)
        records: list[dict[str, Any]] = []
        for channel in channels:
            stream_id = str(channel.get("stream_id") or channel.get("id") or "").strip()
            if not stream_id:
                continue
            channel_name = str(channel.get("name") or channel.get("stream_display_name") or "").strip()
            category_id = str(channel.get("category_id") or "").strip()
            category_name = category_names.get(category_id, "")
            panel_epg_id = str(channel.get("epg_channel_id") or "").strip()
            channel_number = str(channel.get("num") or channel.get("channel_number") or "").strip()
            if panel_epg_id:
                panel_info = panel_quality.get(panel_epg_id.casefold(), {
                    "status": "NOT_CHECKED", "current_or_future_informative_rows": 0,
                    "latest_stop_utc": "", "reason": "Panel EPG ID was not checked",
                })
            else:
                panel_info = {
                    "status": "NO_PANEL_ID", "current_or_future_informative_rows": 0,
                    "latest_stop_utc": "", "reason": "Server channel has no panel EPG ID",
                }
            row = {"category_name": category_name, "channel_name": channel_name, "panel_epg_id": panel_epg_id}
            signal = signals.get(stream_id, {})
            query, match = self.resolve(
                row, panel_is_usable=bool(panel_epg_id and panel_info.get("status") == "USABLE"),
                category_profile=profiles.get(category_id), inventory_signal=signal,
            )
            record: dict[str, Any] = {
                "server_id": server_id, "stream_id": stream_id, "category_id": category_id,
                "category_name": category_name, "category_namespace": query.category_namespace,
                "category_topic": query.category_topic, "category_market": query.category_market,
                "inventory_role": str(signal.get("role", "linear")),
                "channel_number": channel_number, "channel_name": channel_name,
                "normalized_name": query.strict_key, "parsed_channel_name": query.core_name,
                "detected_region": query.explicit_market or (query.route_plan[0] if query.route_plan else "ALL"),
                "route_plan": " > ".join(query.route_plan), "route_reason": query.route_reason,
                "provider_wrappers": " | ".join(query.wrapper_tokens),
                "language_hints": " | ".join(sorted(query.languages)),
                "panel_epg_id": panel_epg_id, "panel_epg_status": str(panel_info.get("status", "")),
                "panel_usable_programmes": int(panel_info.get("current_or_future_informative_rows", 0) or 0),
                "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
                "action": "", "source": "", "epg_id": "", "epg_feed": "",
                "best_score": "", "second_epg_id": "", "second_epg_feed": "",
                "second_score": "", "score_margin": "", "reason": "", "match_method": "",
            }
            for key in (
                "action", "source", "epg_id", "epg_feed", "best_score", "second_epg_id",
                "second_epg_feed", "second_score", "score_margin", "reason", "match_method",
            ):
                record[key] = match.get(key, "")
            records.append(record)
        return pd.DataFrame(records)

    def classify_review_row(self, row: dict[str, Any]) -> dict[str, Any]:
        query, match = self.resolve(row, panel_is_usable=False)
        return {
            "proposed_action": match.get("action", ""), "proposed_source": match.get("source", ""),
            "proposed_epg_id": match.get("epg_id", ""), "proposed_epg_feed": match.get("epg_feed", ""),
            "proposed_best_score": match.get("best_score", ""),
            "proposed_second_epg_id": match.get("second_epg_id", ""),
            "proposed_second_epg_feed": match.get("second_epg_feed", ""),
            "proposed_second_score": match.get("second_score", ""),
            "proposed_score_margin": match.get("score_margin", ""),
            "proposed_reason": match.get("reason", ""),
            "proposed_region": query.explicit_market or (query.route_plan[0] if query.route_plan else "ALL"),
            "parsed_channel_name": query.core_name, "route_plan": " > ".join(query.route_plan),
            "route_reason": query.route_reason, "provider_wrappers": " | ".join(query.wrapper_tokens),
            "language_hints": " | ".join(sorted(query.languages)), "match_method": match.get("match_method", ""),
            "category_namespace": query.category_namespace, "category_topic": query.category_topic,
            "category_market": query.category_market, "inventory_role": "linear",
        }

    def classify_review_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        records = [dict(row) for row in frame.to_dict("records")]
        category_names: dict[str, str] = {}
        channels: list[dict[str, Any]] = []
        for index, row in enumerate(records):
            category_id = str(row.get("category_id") or f"category-{index}")
            category_names.setdefault(category_id, str(row.get("category_name", "")))
            channels.append({
                "stream_id": str(row.get("stream_id") or f"row-{index}"),
                "category_id": category_id, "name": str(row.get("channel_name", "")),
            })
        profiles, signals = _build_inventory_profiles_v84(channels, category_names)
        proposals: list[dict[str, Any]] = []
        for index, row in enumerate(records):
            category_id = str(row.get("category_id") or f"category-{index}")
            stream_id = str(row.get("stream_id") or f"row-{index}")
            query, match = self.resolve(
                row, panel_is_usable=False, category_profile=profiles.get(category_id),
                inventory_signal=signals.get(stream_id),
            )
            proposals.append({
                "proposed_action": match.get("action", ""), "proposed_source": match.get("source", ""),
                "proposed_epg_id": match.get("epg_id", ""), "proposed_epg_feed": match.get("epg_feed", ""),
                "proposed_best_score": match.get("best_score", ""),
                "proposed_second_epg_id": match.get("second_epg_id", ""),
                "proposed_second_epg_feed": match.get("second_epg_feed", ""),
                "proposed_second_score": match.get("second_score", ""),
                "proposed_score_margin": match.get("score_margin", ""),
                "proposed_reason": match.get("reason", ""),
                "proposed_region": query.explicit_market or (query.route_plan[0] if query.route_plan else "ALL"),
                "parsed_channel_name": query.core_name, "route_plan": " > ".join(query.route_plan),
                "route_reason": query.route_reason, "provider_wrappers": " | ".join(query.wrapper_tokens),
                "language_hints": " | ".join(sorted(query.languages)),
                "match_method": match.get("match_method", ""),
                "category_namespace": query.category_namespace, "category_topic": query.category_topic,
                "category_market": query.category_market,
                "inventory_role": str(signals.get(stream_id, {}).get("role", "linear")),
            })
        return pd.DataFrame(proposals, index=frame.index)




V8_POSITIVE_CASES: tuple[tuple[str, str, str, str, str], ...] = (
    ("Punjabi wrapper exact", "PUNJABI", "PB: PTC PUNJABI", "AUTO_EPGSHARE", "PTC.PUNJABI.in"),
    ("Punjabi USA diaspora override", "PUNJABI", "PB | PTC PUNJABI | USA", "AUTO_EPGSHARE", "PTC.Punjabi.TV.ca2"),
    ("Punjabi bare USA suffix", "PUNJABI", "PB: PTC PUNJABI USA HD", "AUTO_EPGSHARE", "PTC.Punjabi.TV.ca2"),
    ("Punjabi Gold quality ignored", "PUNJABI", "PB: PTC Punjabi Gold HD", "AUTO_EPGSHARE", "PTC.PUNJABI.GOLD.in"),
    ("Punjabi Simran wrapper", "PUNJABI", "PB: PTC SIMRAN", "AUTO_EPGSHARE", "PTC.Simran.in"),
    ("Punjabi Chakde compact spelling", "PUNJABI", "PB: PTC CHAKDE", "AUTO_EPGSHARE", "PTC.CHAK.DE.in"),
    ("MH One number word", "PUNJABI", "PB: MH ONE", "AUTO_EPGSHARE", "MH.ONE.in"),
    ("Malayalam wrapper Asianet", "MALAYALAM | ENTRTNMNT", "MY: ASIANET (4K).", "AUTO_EPGSHARE", "ASIANET.HD.in"),
    ("Cricket wrapper Telugu", "CRICKET", "CRIC || STAR SPORTS 1 TELUGU 4K", "AUTO_EPGSHARE", "STAR.SPORTS.1.TELUGU.HD.in"),
    ("USA Network remains brand", "USA", "US: USA Network East", "AUTO_EPGSHARE", "USA.Network.HD.us2"),
    ("Attached quality wrapper", "PUNJABI", "PBHD: PTC PUNJABI", "AUTO_EPGSHARE", "PTC.PUNJABI.in"),
    ("Hyphen wrapper separator", "PUNJABI", "PB-PTC PUNJABI", "AUTO_EPGSHARE", "PTC.PUNJABI.in"),
    ("Leading USA diaspora token", "PUNJABI", "USA PTC PUNJABI", "AUTO_EPGSHARE", "PTC.Punjabi.TV.ca2"),
    ("Underscore USA diaspora token", "PUNJABI", "US_PTC PUNJABI", "AUTO_EPGSHARE", "PTC.Punjabi.TV.ca2"),
    ("Alphanumeric provider wrapper", "CRICKET", "D2H: STAR SPORTS 1 TELUGU HD", "AUTO_EPGSHARE", "STAR.SPORTS.1.TELUGU.HD.in"),
    ("Provider phrase wrapper", "PUNJABI", "TATA PLAY: PTC PUNJABI HD", "AUTO_EPGSHARE", "PTC.PUNJABI.in"),

    # v8.2 canonical identity and metadata parsing.
    ("FS2 acronym metadata", "USA", "USA: Fox Sport 2 HD (FS2)", "AUTO_EPGSHARE", "FS2.Fox.Sports.2.HD.us2"),
    ("FS1 acronym metadata", "USA", "USA: Fox Sport 1 HD (FS1)", "AUTO_EPGSHARE", "FS1.Fox.Sports.1.HD.us2"),
    ("FS1 language bracket", "FIFA World Cup 2026", "Fox Sports 1 (FS1) [English]", "AUTO_EPGSHARE", "FS1.Fox.Sports.1.HD.us2"),
    ("FS2 language bracket", "FIFA World Cup 2026", "Fox Sports 2 (FS2) [English]", "AUTO_EPGSHARE", "FS2.Fox.Sports.2.HD.us2"),
    ("News18 regional bundle containment", "PUNJABI", "PB : News18 Punjab/Haryana/Himachal", "AUTO_EPGSHARE", "News18.Punjab.Haryana.in"),
    ("Colors singular brand", "UK| ASIAN", "UK : COLOR SD", "AUTO_EPGSHARE", "COLORS.HD.uk"),
    ("PTC News redundant language bracket", "PUNJABI", "PB: PTC News (PUNJABI)", "AUTO_EPGSHARE", "PTC.NEWS.in"),
    ("Sky brand retained after UK wrapper", "UK| MOVIES", "UK: SKY CINEMA HITS FHD", "AUTO_EPGSHARE", "Sky.Cinema.Hits.HD.uk"),
    ("Sky Cinema Select technical bracket", "UK| MOVIES", "UK: SKY CINEMA SELECT (FHD)", "AUTO_EPGSHARE", "Sky.Cinema.Select.uk"),
    ("Sky Cinema Comedy technical bracket", "UK| MOVIES", "UK: SKY CINEMA COMEDY (FHD)", "AUTO_EPGSHARE", "Sky.Cinema.Comedy.uk"),
    ("Verified Discovery Turbo schedule relationship", "UK| DOCUMENTARY", "UK SD : Discovery Turbo", "AUTO_EPGSHARE", "Discovery.HD.uk"),
    ("Verified Discovery Turbo plus one relationship", "UK| DOCUMENTARY", "UK SD : Discovery Turbo +1", "AUTO_EPGSHARE", "Discovery+1.uk"),
    ("Discovery abbreviation Science", "UK| DOCUMENTARY", "UK SD : Discovery Science", "AUTO_EPGSHARE", "Disc.Science.uk"),
    ("Discovery abbreviation History", "UK| DOCUMENTARY", "UKSD Discovery History", "AUTO_EPGSHARE", "Disc.History.uk"),
    ("BBC South West directional abbreviation", "UK| GENERAL", "UK SD : BBC One South West", "AUTO_EPGSHARE", "BBC.One.S.West.HD.uk"),
    ("Telugu TL wrapper", "TELUGU", "TL : CALVARY TV", "AUTO_EPGSHARE", "Calvary.in"),

    # Generic numbered genre-bank classification.
    ("Malayalam numbered movie slot 5", "MALAYALAM | MOVIES", "MALAYALAM-MOVIES 5 HD", "AUTO_DUMMY", "Movie.Dummy.us"),
    ("Malayalam numbered movie slot 10", "MALAYALAM | MOVIES", "MALAYALAM-MOVIES 10 HD", "AUTO_DUMMY", "Movie.Dummy.us"),
    ("Tamil numbered cinema slot", "TAMIL | MOVIES", "TAMIL CINEMA 8 FHD", "AUTO_DUMMY", "Movie.Dummy.us"),
    ("Hindi numbered movie channel", "HINDI | MOVIES", "HINDI MOVIES CHANNEL 12 HD", "AUTO_DUMMY", "Movie.Dummy.us"),
    ("Generic numbered sports slot", "SPORTS | LIVE", "SPORTS SLOT 7 HD", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),

    # Unsupported catalog scopes must stop before cross-country fuzzy matching.
    ("Unsupported MBC Arabic", "MBC ARABIC", "MBC 1 HD", "UNMATCHED", ""),
    ("Unsupported Arabic News", "ARABIC NEWS", "Al Arabiya HD", "UNMATCHED", ""),
    ("Unsupported Islamic", "ISLAMIC", "Peace TV HD", "UNMATCHED", ""),
    ("Unsupported Israel", "ISRAEL", "Israel Channel 12", "UNMATCHED", ""),
    ("Unsupported Norway", "NORWAY", "NO: NRK 1 HD", "UNMATCHED", ""),
    ("Unsupported Finland", "FINLAND", "FI: YLE TV1 HD", "UNMATCHED", ""),
    ("Unsupported Algeria", "ALGERIA", "DZ: ENTV HD", "UNMATCHED", ""),
    ("Unsupported Lebanon", "LEBANON", "LB: LBCI HD", "UNMATCHED", ""),
    ("Unsupported Italy", "ITALY", "IT: RAI 1 HD", "UNMATCHED", ""),
    ("Unsupported Iraq", "IRAQ", "IQ: Al Iraqiya HD", "UNMATCHED", ""),
    ("Unsupported Qatar", "QATAR", "QA: Al Rayyan HD", "UNMATCHED", ""),
    ("Unsupported Egypt", "EGYPT", "EG: ON TV HD", "UNMATCHED", ""),
    ("Unsupported Russia", "RUSSIAN", "RU: Channel One HD", "UNMATCHED", ""),
    ("Unsupported EXYU", "EXYU", "EX-YU: Nova TV HD", "UNMATCHED", ""),
    ("Unsupported Romania", "ROMANIA", "RO: Pro TV HD", "UNMATCHED", ""),
    ("Unsupported Brazil", "BRASIL", "BR: Globo HD", "UNMATCHED", ""),
    ("Unsupported Netherlands", "NETHERLANDS", "NL: NPO 1 HD", "UNMATCHED", ""),
    ("Unsupported Sweden", "SWEDEN", "SE: SVT 1 HD", "UNMATCHED", ""),
    ("Unsupported Czechia", "CZECH", "CZ: CT 1 HD", "UNMATCHED", ""),
    ("Unsupported Bulgaria", "BULGARIA", "BG: BNT 1 HD", "UNMATCHED", ""),
    ("Unsupported Indonesia", "INDONESIA", "ID: RCTI HD", "UNMATCHED", ""),
    ("Unsupported Sri Lanka", "SRI LANKA", "Color tamil", "UNMATCHED", ""),
)

V8_POSITIVE_CASES += (
    ("AS sports category echo", "|AS| SPORTS", "SPORTS - DD SPORTS", "AUTO_EPGSHARE", "DD.Sports.in"),
    ("AS sports English default", "|AS| SPORTS", "SPORTS - STAR SPORTS 1 ENGLISH UHD", "AUTO_EPGSHARE", "STAR.SPORTS.1.HD.in"),
    ("AS Hindi order one", "|AS| SPORTS", "SPORTS - STAR SPORTS HINDI 1 FHD", "AUTO_EPGSHARE", "Star.Sports.Hindi.1.in"),
    ("AS Hindi order two", "|AS| SPORTS", "SPORTS - STAR SPORTS 1 HINDI UHD", "AUTO_EPGSHARE", "STAR.SPORTS.1.HD.HINDI.in"),
    ("AS English prefix", "|AS| ENGLISH", "ENG - AND PRIVE UHD", "AUTO_EPGSHARE", "and.PRIVE.HD.in"),
    ("AS English Star Movies", "|AS| ENGLISH", "ENG - STAR MOVIES UHD", "AUTO_EPGSHARE", "STAR.MOVIES.HD.in"),
    ("AS Bangla prefix", "|AS| BANGLA", "BGL - ZEE BANGLA", "AUTO_EPGSHARE", "ZEE.BANGLA.in"),
    ("AS Punjabi prefix", "|AS| PUNJABI", "PJB - PTC SIMRAN", "AUTO_EPGSHARE", "PTC.Simran.in"),
    ("Punjab adjective morphology", "|AS| PUNJABI", "PJB - GLOBAL PUNJABI UHD", "AUTO_EPGSHARE", "Global.Punjab.in"),
    ("Chardikala morphology", "|AS| PUNJABI", "PJB - CHARDIKALAH TIMES TV", "AUTO_EPGSHARE", "Chardikala.Time.TV.in"),
    ("AS Gujarati prefix", "|AS| GUJARATI", "GJR - COLORS GUJARATI", "AUTO_EPGSHARE", "COLORS.GUJARATI.in"),
    ("Verified Gujarati TV9 schedule", "|AS| GUJARATI", "GJR - TV9 NEWS", "AUTO_EPGSHARE", "VIP.News.in"),
    ("Punjabi one-token transliteration", "|AS| PUNJABI", "PJB - MH1 SHARADDHA", "AUTO_EPGSHARE", "mh1.Shraddha.in"),
    ("Punjabi regional catalog suffix", "|AS| PUNJABI", "PJB - ZEE PUNJAB HARYANA", "AUTO_EPGSHARE", "Zee.Punjab.Haryana.HP.in"),
    ("India News brand begins with market word", "|AS| PUNJABI", "PJB - INDIA NEWS PUNJAB", "AUTO_EPGSHARE", "India.News.Punjab.in"),
    ("beIN catalog encoding metadata", "|AR| BEIN SPORTS NX", "BEIN SPORTS XTRA 1 4K", "AUTO_EPGSHARE", "beIN_SPORTS_XTRA1_Digital_Mono_AR.bein"),
    ("UK U and catalog namespace", "|UK| ENTERTAINMENT", "UK - YESTERDAY FHD", "AUTO_EPGSHARE", "U.and.YESTERDAY.uk"),
    ("Tamil category language equivalence", "|AS| TAMIL", "TAM - SUN NEWS TAMIL", "AUTO_EPGSHARE", "SUN.NEWS.in"),
    ("Decorative heading", "|AR| BEIN SPORTS 8K", "####### BEIN SPORTS HD #######", "AUTO_DUMMY", "Blank.Dummy.us"),
    ("Eurosport 360 bank", "|FR| EUROSPORTS 360", "FR - EUROSPORT 360 3 HD", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
    ("Unsupported EU film category", "|EU| LE MEILLEUR DES FILMS", "FR - OCS GO CINEMA 1 HD", "UNMATCHED", ""),
    ("Unsupported Arabic Rotana", "|AR| ROTANA | ART", "AR - ROTANA CINEMA", "UNMATCHED", ""),
)


# Full Server 3 direct-match regression set supplied during v8.4 development.
# These are tests only: the resolver still reaches them through structural
# parsing, catalog indexes, source authority and reusable knowledge rules.
V84_SERVER3_DIRECT_REGRESSION_CASES: tuple[tuple[str, str, str, str, str], ...] = (
    ('Server 3 direct 01: SPORTS - DD SPORTS', '|AS| SPORTS', 'SPORTS - DD SPORTS', "AUTO_EPGSHARE", 'DD.Sports.in'),
    ('Server 3 direct 02: SPORTS - STAR SPORTS 1 ENGLISH UHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS 1 ENGLISH UHD', "AUTO_EPGSHARE", 'STAR.SPORTS.1.HD.in'),
    ('Server 3 direct 03: SPORTS - STAR SPORTS HINDI 1 FHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS HINDI 1 FHD', "AUTO_EPGSHARE", 'Star.Sports.Hindi.1.in'),
    ('Server 3 direct 04: SPORTS - STAR SPORTS 1 HINDI UHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS 1 HINDI UHD', "AUTO_EPGSHARE", 'STAR.SPORTS.1.HD.HINDI.in'),
    ('Server 3 direct 05: SPORTS - STAR SPORTS 1 FHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS 1 FHD', "AUTO_EPGSHARE", 'STAR.SPORTS.1.HD.in'),
    ('Server 3 direct 06: SPORTS - STAR SPORTS 2 FHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS 2 FHD', "AUTO_EPGSHARE", 'STAR.SPORTS.2.HD.in'),
    ('Server 3 direct 07: SPORTS - STAR SPORTS 2 UHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS 2 UHD', "AUTO_EPGSHARE", 'STAR.SPORTS.2.HD.in'),
    ('Server 3 direct 08: SPORTS - STAR SPORTS 3', '|AS| SPORTS', 'SPORTS - STAR SPORTS 3', "AUTO_EPGSHARE", 'STAR.SPORTS.3.in'),
    ('Server 3 direct 09: SPORTS - STAR SPORTS SELECT 1 FHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS SELECT 1 FHD', "AUTO_EPGSHARE", 'STAR.SPORTS.SELECT.1.HD.in'),
    ('Server 3 direct 10: SPORTS - STAR SPORTS SELECT 1 UHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS SELECT 1 UHD', "AUTO_EPGSHARE", 'STAR.SPORTS.SELECT.1.HD.in'),
    ('Server 3 direct 11: SPORTS - STAR SPORTS SELECT 2 FHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS SELECT 2 FHD', "AUTO_EPGSHARE", 'STAR.SPORTS.SELECT.2.HD.in'),
    ('Server 3 direct 12: SPORTS - STAR SPORTS SELECT 2 UHD', '|AS| SPORTS', 'SPORTS - STAR SPORTS SELECT 2 UHD', "AUTO_EPGSHARE", 'STAR.SPORTS.SELECT.2.HD.in'),
    ('Server 3 direct 13: SPORTS - STAR SPORTS FIRST', '|AS| SPORTS', 'SPORTS - STAR SPORTS FIRST', "AUTO_EPGSHARE", 'Star.Sports.First.in'),
    ('Server 3 direct 14: ENG - AND PRIVE UHD', '|AS| ENGLISH', 'ENG - AND PRIVE UHD', "AUTO_EPGSHARE", 'and.PRIVE.HD.in'),
    ('Server 3 direct 15: ENG - STAR MOVIES UHD', '|AS| ENGLISH', 'ENG - STAR MOVIES UHD', "AUTO_EPGSHARE", 'STAR.MOVIES.HD.in'),
    ('Server 3 direct 16: ENG - STAR MOVIES SELECT UHD', '|AS| ENGLISH', 'ENG - STAR MOVIES SELECT UHD', "AUTO_EPGSHARE", 'STAR.MOVIES.SELECT.HD.in'),
    ('Server 3 direct 17: ENG - COLORS INFINITY UHD', '|AS| ENGLISH', 'ENG - COLORS INFINITY UHD', "AUTO_EPGSHARE", 'COLORS.INFINITY.HD.in'),
    ('Server 3 direct 18: ENG - TIMES NOW UHD', '|AS| ENGLISH', 'ENG - TIMES NOW UHD', "AUTO_EPGSHARE", 'TIMES.NOW.in'),
    ('Server 3 direct 19: ENG - TLC WORLD UHD', '|AS| ENGLISH', 'ENG - TLC WORLD UHD', "AUTO_EPGSHARE", 'TLC.HD.World.in'),
    ('Server 3 direct 20: ENG - TLC UHD', '|AS| ENGLISH', 'ENG - TLC UHD', "AUTO_EPGSHARE", 'TLC.HD.in'),
    ('Server 3 direct 21: ENG - TRAVEL XP UHD', '|AS| ENGLISH', 'ENG - TRAVEL XP UHD', "AUTO_EPGSHARE", 'TRAVEL.XP.(SD).in'),
    ('Server 3 direct 22: ENG - ALJAZEERA UHD', '|AS| ENGLISH', 'ENG - ALJAZEERA UHD', "AUTO_EPGSHARE", 'ALJAZEERA.in'),
    ('Server 3 direct 23: ENG - AND FLIX UHD', '|AS| ENGLISH', 'ENG - AND FLIX UHD', "AUTO_EPGSHARE", 'and.Flix.HD.in'),
    ('Server 3 direct 24: ENG - SONY PIX UHD', '|AS| ENGLISH', 'ENG - SONY PIX UHD', "AUTO_EPGSHARE", 'SONY.PIX.HD.in'),
    ('Server 3 direct 25: ENG - ROMEDY NOW UHD', '|AS| ENGLISH', 'ENG - ROMEDY NOW UHD', "AUTO_EPGSHARE", 'ROMEDY.NOW.in'),
    ('Server 3 direct 26: ENG - ZEE CAFE UHD', '|AS| ENGLISH', 'ENG - ZEE CAFE UHD', "AUTO_EPGSHARE", 'ZEE.CAFE.HD.in'),
    ('Server 3 direct 27: BGL - ZEE BANGLA', '|AS| BANGLA', 'BGL - ZEE BANGLA', "AUTO_EPGSHARE", 'ZEE.BANGLA.in'),
    ('Server 3 direct 28: BGL - ZEE BANGLA CINEMA', '|AS| BANGLA', 'BGL - ZEE BANGLA CINEMA', "AUTO_EPGSHARE", 'ZEE.BANGLA.CINEMA.in'),
    ('Server 3 direct 29: BGL - STAR JALSHA MOVIES UHD', '|AS| BANGLA', 'BGL - STAR JALSHA MOVIES UHD', "AUTO_EPGSHARE", 'Star.Jalsha.Movies.in'),
    ('Server 3 direct 30: BGL - STAR JALSHA UHD', '|AS| BANGLA', 'BGL - STAR JALSHA UHD', "AUTO_EPGSHARE", 'STAR.JALSHA.HD.in'),
    ('Server 3 direct 31: BGL - COLORS BANGLA UHD', '|AS| BANGLA', 'BGL - COLORS BANGLA UHD', "AUTO_EPGSHARE", 'COLORS.BANGLA.HD.in'),
    ('Server 3 direct 32: BGL - 24 GHANTA', '|AS| BANGLA', 'BGL - 24 GHANTA', "AUTO_EPGSHARE", '24.GHANTA.in'),
    ('Server 3 direct 33: BGL - NEWS 24 FHD', '|AS| BANGLA', 'BGL - NEWS 24 FHD', "AUTO_EPGSHARE", 'NEWS.24.in'),
    ('Server 3 direct 34: PJB - ZEE PUNJABI', '|AS| PUNJABI', 'PJB - ZEE PUNJABI', "AUTO_EPGSHARE", 'Zee.Punjabi.in'),
    ('Server 3 direct 35: PJB - GLOBAL PUNJABI UHD', '|AS| PUNJABI', 'PJB - GLOBAL PUNJABI UHD', "AUTO_EPGSHARE", 'Global.Punjab.in'),
    ('Server 3 direct 36: PJB - PRIME ASIA UHD', '|AS| PUNJABI', 'PJB - PRIME ASIA UHD', "AUTO_EPGSHARE", 'Prime.Asia.HD.in'),
    ('Server 3 direct 37: PJB - SANJHA TV UHD', '|AS| PUNJABI', 'PJB - SANJHA TV UHD', "AUTO_EPGSHARE", 'Sanjha.TV.in'),
    ('Server 3 direct 38: PJB - PTC SIMRAN', '|AS| PUNJABI', 'PJB - PTC SIMRAN', "AUTO_EPGSHARE", 'PTC.Simran.in'),
    ('Server 3 direct 39: PJB - BRIT ASIA', '|AS| PUNJABI', 'PJB - BRIT ASIA', "AUTO_EPGSHARE", 'Brit.Asia.in'),
    ('Server 3 direct 40: PJB - CHARDIKALAH TIMES TV', '|AS| PUNJABI', 'PJB - CHARDIKALAH TIMES TV', "AUTO_EPGSHARE", 'Chardikala.Time.TV.in'),
    ('Server 3 direct 41: PJB - DD PUNJABI', '|AS| PUNJABI', 'PJB - DD PUNJABI', "AUTO_EPGSHARE", 'DD.PUNJABI.in'),
    ('Server 3 direct 42: PJB - DESI CHANNEL', '|AS| PUNJABI', 'PJB - DESI CHANNEL', "AUTO_EPGSHARE", 'Desi.Channel.in'),
    ('Server 3 direct 43: PJB - FATEH TV', '|AS| PUNJABI', 'PJB - FATEH TV', "AUTO_EPGSHARE", 'Fateh.TV.in'),
    ('Server 3 direct 44: PJB - INDIA NEWS PUNJAB', '|AS| PUNJABI', 'PJB - INDIA NEWS PUNJAB', "AUTO_EPGSHARE", 'India.News.Punjab.in'),
    ('Server 3 direct 45: PJB - JUS ONE', '|AS| PUNJABI', 'PJB - JUS ONE', "AUTO_EPGSHARE", 'JUSOne.in'),
    ('Server 3 direct 46: PJB - LIVING INDIA NEWS', '|AS| PUNJABI', 'PJB - LIVING INDIA NEWS', "AUTO_EPGSHARE", 'Living.India.News.in'),
    ('Server 3 direct 47: PJB - MANORANJAN MOVIES', '|AS| PUNJABI', 'PJB - MANORANJAN MOVIES', "AUTO_EPGSHARE", 'Manoranjan.Movies.in2'),
    ('Server 3 direct 48: PJB - MH1 MUSIC', '|AS| PUNJABI', 'PJB - MH1 MUSIC', "AUTO_EPGSHARE", 'mh1.(Music).in'),
    ('Server 3 direct 49: PJB - MH1 SHARADDHA', '|AS| PUNJABI', 'PJB - MH1 SHARADDHA', "AUTO_EPGSHARE", 'mh1.Shraddha.in'),
    ('Server 3 direct 50: PJB - PITAARA', '|AS| PUNJABI', 'PJB - PITAARA', "AUTO_EPGSHARE", 'Pitaara.in'),
    ('Server 3 direct 51: PJB - NEWS 18 PUNJAB HARYANA', '|AS| PUNJABI', 'PJB - NEWS 18 PUNJAB HARYANA', "AUTO_EPGSHARE", 'News18.Punjab.Haryana.in'),
    ('Server 3 direct 52: PJB - PTC CHAKDE', '|AS| PUNJABI', 'PJB - PTC CHAKDE', "AUTO_EPGSHARE", 'PTC.CHAK.DE.in'),
    ('Server 3 direct 53: PJB - PTC NEWS', '|AS| PUNJABI', 'PJB - PTC NEWS', "AUTO_EPGSHARE", 'PTC.NEWS.in'),
    ('Server 3 direct 54: PJB - PTC PUNJABI', '|AS| PUNJABI', 'PJB - PTC PUNJABI', "AUTO_EPGSHARE", 'PTC.PUNJABI.in'),
    ('Server 3 direct 55: PJB - PTC PUNJABI USA', '|AS| PUNJABI', 'PJB - PTC PUNJABI USA', "AUTO_EPGSHARE", 'PTC.Punjabi.TV.ca2'),
    ('Server 3 direct 56: PJB - PTC PUNJABI UK', '|AS| PUNJABI', 'PJB - PTC PUNJABI UK', "AUTO_EPGSHARE", 'PTC.PUNJABI.uk'),
    ('Server 3 direct 57: PJB - ZEE PUNJAB HARYANA', '|AS| PUNJABI', 'PJB - ZEE PUNJAB HARYANA', "AUTO_EPGSHARE", 'Zee.Punjab.Haryana.HP.in'),
    ('Server 3 direct 58: PJB - WAH PUNJABI', '|AS| PUNJABI', 'PJB - WAH PUNJABI', "AUTO_EPGSHARE", 'Wah.Punjabi.in'),
    ('Server 3 direct 59: PJB - MH1 NEWS', '|AS| PUNJABI', 'PJB - MH1 NEWS', "AUTO_EPGSHARE", 'Mh.One.News.in'),
    ('Server 3 direct 60: GJR - DIVYANG NEWS', '|AS| GUJARATI', 'GJR - DIVYANG NEWS', "AUTO_EPGSHARE", 'Divyang.News.in'),
    ('Server 3 direct 61: GJR - TV9 NEWS', '|AS| GUJARATI', 'GJR - TV9 NEWS', "AUTO_EPGSHARE", 'VIP.News.in'),
    ('Server 3 direct 62: GJR - ABP ASMITA', '|AS| GUJARATI', 'GJR - ABP ASMITA', "AUTO_EPGSHARE", 'ABP.ASMITA.in'),
    ('Server 3 direct 63: GJR - CNBC BAZAR', '|AS| GUJARATI', 'GJR - CNBC BAZAR', "AUTO_EPGSHARE", 'CNBC.BAJAR.in'),
    ('Server 3 direct 64: GJR - COLORS GUJARATI', '|AS| GUJARATI', 'GJR - COLORS GUJARATI', "AUTO_EPGSHARE", 'COLORS.GUJARATI.in'),
    ('Server 3 direct 65: GJR - COLORS GUJARATI CINEMA', '|AS| GUJARATI', 'GJR - COLORS GUJARATI CINEMA', "AUTO_EPGSHARE", 'COLORS.GUJARATI.CINEMA.in'),
    ('Server 3 direct 66: GJR - KARTAVYA TV', '|AS| GUJARATI', 'GJR - KARTAVYA TV', "AUTO_EPGSHARE", 'Kartavya.TV.in'),
    ('Server 3 direct 67: GJR - MANTAVYA NEWS', '|AS| GUJARATI', 'GJR - MANTAVYA NEWS', "AUTO_EPGSHARE", 'Mantavya.News.in'),
    ('Server 3 direct 68: GJR - SANDESH NEWS', '|AS| GUJARATI', 'GJR - SANDESH NEWS', "AUTO_EPGSHARE", 'Sandesh.News.in'),
    ('Server 3 direct 69: GJR - LAKSHYA TV UHD', '|AS| GUJARATI', 'GJR - LAKSHYA TV UHD', "AUTO_EPGSHARE", 'Lakshya.TV.in'),
    ('Server 3 direct 70: GJR - VTV NEWS', '|AS| GUJARATI', 'GJR - VTV NEWS', "AUTO_EPGSHARE", 'VTV.NEWS.in'),
)

V8_POSITIVE_CASES += V84_SERVER3_DIRECT_REGRESSION_CASES


V8_SAFETY_CASES: tuple[tuple[str, str, str, set[str]], ...] = (
    ("USA suffix must not fall back to India", "TAMIL | MOVIES", "TM: KTV FHD (usa)", {"KTV.in", "KTV.HD.in"}),
    ("Discovery Science not generic Discovery", "USA", "US Discovery Science", {"Discovery.Channel.HD.us2", "The.Discovery.Channel.HD.us2"}),
    ("ITV Be not ITV1 plus one", "UK| ENTERTAINMENT", "UKHD: ITV Be", {"ITV1+1.uk", "ITV.1.+1.uk"}),
    ("Gold remains distinct from base Punjabi", "PUNJABI", "PB: PTC Punjabi Gold HD", {"PTC.PUNJABI.in", "PTC.Punjabi.in"}),
    ("Explicit diaspora never silently uses India", "PUNJABI", "PB | Unknown Punjabi Brand | USA", {"PTC.PUNJABI.in", "PTC.Punjabi.in"}),
    ("Real branded movie channel is not a synthetic slot", "IN | MOVIES", "IN | Star Movies 1 HD", {"Movie.Dummy.us", "PPV.EVENTS.Dummy.us"}),
    ("Named Malayalam movie stream is not a numbered bank", "MALAYALAM | MOVIES", "MALAYALAM-MADHU MOVIES HD", {"Movie.Dummy.us", "PPV.EVENTS.Dummy.us"}),
    ("Brazil Discovery cannot borrow India or UK", "BRASIL", "BR: Discovery Science HD", {"Discovery.Science.in", "Disc.Science.uk"}),
    ("Norway Discovery cannot borrow India or UK", "NORWAY", "|NO| Discovery Science (ALLENTE)", {"Discovery.Science.in", "Disc.Science.uk"}),
    ("Fox Sports South is not generic Fox Sports", "USA", "USA: FOX SPORTS SOUTH", {"Fox.Sports.4K.us2"}),
    ("Zee Punjabi News is not generic Zee News", "PUNJABI", "PB : Zee Punjabi News", {"Zee.News.in"}),
    ("Sri Lanka cannot borrow an Indian language feed", "SRI LANKA", "Color tamil", {"Colors.Tamil.in"}),
)


def _run_smart_rules_v8_self_test(
    resolver: ContextualMatcherV8,
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
    *,
    include_legacy: bool = True,
) -> pd.DataFrame:
    """Run all legacy v7 cases plus v8 contextual regression/safety cases."""
    resolver.prepare(real_candidates, dummy_ids)
    rows: list[dict[str, Any]] = []

    if include_legacy:
        legacy = resolver.engine.run_smart_rules_v7_self_test(real_candidates, dummy_ids).copy()
        legacy.insert(0, "suite", "legacy_v7")
        rows.extend(legacy.to_dict("records"))
        # v7's self-test prepared its own global indexes. Restore v8's exact state
        # (normally a cheap fingerprint reuse) before contextual cases.
        resolver.prepare(real_candidates, dummy_ids)

    for label, category, channel, expected_action, expected_id in V8_POSITIVE_CASES:
        query, actual = resolver.resolve({
            "category_name": category,
            "channel_name": channel,
            "panel_epg_id": "",
        })
        passed = (
            str(actual.get("action", "")) == expected_action
            and str(actual.get("epg_id", "")).casefold() == expected_id.casefold()
        )
        rows.append({
            "suite": "contextual_v8", "test": label, "check_type": "positive",
            "passed": passed, "expected_action": expected_action,
            "actual_action": actual.get("action", ""), "expected_epg_id": expected_id,
            "forbidden_epg_ids": "", "actual_epg_id": actual.get("epg_id", ""),
            "reason": actual.get("reason", ""), "parsed_channel_name": query.core_name,
            "route_plan": " > ".join(query.route_plan),
        })

    forced_actions = {"AUTO_EPGSHARE", "KEEP_PANEL", "MANUAL", "APPROVED"}
    for label, category, channel, forbidden_ids in V8_SAFETY_CASES:
        query, actual = resolver.resolve({
            "category_name": category,
            "channel_name": channel,
            "panel_epg_id": "",
        })
        actual_id = str(actual.get("epg_id", ""))
        forbidden = {value.casefold() for value in forbidden_ids}
        passed = not (
            str(actual.get("action", "")) in forced_actions
            and actual_id.casefold() in forbidden
        )
        rows.append({
            "suite": "contextual_v8", "test": label, "check_type": "safety",
            "passed": passed, "expected_action": "not forced to forbidden mapping",
            "actual_action": actual.get("action", ""), "expected_epg_id": "",
            "forbidden_epg_ids": " | ".join(sorted(forbidden_ids)),
            "actual_epg_id": actual_id, "reason": actual.get("reason", ""),
            "parsed_channel_name": query.core_name,
            "route_plan": " > ".join(query.route_plan),
        })

    # Batch-level profiling distinguishes virtual numbered banks from real
    # linear numbered networks.
    bank_channels = [
        {"stream_id": f"bank-{number}", "category_id": "bank", "name": f"UK| SKY STORE PREMIERE {number} HD"}
        for number in range(2, 7)
    ]
    bank_profiles, bank_signals = _build_inventory_profiles_v84(
        bank_channels, {"bank": "|UK| SKY STORE"}
    )
    for item in bank_channels:
        query, actual = resolver.resolve(
            {"category_name": "|UK| SKY STORE", "channel_name": item["name"], "panel_epg_id": ""},
            category_profile=bank_profiles.get("bank"),
            inventory_signal=bank_signals.get(item["stream_id"]),
        )
        rows.append({
            "suite": "contextual_v8", "test": f"Inventory movie bank {item['stream_id']}",
            "check_type": "batch_profile",
            "passed": actual.get("action") == "AUTO_DUMMY" and str(actual.get("epg_id", "")).casefold() == "movie.dummy.us",
            "expected_action": "AUTO_DUMMY", "actual_action": actual.get("action", ""),
            "expected_epg_id": "Movie.Dummy.us", "forbidden_epg_ids": "",
            "actual_epg_id": actual.get("epg_id", ""), "reason": actual.get("reason", ""),
            "parsed_channel_name": query.core_name, "route_plan": " > ".join(query.route_plan),
        })

    result = pd.DataFrame(rows)
    failures = result.loc[~result["passed"].astype(bool)]
    if not failures.empty:
        try:
            resolver.engine.display(failures)
        except Exception:
            print(failures.to_string(index=False))
        raise RuntimeError(f"Smart Rules v{SMART_RULES_V8_VERSION} self-test failed. Stop before matching the server.")
    return result


def install_contextual_v8(engine: Any) -> ContextualMatcherV8:
    existing = getattr(engine, "_SKYTV_CONTEXTUAL_V8", None)
    if isinstance(existing, ContextualMatcherV8):
        return existing
    resolver = ContextualMatcherV8(engine)
    engine._SKYTV_CONTEXTUAL_V8 = resolver
    engine._smart_v8_prepare_matcher = resolver.prepare
    engine.parse_channel_context_v8 = lambda channel_name, category_name="": _parse_channel_context_impl_v8(engine, channel_name, category_name)
    engine.read_approved_alias_rows_v8 = read_approved_alias_rows_v8
    engine.register_approved_aliases_v8 = resolver.register_approved_aliases
    engine.load_approved_aliases_v8 = resolver.load_approved_aliases
    engine.approved_alias_table_v8 = resolver.approved_alias_table
    engine.export_approved_alias_memory_v8 = resolver.export_approved_alias_memory
    engine.build_schedule_fingerprints_v8 = lambda xmltv_path, epg_ids, **kwargs: _build_schedule_fingerprints_impl_v8(engine, xmltv_path, epg_ids, **kwargs)
    engine.schedule_similarity_v8 = schedule_similarity_v8
    engine.discover_schedule_equivalence_groups_v8 = discover_schedule_equivalence_groups_v8
    engine.load_schedule_equivalence_groups_v8 = load_schedule_equivalence_groups_v8
    engine.register_schedule_equivalences_v8 = resolver.register_schedule_equivalences
    engine.load_schedule_equivalences_v8 = resolver.load_schedule_equivalences
    def make_matching_report_v8(
        server_id: str,
        channels: list[dict[str, Any]],
        category_names: dict[str, str],
        panel_quality: dict[str, dict[str, Any]],
        real_candidates: list[dict[str, str]],
        dummy_ids: dict[str, str],
    ) -> pd.DataFrame:
        return resolver.make_report(
            server_id, channels, category_names, panel_quality,
            real_candidates, dummy_ids,
        )

    def classify_review_row_v8(row: dict[str, Any]) -> dict[str, Any]:
        return resolver.classify_review_row(row)

    def run_smart_rules_v8_self_test(
        real_candidates: list[dict[str, str]],
        dummy_ids: dict[str, str],
        *,
        include_legacy: bool = True,
    ) -> pd.DataFrame:
        return _run_smart_rules_v8_self_test(
            resolver, real_candidates, dummy_ids, include_legacy=include_legacy
        )

    make_matching_report_v8.smart_rules_version = SMART_RULES_V8_VERSION
    make_matching_report_v8.matcher_build_id = MATCHER_V8_BUILD_ID
    run_smart_rules_v8_self_test.smart_rules_version = SMART_RULES_V8_VERSION
    run_smart_rules_v8_self_test.matcher_build_id = MATCHER_V8_BUILD_ID
    engine.make_matching_report_v8 = make_matching_report_v8
    engine.classify_review_row_v8 = classify_review_row_v8
    engine.classify_review_frame_v8 = resolver.classify_review_frame
    engine.build_inventory_profiles_v8 = _build_inventory_profiles_v84
    engine.run_smart_rules_v8_self_test = run_smart_rules_v8_self_test
    engine.SMART_RULES_VERSION = SMART_RULES_V8_VERSION
    engine.MATCHER_BUILD_ID = MATCHER_V8_BUILD_ID
    return resolver
