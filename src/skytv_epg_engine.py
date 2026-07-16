from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import pandas as pd
import requests
from IPython.display import display
from lxml import etree
from rapidfuzz import fuzz, process

# Output policy requested for the TiviMate guide.
DEFAULT_PAST_DAYS = 3
REFRESH_SAFETY_HOURS = 12

# Build-layer revision. The Smart Rules matcher itself remains v7.0.
BUILDER_VERSION = "7.1"
BUILDER_BUILD_ID = "SKYTV-TIVIMATE-BUILDER-7.1-2026-07-15"

# Server details are held only in this running Colab session. They are never
# written to the inventory ZIP or TiviMate output ZIP. Restarting the runtime
# clears this dictionary and the temporary panel XMLTV cache.
if not isinstance(globals().get("_SKYTV_RUNTIME_SERVER_CONTEXTS"), dict):
    _SKYTV_RUNTIME_SERVER_CONTEXTS: dict[str, dict[str, str]] = {}


def runtime_panel_cache_path(server_id: str) -> Path:
    cache_dir = Path("/content/skytv_epg_runtime_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{safe_server_id(server_id)}_panel.xmltv"


def remember_runtime_server_context(
    server_id: str,
    base_url: str,
    username: str,
    password: str,
    panel_xmltv_path: str | Path = "",
) -> None:
    sid = safe_server_id(server_id)
    panel_path = str(panel_xmltv_path or "").strip()
    _SKYTV_RUNTIME_SERVER_CONTEXTS[sid] = {
        "base_url": safe_base_url(base_url),
        "username": str(username or ""),
        "password": str(password or ""),
        "panel_xmltv_path": panel_path,
    }


def get_runtime_server_context(server_id: str) -> dict[str, str] | None:
    sid = safe_server_id(server_id)
    context = _SKYTV_RUNTIME_SERVER_CONTEXTS.get(sid)
    return dict(context) if isinstance(context, dict) else None


def clear_runtime_server_context(server_id: str | None = None) -> None:
    if server_id is None:
        _SKYTV_RUNTIME_SERVER_CONTEXTS.clear()
        return
    _SKYTV_RUNTIME_SERVER_CONTEXTS.pop(safe_server_id(server_id), None)

# Conservative automatic matching. Doubtful results go to the review CSV.
AUTO_MATCH_SCORE = 95.0
AUTO_MATCH_MIN_MARGIN = 6.0
REVIEW_MATCH_SCORE = 80.0

SAFE_MAPPING_ACTIONS = {
    "KEEP_PANEL",
    "AUTO_EPGSHARE",
    "AUTO_DUMMY",
    "MANUAL",
    "APPROVED",
}

DEFAULT_EPGSHARE_FEEDS: dict[str, dict[str, Any]] = {
    "US_SPORTS1": {
        "region": "US", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    },
    "US_LOCALS1": {
        "region": "US", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    },
    "US2": {
        "region": "US", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_US2.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    },
    "FANDUEL1": {
        "region": "US", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_FANDUEL1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_FANDUEL1.xml.gz",
    },
    "UK1": {
        "region": "UK", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_UK1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    },
    "IN4": {
        "region": "IN", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_IN4.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_IN4.xml.gz",
    },
    "IN2": {
        "region": "IN", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_IN2.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_IN2.xml.gz",
    },
    "IN1": {
        "region": "IN", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_IN1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz",
    },
    "CA2": {
        "region": "CA", "kind": "real",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_CA2.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    },
    "DUMMY_CHANNELS": {
        "region": "DUMMY", "kind": "dummy",
        "txt_url": "https://epgshare01.online/epgshare01/epg_ripper_DUMMY_CHANNELS.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_DUMMY_CHANNELS.xml.gz",
    },
}

# Active registry. The source-manager cell resets this from the defaults and
# merges any future feeds supplied by the user.
EPGSHARE_FEEDS: dict[str, dict[str, Any]] = {
    name: dict(details) for name, details in DEFAULT_EPGSHARE_FEEDS.items()
}

NOISE_WORDS = {
    "hd", "fhd", "uhd", "sd", "4k", "8k", "hevc", "h265", "h264",
    "1080p", "1080i", "720p", "50fps", "60fps", "vip", "backup",
    "raw", "feed", "channel", "ch", "test", "multi",
}

REGION_PREFIX_RE = re.compile(
    r"^\s*(?:us|usa|u\.s\.?|uk|gb|in|india|ca|canada)"
    r"(?:\s*[:|/\\-]+\s*|\s+)",
    re.IGNORECASE,
)
PPV_RE = re.compile(r"\b(?:ppv|pay\s*per\s*view)\b", re.IGNORECASE)
TWENTY_FOUR_SEVEN_RE = re.compile(
    r"(?:\b24\s*[/x.-]\s*7\b|\b24\s+7\b|\b24hr\b|\b24\s*hours?\b)",
    re.IGNORECASE,
)


# Rules that identify provider-created slot channels. These rules inspect BOTH
# the category name and channel name before fuzzy EPG matching is attempted.
EVENT_CATEGORY_RE = re.compile(r"\bevents?\b", re.IGNORECASE)
EVENT_CHANNEL_RE = re.compile(r"\bevents?\b", re.IGNORECASE)
NUMBERED_EVENT_CHANNEL_RE = re.compile(
    r"\bevents?(?:\s*(?:channel|ch|feed|stream|slot))?\s*[-:#|]*\s*\d+\b",
    re.IGNORECASE,
)
ONEFOOTBALL_RE = re.compile(r"\bone\s*football\b|\bonefootball\b", re.IGNORECASE)
MAIN_EVENT_LINEAR_RE = re.compile(r"\b(?:sky\s+sports\s+)?main\s+event\b", re.IGNORECASE)
MOVIE_DUMMY_CONTEXT_RE = re.compile(
    r"\b(?:cinemania|cinema|movie|movies|film|films|new\s+release|"
    r"hollywood|bollywood|netflix)\b",
    re.IGNORECASE,
)
PT_SPORTS_CATEGORY_RE = re.compile(
    r"(?:^|[|:/\\-])\s*pt\s*(?:[|:/\\-])\s*sports?\b|\bportugal\b.*\bsports?\b",
    re.IGNORECASE,
)

# These exact aliases are deliberately small and conservative. They run before
# fuzzy matching and only apply to channels positively identified as US channels.
PREFERRED_EPG_RULES: list[dict[str, Any]] = [
    {
        "label": "US Fox Sports 1",
        "regions": {"US"},
        "pattern": re.compile(r"\b(?:fox\s+sports\s*1|fs\s*1|fs1)\b", re.IGNORECASE),
        "feed": "US2",
        "epg_ids": ["FS1.Fox.Sports.1.HD.us2", "FS1.HD.us2"],
    },
    {
        "label": "US Fox Sports 2",
        "regions": {"US"},
        "pattern": re.compile(r"\b(?:fox\s+sports\s*2|fs\s*2|fs2)\b", re.IGNORECASE),
        "feed": "US2",
        "epg_ids": ["FS2.Fox.Sports.2.HD.us2", "FS2.HD.us2"],
    },
    {
        "label": "US NFL RedZone",
        "regions": {"US"},
        "pattern": re.compile(r"\bnfl\s*red\s*zone\b|\bnfl\s*redzone\b", re.IGNORECASE),
        "feed": "US2",
        "epg_ids": ["NFL.RedZone.HD.us2"],
    },
    {
        "label": "US YES Network",
        "regions": {"US"},
        "pattern": re.compile(r"\b(?:bally\s+sports\s+yes|yes\s+network)\b", re.IGNORECASE),
        "feed": "US2",
        "epg_ids": ["Yes.Network.us2"],
    },
]

# Words that are too generic to prove that two channel names are the same.
MATCH_GENERIC_WORDS = NOISE_WORDS | {
    "tv", "network", "sports", "sport", "live", "plus", "extra",
    "east", "west", "the", "and", "of", "international",
}

FANDUEL_TRIGGER_RE = re.compile(
    r"\b(?:bally\s+sports|fanduel\s+sports|fanduel\s+tv)\b",
    re.IGNORECASE,
)

# Extra aliases for Bally -> FanDuel names that changed more than the brand.
FANDUEL_QUERY_ALIASES: dict[str, list[str]] = {
    "fanduel sports carolinas": ["fanduel sports south carolinas"],
    "fanduel sports cincinnati": ["fanduel sports ohio cincinnati"],
    "fanduel sports cleveland": ["fanduel sports ohio cleveland"],
    "fanduel sports florida extra": ["fanduel sports florida 2"],
    "fanduel sports kansas city": ["fanduel sports midwest kansas city"],
    "fanduel sports midwest": ["fanduel sports midwest st louis"],
    "fanduel sports south georgia": ["fanduel sports southeast georgia"],
}


# Titles like these do not provide useful EPG information. They are ignored for
# panel validation and are not written into the final XMLTV file.
NO_INFORMATION_TITLES = {
    "", "n a", "na", "none", "unknown", "tba", "to be announced",
    "no information", "no information available", "no programme information",
    "no program information", "no programme information available",
    "no program information available", "no epg", "no epg information",
    "no event", "no event information", "not available", "unavailable",
    "schedule unavailable", "programme unavailable", "program unavailable",
    "no data", "no schedule", "epg not available",
    "programme information not available", "program information not available",
}
NO_INFORMATION_TITLE_RE = re.compile(
    r"^(?:no|not)\s+(?:(?:programme|program|event|epg|schedule)\s+)?"
    r"(?:information\s*)?(?:available|unavailable)?$",
    re.IGNORECASE,
)


def normalize_programme_title_for_quality(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_informative_programme_title(value: object) -> bool:
    normalized = normalize_programme_title_for_quality(value)
    if normalized in NO_INFORMATION_TITLES:
        return False
    if NO_INFORMATION_TITLE_RE.fullmatch(normalized):
        return False
    return bool(normalized)


def parse_xmltv_time(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.strip().split()
    if not parts:
        return None
    stamp = parts[0]
    if not stamp.isdigit() or len(stamp) < 12:
        return None
    stamp = stamp[:14]
    fmt = "%Y%m%d%H%M%S" if len(stamp) >= 14 else "%Y%m%d%H%M"
    try:
        dt = datetime.strptime(stamp, fmt)
    except ValueError:
        return None

    tz_token = parts[1] if len(parts) > 1 else "+0000"
    if tz_token.upper() in {"Z", "UTC", "GMT"}:
        tz = timezone.utc
    else:
        match = re.fullmatch(r"([+-])(\d{2})(\d{2})", tz_token)
        if not match:
            tz = timezone.utc
        else:
            sign = 1 if match.group(1) == "+" else -1
            offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
            tz = timezone(sign * offset)
    return int(dt.replace(tzinfo=tz).timestamp())


def xmltv_timestamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(int(epoch_seconds), timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000"
    )


def epoch_iso(epoch_seconds: int | None) -> str:
    if epoch_seconds is None:
        return ""
    return datetime.fromtimestamp(int(epoch_seconds), timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def child_text(element: etree._Element, wanted_name: str) -> str:
    fallback = ""
    for child in element:
        if local_name(child.tag) != wanted_name:
            continue
        text = " ".join("".join(child.itertext()).split())
        if not text:
            continue
        language = (child.get("lang") or "").lower()
        if language in {"en", "eng", "en-us", "en-gb"}:
            return text
        if not fallback:
            fallback = text
    return fallback


EPG_SOURCE_CONFIG_SCHEMA_VERSION = 1
EPGSHARE_STANDARD_BASE_URL = "https://epgshare01.online/epgshare01"
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SOURCE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REGION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")

DEFAULT_REGION_MATCH_KEYWORDS: dict[str, set[str]] = {
    "US": {
        "us", "usa", "united states", "american", "fanduel",
        "nfl", "nba", "mlb", "nhl", "wnba", "ncaa",
    },
    "UK": {
        "uk", "united kingdom", "british", "england", "scotland",
        "wales", "northern ireland",
    },
    "CA": {
        "canada", "canadian", "ontario", "toronto", "vancouver",
        "calgary", "edmonton", "montreal",
    },
    "IN": {
        "india", "indian", "hindi", "punjabi", "tamil", "telugu",
        "malayalam", "kannada", "marathi", "bengali", "gujarati",
    },
}

EPG_REGION_MATCH_KEYWORDS: dict[str, set[str]] = {}


def _as_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _clean_keyword_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
    elif isinstance(value, Iterable):
        candidates = [str(item).strip() for item in value]
    else:
        raise TypeError("match_keywords must be a string or a list of strings.")
    return list(dict.fromkeys(item.lower() for item in candidates if item))


def _validate_source_url(value: object, label: str, *, required: bool = True) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        if required:
            raise ValueError(f"{label} is required.")
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{label} must not contain embedded credentials or a URL fragment.")
    return cleaned


def _standard_epgshare_urls(source_code: str) -> tuple[str, str]:
    code = str(source_code or "").strip()
    if not _SOURCE_CODE_RE.fullmatch(code):
        raise ValueError(
            "source_code may contain only letters, numbers, underscore, dot, or hyphen."
        )
    base = EPGSHARE_STANDARD_BASE_URL.rstrip("/")
    return (
        f"{base}/epg_ripper_{code}.txt",
        f"{base}/epg_ripper_{code}.xml.gz",
    )


def reset_epgshare_sources() -> None:
    """Restore built-in feeds and discard runtime additions."""
    EPGSHARE_FEEDS.clear()
    for raw_name, raw_details in DEFAULT_EPGSHARE_FEEDS.items():
        name = str(raw_name).strip().upper()
        details = dict(raw_details)
        details["region"] = str(details.get("region", "ALL")).strip().upper() or "ALL"
        details["kind"] = str(details.get("kind", "real")).strip().lower() or "real"
        details["source_code"] = str(details.get("source_code", name)).strip()
        details["match_keywords"] = _clean_keyword_list(details.get("match_keywords", []))
        details["use_for_matching"] = _as_bool(details.get("use_for_matching"), True)
        EPGSHARE_FEEDS[name] = details

    EPG_REGION_MATCH_KEYWORDS.clear()
    for region, keywords in DEFAULT_REGION_MATCH_KEYWORDS.items():
        EPG_REGION_MATCH_KEYWORDS[region] = set(keywords)


def register_epgshare_source(
    *,
    name: str = "",
    region: str = "ALL",
    source_code: str = "",
    txt_url: str = "",
    xml_url: str = "",
    kind: str = "real",
    match_keywords: object = None,
    use_for_matching: object = True,
    enabled: object = True,
    overwrite: bool = False,
) -> str | None:
    """Validate and add one source to the active registry."""
    if not _as_bool(enabled, True):
        return None

    code = str(source_code or "").strip()
    feed_name = str(name or code).strip().upper()
    if not feed_name or not _SOURCE_NAME_RE.fullmatch(feed_name):
        raise ValueError(
            "Source name must start with a letter or number and use only letters, "
            "numbers, underscore, dot, or hyphen (maximum 64 characters)."
        )

    region_code = str(region or "ALL").strip().upper() or "ALL"
    if not _REGION_RE.fullmatch(region_code):
        raise ValueError(f"Invalid region code for {feed_name}: {region_code!r}")

    source_kind = str(kind or "real").strip().lower()
    if source_kind not in {"real", "dummy"}:
        raise ValueError("kind must be 'real' or 'dummy'.")

    scan_catalog = _as_bool(use_for_matching, True)
    generated_txt = generated_xml = ""
    if code:
        generated_txt, generated_xml = _standard_epgshare_urls(code)

    final_txt = _validate_source_url(
        txt_url or generated_txt,
        f"txt_url for {feed_name}",
        required=scan_catalog,
    )
    final_xml = _validate_source_url(
        xml_url or generated_xml,
        f"xml_url for {feed_name}",
        required=True,
    )

    if feed_name in EPGSHARE_FEEDS and not overwrite:
        raise ValueError(
            f"EPG source {feed_name} already exists. Set overwrite=True only when "
            "you intentionally want to replace it."
        )

    keywords = _clean_keyword_list(match_keywords)
    EPGSHARE_FEEDS[feed_name] = {
        "region": region_code,
        "kind": source_kind,
        "source_code": code,
        "txt_url": final_txt,
        "xml_url": final_xml,
        "match_keywords": keywords,
        "use_for_matching": scan_catalog,
    }
    if region_code not in {"ALL", "DUMMY"} and keywords:
        EPG_REGION_MATCH_KEYWORDS.setdefault(region_code, set()).update(keywords)
    return feed_name


def register_epgshare_sources(
    config: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    overwrite: bool = False,
) -> list[str]:
    """Register sources from a list or from {"sources": [...]} JSON data."""
    if isinstance(config, Mapping):
        schema_version = config.get("schemaVersion", config.get("schema_version", 1))
        if int(schema_version) != EPG_SOURCE_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported EPG source schemaVersion {schema_version}; "
                f"expected {EPG_SOURCE_CONFIG_SCHEMA_VERSION}."
            )
        records = config.get("sources", [])
    else:
        records = config

    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise TypeError("EPG source configuration must contain a list of source records.")

    added: list[str] = []
    for index, raw_record in enumerate(records, start=1):
        if not isinstance(raw_record, Mapping):
            raise TypeError(f"Source record {index} is not an object.")
        record = dict(raw_record)
        name = register_epgshare_source(
            name=record.get("name", ""),
            region=record.get("region", "ALL"),
            source_code=record.get("source_code", record.get("sourceCode", "")),
            txt_url=record.get("txt_url", record.get("txtUrl", "")),
            xml_url=record.get("xml_url", record.get("xmlUrl", "")),
            kind=record.get("kind", "real"),
            match_keywords=record.get(
                "match_keywords", record.get("matchKeywords", [])
            ),
            use_for_matching=record.get(
                "use_for_matching", record.get("useForMatching", True)
            ),
            enabled=record.get("enabled", True),
            overwrite=overwrite,
        )
        if name:
            added.append(name)
    return added


def load_epg_source_config(
    location_or_data: str | Path | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    overwrite: bool = False,
) -> list[str]:
    """Load an EPG source list from JSON data, a local file, or an HTTP(S) URL."""
    if isinstance(location_or_data, Mapping) or (
        isinstance(location_or_data, Iterable)
        and not isinstance(location_or_data, (str, bytes, Path))
    ):
        payload = location_or_data
    else:
        location = str(location_or_data or "").strip()
        if not location:
            return []
        if location.startswith("{") or location.startswith("["):
            payload = json.loads(location)
        else:
            parsed = urlparse(location)
            if parsed.scheme.lower() in {"http", "https"}:
                response = requests.get(location, timeout=(20, 180))
                response.raise_for_status()
                payload = response.json()
            else:
                path = Path(location).expanduser()
                if not path.is_file():
                    raise FileNotFoundError(f"EPG source config not found: {path}")
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return register_epgshare_sources(payload, overwrite=overwrite)


def epg_source_config_payload(
    source_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a portable, credential-free snapshot of active sources."""
    selected = None
    if source_names is not None:
        selected = {str(name).strip().upper() for name in source_names}

    records: list[dict[str, Any]] = []
    for name, details in sorted(EPGSHARE_FEEDS.items()):
        if selected is not None and name not in selected:
            continue
        record: dict[str, Any] = {
            "enabled": True,
            "name": name,
            "region": str(details.get("region", "ALL")),
            "kind": str(details.get("kind", "real")),
            "txt_url": str(details.get("txt_url", "")),
            "xml_url": str(details.get("xml_url", "")),
            "match_keywords": list(details.get("match_keywords", [])),
            "use_for_matching": bool(details.get("use_for_matching", True)),
        }
        source_code = str(details.get("source_code", "")).strip()
        if source_code:
            record["source_code"] = source_code
        records.append(record)
    return {
        "schemaVersion": EPG_SOURCE_CONFIG_SCHEMA_VERSION,
        "sources": records,
    }


def epg_source_config_json(source_names: Iterable[str] | None = None) -> str:
    return json.dumps(
        epg_source_config_payload(source_names),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"


def save_epg_source_config(
    path: str | Path,
    source_names: Iterable[str] | None = None,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(epg_source_config_json(source_names), encoding="utf-8")
    return destination


def epg_source_table() -> pd.DataFrame:
    rows = []
    for name, details in sorted(EPGSHARE_FEEDS.items()):
        rows.append({
            "name": name,
            "region": details.get("region", "ALL"),
            "kind": details.get("kind", "real"),
            "use_for_matching": bool(details.get("use_for_matching", True)),
            "match_keywords": ", ".join(details.get("match_keywords", [])),
            "txt_url": details.get("txt_url", ""),
            "xml_url": details.get("xml_url", ""),
        })
    return pd.DataFrame(rows)


reset_epgshare_sources()


def safe_server_id(value: str) -> str:
    server_id = value.strip().lower()
    if server_id not in {"server_1", "server_2", "server_3"}:
        raise ValueError("Server ID must be server_1, server_2, or server_3.")
    return server_id


def safe_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    for suffix in ("/player_api.php", "/xmltv.php"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must begin with http:// or https:// and include a host.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use only the base URL; do not include credentials, query text, or fragments.")
    return cleaned


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SKYTV-EPG-Builder/2.0",
        "Accept": "application/json,application/xml,text/xml,text/plain,application/gzip,application/octet-stream,*/*",
    })
    return session


def normalize_api_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "streams", "live_streams", "channels"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        values = list(payload.values())
        if values and all(isinstance(item, dict) for item in values):
            return values
    return []


def panel_json_request(
    session: requests.Session,
    base_url: str,
    username: str,
    password: str,
    action: str,
) -> list[dict[str, Any]]:
    try:
        response = session.get(
            f"{base_url}/player_api.php",
            params={"username": username, "password": password, "action": action},
            timeout=(20, 180),
        )
        if response.status_code != 200:
            raise RuntimeError(f"Panel returned HTTP {response.status_code} for {action}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Panel returned non-JSON data for {action}.") from exc
        items = normalize_api_list(payload)
        if not items:
            raise RuntimeError(f"Panel returned no usable records for {action}.")
        return items
    except requests.RequestException:
        # Never include the credential-bearing request URL in the error.
        raise RuntimeError(f"Network request failed while running {action}.") from None


def download_streamed(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    params: Mapping[str, str] | None = None,
    attempts: int = 3,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            with session.get(url, params=params, stream=True, timeout=(25, 300)) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            if temporary.stat().st_size < 50:
                raise RuntimeError("Downloaded file is unexpectedly small.")
            temporary.replace(destination)
            return destination
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Could not download the requested source: {last_error}")


def open_maybe_gzip(path: Path):
    with path.open("rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return path.open("rb")


def validate_xmlish(path: Path) -> None:
    try:
        with open_maybe_gzip(path) as handle:
            prefix = handle.read(512).lstrip().lower()
    except OSError as exc:
        raise RuntimeError(f"{path.name} is not valid gzip/XML.") from exc
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        raise RuntimeError(f"{path.name} contains an HTML error page instead of XMLTV data.")
    if not prefix.startswith(b"<?xml") and b"<tv" not in prefix:
        raise RuntimeError(f"{path.name} does not look like XMLTV data.")


def panel_quality_unavailable_report(
    requested_ids: Mapping[str, str],
    status: str,
    reason: str,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for key, original in sorted(requested_ids.items()):
        row = {
            "panel_epg_id": original,
            "status": status,
            "total_programme_rows": 0,
            "informative_rows": 0,
            "current_or_future_informative_rows": 0,
            "placeholder_or_blank_rows": 0,
            "invalid_time_rows": 0,
            "latest_stop_utc": "",
            "reason": reason,
        }
        rows.append(row)
        lookup[key] = row
    return lookup, pd.DataFrame(rows)


def scan_panel_xmltv_quality(
    path: Path,
    requested_ids: Mapping[str, str],
    *,
    now_epoch: int | None = None,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    """Accept a panel EPG ID only when it has useful current/future programme titles."""
    now_value = int(now_epoch or datetime.now(timezone.utc).timestamp())
    counters: dict[str, dict[str, Any]] = {
        key: {
            "total_programme_rows": 0,
            "informative_rows": 0,
            "current_or_future_informative_rows": 0,
            "placeholder_or_blank_rows": 0,
            "invalid_time_rows": 0,
            "latest_stop": None,
        }
        for key in requested_ids
    }

    with open_maybe_gzip(path) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            if local_name(element.tag) != "programme":
                if local_name(element.tag) == "channel":
                    element.clear()
                    while element.getprevious() is not None:
                        del element.getparent()[0]
                continue

            channel_id = (element.get("channel") or "").strip()
            key = channel_id.casefold()
            if key not in counters:
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
                continue

            item = counters[key]
            item["total_programme_rows"] += 1
            title = child_text(element, "title")
            if not is_informative_programme_title(title):
                item["placeholder_or_blank_rows"] += 1
            else:
                start = parse_xmltv_time(element.get("start"))
                stop = parse_xmltv_time(element.get("stop"))
                if start is None:
                    item["invalid_time_rows"] += 1
                else:
                    if stop is None or stop <= start:
                        stop = start + 3600
                    item["informative_rows"] += 1
                    if stop >= now_value:
                        item["current_or_future_informative_rows"] += 1
                        if item["latest_stop"] is None or stop > item["latest_stop"]:
                            item["latest_stop"] = stop

            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
        del context

    rows: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for key, original in sorted(requested_ids.items()):
        item = counters[key]
        if item["current_or_future_informative_rows"] > 0:
            status = "USABLE"
            reason = "Panel has at least one useful current/future programme title"
        elif item["informative_rows"] > 0:
            status = "STALE_PAST_ONLY"
            reason = "Panel has titles, but no current/future programme information"
        elif item["total_programme_rows"] > 0:
            status = "NO_USEFUL_INFORMATION"
            reason = "Panel rows are blank or contain placeholder text such as No information"
        else:
            status = "NOT_IN_PANEL_XMLTV"
            reason = "Panel XMLTV has no programme rows for this EPG ID"

        row = {
            "panel_epg_id": original,
            "status": status,
            "total_programme_rows": int(item["total_programme_rows"]),
            "informative_rows": int(item["informative_rows"]),
            "current_or_future_informative_rows": int(
                item["current_or_future_informative_rows"]
            ),
            "placeholder_or_blank_rows": int(item["placeholder_or_blank_rows"]),
            "invalid_time_rows": int(item["invalid_time_rows"]),
            "latest_stop_utc": epoch_iso(item["latest_stop"]),
            "reason": reason,
        }
        rows.append(row)
        lookup[key] = row

    return lookup, pd.DataFrame(rows)


def parse_epgshare_ids(text: str) -> list[str]:
    parts = text.replace("\r", " ").replace("\n", " ").split("--")
    body = parts[-1] if len(parts) >= 3 else text
    result: list[str] = []
    seen: set[str] = set()
    for token in body.split():
        token = token.strip()
        if not token or "." not in token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def epg_id_to_name(epg_id: str) -> str:
    text = epg_id.strip()
    # EPGShare sometimes appends the catalog suffix to the country code
    # (for example .us2, .ca2, or .us_locals1). Remove that source suffix
    # before comparing it with the visible channel name.
    text = re.sub(
        r"\.(?:us(?:\d+|_locals\d+)?|uk\d*|in\d*|ie\d*|ca\d*|"
        r"au\d*|nz\d*|ch\d*|es\d*)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = text.replace("+", " plus ")
    text = re.sub(r"[._/\\-]+", " ", text)
    text = re.sub(r"\bdummy\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(value: str) -> str:
    value = REGION_PREFIX_RE.sub("", value or "")
    value = value.replace("&", " and ").replace("+", " plus ")
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    value = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [word for word in value.split() if word not in NOISE_WORDS]
    return " ".join(words)


def _region_keyword_matches(raw_text: str, keyword: str) -> bool:
    token = str(keyword or "").strip().lower()
    if not token:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
            raw_text,
            flags=re.IGNORECASE,
        )
    )


def detect_region(channel_name: str, category_name: str) -> str:
    raw = f"{category_name} {channel_name}".lower()

    # A configured region prefix such as "AU:" works automatically.
    configured_regions = sorted(
        {
            str(details.get("region", "ALL")).upper()
            for details in EPGSHARE_FEEDS.values()
            if str(details.get("region", "ALL")).upper() not in {"ALL", "DUMMY"}
        },
        key=lambda value: (-len(value), value),
    )
    for region in configured_regions:
        prefix = re.compile(rf"^\s*{re.escape(region)}\s*(?:[:|/\-]+)", re.IGNORECASE)
        if prefix.search(channel_name or "") or prefix.search(category_name or ""):
            return region

    # Preserve the original, carefully chosen region rules.
    if re.search(r"\b(?:india|indian|hindi|punjabi|tamil|telugu|malayalam|kannada|marathi|bengali|gujarati)\b", raw):
        return "IN"
    if re.search(r"\b(?:uk|united kingdom|british|england|scotland|wales|northern ireland)\b", raw):
        return "UK"
    if re.search(r"\b(?:canada|canadian|ontario|toronto|vancouver|calgary|edmonton|montreal)\b", raw):
        return "CA"
    if re.search(r"\b(?:us|usa|united states|american|fanduel|nfl|nba|mlb|nhl|wnba|ncaa)\b", raw):
        return "US"

    # New regions can add recognition terms in the source-manager cell.
    for region, keywords in sorted(EPG_REGION_MATCH_KEYWORDS.items()):
        if region in {"US", "UK", "CA", "IN", "ALL", "DUMMY"}:
            continue
        if any(_region_keyword_matches(raw, keyword) for keyword in keywords):
            return region
    return "ALL"


def find_dummy(dummy_ids: dict[str, str], preferred: str, fallback: str = "Blank.Dummy.us") -> str:
    return dummy_ids.get(preferred.lower()) or dummy_ids.get(fallback.lower()) or preferred



def has_real_feed_for_region(region: str) -> bool:
    wanted = str(region or "").upper()
    return any(
        str(feed.get("region", "ALL")).upper() == wanted
        and str(feed.get("kind", "real")).lower() == "real"
        and bool(feed.get("use_for_matching", True))
        for feed in EPGSHARE_FEEDS.values()
    )


def automatic_dummy_rule(
    category_name: str,
    channel_name: str,
    dummy_ids: dict[str, str],
) -> dict[str, str] | None:
    """Return a safe dummy mapping before any panel/fuzzy matching."""
    category = str(category_name or "")
    channel = str(channel_name or "")
    context = f"{category} {channel}"

    if PPV_RE.search(context):
        return {
            "epg_id": find_dummy(dummy_ids, "PPV.EVENTS.Dummy.us"),
            "reason": "PPV name/category rule",
        }

    if ONEFOOTBALL_RE.search(context):
        return {
            "epg_id": find_dummy(dummy_ids, "PPV.EVENTS.Dummy.us"),
            "reason": "OneFootball numbered event-slot rule",
        }

    category_is_event = bool(EVENT_CATEGORY_RE.search(category)) and not bool(
        MAIN_EVENT_LINEAR_RE.search(category)
    )
    channel_mentions_event = bool(EVENT_CHANNEL_RE.search(channel))
    channel_is_numbered_event = bool(NUMBERED_EVENT_CHANNEL_RE.search(channel))
    linear_main_event = bool(MAIN_EVENT_LINEAR_RE.search(channel))
    if category_is_event or (
        (channel_mentions_event or channel_is_numbered_event) and not linear_main_event
    ):
        return {
            "epg_id": find_dummy(dummy_ids, "PPV.EVENTS.Dummy.us"),
            "reason": "Event category/numbered event-channel rule",
        }

    if TWENTY_FOUR_SEVEN_RE.search(context):
        if MOVIE_DUMMY_CONTEXT_RE.search(context):
            return {
                "epg_id": find_dummy(dummy_ids, "Movie.Dummy.us"),
                "reason": "24/7 movie/cinema rule",
            }
        return {
            "epg_id": find_dummy(dummy_ids, "24.7.Dummy.us"),
            "reason": "24/7 continuous-channel rule",
        }

    # Until a Portugal feed is added, do not fuzzy-match PT sports against
    # unrelated US/UK/India IDs. A sports dummy is safer than a wrong guide.
    if PT_SPORTS_CATEGORY_RE.search(category) and not has_real_feed_for_region("PT"):
        return {
            "epg_id": find_dummy(dummy_ids, "Sports.Dummy.us"),
            "reason": "PT sports has no configured Portugal EPGShare source",
        }

    return None


def _candidate_lookup(
    candidates: list[dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (str(item.get("feed", "")).upper(), str(item.get("epg_id", "")).casefold()): item
        for item in candidates
    }


def preferred_exact_match(
    channel_name: str,
    category_name: str,
    region: str,
    candidates: list[dict[str, str]],
) -> dict[str, Any] | None:
    context = f"{category_name} {channel_name}"
    lookup = _candidate_lookup(candidates)
    for rule in PREFERRED_EPG_RULES:
        if str(region).upper() not in set(rule["regions"]):
            continue
        if not rule["pattern"].search(context):
            continue
        available = [
            lookup[(str(rule["feed"]).upper(), epg_id.casefold())]
            for epg_id in rule["epg_ids"]
            if (str(rule["feed"]).upper(), epg_id.casefold()) in lookup
        ]
        if not available:
            return {
                "action": "UNMATCHED",
                "source": "",
                "epg_id": "",
                "epg_feed": "",
                "reason": f"Preferred rule matched {rule['label']}, but its EPG IDs were not in the downloaded catalog",
            }
        primary = available[0]
        alternate = available[1] if len(available) > 1 else None
        return {
            "action": "AUTO_EPGSHARE",
            "source": "epgshare",
            "epg_id": primary["epg_id"],
            "epg_feed": primary["feed"],
            "best_score": 100.0,
            "second_epg_id": alternate["epg_id"] if alternate else "",
            "second_epg_feed": alternate["feed"] if alternate else "",
            "second_score": 100.0 if alternate else "",
            "score_margin": 0.0 if alternate else 100.0,
            "reason": f"Built-in exact alias rule: {rule['label']}",
        }
    return None


def normalize_fanduel_identity(value: str) -> str:
    text = normalize_name(value)
    text = re.sub(r"\bfan\s+duel\b", "fanduel", text)
    text = re.sub(r"\b(?:us|usa)\b", " ", text)
    text = re.sub(r"\bbally\s+sports\b", "fanduel sports", text)
    text = re.sub(r"\bfanduel\s+sports\s+network\b", "fanduel sports", text)
    text = re.sub(r"\bfanduel\s+sports\b", "fanduel sports", text)
    text = re.sub(r"\bout\s+of\s+market\b", " ", text)
    text = re.sub(r"\bfeed\s+\d+\b", " ", text)
    text = re.sub(r"\bplus\b", "extra", text)
    text = re.sub(r"\bsocal\b", "so cal", text)
    return re.sub(r"\s+", " ", text).strip()


def fanduel_query_variants(identity: str) -> set[str]:
    variants = {identity}
    variants.update(FANDUEL_QUERY_ALIASES.get(identity, []))
    return {re.sub(r"\s+", " ", item).strip() for item in variants if item.strip()}


def _fanduel_candidate_quality(item: dict[str, str]) -> tuple[int, int, str]:
    epg_id = str(item.get("epg_id", ""))
    score = 0
    if ".HD." in epg_id or epg_id.endswith(".HD.us"):
        score += 20
    if ".Extra." in epg_id:
        score += 5
    if ".Alt." in epg_id:
        score -= 5
    return (score, -len(epg_id), epg_id.casefold())


def fanduel_or_bally_match(
    channel_name: str,
    category_name: str,
    region: str,
    candidates: list[dict[str, str]],
) -> dict[str, Any] | None:
    context = f"{category_name} {channel_name}"
    if str(region).upper() != "US" or not FANDUEL_TRIGGER_RE.search(context):
        return None

    # Bally Sports YES is not a FanDuel regional-network alias; the preferred
    # US2 rule above handles it.
    if re.search(r"\bbally\s+sports\s+yes\b", context, re.IGNORECASE):
        return None

    pool = [item for item in candidates if str(item.get("feed", "")).upper() == "FANDUEL1"]
    if not pool:
        return {
            "action": "UNMATCHED",
            "source": "",
            "epg_id": "",
            "epg_feed": "",
            "reason": "Bally/FanDuel channel detected, but the FANDUEL1 catalog was unavailable",
        }

    query_identity = normalize_fanduel_identity(channel_name)
    query_variants = fanduel_query_variants(query_identity)
    prepared: list[tuple[dict[str, str], str]] = [
        (item, normalize_fanduel_identity(epg_id_to_name(item["epg_id"])))
        for item in pool
    ]

    exact = [item for item, identity in prepared if identity in query_variants]
    if exact:
        exact.sort(key=_fanduel_candidate_quality, reverse=True)
        primary = exact[0]
        alternate = exact[1] if len(exact) > 1 else None
        return {
            "action": "AUTO_EPGSHARE",
            "source": "epgshare",
            "epg_id": primary["epg_id"],
            "epg_feed": primary["feed"],
            "best_score": 100.0,
            "second_epg_id": alternate["epg_id"] if alternate else "",
            "second_epg_feed": alternate["feed"] if alternate else "",
            "second_score": 100.0 if alternate else "",
            "score_margin": 0.0 if alternate else 100.0,
            "reason": "Bally Sports/FanDuel exact regional-network alias",
        }

    # Ambiguous Plus/Extra/sub-region names stay in REVIEW, but suggestions are
    # restricted to FANDUEL1 instead of unrelated local/sports feeds. Require
    # the regional/location words to overlap so a generic FanDuel network ID
    # cannot outrank Ohio, Southwest, Tennessee, and similar regional IDs.
    brand_words = {
        "fanduel", "sports", "network", "extra", "plus", "out", "of",
        "market", "feed", "zone", "hd", "us",
    }
    query_location = set(query_identity.split()) - brand_words
    plausible_prepared = [
        (item, identity)
        for item, identity in prepared
        if not query_location
        or bool(query_location & (set(identity.split()) - brand_words))
    ]
    if not plausible_prepared:
        plausible_prepared = prepared
    choices = [identity for _item, identity in plausible_prepared]
    raw = process.extract(query_identity, choices, scorer=fuzz.WRatio, limit=2)
    matches = [
        (plausible_prepared[index][0], float(score))
        for _choice, score, index in raw
    ]
    best = matches[0] if matches else None
    second = matches[1] if len(matches) > 1 else None
    if best and best[1] >= 72.0:
        return {
            "action": "REVIEW",
            "source": "epgshare_candidate",
            "epg_id": best[0]["epg_id"],
            "epg_feed": best[0]["feed"],
            "best_score": round(best[1], 1),
            "second_epg_id": second[0]["epg_id"] if second else "",
            "second_epg_feed": second[0]["feed"] if second else "",
            "second_score": round(second[1], 1) if second else "",
            "score_margin": round(best[1] - (second[1] if second else 0.0), 1),
            "reason": "Bally/FanDuel alias found; regional Plus/Extra variant needs review",
        }

    return {
        "action": "UNMATCHED",
        "source": "",
        "epg_id": "",
        "epg_feed": "",
        "reason": "Bally/FanDuel channel found, but no safe FANDUEL1 regional match was available",
    }


def match_candidate_is_plausible(query: str, candidate: dict[str, str]) -> bool:
    candidate_name = str(candidate.get("normalized", ""))
    if not query or not candidate_name:
        return False
    query_tokens = set(query.split())
    candidate_tokens = set(candidate_name.split())

    query_numbers = {token for token in query_tokens if token.isdigit()}
    candidate_numbers = {token for token in candidate_tokens if token.isdigit()}
    if query_numbers and candidate_numbers and query_numbers.isdisjoint(candidate_numbers):
        return False

    # Protect East/West identities.
    if ("east" in query_tokens and "west" in candidate_tokens) or (
        "west" in query_tokens and "east" in candidate_tokens
    ):
        return False

    query_core = query_tokens - MATCH_GENERIC_WORDS
    candidate_core = candidate_tokens - MATCH_GENERIC_WORDS
    if not query_core or not candidate_core:
        return query == candidate_name
    return bool(query_core & candidate_core)


def safe_top_two_matches(
    query: str,
    candidates: list[dict[str, str]],
) -> list[tuple[dict[str, str], float]]:
    plausible = [item for item in candidates if match_candidate_is_plausible(query, item)]
    return top_two_matches(query, plausible)


def download_epgshare_catalog(
    session: requests.Session,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    real_candidates: list[dict[str, str]] = []
    dummy_ids: dict[str, str] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for feed_name, feed in EPGSHARE_FEEDS.items():
        if not bool(feed.get("use_for_matching", True)):
            print(f"{feed_name}: available for builds, skipped during matching")
            continue
        if not str(feed.get("txt_url", "")).strip():
            print(f"Warning: {feed_name} has no text catalog; skipped during matching.")
            continue
        try:
            response = session.get(feed["txt_url"], timeout=(20, 180))
            if response.status_code != 200:
                print(f"Warning: {feed_name} returned HTTP {response.status_code}; continuing.")
                continue
            ids = parse_epgshare_ids(response.text)
        except requests.RequestException:
            print(f"Warning: could not download {feed_name}; continuing.")
            continue

        print(f"{feed_name}: {len(ids):,} EPG IDs")
        if feed["kind"] == "dummy":
            for epg_id in ids:
                dummy_ids[epg_id.lower()] = epg_id
            continue

        for epg_id in ids:
            pair = (feed_name, epg_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            display_name = epg_id_to_name(epg_id)
            real_candidates.append({
                "epg_id": epg_id,
                "feed": feed_name,
                "region": feed["region"],
                "display_name": display_name,
                "normalized": normalize_name(display_name),
            })

    if not real_candidates:
        raise RuntimeError("No real EPGShare IDs could be downloaded.")
    return real_candidates, dummy_ids




def top_two_matches(
    query: str,
    candidates: list[dict[str, str]],
) -> list[tuple[dict[str, str], float]]:
    if not query or not candidates:
        return []
    choices = [candidate["normalized"] for candidate in candidates]
    raw_matches = process.extract(query, choices, scorer=fuzz.WRatio, limit=2)
    return [(candidates[index], float(score)) for _choice, score, index in raw_matches]


def _legacy_make_matching_report_v2(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    by_region: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in real_candidates:
        by_region[str(candidate.get("region", "ALL")).upper()].append(candidate)
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
        normalized = normalize_name(channel_name)
        region = detect_region(channel_name, category_name)
        raw_context = f"{category_name} {channel_name}"

        if panel_epg_id:
            panel_info = panel_quality.get(
                panel_epg_id.casefold(),
                {
                    "status": "NOT_CHECKED",
                    "current_or_future_informative_rows": 0,
                    "latest_stop_utc": "",
                    "reason": "Panel EPG ID was not checked",
                },
            )
        else:
            panel_info = {
                "status": "NO_PANEL_ID",
                "current_or_future_informative_rows": 0,
                "latest_stop_utc": "",
                "reason": "Server channel has no panel EPG ID",
            }

        record: dict[str, Any] = {
            "server_id": server_id,
            "stream_id": stream_id,
            "category_id": category_id,
            "category_name": category_name,
            "channel_number": channel_number,
            "channel_name": channel_name,
            "normalized_name": normalized,
            "detected_region": region,
            "panel_epg_id": panel_epg_id,
            "panel_epg_status": str(panel_info.get("status", "")),
            "panel_usable_programmes": int(
                panel_info.get("current_or_future_informative_rows", 0) or 0
            ),
            "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
            "action": "",
            "source": "",
            "epg_id": "",
            "epg_feed": "",
            "best_score": "",
            "second_epg_id": "",
            "second_epg_feed": "",
            "second_score": "",
            "score_margin": "",
            "reason": "",
        }

        dummy_rule = automatic_dummy_rule(category_name, channel_name, dummy_ids)
        if dummy_rule:
            record.update({
                "action": "AUTO_DUMMY",
                "source": "dummy",
                "epg_id": dummy_rule["epg_id"],
                "epg_feed": "DUMMY_CHANNELS",
                "reason": dummy_rule["reason"],
            })
            records.append(record)
            continue

        preferred = preferred_exact_match(
            channel_name, category_name, region, real_candidates
        )
        if preferred:
            record.update(preferred)
            records.append(record)
            continue

        fanduel = fanduel_or_bally_match(
            channel_name, category_name, region, real_candidates
        )
        if fanduel:
            record.update(fanduel)
            records.append(record)
            continue

        # A server/panel EPG ID is used only when it has real current/future
        # programme titles. Empty, placeholder, and stale panel data is ignored.
        if panel_epg_id and panel_info.get("status") == "USABLE":
            record.update({
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_id": panel_epg_id,
                "epg_feed": "server xmltv.php",
                "reason": "Panel XMLTV contains useful current/future programme information",
            })
            records.append(record)
            continue

        pool = by_region.get(region, real_candidates) if region != "ALL" else real_candidates
        if not pool:
            pool = real_candidates
        matches = safe_top_two_matches(normalized, pool)
        best = matches[0] if matches else None
        second = matches[1] if len(matches) > 1 else None
        best_score = best[1] if best else 0.0
        second_score = second[1] if second else 0.0
        margin = best_score - second_score

        if best:
            record.update({
                "epg_id": best[0]["epg_id"],
                "epg_feed": best[0]["feed"],
                "best_score": round(best_score, 1),
                "second_epg_id": second[0]["epg_id"] if second else "",
                "second_epg_feed": second[0]["feed"] if second else "",
                "second_score": round(second_score, 1) if second else "",
                "score_margin": round(margin, 1),
            })

        exact_matches = []
        if normalized:
            exact_matches = [candidate for candidate in pool if candidate["normalized"] == normalized]
        unique_exact = len({(item["feed"], item["epg_id"]) for item in exact_matches}) == 1

        if best and (
            (unique_exact and exact_matches)
            or (best_score >= AUTO_MATCH_SCORE and margin >= AUTO_MATCH_MIN_MARGIN)
        ):
            record.update({
                "action": "AUTO_EPGSHARE",
                "source": "epgshare",
                "reason": (
                    "Unique exact normalized name"
                    if unique_exact and exact_matches
                    else "High-confidence unique EPGShare match"
                ),
            })
        elif best and best_score >= REVIEW_MATCH_SCORE:
            record.update({
                "action": "REVIEW",
                "source": "epgshare_candidate",
                "reason": "Possible EPGShare match requires review",
            })
        else:
            record.update({
                "action": "UNMATCHED",
                "source": "",
                "epg_id": "",
                "epg_feed": "",
                "reason": "No sufficiently safe match",
            })

        records.append(record)

    report = pd.DataFrame.from_records(records)
    if report.empty:
        raise RuntimeError("No usable live channels were found.")
    return report


MAPPING_COLUMNS = [
    "server_id", "stream_id", "category_id", "category_name", "channel_name",
    "action", "source", "epg_id", "epg_feed", "reason",
]


def safe_mapping_from_report(report: pd.DataFrame) -> pd.DataFrame:
    safe = report[
        report["action"].isin(SAFE_MAPPING_ACTIONS)
        & report["epg_id"].fillna("").astype(str).str.strip().ne("")
        & report["epg_feed"].fillna("").astype(str).str.strip().ne("")
    ].copy()
    for column in MAPPING_COLUMNS:
        if column not in safe.columns:
            safe[column] = ""
    return safe[MAPPING_COLUMNS].drop_duplicates(["server_id", "stream_id"], keep="last")


def category_match_coverage(report: pd.DataFrame) -> pd.DataFrame:
    work = report.copy()
    work["is_mapped"] = work["action"].isin(SAFE_MAPPING_ACTIONS)
    work["is_review"] = work["action"].eq("REVIEW")
    work["is_unmatched"] = work["action"].eq("UNMATCHED")
    grouped = (
        work.groupby(["category_id", "category_name"], dropna=False, sort=True)
        .agg(
            channels=("stream_id", "count"),
            mapped=("is_mapped", "sum"),
            review=("is_review", "sum"),
            unmatched=("is_unmatched", "sum"),
        )
        .reset_index()
    )
    grouped["mapped_percent"] = (grouped["mapped"] * 100.0 / grouped["channels"]).round(2)
    return grouped.sort_values(["mapped_percent", "channels", "category_name"], ascending=[False, False, True])


def write_inventory_bundle(
    server_id: str,
    categories: list[dict[str, Any]],
    report: pd.DataFrame,
    root: Path,
    *,
    panel_quality_report: pd.DataFrame | None = None,
) -> tuple[Path, dict[str, Any]]:
    if root.exists():
        shutil.rmtree(root)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "epg_sources.json").write_text(
        epg_source_config_json(), encoding="utf-8"
    )

    report.to_csv(
        output_dir / "all_channels_and_matches.csv", index=False, encoding="utf-8-sig"
    )
    report[[
        "server_id", "stream_id", "category_id", "category_name", "channel_number",
        "channel_name", "panel_epg_id", "panel_epg_status",
    ]].to_csv(
        output_dir / "channel_names_only.csv", index=False, encoding="utf-8-sig"
    )

    category_rows = []
    for item in categories:
        category_rows.append({
            "server_id": server_id,
            "category_id": str(item.get("category_id") or item.get("id") or "").strip(),
            "category_name": str(item.get("category_name") or item.get("name") or "").strip(),
            "parent_id": str(item.get("parent_id") or "").strip(),
        })
    pd.DataFrame(category_rows).to_csv(
        output_dir / "categories.csv", index=False, encoding="utf-8-sig"
    )

    safe_mapping = safe_mapping_from_report(report)
    safe_mapping.to_csv(
        output_dir / "mapping_ready_for_test.csv", index=False, encoding="utf-8-sig"
    )
    report[report["action"].isin(SAFE_MAPPING_ACTIONS)].to_csv(
        output_dir / "automatic_matches.csv", index=False, encoding="utf-8-sig"
    )
    report[report["action"].eq("REVIEW")].to_csv(
        output_dir / "review_doubtful_matches.csv", index=False, encoding="utf-8-sig"
    )
    report[report["action"].eq("UNMATCHED")].to_csv(
        output_dir / "unmatched.csv", index=False, encoding="utf-8-sig"
    )

    coverage = category_match_coverage(report)
    coverage.to_csv(
        output_dir / "category_match_coverage.csv", index=False, encoding="utf-8-sig"
    )

    if panel_quality_report is not None:
        panel_quality_report.to_csv(
            output_dir / "panel_epg_quality.csv", index=False, encoding="utf-8-sig"
        )

    counts = report["action"].value_counts(dropna=False).to_dict()
    panel_usable_ids = 0
    if panel_quality_report is not None and not panel_quality_report.empty:
        panel_usable_ids = int(panel_quality_report["status"].eq("USABLE").sum())
    summary = {
        "server_id": server_id,
        "categories": int(len(category_rows)),
        "channels": int(len(report)),
        "safe_mapped_channels": int(len(safe_mapping)),
        "safe_mapping_percent": round(len(safe_mapping) * 100.0 / len(report), 2),
        "panel_epg_ids_with_useful_future_information": panel_usable_ids,
        "actions": {str(key): int(value) for key, value in counts.items()},
        "active_epgshare_sources": int(len(EPGSHARE_FEEDS)),
        "trusted_mapping_used": False,
        "matching_engine": (
            str(report["smart_rules_version"].iloc[0])
            if "smart_rules_version" in report.columns and not report.empty
            else "unknown"
        ),
    }
    (output_dir / "inventory_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    readme = f"""SKY TV simple EPG inventory and fresh matching report

Server: {server_id}
Channels: {summary['channels']}
Safe mapped channels: {summary['safe_mapped_channels']}
Safe mapping percent: {summary['safe_mapping_percent']}%
Old/trusted mapping used: NO
Matching engine: {summary['matching_engine']}

Important files
---------------
all_channels_and_matches.csv
  The complete fresh result. Edit this file in Google Sheets when reviewing.

mapping_ready_for_test.csv
  Only working panel IDs, high-confidence EPGShare matches, and dummy rules.

review_doubtful_matches.csv
  Suggestions that need your decision.

unmatched.csv
  Channels with no safe suggestion.

panel_epg_quality.csv
  Shows why each server EPG ID was accepted or ignored. A panel ID is accepted
  only when it has useful current/future programme titles.

epg_sources.json
  A reusable copy of all active EPGShare sources.

No server URL, username, or password is included.
"""
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")

    zip_path = root / f"{server_id}_fresh_inventory_and_match.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
    return zip_path, summary


def download_in_colab(path: Path) -> None:
    try:
        from google.colab import files
    except ImportError:
        print(f"Created: {path.resolve()}")
        return
    files.download(str(path))


@dataclass
class ParseStats:
    feed: str
    mapped_epg_ids: int = 0
    epg_ids_with_programmes: int = 0
    programme_count: int = 0
    future_programme_count: int = 0
    invalid_time_rows: int = 0
    placeholder_or_blank_title_rows: int = 0
    downloaded_bytes: int = 0
    earliest_included_start: int | None = None
    latest_included_stop: int | None = None
    latest_future_stop: int | None = None
    future_stop_by_epg_id: dict[str, int] = field(default_factory=dict)










def parse_xmltv(
    path: Path,
    wanted_ids: set[str],
    feed: str,
    window_start: int,
    now_epoch: int,
) -> tuple[dict[str, list[list[object]]], ParseStats]:
    """Keep the previous three days and every future row available in the source."""
    programmes: dict[str, list[list[object]]] = defaultdict(list)
    stats = ParseStats(
        feed=feed,
        mapped_epg_ids=len(wanted_ids),
        downloaded_bytes=path.stat().st_size,
    )
    seen: dict[str, set[tuple[int, int, str]]] = defaultdict(set)
    wanted_lookup = {str(value).casefold(): str(value) for value in wanted_ids}

    with open_maybe_gzip(path) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            name = local_name(element.tag)
            if name == "programme":
                source_channel_id = (element.get("channel") or "").strip()
                channel_id = wanted_lookup.get(source_channel_id.casefold())
                if channel_id is not None:
                    start = parse_xmltv_time(element.get("start"))
                    stop = parse_xmltv_time(element.get("stop"))
                    title = child_text(element, "title")
                    if not is_informative_programme_title(title):
                        stats.placeholder_or_blank_title_rows += 1
                    elif start is None:
                        stats.invalid_time_rows += 1
                    else:
                        if stop is None or stop <= start:
                            stop = start + 3600
                        # No upper limit: all future rows present in the source are retained.
                        if stop >= window_start:
                            key = (start, stop, title)
                            if key not in seen[channel_id]:
                                seen[channel_id].add(key)
                                programmes[channel_id].append([start, stop, title])
                                stats.programme_count += 1
                                if stop > now_epoch:
                                    stats.future_programme_count += 1
                                    previous = stats.future_stop_by_epg_id.get(channel_id)
                                    if previous is None or stop > previous:
                                        stats.future_stop_by_epg_id[channel_id] = stop
                                    if (
                                        stats.latest_future_stop is None
                                        or stop > stats.latest_future_stop
                                    ):
                                        stats.latest_future_stop = stop
                                if (
                                    stats.earliest_included_start is None
                                    or start < stats.earliest_included_start
                                ):
                                    stats.earliest_included_start = start
                                if (
                                    stats.latest_included_stop is None
                                    or stop > stats.latest_included_stop
                                ):
                                    stats.latest_included_stop = stop
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
            elif name == "channel":
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
        del context

    for channel_id, items in programmes.items():
        items.sort(key=lambda item: (int(item[0]), int(item[1]), str(item[2])))

    stats.epg_ids_with_programmes = len(programmes)
    return dict(programmes), stats


def mapping_feed_key(row: pd.Series) -> str:
    source = str(row.get("source", "")).strip().lower()
    feed = str(row.get("epg_feed", "")).strip()
    if source == "panel" or feed.lower() == "server xmltv.php":
        return "PANEL"
    canonical = feed.upper()
    return canonical if canonical in EPGSHARE_FEEDS else feed


def normalize_mapping_frame(frame: pd.DataFrame, expected_server_id: str) -> pd.DataFrame:
    aliases = {
        "selected_epg_id": "epg_id",
        "chosen_epg_id": "epg_id",
        "selected_feed": "epg_feed",
        "chosen_feed": "epg_feed",
    }
    out = frame.copy()
    for old, new in aliases.items():
        if new not in out.columns and old in out.columns:
            out[new] = out[old]

    if "server_id" not in out.columns:
        out["server_id"] = expected_server_id
    out["server_id"] = (
        out["server_id"].fillna("").astype(str).str.strip().replace("", expected_server_id)
    )
    out = out[out["server_id"].eq(expected_server_id)].copy()

    for column in [
        "stream_id", "category_id", "category_name", "channel_name", "action",
        "source", "epg_id", "epg_feed", "reason",
    ]:
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].fillna("").astype(str).str.strip()

    rejected = {"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED", "SKIP"}
    out = out[~out["action"].str.upper().isin(rejected)].copy()
    out = out[
        out["stream_id"].ne("")
        & out["channel_name"].ne("")
        & out["epg_id"].ne("")
        & out["epg_feed"].ne("")
    ].copy()

    blank_source = out["source"].eq("")
    panel_rows = out["epg_feed"].str.lower().eq("server xmltv.php")
    dummy_rows = out["epg_feed"].eq("DUMMY_CHANNELS")
    out.loc[blank_source & panel_rows, "source"] = "panel"
    out.loc[blank_source & dummy_rows, "source"] = "dummy"
    out.loc[blank_source & ~panel_rows & ~dummy_rows, "source"] = "epgshare"

    out = out.drop_duplicates(["server_id", "stream_id"], keep="last")
    return out[MAPPING_COLUMNS]


def prepare_mapping(mapping: pd.DataFrame, server_id: str) -> pd.DataFrame:
    out = normalize_mapping_frame(mapping, server_id)
    if out.empty:
        raise ValueError(f"No usable mapping rows were found for {server_id}.")
    out["feed_key"] = out.apply(mapping_feed_key, axis=1)
    unknown = sorted(set(out["feed_key"]) - ({"PANEL"} | set(EPGSHARE_FEEDS)))
    if unknown:
        raise ValueError(
            f"Unknown EPG feed names in mapping: {', '.join(unknown)}. "
            "Add them in the EPGShare source-manager cell or load the matching "
            "epg_sources.json file before Stage B."
        )
    if out.duplicated(["server_id", "stream_id"]).any():
        raise ValueError("Duplicate stream IDs remain after mapping normalization.")
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mapping_sha256(mapping: pd.DataFrame) -> str:
    stable = mapping[["server_id", "stream_id", "feed_key", "epg_id", "channel_name"]].sort_values(
        ["server_id", "stream_id", "channel_name"]
    ).to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(stable).hexdigest()


_XML_INVALID_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)


def clean_xml_text(value: object) -> str:
    return _XML_INVALID_RE.sub("", str(value or ""))


def xml_text(value: object) -> str:
    from xml.sax.saxutils import escape
    return escape(clean_xml_text(value), {"\r": "&#13;"})


def xml_attribute(value: object) -> str:
    from xml.sax.saxutils import quoteattr
    return quoteattr(clean_xml_text(value))


def is_dummy_feed(feed: str) -> bool:
    return feed != "PANEL" and EPGSHARE_FEEDS.get(feed, {}).get("kind") == "dummy"


def source_priority(feed: str, *, canonical: bool) -> int:
    if canonical:
        return 400
    if feed == "PANEL":
        return 300
    if is_dummy_feed(feed):
        return 100
    return 250


def validate_canonical_epg_ids(prepared: pd.DataFrame) -> pd.DataFrame:
    """Return repeated real EPG IDs instead of stopping the build.

    A server panel and an EPGShare feed can legitimately expose the same XMLTV
    channel ID. The old builder treated that as a fatal mapping error. Builder
    v7.1 downloads both schedules, compares their useful future coverage, and
    chooses one deterministic schedule for the final XMLTV channel ID.
    """
    real = prepared[~prepared["feed_key"].map(is_dummy_feed)].copy()
    grouped = (
        real.groupby("epg_id", sort=True)["feed_key"]
        .agg(lambda values: sorted(set(str(value) for value in values)))
    )
    rows = [
        {
            "epg_id": str(epg_id),
            "candidate_feeds": " | ".join(feeds),
            "candidate_feed_count": len(feeds),
        }
        for epg_id, feeds in grouped.items()
        if len(feeds) > 1
    ]
    conflicts = pd.DataFrame(
        rows,
        columns=["epg_id", "candidate_feeds", "candidate_feed_count"],
    )
    if not conflicts.empty:
        examples = ", ".join(conflicts["epg_id"].astype(str).tolist()[:10])
        print(
            f"Found {len(conflicts):,} XMLTV IDs repeated across real feeds. "
            "Builder v7.1 will choose the schedule with the best useful future "
            f"coverage automatically. Examples: {examples}"
        )
    return conflicts


def _schedule_metrics(
    items: list[list[object]],
    now_epoch: int,
) -> dict[str, int]:
    valid: list[tuple[int, int]] = []
    for item in items:
        if len(item) < 2:
            continue
        try:
            start = int(item[0])
            stop = int(item[1])
        except (TypeError, ValueError):
            continue
        if stop <= start:
            continue
        valid.append((start, stop))

    future = [(start, stop) for start, stop in valid if stop > int(now_epoch)]
    return {
        "programme_rows": len(valid),
        "future_programme_rows": len(future),
        "latest_stop_epoch": max((stop for _, stop in valid), default=0),
        "latest_future_stop_epoch": max((stop for _, stop in future), default=0),
        "earliest_future_start_epoch": min((start for start, _ in future), default=0),
    }


def resolve_canonical_epg_schedules(
    prepared: pd.DataFrame,
    schedules: dict[tuple[str, str], list[list[object]]],
    now_epoch: int,
) -> tuple[dict[str, tuple[str, str]], list[dict[str, Any]]]:
    """Choose one best schedule whenever a real XMLTV ID occurs in many feeds."""
    real = prepared[~prepared["feed_key"].map(is_dummy_feed)].copy()
    feed_order = {"PANEL": 0}
    for index, feed in enumerate(EPGSHARE_FEEDS, start=1):
        feed_order.setdefault(str(feed), index)

    choices: dict[str, tuple[str, str]] = {}
    resolution_rows: list[dict[str, Any]] = []

    for epg_id, group in real.groupby("epg_id", sort=True):
        epg_id = str(epg_id)
        mapped_counts = (
            group.groupby("feed_key", sort=True).size().astype(int).to_dict()
        )
        candidate_feeds = sorted(set(group["feed_key"].astype(str)))
        candidate_rows: list[dict[str, Any]] = []

        for feed in candidate_feeds:
            key = (str(feed), epg_id)
            metrics = _schedule_metrics(schedules.get(key, []), now_epoch)
            rank = int(feed_order.get(str(feed), 100000))
            # Order of importance:
            # 1) a useful future schedule exists;
            # 2) it extends furthest into the future;
            # 3) it contains more future rows;
            # 4) it has more total useful rows;
            # 5) more mapped streams already selected this feed;
            # 6) stable configured source order (PANEL first only as final tie-break).
            quality = (
                1 if metrics["future_programme_rows"] > 0 else 0,
                int(metrics["latest_future_stop_epoch"]),
                int(metrics["future_programme_rows"]),
                int(metrics["programme_rows"]),
                int(mapped_counts.get(feed, 0)),
                int(metrics["latest_stop_epoch"]),
                -rank,
            )
            candidate_rows.append({
                "feed": str(feed),
                "key": key,
                "quality": quality,
                "mapped_streams": int(mapped_counts.get(feed, 0)),
                **metrics,
            })

        available = [
            row for row in candidate_rows if row["programme_rows"] > 0
        ]
        if not available:
            continue

        chosen = max(available, key=lambda row: row["quality"])
        choices[epg_id] = chosen["key"]

        if len(candidate_feeds) > 1:
            detail_parts = []
            for row in candidate_rows:
                latest = epoch_iso(row["latest_future_stop_epoch"]) or "no future"
                detail_parts.append(
                    f"{row['feed']}: {row['programme_rows']} rows, "
                    f"{row['future_programme_rows']} future, until {latest}"
                )
            resolution_rows.append({
                "epg_id": epg_id,
                "candidate_feeds": " | ".join(candidate_feeds),
                "feeds_with_programmes": " | ".join(
                    row["feed"] for row in available
                ),
                "chosen_feed": chosen["feed"],
                "chosen_programme_rows": int(chosen["programme_rows"]),
                "chosen_future_programme_rows": int(chosen["future_programme_rows"]),
                "chosen_latest_future_stop_utc": epoch_iso(
                    int(chosen["latest_future_stop_epoch"])
                ),
                "chosen_mapped_streams": int(chosen["mapped_streams"]),
                "candidate_details": " || ".join(detail_parts),
                "resolution": (
                    "automatic_best_future_coverage_then_programme_rows_"
                    "then_mapping_count_then_source_order"
                ),
            })

    return choices, resolution_rows


def effective_schedule_key_for_row(
    row: pd.Series | Mapping[str, Any],
    canonical_schedule_choices: Mapping[str, tuple[str, str]],
) -> tuple[str, str]:
    feed = str(row.get("feed_key", ""))
    epg_id = str(row.get("epg_id", ""))
    if not is_dummy_feed(feed):
        return tuple(canonical_schedule_choices.get(epg_id, (feed, epg_id)))
    return (feed, epg_id)


def make_xmltv_entries(
    prepared: pd.DataFrame,
    schedules: dict[tuple[str, str], list[list[object]]],
    canonical_schedule_choices: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[str]]:
    entries: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    mapped_streams_with_programmes: set[str] = set()
    canonical_schedule_choices = dict(canonical_schedule_choices or {})

    def register(
        channel_id: str,
        display_names: Iterable[str],
        schedule_key: tuple[str, str],
        *,
        entry_type: str,
        priority: int,
        stream_id: str = "",
    ) -> None:
        channel_id = clean_xml_text(channel_id).strip()
        names = [clean_xml_text(name).strip() for name in display_names]
        names = [name for name in names if name]
        if not channel_id or not names or schedule_key not in schedules:
            return

        current = entries.get(channel_id)
        if current is None:
            entries[channel_id] = {
                "display_names": list(dict.fromkeys(names)),
                "schedule_key": schedule_key,
                "entry_type": entry_type,
                "priority": priority,
            }
            return

        current["display_names"] = list(
            dict.fromkeys([*current["display_names"], *names])
        )
        if current["schedule_key"] == schedule_key:
            return

        conflict = {
            "channel_id": channel_id,
            "kept_feed": current["schedule_key"][0],
            "kept_epg_id": current["schedule_key"][1],
            "candidate_feed": schedule_key[0],
            "candidate_epg_id": schedule_key[1],
            "candidate_stream_id": stream_id,
            "resolution": "kept_existing",
        }
        if priority > int(current["priority"]):
            conflict["resolution"] = "used_candidate"
            current["schedule_key"] = schedule_key
            current["entry_type"] = entry_type
            current["priority"] = priority
        conflicts.append(conflict)

    found_rows = prepared.copy()
    found_rows["effective_schedule_key"] = found_rows.apply(
        lambda row: effective_schedule_key_for_row(
            row, canonical_schedule_choices
        ),
        axis=1,
    )
    found_rows = found_rows[
        found_rows["effective_schedule_key"].map(lambda key: key in schedules)
    ].copy()

    # Every mapped server channel gets a name-based XMLTV entry. For repeated
    # canonical IDs, all visible aliases use the same automatically selected
    # schedule so TiviMate receives one consistent guide.
    for row in found_rows.itertuples(index=False):
        schedule_key = tuple(row.effective_schedule_key)
        mapped_streams_with_programmes.add(str(row.stream_id))
        register(
            str(row.channel_name),
            [str(row.channel_name)],
            schedule_key,
            entry_type="channel_name",
            priority=source_priority(str(schedule_key[0]), canonical=False),
            stream_id=str(row.stream_id),
        )

    # Real feeds retain their canonical XMLTV IDs. Group only by EPG ID because
    # duplicate feed copies have already been resolved to one best schedule.
    real_found = found_rows[~found_rows["feed_key"].map(is_dummy_feed)].copy()
    for epg_id, group in real_found.groupby("epg_id", sort=True):
        epg_id = str(epg_id)
        schedule_key = tuple(
            canonical_schedule_choices.get(
                epg_id, tuple(group.iloc[0]["effective_schedule_key"])
            )
        )
        aliases = list(dict.fromkeys(group["channel_name"].astype(str).tolist()))
        register(
            epg_id,
            aliases,
            schedule_key,
            entry_type="canonical_epg_id",
            priority=source_priority(str(schedule_key[0]), canonical=True),
        )

    return entries, conflicts, mapped_streams_with_programmes

def write_tivimate_xmltv(
    destination: Path,
    entries: dict[str, dict[str, Any]],
    schedules: dict[tuple[str, str], list[list[object]]],
) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    programme_rows = 0

    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                output.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                output.write('<tv generator-info-name="SKY TV EPG Builder">\n')

                for channel_id in sorted(entries, key=lambda value: value.casefold()):
                    entry = entries[channel_id]
                    output.write(f"  <channel id={xml_attribute(channel_id)}>\n")
                    for display_name in sorted(
                        set(entry["display_names"]), key=lambda value: value.casefold()
                    ):
                        output.write(
                            f"    <display-name>{xml_text(display_name)}</display-name>\n"
                        )
                    output.write("  </channel>\n")

                for channel_id in sorted(entries, key=lambda value: value.casefold()):
                    schedule_key = entries[channel_id]["schedule_key"]
                    items = schedules.get(schedule_key, [])
                    for start, stop, title in items:
                        output.write(
                            "  <programme "
                            f"start={xml_attribute(xmltv_timestamp(int(start)))} "
                            f"stop={xml_attribute(xmltv_timestamp(int(stop)))} "
                            f"channel={xml_attribute(channel_id)}>\n"
                        )
                        output.write(f"    <title>{xml_text(title)}</title>\n")
                        output.write("  </programme>\n")
                        programme_rows += 1

                output.write("</tv>\n")

    return {
        "xmltv_channels": len(entries),
        "programme_rows": programme_rows,
        "canonical_epg_channels": sum(
            1 for entry in entries.values() if entry["entry_type"] == "canonical_epg_id"
        ),
        "channel_name_entries": sum(
            1 for entry in entries.values() if entry["entry_type"] == "channel_name"
        ),
    }


def validate_tivimate_xmltv(path: Path) -> dict[str, Any]:
    channel_ids: set[str] = set()
    programme_channels: set[str] = set()
    duplicate_channel_ids: set[str] = set()
    programme_rows = 0
    root_name = ""
    generator_name = ""

    with gzip.open(path, "rb") as source:
        context = etree.iterparse(source, events=("start", "end"), recover=False, huge_tree=True)
        for event, element in context:
            name = local_name(element.tag)
            if event == "start" and not root_name:
                root_name = name
                generator_name = element.get("generator-info-name") or ""
            if event != "end":
                continue
            if name == "channel":
                channel_id = (element.get("id") or "").strip()
                if channel_id in channel_ids:
                    duplicate_channel_ids.add(channel_id)
                channel_ids.add(channel_id)
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
            elif name == "programme":
                programme_rows += 1
                programme_channels.add((element.get("channel") or "").strip())
                element.clear()
                while element.getprevious() is not None:
                    del element.getparent()[0]
        del context

    undeclared = programme_channels - channel_ids
    if root_name != "tv":
        raise RuntimeError("The generated file is not an XMLTV <tv> document.")
    if duplicate_channel_ids:
        raise RuntimeError("The generated XMLTV contains duplicate channel IDs.")
    if undeclared:
        raise RuntimeError("One or more programme rows refer to undeclared channel IDs.")
    if not channel_ids or not programme_rows:
        raise RuntimeError("The generated XMLTV does not contain usable channels and programmes.")

    return {
        "gzip_ok": True,
        "xml_ok": True,
        "generator_info_name": generator_name,
        "channels": len(channel_ids),
        "programme_rows": programme_rows,
        "programme_channel_ids": len(programme_channels),
        "undeclared_programme_channels": 0,
        "duplicate_channel_ids": 0,
    }


def make_refresh_plan(
    stats_rows: list[dict[str, Any]],
    used_epgshare_feeds: list[str],
    *,
    now_epoch: int,
    safety_hours: int,
) -> dict[str, Any]:
    by_feed = {str(row["feed"]): row for row in stats_rows}
    conservative_ends: dict[str, int] = {}
    latest_source_ends: dict[str, int] = {}
    mapped_ids_without_future: dict[str, int] = {}
    feeds_without_any_future: list[str] = []

    for feed in used_epgshare_feeds:
        row = by_feed.get(feed, {})
        latest = row.get("latest_future_stop_epoch")
        conservative = row.get("earliest_mapped_epg_id_future_stop_epoch")
        missing_count = int(row.get("mapped_epg_ids_without_future", 0) or 0)
        mapped_ids_without_future[feed] = missing_count
        if latest in {None, ""}:
            feeds_without_any_future.append(feed)
        else:
            latest_source_ends[feed] = int(latest)
        if conservative not in {None, ""}:
            conservative_ends[feed] = int(conservative)

    earliest_feed = ""
    earliest_end: int | None = None
    if conservative_ends:
        earliest_feed, earliest_end = min(
            conservative_ends.items(), key=lambda item: item[1]
        )

    feeds_with_missing_ids = sorted(
        feed for feed, count in mapped_ids_without_future.items() if count > 0
    )
    if feeds_without_any_future or feeds_with_missing_ids:
        recommended = now_epoch
        reason = (
            "Refresh now: at least one mapped EPGShare ID has no useful future "
            "programme information. Check the missing-future report after rebuilding."
        )
    elif earliest_end is not None:
        recommended = max(now_epoch, earliest_end - int(safety_hours) * 3600)
        reason = (
            "Refresh before the first mapped EPGShare schedule reaches the end of "
            "its available future guide."
        )
    else:
        recommended = None
        reason = "No real EPGShare feed was used, so no EPGShare refresh time was calculated."

    return {
        "policy": "KEEP_3_DAYS_PAST_AND_ALL_AVAILABLE_FUTURE",
        "pastDaysRetained": DEFAULT_PAST_DAYS,
        "futureLimit": None,
        "refreshSafetyHours": int(safety_hours),
        "epgShareFeedConservativeCoverageUntil": {
            feed: epoch_iso(value) for feed, value in sorted(conservative_ends.items())
        },
        "epgShareFeedLatestProgrammeStop": {
            feed: epoch_iso(value) for feed, value in sorted(latest_source_ends.items())
        },
        "mappedEpgIdsWithoutFutureByFeed": mapped_ids_without_future,
        "epgShareFeedsWithoutAnyFutureProgrammes": sorted(feeds_without_any_future),
        "earliestEndingEpgShareFeed": earliest_feed,
        "earliestMappedScheduleAvailableUntil": epoch_iso(earliest_end),
        "recommendedRefreshAt": epoch_iso(recommended),
        "recommendedRefreshAtEpoch": recommended,
        "refreshDueNow": bool(recommended is not None and recommended <= now_epoch),
        "reason": reason,
    }

def build_tivimate_package(
    mapping: pd.DataFrame,
    server_id: str,
    work_dir: Path,
    *,
    server_base_url: str = "",
    username: str = "",
    password: str = "",
    panel_xmltv_path: str | Path = "",
    channel_report: pd.DataFrame | None = None,
    past_days: int = DEFAULT_PAST_DAYS,
    refresh_safety_hours: int = REFRESH_SAFETY_HOURS,
) -> tuple[Path, dict[str, Any]]:
    prepared = prepare_mapping(mapping, server_id)
    canonical_conflict_table = validate_canonical_epg_ids(prepared)

    if work_dir.exists():
        shutil.rmtree(work_dir)
    sources_dir = work_dir / "sources"
    output_dir = work_dir / "output"
    sources_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    generated_at = int(now.timestamp())
    window_start = int((now - timedelta(days=int(past_days))).timestamp())

    wanted_by_feed = {
        str(feed): set(group["epg_id"].astype(str).tolist())
        for feed, group in prepared.groupby("feed_key", sort=True)
    }
    used_epgshare_feeds = sorted(feed for feed in wanted_by_feed if feed != "PANEL")
    source_config_text = epg_source_config_json(used_epgshare_feeds)
    source_config_file = output_dir / "epg_sources.json"
    source_config_file.write_text(source_config_text, encoding="utf-8")
    source_config_sha256 = hashlib.sha256(source_config_text.encode("utf-8")).hexdigest()

    print(f"Mapped streams: {len(prepared):,}")
    print(
        "Programme window: previous "
        f"{past_days} days plus ALL future rows available in each source file"
    )

    needs_panel = "PANEL" in wanted_by_feed
    cached_panel_source: Path | None = None
    if str(panel_xmltv_path or "").strip():
        candidate_panel_path = Path(str(panel_xmltv_path)).expanduser()
        if candidate_panel_path.is_file():
            cached_panel_source = candidate_panel_path

    if needs_panel:
        if cached_panel_source is not None:
            base_url = ""
            print("Using the panel XMLTV already downloaded in Step 3.")
        elif server_base_url and username and password:
            base_url = safe_base_url(server_base_url)
            print("Using the server details already held in this Colab runtime.")
        else:
            raise ValueError(
                "This mapping uses panel XMLTV, but the Step 3 runtime cache is not "
                "available. Enter the server details once after a runtime restart."
            )
    else:
        base_url = ""

    session = make_session()
    source_paths: dict[str, Path] = {}
    try:
        for feed in sorted(wanted_by_feed):
            if feed == "PANEL":
                destination = sources_dir / "PANEL.xmltv"
                if cached_panel_source is not None:
                    print("Copying cached panel XMLTV from Step 3...")
                    shutil.copy2(cached_panel_source, destination)
                else:
                    print("Downloading panel XMLTV feed...")
                    download_streamed(
                        session,
                        f"{base_url}/xmltv.php",
                        destination,
                        params={"username": username, "password": password},
                    )
            else:
                print(f"Downloading EPGShare {feed}...")
                destination = sources_dir / f"{feed}.xml.gz"
                download_streamed(session, EPGSHARE_FEEDS[feed]["xml_url"], destination)
            validate_xmlish(destination)
            source_paths[feed] = destination
    finally:
        session.close()

    schedules: dict[tuple[str, str], list[list[object]]] = {}
    stats_rows: list[dict[str, Any]] = []
    found_ids_by_feed: dict[str, set[str]] = {}
    future_ids_by_feed: dict[str, set[str]] = {}
    future_stop_by_feed_id: dict[tuple[str, str], int] = {}

    for feed in sorted(wanted_by_feed):
        print(f"Parsing {feed} for {len(wanted_by_feed[feed]):,} mapped EPG IDs...")
        programmes, stats = parse_xmltv(
            source_paths[feed],
            wanted_by_feed[feed],
            feed,
            window_start,
            generated_at,
        )
        found_ids_by_feed[feed] = set(programmes)
        future_ids_by_feed[feed] = set(stats.future_stop_by_epg_id)
        for epg_id, stop_epoch in stats.future_stop_by_epg_id.items():
            future_stop_by_feed_id[(feed, epg_id)] = int(stop_epoch)
        for epg_id, items in programmes.items():
            schedules[(feed, epg_id)] = items
        per_id_future_stops = list(stats.future_stop_by_epg_id.values())
        stats_rows.append({
            "feed": feed,
            "mapped_epg_ids": stats.mapped_epg_ids,
            "epg_ids_with_programmes": stats.epg_ids_with_programmes,
            "missing_epg_ids": stats.mapped_epg_ids - stats.epg_ids_with_programmes,
            "included_programme_rows": stats.programme_count,
            "future_programme_rows": stats.future_programme_count,
            "mapped_epg_ids_with_future": len(stats.future_stop_by_epg_id),
            "mapped_epg_ids_without_future": stats.mapped_epg_ids - len(stats.future_stop_by_epg_id),
            "future_coverage_percent": round(
                len(stats.future_stop_by_epg_id) * 100.0 / max(1, stats.mapped_epg_ids), 2
            ),
            "earliest_mapped_epg_id_future_stop_epoch": (
                min(per_id_future_stops) if per_id_future_stops else None
            ),
            "earliest_mapped_epg_id_future_stop_utc": epoch_iso(
                min(per_id_future_stops) if per_id_future_stops else None
            ),
            "placeholder_or_blank_title_rows_skipped": stats.placeholder_or_blank_title_rows,
            "invalid_time_rows": stats.invalid_time_rows,
            "downloaded_bytes": stats.downloaded_bytes,
            "earliest_included_start_epoch": stats.earliest_included_start,
            "earliest_included_start_utc": epoch_iso(stats.earliest_included_start),
            "latest_included_stop_epoch": stats.latest_included_stop,
            "latest_included_stop_utc": epoch_iso(stats.latest_included_stop),
            "latest_future_stop_epoch": stats.latest_future_stop,
            "latest_future_stop_utc": epoch_iso(stats.latest_future_stop),
        })
        print(
            f"  Useful programmes found for {stats.epg_ids_with_programmes:,}/"
            f"{stats.mapped_epg_ids:,} IDs; {stats.programme_count:,} rows kept."
        )

    canonical_schedule_choices, canonical_resolution_rows = (
        resolve_canonical_epg_schedules(prepared, schedules, generated_at)
    )
    if canonical_resolution_rows:
        print(
            f"Automatically resolved {len(canonical_resolution_rows):,} repeated "
            "real XMLTV IDs by comparing useful future schedule coverage."
        )

    prepared_effective = prepared.copy()
    prepared_effective["effective_schedule_key"] = prepared_effective.apply(
        lambda row: effective_schedule_key_for_row(
            row, canonical_schedule_choices
        ),
        axis=1,
    )
    prepared_effective["effective_feed_key"] = prepared_effective[
        "effective_schedule_key"
    ].map(lambda key: str(key[0]))
    prepared_effective["effective_epg_id"] = prepared_effective[
        "effective_schedule_key"
    ].map(lambda key: str(key[1]))
    prepared_effective["has_programmes"] = prepared_effective[
        "effective_schedule_key"
    ].map(lambda key: key in schedules)

    entries, id_conflicts, streams_with_programmes = make_xmltv_entries(
        prepared, schedules, canonical_schedule_choices
    )
    if not entries:
        raise RuntimeError(
            "No mapped EPG ID returned useful programme information in the selected window."
        )

    data_file = output_dir / f"{server_id}_tivimate.xml.gz"
    write_stats = write_tivimate_xmltv(data_file, entries, schedules)
    validation = validate_tivimate_xmltv(data_file)

    effective_epgshare_feeds = sorted({
        str(key[0])
        for key in prepared_effective.loc[
            prepared_effective["has_programmes"], "effective_schedule_key"
        ]
        if str(key[0]) != "PANEL" and not is_dummy_feed(str(key[0]))
    })
    refresh_epgshare_feeds = effective_epgshare_feeds
    refresh_plan = make_refresh_plan(
        stats_rows,
        refresh_epgshare_feeds,
        now_epoch=generated_at,
        safety_hours=refresh_safety_hours,
    )
    (output_dir / f"{server_id}_refresh_plan.json").write_text(
        json.dumps(refresh_plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / f"{server_id}_next_refresh_utc.txt").write_text(
        (refresh_plan.get("recommendedRefreshAt") or "NOT_CALCULATED") + "\n",
        encoding="utf-8",
    )

    data_sha = sha256_file(data_file)
    map_sha = mapping_sha256(prepared)
    catalog_streams = int(len(channel_report)) if channel_report is not None else len(prepared)
    final_schedule_keys = {
        (str(row.feed_key), str(row.epg_id))
        for row in prepared[["feed_key", "epg_id"]].drop_duplicates().itertuples(index=False)
    }
    unique_source_schedules = sum(
        1 for schedule_key in final_schedule_keys if schedule_key in schedules
    )

    manifest = {
        "format": "XMLTV",
        "targetApp": "TiviMate",
        "builderVersion": BUILDER_VERSION,
        "builderBuildId": BUILDER_BUILD_ID,
        "serverId": server_id,
        "generatedAt": generated_at,
        "generatedAtIso": now.isoformat().replace("+00:00", "Z"),
        "windowStart": window_start,
        "windowStartIso": epoch_iso(window_start),
        "pastDaysRetained": int(past_days),
        "futurePolicy": "ALL_AVAILABLE_FROM_SOURCE_FILES",
        "windowEnd": None,
        "dataFile": data_file.name,
        "dataSha256": data_sha,
        "mappingSha256": map_sha,
        "compressedBytes": data_file.stat().st_size,
        "catalogStreams": catalog_streams,
        "mappedStreams": len(prepared),
        "mappedStreamsWithProgrammes": len(streams_with_programmes),
        "mappingProgrammeCoveragePercent": round(
            len(streams_with_programmes) * 100.0 / len(prepared), 2
        ),
        "downloadedUniqueSourceSchedulesRequested": sum(
            len(values) for values in wanted_by_feed.values()
        ),
        "uniqueSourceSchedulesRequested": len(final_schedule_keys),
        "uniqueSourceSchedulesWithProgrammes": unique_source_schedules,
        "xmltvChannels": write_stats["xmltv_channels"],
        "channelNameEntries": write_stats["channel_name_entries"],
        "canonicalEpgIdEntries": write_stats["canonical_epg_channels"],
        "programmeRows": write_stats["programme_rows"],
        "channelIdConflicts": len(id_conflicts),
        "canonicalIdFeedConflictsDetected": int(len(canonical_conflict_table)),
        "canonicalIdFeedConflictsResolved": int(len(canonical_resolution_rows)),
        "epgShareFeeds": used_epgshare_feeds,
        "effectiveEpgShareFeeds": effective_epgshare_feeds,
        "refreshEpgShareFeeds": refresh_epgshare_feeds,
        "epgSourceConfigFile": source_config_file.name,
        "epgSourceConfigSha256": source_config_sha256,
        "refreshPlan": refresh_plan,
        "trustedMappingUsed": False,
        "validation": validation,
    }
    manifest_file = output_dir / f"{server_id}_tivimate_manifest.json"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    prepared_effective[
        MAPPING_COLUMNS + [
            "feed_key", "effective_feed_key", "effective_epg_id", "has_programmes"
        ]
    ].to_csv(
        output_dir / f"{server_id}_final_mapping.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(stats_rows).to_csv(
        output_dir / f"{server_id}_feed_coverage.csv", index=False, encoding="utf-8-sig"
    )

    missing_rows: list[dict[str, Any]] = []
    for (feed, epg_id), group in prepared.groupby(["feed_key", "epg_id"], sort=True):
        sample_row = group.iloc[0]
        effective_key = effective_schedule_key_for_row(
            sample_row, canonical_schedule_choices
        )
        if effective_key in schedules:
            continue
        names = list(dict.fromkeys(group["channel_name"].astype(str).tolist()))[:10]
        missing_rows.append({
            "feed": feed,
            "epg_id": epg_id,
            "mapped_streams": len(group),
            "example_channels": " | ".join(names),
            "reason": "No useful programme title was available from any selected copy of this ID",
        })
    pd.DataFrame(
        missing_rows,
        columns=["feed", "epg_id", "mapped_streams", "example_channels", "reason"],
    ).to_csv(
        output_dir / f"{server_id}_missing_epg_ids.csv", index=False, encoding="utf-8-sig"
    )


    missing_future_rows: list[dict[str, Any]] = []
    for (feed, epg_id), group in prepared.groupby(["feed_key", "epg_id"], sort=True):
        if is_dummy_feed(str(feed)):
            continue
        sample_row = group.iloc[0]
        effective_key = effective_schedule_key_for_row(
            sample_row, canonical_schedule_choices
        )
        effective_feed, effective_epg_id = effective_key
        if effective_epg_id in future_ids_by_feed.get(effective_feed, set()):
            continue
        names = list(dict.fromkeys(group["channel_name"].astype(str).tolist()))[:10]
        missing_future_rows.append({
            "feed": feed,
            "epg_id": epg_id,
            "mapped_streams": len(group),
            "example_channels": " | ".join(names),
            "has_recent_past_programmes": effective_key in schedules,
            "reason": "No useful future programme information was available from the selected copy of this ID",
        })
    pd.DataFrame(
        missing_future_rows,
        columns=[
            "feed", "epg_id", "mapped_streams", "example_channels",
            "has_recent_past_programmes", "reason",
        ],
    ).to_csv(
        output_dir / f"{server_id}_missing_future_epg_ids.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(
        canonical_resolution_rows,
        columns=[
            "epg_id", "candidate_feeds", "feeds_with_programmes", "chosen_feed",
            "chosen_programme_rows", "chosen_future_programme_rows",
            "chosen_latest_future_stop_utc", "chosen_mapped_streams",
            "candidate_details", "resolution",
        ],
    ).to_csv(
        output_dir / f"{server_id}_canonical_id_feed_resolutions.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(
        id_conflicts,
        columns=[
            "channel_id", "kept_feed", "kept_epg_id", "candidate_feed",
            "candidate_epg_id", "candidate_stream_id", "resolution",
        ],
    ).to_csv(
        output_dir / f"{server_id}_channel_id_conflicts.csv",
        index=False,
        encoding="utf-8-sig",
    )

    mapping_status = prepared_effective.copy()
    if channel_report is not None and {
        "stream_id", "category_id", "category_name", "channel_name"
    }.issubset(channel_report.columns):
        catalog = channel_report[
            ["stream_id", "category_id", "category_name", "channel_name"]
        ].copy()
        for column in catalog.columns:
            catalog[column] = catalog[column].fillna("").astype(str)
        catalog = catalog.drop_duplicates("stream_id")
        joined = catalog.merge(
            mapping_status[["stream_id", "has_programmes"]], on="stream_id", how="left"
        )
        joined["is_mapped"] = joined["has_programmes"].notna()
        joined["has_programmes"] = joined["has_programmes"].fillna(False).astype(bool)
        category_coverage = (
            joined.groupby(["category_id", "category_name"], dropna=False, sort=True)
            .agg(
                catalog_channels=("stream_id", "count"),
                mapped_channels=("is_mapped", "sum"),
                channels_with_programmes=("has_programmes", "sum"),
            )
            .reset_index()
        )
        category_coverage["mapped_percent"] = (
            category_coverage["mapped_channels"] * 100.0
            / category_coverage["catalog_channels"]
        ).round(2)
        category_coverage["programme_percent"] = (
            category_coverage["channels_with_programmes"] * 100.0
            / category_coverage["catalog_channels"]
        ).round(2)
        category_coverage.sort_values(
            ["programme_percent", "catalog_channels", "category_name"],
            ascending=[False, False, True],
        ).to_csv(
            output_dir / f"{server_id}_category_programme_coverage.csv",
            index=False,
            encoding="utf-8-sig",
        )

    (output_dir / f"{server_id}_validation.json").write_text(
        json.dumps(validation, indent=2), encoding="utf-8"
    )

    readme = f"""SKY TV simple TiviMate EPG package

Server: {server_id}
Generated UTC: {manifest['generatedAtIso']}
Builder: {BUILDER_VERSION} ({BUILDER_BUILD_ID})
Old/trusted mapping used: NO
Past retained: {past_days} days
Future retained: every useful future programme supplied by the source files
Mapped streams: {manifest['mappedStreams']}
Mapped streams with programme data: {manifest['mappedStreamsWithProgrammes']}
Programme rows: {manifest['programmeRows']}
Recommended next refresh UTC: {refresh_plan.get('recommendedRefreshAt') or 'not calculated'}

TiviMate needs only:
- {data_file.name}

The refresh-plan JSON records the future endpoint of every used EPGShare feed.
A later GitHub workflow can rebuild when the current time reaches the recommended
refresh time. Blank and placeholder programme titles are skipped.

Repeated canonical XMLTV IDs across panel/EPGShare feeds are resolved automatically
using the schedule with the strongest useful future coverage. The detailed choices are
listed in {server_id}_canonical_id_feed_resolutions.csv.

No server URL, username, or password is written to this package.
"""
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")

    package_zip = work_dir / f"{server_id}_tivimate_epg_output.zip"
    with zipfile.ZipFile(package_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)

    return package_zip, manifest


def load_mapping_bundle_from_uploaded_file(
    expected_server_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    try:
        from google.colab import files
    except ImportError as exc:
        raise RuntimeError(
            "File upload is available when this notebook runs in Google Colab."
        ) from exc

    uploaded = files.upload()
    if not uploaded:
        raise RuntimeError("No mapping file was uploaded.")
    name, content = next(iter(uploaded.items()))
    channel_report: pd.DataFrame | None = None

    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            config_name = next(
                (
                    item for item in archive.namelist()
                    if Path(item).name.lower() in {
                        "epg_sources.json", "epg_sources_used.json"
                    }
                ),
                None,
            )
            if config_name:
                with archive.open(config_name) as handle:
                    source_payload = json.load(io.TextIOWrapper(handle, encoding="utf-8-sig"))
                restored = register_epgshare_sources(source_payload, overwrite=True)
                if restored:
                    print("Restored EPG sources from uploaded ZIP:", ", ".join(restored))

            candidates = [item for item in archive.namelist() if item.lower().endswith(".csv")]
            priority = [
                f"{expected_server_id}_final_mapping.csv",
                "final_mapping.csv",
                "mapping_ready_for_test.csv",
                "all_channels_and_matches.csv",
                "automatic_matches.csv",
            ]
            chosen = None
            for wanted in priority:
                chosen = next(
                    (item for item in candidates if Path(item).name == wanted), None
                )
                if chosen:
                    break
            if chosen is None:
                raise RuntimeError("The ZIP does not contain a recognized mapping CSV.")
            with archive.open(chosen) as handle:
                frame = pd.read_csv(handle, dtype=str, keep_default_na=False)

            report_name = next(
                (
                    item for item in candidates
                    if Path(item).name in {"all_channels_and_matches.csv", "channel_names_only.csv"}
                ),
                None,
            )
            if report_name:
                with archive.open(report_name) as handle:
                    channel_report = pd.read_csv(handle, dtype=str, keep_default_na=False)
    elif name.lower().endswith(".csv"):
        frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        if {"stream_id", "channel_name"}.issubset(frame.columns):
            channel_report = frame.copy()
    else:
        raise RuntimeError("Upload a CSV mapping or an inventory ZIP.")

    return normalize_mapping_frame(frame, expected_server_id), channel_report
# -----------------------------------------------------------------------------
# Smart Rules v4 - event-bank detection and exact alias matcher
# -----------------------------------------------------------------------------
# This single cell contains the active fresh matching logic. It does not contain or use
# any old/trusted server mapping. The EPG source list is still controlled by the
# editable source-manager cell below.

SMART_RULES_VERSION = "4.0"

TECH_WORDS = {
    'hd', 'fhd', 'uhd', 'sd', '4k', '8k', 'hevc', 'h265', 'h264',
    '1080p', '1080i', '720p', '50fps', '60fps', 'vip', 'backup',
    'raw', 'test', 'multi', 'low', 'bw', 'hq', 'lq',
}
GENERIC_WORDS = {
    'tv', 'channel', 'network', 'live', 'the', 'and', 'of', 'international',
    'feed', 'stream', 'slot', 'radio',
}
CONTENT_WORDS = {
    'movie', 'movies', 'cinema', 'cinemania', 'film', 'films', 'news',
    'sport', 'sports', 'music', 'entertainment', 'kids', 'children',
    'documentary', 'documentaries', 'religious', 'religion', 'cricket',
    'football', 'soccer', 'tennis', 'golf', 'wrestling', 'boxing', 'racing',
}
LANGUAGE_ALIASES = {
    'guj': 'gujarati', 'gujrati': 'gujarati', 'gujrathi': 'gujarati',
    'gujarati': 'gujarati',
    'pun': 'punjabi', 'punj': 'punjabi', 'punjab': 'punjabi', 'punjabi': 'punjabi', 'panjabi': 'punjabi',
    'tm': 'tamil', 'tam': 'tamil', 'tamil': 'tamil',
    'tg': 'telugu', 'tel': 'telugu', 'telugu': 'telugu',
    'mal': 'malayalam', 'malayalam': 'malayalam',
    'kan': 'kannada', 'kand': 'kannada', 'kannada': 'kannada',
    'mar': 'marathi', 'marathi': 'marathi',
    'bn': 'bengali', 'bangla': 'bengali', 'bengali': 'bengali',
    'odia': 'odia', 'odiya': 'odia', 'oriya': 'odia',
    'asam': 'assamese', 'assam': 'assamese', 'assamese': 'assamese',
    'hindi': 'hindi', 'hin': 'hindi', 'urdu': 'urdu',
}
LANGUAGE_WORDS = set(LANGUAGE_ALIASES.values())
REGION_WORDS = {
    'us', 'usa', 'uk', 'gb', 'in', 'india', 'inr', 'ca', 'canada',
    'za', 'south', 'africa', 'bein',
}

SOURCE_SUFFIX_RE = re.compile(
    r"\.(?:us(?:\d+|_locals\d*)?|uk\d*|in\d*|ie\d*|ca\d*|"
    r"au\d*|nz\d*|ch\d*|es\d*|za\d*|pk\d*|lk\d*|np\d*|"
    r"my\d*|sg\d*|pl\d*|gr\d*|ar\d*|pt\d*|efl|laliga|nrl|bein)$",
    re.IGNORECASE,
)
ROUTE_SEGMENT_RE = re.compile(
    r"^(?:\+?18|18\+|xxx|adults?|inr?|in(?:[-_/ ](?:guj(?:rati)?|punj(?:abi)?|"
    r"tm|tamil|tg|telugu|mal|my|ml|malayalam|kan|kn|kannada|mar|marathi|bn|bangla|"
    r"bengali|beng|odia|odiya|od|asam|assam|as|assamese|news))?|us|usa|uk|gb|ca|canada|pk|bd|np|"
    r"pt|tr|fr|ar|ie|my|sg|pl|gr|dstv|live|sports?|efl(?:\s+(?:l1|l2|ch))?)$",
    re.IGNORECASE,
)
WRAPPER_RE = re.compile(r"^\s*(?:\([^)]{1,20}\)|\[[^]]{1,20}\])\s*")


def deaccent(value: object) -> str:
    text = unicodedata.normalize('NFKD', str(value or ''))
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def canonical_text(value: object) -> str:
    text = deaccent(value)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    text = text.replace('&', ' and ').replace('+', ' plus ')
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [LANGUAGE_ALIASES.get(word, word) for word in text.split()]
    text = ' '.join(words)
    text = re.sub(r"\bredzone\b", "red zone", text)
    text = re.sub(r"\bfan\s+duel\b", "fanduel", text)
    text = re.sub(r"\bbally\s+sports(?:\s+network)?\b", "fanduel sports", text)
    text = re.sub(r"\bfanduel\s+sports\s+network\b", "fanduel sports", text)
    text = re.sub(r"\bb\s*4\s*u\b", "b4u", text)
    text = re.sub(r"\benter+r\s*10\b", "enter 10", text)
    text = re.sub(r"\bm\s*n\s+plus(?:\s+movies)?\b", "mn plus", text)
    text = re.sub(r"\b(?:bazar|bazaar)\b", "bajar", text)
    text = re.sub(r"\basmitha\b", "asmita", text)
    text = re.sub(r"\bchard(?:h)?ikla\b|\bchardikala\b", "chardikala", text)
    text = re.sub(r"\baakaash\b", "akash", text)
    text = re.sub(r"\bsuvana\b", "suvarna", text)
    text = re.sub(r"\bgujrat\b", "gujarat", text)
    text = re.sub(r"\bcheannl\b", "channel", text)
    text = re.sub(r"\bid\s+investigation\s+discovery\b", "investigation discovery", text)
    text = re.sub(r"\bpacific\b", "west", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_routing_prefix(value: object) -> str:
    text = str(value or '').strip()
    # Remove simple provider wrappers such as (SP2) before a normal region prefix.
    for _ in range(2):
        updated = WRAPPER_RE.sub('', text)
        if updated == text:
            break
        text = updated

    # Remove up to two routing segments before a pipe, e.g. IN-GUJ | or EFL L1 |.
    for _ in range(2):
        if '|' not in text:
            break
        left, right = text.split('|', 1)
        cleaned_left = canonical_text(left)
        compact_left = cleaned_left.replace(' ', '-')
        if ROUTE_SEGMENT_RE.fullmatch(cleaned_left) or ROUTE_SEGMENT_RE.fullmatch(compact_left):
            text = right.strip()
        else:
            break

    # Remove a plain leading country/provider tag. Do not remove words elsewhere.
    text = re.sub(
        r"^\s*(?:US|USA|UK|GB|INR?|INDIA|CA|CANADA|PK|PT|BEIN|DSTV|XXX)"
        r"(?:[-_/ ](?:GUJ(?:RATI)?|PUNJ(?:ABI)?|TM|TAMIL|TG|TELUGU|MAL|"
        r"MALAYALAM|MY|ML|KAN|KN|KANNADA|MAR|MARATHI|BN|BANGLA|BENGALI|BENG|ODIA|ODIYA|OD|ASAM|ASSAM|AS|"
        r"ASSAMESE|NEWS))?\s*(?:[:/\\-]+\s*|\s+)",
        '', text, flags=re.IGNORECASE,
    )
    # Remove short provider labels left after a country pipe, such as TS or D2H.
    text = re.sub(r"^\s*(?:TS|D2H|AT)\s+(?=[A-Za-z0-9&+])", "", text, flags=re.IGNORECASE)
    return text.strip()


def identity_name(value: object, *, is_epg: bool = False) -> str:
    raw = str(value or '').strip()
    if is_epg:
        raw = SOURCE_SUFFIX_RE.sub('', raw)
        raw = raw.replace('.', ' ').replace('_', ' ').replace('/', ' ').replace('\\', ' ')
        raw = re.sub(r"\b(?:tv\s+channel\s+today|list\s+checkout|today)\b", " ", raw, flags=re.IGNORECASE)
    else:
        raw = strip_routing_prefix(raw)
    text = canonical_text(raw)
    words = [word for word in text.split() if word not in TECH_WORDS and word not in REGION_WORDS]
    return ' '.join(words)


def compact_key(value: object, *, is_epg: bool = False) -> str:
    words = identity_name(value, is_epg=is_epg).split()
    words = [word for word in words if word not in {'tv', 'channel'}]
    key = ''.join(words)
    replacements = {
        'zanmol': 'zeeanmol',
        'zeecinemaindia': 'zeecinema',
        'enterr10': 'enter10',
        'enter10': 'enter10',
        'mhone': 'mhone',
    }
    return replacements.get(key, key)


def distinctive_tokens(value: object, *, is_epg: bool = False) -> set[str]:
    words = set(identity_name(value, is_epg=is_epg).split())
    return {
        word for word in words
        if word not in GENERIC_WORDS
        and word not in CONTENT_WORDS
        and word not in LANGUAGE_WORDS
        and word not in REGION_WORDS
        and word not in TECH_WORDS
        and len(word) > 1
    }


def all_tokens(value: object, *, is_epg: bool = False) -> set[str]:
    return set(identity_name(value, is_epg=is_epg).split())


def row_languages(row: dict[str, Any]) -> set[str]:
    text = canonical_text(f"{row.get('category_name', '')} {row.get('channel_name', '')}")
    return {LANGUAGE_ALIASES.get(word, word) for word in text.split()} & LANGUAGE_WORDS


def row_content(row: dict[str, Any]) -> set[str]:
    text = canonical_text(str(row.get('category_name', '')))
    words = set(text.split())
    result: set[str] = set()
    if words & {'movie', 'movies', 'cinema', 'cinemania', 'film', 'films'}:
        result.update({'movie', 'movies', 'cinema', 'cinemania', 'film', 'films', 'cineplex'})
    result.update(words & CONTENT_WORDS)
    return result



def build_indexes(items: Iterable[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    id_keys: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in items:
        families[item['family_key']].append(item)
        if item['id_key']:
            id_keys[item['id_key']].append(item)
    return dict(families), dict(id_keys)

# These structures are rebuilt from the catalogs downloaded during each Stage 1
# run. Keeping them dynamic means additional EPGShare sources work without any
# matcher-code changes.
SOURCE_REGIONS: dict[str, str] = {}
SOURCE_PRIORITY: dict[str, int] = {}
CANDIDATES: list[dict[str, str]] = []
DUMMY_IDS: dict[str, str] = {}
BY_REGION: dict[str, list[dict[str, str]]] = defaultdict(list)
FAMILIES: dict[str, dict[str, list[dict[str, str]]]] = {}
ID_KEYS: dict[str, dict[str, list[dict[str, str]]]] = {}
FAMILY_BASES: dict[str, dict[tuple[str, ...], list[list[dict[str, str]]]]] = {}
ID_LOOKUP: dict[str, dict[str, str]] = {}
CALLSIGN_INDEX: dict[str, list[dict[str, str]]] = defaultdict(list)


def active_regions() -> set[str]:
    return {region for region in BY_REGION if region and region != "ALL"}


PROVIDER_REGION_RULES: list[tuple[str, re.Pattern[str]]] = [
    ('ZA', re.compile(r"\bdstv\b|\.za(?:\b|$)", re.IGNORECASE)),
    ('AR', re.compile(r"^\s*\(AR\)|\bargentina\b|\.ar(?:\b|$)", re.IGNORECASE)),
    ('PK', re.compile(r"^\s*PK\s*(?:[|:/\\-]|$)|\bpakistan\b|\.pk(?:\b|$)", re.IGNORECASE)),
    ('NP', re.compile(r"\bnepal\b|\.np(?:\b|$)", re.IGNORECASE)),
    ('LK', re.compile(r"\bsri\s+lanka\b|\.lk(?:\b|$)", re.IGNORECASE)),
    ('MY', re.compile(r"\bmalaysia(?:n)?\b|\bastro\s+sports?\b|\.my(?:\b|$)", re.IGNORECASE)),
    ('PL', re.compile(r"\bpoland\b|^\s*PL\s*(?:[|:/\\-]|$)|\.pl(?:\b|$)", re.IGNORECASE)),
    ('GR', re.compile(r"\bgreece\b|\bgreek\b|\.gr(?:\b|$)", re.IGNORECASE)),
    ('SG', re.compile(r"\bsingapore\b|^\s*SG\s*(?:[|:/\\-]|$)|\.sg(?:\b|$)", re.IGNORECASE)),
    ('IE', re.compile(r"\bireland\b|^\s*IE\s*(?:[|:/\\-]|$)|\.ie(?:\b|$)", re.IGNORECASE)),
    ('PT', re.compile(r"\bportugal\b|^\s*PT\s*(?:[|:/\\-]|$)|\.pt(?:\b|$)", re.IGNORECASE)),
    ('LATAM', re.compile(r"^\s*Latin\b|\blatin\s+america\b", re.IGNORECASE)),
]


def detect_route(row: dict[str, Any]) -> tuple[str, bool, str]:
    category = str(row.get('category_name', ''))
    channel = str(row.get('channel_name', ''))
    panel_id = str(row.get('panel_epg_id', ''))
    category_channel = f"{category} {channel}"
    all_text = f"{category_channel} {panel_id}"

    # Strong provider/category clues win first.
    for region, pattern in PROVIDER_REGION_RULES:
        if pattern.search(category_channel) or (region == 'ZA' and pattern.search(all_text)):
            return region, True, f'explicit provider/region {region}'

    # When the channel prefix and panel suffix agree, that regional variant is
    # stronger than a broad provider category (for example UK-Zee Cinema inside
    # an India category).
    prefix_match = re.match(r"^\s*(US|USA|UK|GB|IN|INDIA|CA|CANADA)\s*[-:|/]", channel, re.IGNORECASE)
    if prefix_match:
        prefix_code = prefix_match.group(1).upper()
        prefix_region = {"USA": "US", "GB": "UK", "INDIA": "IN", "CANADA": "CA"}.get(prefix_code, prefix_code)
        suffix_for_region = {"US": ".us", "UK": ".uk", "IN": ".in", "CA": ".ca"}.get(prefix_region)
        if suffix_for_region and panel_id.casefold().endswith(suffix_for_region):
            return prefix_region, True, f'channel prefix and panel suffix agree on {prefix_region}'

    # Standard country categories/prefixes. Category is checked before channel so
    # an Indian category can still hold a UK/USA variant listed in the IN catalog.
    standard_rules = [
        ('IN', re.compile(r"(?:^|[|:/\\\s-])(?:INR?|INDIA)(?:$|[|:/\\\s-])|\b(?:indian|hindi|punjabi|tamil|telugu|malayalam|kannada|marathi|bengali|bangla|gujarati|gujrati|odia|assamese)\b", re.IGNORECASE)),
        ('UK', re.compile(r"(?:^|[|:/\\\s-])(?:UK|GB)(?:$|[|:/\\\s-])|\b(?:united kingdom|british|england|scotland|wales|northern ireland)\b", re.IGNORECASE)),
        ('CA', re.compile(r"(?:^|[|:/\\\s-])CA(?:$|[|:/\\\s-])|\b(?:canada|canadian|ontario|toronto|vancouver|calgary|edmonton|montreal)\b", re.IGNORECASE)),
        ('US', re.compile(r"(?:^|[|:/\\\s-])(?:US|USA)(?:$|[|:/\\\s-])|\b(?:united states|american|fanduel|nfl|nba|mlb|nhl|wnba|ncaa)\b", re.IGNORECASE)),
        ('BEIN', re.compile(r"\bbein\b", re.IGNORECASE)),
    ]
    for region, pattern in standard_rules:
        if pattern.search(category):
            return region, True, f'category indicates {region}'
    for region, pattern in standard_rules:
        if pattern.search(channel):
            return region, True, f'channel indicates {region}'

    # A reliable panel suffix is a final hint only after provider/category checks.
    suffix_map = {
        '.za': 'ZA', '.ar': 'AR', '.pk': 'PK', '.np': 'NP', '.lk': 'LK',
        '.my': 'MY', '.sg': 'SG', '.pl': 'PL', '.gr': 'GR', '.ie': 'IE',
        '.pt': 'PT', '.ca': 'CA', '.uk': 'UK', '.in': 'IN', '.us': 'US',
        '.efl': 'UK', '.laliga': 'ES', '.nrl': 'AU',
    }
    folded = panel_id.casefold()
    for suffix, region in suffix_map.items():
        if folded.endswith(suffix):
            return region, True, f'panel ID suffix indicates {region}'
    return 'ALL', False, 'no reliable region clue'


ADULT_CATEGORY_RE = re.compile(r"(?:^|[|:/\\\s-])(?:xxx|adults?|for\s+adult|18\+)(?:$|[|:/\\\s-])", re.IGNORECASE)
ADULT_CHANNEL_PREFIX_RE = re.compile(r"^\s*(?:\+?18\+?|18\+|xxx)\s*(?:[|:/\\-]|$)", re.IGNORECASE)
ADULT_BRAND_RE = re.compile(r"\b(?:pornbox|playboy|penthouse|brazzers|hustler|vivid|dorcel|redlight|sextreme|babestation)\b", re.IGNORECASE)
ADULT_EXCLUSION_RE = re.compile(r"\badult\s+(?:swim|animation|alternative)\b|\bstingray\s+pop\s+adult\b", re.IGNORECASE)
ONEFOOTBALL_RE = re.compile(r"\bone\s*football\b|\bonefootball\b", re.IGNORECASE)
PPV_RE = re.compile(r"\b(?:ppv|pay\s*per\s*view)\b", re.IGNORECASE)
EVENT_RE = re.compile(r"\bevents?\b", re.IGNORECASE)
MAIN_EVENT_LINEAR_RE = re.compile(r"\b(?:sky\s+sports\s+)?main\s+event\b", re.IGNORECASE)
STREAM_SLOT_RE = re.compile(r"\b(?:stream|feed|slot)\s*\d+\b|\(\s*stream\s*\d+\s*\)", re.IGNORECASE)
VERSUS_RE = re.compile(r"\bvs\.?\b|\bv\.?\s+|\s@\s", re.IGNORECASE)
TWENTY_FOUR_SEVEN_RE = re.compile(r"\b24\s*[/x.-]\s*7\b|\b24\s+7\b|\b24hr\b|\b24\s*hours?\b", re.IGNORECASE)
MOVIE_CONTEXT_RE = re.compile(r"\b(?:cinemania|cinema|movie|movies|film|films|new\s+release|hollywood|bollywood|netflix)\b", re.IGNORECASE)


def dummy_id(name: str, fallback: str = 'Blank.Dummy.us') -> str:
    return DUMMY_IDS.get(name.casefold()) or DUMMY_IDS.get(fallback.casefold()) or name


def special_rule(row: dict[str, Any]) -> dict[str, str] | None:
    category = str(row.get('category_name', ''))
    channel = str(row.get('channel_name', ''))
    panel_id = str(row.get('panel_epg_id', ''))
    context = f"{category} {channel} {panel_id}"

    explicit_adult = ADULT_CATEGORY_RE.search(category) or ADULT_CHANNEL_PREFIX_RE.search(channel) or ADULT_BRAND_RE.search(channel)
    if explicit_adult and not ADULT_EXCLUSION_RE.search(context):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Adult.Programming.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Explicit adult category/channel mapped to the adult dummy guide',
        }

    if ONEFOOTBALL_RE.search(context):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'OneFootball numbered event slot mapped to PPV dummy',
        }

    category_event = bool(PPV_RE.search(category) or EVENT_RE.search(category))
    channel_event = bool(PPV_RE.search(channel) or EVENT_RE.search(channel)) and not MAIN_EVENT_LINEAR_RE.search(channel)
    if category_event or channel_event:
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'PPV/Event category or channel mapped to PPV dummy',
        }

    # Provider-created event banks.
    if re.search(r"\bSPORTS\s*\|\s*ESPN\+", category, re.IGNORECASE):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('ESPN+.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'ESPN+ numbered event slot mapped to ESPN+ dummy',
        }
    if re.search(r"\bSPORTS\s*\|\s*FloSports\b", category, re.IGNORECASE) or re.search(r"^\s*Flo\s*\d+", channel, re.IGNORECASE):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Flo.Events.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'FloSports numbered event slot mapped to Flo dummy',
        }
    if re.search(r"^\s*Fite\s*TV\b", category, re.IGNORECASE) or re.search(r"^\s*Fite\s*TV\s*\d+", channel, re.IGNORECASE):
        target = 'TrillerTV.Dummy.us' if re.search(r"\bTriller", context, re.IGNORECASE) else 'FITE.TV.Dummy.us'
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id(target), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'FITE/Triller numbered event slot mapped to event dummy',
        }
    if re.search(r"\bSPORTS\s*\|\s*Paramount\+", category, re.IGNORECASE) and re.search(r"\b\d+\b", channel):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Paramount+ numbered sports event slot mapped to PPV dummy',
        }

    # EFL and similar team/event streams: inspect category, channel, and panel ID.
    live_category = bool(re.search(r"^\s*LIVE\s*\|", category, re.IGNORECASE))
    known_live_event_group = bool(re.search(r"\b(?:EFL|La\s*Liga|NRL|OneFootball)\b", category, re.IGNORECASE))
    event_specific_name = bool(STREAM_SLOT_RE.search(channel) or VERSUS_RE.search(channel))
    event_panel_suffix = bool(re.search(r"\.(?:efl|laliga|nrl)$", panel_id, re.IGNORECASE))
    if live_category and known_live_event_group and (event_specific_name or event_panel_suffix):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Team/event-specific live hub stream mapped to PPV dummy',
        }
    if live_category and re.search(r"\bDIRTVISION\s*\d+", channel, re.IGNORECASE):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'DIRTVision numbered event slot mapped to PPV dummy',
        }

    # CA DAZN rows that include an event title/time are event slots, not stable channels.
    if re.search(r"^\s*CA\s+DAZN\s*\d+", channel, re.IGNORECASE) and (VERSUS_RE.search(context) or re.search(r"\b(?:event|only)\b", context, re.IGNORECASE)):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'DAZN event-specific slot mapped to PPV dummy',
        }

    if TWENTY_FOUR_SEVEN_RE.search(context):
        target = 'Movie.Dummy.us' if MOVIE_CONTEXT_RE.search(context) else '24.7.Dummy.us'
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id(target), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': '24/7 channel mapped to the appropriate continuous dummy guide',
        }

    if re.search(r"(?:^|[|:/\\-])\s*PT\s*(?:[|:/\\-])\s*Sports?\b", category, re.IGNORECASE) and 'PT' not in active_regions():
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Sports.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'PT sports has no configured Portugal source, so a sports dummy is safer',
        }
    return None


DIRECT_RULES: list[dict[str, Any]] = [
    {'region': 'US', 'pattern': re.compile(r"\b(?:fox\s+sports\s*1|fs\s*1|fs1)\b", re.IGNORECASE), 'ids': ['FS1.Fox.Sports.1.HD.us2', 'FS1.HD.us2'], 'label': 'US Fox Sports 1'},
    {'region': 'US', 'pattern': re.compile(r"\b(?:fox\s+sports\s*2|fs\s*2|fs2)\b", re.IGNORECASE), 'ids': ['FS2.Fox.Sports.2.HD.us2', 'FS2.HD.us2'], 'label': 'US Fox Sports 2'},
    {'region': 'US', 'pattern': re.compile(r"\bnfl\s*red\s*zone\b|\bnfl\s*redzone\b", re.IGNORECASE), 'ids': ['NFL.RedZone.HD.us2'], 'label': 'US NFL RedZone'},
    {'region': 'US', 'pattern': re.compile(r"\b(?:bally\s+sports\s+yes|yes\s+network)\b", re.IGNORECASE), 'ids': ['Yes.Network.us2'], 'label': 'US YES Network'},
]

def direct_rule(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    context = f"{row.get('category_name', '')} {row.get('channel_name', '')}"
    for rule in DIRECT_RULES:
        if region != rule['region'] or not rule['pattern'].search(context):
            continue
        available = [ID_LOOKUP[item.casefold()] for item in rule['ids'] if item.casefold() in ID_LOOKUP]
        if not available:
            return None
        primary = available[0]
        alternate = available[1] if len(available) > 1 else None
        return {
            'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
            'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
            'best_score': 100.0,
            'second_epg_id': alternate['epg_id'] if alternate else '',
            'second_epg_feed': alternate['feed'] if alternate else '',
            'second_score': 100.0 if alternate else '',
            'score_margin': 0.0 if alternate else 100.0,
            'reason': f"Built-in exact alias rule: {rule['label']}",
        }
    return None


def candidate_preference(candidate: dict[str, str], row: dict[str, Any]) -> tuple[Any, ...]:
    channel = str(row.get('channel_name', ''))
    epg_id = candidate['epg_id']
    folded = epg_id.casefold()
    wants_hd = bool(re.search(r"\b(?:hd|fhd|uhd|4k)\b", channel, re.IGNORECASE))
    has_hd = bool(re.search(r"(?:^|[._])HD(?:[._]|$)", epg_id, re.IGNORECASE))
    junk = bool(re.search(r"today|list\.checkout|tv\.channel\.today", folded))
    duplicate = bool(re.search(r"duplicate|test", folded))
    return (
        1 if junk else 0,
        1 if duplicate else 0,
        SOURCE_PRIORITY.get(candidate['feed'], 50),
        0 if wants_hd and has_hd else 1,
        len(epg_id),
        epg_id.casefold(),
    )


def choose_preferred(group: list[dict[str, str]], row: dict[str, Any]) -> dict[str, str]:
    return min(group, key=lambda candidate: candidate_preference(candidate, row))


def direction_safe(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    query_tokens = all_tokens(row.get('channel_name', ''))
    candidate_tokens = all_tokens(candidate['epg_id'], is_epg=True)
    query_west = 'west' in query_tokens or 'pacific' in query_tokens
    candidate_west = 'west' in candidate_tokens or 'pacific' in candidate_tokens
    if query_west and not candidate_west:
        return False
    if 'east' in query_tokens and candidate_west:
        return False
    return True


def numbers_safe(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    query_numbers = {token for token in all_tokens(row.get('channel_name', '')) if token.isdigit()}
    candidate_numbers = {token for token in all_tokens(candidate['epg_id'], is_epg=True) if token.isdigit()}
    return not (query_numbers and candidate_numbers and query_numbers.isdisjoint(candidate_numbers))


def languages_safe(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    query_languages = row_languages(row)
    candidate_languages = all_tokens(candidate['epg_id'], is_epg=True) & LANGUAGE_WORDS
    return not (query_languages and candidate_languages and query_languages.isdisjoint(candidate_languages))


def candidate_plausible(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    if not direction_safe(row, candidate) or not numbers_safe(row, candidate) or not languages_safe(row, candidate):
        return False
    query_core = distinctive_tokens(row.get('channel_name', ''))
    candidate_core = distinctive_tokens(candidate['epg_id'], is_epg=True)
    if not query_core or not candidate_core:
        return identity_name(row.get('channel_name', '')) == candidate['normalized']
    return bool(query_core & candidate_core)


def exact_family_match(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    query = identity_name(row.get('channel_name', ''))
    if not query:
        return None
    families = FAMILIES.get(region, FAMILIES['ALL']) if region != 'ALL' else FAMILIES['ALL']
    group = [item for item in families.get(query, []) if direction_safe(row, item) and numbers_safe(row, item) and languages_safe(row, item)]
    if not group:
        # Allow a visible language suffix/prefix to be omitted by the EPG ID only
        # when exactly one family has the same non-language identity.
        query_languages = row_languages(row)
        if not query_languages:
            return None
        query_base = tuple(word for word in query.split() if word not in LANGUAGE_WORDS and word not in {"tv", "channel"})
        base_core = [
            word for word in query_base
            if word not in GENERIC_WORDS and word not in CONTENT_WORDS and not word.isdigit()
        ]
        if len(base_core) < 2:
            return None
        base_matches: list[list[dict[str, str]]] = []
        for family_group in FAMILY_BASES.get(region if region in FAMILY_BASES else 'ALL', {}).get(query_base, []):
            plausible = [
                item for item in family_group
                if direction_safe(row, item)
                and numbers_safe(row, item)
                and languages_safe(row, item)
                and ((all_tokens(item['epg_id'], is_epg=True) & LANGUAGE_WORDS) in (set(), query_languages))
            ]
            if plausible:
                base_matches.append(plausible)
        if len(base_matches) == 1:
            group = base_matches[0]
        else:
            return None
    if region == 'ALL' and len({item['region'] for item in group}) != 1:
        return None
    # Generic-only identities such as "movies" are not enough to auto-map.
    if not distinctive_tokens(query) and len(query.split()) <= 1:
        return None
    primary = choose_preferred(group, row)
    alternates = [item for item in sorted(group, key=lambda item: candidate_preference(item, row)) if item is not primary]
    alternate = alternates[0] if alternates else None
    return {
        'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': 100.0,
        'second_epg_id': alternate['epg_id'] if alternate else '',
        'second_epg_feed': alternate['feed'] if alternate else '',
        'second_score': 100.0 if alternate else '',
        'score_margin': 0.0 if alternate else 100.0,
        'reason': 'Exact channel identity; equivalent EPG variants were grouped instead of treated as competitors',
    }


def language_context_match(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    languages = row_languages(row)
    query_core = distinctive_tokens(row.get('channel_name', ''))
    if not languages or not query_core:
        return None
    families = FAMILIES.get(region, {})
    matching: list[tuple[str, list[dict[str, str]]]] = []
    for family, group in families.items():
        family_tokens = set(family.split())
        family_core = distinctive_tokens(family)
        family_languages = family_tokens & LANGUAGE_WORDS
        if not family_languages or family_languages.isdisjoint(languages):
            continue
        if query_core != family_core and not query_core.issubset(family_core):
            continue
        plausible = [item for item in group if candidate_plausible(row, item)]
        if plausible:
            matching.append((family, plausible))
    if len(matching) != 1:
        return None
    _family, group = matching[0]
    primary = choose_preferred(group, row)
    return {
        'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': 99.0, 'second_epg_id': '', 'second_epg_feed': '',
        'second_score': '', 'score_margin': 99.0,
        'reason': 'Channel brand plus category language uniquely identifies the EPGShare family',
    }


def safe_panel_hint(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    panel_id = str(row.get('panel_epg_id', '')).strip()
    key = compact_key(panel_id, is_epg=True)
    if not key or len(key) < 4 or key in {'none', 'null', 'test', 'covid19'} or key.isdigit():
        return None
    id_index = ID_KEYS.get(region, ID_KEYS['ALL']) if region != 'ALL' else ID_KEYS['ALL']
    candidate_groups: list[list[dict[str, str]]] = []
    if key in id_index:
        candidate_groups.append(id_index[key])
    else:
        row_languages_set = row_languages(row)
        row_content_set = row_content(row)
        # Prefix expansion is accepted only when the candidate adds category
        # language/content, e.g. Enter10 -> Enter10 Movies.
        for candidate_key, group in id_index.items():
            if not candidate_key.startswith(key) or len(candidate_key) <= len(key):
                continue
            extras = candidate_key[len(key):]
            allowed_extras = ''.join(sorted(row_languages_set | row_content_set))
            if not extras or not any(word in extras for word in row_languages_set | row_content_set):
                continue
            candidate_groups.append(group)
    if not candidate_groups:
        return None

    candidates = [item for group in candidate_groups for item in group]
    safe: list[dict[str, str]] = []
    query_core = distinctive_tokens(row.get('channel_name', ''))
    query_languages = row_languages(row)
    category_words = row_content(row)
    for candidate in candidates:
        if not direction_safe(row, candidate) or not numbers_safe(row, candidate) or not languages_safe(row, candidate):
            continue
        candidate_core = distinctive_tokens(candidate['epg_id'], is_epg=True)
        if query_core and not query_core.issubset(candidate_core):
            continue
        candidate_tokens = all_tokens(candidate['epg_id'], is_epg=True)
        candidate_extra_core = candidate_core - query_core
        # An exact panel key is a strong provider-supplied identity hint. It may
        # expand a short visible brand (Sonic -> Sonic Nickelodeon), but it may
        # not replace visible distinctive words (Republic Bharat -> Republic TV).
        if candidate_extra_core and (candidate['id_key'] != key or len(candidate_extra_core) > 2):
            continue
        candidate_languages = candidate_tokens & LANGUAGE_WORDS
        if candidate_languages and query_languages and candidate_languages.isdisjoint(query_languages):
            continue
        # For a prefix expansion, require its added concept to match the category.
        if candidate['id_key'] != key:
            candidate_content = candidate_tokens & CONTENT_WORDS
            if not ((candidate_languages & query_languages) or (candidate_content & category_words)):
                continue
        safe.append(candidate)
    if not safe:
        return None

    # Collapse only one identity family. Multiple distinct families remain review.
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in safe:
        families[candidate['family_key']].append(candidate)
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    if region == 'ALL' and len({item['region'] for item in group}) != 1:
        return None
    primary = choose_preferred(group, row)
    alternate_options = [item for item in sorted(group, key=lambda item: candidate_preference(item, row)) if item is not primary]
    alternate = alternate_options[0] if alternate_options else None
    return {
        'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': 100.0,
        'second_epg_id': alternate['epg_id'] if alternate else '',
        'second_epg_feed': alternate['feed'] if alternate else '',
        'second_score': 100.0 if alternate else '',
        'score_margin': 0.0 if alternate else 100.0,
        'reason': 'Unused panel ID safely identifies the same EPGShare channel family',
    }


# US local-station call signs. Explicit subchannels must match the same subchannel;
# KSHB-DT3 must never silently fall back to KSHB-DT.
ROW_CALLSIGN_RE = re.compile(r"\b([KW][A-Z]{2,4})(?:[- ](?:TV|DT|LD|CD))?(?:[- ]?(\d+))?\b", re.IGNORECASE)


def row_callsigns(value: object) -> list[tuple[str, str]]:
    raw = str(value or '')
    result: list[tuple[str, str]] = []
    for wrapper in re.finditer(r"[\[(]([^\])]{2,24})[\])]", raw):
        part = wrapper.group(1).strip()
        match = ROW_CALLSIGN_RE.fullmatch(part)
        if match:
            item = (match.group(1).upper(), match.group(2) or '')
            if item not in result:
                result.append(item)
    for match in re.finditer(r"\b([KW][A-Z]{2,4})[- ](?:TV|DT|LD|CD)(?:[- ]?(\d+))?\b", raw, re.IGNORECASE):
        item = (match.group(1).upper(), match.group(2) or '')
        if item not in result:
            result.append(item)
    return result


def candidate_callsigns(candidate: dict[str, str]) -> set[tuple[str, str]]:
    raw = SOURCE_SUFFIX_RE.sub('', candidate['epg_id'])
    result: set[tuple[str, str]] = set()
    for match in re.finditer(r"(?:^|[.(])([KW][A-Z]{2,4})(?:-(?:TV|DT|LD|CD))?(?:-(\d+))?(?=[.)]|$)", raw, re.IGNORECASE):
        result.add((match.group(1).upper(), match.group(2) or ''))
    return result



def callsign_match(row: dict[str, Any]) -> dict[str, Any] | None:
    signs = row_callsigns(f"{row.get('channel_name', '')} {row.get('panel_epg_id', '')}")
    if not signs:
        return None
    possible_sets: list[set[tuple[str, str]]] = []
    candidate_lookup: dict[tuple[str, str], dict[str, str]] = {}
    for base, sub in signs:
        matches: list[dict[str, str]] = []
        for candidate in CALLSIGN_INDEX.get(base, []):
            csigns = candidate_callsigns(candidate)
            if sub:
                if (base, sub) in csigns:
                    matches.append(candidate)
            else:
                if (base, '') in csigns:
                    matches.append(candidate)
        if not matches:
            # An explicit subchannel with no exact match must remain unresolved.
            if sub:
                return None
            continue
        keys = {(item['feed'], item['epg_id']) for item in matches}
        possible_sets.append(keys)
        for item in matches:
            candidate_lookup[(item['feed'], item['epg_id'])] = item
    if not possible_sets:
        return None
    intersection = set.intersection(*possible_sets) if len(possible_sets) > 1 else possible_sets[0]
    if not intersection:
        # Several call signs can refer to one combined candidate; accept only when
        # exactly one candidate contains every listed base call sign.
        combined = []
        wanted_bases = {base for base, _sub in signs}
        for candidate in BY_REGION.get('US', []):
            present = {base for base, _sub in candidate_callsigns(candidate)}
            if wanted_bases.issubset(present):
                combined.append(candidate)
        intersection = {(item['feed'], item['epg_id']) for item in combined}
        for item in combined:
            candidate_lookup[(item['feed'], item['epg_id'])] = item
    if not intersection:
        return None
    options = [candidate_lookup[key] for key in intersection]
    # Multiple genuinely different station schedules remain review.
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in options:
        families[item['family_key']].append(item)
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    primary = choose_preferred(group, row)
    return {
        'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': 100.0, 'second_epg_id': '', 'second_epg_feed': '',
        'second_score': '', 'score_margin': 100.0,
        'reason': 'Exact US broadcast call-sign match; explicit subchannels require an exact subchannel',
    }


def fanduel_match(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    context = f"{row.get('category_name', '')} {row.get('channel_name', '')}"
    if region != 'US' or not re.search(r"\b(?:bally\s+sports|fanduel\s+sports|fanduel\s+tv)\b", context, re.IGNORECASE):
        return None
    if re.search(r"\bbally\s+sports\s+yes\b", context, re.IGNORECASE):
        return None
    pool = [item for item in BY_REGION['US'] if item['feed'] == 'FANDUEL1']
    query = identity_name(row.get('channel_name', ''))
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in pool:
        groups[item['family_key']].append(item)
    if query in groups:
        primary = choose_preferred(groups[query], row)
        return {
            'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
            'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
            'best_score': 100.0, 'second_epg_id': '', 'second_epg_feed': '',
            'second_score': '', 'score_margin': 100.0,
            'reason': 'Bally Sports/FanDuel exact regional-network alias',
        }
    names = list(groups)
    raw = process.extract(query, names, scorer=fuzz.WRatio, limit=2, score_cutoff=70)
    scored: list[tuple[str, float]] = [(name, float(score)) for name, score, _idx in raw]
    if not scored:
        return None
    best_name, best_score = scored[0]
    second_name, second_score = scored[1] if len(scored) > 1 else ('', 0.0)
    primary = choose_preferred(groups[best_name], row)
    second_candidate = choose_preferred(groups[second_name], row) if second_name else None
    # Regional networks can have close Plus/Extra alternatives. Only exact names
    # auto-map; fuzzy ones remain review but never point to unrelated feeds.
    return {
        'action': 'REVIEW', 'source': 'epgshare_candidate',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': round(best_score, 1),
        'second_epg_id': second_candidate['epg_id'] if second_candidate else '',
        'second_epg_feed': second_candidate['feed'] if second_candidate else '',
        'second_score': round(second_score, 1) if second_candidate else '',
        'score_margin': round(best_score - second_score, 1),
        'reason': 'Bally/FanDuel regional variant needs one review',
    }


def fuzzy_family_match(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region == 'ALL':
        return None
    families = FAMILIES.get(region, {})
    if not families:
        return None
    query = identity_name(row.get('channel_name', ''))
    if not query:
        return None
    names = list(families)
    raw = process.extract(query, names, scorer=fuzz.WRatio, limit=25, score_cutoff=72)
    scored: list[tuple[float, str, list[dict[str, str]]]] = []
    query_core = distinctive_tokens(query)
    for family, raw_score, _idx in raw:
        plausible = [item for item in families[family] if candidate_plausible(row, item)]
        if not plausible:
            continue
        family_core = distinctive_tokens(family)
        shared = query_core & family_core
        if not shared:
            continue
        score = float(raw_score)
        # A single shared word is enough for review, but not for automatic use
        # when either side contains additional distinctive words.
        if len(shared) == 1 and (len(query_core) > 1 or len(family_core) > 1):
            score = min(score, 92.0)
        q_coverage = len(shared) / max(1, len(query_core))
        c_coverage = len(shared) / max(1, len(family_core))
        score += 3.0 * min(q_coverage, c_coverage)
        scored.append((min(score, 100.0), family, plausible))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_family, best_group = scored[0]
    second_score, second_family, second_group = scored[1] if len(scored) > 1 else (0.0, '', [])
    margin = best_score - second_score
    primary = choose_preferred(best_group, row)
    secondary = choose_preferred(second_group, row) if second_group else None
    query_core = distinctive_tokens(query)
    family_core = distinctive_tokens(best_family)
    strong_identity = bool(query_core) and query_core == family_core
    query_numbers = {token for token in all_tokens(row.get('channel_name', '')) if token.isdigit()}
    candidate_numbers = {token for token in all_tokens(primary['epg_id'], is_epg=True) if token.isdigit()}
    number_identity = query_numbers == candidate_numbers or (not query_numbers and not candidate_numbers)
    action = 'AUTO_EPGSHARE' if best_score >= 97.0 and margin >= 7.0 and strong_identity and number_identity else 'REVIEW'
    return {
        'action': action,
        'source': 'epgshare' if action == 'AUTO_EPGSHARE' else 'epgshare_candidate',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': round(best_score, 1),
        'second_epg_id': secondary['epg_id'] if secondary else '',
        'second_epg_feed': secondary['feed'] if secondary else '',
        'second_score': round(second_score, 1) if secondary else '',
        'score_margin': round(margin, 1),
        'reason': 'High-confidence unique EPGShare family match' if action == 'AUTO_EPGSHARE' else 'Possible source-specific EPGShare family match requires review',
    }


def classify_review_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        'proposed_action': 'UNMATCHED', 'proposed_source': '',
        'proposed_epg_id': '', 'proposed_epg_feed': '',
        'proposed_best_score': '', 'proposed_second_epg_id': '',
        'proposed_second_epg_feed': '', 'proposed_second_score': '',
        'proposed_score_margin': '', 'proposed_reason': 'No safe match',
        'proposed_region': '',
    }
    special = special_rule(row)
    if special:
        result.update({
            'proposed_action': special['action'], 'proposed_source': special['source'],
            'proposed_epg_id': special['epg_id'], 'proposed_epg_feed': special['epg_feed'],
            'proposed_reason': special['reason'],
        })
        return result

    region, explicit, route_reason = detect_route(row)
    result['proposed_region'] = region

    direct = direct_rule(row, region)
    if direct:
        return convert_result(result, direct)

    fanduel = fanduel_match(row, region)
    if fanduel:
        return convert_result(result, fanduel)

    if region == 'US':
        call = callsign_match(row)
        if call:
            return convert_result(result, call)

    if explicit and region not in active_regions() and region != 'ALL':
        result['proposed_action'] = 'UNMATCHED'
        result['proposed_reason'] = f'No enabled EPGShare source is configured for {region}; unrelated countries are not searched'
        return result

    exact = exact_family_match(row, region)
    if exact:
        return convert_result(result, exact)

    panel = safe_panel_hint(row, region)
    if panel:
        return convert_result(result, panel)

    language_match = language_context_match(row, region)
    if language_match:
        return convert_result(result, language_match)

    fuzzy = fuzzy_family_match(row, region)
    if fuzzy:
        return convert_result(result, fuzzy)

    result['proposed_action'] = 'UNMATCHED'
    result['proposed_reason'] = (
        f'No safe {region} EPGShare identity match' if region != 'ALL'
        else 'No exact global identity match; unsafe all-country fuzzy matching was skipped'
    )
    return result


def convert_result(base: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    base.update({
        'proposed_action': match.get('action', ''),
        'proposed_source': match.get('source', ''),
        'proposed_epg_id': match.get('epg_id', ''),
        'proposed_epg_feed': match.get('epg_feed', ''),
        'proposed_best_score': match.get('best_score', ''),
        'proposed_second_epg_id': match.get('second_epg_id', ''),
        'proposed_second_epg_feed': match.get('second_epg_feed', ''),
        'proposed_second_score': match.get('second_score', ''),
        'proposed_score_margin': match.get('score_margin', ''),
        'proposed_reason': match.get('reason', ''),
    })
    return base



def detect_route(row: dict[str, Any]) -> tuple[str, bool, str]:
    category = str(row.get("category_name", ""))
    channel = str(row.get("channel_name", ""))
    panel_id = str(row.get("panel_epg_id", ""))
    category_channel = f"{category} {channel}"
    all_text = f"{category_channel} {panel_id}"

    # Strong provider/category clues win first. This prevents a DStv channel
    # carrying a stale .uk panel ID from being compared with UK channels.
    for region, pattern in PROVIDER_REGION_RULES:
        if pattern.search(category_channel) or (region == "ZA" and pattern.search(all_text)):
            return region, True, f"explicit provider/region {region}"

    # A visible country prefix that agrees with the panel suffix is stronger
    # than a broad category label.
    prefix_match = re.match(
        r"^\s*(US|USA|UK|GB|IN|INDIA|CA|CANADA|AU|NZ|ZA|PT|IE|PK)\s*[-:|/]",
        channel,
        re.IGNORECASE,
    )
    if prefix_match:
        prefix_code = prefix_match.group(1).upper()
        prefix_region = {
            "USA": "US", "GB": "UK", "INDIA": "IN", "CANADA": "CA",
        }.get(prefix_code, prefix_code)
        suffix_for_region = {
            "US": ".us", "UK": ".uk", "IN": ".in", "CA": ".ca",
            "AU": ".au", "NZ": ".nz", "ZA": ".za", "PT": ".pt",
            "IE": ".ie", "PK": ".pk",
        }.get(prefix_region)
        if suffix_for_region and panel_id.casefold().endswith(suffix_for_region):
            return prefix_region, True, f"channel prefix and panel suffix agree on {prefix_region}"

    standard_rules = [
        ("IN", re.compile(
            r"(?:^|[|:/\\\s-])(?:INR?|INDIA)(?:$|[|:/\\\s-])|"
            r"\b(?:indian|hindi|punjabi|tamil|telugu|malayalam|kannada|marathi|"
            r"bengali|bangla|gujarati|gujrati|odia|assamese)\b",
            re.IGNORECASE,
        )),
        ("UK", re.compile(
            r"(?:^|[|:/\\\s-])(?:UK|GB)(?:$|[|:/\\\s-])|"
            r"\b(?:united kingdom|british|england|scotland|wales|northern ireland)\b",
            re.IGNORECASE,
        )),
        ("CA", re.compile(
            r"(?:^|[|:/\\\s-])CA(?:$|[|:/\\\s-])|"
            r"\b(?:canada|canadian|ontario|toronto|vancouver|calgary|edmonton|montreal)\b",
            re.IGNORECASE,
        )),
        ("US", re.compile(
            r"(?:^|[|:/\\\s-])(?:US|USA)(?:$|[|:/\\\s-])|"
            r"\b(?:united states|american|fanduel|nfl|nba|mlb|nhl|wnba|ncaa)\b",
            re.IGNORECASE,
        )),
        ("BEIN", re.compile(r"\bbein\b", re.IGNORECASE)),
    ]
    for region, pattern in standard_rules:
        if pattern.search(category):
            return region, True, f"category indicates {region}"
    for region, pattern in standard_rules:
        if pattern.search(channel):
            return region, True, f"channel indicates {region}"

    # Future sources can define their own words in match_keywords. The same
    # source-manager cell that registers a feed populates this dictionary.
    searchable_category = canonical_text(category)
    searchable_channel = canonical_text(channel)
    for region, keywords in EPG_REGION_MATCH_KEYWORDS.items():
        region_code = str(region).upper()
        if region_code in {"ALL", "DUMMY", "US", "UK", "IN", "CA", "BEIN"}:
            continue
        for keyword in sorted({canonical_text(item) for item in keywords if str(item).strip()}, key=len, reverse=True):
            if not keyword:
                continue
            keyword_pattern = re.compile(rf"(?:^|\s){re.escape(keyword)}(?:$|\s)")
            if keyword_pattern.search(searchable_category):
                return region_code, True, f"category keyword indicates {region_code}"
            if keyword_pattern.search(searchable_channel):
                return region_code, True, f"channel keyword indicates {region_code}"

    # Panel suffix is only a final hint after provider/category checks.
    suffix_map = {
        ".za": "ZA", ".ar": "AR", ".pk": "PK", ".np": "NP", ".lk": "LK",
        ".my": "MY", ".sg": "SG", ".pl": "PL", ".gr": "GR", ".ie": "IE",
        ".pt": "PT", ".ca": "CA", ".uk": "UK", ".in": "IN", ".us": "US",
        ".au": "AU", ".nz": "NZ", ".es": "ES", ".fr": "FR", ".de": "DE",
        ".efl": "UK", ".laliga": "ES", ".nrl": "AU", ".bein": "BEIN",
    }
    folded = panel_id.casefold()
    for suffix, region in suffix_map.items():
        if folded.endswith(suffix):
            return region, True, f"panel ID suffix indicates {region}"
    return "ALL", False, "no reliable region clue"


def _smart_v3_prepare_matcher(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> None:
    global SOURCE_REGIONS, SOURCE_PRIORITY, CANDIDATES, DUMMY_IDS
    global BY_REGION, FAMILIES, ID_KEYS, FAMILY_BASES, ID_LOOKUP, CALLSIGN_INDEX

    prepared: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for original in real_candidates:
        feed = str(original.get("feed", "")).strip().upper()
        epg_id = str(original.get("epg_id", "")).strip()
        region = str(original.get("region", "ALL")).strip().upper() or "ALL"
        if not feed or not epg_id or (feed, epg_id) in seen:
            continue
        seen.add((feed, epg_id))
        display_name = str(original.get("display_name", "")).strip() or epg_id_to_name(epg_id)
        normalized = identity_name(epg_id, is_epg=True)
        item = dict(original)
        item.update({
            "feed": feed,
            "epg_id": epg_id,
            "region": region,
            "display_name": display_name,
            "normalized": normalized,
            "family_key": normalized,
            "id_key": compact_key(epg_id, is_epg=True),
        })
        prepared.append(item)

    CANDIDATES = prepared
    DUMMY_IDS = {str(key).casefold(): str(value) for key, value in dummy_ids.items()}
    SOURCE_REGIONS = {item["feed"]: item["region"] for item in CANDIDATES}

    # Prefer the source order shown in the editable registry, while preserving
    # known best-source preferences for equivalent identities.
    SOURCE_PRIORITY = {
        name.upper(): index + 10 for index, name in enumerate(EPGSHARE_FEEDS)
    }
    SOURCE_PRIORITY.update({
        "FANDUEL1": 0, "US2": 0, "US_LOCALS1": 1, "US_SPORTS1": 2,
        "UK1": 0, "IN4": 0, "IN1": 1, "IN2": 4, "CA2": 0, "BEIN1": 0,
    })

    BY_REGION = defaultdict(list)
    for item in CANDIDATES:
        BY_REGION[item["region"]].append(item)

    FAMILIES = {}
    ID_KEYS = {}
    FAMILY_BASES = {}
    for region, items in list(BY_REGION.items()) + [("ALL", CANDIDATES)]:
        family_index, id_index = build_indexes(items)
        FAMILIES[region] = family_index
        ID_KEYS[region] = id_index
        base_index: dict[tuple[str, ...], list[list[dict[str, str]]]] = defaultdict(list)
        for family, family_group in family_index.items():
            family_base = tuple(
                word for word in family.split()
                if word not in LANGUAGE_WORDS and word not in {"tv", "channel"}
            )
            if family_base:
                base_index[family_base].append(family_group)
        FAMILY_BASES[region] = dict(base_index)

    ID_LOOKUP = {item["epg_id"].casefold(): item for item in CANDIDATES}
    CALLSIGN_INDEX = defaultdict(list)
    for item in BY_REGION.get("US", []):
        for base, _subchannel in candidate_callsigns(item):
            CALLSIGN_INDEX[base].append(item)


def _smart_v3_unmatched_reason(region: str) -> str:
    if region != "ALL":
        return f"No safe {region} EPGShare identity match"
    return "No exact global identity match; unsafe all-country fuzzy matching was skipped"


def _smart_v3_apply_match(record: dict[str, Any], match: dict[str, Any]) -> None:
    for key in (
        "action", "source", "epg_id", "epg_feed", "best_score",
        "second_epg_id", "second_epg_feed", "second_score",
        "score_margin", "reason",
    ):
        if key in match:
            record[key] = match.get(key, "")


def _legacy_make_matching_report_v31(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Build a fresh conservative mapping report using the earlier v3.1 rules."""
    _smart_v3_prepare_matcher(real_candidates, dummy_ids)
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
            panel_info = panel_quality.get(
                panel_epg_id.casefold(),
                {
                    "status": "NOT_CHECKED",
                    "current_or_future_informative_rows": 0,
                    "latest_stop_utc": "",
                    "reason": "Panel EPG ID was not checked",
                },
            )
        else:
            panel_info = {
                "status": "NO_PANEL_ID",
                "current_or_future_informative_rows": 0,
                "latest_stop_utc": "",
                "reason": "Server channel has no panel EPG ID",
            }

        row = {
            "category_name": category_name,
            "channel_name": channel_name,
            "panel_epg_id": panel_epg_id,
        }
        region, explicit_region, _route_reason = detect_route(row)
        record: dict[str, Any] = {
            "server_id": server_id,
            "stream_id": stream_id,
            "category_id": category_id,
            "category_name": category_name,
            "channel_number": channel_number,
            "channel_name": channel_name,
            "normalized_name": identity_name(channel_name),
            "detected_region": region,
            "panel_epg_id": panel_epg_id,
            "panel_epg_status": str(panel_info.get("status", "")),
            "panel_usable_programmes": int(
                panel_info.get("current_or_future_informative_rows", 0) or 0
            ),
            "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
            "action": "",
            "source": "",
            "epg_id": "",
            "epg_feed": "",
            "best_score": "",
            "second_epg_id": "",
            "second_epg_feed": "",
            "second_score": "",
            "score_margin": "",
            "reason": "",
        }

        # Business rules must run before fuzzy matching and before panel reuse.
        # This ensures 24/7, adult, PPV/event, OneFootball and event-hub rows
        # receive the intended dummy schedule even when the provider supplied a
        # misleading panel ID.
        match = special_rule(row)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        match = direct_rule(row, region)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        match = fanduel_match(row, region)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        # The panel guide is retained only when Stage 1 found at least one useful
        # current/future programme title. Blank, placeholder-only and stale panel
        # IDs continue to EPGShare matching instead.
        if panel_epg_id and panel_info.get("status") == "USABLE":
            record.update({
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_id": panel_epg_id,
                "epg_feed": "server xmltv.php",
                "reason": "Panel XMLTV contains useful current/future programme information",
            })
            records.append(record)
            continue

        if region == "US":
            match = callsign_match(row)
            if match:
                _smart_v3_apply_match(record, match)
                records.append(record)
                continue

        if explicit_region and region not in active_regions() and region != "ALL":
            record.update({
                "action": "UNMATCHED",
                "source": "",
                "epg_id": "",
                "epg_feed": "",
                "reason": (
                    f"No enabled EPGShare source is configured for {region}; "
                    "unrelated countries are not searched"
                ),
            })
            records.append(record)
            continue

        for resolver in (exact_family_match, safe_panel_hint, language_context_match, fuzzy_family_match):
            match = resolver(row, region)
            if match:
                _smart_v3_apply_match(record, match)
                break

        if not record["action"]:
            record.update({
                "action": "UNMATCHED",
                "source": "",
                "epg_id": "",
                "epg_feed": "",
                "reason": _smart_v3_unmatched_reason(region),
            })
        records.append(record)

    return pd.DataFrame(records)

# =============================================================================
# Smart Rules v4 active layer
# =============================================================================
# The v4 functions below are in the SAME code cell as the matcher. Stage 1 calls
# make_matching_report_v4 directly and verifies the build ID before credentials
# are requested. A skipped/stale cell therefore stops instead of silently using
# an older matcher.

SMART_RULES_VERSION = "4.0"
MATCHER_BUILD_ID = "SKYTV-SMART-RULES-4.0-2026-07-14"

# South/North are channel-region names (FanDuel South, BBC South), not country
# prefixes by themselves. Keep them in channel identities.
REGION_WORDS = set(REGION_WORDS) - {"south", "africa"}

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
}
TRAILING_PROVIDER_TAG_RE = re.compile(
    r"(?:\s*\((?:A|B|C|D|E|F|F2|FL|H|S|X|CX|PC|SD|HD|FHD|UHD|4K)\))+\s*$",
    re.IGNORECASE,
)


def clean_provider_variant(value: object) -> str:
    text = str(value or "").strip()
    text = TRAILING_PROVIDER_TAG_RE.sub("", text)
    text = re.sub(r"\s*\*+\s*$", "", text)
    return text.strip()


def canonical_text(value: object) -> str:
    text = deaccent(value)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    text = text.replace('&', ' and ').replace('+', ' plus ')
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(4|8)\s+k\b", r"\1k", text)
    text = re.sub(r"\b(720|1080)\s*([pi])\b", r"\1\2", text)
    text = re.sub(r"\bh\s*(264|265)\b", r"h\1", text)
    words = [LANGUAGE_ALIASES.get(word, NUMBER_WORDS.get(word, word)) for word in text.split()]
    text = ' '.join(words)

    # Brand/name equivalences.
    text = re.sub(r"\bredzone\b", "red zone", text)
    text = re.sub(r"\bfan\s+duel\b", "fanduel", text)
    text = re.sub(r"\bbally\s+sports(?:\s+network)?\b", "fanduel sports", text)
    text = re.sub(r"\bfanduel\s+sports\s+network\b", "fanduel sports", text)
    text = re.sub(r"\bb\s*4\s*u\b", "b4u", text)
    text = re.sub(r"\benter+r\s*10\b", "enter 10", text)
    text = re.sub(r"\bm\s*n\s+plus(?:\s+movies)?\b", "mn plus", text)
    text = re.sub(r"\b(?:bazar|bazaar|bajaar)\b", "bajar", text)
    text = re.sub(r"\basmitha\b", "asmita", text)
    text = re.sub(r"\bchard(?:h)?ikla\b|\bchardikala\b", "chardikala", text)
    text = re.sub(r"\baakaash\b", "akash", text)
    text = re.sub(r"\bsuvana\b", "suvarna", text)
    text = re.sub(r"\bgujrat\b", "gujarat", text)
    text = re.sub(r"\bcheannl\b", "channel", text)
    text = re.sub(r"\bid\s+investigation\s+discovery\b", "investigation discovery", text)
    text = re.sub(r"\bpacific\b", "west", text)
    text = re.sub(r"\bbig 10\b", "big ten", text)

    # Common EPG abbreviations. These are token/phrase based, not fuzzy guesses.
    text = re.sub(r"\bn\s+west\b", "north west", text)
    text = re.sub(r"\be\s+mid\b", "east midlands", text)
    text = re.sub(r"\bwm\b", "west midlands", text)
    text = re.sub(r"\blon\b", "london", text)
    text = re.sub(r"\bscot\b", "scotland", text)
    text = re.sub(r"\bwal\b", "wales", text)
    text = re.sub(r"\byorks\b", "yorkshire", text)
    text = re.sub(r"\bmpcg\b|\bmp\s+cg\b", "madhya pradesh chhattisgarh", text)
    text = re.sub(r"\bmadhya\s*pradesh\b", "madhya pradesh", text)
    text = re.sub(r"\bchh?attisgarh\b", "chhattisgarh", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_routing_prefix(value: object) -> str:
    text = clean_provider_variant(value)
    for _ in range(2):
        updated = WRAPPER_RE.sub('', text)
        if updated == text:
            break
        text = updated
    for _ in range(2):
        if '|' not in text:
            break
        left, right = text.split('|', 1)
        cleaned_left = canonical_text(left)
        compact_left = cleaned_left.replace(' ', '-')
        if ROUTE_SEGMENT_RE.fullmatch(cleaned_left) or ROUTE_SEGMENT_RE.fullmatch(compact_left):
            text = right.strip()
        else:
            break
    # Preserve the real channel brand "India News". The word News is a
    # routing suffix in labels such as IN-NEWS, but not after the full word India.
    if not re.match(r"^\s*INDIA\s+NEWS\b", text, re.IGNORECASE):
        text = re.sub(
            r"^\s*(?:US|USA|UK|GB|INR?|INDIA|CA|CANADA|PK|PT|BEIN|DSTV|XXX)"
            r"(?:[-_/ ](?:GUJ(?:RATI)?|PUNJ(?:ABI)?|TM|TAMIL|TG|TELUGU|MAL|"
            r"MALAYALAM|MY|ML|KAN|KN|KANNADA|MAR|MARATHI|BN|BANGLA|BENGALI|BENG|ODIA|ODIYA|OD|ASAM|ASSAM|AS|"
            r"ASSAMESE|NEWS))?\s*(?:[:/\\-]+\s*|\s+)",
            '', text, flags=re.IGNORECASE,
        )
    text = re.sub(r"^\s*(?:TS|D2H|AT)\s+(?=[A-Za-z0-9&+])", "", text, flags=re.IGNORECASE)
    return clean_provider_variant(text)


def identity_name(value: object, *, is_epg: bool = False) -> str:
    raw = clean_provider_variant(value)
    if is_epg:
        raw = SOURCE_SUFFIX_RE.sub('', raw)
        raw = raw.replace('.', ' ').replace('_', ' ').replace('/', ' ').replace('\\', ' ')
        # Remove only known scraper-noise phrases. Standalone "Today" is a real
        # part of channels such as Good News Today and must be preserved.
        raw = re.sub(r"\b(?:tv\s+channel\s+today|list\s+checkout)\b", " ", raw, flags=re.IGNORECASE)
    else:
        raw = strip_routing_prefix(raw)
    text = canonical_text(raw)
    words = [word for word in text.split() if word not in TECH_WORDS and word not in REGION_WORDS]
    return ' '.join(words)


# Explicit country/category labels win before broad words such as Ireland. This
# keeps "UK-BBC One Northern Ireland" in UK while still protecting DStv/ZA.
def detect_route(row: dict[str, Any]) -> tuple[str, bool, str]:
    category = str(row.get('category_name', ''))
    channel = str(row.get('channel_name', ''))
    panel_id = str(row.get('panel_epg_id', ''))
    combined = f"{category} {channel}"

    if re.search(r"\bdstv\b|\.za(?:\b|$)", f"{combined} {panel_id}", re.IGNORECASE):
        return 'ZA', True, 'explicit DStv/South Africa provider'

    standard_rules = [
        ('IN', re.compile(r"(?:^|[|:/\\\s-])(?:INR?|INDIA)(?:$|[|:/\\\s-])|\b(?:indian|hindi|punjabi|tamil|telugu|malayalam|kannada|marathi|bengali|bangla|gujarati|gujrati|odia|assamese)\b", re.IGNORECASE)),
        ('UK', re.compile(r"(?:^|[|:/\\\s-])(?:UK|GB)(?:$|[|:/\\\s-])|\b(?:united kingdom|british|england|scotland|wales|northern ireland)\b", re.IGNORECASE)),
        ('CA', re.compile(r"(?:^|[|:/\\\s-])CA(?:$|[|:/\\\s-])|\b(?:canada|canadian|ontario|toronto|vancouver|calgary|edmonton|montreal)\b", re.IGNORECASE)),
        ('US', re.compile(r"(?:^|[|:/\\\s-])(?:US|USA)(?:$|[|:/\\\s-])|\b(?:united states|american|fanduel|nfl|nba|mlb|nhl|wnba|ncaa)\b", re.IGNORECASE)),
        ('BEIN', re.compile(r"\bbein\b", re.IGNORECASE)),
    ]
    for region, pattern in standard_rules:
        if pattern.search(category):
            return region, True, f'category indicates {region}'
    for region, pattern in standard_rules:
        if pattern.search(channel):
            return region, True, f'channel indicates {region}'

    for region, pattern in PROVIDER_REGION_RULES:
        if region == 'ZA':
            continue
        if region == 'IE' and re.search(r"\bnorthern ireland\b", combined, re.IGNORECASE):
            continue
        if pattern.search(combined):
            return region, True, f'explicit provider/region {region}'

    searchable_category = canonical_text(category)
    searchable_channel = canonical_text(channel)
    for region, keywords in EPG_REGION_MATCH_KEYWORDS.items():
        region_code = str(region).upper()
        if region_code in {'ALL', 'DUMMY', 'US', 'UK', 'IN', 'CA', 'BEIN'}:
            continue
        for keyword in sorted({canonical_text(item) for item in keywords if str(item).strip()}, key=len, reverse=True):
            if not keyword:
                continue
            pattern = re.compile(rf"(?:^|\s){re.escape(keyword)}(?:$|\s)")
            if pattern.search(searchable_category):
                return region_code, True, f'category keyword indicates {region_code}'
            if pattern.search(searchable_channel):
                return region_code, True, f'channel keyword indicates {region_code}'

    suffix_map = {
        '.za': 'ZA', '.ar': 'AR', '.pk': 'PK', '.np': 'NP', '.lk': 'LK',
        '.my': 'MY', '.sg': 'SG', '.pl': 'PL', '.gr': 'GR', '.ie': 'IE',
        '.pt': 'PT', '.ca': 'CA', '.uk': 'UK', '.in': 'IN', '.us': 'US',
        '.au': 'AU', '.nz': 'NZ', '.es': 'ES', '.fr': 'FR', '.de': 'DE',
        '.efl': 'UK', '.laliga': 'ES', '.nrl': 'AU', '.bein': 'BEIN',
    }
    folded = panel_id.casefold()
    for suffix, region in suffix_map.items():
        if folded.endswith(suffix):
            return region, True, f'panel ID suffix indicates {region}'
    return 'ALL', False, 'no reliable region clue'


# ------------------------------- event banks ---------------------------------
EVENT_BANK_CATEGORY_RE = re.compile(
    r"^\s*(?:US|USA)\s*\|\s*(?:MLB|NHL|NBA|NFL|MLS|NCAAF|NCAAB|NCAA)"
    r"(?:\s*(?:GAMES?|TEAMS?|EVENTS?))?\s*$",
    re.IGNORECASE,
)
SPORTS_CONTEXT_RE = re.compile(r"\b(?:sport|sports|mlb|nhl|nba|nfl|ncaa|ncaaf|ncaab|f1|motogp|dirtvision|dazn|efl)\b", re.IGNORECASE)
LEAGUE_SLOT_PREFIX_RE = re.compile(
    r"^\s*(?:MLB|NHL|NBA|NFL|MLS)\s*(?:GAME\s*)?(?:\d+\b|\|)",
    re.IGNORECASE,
)
COLLEGE_EVENT_PREFIX_RE = re.compile(r"^\s*NCAA[FB]?\s*\d+\b", re.IGNORECASE)
ESPN_COLLEGE_EXTRA_RE = re.compile(r"\bESPN\s+COLLEGE\s+EXTRA\s*\d+\b", re.IGNORECASE)
GB_DAZN_SLOT_RE = re.compile(r"^\s*GB[- ]DAZN\s*\d+\s*:", re.IGNORECASE)
DYNAMIC_PANEL_SLOT_RE = re.compile(
    r"^(?:MLB(?:GAME)?|NHL(?:BACKUP)?|NBA|NFL|NCAAF|NCAAB|NCAA)\s*0*\d+(?:\.V\d*)?$",
    re.IGNORECASE,
)
MATCHUP_MARKER_RE = re.compile(r"\bvs\.?\b|\s@\s|\s+x\s+", re.IGNORECASE)
DATE_TIME_MARKER_RE = re.compile(
    r"\bstart\s*:\s*\d{4}-\d{2}-\d{2}|\b\d{1,2}[:.]\d{2}\s*(?:am|pm|gmt|utc)?\b|"
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)\b.*\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}-\d{1,2}\b",
    re.IGNORECASE,
)
NUMBERED_LIVE_SLOT_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9 .+/'-]{2,40}\s+\d{1,3}\s*:", re.IGNORECASE)
LINEAR_SPORTS_EXCLUSION_RE = re.compile(
    r"\b(?:MLB|NHL|NBA|NFL)\s+NETWORK\b|\bMLB\s+STRIKE\s*ZONE\b|"
    r"\bNBA\s+TV\b|\bNFL\s+RED\s*ZONE\b",
    re.IGNORECASE,
)


def is_dynamic_event_channel(row: dict[str, Any]) -> tuple[bool, str]:
    category = str(row.get('category_name', ''))
    channel = clean_provider_variant(row.get('channel_name', ''))
    panel_id = clean_provider_variant(row.get('panel_epg_id', ''))
    context = f"{category} {channel} {panel_id}"

    if MAIN_EVENT_LINEAR_RE.search(channel) and not MATCHUP_MARKER_RE.search(channel):
        return False, ''
    if ESPN_COLLEGE_EXTRA_RE.search(channel):
        return True, 'ESPN College Extra numbered event slot'
    if COLLEGE_EVENT_PREFIX_RE.search(channel):
        return True, 'NCAAF/NCAAB numbered event slot'
    if GB_DAZN_SLOT_RE.search(channel):
        return True, 'GB-DAZN numbered event slot with provider-updated event title'
    compact_panel_id = re.sub(r'[^A-Za-z0-9.]', '', panel_id)
    if DYNAMIC_PANEL_SLOT_RE.fullmatch(compact_panel_id):
        return True, 'dynamic league slot panel ID'

    category_is_bank = bool(EVENT_BANK_CATEGORY_RE.fullmatch(category.strip()))
    league_slot = bool(LEAGUE_SLOT_PREFIX_RE.search(channel))
    if category_is_bank and not LINEAR_SPORTS_EXCLUSION_RE.search(channel):
        if league_slot or MATCHUP_MARKER_RE.search(channel) or DATE_TIME_MARKER_RE.search(channel):
            return True, 'MLB/NHL/NBA/NFL event-bank channel'
        # Team-labelled rows such as "NHL | Utah Hockey Club" are also provider
        # slots; the server replaces the visible name when a game is active.
        if re.match(r"^\s*(?:MLB|NHL|NBA|NFL|MLS)\s*\|", channel, re.IGNORECASE):
            return True, 'league team/event-bank slot'

    live_or_sports = bool(re.search(r"^\s*LIVE\s*\|", category, re.IGNORECASE) or SPORTS_CONTEXT_RE.search(category))
    if live_or_sports and NUMBERED_LIVE_SLOT_RE.search(channel):
        if MATCHUP_MARKER_RE.search(context) or DATE_TIME_MARKER_RE.search(context) or '|' in channel:
            return True, 'numbered live sports slot with event title/time'
    if live_or_sports and MATCHUP_MARKER_RE.search(channel) and (
        DATE_TIME_MARKER_RE.search(channel) or STREAM_SLOT_RE.search(channel) or league_slot
    ):
        return True, 'sports channel name contains a specific matchup and time/slot'
    return False, ''


def special_rule(row: dict[str, Any]) -> dict[str, str] | None:
    category = str(row.get('category_name', ''))
    channel = str(row.get('channel_name', ''))
    panel_id = str(row.get('panel_epg_id', ''))
    context = f"{category} {channel} {panel_id}"

    explicit_adult = ADULT_CATEGORY_RE.search(category) or ADULT_CHANNEL_PREFIX_RE.search(channel) or ADULT_BRAND_RE.search(channel)
    if explicit_adult and not ADULT_EXCLUSION_RE.search(context):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Adult.Programming.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Explicit adult category/channel mapped to the adult dummy guide',
        }

    if ONEFOOTBALL_RE.search(context):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'OneFootball event slot mapped to PPV dummy',
        }

    dynamic_event, event_reason = is_dynamic_event_channel(row)
    if dynamic_event:
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': event_reason,
        }

    category_event = bool(PPV_RE.search(category) or EVENT_RE.search(category))
    channel_event = bool(PPV_RE.search(channel) or EVENT_RE.search(channel)) and not MAIN_EVENT_LINEAR_RE.search(channel)
    if category_event or channel_event:
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'),
            'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'PPV/Event category or channel mapped to PPV dummy',
        }

    # Provider-created event banks. Do not turn a clearly named linear ESPNU or
    # ESPN channel into a dummy merely because it sits inside an ESPN+ category.
    if re.search(r"\bSPORTS\s*\|\s*ESPN\+", category, re.IGNORECASE) and (
        re.search(r"\b(?:event|stream|feed|slot)\s*\d+\b", channel, re.IGNORECASE)
        or MATCHUP_MARKER_RE.search(channel)
        or DATE_TIME_MARKER_RE.search(channel)
    ):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('ESPN+.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'ESPN+ event slot mapped to ESPN+ dummy',
        }
    if re.search(r"\bSPORTS\s*\|\s*FloSports\b", category, re.IGNORECASE) or re.search(r"^\s*Flo\s*\d+", channel, re.IGNORECASE):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Flo.Events.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'FloSports numbered event slot mapped to Flo dummy',
        }
    if re.search(r"^\s*Fite\s*TV\b", category, re.IGNORECASE) or re.search(r"^\s*Fite\s*TV\s*\d+", channel, re.IGNORECASE):
        target = 'TrillerTV.Dummy.us' if re.search(r"\bTriller", context, re.IGNORECASE) else 'FITE.TV.Dummy.us'
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id(target), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'FITE/Triller numbered event slot mapped to event dummy',
        }
    if re.search(r"\bSPORTS\s*\|\s*Paramount\+", category, re.IGNORECASE) and re.search(r"\b\d+\b", channel):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Paramount+ numbered sports event slot mapped to PPV dummy',
        }

    live_category = bool(re.search(r"^\s*LIVE\s*\|", category, re.IGNORECASE))
    known_live_event_group = bool(re.search(r"\b(?:EFL|La\s*Liga|NRL|OneFootball)\b", category, re.IGNORECASE))
    event_specific_name = bool(STREAM_SLOT_RE.search(channel) or VERSUS_RE.search(channel))
    event_panel_suffix = bool(re.search(r"\.(?:efl|laliga|nrl)$", panel_id, re.IGNORECASE))
    if live_category and known_live_event_group and (event_specific_name or event_panel_suffix):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'Team/event-specific live hub stream mapped to PPV dummy',
        }
    if live_category and re.search(r"\bDIRTVISION\s*\d+", channel, re.IGNORECASE):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'DIRTVision numbered event slot mapped to PPV dummy',
        }
    if re.search(r"^\s*CA\s+DAZN\s*\d+", channel, re.IGNORECASE) and (
        VERSUS_RE.search(context) or re.search(r"\b(?:event|only)\b", context, re.IGNORECASE)
    ):
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('PPV.EVENTS.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'DAZN event-specific slot mapped to PPV dummy',
        }
    if TWENTY_FOUR_SEVEN_RE.search(context):
        target = 'Movie.Dummy.us' if MOVIE_CONTEXT_RE.search(context) else '24.7.Dummy.us'
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id(target), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': '24/7 channel mapped to the appropriate continuous dummy guide',
        }
    if re.search(r"(?:^|[|:/\\-])\s*PT\s*(?:[|:/\\-])\s*Sports?\b", category, re.IGNORECASE) and 'PT' not in active_regions():
        return {
            'action': 'AUTO_DUMMY', 'source': 'dummy',
            'epg_id': dummy_id('Sports.Dummy.us'), 'epg_feed': 'DUMMY_CHANNELS',
            'reason': 'PT sports has no configured Portugal source, so a sports dummy is safer',
        }
    return None


# -------------------------- exact/known alias rules ---------------------------
def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


DIRECT_RULES: list[dict[str, Any]] = [
    # US national and regional sports.
    {'region': 'US', 'pattern': _r(r'^fox sports 1$|^fs 1$'), 'ids': ['FS1.Fox.Sports.1.HD.us2', 'FS1.HD.us2'], 'label': 'US Fox Sports 1'},
    {'region': 'US', 'pattern': _r(r'^fox sports 2$|^fs 2$'), 'ids': ['FS2.Fox.Sports.2.HD.us2', 'FS2.HD.us2'], 'label': 'US Fox Sports 2'},
    {'region': 'US', 'pattern': _r(r'^nfl red zone(?: lower bw)?$'), 'ids': ['NFL.RedZone.HD.us2'], 'label': 'US NFL RedZone'},
    {'region': 'US', 'pattern': _r(r'^big ten(?: network)? (?:overflow|alternate)(?: \d+)?$'), 'ids': ['Big.Ten.Network.Overflow.HD.us2', 'Big.Ten.Network.Overflow.us2'], 'label': 'Big Ten overflow/alternate'},
    {'region': 'US', 'pattern': _r(r'^big ten(?: network)?$'), 'ids': ['Big.Ten.Network.HD.us2'], 'label': 'Big Ten Network'},
    {'region': 'US', 'pattern': _r(r'^(?:espn )?sec(?: network)?$'), 'ids': ['SEC.Network.HD.us2'], 'label': 'SEC Network'},
    {'region': 'US', 'pattern': _r(r'^(?:espn )?espnu(?: college sports)?$|^espn u(?: college sports)?$'), 'ids': ['ESPNU.HD.us2'], 'label': 'ESPNU'},
    {'region': 'US', 'pattern': _r(r'^espn(?: 1)?$'), 'ids': ['ESPN.HD.us2'], 'label': 'ESPN'},
    {'region': 'US', 'pattern': _r(r'^masn 2$'), 'ids': ['MASN2.-.Mid.Atlantic.Sports.Network.2.HD.us2'], 'label': 'MASN2'},
    {'region': 'US', 'pattern': _r(r'^masn$'), 'ids': ['MASN.-.Mid.Atlantic.Sports.Network.us2'], 'label': 'MASN'},
    {'region': 'US', 'pattern': _r(r'^msg 2 plus$|^msg plus$'), 'ids': ['MSG.Plus.us2'], 'label': 'MSG Plus'},
    {'region': 'US', 'pattern': _r(r'^msg$'), 'ids': ['MSG.National.us2'], 'label': 'MSG National'},
    {'region': 'US', 'pattern': _r(r'^nbc sports california(?: plus| alternate)?$'), 'ids': ['NBC.Sports.California.Plus.us2', 'NBC.Sports.California.Plus.3.us2'], 'label': 'NBC Sports California'},
    {'region': 'US', 'pattern': _r(r'^nbc sports chicago$'), 'ids': ['CHSN.Chicago.Sports.Network.us2'], 'label': 'legacy NBC Sports Chicago / CHSN'},
    {'region': 'US', 'pattern': _r(r'^nbc sports washington$'), 'ids': ['Monumental.Sports.Network.HD.us2'], 'label': 'legacy NBC Sports Washington / Monumental'},
    {'region': 'US', 'pattern': _r(r'^nbc sports network$'), 'ids': ['NBC.Sports.HD.1.us2', 'NBC.Sports.HD.2.us2'], 'label': 'NBC Sports provider feed'},
    {'region': 'US', 'pattern': _r(r'^nesn plus(?: new)?$'), 'ids': ['NESN+.New.England.Sports.Network.Plus.us2'], 'label': 'NESN Plus'},
    {'region': 'US', 'pattern': _r(r'^root sports(?: northwest)? plus$'), 'ids': ['Root.Sports.Northwest.Plus.us2'], 'label': 'Root Sports Northwest Plus'},
    {'region': 'US', 'pattern': _r(r'^spectrum sports ?net lakers alternate$'), 'ids': ['Spectrum.SportsNet.Lakers.HD.us2'], 'label': 'Spectrum SportsNet Lakers'},
    {'region': 'US', 'pattern': _r(r'^bloomberg plus$'), 'ids': ['Bloomberg.HD.us2', 'Bloomberg.Business.Television.us2'], 'label': 'Bloomberg Plus'},
    {'region': 'US', 'pattern': _r(r'^(?:fanduel sports )?yes network$|^fanduel sports yes$'), 'ids': ['Yes.Network.us2'], 'label': 'YES Network'},

    # UK regional BBC One aliases. BBC Scotland and BBC One Scotland are not the same channel.
    {'region': 'UK', 'pattern': _r(r'^bbc 1 london$'), 'ids': ['BBC.One.Lon.HD.uk'], 'label': 'BBC One London'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 north west$'), 'ids': ['BBC.One.N.West.HD.uk'], 'label': 'BBC One North West'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 wales$'), 'ids': ['BBC.One.Wal.HD.uk'], 'label': 'BBC One Wales'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 scotland$'), 'ids': ['BBC.One.ScotHD.uk'], 'label': 'BBC One Scotland'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 yorkshire(?: and north midlands)?$'), 'ids': ['BBC.One.Yorks.HD.uk'], 'label': 'BBC One Yorkshire'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 east midlands$'), 'ids': ['BBC.One.E.Mid.HD.uk'], 'label': 'BBC One East Midlands'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 west midlands$'), 'ids': ['BBC.One.WM.HD.uk'], 'label': 'BBC One West Midlands'},
    {'region': 'UK', 'pattern': _r(r'^bbc 1 northern ireland$'), 'ids': ['BBC.One.NI.HD.uk'], 'label': 'BBC One Northern Ireland'},
    {'region': 'UK', 'pattern': _r(r'^itv 1 central west$'), 'ids': ['ITV1.HD.uk'], 'label': 'ITV1 Central West using available ITV1 guide'},

    # Canada CTV Two: number-word normalization makes CTV2 and CTV Two identical.
    {'region': 'CA', 'pattern': _r(r'^ctv 2 toronto$'), 'ids': ['CTV.Two.-.Toronto.ca2'], 'label': 'CTV Two Toronto'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 atlantic$'), 'ids': ['CTV.Two.-.Atlantic.ca2'], 'label': 'CTV Two Atlantic'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 ottawa(?: ontario)?$'), 'ids': ['CTV.Two.-.Ottawa.ca2'], 'label': 'CTV Two Ottawa'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 (?:london|windsor)(?: ontario)?$'), 'ids': ['CTV.Two.-.London/Windsor.ca2'], 'label': 'CTV Two London/Windsor'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 (?:victoria|vancouver island|vancouver)$'), 'ids': ['CTV.Two.-.Vancouver/Victoria.ca2'], 'label': 'CTV Two Vancouver/Victoria'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 alberta$'), 'ids': ['CTV.Two.Alberta.ca2'], 'label': 'CTV Two Alberta'},
    {'region': 'CA', 'pattern': _r(r'^ctv 2 barrie$'), 'ids': ['CTV.Two.-.Barrie.ca2'], 'label': 'CTV Two Barrie'},

    # India exact aliases and common spelling/abbreviation differences.
    {'region': 'IN', 'pattern': _r(r'^colors gujarati$|^colors(?: tv)?$'), 'category_pattern': _r(r'gujarati'), 'ids': ['COLORS.GUJARATI.in', 'Colors.Gujarati.in'], 'label': 'Colors Gujarati'},
    {'region': 'IN', 'pattern': _r(r'^colors gujarati cinema$|^colors cinema$'), 'category_pattern': _r(r'gujarati'), 'ids': ['COLORS.GUJARATI.CINEMA.in', 'Colors.Gujarati.Cinema.in'], 'label': 'Colors Gujarati Cinema'},
    {'region': 'IN', 'pattern': _r(r'^mh 1$'), 'ids': ['MH.ONE.in', 'Mh.One.in2'], 'label': 'MH One'},
    {'region': 'IN', 'pattern': _r(r'^news up(?: uk)?$'), 'ids': ['India.News.UP.in'], 'label': 'India News UP'},
    {'region': 'IN', 'pattern': _r(r'^good news today$'), 'ids': ['Good.News.Today.in'], 'label': 'Good News Today'},
    {'region': 'IN', 'pattern': _r(r'^sadhna(?: tv)?(?: devotional)?$'), 'ids': ['Sadhna.in'], 'label': 'Sadhna'},
    {'region': 'IN', 'pattern': _r(r'^cnbc bajar$'), 'ids': ['CNBC.BAJAR.in', 'CNBC.Bajar.in'], 'label': 'CNBC Bajar'},
    {'region': 'IN', 'pattern': _r(r'^cnn news(?: 18)?$'), 'ids': ['CNN.NEWS.18.in'], 'label': 'CNN News18'},
    {'region': 'IN', 'pattern': _r(r'^first rajasthan$'), 'ids': ['First.India.News.in'], 'label': 'First India Rajasthan'},
    {'region': 'IN', 'pattern': _r(r'^news 24 madhya pradesh chhattisgarh$'), 'ids': ['NEWS.24.MPCG.in'], 'label': 'News 24 MP/CG'},
    {'region': 'IN', 'pattern': _r(r'^news state madhya pradesh chhattisgarh$'), 'ids': ['News.State.MPCG..in'], 'label': 'News State MP/CG'},
    {'region': 'IN', 'pattern': _r(r'^channel news asia$'), 'ids': ['Channel.News.Asia.International.in'], 'label': 'Channel News Asia'},
    {'region': 'IN', 'pattern': _r(r'^today 24 news$'), 'ids': ['Today.24.News.in', 'IN.24.News.in'], 'label': 'Today 24 News'},
    {'region': 'IN', 'pattern': _r(r'^zee cinema$'), 'ids': ['ZEE.CINEMA.HD.in', 'ZEE.CINEMA.in'], 'label': 'Zee Cinema'},
    {'region': 'IN', 'pattern': _r(r'^b4u movies$'), 'ids': ['B4U.Movies.in'], 'label': 'B4U Movies'},
    {'region': 'IN', 'pattern': _r(r'^sony max$'), 'ids': ['SONY.MAX.in'], 'label': 'Sony Max'},
    {'region': 'IN', 'pattern': _r(r'^zee anmol$'), 'ids': ['ZEE.ANMOL.in'], 'label': 'Zee Anmol'},
    {'region': 'IN', 'pattern': _r(r'^star gold$'), 'ids': ['STAR.GOLD.HD.in', 'STAR.GOLD.in'], 'label': 'Star Gold'},
    {'region': 'IN', 'pattern': _r(r'^and flix$'), 'ids': ['and.Flix.HD.in'], 'label': '&Flix'},
    {'region': 'IN', 'pattern': _r(r'^b4u kadak$'), 'ids': ['B4U.KADAK.in'], 'label': 'B4U Kadak'},
    {'region': 'IN', 'pattern': _r(r'^colors cineplex$'), 'ids': ['COLORS.CINEPLEX.in'], 'label': 'Colors Cineplex'},
    {'region': 'IN', 'pattern': _r(r'^star utsav movies$'), 'ids': ['STAR.UTSAV.MOVIES.in'], 'label': 'Star Utsav Movies'},
    {'region': 'IN', 'pattern': _r(r'^enter 10$'), 'ids': ['Enterr10.Movies.in'], 'label': 'Enter 10 Movies'},
    {'region': 'IN', 'pattern': _r(r'^sony wah$'), 'ids': ['SONY.WAH.in'], 'label': 'Sony Wah'},
    {'region': 'IN', 'pattern': _r(r'^sony pix$'), 'ids': ['SONY.PIX.HD.in'], 'label': 'Sony Pix'},
    {'region': 'IN', 'pattern': _r(r'^star gold select$'), 'ids': ['Star.Gold.Select.HD.in', 'STAR.GOLD.SELECT.in'], 'label': 'Star Gold Select'},
    {'region': 'IN', 'pattern': _r(r'^mn plus$'), 'ids': ['MN+.HD.in'], 'label': 'MN+ Movies'},
    {'region': 'IN', 'pattern': _r(r'^zee classic$'), 'ids': ['ZEE.CLASSIC.in'], 'label': 'Zee Classic'},
]


def _rule_result(ids: list[str], label: str) -> dict[str, Any] | None:
    available = [ID_LOOKUP[item.casefold()] for item in ids if item.casefold() in ID_LOOKUP]
    if not available:
        return None
    primary = available[0]
    alternate = available[1] if len(available) > 1 else None
    return {
        'action': 'AUTO_EPGSHARE', 'source': 'epgshare',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': 100.0,
        'second_epg_id': alternate['epg_id'] if alternate else '',
        'second_epg_feed': alternate['feed'] if alternate else '',
        'second_score': 100.0 if alternate else '',
        'score_margin': 0.0 if alternate else 100.0,
        'reason': f'Exact/known channel alias: {label}',
    }


def direct_rule(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    channel_identity = identity_name(row.get('channel_name', ''))
    category_identity = canonical_text(row.get('category_name', ''))

    # City-branded NBC news streams without a station callsign are not NBC
    # Sports channels.  Leave them unmatched instead of allowing a fuzzy
    # comparison to select NBC Sports or another unrelated NBC service.
    if region == 'US' and re.fullmatch(r'nbc [a-z0-9 ]+ news', channel_identity):
        return {
            'action': 'UNMATCHED', 'source': '', 'epg_id': '', 'epg_feed': '',
            'reason': 'City-branded NBC news stream has no exact station/callsign EPG match; sports guesses are blocked',
        }

    for rule in DIRECT_RULES:
        if region != rule['region']:
            continue
        if not rule['pattern'].search(channel_identity):
            continue
        category_pattern = rule.get('category_pattern')
        if category_pattern is not None and not category_pattern.search(category_identity):
            continue
        return _rule_result(rule['ids'], rule['label'])
    return None


# -------------------------- FanDuel/Bally aliases -----------------------------
FANDUEL_ALIAS_RULES: list[dict[str, Any]] = [
    {'pattern': _r(r'^fanduel sports cincinnati$'), 'ids': ['FanDuel.Sports.Ohio.-.Cincinnati.HD.us', 'FanDuel.Sports.Network.Ohio.-.Cincinnati.us'], 'label': 'Cincinnati'},
    {'pattern': _r(r'^fanduel sports detroit (?:plus|extra)$'), 'ids': ['FanDuel.Sports.Network.Detroit.Extra.us', 'FanDuel.Sports.Network.Detroit.HD.us'], 'label': 'Detroit Extra'},
    {'pattern': _r(r'^fanduel sports detroit$'), 'ids': ['FanDuel.Sports.Network.Detroit.HD.us'], 'label': 'Detroit'},
    {'pattern': _r(r'^fanduel sports florida (?:plus|extra)$'), 'ids': ['FanDuel.Sports.Network.Florida.2.us', 'FanDuel.Sports.Network.Florida.us'], 'label': 'Florida Plus'},
    {'pattern': _r(r'^fanduel sports florida$'), 'ids': ['FanDuel.Sports.Network.Florida.us'], 'label': 'Florida'},
    {'pattern': _r(r'^fanduel sports indiana(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Indiana.us'], 'label': 'Indiana'},
    {'pattern': _r(r'^fanduel sports kansas city$'), 'ids': ['FanDuel.Sports.Midwest.-.Kansas.City.us'], 'label': 'Kansas City'},
    {'pattern': _r(r'^fanduel sports midwest(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.Midwest.St..Louis.us'], 'label': 'Midwest/St. Louis'},
    {'pattern': _r(r'^fanduel sports north out of market$'), 'ids': ['FanDuel.Sports.Network.North.us', 'FanDuel.Sports.Network.North.Extra.us'], 'label': 'North'},
    {'pattern': _r(r'^fanduel sports ohio(?: plus| extra| zone \d+(?: louisville lexington)?| \d+(?: evansville)?)?$'), 'ids': ['FanDuel.Sports.Network.us', 'FanDuel.Sports.Ohio.-.Cleveland.HD.us', 'FanDuel.Sports.Ohio.-.Cincinnati.HD.us'], 'label': 'Ohio generic provider feed'},
    {'pattern': _r(r'^fanduel sports socal(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.SoCal.us'], 'label': 'SoCal'},
    {'pattern': _r(r'^fanduel sports south georgia$'), 'ids': ['FanDuel.Sports.Southeast.-.Georgia.us'], 'label': 'South Georgia'},
    {'pattern': _r(r'^fanduel sports south(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.South.us'], 'label': 'South'},
    {'pattern': _r(r'^fanduel sports southwest(?: feed \d+| plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.Southwest.us'], 'label': 'Southwest'},
    {'pattern': _r(r'^fanduel sports sun alt(?:ernate)?$'), 'ids': ['Fanduel.Sports.Sun.Alt.3.us', 'Fanduel.Sports.Sun.us'], 'label': 'Sun Alternate'},
    {'pattern': _r(r'^fanduel sports sun(?: plus| extra)?$'), 'ids': ['Fanduel.Sports.Sun.HD.us', 'Fanduel.Sports.Sun.us'], 'label': 'Sun'},
    {'pattern': _r(r'^fanduel sports west(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.West.us'], 'label': 'West'},
    {'pattern': _r(r'^fanduel sports wisconsin(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.Wisconsin.us'], 'label': 'Wisconsin'},
    {'pattern': _r(r'^fanduel sports oklahoma(?: plus| extra)?$'), 'ids': ['FanDuel.Sports.Network.Oklahoma.us'], 'label': 'Oklahoma'},
    {'pattern': _r(r'^fanduel tv$'), 'ids': ['FanDuel.TV.us'], 'label': 'FanDuel TV'},
]



def fanduel_channel_identity(value: object) -> str:
    text = canonical_text(strip_routing_prefix(value))
    country_only = {'us', 'usa', 'uk', 'gb', 'in', 'india', 'ca', 'canada', 'za', 'bein'}
    return ' '.join(word for word in text.split() if word not in TECH_WORDS and word not in country_only)

def fanduel_match(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    context = f"{row.get('category_name', '')} {row.get('channel_name', '')}"
    if region != 'US' or not re.search(r"\b(?:bally\s+sports|fanduel\s+sports|fanduel\s+tv)\b", context, re.IGNORECASE):
        return None
    channel_identity = fanduel_channel_identity(row.get('channel_name', ''))
    for rule in FANDUEL_ALIAS_RULES:
        if rule['pattern'].search(channel_identity):
            result = _rule_result(rule['ids'], f"Bally/FanDuel {rule['label']}")
            if result:
                return result

    # For unlisted future regions, stay inside FANDUEL1 and auto-map only a
    # unique high-confidence identity. Otherwise keep one focused review row.
    pool = [item for item in BY_REGION.get('US', []) if item['feed'] == 'FANDUEL1']
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in pool:
        groups[item['family_key']].append(item)
    query = channel_identity
    if query in groups:
        primary = choose_preferred(groups[query], row)
        return _rule_result([primary['epg_id']], 'Bally/FanDuel exact identity')
    names = list(groups)
    raw = process.extract(query, names, scorer=fuzz.WRatio, limit=3, score_cutoff=72)
    scored = [(name, float(score)) for name, score, _idx in raw]
    if not scored:
        return None
    best_name, best_score = scored[0]
    second_name, second_score = scored[1] if len(scored) > 1 else ('', 0.0)
    primary = choose_preferred(groups[best_name], row)
    second_candidate = choose_preferred(groups[second_name], row) if second_name else None
    action = 'AUTO_EPGSHARE' if best_score >= 96.0 and best_score - second_score >= 8.0 else 'REVIEW'
    return {
        'action': action,
        'source': 'epgshare' if action == 'AUTO_EPGSHARE' else 'epgshare_candidate',
        'epg_id': primary['epg_id'], 'epg_feed': primary['feed'],
        'best_score': round(best_score, 1),
        'second_epg_id': second_candidate['epg_id'] if second_candidate else '',
        'second_epg_feed': second_candidate['feed'] if second_candidate else '',
        'second_score': round(second_score, 1) if second_candidate else '',
        'score_margin': round(best_score - second_score, 1),
        'reason': 'Unique FanDuel family match' if action == 'AUTO_EPGSHARE' else 'Unlisted Bally/FanDuel regional variant needs review',
    }


# Reject obvious cross-content guesses such as NBC Los Angeles News -> NBC Sports.
def _content_classes(value: object, *, is_epg: bool = False) -> set[str]:
    tokens = all_tokens(value, is_epg=is_epg)
    result: set[str] = set()
    if tokens & {'news', 'newscast'}:
        result.add('news')
    if tokens & {'sport', 'sports', 'football', 'baseball', 'basketball', 'hockey', 'cricket', 'golf', 'tennis', 'racing'}:
        result.add('sports')
    if tokens & {'movie', 'movies', 'cinema', 'film', 'films'}:
        result.add('movies')
    if tokens & {'music', 'radio'}:
        result.add('music')
    if tokens & {'kids', 'children', 'junior', 'cartoon'}:
        result.add('kids')
    return result


def candidate_plausible(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    if not direction_safe(row, candidate) or not numbers_safe(row, candidate) or not languages_safe(row, candidate):
        return False
    query_classes = _content_classes(row.get('channel_name', ''))
    candidate_classes = _content_classes(candidate['epg_id'], is_epg=True)
    if query_classes and candidate_classes and query_classes.isdisjoint(candidate_classes):
        return False
    query_core = distinctive_tokens(row.get('channel_name', ''))
    candidate_core = distinctive_tokens(candidate['epg_id'], is_epg=True)
    if not query_core or not candidate_core:
        return identity_name(row.get('channel_name', '')) == candidate['normalized']
    return bool(query_core & candidate_core)


def make_matching_report_v4(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Build a fresh mapping report using Smart Rules v4."""
    _smart_v3_prepare_matcher(real_candidates, dummy_ids)
    records: list[dict[str, Any]] = []

    for channel in channels:
        stream_id = str(channel.get('stream_id') or channel.get('id') or '').strip()
        if not stream_id:
            continue
        channel_name = str(channel.get('name') or channel.get('stream_display_name') or '').strip()
        category_id = str(channel.get('category_id') or '').strip()
        category_name = category_names.get(category_id, '')
        panel_epg_id = str(channel.get('epg_channel_id') or '').strip()
        channel_number = str(channel.get('num') or channel.get('channel_number') or '').strip()

        if panel_epg_id:
            panel_info = panel_quality.get(panel_epg_id.casefold(), {
                'status': 'NOT_CHECKED', 'current_or_future_informative_rows': 0,
                'latest_stop_utc': '', 'reason': 'Panel EPG ID was not checked',
            })
        else:
            panel_info = {
                'status': 'NO_PANEL_ID', 'current_or_future_informative_rows': 0,
                'latest_stop_utc': '', 'reason': 'Server channel has no panel EPG ID',
            }

        row = {'category_name': category_name, 'channel_name': channel_name, 'panel_epg_id': panel_epg_id}
        region, explicit_region, _route_reason = detect_route(row)
        record: dict[str, Any] = {
            'server_id': server_id, 'stream_id': stream_id, 'category_id': category_id,
            'category_name': category_name, 'channel_number': channel_number,
            'channel_name': channel_name, 'normalized_name': identity_name(channel_name),
            'detected_region': region, 'panel_epg_id': panel_epg_id,
            'panel_epg_status': str(panel_info.get('status', '')),
            'panel_usable_programmes': int(panel_info.get('current_or_future_informative_rows', 0) or 0),
            'panel_latest_stop_utc': str(panel_info.get('latest_stop_utc', '')),
            'action': '', 'source': '', 'epg_id': '', 'epg_feed': '',
            'best_score': '', 'second_epg_id': '', 'second_epg_feed': '',
            'second_score': '', 'score_margin': '', 'reason': '',
        }

        # Business rules and exact aliases intentionally run before panel reuse.
        # Dynamic event slots must never retain a stale game-specific panel ID.
        for resolver in (
            lambda r: special_rule(r),
            lambda r: direct_rule(r, region),
            lambda r: fanduel_match(r, region),
        ):
            match = resolver(row)
            if match:
                _smart_v3_apply_match(record, match)
                break
        if record['action']:
            records.append(record)
            continue

        if panel_epg_id and panel_info.get('status') == 'USABLE':
            record.update({
                'action': 'KEEP_PANEL', 'source': 'panel', 'epg_id': panel_epg_id,
                'epg_feed': 'server xmltv.php',
                'reason': 'Panel XMLTV contains useful current/future programme information',
            })
            records.append(record)
            continue

        if region == 'US':
            match = callsign_match(row)
            if match:
                _smart_v3_apply_match(record, match)
                records.append(record)
                continue

        if explicit_region and region not in active_regions() and region != 'ALL':
            record.update({
                'action': 'UNMATCHED', 'source': '', 'epg_id': '', 'epg_feed': '',
                'reason': f'No enabled EPGShare source is configured for {region}; unrelated countries are not searched',
            })
            records.append(record)
            continue

        for resolver in (exact_family_match, safe_panel_hint, language_context_match, fuzzy_family_match):
            match = resolver(row, region)
            if match:
                _smart_v3_apply_match(record, match)
                break
        if not record['action']:
            record.update({
                'action': 'UNMATCHED', 'source': '', 'epg_id': '', 'epg_feed': '',
                'reason': _smart_v3_unmatched_reason(region),
            })
        records.append(record)
    return pd.DataFrame(records)


def run_smart_rules_v4_self_test(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Run catalog-backed examples before the real server match starts."""
    cases = [
        ('MLB event bank', 'US | MLB', 'MLB 01 | Royals x Orioles start:2026-07-12 18:35:00', 'MLB01.v3', 'AUTO_DUMMY', 'PPV.EVENTS.Dummy.us'),
        ('NHL event bank', 'US | NHL', 'NHL | Utah Hockey Club', 'UtahHockeyClub.nhl', 'AUTO_DUMMY', 'PPV.EVENTS.Dummy.us'),
        ('NCAAB event', 'US | Sports', 'NCAAB 2 Alabama A&M at Ohio State 3PM', '', 'AUTO_DUMMY', 'PPV.EVENTS.Dummy.us'),
        ('GB DAZN event', 'United Kingdom Sports', 'GB-DAZN 74: NHL.TV| Flames @ Golden Knights| Sun 19 Oct 3:00 AM', '', 'AUTO_DUMMY', 'PPV.EVENTS.Dummy.us'),
        ('ESPN College Extra event', 'US | Sports', 'US ESPN College Extra 3', '', 'AUTO_DUMMY', 'PPV.EVENTS.Dummy.us'),
        ('Bally Cincinnati', 'US | Sports', 'US Bally Sports Cincinnati (A)', '', 'AUTO_EPGSHARE', 'FanDuel.Sports.Ohio.-.Cincinnati.HD.us'),
        ('Bally Detroit Plus', 'US | Sports', 'US Bally Sports Detroit Plus (A)', '', 'AUTO_EPGSHARE', 'FanDuel.Sports.Network.Detroit.Extra.us'),
        ('Bally South Plus', 'US | Sports', 'US Bally Sports South Plus (A)', '', 'AUTO_EPGSHARE', 'FanDuel.Sports.Network.South.us'),
        ('Bally South Georgia', 'US | Sports', 'US Bally Sports South Georgia (A)', '', 'AUTO_EPGSHARE', 'FanDuel.Sports.Southeast.-.Georgia.us'),
        ('Bally Southwest Plus', 'US | Sports', 'US Bally Sports Southwest Plus (H)', '', 'AUTO_EPGSHARE', 'FanDuel.Sports.Network.Southwest.us'),
        ('Bally Sun Alt', 'US | Sports', 'US Bally Sports Sun (Alt) (A)', '', 'AUTO_EPGSHARE', 'Fanduel.Sports.Sun.Alt.3.us'),
        ('Big Ten overflow', 'US | Sports', 'US Big Ten Network Overflow 3 HD (A)', '', 'AUTO_EPGSHARE', 'Big.Ten.Network.Overflow.HD.us2'),
        ('ESPN', 'US | Sports', 'US ESPN 1 (A)', '', 'AUTO_EPGSHARE', 'ESPN.HD.us2'),
        ('MASN2', 'US | Sports', 'US MASN2 (A)', '', 'AUTO_EPGSHARE', 'MASN2.-.Mid.Atlantic.Sports.Network.2.HD.us2'),
        ('Root Sports Plus', 'US | Sports', 'US Root Sports Plus (A)', '', 'AUTO_EPGSHARE', 'Root.Sports.Northwest.Plus.us2'),
        ('Spectrum Lakers Alternate', 'US | Sports', 'US Spectrum SportsNet Lakers Alternate (A)', '', 'AUTO_EPGSHARE', 'Spectrum.SportsNet.Lakers.HD.us2'),
        ('NBC Sports Chicago', 'US | Sports', 'US NBC Sports Chicago (A)', '', 'AUTO_EPGSHARE', 'CHSN.Chicago.Sports.Network.us2'),
        ('Bloomberg Plus', 'US | Sports', 'US Bloomberg+ UHD/4K (D)', '', 'AUTO_EPGSHARE', 'Bloomberg.HD.us2'),
        ('BBC One London', 'UK | Regional & Red Button', 'UK-BBC One London FHD*', '', 'AUTO_EPGSHARE', 'BBC.One.Lon.HD.uk'),
        ('BBC One Scotland', 'UK | Regional & Red Button', 'UK-BBC One Scotland FHD*', '', 'AUTO_EPGSHARE', 'BBC.One.ScotHD.uk'),
        ('BBC One Northern Ireland', 'UK | Regional & Red Button', 'UK-BBC One Northern Ireland', '', 'AUTO_EPGSHARE', 'BBC.One.NI.HD.uk'),
        ('ITV Central West', 'UK | Entertainment', 'UK-ITV 1 Central West', '', 'AUTO_EPGSHARE', 'ITV1.HD.uk'),
        ('India News UP', 'IN | News', 'IN-NEWS | India News Up', 'indianewsupuk.in', 'AUTO_EPGSHARE', 'India.News.UP.in'),
        ('Colors Gujarati', 'INR | Gujrati', 'IN-GUJ | Colors TV', 'colorsgujarati.in', 'AUTO_EPGSHARE', 'COLORS.GUJARATI.in'),
        ('Good News Today', 'IN | News', 'IN-NEWS | Good News Today', '', 'AUTO_EPGSHARE', 'Good.News.Today.in'),
        ('CNBC Bajaar spelling', 'IN | News', 'IN-NEWS | CNBC Bajaar', '', 'AUTO_EPGSHARE', 'CNBC.BAJAR.in'),
        ('News 24 MPCG', 'IN | News', 'IN-NEWS | News 24 Madhyapradesh Chattisgarh', '', 'AUTO_EPGSHARE', 'NEWS.24.MPCG.in'),
        ('CTV2 Toronto', 'CA | General', 'CA CTV2 TORONTO HD (D)', '', 'AUTO_EPGSHARE', 'CTV.Two.-.Toronto.ca2'),
        ('NBC Los Angeles News guard', 'US | News', 'US NBC Los Angeles News (PC)', '', 'UNMATCHED', ''),
    ]
    channels = []
    categories = {}
    for index, (_label, category, channel, panel_id, _action, _epg_id) in enumerate(cases, start=1):
        cid = str(index)
        categories[cid] = category
        channels.append({'stream_id': str(index), 'category_id': cid, 'name': channel, 'epg_channel_id': panel_id})
    report = make_matching_report_v4('self_test', channels, categories, {}, real_candidates, dummy_ids)
    rows = []
    for index, case in enumerate(cases):
        label, _cat, _channel, _panel, expected_action, expected_id = case
        actual = report.iloc[index]
        passed = actual['action'] == expected_action and actual['epg_id'].casefold() == expected_id.casefold()
        rows.append({
            'test': label, 'passed': passed,
            'expected_action': expected_action, 'actual_action': actual['action'],
            'expected_epg_id': expected_id, 'actual_epg_id': actual['epg_id'],
        })
    result = pd.DataFrame(rows)
    failures = result.loc[~result['passed']]
    if not failures.empty:
        display(failures)
        raise RuntimeError('Smart Rules v4 self-test failed. Stop before matching the server.')
    return result


globals().pop('make_matching_report', None)
make_matching_report_v4.smart_rules_version = SMART_RULES_VERSION
make_matching_report_v4.matcher_build_id = MATCHER_BUILD_ID
# Smart Rules v4 base loaded; v5 active layer is defined immediately below.

# =============================================================================
# Smart Rules v5 active layer
# =============================================================================
# v5 adds dynamic WNBA/event-bank detection, official US league-team mapping,
# safer exact aliases (including useful dead-panel-ID clues), and additional
# source-backed India/US rules. Stage 1 calls make_matching_report_v5 directly.

SMART_RULES_VERSION = "5.0"
MATCHER_BUILD_ID = "SKYTV-SMART-RULES-5.0-2026-07-15"

# These are provider/playlist suffixes, not part of a channel identity. Do not
# add arbitrary call signs here: a tag such as (WISH) can identify a local TV
# station and must remain available to the call-sign matcher.
TRAILING_PROVIDER_TAG_RE = re.compile(
    r"(?:\s*\((?:A|B|C|D|E|F|F2|FL|H|S|X|CX|PC|SD|HD|FHD|UHD|4K|DZ|HX|ST|HG)\))+\s*$",
    re.IGNORECASE,
)

_CANONICAL_TEXT_V4 = canonical_text
_STRIP_ROUTING_PREFIX_V4 = strip_routing_prefix


def canonical_text(value: object) -> str:
    """v4 normalization plus a few provider spelling/split repairs."""
    text = _CANONICAL_TEXT_V4(value)
    repairs = (
        (r"\bg\s+old\b", "gold"),
        (r"\bho\s+us\s+ton\b", "houston"),
        (r"\bnew\s+york\s+knicks\s+nyk\b", "new york knicks"),
        (r"\blos\s+angeles\s+clippers\s+lac\b", "los angeles clippers"),
    )
    for pattern, replacement in repairs:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", " ", text).strip()


# Provider labels such as D2H |, TS | and AT | are routing wrappers, not part
# of the channel brand.  v4 removed them only when they appeared after another
# recognized prefix.  Strip them safely before the normal routing parser.
V5_PROVIDER_ROUTE_SEGMENT_RE = re.compile(
    r"^(?:D2H|TS|AT|SP2?|JIO|TATA\s+PLAY|VIDEOCON|AIRTEL|DISH\s*TV)$",
    re.IGNORECASE,
)


def strip_routing_prefix(value: object) -> str:
    text = clean_provider_variant(value)
    for _ in range(3):
        if "|" not in text:
            break
        left, right = text.split("|", 1)
        if V5_PROVIDER_ROUTE_SEGMENT_RE.fullmatch(canonical_text(left).replace(" " , "")):
            text = right.strip()
            continue
        # Also accept the spaced form for provider names such as Tata Play.
        if V5_PROVIDER_ROUTE_SEGMENT_RE.fullmatch(canonical_text(left)):
            text = right.strip()
            continue
        break
    text = re.sub(
        r"^\s*(?:D2H|TS|AT|SP2?|JIO|TATA\s+PLAY|VIDEOCON|AIRTEL|DISH\s*TV)\s+(?=[A-Za-z0-9&+])",
        "", text, flags=re.IGNORECASE,
    )
    result = _STRIP_ROUTING_PREFIX_V4(text)
    # The old parser removes US/CA first, so a short market/provider wrapper may
    # remain at the new beginning, e.g. "US (PH) GMA Pinoy" -> "(PH) GMA Pinoy".
    result = re.sub(
        r"^\s*\((?:PH|CN|GR|IT|FR|MX|PA|TB|SP2?|F2|FD|DZ|HX|ST|HG)\)\s*",
        "", result, flags=re.IGNORECASE,
    )
    return result.strip()


# WNBA and bare league-number slots are provider-controlled event banks just
# like MLB/NHL/NBA numbered slots.
EVENT_BANK_CATEGORY_RE = re.compile(
    r"^\s*(?:US|USA)\s*\|\s*(?:MLB|NHL|NBA|WNBA|NFL|MLS|NCAAF|NCAAB|NCAA)"
    r"(?:\s*(?:GAMES?|TEAMS?|EVENTS?))?\s*$",
    re.IGNORECASE,
)
LEAGUE_SLOT_PREFIX_RE = re.compile(
    r"^\s*(?:MLB|NHL|NBA|WNBA|NFL|MLS)\s*(?:GAME\s*)?(?:(?:[:|#-]?\s*)\d+\b|\|)",
    re.IGNORECASE,
)
DYNAMIC_PANEL_SLOT_RE = re.compile(
    r"^(?:MLB(?:GAME)?|NHL(?:BACKUP)?|NBA|WNBA|NFL|NCAAF|NCAAB|NCAA)\s*0*\d+(?:\.V\d*)?$",
    re.IGNORECASE,
)
BARE_LEAGUE_EVENT_SLOT_RE = re.compile(
    r"^\s*[:|#-]*\s*(?:US\s+)?(?:MLB|NHL|NBA|WNBA|NFL|MLS|NCAAF|NCAAB|NCAA)\s*"
    r"(?:GAME\s*)?(?:[:|#-]?\s*)\d{1,3}\b",
    re.IGNORECASE,
)
LATIN_MLB_EI_RE = re.compile(r"\bMLB\s+EI\s*\d+\b", re.IGNORECASE)

_IS_DYNAMIC_EVENT_CHANNEL_V4 = is_dynamic_event_channel


def is_dynamic_event_channel(row: dict[str, Any]) -> tuple[bool, str]:
    category = str(row.get("category_name", ""))
    channel = clean_provider_variant(row.get("channel_name", ""))
    stripped = strip_routing_prefix(channel)

    if BARE_LEAGUE_EVENT_SLOT_RE.search(stripped):
        # Permanent services such as NFL Network and NBA TV are explicitly
        # excluded by the v4 linear-sports guard.
        if not LINEAR_SPORTS_EXCLUSION_RE.search(stripped):
            return True, "numbered league event slot whose visible game name can change"

    if re.fullmatch(r"\s*[:|#-]*\s*(?:US\s+)?WNBA\s*\d+\s*:?.*", stripped, re.IGNORECASE):
        return True, "WNBA numbered event slot"

    return _IS_DYNAMIC_EVENT_CHANNEL_V4(row)


_SPECIAL_RULE_V4 = special_rule


def special_rule(row: dict[str, Any]) -> dict[str, str] | None:
    category = str(row.get("category_name", ""))
    channel = str(row.get("channel_name", ""))

    # These are Mexican/Latin MLB Extra Innings playlist slots. The enabled
    # sources do not provide a one-to-one schedule for slot 1, 2, 3, etc.; a
    # random MLB team guide is worse than no guide.
    if re.search(r"\b(?:Latin|MX|Mexico)\b", category, re.IGNORECASE) and LATIN_MLB_EI_RE.search(channel):
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": "Latin/MX MLB Extra Innings numbered slot has no one-to-one enabled EPGShare guide",
        }

    return _SPECIAL_RULE_V4(row)


# -----------------------------------------------------------------------------
# Source-backed direct aliases
# -----------------------------------------------------------------------------
V5_DIRECT_RULES: list[dict[str, Any]] = [
    # US sports/network names that should not be left to fuzzy matching.
    {"region": "US", "pattern": _r(r"^nfl network(?: usa)?$"), "ids": ["NFL.Network.HD.us2"], "label": "NFL Network"},
    {"region": "US", "pattern": _r(r"^espn news$|^espnews$"), "ids": ["ESPNEWS.HD.us2"], "label": "ESPNews"},
    {"region": "US", "pattern": _r(r"^(?:espn )?acc(?: network)?$"), "ids": ["ACC.Network.us2"], "label": "ACC Network"},
    {"region": "US", "pattern": _r(r"^mlb strike ?zone$"), "ids": ["MLB.Network.Strike.Zone.HD.us2"], "label": "MLB Strike Zone"},
    {"region": "US", "pattern": _r(r"^willow cricket extra$|^willow extra$"), "ids": ["Willow.Xtra.us2"], "label": "Willow Xtra"},

    # Common US linear channels where EPGShare adds/removes Channel/Network.
    {"region": "US", "pattern": _r(r"^smithsonian(?: channel)?$"), "ids": ["Smithsonian.HD.Network.us2"], "label": "Smithsonian Channel"},
    {"region": "US", "pattern": _r(r"^heroes and icons$"), "ids": ["Heroes.and.Icons.Network.SD.us2"], "label": "Heroes & Icons"},
    {"region": "US", "pattern": _r(r"^start tv$"), "ids": ["Start.TV.Network.us2"], "label": "Start TV"},
    {"region": "US", "pattern": _r(r"^one america news$"), "ids": ["One.America.News.Network.HD.us2"], "label": "One America News"},
    {"region": "US", "pattern": _r(r"^reelz$"), "ids": ["ReelzChannel.HD.us2"], "label": "Reelz"},
    {"region": "US", "pattern": _r(r"^law and crime(?: network)?$"), "ids": ["Law.and.Crime.us2"], "label": "Law & Crime"},
    {"region": "US", "pattern": _r(r"^get(?: tv)?$"), "ids": ["get.us2"], "label": "getTV"},
    {"region": "US", "pattern": _r(r"^bounce(?: tv)?$"), "ids": ["Bounce.TV.us2"], "label": "Bounce"},
    {"region": "US", "pattern": _r(r"^pursuit(?: channel)?$"), "ids": ["Pursuit.Channel.us2"], "label": "Pursuit"},
    {"region": "US", "pattern": _r(r"^story(?: tv)?$"), "ids": ["Story.us2"], "label": "Story TV"},
    {"region": "US", "pattern": _r(r"^military history(?: channel)?$"), "ids": ["Military.History.Channel.us2"], "label": "Military History"},
    {"region": "US", "pattern": _r(r"^fyi(?: channel)?$"), "ids": ["FYI.Channel.HD.us2"], "label": "FYI"},

    # More exact US aliases found across the complete supplied review file.
    {"region": "US", "pattern": _r(r"^sportsnet new york$|^sny sportsnet new york$"), "ids": ["SNY.SportsNet.New.York.HD.us2"], "label": "SportsNet New York / SNY"},
    {"region": "US", "pattern": _r(r"^(?:ph )?gma life tv$"), "ids": ["GMA.Life.TV.us2"], "label": "GMA Life TV"},
    {"region": "US", "pattern": _r(r"^(?:ph )?gma pinoy tv$"), "ids": ["GMA.Pinoy.TV.us2"], "label": "GMA Pinoy TV"},
    {"region": "US", "pattern": _r(r"^great america(?:n)? family$"), "ids": ["Great.American.Family.HD.us2"], "label": "Great American Family"},
    {"region": "US", "pattern": _r(r"^great american living$"), "ids": ["Great.American.Faith.and.Living.us2"], "label": "Great American Faith & Living"},
    {"region": "US", "pattern": _r(r"^real america s voice$|^real americas voice$"), "ids": ["Real.Americas.Voice.us2"], "label": "Real America's Voice"},
    {"region": "US", "pattern": _r(r"^cinevault classics?$"), "ids": ["Cinevault.Classics.us2"], "label": "Cinevault Classics"},
    {"region": "US", "pattern": _r(r"^bloomberg television$"), "ids": ["Bloomberg.Business.Television.us2", "Bloomberg.HD.us2"], "label": "Bloomberg Television"},
    {"region": "US", "pattern": _r(r"^fuse music$"), "ids": ["FM.Fuse.Music.us2"], "label": "Fuse Music"},
    {"region": "US", "pattern": _r(r"^screenpix westerns?$"), "ids": ["ScreenPix.Westerns.us2"], "label": "ScreenPix Westerns"},
    {"region": "US", "pattern": _r(r"^(?:it )?rai italia$"), "ids": ["Rai.Italia.us2"], "label": "Rai Italia"},
    {"region": "US", "pattern": _r(r"^(?:latin )?espn deportes$"), "ids": ["ESPN.Deportes.HD.us2"], "label": "ESPN Deportes"},
    {"region": "US", "pattern": _r(r"^(?:latin )?(?:national geographic|nat geo) mundo$"), "ids": ["Nat.Geo.Mundo.us2"], "label": "Nat Geo Mundo"},
    {"region": "US", "pattern": _r(r"^own oprah winfrey network$|^oprah winfrey network$"), "ids": ["Oprah.Winfrey.Network.HD.us2"], "label": "OWN / Oprah Winfrey Network"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?syfy west$"), "ids": ["Syfy.HD.(Pacific).us2"], "label": "Syfy West/Pacific"},
    {"region": "US", "pattern": _r(r"^news 12 plus new jersey$|^news 12 new jersey$"), "ids": ["News.12.New.Jersey.us2"], "label": "News 12 New Jersey"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?new england cable news(?: necn)?$"), "ids": ["NECN.New.England.Cable.News.us2"], "label": "NECN"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?american crimes$"), "ids": ["American.Crimes.us2"], "label": "American Crimes"},
    {"region": "US", "pattern": _r(r"^spectrum bay news 9$"), "ids": ["Spectrum.Bay.News.9.Tampa.us2"], "label": "Spectrum Bay News 9"},

    # UK direct spelling/format aliases.
    {"region": "UK", "pattern": _r(r"^now 90s and 00s$|^now 90s 00s$"), "ids": ["NOW.90s00s.uk"], "label": "NOW 90s & 00s"},
    {"region": "UK", "pattern": _r(r"^food network plus 1$"), "ids": ["Food.Netwrk+1.uk"], "label": "Food Network +1"},

    # Canadian direct aliases that are not regional CTV/CTV2 guesses.
    {"region": "CA", "pattern": _r(r"^canal vie$"), "ids": ["Le.Canal.Vie.ca2"], "label": "Canal Vie"},
    {"region": "CA", "pattern": _r(r"^cable pulse 24$|^cp 24$"), "ids": ["Cable.Pulse.24.(CP24).HD.ca2"], "label": "CP24"},
    {"region": "CA", "pattern": _r(r"^ici rdi$"), "ids": ["ICI.RDI.HD.ca2"], "label": "ICI RDI"},
    {"region": "CA", "pattern": _r(r"^ici tele$"), "ids": ["ICI.Tele.ca2"], "label": "ICI Tele"},

    # India news and regional naming differences found in the supplied review.
    {"region": "IN", "pattern": _r(r"^zee bharat(?: news)?$"), "ids": ["ZEE.BHARAT.in"], "label": "Zee Bharat"},
    {"region": "IN", "pattern": _r(r"^r bharat$|^republic bharat$"), "ids": ["RS.Bharat.in", "REPUBLIC.BHARAT.in"], "label": "R Bharat / Republic Bharat"},
    {"region": "IN", "pattern": _r(r"^ndtv profit(?: prime)?$"), "ids": ["NDTV.Profit.in"], "label": "NDTV Profit"},
    {"region": "IN", "pattern": _r(r"^news 18 jammu kashmir ladakh himachal(?: haryana)?$"), "ids": ["NEWS18.JAMMU.KASHMIR.LADAKH.HIMACHAL.HARYANA.in"], "label": "News18 Jammu Kashmir Ladakh Himachal"},
    {"region": "IN", "pattern": _r(r"^news 18 madhya pradesh chhattisgarh$"), "ids": ["NEWS18.MP.CHHATTISGARH.in", "News.State.MPCG..in"], "label": "News18 MP/CG"},
    {"region": "IN", "pattern": _r(r"^(?:india )?news mp ch$|^(?:india )?news madhya pradesh chhattisgarh$"), "ids": ["India.News.MP.in", "MP.News.in"], "label": "India News MP/Chhattisgarh"},
    {"region": "IN", "pattern": _r(r"^taaza tv(?: news)?$"), "ids": ["Taaza.TV.in"], "label": "Taaza TV"},
    {"region": "IN", "pattern": _r(r"^ptc punjabi gold$"), "ids": ["PTC.PUNJABI.GOLD.in"], "label": "PTC Punjabi Gold"},

    # Additional obvious rows from the complete supplied review CSV.
    {"region": "IN", "pattern": _r(r"^aaj tak$"), "ids": ["AAJ.TAK.in"], "label": "Aaj Tak"},
    {"region": "IN", "pattern": _r(r"^bhojpuri cinema$"), "ids": ["BHOJPURI.CINEMA.in"], "label": "Bhojpuri Cinema"},
    {"region": "IN", "pattern": _r(r"^dangal$"), "ids": ["DANGAL.in"], "label": "Dangal"},
    {"region": "IN", "pattern": _r(r"^zee big magic$|^big magic$"), "ids": ["BIG.MAGIC.in"], "label": "Big Magic"},
    {"region": "IN", "pattern": _r(r"^divya tv(?: devotional)?$"), "ids": ["Divya.TV.in"], "label": "Divya TV"},
    {"region": "IN", "pattern": _r(r"^polimer news$"), "ids": ["POLIMER.NEWS.in"], "label": "Polimer News"},
    {"region": "IN", "pattern": _r(r"^sonic nick$|^sonic nickelodeon$"), "ids": ["SONIC.NICKELODEON.in"], "label": "Sonic Nickelodeon"},
    {"region": "IN", "pattern": _r(r"^news 18 tamil nadu$"), "ids": ["NEWS.18.TAMILNADU.in"], "label": "News18 Tamil Nadu"},
    {"region": "IN", "pattern": _r(r"^news 18 odisha$"), "ids": ["NEWS18.ORIYA.in"], "label": "News18 Odisha"},
    {"region": "IN", "pattern": _r(r"^national geo(?:graphic)?$|^nat ?geo$"), "ids": ["NATIONAL.GEOGRAPHIC.HD.in", "NATIONAL.GEOGRAPHIC.in"], "label": "National Geographic India"},
    {"region": "IN", "pattern": _r(r"^nat ?geo wild$|^national geographic wild$"), "ids": ["NAT.GEO.WILD.HD.in", "NAT.GEO.WILD.in"], "label": "Nat Geo Wild India"},
    {"region": "IN", "pattern": _r(r"^shemaroo marathi bana$"), "ids": ["Shemaroo.MarathiBana.in"], "label": "Shemaroo MarathiBana"},
    {"region": "IN", "pattern": _r(r"^aakash aath$"), "ids": ["AAKASH.AATH.in"], "label": "Aakash Aath"},
    {"region": "IN", "pattern": _r(r"^pravah pictures?$"), "ids": ["PRAVAH.PICTURE.in"], "label": "Pravah Picture"},
    {"region": "IN", "pattern": _r(r"^cnbc bajar(?: gujarat)?$"), "ids": ["CNBC.BAJAR.in"], "label": "CNBC Bajar"},
    {"region": "IN", "pattern": _r(r"^bflix(?: movies)?$"), "ids": ["Bflix.Movies.in"], "label": "Bflix Movies"},
    {"region": "IN", "pattern": _r(r"^wow cinema(?: one)?$"), "ids": ["Wow.Cinema.One.in"], "label": "Wow Cinema"},
    {"region": "IN", "pattern": _r(r"^aaj tak$"), "ids": ["AAJ.TAK.in"], "label": "Aaj Tak"},
    {"region": "IN", "pattern": _r(r"^chardikala times?$"), "ids": ["Chardikala.Time.TV.in"], "label": "Chardikala Time TV"},
    {"region": "IN", "pattern": _r(r"^tarang(?: tv)?$"), "ids": ["TARANG.in"], "label": "Tarang TV"},
    {"region": "IN", "pattern": _r(r"^protidin time$"), "ids": ["PRATIDIN.TIME.in"], "label": "Protidin Time"},
    {"region": "IN", "pattern": _r(r"^ntv(?: news)?$"), "ids": ["NTV.in"], "label": "NTV"},
    {"region": "IN", "pattern": _r(r"^r plus(?: news)?$"), "ids": ["R.Plus.in"], "label": "R Plus"},
    {"region": "IN", "pattern": _r(r"^ishara(?: tv)?$"), "ids": ["Ishara.TV.in"], "label": "Ishara TV"},
    {"region": "IN", "pattern": _r(r"^krishna vani(?: devotional)?$"), "ids": ["Krishna.Vani.in"], "label": "Krishna Vani"},
    {"region": "IN", "pattern": _r(r"^ishwar tv(?: devotional)?$"), "ids": ["Ishwar.TV.in"], "label": "Ishwar TV"},
    {"region": "IN", "pattern": _r(r"^evergreen classic(?:s)? active$"), "ids": ["EVERGREEN.CLASSICS.ACTIVE.in"], "label": "Evergreen Classics Active"},
    {"region": "IN", "pattern": _r(r"^bhakti tv active$|^bhakti active$"), "ids": ["BHAKTI.ACTIVE.in"], "label": "Bhakti Active"},
    {"region": "IN", "pattern": _r(r"^dd podhigai$"), "ids": ["DD5.Podhigai.in"], "label": "DD Podhigai"},
    {"region": "IN", "pattern": _r(r"^zee punjab haryana himachal(?: pradesh)?$"), "ids": ["ZEE.PUNJAB.HARYANA.HIMACHAL.in"], "label": "Zee Punjab Haryana Himachal"},
]
DIRECT_RULES = V5_DIRECT_RULES + DIRECT_RULES


# -----------------------------------------------------------------------------
# Exact channel-identity variants and official league-team feeds
# -----------------------------------------------------------------------------
TEAM_LEAGUES = ("MLB", "NBA", "NFL", "NHL", "WNBA")
TEAM_LEAGUE_RE = re.compile(r"\b(MLB|NBA|NFL|NHL|WNBA)\b", re.IGNORECASE)
TEAM_CODE_AFTER_LEAGUE_RE = re.compile(
    r"\b(MLB|NBA|NFL|NHL|WNBA)\s*\([A-Z0-9]{2,5}\)\s*",
    re.IGNORECASE,
)
TEAM_CODE_AT_END_RE = re.compile(
    r"\s*\([A-Z0-9]{2,5}\)\s*(?=(?:SD|HD|FHD|UHD|4K|8K)?\s*$)",
)


def _initials_for_channel(value: object) -> str:
    text = canonical_text(strip_routing_prefix(value))
    words = [
        word for word in text.split()
        if word not in TECH_WORDS and word not in REGION_WORDS and word not in {"the", "and", "of"}
    ]
    return "".join(word[0] for word in words if word).upper()


def channel_identity_variants_v5(row: dict[str, Any]) -> list[str]:
    raw = str(row.get("channel_name", ""))
    variants: list[str] = []

    def add(value: object) -> None:
        identity = identity_name(value)
        if identity and identity not in variants:
            variants.append(identity)

    add(raw)
    cleaned = clean_provider_variant(raw)
    add(cleaned)

    # Team abbreviations may appear immediately after the league or at the end.
    team_cleaned = TEAM_CODE_AFTER_LEAGUE_RE.sub(r"\1 ", cleaned)
    team_cleaned = TEAM_CODE_AT_END_RE.sub("", team_cleaned)
    add(team_cleaned)

    # Remove a trailing acronym only when it is actually the initials of the
    # visible channel name. This safely handles AHC/GSN but preserves station
    # call signs such as (WISH).
    acronym_match = re.search(r"\s*\(([A-Z0-9]{2,6})\)\s*$", cleaned)
    if acronym_match:
        code = acronym_match.group(1).upper()
        base = cleaned[:acronym_match.start()].strip()
        if code == _initials_for_channel(base):
            add(base)

    # Most EPGShare US/Canadian IDs omit the word East because East is the
    # default national feed.  Remove East only; West/Pacific is never removed.
    for value in list(variants):
        if re.search(r"(?:^| )east(?:ern)?$", value):
            trimmed = re.sub(r"(?:^| )east(?:ern)?$", "", value).strip()
            if trimmed and trimmed not in variants:
                variants.append(trimmed)

    return variants


def exact_alias_variant_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    families = FAMILIES.get(region, FAMILIES.get("ALL", {})) if region != "ALL" else FAMILIES.get("ALL", {})
    matches: dict[str, list[dict[str, str]]] = {}
    for query in channel_identity_variants_v5(row):
        group = [
            item for item in families.get(query, [])
            if direction_safe(row, item) and numbers_safe(row, item) and languages_safe(row, item)
        ]
        if group:
            matches[query] = group
    if len(matches) != 1:
        return None
    query, group = next(iter(matches.items()))
    if region == "ALL" and len({item["region"] for item in group}) != 1:
        return None
    if not distinctive_tokens(query) and len(query.split()) <= 1:
        return None
    primary = choose_preferred(group, row)
    alternates = [item for item in sorted(group, key=lambda item: candidate_preference(item, row)) if item is not primary]
    alternate = alternates[0] if alternates else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": "Exact channel identity after removing only verified provider/team tags",
    }


V5_TRAILING_DESCRIPTOR_WORDS = {"channel", "network", "television"}
V5_SAFE_SINGLE_BRANDS = {
    "smithsonian", "reelz", "fyi", "get", "bounce", "pursuit", "story",
}
V5_BRAND_INDEX: dict[str, dict[tuple[str, ...], list[dict[str, str]]]] = {}


def brand_key_v5(value: object, *, is_epg: bool = False) -> tuple[str, ...]:
    tokens = identity_name(value, is_epg=is_epg).split()
    if tokens and tokens[-1] in {"east", "eastern"}:
        tokens = tokens[:-1]
    while tokens and tokens[-1] in V5_TRAILING_DESCRIPTOR_WORDS:
        tokens = tokens[:-1]
    singular = {"classics": "classic", "pictures": "picture"}
    tokens = [singular.get(token, token) for token in tokens]
    return tuple(tokens)


def safe_brand_descriptor_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region == "ALL":
        return None
    key = brand_key_v5(row.get("channel_name", ""))
    if not key:
        return None
    distinctive = [
        token for token in key
        if token not in GENERIC_WORDS and token not in CONTENT_WORDS and token not in LANGUAGE_WORDS
    ]
    if len(distinctive) < 2 and " ".join(key) not in V5_SAFE_SINGLE_BRANDS:
        return None
    group = [
        item for item in V5_BRAND_INDEX.get(region, {}).get(key, [])
        if direction_safe(row, item) and numbers_safe(row, item) and languages_safe(row, item)
    ]
    if not group:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in group:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    family_group = next(iter(families.values()))
    primary = choose_preferred(family_group, row)
    alternates = [item for item in sorted(family_group, key=lambda item: candidate_preference(item, row)) if item is not primary]
    alternate = alternates[0] if alternates else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": "Exact channel brand after removing only East/default and trailing Channel/Network descriptors",
    }


def _league_team_tokens(value: object, league: str, *, is_epg: bool = False) -> tuple[str, ...]:
    text = identity_name(value, is_epg=is_epg)
    tokens = text.split()
    league_lower = league.lower()
    if league_lower not in tokens:
        return tuple()
    index = tokens.index(league_lower)
    team = tokens[index + 1:]
    if league == "NBA" and team[:2] == ["la", "clippers"]:
        team = ["los", "angeles", "clippers"] + team[2:]
    return tuple(word for word in team if word not in {"team", "teams"})


def league_team_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region != "US":
        return None
    context = f"{row.get('category_name', '')} {row.get('channel_name', '')}"
    league_match = TEAM_LEAGUE_RE.search(context)
    if not league_match:
        return None
    league = league_match.group(1).upper()
    dynamic, _reason = is_dynamic_event_channel(row)
    if dynamic:
        return None

    raw = clean_provider_variant(row.get("channel_name", ""))
    raw = TEAM_CODE_AFTER_LEAGUE_RE.sub(r"\1 ", raw)
    raw = TEAM_CODE_AT_END_RE.sub("", raw)
    query_team = _league_team_tokens(raw, league)
    if not query_team or query_team in {("network",), ("tv",), ("strike", "zone"), ("big", "inning")}:
        return None
    query_set = set(query_team)

    matched_families: list[tuple[int, str, list[dict[str, str]]]] = []
    for family, group in FAMILIES.get("US", {}).items():
        if not family.startswith(league.lower() + " "):
            continue
        league_group = [item for item in group if item["epg_id"].upper().startswith(league + "-")]
        if not league_group:
            continue
        candidate_team = _league_team_tokens(league_group[0]["epg_id"], league, is_epg=True)
        if not candidate_team:
            continue
        candidate_set = set(candidate_team)
        score = 0
        if candidate_team == query_team:
            score = 3
        elif candidate_set.issubset(query_set) and candidate_set:
            score = 2
        elif query_set.issubset(candidate_set) and query_set:
            score = 1
        if score:
            plausible = [item for item in league_group if direction_safe(row, item) and numbers_safe(row, item)]
            if plausible:
                matched_families.append((score, family, plausible))

    if not matched_families:
        return None
    matched_families.sort(key=lambda item: (-item[0], item[1]))
    best_score = matched_families[0][0]
    best = [item for item in matched_families if item[0] == best_score]
    if len(best) != 1:
        return None
    _score, _family, group = best[0]
    primary = choose_preferred(group, row)
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0, "second_epg_id": "", "second_epg_feed": "",
        "second_score": "", "score_margin": 100.0,
        "reason": f"Exact/unique {league} team identity matched to the official team EPG feed",
    }


# -----------------------------------------------------------------------------
# Regional exact-name helpers
# -----------------------------------------------------------------------------
SPECTRUM_NOISE = {"stva"}
CA_PROVINCE_CODES = {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"}


def _spectrum_key_v5(value: object, *, is_epg: bool = False) -> tuple[str, ...]:
    tokens = identity_name(value, is_epg=is_epg).split()
    tokens = [token for token in tokens if token not in SPECTRUM_NOISE]
    if len(tokens) >= 3 and tokens[:2] == ["spectrum", "news"] and tokens[2] == "1":
        tokens = tokens[:2] + tokens[3:]
    return tuple(tokens)


def spectrum_news_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region != "US":
        return None
    query = _spectrum_key_v5(row.get("channel_name", ""))
    if len(query) < 3 or query[:2] != ("spectrum", "news"):
        return None
    matched: list[dict[str, str]] = []
    for candidate in BY_REGION.get("US", []):
        if not candidate["epg_id"].casefold().startswith("spectrum.news"):
            continue
        if _spectrum_key_v5(candidate["epg_id"], is_epg=True) == query:
            matched.append(candidate)
    if not matched:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in matched:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    primary = choose_preferred(group, row)
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0, "second_epg_id": "", "second_epg_feed": "",
        "second_score": "", "score_margin": 100.0,
        "reason": "Exact Spectrum News market identity after removing only source label 1/STVA",
    }


def _canada_location_tokens_v5(value: object, network: str, *, is_epg: bool = False) -> tuple[str, ...]:
    tokens = identity_name(value, is_epg=is_epg).split()
    if network == "cbc":
        if "cbc" not in tokens:
            return tuple()
        tokens = tokens[tokens.index("cbc") + 1:]
    elif network == "ctv":
        if "ctv" not in tokens:
            return tuple()
        tokens = tokens[tokens.index("ctv") + 1:]
    tokens = [
        token for token in tokens
        if token not in CA_PROVINCE_CODES and token not in {"television", "network", "channel", "mctv"}
        and not (len(token) >= 3 and token.startswith("cb") and token.isalpha())
    ]
    return tuple(tokens)


def canada_local_network_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region != "CA":
        return None
    query_identity = identity_name(row.get("channel_name", ""))
    network = "cbc" if query_identity.startswith("cbc ") else "ctv" if query_identity.startswith("ctv ") else ""
    if not network:
        return None
    query_two = bool(re.search(r"^ctv (?:2|two)\b", query_identity))
    query_location = _canada_location_tokens_v5(row.get("channel_name", ""), network)
    if network == "ctv" and query_location and query_location[0] in {"2", "two"}:
        query_location = query_location[1:]
    if not query_location:
        return None
    matched: list[dict[str, str]] = []
    for candidate in BY_REGION.get("CA", []):
        candidate_identity = identity_name(candidate["epg_id"], is_epg=True)
        if network not in candidate_identity.split():
            continue
        candidate_two = bool(re.search(r"\bctv (?:2|two)\b", candidate_identity))
        if network == "ctv" and candidate_two != query_two:
            continue
        location = _canada_location_tokens_v5(candidate["epg_id"], network, is_epg=True)
        if network == "ctv" and location and location[0] in {"2", "two"}:
            location = location[1:]
        if location == query_location:
            matched.append(candidate)
    if not matched:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in matched:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    primary = choose_preferred(group, row)
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0, "second_epg_id": "", "second_epg_feed": "",
        "second_score": "", "score_margin": 100.0,
        "reason": f"Exact Canadian {network.upper()} market identity; CTV and CTV2 are kept separate",
    }


# -----------------------------------------------------------------------------
# Dead/stale panel ID used only as a name clue, never as programme data
# -----------------------------------------------------------------------------
def panel_id_catalog_match_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    panel_id = str(row.get("panel_epg_id", "")).strip()
    key = compact_key(panel_id, is_epg=True)
    if not key or len(key) < 4 or key in {"none", "null", "test", "covid19"} or key.isdigit():
        return None
    index = ID_KEYS.get(region, ID_KEYS.get("ALL", {})) if region != "ALL" else ID_KEYS.get("ALL", {})
    group = index.get(key, [])
    if not group:
        return None

    query_variants = channel_identity_variants_v5(row)
    safe: list[tuple[float, dict[str, str]]] = []
    query_classes = _content_classes(row.get("channel_name", ""))
    for candidate in group:
        if not direction_safe(row, candidate) or not numbers_safe(row, candidate) or not languages_safe(row, candidate):
            continue
        candidate_classes = _content_classes(candidate["epg_id"], is_epg=True)
        # A visible News/Movie/Sports/Kids/Music label must not disappear merely
        # because the server supplied a short or wrong panel ID.
        if query_classes and not query_classes.issubset(candidate_classes):
            continue
        similarity = max(
            (float(fuzz.WRatio(query, candidate["normalized"])) for query in query_variants),
            default=0.0,
        )
        if similarity >= 90.0:
            safe.append((similarity, candidate))
    if not safe:
        return None

    families: dict[str, list[tuple[float, dict[str, str]]]] = defaultdict(list)
    for item in safe:
        families[item[1]["family_key"]].append(item)
    if len(families) != 1:
        return None
    items = next(iter(families.values()))
    group_candidates = [item[1] for item in items]
    primary = choose_preferred(group_candidates, row)
    best_similarity = max(item[0] for item in items)
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": round(best_similarity, 1),
        "second_epg_id": "", "second_epg_feed": "", "second_score": "",
        "score_margin": 100.0,
        "reason": "Unused panel ID was used only as a verified name clue; programme data comes from EPGShare",
    }


def _smart_v5_prepare_matcher(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> None:
    global V5_BRAND_INDEX
    _smart_v3_prepare_matcher(real_candidates, dummy_ids)
    V5_BRAND_INDEX = {}
    for region, items in list(BY_REGION.items()) + [("ALL", CANDIDATES)]:
        index: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
        for item in items:
            key = brand_key_v5(item["epg_id"], is_epg=True)
            if key:
                index[key].append(item)
        V5_BRAND_INDEX[region] = dict(index)


def _resolve_pre_panel_v5(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    for resolver in (
        lambda r: special_rule(r),
        lambda r: direct_rule(r, region),
        lambda r: fanduel_match(r, region),
        lambda r: league_team_match_v5(r, region),
    ):
        match = resolver(row)
        if match:
            return match
    return None


def _resolve_post_panel_v5(row: dict[str, Any], region: str, explicit_region: bool) -> dict[str, Any] | None:
    if region == "US":
        call = callsign_match(row)
        if call:
            return call

    if explicit_region and region not in active_regions() and region != "ALL":
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": f"No enabled EPGShare source is configured for {region}; unrelated countries are not searched",
        }

    for resolver in (
        spectrum_news_match_v5,
        canada_local_network_match_v5,
        exact_alias_variant_match_v5,
        safe_brand_descriptor_match_v5,
        panel_id_catalog_match_v5,
        exact_family_match,
        safe_panel_hint,
        language_context_match,
        fuzzy_family_match,
    ):
        match = resolver(row, region)
        if match:
            return match
    return None


def make_matching_report_v5(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Build a fresh mapping report using Smart Rules v5."""
    _smart_v5_prepare_matcher(real_candidates, dummy_ids)
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
        region, explicit_region, _route_reason = detect_route(row)
        record: dict[str, Any] = {
            "server_id": server_id, "stream_id": stream_id, "category_id": category_id,
            "category_name": category_name, "channel_number": channel_number,
            "channel_name": channel_name, "normalized_name": identity_name(channel_name),
            "detected_region": region, "panel_epg_id": panel_epg_id,
            "panel_epg_status": str(panel_info.get("status", "")),
            "panel_usable_programmes": int(panel_info.get("current_or_future_informative_rows", 0) or 0),
            "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
            "action": "", "source": "", "epg_id": "", "epg_feed": "",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "", "reason": "",
        }

        match = _resolve_pre_panel_v5(row, region)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        # A panel schedule is retained only when the panel-quality scan found
        # useful current/future programme titles. Empty/stale panel IDs never
        # become KEEP_PANEL.
        if panel_epg_id and panel_info.get("status") == "USABLE":
            record.update({
                "action": "KEEP_PANEL", "source": "panel", "epg_id": panel_epg_id,
                "epg_feed": "server xmltv.php",
                "reason": "Panel XMLTV contains useful current/future programme information",
            })
            records.append(record)
            continue

        match = _resolve_post_panel_v5(row, region, explicit_region)
        if match:
            _smart_v3_apply_match(record, match)
        else:
            record.update({
                "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
                "reason": _smart_v3_unmatched_reason(region),
            })
        records.append(record)

    return pd.DataFrame(records)


def classify_review_row_v5(row: dict[str, Any]) -> dict[str, Any]:
    """Re-run v5 rules against one old REVIEW row for audit/preview files."""
    region, explicit_region, _route_reason = detect_route(row)
    match = _resolve_pre_panel_v5(row, region)
    if not match:
        match = _resolve_post_panel_v5(row, region, explicit_region)
    if not match:
        match = {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": _smart_v3_unmatched_reason(region),
        }
    return {
        "proposed_action": match.get("action", ""),
        "proposed_source": match.get("source", ""),
        "proposed_epg_id": match.get("epg_id", ""),
        "proposed_epg_feed": match.get("epg_feed", ""),
        "proposed_best_score": match.get("best_score", ""),
        "proposed_second_epg_id": match.get("second_epg_id", ""),
        "proposed_second_epg_feed": match.get("second_epg_feed", ""),
        "proposed_second_score": match.get("second_score", ""),
        "proposed_score_margin": match.get("score_margin", ""),
        "proposed_reason": match.get("reason", ""),
        "proposed_region": region,
    }


def run_smart_rules_v5_self_test(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Run catalog-backed v5 examples before the real server match starts."""
    cases = [
        ("WNBA matchup slot", "US | WNBA", "WNBA 1 Connecticut Sun @ Indiana Fever start:2025-05-31 00:30:00", "COVID19", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("WNBA plain numbered slot", "US | WNBA", "WNBA 7", "US NBA League Pass 9 (F)", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("WNBA leading punctuation slot", "US | WNBA", ":WNBA 03", "", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("WNBA general-sports slot", "US | Sports", "US WNBA 4", "", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("NBA team", "US | NBA", "US NBA (CHI) Chicago Bulls", "", "AUTO_EPGSHARE", "NBA-ChicagoBulls.us"),
        ("MLB team", "US | MLB Teams", "MLB Toronto Blue Jays (TOR) (F)", "", "AUTO_EPGSHARE", "MLB-TorontoBlueJays.us"),
        ("NFL Network", "US | Sports", "NFL NETWORK USA (DZ)", "", "AUTO_EPGSHARE", "NFL.Network.HD.us2"),
        ("MLB Strike Zone", "US | MLB Teams", "US MLB Strike Zone", "", "AUTO_EPGSHARE", "MLB.Network.Strike.Zone.HD.us2"),
        ("Latin MLB EI no source", "Latin", "(MX) (IZ) MLB EI 4", "", "UNMATCHED", ""),
        ("Zee Bharat", "IN | News", "In -News | Ts Zee Bharat News", "", "AUTO_EPGSHARE", "ZEE.BHARAT.in"),
        ("R Bharat", "IN | News", "IN-NEWS | R Bharat", "", "AUTO_EPGSHARE", "RS.Bharat.in"),
        ("NDTV Profit", "IN | News", "IN-NEWS | NDTV Profit Prime", "ndtvprofit-prime.in", "AUTO_EPGSHARE", "NDTV.Profit.in"),
        ("News18 Jammu", "IN | News", "IN-NEWS | News18 Jammu Kashmir Ladakh Himachal", "news18upuk.in", "AUTO_EPGSHARE", "NEWS18.JAMMU.KASHMIR.LADAKH.HIMACHAL.HARYANA.in"),
        ("News18 MPCG", "IN | News", "IN-NEWS | News 18 MP/CG", "news18mpchhattisgarh.in", "AUTO_EPGSHARE", "NEWS18.MP.CHHATTISGARH.in"),
        ("India News MP", "IN | News", "IN-NEWS | India News MP Ch", "indianewsmpch.in", "AUTO_EPGSHARE", "India.News.MP.in"),
        ("Taaza TV", "IN | News", "IN-NEWS | Taaza TV News", "24taas.in", "AUTO_EPGSHARE", "Taaza.TV.in"),
        ("PTC Punjabi Gold", "INR | Punjabi", "IN-PUNJ | PTC Punjabi GOld", "ptcpunjabi.in", "AUTO_EPGSHARE", "PTC.PUNJABI.GOLD.in"),
        ("Polimer second-candidate exact", "INR | Tamil", "IN-MY-News | TS Polimer News", "polimernews.in", "AUTO_EPGSHARE", "POLIMER.NEWS.in"),
        ("Sonic Nickelodeon", "INR | Tamil", "IN-TM | Sonic Nick", "sonicnickelodeon.in", "AUTO_EPGSHARE", "SONIC.NICKELODEON.in"),
        ("Spectrum exact market", "US | Spectrum", "(SP2) US Spectrum News San Antonio", "", "AUTO_EPGSHARE", "Spectrum.News.1.-.San.Antonio.us2"),
        ("CBC city", "CA | General", "CA CBC Calgary AB", "", "AUTO_EPGSHARE", "CBC.Calgary.ca2"),
        ("CTV not CTV2", "CA | General", "CA CTV North Bay HD", "", "AUTO_EPGSHARE", "MCTV/CTV.North.Bay.ca2"),
        ("UK Food plus one", "UK | Entertainment", "UK-Food Network +1 SD*", "foodnetworkplus1.uk", "AUTO_EPGSHARE", "Food.Netwrk+1.uk"),
        ("Great American Family", "US | Entertainment", "US Great America Family", "", "AUTO_EPGSHARE", "Great.American.Family.HD.us2"),
        ("Default East feed", "US | Entertainment", "US Discovery Channel (East)", "", "AUTO_EPGSHARE", "Discovery.Channel.HD.us2"),
        ("Provider pipe removal", "IN | News", "D2H | AAJ TAK FHD", "aajtakhd.in", "AUTO_EPGSHARE", "AAJ.TAK.in"),
        ("US descriptor alias", "US | Entertainment", "US Heroes & Icons", "", "AUTO_EPGSHARE", "Heroes.and.Icons.Network.SD.us2"),
        ("Indian Bflix", "IN | Movies", "IN | Bflix", "bflixmovies.in", "AUTO_EPGSHARE", "Bflix.Movies.in"),
        ("Indian Tarang", "INR | Odia", "IN-ODIA | Tarang TV", "tarangtv.in", "AUTO_EPGSHARE", "TARANG.in"),
        ("Bally Cincinnati regression", "US | Sports", "US Bally Sports Cincinnati (A)", "", "AUTO_EPGSHARE", "FanDuel.Sports.Ohio.-.Cincinnati.HD.us"),
        ("BBC London regression", "UK | Regional", "UK-BBC One London FHD*", "", "AUTO_EPGSHARE", "BBC.One.Lon.HD.uk"),
        ("CTV2 Toronto regression", "CA | General", "CA CTV2 TORONTO HD (D)", "", "AUTO_EPGSHARE", "CTV.Two.-.Toronto.ca2"),
        ("Adult regression", "XXX | Adults", "+18 | Private", "", "AUTO_DUMMY", "Adult.Programming.Dummy.us"),
    ]

    channels: list[dict[str, str]] = []
    categories: dict[str, str] = {}
    for index, (_label, category, channel, panel_id, _action, _epg_id) in enumerate(cases, start=1):
        cid = str(index)
        categories[cid] = category
        channels.append({"stream_id": str(index), "category_id": cid, "name": channel, "epg_channel_id": panel_id})

    report = make_matching_report_v5("self_test", channels, categories, {}, real_candidates, dummy_ids)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        label, _category, _channel, _panel, expected_action, expected_id = case
        actual = report.iloc[index]
        passed = actual["action"] == expected_action and actual["epg_id"].casefold() == expected_id.casefold()
        rows.append({
            "test": label, "passed": passed,
            "expected_action": expected_action, "actual_action": actual["action"],
            "expected_epg_id": expected_id, "actual_epg_id": actual["epg_id"],
            "reason": actual["reason"],
        })
    result = pd.DataFrame(rows)
    failures = result.loc[~result["passed"]]
    if not failures.empty:
        display(failures)
        raise RuntimeError("Smart Rules v5 self-test failed. Stop before matching the server.")
    return result


make_matching_report_v5.smart_rules_version = SMART_RULES_VERSION
make_matching_report_v5.matcher_build_id = MATCHER_BUILD_ID

# Remove the generic alias so an old runtime cannot silently call a prior matcher.
globals().pop("make_matching_report", None)
# Smart Rules v5 base loaded; the v6 active layer is defined immediately below.

# =============================================================================
# Smart Rules v6 active layer
# =============================================================================
# v6 keeps every v5 safety rule and adds:
# - UTF-8-safe EPGShare catalog decoding (prevents mojibake channel IDs),
# - UFC/Triller and Peacock sports event-bank rules,
# - requested exact aliases for MLB Strike Zone, Bon Appetit, NHL Alternate,
#   BBC Two Northern Ireland, Sony Sports Ten 3/4, and Haryana Beats,
# - stronger exact identity matching that can choose a clearly exact second
#   candidate while preserving number, language, East/West, Plus/Extra and HQ,
# - safer US call-sign extraction for wrappers containing more than one call sign,
# - a generic Music Choice dummy for provider radio-only rows.

SMART_RULES_VERSION = "6.0"
MATCHER_BUILD_ID = "SKYTV-SMART-RULES-6.0-2026-07-15"

# -----------------------------------------------------------------------------
# UTF-8 and text repair
# -----------------------------------------------------------------------------
_V5_CANONICAL_TEXT = canonical_text


def repair_utf8_mojibake(value: object) -> str:
    """Repair common UTF-8 text that was accidentally decoded as Latin-1/CP1252."""
    text = str(value or "")
    markers = ("Ã", "Â", "â", "ð", "�")
    for _ in range(2):
        if not any(marker in text for marker in markers):
            break
        candidates: list[str] = []
        for encoding in ("latin-1", "cp1252"):
            try:
                candidates.append(text.encode(encoding).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        if not candidates:
            break
        repaired = min(
            candidates,
            key=lambda item: sum(item.count(marker) for marker in markers),
        )
        if repaired == text:
            break
        old_score = sum(text.count(marker) for marker in markers)
        new_score = sum(repaired.count(marker) for marker in markers)
        if new_score >= old_score:
            break
        text = repaired
    # Preserve legitimate Unicode punctuation in the EPG ID itself. The
    # comparison normalizer removes punctuation separately, but the final ID
    # must remain byte-for-byte compatible with the XMLTV channel ID.
    return text.replace(" ", " ")


def canonical_text(value: object) -> str:
    """Use v5 normalization after repairing source-catalog text encoding."""
    text = _V5_CANONICAL_TEXT(repair_utf8_mojibake(value))
    # Mixed-case provider spelling STRIKEzone is split by the generic camel
    # case normalizer as "strik ezone". Repair that exact sports brand.
    text = re.sub(r"\bstrik\s+ezone\b|\bstrikezone\b", "strike zone", text)
    return re.sub(r"\s+", " ", text).strip()


# Replace the catalog downloader so EPG IDs are decoded exactly as UTF-8. The
# IDs written into the mapping must exactly match the channel IDs in XMLTV.
def download_epgshare_catalog(
    session: requests.Session,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    real_candidates: list[dict[str, str]] = []
    dummy_ids: dict[str, str] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for feed_name, feed in EPGSHARE_FEEDS.items():
        if not bool(feed.get("use_for_matching", True)):
            print(f"{feed_name}: available for builds, skipped during matching")
            continue
        if not str(feed.get("txt_url", "")).strip():
            print(f"Warning: {feed_name} has no text catalog; skipped during matching.")
            continue
        try:
            response = session.get(feed["txt_url"], timeout=(20, 180))
            if response.status_code != 200:
                print(f"Warning: {feed_name} returned HTTP {response.status_code}; continuing.")
                continue
            # EPGShare text catalogs are UTF-8. requests may otherwise assume
            # Latin-1 for text/plain and turn Appétit into AppÃ©tit.
            catalog_text = response.content.decode("utf-8-sig", errors="replace")
            ids = parse_epgshare_ids(catalog_text)
        except (requests.RequestException, UnicodeError):
            print(f"Warning: could not download {feed_name}; continuing.")
            continue

        print(f"{feed_name}: {len(ids):,} EPG IDs")
        if feed["kind"] == "dummy":
            for epg_id in ids:
                dummy_ids[epg_id.casefold()] = epg_id
            continue

        for epg_id in ids:
            pair = (feed_name, epg_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            display_name = epg_id_to_name(epg_id)
            real_candidates.append({
                "epg_id": epg_id,
                "feed": feed_name,
                "region": feed["region"],
                "display_name": display_name,
                "normalized": normalize_name(display_name),
            })

    if not real_candidates:
        raise RuntimeError("No real EPGShare IDs could be downloaded.")
    return real_candidates, dummy_ids


# -----------------------------------------------------------------------------
# Event-bank and non-linear channel rules
# -----------------------------------------------------------------------------
_IS_DYNAMIC_EVENT_CHANNEL_V5 = is_dynamic_event_channel

UFC_TRILLER_CATEGORY_RE = re.compile(
    r"(?:^|\|)\s*(?:UFC|TRILLER(?:\s*TV)?|FITE)(?:\s*/\s*(?:UFC|TRILLER(?:\s*TV)?|FITE))*\s*$",
    re.IGNORECASE,
)
UFC_NUMBERED_SLOT_RE = re.compile(
    r"^\s*(?:US\s*[|:-]\s*)?(?:UFC|TRILLER|FITE)\s*(?:TV\s*)?\d{1,3}\s*:?.*$",
    re.IGNORECASE,
)
PEACOCK_SPORTS_CATEGORY_RE = re.compile(
    r"(?:^|\|)\s*PEACOCK(?:\s+SPORTS?)?\s*$",
    re.IGNORECASE,
)
PEACOCK_EVENT_SLOT_RE = re.compile(
    r"\bPEACOCK\b.*(?:\b(?:GOLF|WWE|SPORTS?)\s+PASS\b|\b\d{1,3}\b)",
    re.IGNORECASE,
)
MUSIC_CHOICE_RADIO_RE = re.compile(
    r"^\s*(?:US\s*(?:[|:-]\s*)?)?(?:MC|MUSIC\s+CHOICE)\s+RADIO\s*[|:-]",
    re.IGNORECASE,
)
CANADA_EVENT_PACK_RE = re.compile(
    r"\b(?:NHL\s+CENTRE\s+ICE|NBA\s+LEAGUE\s+PASS|MLB\s+EXTRA\s+INNINGS|"
    r"SPORTS?\s+PACK\s*\d+|SUPER\s+SPORTS?\s+(?:CH|CHANNEL)\s*\d+)\b",
    re.IGNORECASE,
)


def is_dynamic_event_channel(row: dict[str, Any]) -> tuple[bool, str]:
    category = str(row.get("category_name", ""))
    channel = clean_provider_variant(row.get("channel_name", ""))
    context = f"{category} {channel}"

    if UFC_TRILLER_CATEGORY_RE.search(category) and (
        UFC_NUMBERED_SLOT_RE.search(channel)
        or MATCHUP_MARKER_RE.search(channel)
        or DATE_TIME_MARKER_RE.search(channel)
    ):
        return True, "UFC/Triller/FITE numbered or game-specific event slot"

    if PEACOCK_SPORTS_CATEGORY_RE.search(category) and PEACOCK_EVENT_SLOT_RE.search(context):
        return True, "Peacock sports pass/numbered slot whose event changes"

    return _IS_DYNAMIC_EVENT_CHANNEL_V5(row)


_SPECIAL_RULE_V5 = special_rule


def special_rule(row: dict[str, Any]) -> dict[str, str] | None:
    category = str(row.get("category_name", ""))
    channel = str(row.get("channel_name", ""))

    # Music Choice radio streams do not carry a normal linear TV schedule. A
    # generic music dummy is more useful and safer than a fuzzy TV-channel hit.
    if re.search(r"\bUS\s*\|\s*MUSIC\b", category, re.IGNORECASE) and MUSIC_CHOICE_RADIO_RE.search(channel):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("Music.Choice.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Music Choice radio-only row mapped to the Music Choice dummy guide",
        }

    # Canadian Centre Ice/League Pass/sports-pack rows are rotating event
    # slots, not permanent linear channels. Their visible event changes.
    if re.search(r"\bCA\s*\|\s*SPORTS\b", category, re.IGNORECASE) and CANADA_EVENT_PACK_RE.search(channel):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("PPV.EVENTS.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Canadian sports-package/event slot mapped to the PPV events dummy guide",
        }

    return _SPECIAL_RULE_V5(row)


# -----------------------------------------------------------------------------
# Requested exact aliases and related safe naming variations
# -----------------------------------------------------------------------------
V6_DIRECT_RULES: list[dict[str, Any]] = [
    # Requested US rules.
    {
        "region": "US",
        "pattern": _r(r"^mlb(?: network)? strike zone$"),
        "ids": ["MLB.Network.Strike.Zone.HD.us2"],
        "label": "MLB Network Strike Zone",
    },
    {
        "region": "US",
        "pattern": _r(r"^bon appetit(?: tv)?$"),
        "ids": ["Bon.Appétit.TV.us2", "Bon.AppÃ©tit.TV.us2"],
        "label": "Bon Appetit TV",
    },
    {
        "region": "US",
        "pattern": _r(r"^nhl(?: network)? alternate$|^nhl alternate(?: feed)?$"),
        "ids": ["NHL.Network.HD.us2"],
        "label": "NHL Network alternate provider feed",
    },

    # Requested UK regional rule.
    {
        "region": "UK",
        "pattern": _r(r"^bbc 2 (?:northern ireland|ni)$"),
        "ids": ["BBC.Two.NI.HD.uk"],
        "label": "BBC Two Northern Ireland",
    },

    # Requested India sports/music rules. canonical_text converts Ten to 10.
    {
        "region": "IN",
        "pattern": _r(r"^sony(?: sports)? 10 3$"),
        "ids": ["Sony.Sports.Ten.3.HD.in"],
        "label": "Sony Sports Ten 3",
    },
    {
        "region": "IN",
        "pattern": _r(r"^sony(?: sports)? 10 4$"),
        "ids": ["Sony.Sports.Ten.4.HD.in"],
        "label": "Sony Sports Ten 4",
    },
    {
        "region": "IN",
        "pattern": _r(r"^(?:haryanvi|haryana) beats$"),
        "ids": ["Haryana.Beats.in"],
        "label": "Haryana/Haryanvi Beats",
    },

    # Additional exact identities found repeatedly in the latest review file.
    {
        "region": "US",
        "pattern": _r(r"^nat(?:ional)? geo(?:graphic)? wild(?: east)?$"),
        "ids": ["National.Geographic.Wild.HD.us2"],
        "label": "National Geographic Wild",
    },
    {
        "region": "US",
        "pattern": _r(r"^tastmade travel$|^tastemade travel$"),
        "ids": ["Tastemade.Travel.us2"],
        "label": "Tastemade Travel spelling",
    },
    {
        "region": "IN",
        "pattern": _r(r"^saga music (?:haryanvi|haryana)$"),
        "ids": ["Saga.Music.Haryanvi.in"],
        "label": "Saga Music Haryanvi",
    },
    {
        "region": "IN",
        "pattern": _r(r"^shee?maroo marathi bana$"),
        "ids": ["Shemaroo.MarathiBana.in"],
        "label": "Shemaroo Marathi Bana spelling",
    },
    {
        "region": "CA",
        "pattern": _r(r"^vision$"),
        "ids": ["Vision.TV.HD.ca2", "Vision.TV.ca2"],
        "label": "Vision TV Canada",
    },
    {
        "region": "US",
        "pattern": _r(r"^ion$"),
        "ids": ["ION.Television.HD.us2"],
        "label": "ION Television",
    },
    {
        "region": "US",
        "pattern": _r(r"^ewtn espanol$"),
        "ids": ["EWTN.Español.us2", "EWTN.EspaÃ±ol.us2"],
        "label": "EWTN Español",
    },
    {
        "region": "US",
        "pattern": _r(r"^starz encore espanol$"),
        "ids": ["Starz.Encore.Español.SD.us2", "Starz.Encore.EspaÃ±ol.SD.us2"],
        "label": "Starz Encore Español",
    },

    # 3ABN and Filipino/Latin provider wrappers found repeatedly in review.
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn$"), "ids": ["3ABN.us2"], "label": "3ABN"},
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn dare to dream$"), "ids": ["3ABN.Dare.to.Dream.Network.us2"], "label": "3ABN Dare to Dream"},
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn international$"), "ids": ["3ABN.International.Network.us2"], "label": "3ABN International"},
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn kids$"), "ids": ["3ABN.Kids.Network.us2"], "label": "3ABN Kids"},
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn praise(?: him)?$"), "ids": ["3ABN.Praise.Him.Music.us2"], "label": "3ABN Praise Him"},
    {"region": "US", "pattern": _r(r"^(?:religious )?3 abn proclaim$"), "ids": ["3ABN.Proclaim.Network.us2"], "label": "3ABN Proclaim"},
    {"region": "US", "pattern": _r(r"^(?:ph )?tfc$"), "ids": ["[TFC].The.Filipino.Channel.HD.us2"], "label": "The Filipino Channel / TFC"},
    {"region": "US", "pattern": _r(r"^(?:latin )?fox(?: sports)? deportes$"), "ids": ["Fox.Deportes.HD.us2"], "label": "Fox Deportes"},
    {"region": "US", "pattern": _r(r"^(?:latin )?atreseries$"), "ids": ["Atreseries.us2"], "label": "Atreseries"},
    {"region": "US", "pattern": _r(r"^(?:latin )?tudn$"), "ids": ["TUDN.us2"], "label": "TUDN"},
    {"region": "US", "pattern": _r(r"^(?:latin )?telefe$"), "ids": ["Telefe.Internacional.us2", "Telefe.international.us2"], "label": "Telefe Internacional"},
    {"region": "US", "pattern": _r(r"^(?:latin )?wapa america$"), "ids": ["WAPA.America.us2"], "label": "WAPA America"},
    {"region": "US", "pattern": _r(r"^(?:latin )?video rola$"), "ids": ["Video.Rola.us2"], "label": "Video Rola"},
    {"region": "US", "pattern": _r(r"^(?:latin )?cinema dinamita$"), "ids": ["Cinema.Dinamita.us2", "Cinema.Dinamita.(HD.Feed).us2"], "label": "Cinema Dinamita"},
    {"region": "US", "pattern": _r(r"^(?:latin )?ntn 24$"), "ids": ["NTN24.us2"], "label": "NTN24"},

    # Canadian identities where the current first fuzzy suggestion is often a
    # related but different service.
    {"region": "CA", "pattern": _r(r"^bbc world(?: news)?$"), "ids": ["BBC.News.(North.America).ca2"], "label": "BBC News North America"},
    {"region": "CA", "pattern": _r(r"^aptn$"), "ids": ["Aboriginal.Peoples.Television.Network.HD.ca2"], "label": "APTN"},
    {"region": "CA", "pattern": _r(r"^canal savoir$"), "ids": ["Savoir.média.ca2"], "label": "Savoir média"},
    {"region": "CA", "pattern": _r(r"^chch hamilton$|^chch$"), "ids": ["CHCH-DT.ca2"], "label": "CHCH Hamilton"},
    {"region": "CA", "pattern": _r(r"^cpac english$|^cpac$"), "ids": ["[CPAC].Cable.Public.Affairs.Channel.HD.ca2", "Cable.Public.Affairs.Channel.HD.ca2"], "label": "CPAC English"},
    {"region": "CA", "pattern": _r(r"^cpac french$"), "ids": ["[CPACF].Cable.Public.Affairs.Channel.(French).ca2", "Cable.Public.Affairs.Channel.(French).ca2"], "label": "CPAC French"},
    {"region": "CA", "pattern": _r(r"^rds(?: 1)?$"), "ids": ["Réseau.des.Sports.(RDS).HD.ca2", "Réseau.des.Sports.(RDS).ca2"], "label": "RDS"},
    {"region": "CA", "pattern": _r(r"^rds info$"), "ids": ["Réseau.des.Sports.Info.ca2"], "label": "RDS Info"},
    {"region": "CA", "pattern": _r(r"^nfl network$"), "ids": ["NFL.Network.ALT.-.Canadian.Feed.HD.ca2", "NFL.Network.ALT.-.Canadian.Feed.ca2"], "label": "NFL Network Canadian feed"},
    {"region": "CA", "pattern": _r(r"^fox racing$"), "ids": ["Fox.Sports.Racing.HD.ca2", "Fox.Sports.Racing.ca2"], "label": "Fox Sports Racing"},
    {"region": "CA", "pattern": _r(r"^bloomberg$"), "ids": ["BNN.Bloomberg.HD.ca2", "BNN.Bloomberg.ca2"], "label": "BNN Bloomberg"},
    {"region": "CA", "pattern": _r(r"^cnbc$"), "ids": ["CNBC.Canadian.Feed.ca2"], "label": "CNBC Canadian Feed"},
    {"region": "CA", "pattern": _r(r"^cp24(?: news)?$"), "ids": ["Cable.Pulse.24.(CP24).HD.ca2"], "label": "CP24"},
    {"region": "CA", "pattern": _r(r"^ewtn$"), "ids": ["EWTNC.(EWTN.-.Canadian.Feed).ca2"], "label": "EWTN Canadian Feed"},
    {"region": "CA", "pattern": _r(r"^al jazeera$"), "ids": ["Al.Jazeera.English.ca2"], "label": "Al Jazeera English Canada"},
    {"region": "CA", "pattern": _r(r"^crime and investigation$"), "ids": ["Crime.Plus.Investigation.SD.ca2"], "label": "Crime + Investigation Canada"},
]

# Put v6 aliases before v5/v4 aliases. _rule_result still confirms that the
# chosen ID exists in the catalog downloaded during this run.
DIRECT_RULES = V6_DIRECT_RULES + DIRECT_RULES


# -----------------------------------------------------------------------------
# Strong exact identity layer
# -----------------------------------------------------------------------------
V6_STRONG_INDEX: dict[str, dict[str, list[dict[str, str]]]] = {}
V6_CA_MARKET_INDEX: dict[str, dict[tuple[str, ...], list[dict[str, str]]]] = {}
V6_SAFE_SINGLE_KEYS = {
    "ion", "me", "wwe", "murasu", "safari", "addik", "yes", "faith",
    "buzzr", "cheddar", "tjc", "ntv", "discovery", "history",
}
V6_DESCRIPTOR_WORDS = {"tv", "television", "channel", "network"}
V6_SINGULAR_WORDS = {
    "westerns": "western",
    "mysteries": "mystery",
    "classics": "classic",
    "movies": "movie",
    "pictures": "picture",
}


def strong_identity_key_v6(value: object, *, is_epg: bool = False) -> str:
    text = identity_name(repair_utf8_mojibake(value), is_epg=is_epg)
    repairs = (
        (r"\bfilm rise\b", "filmrise"),
        (r"\bscreen pix\b", "screenpix"),
        (r"\bcine max\b", "cinemax"),
        (r"\bcity tv\b", "citytv"),
        (r"\btastmade\b", "tastemade"),
        (r"\bsheemaroo\b", "shemaroo"),
        (r"\bharyanvi\b", "haryana"),
        (r"\bnat geo\b", "national geographic"),
        (r"\bbbc 2 ni\b", "bbc 2 northern ireland"),
        (r"\bbbc 1 ni\b", "bbc 1 northern ireland"),
        (r"\bcp 24\b", "cp24"),
        (r"\b3 abn\b", "3abn"),
        (r"\bthe filipino channel\b", "tfc"),
        (r"\btfc tfc\b", "tfc"),
        (r"\bsaga music haryanavi\b", "saga music haryana"),
        (r"\bsony 10\b", "sony sports 10"),
    )
    for pattern, replacement in repairs:
        text = re.sub(pattern, replacement, text)
    tokens = [V6_SINGULAR_WORDS.get(token, token) for token in text.split()]
    while tokens and tokens[-1] in V6_DESCRIPTOR_WORDS:
        tokens.pop()
    return " ".join(tokens)


def semantic_markers_v6(value: object, *, is_epg: bool = False) -> dict[str, Any]:
    raw = repair_utf8_mojibake(value)
    if is_epg:
        raw = SOURCE_SUFFIX_RE.sub("", str(raw))
    text = canonical_text(raw)
    tokens = set(text.split())
    return {
        "plus": "plus" in tokens,
        "extra": "extra" in tokens or "xtra" in tokens,
        "alternate": bool(tokens & {"alternate", "alt"}),
        "hq": "hq" in tokens,
        "west": bool(tokens & {"west", "pacific"}),
        "east": bool(tokens & {"east", "eastern"}),
        "numbers": {token for token in tokens if token.isdigit()},
        "languages": tokens & LANGUAGE_WORDS,
    }


def semantic_markers_safe_v6(row: dict[str, Any], candidate: dict[str, str]) -> bool:
    query = semantic_markers_v6(row.get("channel_name", ""))
    target = semantic_markers_v6(candidate.get("epg_id", ""), is_epg=True)

    # Plus/Extra/Alternate and HQ are meaningful channel identities. Do not
    # silently remove them during an exact rescue.
    for key in ("plus", "extra", "alternate", "hq"):
        if query[key] != target[key]:
            return False

    if query["west"] != target["west"]:
        return False
    # East may be omitted by EPGShare when East is the default national feed.
    if target["east"] and not query["east"]:
        return False

    if query["numbers"] and target["numbers"] and query["numbers"] != target["numbers"]:
        return False
    if query["languages"] and target["languages"] and query["languages"].isdisjoint(target["languages"]):
        return False
    return True


def strong_query_keys_v6(row: dict[str, Any]) -> list[str]:
    base = strong_identity_key_v6(row.get("channel_name", ""))
    keys: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in keys:
            keys.append(value)

    add(base)
    category = canonical_text(row.get("category_name", ""))
    # Provider/category wrappers that are not part of the actual station brand.
    for prefix, category_word in (("latin ", "latin"), ("religious ", ""), ("ph ", ""), ("faith ", "")):
        if base.startswith(prefix) and (not category_word or category_word in category or prefix.strip() in canonical_text(row.get("channel_name", ""))):
            add(base[len(prefix):])
    return keys


def strong_exact_identity_match_v6(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region == "ALL":
        return None

    candidates: list[dict[str, str]] = []
    used_keys: list[str] = []
    for key in strong_query_keys_v6(row):
        words = key.split()
        distinctive = [
            word for word in words
            if word not in GENERIC_WORDS and word not in CONTENT_WORDS and word not in LANGUAGE_WORDS
        ]
        if len(words) == 1 and key not in V6_SAFE_SINGLE_KEYS:
            continue
        if not distinctive and key not in V6_SAFE_SINGLE_KEYS:
            continue

        for candidate in V6_STRONG_INDEX.get(region, {}).get(key, []):
            if not direction_safe(row, candidate) or not numbers_safe(row, candidate) or not languages_safe(row, candidate):
                continue
            if not semantic_markers_safe_v6(row, candidate):
                continue
            if not candidate_plausible(row, candidate):
                # Exact keys with known spelling/compound repairs can still be
                # safe, but never waive direction/number/language/marker checks.
                query_core = distinctive_tokens(key)
                candidate_core = distinctive_tokens(candidate.get("epg_id", ""), is_epg=True)
                if not query_core or not candidate_core or not (query_core & candidate_core):
                    continue
            candidates.append(candidate)
            used_keys.append(key)

    if not candidates:
        return None

    # Deduplicate candidates found through more than one harmless wrapper key.
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for candidate in candidates:
        unique[(candidate["feed"], candidate["epg_id"])] = candidate
    candidates = list(unique.values())

    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in candidates:
        families[candidate["family_key"]].append(candidate)

    # Different exact families remain review. Equivalent HD/SD/source variants
    # within one family are collapsed by choose_preferred.
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    primary = choose_preferred(group, row)
    alternatives = [
        item for item in sorted(group, key=lambda item: candidate_preference(item, row))
        if item is not primary
    ]
    alternate = alternatives[0] if alternatives else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": "Exact channel identity after UTF-8, spelling, compound-brand and descriptor normalization",
    }


# -----------------------------------------------------------------------------
# Canadian market-name resolver
# -----------------------------------------------------------------------------
CA_PROVINCE_AND_NOISE_V6 = {
    "ab", "alberta", "bc", "british", "columbia", "mb", "manitoba",
    "nb", "brunswick", "new", "nl", "newfoundland", "labrador",
    "ns", "nova", "scotia", "nt", "northwest", "territories",
    "nu", "nunavut", "on", "ontario", "pe", "pei", "prince",
    "edward", "island", "qc", "quebec", "sk", "saskatchewan",
    "yt", "yukon", "news", "source", "local", "channel", "station",
}
CA_MARKET_ALIASES_V6: dict[str, dict[tuple[str, ...], tuple[str, ...]]] = {
    "cbc": {
        ("regina",): ("saskatchewan",),
        ("saskatoon",): ("saskatchewan",),
        ("windsor",): ("windsor",),
    },
    "global": {
        ("vancouver",): ("bc",),
        ("british", "columbia"): ("bc",),
        ("winnipeg",): ("manitoba",),
        ("halifax",): ("maritimes",),
        ("monterial",): ("montreal",),
    },
}


def _ca_network_and_market_v6(value: object, *, is_epg: bool = False) -> tuple[str, tuple[str, ...]]:
    text = identity_name(value, is_epg=is_epg)
    text = re.sub(r"^city tv\b", "citytv", text)
    tokens = text.split()
    if not tokens:
        return "", tuple()

    network = ""
    start = 0
    if tokens[:2] == ["ctv", "2"]:
        network, start = "ctv2", 2
    elif tokens[0] == "ctv":
        network, start = "ctv", 1
    elif tokens[0] == "cbc":
        network, start = "cbc", 1
    elif tokens[0] in {"citytv", "city"}:
        network, start = "citytv", 1
    elif tokens[0] == "global":
        network, start = "global", 1
    else:
        return "", tuple()

    market = tokens[start:]
    # CBC.Television-Windsor.9 is the catalog's Windsor affiliate identity.
    if network == "cbc" and market[:1] == ["television"]:
        market = market[1:]
    market = [token for token in market if token not in CA_PROVINCE_AND_NOISE_V6]
    # A channel number on the catalog ID is not part of the geographic market.
    market = [token for token in market if not token.isdigit()]
    result = tuple(market)
    result = CA_MARKET_ALIASES_V6.get(network, {}).get(result, result)
    return network, result


def canada_market_alias_match_v6(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region != "CA":
        return None
    network, market = _ca_network_and_market_v6(row.get("channel_name", ""))
    if not network or not market:
        return None
    group = V6_CA_MARKET_INDEX.get(network, {}).get(market, [])
    group = [
        item for item in group
        if direction_safe(row, item) and numbers_safe(row, item) and languages_safe(row, item)
    ]
    if not group:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in group:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    family = next(iter(families.values()))
    primary = choose_preferred(family, row)
    alternatives = [item for item in sorted(family, key=lambda item: candidate_preference(item, row)) if item is not primary]
    alternate = alternatives[0] if alternatives else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": f"Exact Canadian {network.upper()} market identity after removing province/noise words",
    }


# -----------------------------------------------------------------------------
# Safer US call-sign extraction and full-power preference
# -----------------------------------------------------------------------------
_ROW_CALLSIGNS_V5 = row_callsigns


def row_callsigns(value: object) -> list[tuple[str, str]]:
    raw = str(value or "")
    result: list[tuple[str, str]] = []

    # Parse every call sign inside wrappers, including (WTLV/WJXX).
    for wrapper in re.finditer(r"[\[(]([^\])]{2,48})[\])]", raw):
        part = wrapper.group(1)
        for match in re.finditer(
            r"(?<![A-Z0-9])([KW][A-Z]{2,4})(?:[- ](?:TV|DT|LD|CD))?(?:[- ]?(\d+))?(?![A-Z0-9])",
            part,
            re.IGNORECASE,
        ):
            item = (match.group(1).upper(), match.group(2) or "")
            if item not in result:
                result.append(item)

    # Preserve v5's explicit non-wrapper detection.
    for item in _ROW_CALLSIGNS_V5(raw):
        if item not in result:
            result.append(item)
    return result


def _candidate_station_class_v6(candidate: dict[str, str], base: str) -> str:
    raw = SOURCE_SUFFIX_RE.sub("", candidate.get("epg_id", ""))
    match = re.search(
        rf"(?:^|[.(]){re.escape(base)}-(TV|DT|LD|CD)(?:-\d+)?(?=[.)]|$)",
        raw,
        re.IGNORECASE,
    )
    return match.group(1).upper() if match else ""


def callsign_match_v6(row: dict[str, Any]) -> dict[str, Any] | None:
    signs = row_callsigns(f"{row.get('channel_name', '')} {row.get('panel_epg_id', '')}")
    if not signs:
        return None

    # Let the proven v5 logic handle exact single-family and combined-station IDs.
    old_result = callsign_match(row)
    if old_result:
        return old_result

    # A single bare call sign may have a full-power DT/TV station and one or more
    # LD/CD translators. Prefer the unique full-power family; never guess when
    # an explicit subchannel is present or multiple full-power families exist.
    if len(signs) != 1:
        return None
    base, sub = signs[0]
    if sub:
        return None
    options = [
        candidate for candidate in CALLSIGN_INDEX.get(base, [])
        if any(item_base == base and not item_sub for item_base, item_sub in candidate_callsigns(candidate))
    ]
    primary_options = [
        candidate for candidate in options
        if _candidate_station_class_v6(candidate, base) in {"DT", "TV"}
    ]
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in primary_options:
        families[candidate["family_key"]].append(candidate)
    if len(families) != 1:
        return None
    group = next(iter(families.values()))
    primary = choose_preferred(group, row)
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": "", "second_epg_feed": "", "second_score": "",
        "score_margin": 100.0,
        "reason": "Exact bare US call sign with one unique full-power DT/TV schedule",
    }


# -----------------------------------------------------------------------------
# v6 matcher preparation and resolution order
# -----------------------------------------------------------------------------
def _smart_v6_prepare_matcher(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> None:
    global V6_STRONG_INDEX, V6_CA_MARKET_INDEX
    # Repair any catalog text supplied by an older saved scan as well as fresh
    # downloads. Fresh v6 downloads are already UTF-8, so this is idempotent.
    repaired_candidates: list[dict[str, str]] = []
    for original in real_candidates:
        item = dict(original)
        item["epg_id"] = repair_utf8_mojibake(item.get("epg_id", ""))
        item["display_name"] = repair_utf8_mojibake(item.get("display_name", ""))
        repaired_candidates.append(item)
    _smart_v5_prepare_matcher(repaired_candidates, dummy_ids)
    V6_STRONG_INDEX = {}
    for region, items in list(BY_REGION.items()) + [("ALL", CANDIDATES)]:
        index: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in items:
            key = strong_identity_key_v6(item["epg_id"], is_epg=True)
            if key:
                index[key].append(item)
        V6_STRONG_INDEX[region] = dict(index)

    V6_CA_MARKET_INDEX = {}
    ca_networks: dict[str, dict[tuple[str, ...], list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for item in BY_REGION.get("CA", []):
        network, market = _ca_network_and_market_v6(item["epg_id"], is_epg=True)
        if network and market:
            ca_networks[network][market].append(item)
    V6_CA_MARKET_INDEX = {
        network: dict(markets) for network, markets in ca_networks.items()
    }


def _resolve_pre_panel_v6(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    for resolver in (
        lambda r: special_rule(r),
        lambda r: direct_rule(r, region),
        lambda r: fanduel_match(r, region),
        lambda r: league_team_match_v5(r, region),
    ):
        match = resolver(row)
        if match:
            return match
    return None


def _resolve_post_panel_v6(row: dict[str, Any], region: str, explicit_region: bool) -> dict[str, Any] | None:
    if region == "US":
        call = callsign_match_v6(row)
        if call:
            return call

    if explicit_region and region not in active_regions() and region != "ALL":
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": f"No enabled EPGShare source is configured for {region}; unrelated countries are not searched",
        }

    # Strong exact identity runs before fuzzy matching and can also select a
    # clearly exact alternative that v5 listed as the second candidate.
    for resolver in (
        spectrum_news_match_v5,
        canada_market_alias_match_v6,
        canada_local_network_match_v5,
        strong_exact_identity_match_v6,
        exact_alias_variant_match_v5,
        safe_brand_descriptor_match_v5,
        panel_id_catalog_match_v5,
        exact_family_match,
        safe_panel_hint,
        language_context_match,
        fuzzy_family_match,
    ):
        match = resolver(row, region)
        if match:
            return match
    return None


def make_matching_report_v6(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Build a fresh mapping report using Smart Rules v6."""
    _smart_v6_prepare_matcher(real_candidates, dummy_ids)
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

        row = {
            "category_name": category_name,
            "channel_name": channel_name,
            "panel_epg_id": panel_epg_id,
        }
        region, explicit_region, _route_reason = detect_route(row)
        record: dict[str, Any] = {
            "server_id": server_id, "stream_id": stream_id, "category_id": category_id,
            "category_name": category_name, "channel_number": channel_number,
            "channel_name": channel_name, "normalized_name": identity_name(channel_name),
            "detected_region": region, "panel_epg_id": panel_epg_id,
            "panel_epg_status": str(panel_info.get("status", "")),
            "panel_usable_programmes": int(panel_info.get("current_or_future_informative_rows", 0) or 0),
            "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
            "action": "", "source": "", "epg_id": "", "epg_feed": "",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "", "reason": "",
        }

        match = _resolve_pre_panel_v6(row, region)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        # Retain panel data only when the panel-quality scan found useful
        # current/future programme titles. Empty/stale IDs are never kept.
        if panel_epg_id and panel_info.get("status") == "USABLE":
            record.update({
                "action": "KEEP_PANEL", "source": "panel", "epg_id": panel_epg_id,
                "epg_feed": "server xmltv.php",
                "reason": "Panel XMLTV contains useful current/future programme information",
            })
            records.append(record)
            continue

        match = _resolve_post_panel_v6(row, region, explicit_region)
        if match:
            _smart_v3_apply_match(record, match)
        else:
            record.update({
                "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
                "reason": _smart_v3_unmatched_reason(region),
            })
        records.append(record)

    return pd.DataFrame(records)


def classify_review_row_v6(row: dict[str, Any]) -> dict[str, Any]:
    """Re-run v6 rules against one old REVIEW row for audit/preview files."""
    region, explicit_region, _route_reason = detect_route(row)
    match = _resolve_pre_panel_v6(row, region)
    if not match:
        match = _resolve_post_panel_v6(row, region, explicit_region)
    if not match:
        match = {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": _smart_v3_unmatched_reason(region),
        }
    return {
        "proposed_action": match.get("action", ""),
        "proposed_source": match.get("source", ""),
        "proposed_epg_id": match.get("epg_id", ""),
        "proposed_epg_feed": match.get("epg_feed", ""),
        "proposed_best_score": match.get("best_score", ""),
        "proposed_second_epg_id": match.get("second_epg_id", ""),
        "proposed_second_epg_feed": match.get("second_epg_feed", ""),
        "proposed_second_score": match.get("second_score", ""),
        "proposed_score_margin": match.get("score_margin", ""),
        "proposed_reason": match.get("reason", ""),
        "proposed_region": region,
    }


def run_smart_rules_v6_self_test(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Run catalog-backed v6 examples before the real server match starts."""
    cases = [
        # Newly requested event rules.
        ("UFC numbered event", "LIVE | UFC / Triller TV", "UFC 02 :", "UFC03.us", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("Peacock Golf Pass event", "SPORTS | Peacock", "US | Peacock Golf Pass", "GolfPass.peacocktv", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("Peacock WWE Pass event", "SPORTS | Peacock", "US | Peacock WWE Pass", "WWENetwork.peacocktv", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("Peacock numbered event", "SPORTS | Peacock", "US | Peacock 18", "Peacock_13", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),

        # Newly requested direct mappings.
        ("MLB Strike Zone", "US | MLB Teams", "US MLB STRIKEzone", "US MLB STRIKEzone (CX)", "AUTO_EPGSHARE", "MLB.Network.Strike.Zone.HD.us2"),
        ("Bon Appetit UTF-8", "US | Sports", "US Bon Appetit", "US Bon Appetit (S)", "AUTO_EPGSHARE", "Bon.Appétit.TV.us2"),
        ("NHL Alternate", "US | Sports", "US NHL Alternate", "US NHL Alternate (S)", "AUTO_EPGSHARE", "NHL.Network.HD.us2"),
        ("BBC Two Northern Ireland", "UK | Regional & Red Button", "UK-BBC Two Northern Ireland FHD*", "bbc2northernireland.uk", "AUTO_EPGSHARE", "BBC.Two.NI.HD.uk"),
        ("Sony Sports Ten 3", "IN/PK | Sports", "IN | Sony Ten 3 FHD", "sonyten3hd.in", "AUTO_EPGSHARE", "Sony.Sports.Ten.3.HD.in"),
        ("Sony Sports Ten 4", "IN/PK | Sports", "IN | Sony Ten 4 FHD", "", "AUTO_EPGSHARE", "Sony.Sports.Ten.4.HD.in"),
        ("Haryana Beats", "IN | Music", "IN | Haryanvi Beats", "ts846", "AUTO_EPGSHARE", "Haryana.Beats.in"),

        # Exact-second and encoding/compound normalization tests.
        ("ION exact alternative", "US | Entertainment", "US ion", "", "AUTO_EPGSHARE", "ION.Television.HD.us2"),
        ("Vision exact alternative", "CA | Entertainment", "CA Vision HD", "", "AUTO_EPGSHARE", "Vision.TV.HD.ca2"),
        ("FilmRise compound", "US | Movies", "US FilmRise Mysteries", "", "AUTO_EPGSHARE", "Filmrise.Mysteries.us2"),
        ("Citytv market", "CA | General", "CA CITY TV CALGARY", "", "AUTO_EPGSHARE", "Citytv.Calgary.ca2"),
        ("CBC apostrophe mojibake", "CA | General", "CA CBC St John s HD", "", "AUTO_EPGSHARE", "CBC.St..John’s.ca2"),
        ("Music Choice dummy", "US | Music", "US MC Radio | Classic Rock", "", "AUTO_DUMMY", "Music.Choice.Dummy.us"),

        # Regressions: preserve important distinctions.
        ("CBS Sports HQ guard", "US | News", "US CBS Sports HQ", "", "REVIEW", "CBS.Sports.Network.HD.us2"),
        ("CTV2 Toronto regression", "CA | General", "CA CTV2 TORONTO HD (D)", "", "AUTO_EPGSHARE", "CTV.Two.-.Toronto.ca2"),
        ("Adult regression", "XXX | Adults", "+18 | Private", "", "AUTO_DUMMY", "Adult.Programming.Dummy.us"),
        ("WNBA regression", "US | WNBA", "WNBA 7", "", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("Latin MLB EI regression", "Latin", "(MX) (IZ) MLB EI 4", "", "UNMATCHED", ""),
    ]

    channels: list[dict[str, str]] = []
    categories: dict[str, str] = {}
    for index, (_label, category, channel, panel_id, _action, _epg_id) in enumerate(cases, start=1):
        cid = str(index)
        categories[cid] = category
        channels.append({
            "stream_id": str(index), "category_id": cid,
            "name": channel, "epg_channel_id": panel_id,
        })

    report = make_matching_report_v6("self_test", channels, categories, {}, real_candidates, dummy_ids)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        label, _category, _channel, _panel, expected_action, expected_id = case
        actual = report.iloc[index]
        passed = (
            actual["action"] == expected_action
            and actual["epg_id"].casefold() == expected_id.casefold()
        )
        rows.append({
            "test": label, "passed": passed,
            "expected_action": expected_action, "actual_action": actual["action"],
            "expected_epg_id": expected_id, "actual_epg_id": actual["epg_id"],
            "reason": actual["reason"],
        })
    result = pd.DataFrame(rows)
    failures = result.loc[~result["passed"]]
    if not failures.empty:
        display(failures)
        raise RuntimeError("Smart Rules v6 self-test failed. Stop before matching the server.")
    return result


make_matching_report_v6.smart_rules_version = SMART_RULES_VERSION
make_matching_report_v6.matcher_build_id = MATCHER_BUILD_ID

# Remove the generic alias so a stale runtime cannot silently call an old matcher.
globals().pop("make_matching_report", None)
print(f"ACTIVE MATCHER: Smart Rules v{SMART_RULES_VERSION} ({MATCHER_BUILD_ID})")

# =============================================================================
# Smart Rules v7 active layer
# =============================================================================
# v7 keeps every v6 safety rule and adds:
# - category-aware Indian language matching (Hindi is the default only when no
#   regional language is present),
# - diaspora routing for IN (TO) and US (IN)/(CAN) channels to Canadian feeds,
# - code-first US local station matching, including KPBS, KUSIDT, WNETDT and
#   explicit DT/TV subchannels,
# - PBS brand/market to call-sign matching using the PBS member-station table,
# - CBC numbered sports slots as event channels,
# - direct East/West aliases for Discovery and HGTV plus other requested IDs,
# - a catalog-backed alias identity layer that can select an exact second
#   candidate instead of keeping a weaker fuzzy first candidate.

SMART_RULES_VERSION = "7.0"
MATCHER_BUILD_ID = "SKYTV-SMART-RULES-7.0-2026-07-15"

# -----------------------------------------------------------------------------
# Routing and provider wrappers
# -----------------------------------------------------------------------------
_STRIP_ROUTING_PREFIX_V6_ACTIVE = strip_routing_prefix
_DETECT_ROUTE_V6_ACTIVE = detect_route


def strip_routing_prefix(value: object) -> str:
    """Remove country/provider wrappers without erasing the real channel name."""
    text = _STRIP_ROUTING_PREFIX_V6_ACTIVE(value)
    for _ in range(3):
        updated = re.sub(
            r"^\s*(?:[A-Z]{2,3}\s*)?\(\s*(?:TO|TOR|TORONTO|CAN|CANADA|IN|INDIA)\s*\)\s*",
            "", text, flags=re.IGNORECASE,
        ).strip()
        if updated == text:
            break
        text = updated
    return text


TORONTO_DIASPORA_PREFIX_RE = re.compile(
    r"^\s*(?:[A-Z]{2,3}\s*)?\(\s*TO(?:RONTO)?\s*\)(?=\s|$)",
    re.IGNORECASE,
)
US_INDIAN_DIASPORA_PREFIX_RE = re.compile(
    r"^\s*US\s*\(\s*(?:IN|INDIA)\s*\)(?=\s|$)",
    re.IGNORECASE,
)
PAKISTAN_PREFIX_RE = re.compile(r"^\s*PK(?:\s*[|:/-]|\s+)", re.IGNORECASE)


def is_toronto_diaspora_v7(row: dict[str, Any]) -> bool:
    return bool(TORONTO_DIASPORA_PREFIX_RE.search(str(row.get("channel_name", ""))))


def is_us_indian_diaspora_v7(row: dict[str, Any]) -> bool:
    return bool(US_INDIAN_DIASPORA_PREFIX_RE.search(str(row.get("channel_name", ""))))


def detect_route(row: dict[str, Any]) -> tuple[str, bool, str]:
    channel = str(row.get("channel_name", ""))
    if is_toronto_diaspora_v7(row):
        return "CA", True, "Toronto wrapper selects Canadian EPG schedules"
    if is_us_indian_diaspora_v7(row):
        # Search US identities first, then a safe Canadian fallback. This follows
        # the provider label more closely than forcing every US Indian channel to CA.
        return "US", True, "US Indian diaspora channel; US first, Canada fallback"
    if PAKISTAN_PREFIX_RE.search(channel):
        return "PK", True, "channel prefix indicates Pakistan"
    return _DETECT_ROUTE_V6_ACTIVE(row)

# -----------------------------------------------------------------------------
# Event channels
# -----------------------------------------------------------------------------
_SPECIAL_RULE_V6_ACTIVE = special_rule
CBC_NUMBERED_EVENT_RE = re.compile(
    r"^\s*(?:CA\s*[|:/-]\s*)?CBC\s+\d{1,3}\s*:",
    re.IGNORECASE,
)


def special_rule(row: dict[str, Any]) -> dict[str, str] | None:
    category = str(row.get("category_name", ""))
    channel = str(row.get("channel_name", ""))
    identity = identity_name(channel)

    # Named sports passes/whip-around feeds are event channels whose live
    # content changes, even when the provider placed them in Entertainment.
    if re.fullmatch(r"(?:mlb big inning|nbc golf pass|premier league)", identity):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("PPV.EVENTS.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Sports pass/whip-around provider stream is event-specific and changes with the live event",
        }

    # All Music Choice provider streams are playlist/radio-style channels.
    # They do not have a reliable one-to-one TV schedule, so use the dedicated
    # dummy instead of allowing words such as Kidz, Rock, Jazz, or Country to
    # match unrelated television channels or local call signs.
    if re.search(r"\bmusic\s+choice\b", channel, re.IGNORECASE):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("Music.Choice.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Music Choice playlist/radio stream uses the dedicated Music Choice dummy guide",
        }

    if re.search(r"\bCA\s*\|\s*SPORTS\b", category, re.IGNORECASE) and CBC_NUMBERED_EVENT_RE.search(channel):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("PPV.EVENTS.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Numbered CBC sports slot is a provider-updated live event channel",
        }

    # Seasonal FAST/24-hour channels do not have a reliable programme schedule.
    if re.match(r"^xmas\b", identity):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("24.7.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Seasonal XMAS provider channel uses a 24/7 placeholder schedule",
        }

    # Numbered Vande Gujarat education feeds are separate provider streams and
    # are not represented by the generic Gujarat news schedules in EPGShare.
    if re.fullmatch(r"vande gujarat \d+", identity):
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": "Numbered Vande Gujarat feed has no verified one-to-one EPGShare schedule",
        }

    # Continuous temple/gurdwara live cameras are not regular linear channels;
    # a 24/7 placeholder is safer than a fuzzy devotional-channel assignment.
    religious_context = canonical_text(f"{category} {channel}")
    exact_religious_ids = {
        "shri babulnaath temple mumbai",
        "shri mahalaxmi temple mumbai",
        "shri ashtavinayak mahaganpati ranjangaon",
    }
    if (
        identity not in exact_religious_ids
        and re.search(r"\b(?:punjabi religious|religious)\b", religious_context)
        and re.search(r"\b(?:gurdwara|gurudwara|guru nanak|takht|sikh society|singh sabha|mandir|temple)\b", religious_context)
    ):
        return {
            "action": "AUTO_DUMMY", "source": "dummy",
            "epg_id": dummy_id("24.7.Dummy.us"),
            "epg_feed": "DUMMY_CHANNELS",
            "reason": "Continuous religious live-camera channel uses a 24/7 placeholder schedule",
        }

    return _SPECIAL_RULE_V6_ACTIVE(row)

# -----------------------------------------------------------------------------
# Direct aliases requested in the latest review plus closely related safe cases
# -----------------------------------------------------------------------------
V7_DIRECT_RULES: list[dict[str, Any]] = [
    # India: exact regional/news/entertainment identities.
    {
        "region": "IN",
        "pattern": _r(r"^zee punjab(?:i)? haryana himachal(?: pradesh)?$"),
        "ids": ["ZEE.PUNJAB.HARYANA.HIMACHAL.in", "Zee.Punjab.Haryana.HP.in"],
        "label": "Zee Punjab Haryana Himachal Pradesh",
    },
    {
        "region": "IN",
        "pattern": _r(r"^colors (?:cinema|movies?)$"),
        "category_pattern": re.compile(r"\b(?:kannada|kn)\b", re.IGNORECASE),
        "ids": ["COLORS.KANNADA.MOVIES.in", "Colors.Kannada.Cinema.in"],
        "label": "Colors Kannada Movies/Cinema",
    },
    {
        "region": "IN",
        "pattern": _r(r"^colors (?:cinema|movies?)$"),
        "category_pattern": re.compile(r"\b(?:bangla|bengali|bd)\b", re.IGNORECASE),
        "ids": ["COLORS.BANGLA.CINEMA.in", "Colors.Bangla.Cinema.in"],
        "label": "Colors Bangla Cinema",
    },
    {
        "region": "IN",
        "pattern": _r(r"^colors (?:cinema|movies?)$"),
        "category_pattern": re.compile(r"\b(?:gujarati|gujrati|guj)\b", re.IGNORECASE),
        "ids": ["COLORS.GUJARATI.CINEMA.in", "Colors.Gujarati.Cinema.in"],
        "label": "Colors Gujarati Cinema",
    },
    {
        "region": "IN",
        "pattern": _r(r"^sony(?: entertainment)? (?:tv|television)$"),
        "ids": ["Sony.Entertainment.Television.in"],
        "label": "Sony Entertainment Television",
    },
    {
        "region": "IN",
        "pattern": _r(r"^and flix(?: hindi)?$"),
        "ids": ["and.Flix.HD.in"],
        "label": "&Flix",
    },
    {
        "region": "IN",
        "pattern": _r(r"^news 18 up (?:uttarkand|uttarakhand)$"),
        "ids": ["News18.UP.in"],
        "label": "News18 UP/Uttarakhand",
    },
    {
        "region": "IN",
        "pattern": _r(r"^(?:dd )?ban(?:gla)?$"),
        "category_pattern": re.compile(r"\b(?:bangla|bengali|bd)\b", re.IGNORECASE),
        "ids": ["DD.Bangla.in"],
        "label": "DD Bangla",
    },
    {
        "region": "IN",
        "pattern": _r(r"^zee ban(?:gla)? cinema$"),
        "ids": ["ZEE.BANGLA.CINEMA.in", "Zee.Bangla.Cinema.in"],
        "label": "Zee Bangla Cinema",
    },
    {
        "region": "IN",
        "pattern": _r(r"^nickelodeon junior$|^nick junior$"),
        "ids": ["Nickelodeon.Jr..in", "Nick.Junior.in"],
        "label": "Nickelodeon Junior",
    },
    {
        "region": "IN",
        "pattern": _r(r"^sony kal$|^sony pal$"),
        "ids": ["SONY.PAL.in", "Sony.Pal.in", "Sony.Pal.Entertainment.in2"],
        "label": "Sony Pal (provider spelling Kal/Pal)",
    },
    {
        "region": "IN",
        "pattern": _r(r"^(?:nat geo|national geographic)(?: hindi)?$"),
        "ids": ["NATIONAL.GEOGRAPHIC.HD.in", "National.Geographic.HD.in", "NATIONAL.GEOGRAPHIC.in"],
        "label": "National Geographic India",
    },
    {
        "region": "IN",
        "pattern": _r(r"^food xp$|^foodxp$"),
        "ids": ["Foodxp.in"],
        "label": "Food XP",
    },
    {
        "region": "IN",
        "pattern": _r(r"^cnn intl$|^cnn international$"),
        "ids": ["CNN.INTERNATIONAL.in"],
        "label": "CNN International India",
    },
    {
        "region": "IN",
        "pattern": _r(r"^zee$"),
        "category_pattern": re.compile(r"\b(?:tamil|tm)\b", re.IGNORECASE),
        "ids": ["ZEE.TAMIL.HD.in", "Zee.Tamil.HD.APAC.in", "ZEE.TAMIL.in"],
        "label": "Zee Tamil from Tamil category",
    },
    {
        "region": "IN",
        "pattern": _r(r"^studio yuva$"),
        "ids": ["Studio.Yuva.Alpha.in"],
        "label": "Studio Yuva",
    },

    # US linear services and East/West feeds.
    {
        "region": "US", "pattern": _r(r"^altitude sports alternate$"),
        "ids": ["Altitude.Sports.us2"], "label": "Altitude Sports alternate provider feed",
    },
    {
        "region": "US", "pattern": _r(r"^(?:the )?discovery(?: channel)? east$"),
        "ids": ["Discovery.Channel.HD.us2"], "label": "Discovery Channel East/default",
    },
    {
        "region": "US", "pattern": _r(r"^(?:the )?discovery(?: channel)? west$"),
        "ids": ["The.Discovery.Channel.HD.(Pacific).us2"], "label": "Discovery Channel West/Pacific",
    },
    {
        "region": "US", "pattern": _r(r"^discovery science$|^science channel$"),
        "ids": ["Science.Channel.HD.us2"], "label": "Science Channel",
    },
    {
        "region": "US", "pattern": _r(r"^game plus$"),
        "ids": ["Game+.Game.Plus.HD.us2"], "label": "GAME+",
    },
    {
        "region": "US", "pattern": _r(r"^hgtv east$|^home and garden(?: television)? east$"),
        "ids": ["Home.and.Garden.Television.HD.us2"], "label": "HGTV East/default",
    },
    {
        "region": "US", "pattern": _r(r"^hgtv west$|^home and garden(?: television)? west$"),
        "ids": ["Home.and.Garden.Television.HD.(Pacific).us2"], "label": "HGTV West/Pacific",
    },
    {
        "region": "US", "pattern": _r(r"^cbs news$"),
        "ids": ["CBS.News.us", "CBS.News.National.Stream.us2"], "label": "CBS News national stream",
    },
    {
        "region": "US", "pattern": _r(r"^(?:nat geo|national geographic)(?: channel)? east$"),
        "ids": ["National.Geographic.HD.us2"], "label": "National Geographic East/default",
    },
    {
        "region": "US", "pattern": _r(r"^(?:nat geo|national geographic)(?: channel)? west$"),
        "ids": ["National.Geographic.HD.(Pacific).us2"], "label": "National Geographic West/Pacific",
    },
    {
        "region": "US", "pattern": _r(r"^own east$|^oprah winfrey network east$"),
        "ids": ["Oprah.Winfrey.Network.HD.us2"], "label": "OWN East/default",
    },
    {
        "region": "US", "pattern": _r(r"^own west$|^oprah winfrey network west$"),
        "ids": ["Oprah.Winfrey.Network.(Pacific.A.Feed).us2"], "label": "OWN West/Pacific",
    },
    {
        "region": "US", "pattern": _r(r"^usa network east$"),
        "ids": ["USA.Network.HD.us2"], "label": "USA Network East/default",
    },
    {
        "region": "US", "pattern": _r(r"^usa network west$"),
        "ids": ["USA.Network.HD.(Pacific).us2"], "label": "USA Network West/Pacific",
    },
    {
        "region": "US", "pattern": _r(r"^(?:nbc )?golf(?: channel)?$"),
        "ids": ["Golf.Channel.HD.us2"], "label": "Golf Channel",
    },

    # Canadian/South-Asian diaspora schedules.
    {
        "region": "CA", "pattern": _r(r"^prime asia$|^prime asia tv$"),
        "ids": ["Prime.Asia.TV.SD.ca2"], "label": "Prime Asia Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^star plus$"),
        "ids": ["ATN.Star.Plus.ca2"], "label": "ATN Star Plus Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^b4u(?: plus)?$"),
        "ids": ["B4U.Canada.ca2"], "label": "B4U Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^zee tv$"),
        "ids": ["Zee.TV.Canada.ca2"], "label": "Zee TV Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^(?:atn )?colors$"),
        "ids": ["ATN.Colors.ca2"], "label": "ATN Colors Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^(?:atn )?jaya tv$"),
        "ids": ["ATN.Jaya.TV.ca2"], "label": "ATN Jaya TV Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^ptc punjabi(?: tv)?$"),
        "ids": ["PTC.Punjabi.TV.ca2"], "label": "PTC Punjabi Canada",
    },
    {
        "region": "CA", "pattern": _r(r"^tamil vision$"),
        "ids": ["Tamil.Vision.(Canada).ca2"], "label": "Tamil Vision Canada",
    },
]

DIRECT_RULES = V7_DIRECT_RULES + DIRECT_RULES

# Additional catalog-backed aliases found while replaying the latest full
# review file. These are permanent linear identities or clearly equivalent
# provider names; rotating/themed FAST channels are deliberately excluded.
V7_REVIEW_REFINEMENT_RULES: list[dict[str, Any]] = [
    # More India identities where the provider name is shorter or misspelled.
    {"region": "IN", "pattern": _r(r"^(?:nick )?sonic hindi$"), "ids": ["Sonic.Hindi.in"], "label": "Sonic Hindi"},
    {"region": "IN", "pattern": _r(r"^nickelodeon hindi$|^nick hindi$"), "ids": ["Nick.Hindi.in", "Nick.HD+.in", "NICK.HD+.in"], "label": "Nickelodeon Hindi"},
    {"region": "IN", "pattern": _r(r"^mh 1 prime punjabi news$|^mh1 prime punjabi news$"), "ids": ["Mh.One.News.in", "MH.ONE.in"], "label": "MH One Punjabi News"},
    {"region": "IN", "pattern": _r(r"^udaya tv$"), "ids": ["UDAYA.HD.in", "Udaya.HD.in", "UDAYA.TV.in", "Udaya.TV.in"], "label": "Udaya TV"},
    {"region": "IN", "pattern": _r(r"^nd 24 newsdaily$|^newsdaily nd 24$"), "ids": ["ND.24.in"], "label": "ND24 Newsdaily"},
    {"region": "IN", "pattern": _r(r"^good times$"), "ids": ["GOOD.TiMES.in", "NDTV.Good.Times.in"], "label": "Good Times"},
    {"region": "IN", "pattern": _r(r"^awakening devotional$"), "ids": ["Awakening.in"], "label": "Awakening devotional"},
    {"region": "IN", "pattern": _r(r"^shri ashtavinayak mahaganpati ranjangaon$"), "ids": ["Ashtavinayak.Ranjangaon.in"], "label": "Ashtavinayak Ranjangaon"},
    {"region": "IN", "pattern": _r(r"^shri babulnaath temple mumbai$"), "ids": ["Babulnaath.Mumbai.in"], "label": "Babulnaath Mumbai"},
    {"region": "IN", "pattern": _r(r"^shri mahalaxmi temple mumbai$"), "ids": ["Mahalaxmi.Mumbai.in"], "label": "Mahalaxmi Mumbai"},

    # Common US linear brands and exact schedule-name expansions.
    {"region": "US", "pattern": _r(r"^mtv$"), "ids": ["MTV.-.Music.Television.HD.us2"], "label": "MTV"},
    {"region": "US", "pattern": _r(r"^mtv west$"), "ids": ["MTV.-.Music.Television.HD.(Pacific).us2"], "label": "MTV West/Pacific"},
    {"region": "US", "pattern": _r(r"^mtv 2$"), "ids": ["MTV2:.Music.Television.HD.us2"], "label": "MTV2"},
    {"region": "US", "pattern": _r(r"^mtv live$"), "ids": ["MTVLIVE.us2"], "label": "MTV Live"},
    {"region": "US", "pattern": _r(r"^me tv toons$"), "ids": ["MeTV.Toons.us2"], "label": "MeTV Toons"},
    {"region": "US", "pattern": _r(r"^me tv plus$"), "ids": ["MeTV.Plus.us2"], "label": "MeTV Plus"},
    {"region": "US", "pattern": _r(r"^accu weather now$|^accuweather now$"), "ids": ["AccuWeather.HD.us2"], "label": "AccuWeather Now"},
    {"region": "US", "pattern": _r(r"^cheddar news$"), "ids": ["Cheddar.us2"], "label": "Cheddar News"},
    {"region": "US", "pattern": _r(r"^cmt music$|^country music television$"), "ids": ["CMT.HD.us2"], "label": "CMT"},
    {"region": "US", "pattern": _r(r"^newsmax tv 2$"), "ids": ["Newsmax.TV.HD.us2"], "label": "Newsmax TV duplicate provider feed"},
    {"region": "US", "pattern": _r(r"^fuse music$"), "ids": ["FM.Fuse.Music.us2"], "label": "Fuse Music"},
    {"region": "US", "pattern": _r(r"^sportsnet new york$"), "ids": ["SNY.SportsNet.New.York.HD.us2"], "label": "SNY SportsNet New York"},
    {"region": "US", "pattern": _r(r"^space city home network alternate$"), "ids": ["Space.City.Home.Network.HD.us2"], "label": "Space City Home Network alternate provider feed"},
    {"region": "US", "pattern": _r(r"^catholic faith network$"), "ids": ["CFN.CATHOLIC.FAITH.NETWORK.us2"], "label": "Catholic Faith Network"},
    {"region": "US", "pattern": _r(r"^free movies plus$"), "ids": ["Plus.Movies.us2"], "label": "Free Movies Plus / Plus Movies"},
    {"region": "US", "pattern": _r(r"^jl tv jewish life$|^jltv jewish life$"), "ids": ["Jewish.Life.TV.us2"], "label": "Jewish Life TV"},
    {"region": "US", "pattern": _r(r"^outside tv plus$"), "ids": ["Outside.Television.HD.us2"], "label": "Outside Television"},
    {"region": "US", "pattern": _r(r"^caracol tv$"), "ids": ["CARACOL.INTERNATIONAL.us2"], "label": "Caracol International"},
    {"region": "US", "pattern": _r(r"^cctv 4$"), "ids": ["CCTV4-China.Central.TV.us2"], "label": "CCTV-4"},
    {"region": "US", "pattern": _r(r"^(?:latin )?cnn espanol$"), "ids": ["CNN.En.Español.us2"], "label": "CNN en Español"},
    {"region": "US", "pattern": _r(r"^(?:latin )?history espanol$"), "ids": ["History.Channel.En.Español.us2"], "label": "History en Español"},
    {"region": "US", "pattern": _r(r"^(?:latin )?discovery espanol$"), "ids": ["Discovery.en.Español.us2"], "label": "Discovery en Español"},
    {"region": "US", "pattern": _r(r"^(?:latin )?(?:nbc )?universo(?: east)?$"), "ids": ["UNIVERSO.HD.us2"], "label": "Universo"},
    {"region": "US", "pattern": _r(r"^zona tudn$"), "ids": ["TUDN.us2"], "label": "TUDN"},
    {"region": "US", "pattern": _r(r"^(?:latin )?canal once$"), "ids": ["Once.us2"], "label": "Canal Once"},
    {"region": "US", "pattern": _r(r"^showtime family$"), "ids": ["Showtime.Familyzone.HD.us2"], "label": "Showtime Family Zone"},
    {"region": "US", "pattern": _r(r"^the movie channel (?:xtra|extra) east$"), "ids": ["The.Movie.Channel.Extra.HD.us2"], "label": "The Movie Channel Extra East"},
    {"region": "US", "pattern": _r(r"^telemundo(?: television network)? east$"), "ids": ["Telemundo.Satellite.Feed.us2"], "label": "Telemundo East"},
    {"region": "US", "pattern": _r(r"^telemundo(?: television network)? west$"), "ids": ["Telemundo.Satellite.Feed.Pacific.us2"], "label": "Telemundo West/Pacific"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?bravo east$"), "ids": ["Bravo.HD.us2"], "label": "Bravo East/default"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?bravo west$"), "ids": ["Bravo.(Pacific).us2"], "label": "Bravo West/Pacific"},
    {"region": "US", "pattern": _r(r"^(?:nbc )?syfy east$"), "ids": ["Syfy.HD.us2"], "label": "Syfy East/default"},
    {"region": "US", "pattern": _r(r"^e entertainment east$|^e east$"), "ids": ["E!.Entertainment.Television.HD.us2"], "label": "E! Entertainment East/default"},
    {"region": "US", "pattern": _r(r"^the movie channel (?:xtra|extra) east$"), "ids": ["The.Movie.Channel.Extra.HD.us2"], "label": "The Movie Channel Extra"},

    # Canadian linear brands and provider duplicates.
    {"region": "CA", "pattern": _r(r"^cable 14(?: 1)?$"), "ids": ["Cable.14.ca2"], "label": "Cable 14"},
    {"region": "CA", "pattern": _r(r"^hollywood suite 70s$"), "ids": ["Hollywood.Suite.70s+.ca2"], "label": "Hollywood Suite 70s"},
    {"region": "CA", "pattern": _r(r"^hollywood suite (?:80s|90s)$"), "ids": ["Hollywood.Suite.80s.and.90s.ca2"], "label": "Hollywood Suite 80s/90s"},
    {"region": "CA", "pattern": _r(r"^meteo media$"), "ids": ["MétéoMédia.(MM).HD.ca2"], "label": "MétéoMédia"},
    {"region": "CA", "pattern": _r(r"^series plus$"), "ids": ["Canal.Séries.Plus.HD.ca2"], "label": "Séries Plus"},
    {"region": "CA", "pattern": _r(r"^citytv saskatchewan$"), "ids": ["City.Saskatchewan.ca2"], "label": "Citytv Saskatchewan"},
    {"region": "CA", "pattern": _r(r"^much$|^much music$"), "ids": ["Much.Music.HD.ca2"], "label": "Much Music"},
    {"region": "CA", "pattern": _r(r"^history channel$"), "ids": ["History.Television.HD.(Canada).ca2", "History.ca2"], "label": "History Canada"},
    {"region": "CA", "pattern": _r(r"^ici art$|^ici artv$"), "ids": ["ICI.ARTV.HD.ca2", "ICI.ARTV.Canada.ca2"], "label": "ICI ARTV"},
    {"region": "CA", "pattern": _r(r"^tvo(?: ontario)?$|^tv ontario$"), "ids": ["TVO.ca2"], "label": "TVO"},
    {"region": "CA", "pattern": _r(r"^univision(?: canada)?$"), "ids": ["Univision.Canada.ca2"], "label": "Univision Canada"},
    {"region": "CA", "pattern": _r(r"^fairchild 2$"), "ids": ["Fairchild.2.SD.ca2"], "label": "Fairchild 2"},
    {"region": "CA", "pattern": _r(r"^ntv$"), "ids": ["NTV.HD.ca2", "NTV.ca2"], "label": "NTV Newfoundland Canada"},
    {"region": "CA", "pattern": _r(r"^noovo(?: montreal| sherbrooke)?$"), "ids": ["Noovo.ca2"], "label": "Noovo"},
    {"region": "CA", "pattern": _r(r"^tamil vision(?: tv)?$"), "ids": ["Tamil.Vision.(Canada).ca2"], "label": "Tamil Vision Canada"},
    {"region": "CA", "pattern": _r(r"^chco tv 26 1 st andrews nb$"), "ids": ["CHCO.ca2"], "label": "CHCO"},
    {"region": "CA", "pattern": _r(r"^cottage$|^cottage life$"), "ids": ["Cottage.Life.HD.ca2", "Cottage.Life.ca2"], "label": "Cottage Life"},
    {"region": "CA", "pattern": _r(r"^miracle$|^miracle channel$"), "ids": ["The.Miracle.Channel.-.CJIL.ca2"], "label": "The Miracle Channel"},
    {"region": "CA", "pattern": _r(r"^legislative assembly of ontario$"), "ids": ["Ontario.Legislature.HD.ca2"], "label": "Ontario Legislature"},
    {"region": "CA", "pattern": _r(r"^yes hamilton$"), "ids": ["YesTV.ca2"], "label": "YesTV Hamilton"},
    {"region": "CA", "pattern": _r(r"^cp24(?: news)?(?: live \d+)?$"), "ids": ["Cable.Pulse.24.(CP24).HD.ca2"], "label": "CP24"},
    {"region": "CA", "pattern": _r(r"^rai$|^rai italia$"), "ids": ["Rai.Italia.ca2"], "label": "Rai Italia"},
    {"region": "CA", "pattern": _r(r"^odyssey$"), "ids": ["Odyssey.Greek.TV.ca2"], "label": "Odyssey Greek TV"},
    {"region": "CA", "pattern": _r(r"^teletoon$"), "ids": ["Télétoon.Français.ca2"], "label": "Télétoon Français"},
    {"region": "CA", "pattern": _r(r"^ptc$|^ptc punjabi$"), "ids": ["PTC.Punjabi.TV.ca2"], "label": "PTC Punjabi Canada"},
    {"region": "CA", "pattern": _r(r"^zee$|^zee tv$"), "ids": ["Zee.TV.Canada.ca2"], "label": "Zee TV Canada"},
]
DIRECT_RULES = V7_REVIEW_REFINEMENT_RULES + DIRECT_RULES

# Additional safe, catalog-backed identities found while reviewing every row in
# the latest review CSV. They are kept separate from fuzzy matching so each
# mapping remains explainable and the target must exist in the current catalog.
V7_SAFE_EXPANSION_RULES: list[dict[str, Any]] = [
    # India and UK South-Asian services.
    {"region": "IN", "pattern": _r(r"^nickelodeon hindi(?: plus)?$|^nick hindi(?: plus)?$"), "ids": ["Nick.Hindi.in", "Nick.HD+.in", "NICK.HD+.in"], "label": "Nickelodeon Hindi"},
    {"region": "IN", "pattern": _r(r"^udaya tv(?: hdx*| hd)?$"), "ids": ["UDAYA.HD.in", "Udaya.HD.in", "UDAYA.TV.in", "Udaya.TV.in"], "label": "Udaya TV"},
    {"region": "IN", "pattern": _r(r"^malaimurasu seithigal$"), "ids": ["SEITHIGAL.in", "Kalaignar.Seithigal..in"], "label": "Malaimurasu Seithigal"},
    {"region": "IN", "pattern": _r(r"^smbc insight$"), "ids": ["SMBC.TV.in"], "label": "SMBC Insight / SMBC TV"},
    {"region": "IN", "pattern": _r(r"^utsav gold$"), "ids": ["Utsav.Gold.HD.uk", "Utsav.GOLD.uk"], "label": "Utsav Gold UK feed"},
    {"region": "IN", "pattern": _r(r"^utsav bharat$"), "ids": ["Utsav.Bharat.uk"], "label": "Utsav Bharat UK feed"},
    {"region": "IN", "pattern": _r(r"^utsav plus$"), "ids": ["Utsav.Plus.HD.uk"], "label": "Utsav Plus UK feed"},
    {"region": "UK", "pattern": _r(r"^drama$|^u and drama$"), "ids": ["U.and.Drama.uk"], "label": "U & Drama"},
    {"region": "UK", "pattern": _r(r"^now 90 s and 00 s$|^now 90s and 00s$|^now 90s 00s$"), "ids": ["NOW.90s00s.uk"], "label": "NOW 90s & 00s"},
    {"region": "UK", "pattern": _r(r"^tjc(?: live)?$"), "ids": ["TJC.HD.uk", "TJC.uk"], "label": "TJC"},

    # US linear brands, rebrands, national feeds, and provider duplicate names.
    {"region": "US", "pattern": _r(r"^(?:real )?nosey$"), "ids": ["Nosey.us2", "Nosey.on.Peacock.us2"], "label": "Nosey"},
    {"region": "US", "pattern": _r(r"^hallmark movies and mysteries$|^hallmark movies mysteries$|^hallmark mystery$"), "ids": ["Hallmark.Mystery.HD.us2"], "label": "Hallmark Mystery"},
    {"region": "US", "pattern": _r(r"^hallmark drama$|^hallmark family$"), "ids": ["Hallmark.Family.us2"], "label": "Hallmark Family (formerly Hallmark Drama)"},
    {"region": "US", "pattern": _r(r"^ion east$|^ion television east$"), "ids": ["ION.Television.HD.us2"], "label": "ION East/default"},
    {"region": "US", "pattern": _r(r"^adult swim east$|^adultswim east$|^adult swim$|^adultswim$"), "ids": ["AdultSwim.com.Cartoon.Network.us2"], "label": "Adult Swim"},
    {"region": "US", "pattern": _r(r"^bbc world news$|^bbc news$"), "ids": ["BBC.News.(North.America).HD.us2"], "label": "BBC News North America"},
    {"region": "US", "pattern": _r(r"^cheddar business$|^cheddar news$|^cheddar$"), "ids": ["Cheddar.us2"], "label": "Cheddar"},
    {"region": "US", "pattern": _r(r"^i 24 news$|^i24 news$"), "ids": ["i24.News.English.us2"], "label": "i24 News English"},
    {"region": "US", "pattern": _r(r"^one america news(?: oan)?$|^one america news network$|^oan$"), "ids": ["One.America.News.Network.HD.us2"], "label": "One America News Network"},
    {"region": "US", "pattern": _r(r"^real america s voice(?: rav)?$|^real americas voice(?: rav)?$"), "ids": ["Real.Americas.Voice.us2"], "label": "Real America's Voice"},
    {"region": "US", "pattern": _r(r"^the first news$|^the first$"), "ids": ["The.First.us2"], "label": "The First"},
    {"region": "US", "pattern": _r(r"^ewtn$|^eternal word television network$"), "ids": ["EWTN.-.Eternal.Word.Television.Network.HD.us2"], "label": "EWTN English"},
    {"region": "US", "pattern": _r(r"^yes 1 network$|^yes network$"), "ids": ["Yes.Network.us2"], "label": "YES Network"},
    {"region": "US", "pattern": _r(r"^tbs 2$|^tbs east$|^tbs$"), "ids": ["TBS.HD.us2"], "label": "TBS East/default"},
    {"region": "US", "pattern": _r(r"^tbs west$"), "ids": ["TBS.HD.(Pacific).us2"], "label": "TBS West/Pacific"},
    {"region": "US", "pattern": _r(r"^buzzr$"), "ids": ["BUZZR.Stream.us2"], "label": "BUZZR"},
    {"region": "US", "pattern": _r(r"^oxygen$|^oxygen true crime$"), "ids": ["Oxygen.True.Crime.HD.us2"], "label": "Oxygen True Crime"},
    {"region": "US", "pattern": _r(r"^the asylum movie channel$|^asylum movie channel$"), "ids": ["Asylum.Movie.us2"], "label": "The Asylum Movie Channel"},
    {"region": "US", "pattern": _r(r"^hogar de hgtv$|^hogar hgtv$"), "ids": ["Hogar.HD.us2", "Hogar.TV.us2"], "label": "Hogar de HGTV"},
    {"region": "US", "pattern": _r(r"^tastemade international$|^tastemade$"), "ids": ["Tastemade.us2"], "label": "Tastemade"},
    {"region": "US", "pattern": _r(r"^msg sportsnet$"), "ids": ["MSG.Sportsnet.2.HD.us2", "MSG.Sportsnet.2.Zone.1.us2"], "label": "MSG Sportsnet"},
    {"region": "US", "pattern": _r(r"^sho x bet$|^shoxbet$"), "ids": ["SHO.x.BET.HD.us2"], "label": "SHO x BET"},
    {"region": "US", "pattern": _r(r"^starz kids and family(?: east)?$|^starz kids(?: east)?$"), "ids": ["Starz.Kids.HD.us2"], "label": "Starz Kids & Family"},
    {"region": "US", "pattern": _r(r"^hsn$|^home shopping network$"), "ids": ["HSN.Home.Shopping.Network.HD.us2"], "label": "HSN"},
    {"region": "US", "pattern": _r(r"^(?:stirr )?bloomberg tv$|^bloomberg tv europe$|^bloomberg$"), "ids": ["Bloomberg.HD.us2"], "label": "Bloomberg TV"},
    {"region": "US", "pattern": _r(r"^at and t sportsnet pittsburgh alternate$|^att sportsnet pittsburgh alternate$"), "ids": ["SportsNet.Pittsburgh.HD.us2"], "label": "SportsNet Pittsburgh alternate provider feed"},
    {"region": "US", "pattern": _r(r"^space city home network (?:alternate|alternative)$"), "ids": ["Space.City.Home.Network.HD.us2"], "label": "Space City Home Network alternate provider feed"},
    {"region": "US", "pattern": _r(r"^sec network(?: national| local)?$"), "ids": ["SEC.Network.HD.us2"], "label": "SEC Network"},
    {"region": "US", "pattern": _r(r"^acc network(?: national| local)?$"), "ids": ["ACC.Network.us2"], "label": "ACC Network"},
    {"region": "US", "pattern": _r(r"^vevo hip hop$|^xmas vevo hip hop$"), "ids": ["Vevo.Hip-Hop.us2"], "label": "Vevo Hip-Hop"},

    # Spectrum/Charter names where the city or network identity is exact.
    {"region": "US", "pattern": _r(r"^bay news 9 tampa$|^spectrum bay news 9 tampa$"), "ids": ["Spectrum.Bay.News.9.Tampa.us2"], "label": "Spectrum Bay News 9 Tampa"},
    {"region": "US", "pattern": _r(r"^spectrum sportsnet la$"), "ids": ["Spectrum.SportsNet.LA.Dodgers.HD.us2"], "label": "Spectrum SportsNet LA Dodgers"},
    {"region": "US", "pattern": _r(r"^spectrum sportsnet$"), "ids": ["Spectrum.SportsNet.Lakers.HD.us2"], "label": "Spectrum SportsNet Lakers"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 worcester$"), "ids": ["Spectrum.News.1.-.(MA).Worcester.-.STVA.us2"], "label": "Spectrum News 1 Worcester"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 springfield$"), "ids": ["Spectrum.News.1.-.(MA).Springfield.-.STVA.us2"], "label": "Spectrum News 1 Springfield MA"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 pittsfield$"), "ids": ["Spectrum.News.1.-.(MA).Pittsfield.-.STVA.us2"], "label": "Spectrum News 1 Pittsfield MA"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 rochester$"), "ids": ["Spectrum.News.-.(New.York).Rochester.STVA.us2"], "label": "Spectrum News 1 Rochester NY"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 raleigh$"), "ids": ["Spectrum.News.1.(Carolinas).-.Raleigh.STVA.us2"], "label": "Spectrum News 1 Raleigh"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 greensboro$"), "ids": ["Spectrum.News.1.(Carolinas).-.Greensboro.STVA.us2"], "label": "Spectrum News 1 Greensboro"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 charlotte$"), "ids": ["Spectrum.News.1.(Carolinas).-.Charlotte.STVA.us2"], "label": "Spectrum News 1 Charlotte"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 el paso$"), "ids": ["Spectrum.News.1.-.El.Paso.Texas.STVA.us2"], "label": "Spectrum News 1 El Paso"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 dallas$"), "ids": ["Spectrum.News.1.-.Dallas.-.Ft.Worth.STVA.us2"], "label": "Spectrum News 1 Dallas-Fort Worth"},
    {"region": "US", "pattern": _r(r"^spectrum news 1 albany$"), "ids": ["Spectrum.News.-.Albany.Capital.Region.STVA.us2"], "label": "Spectrum News Albany"},
    {"region": "US", "pattern": _r(r"^spectrum new?s 1 milwaukee$"), "ids": ["Spectrum.News.1.-.Milwaukee.-.STVA.us2"], "label": "Spectrum News 1 Milwaukee"},

    # Canada: exact numbered sports and diaspora/network names.
    {"region": "CA", "pattern": _r(r"^tsn 4(?: blackout)?(?: 2)?$"), "ids": ["TSN.4.HD.ca2", "TSN.4.ca2"], "label": "TSN 4"},
    {"region": "CA", "pattern": _r(r"^tsn 5(?: blackout)?(?: 2)?$"), "ids": ["TSN.5.HD.ca2", "TSN.5.ca2"], "label": "TSN 5"},
    {"region": "CA", "pattern": _r(r"^b4u(?: movies| music| plus)?$"), "ids": ["B4U.Canada.ca2"], "label": "B4U Canada fallback"},
    {"region": "CA", "pattern": _r(r"^rai italia$"), "ids": ["Rai.Italia.ca2"], "label": "Rai Italia Canada"},
]
DIRECT_RULES = V7_SAFE_EXPANSION_RULES + DIRECT_RULES


# -----------------------------------------------------------------------------
# India language context. Regional category language wins; otherwise Hindi is
# the default only for families that actually have language-specific variants.
# -----------------------------------------------------------------------------
INDIA_LANGUAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("kannada", re.compile(r"\b(?:kannada|kn)\b", re.IGNORECASE)),
    ("bangla", re.compile(r"\b(?:bangla|bengali|bd)\b", re.IGNORECASE)),
    ("gujarati", re.compile(r"\b(?:gujarati|gujrati|guj)\b", re.IGNORECASE)),
    ("punjabi", re.compile(r"\b(?:punjabi|punj|pun)\b", re.IGNORECASE)),
    ("tamil", re.compile(r"\b(?:tamil|tm)\b", re.IGNORECASE)),
    ("telugu", re.compile(r"\b(?:telugu|tg)\b", re.IGNORECASE)),
    ("malayalam", re.compile(r"\b(?:malayalam|my)\b", re.IGNORECASE)),
    ("marathi", re.compile(r"\b(?:marathi|mar)\b", re.IGNORECASE)),
    ("odia", re.compile(r"\b(?:odia|oriya)\b", re.IGNORECASE)),
    ("assamese", re.compile(r"\b(?:assamese|asam)\b", re.IGNORECASE)),
    ("urdu", re.compile(r"\burdu\b", re.IGNORECASE)),
    ("hindi", re.compile(r"\bhindi\b", re.IGNORECASE)),
]

SONIC_LANGUAGE_IDS: dict[str, list[str]] = {
    "hindi": ["Sonic.Hindi.in"],
    "bangla": ["Sonic.Bangla.in"],
    "kannada": ["Sonic.Kannada.in"],
    "malayalam": ["Sonic.Malayalam.in"],
    "marathi": ["Sonic.Marathi.in"],
    "tamil": ["sonic.Tamil.in"],
    "telugu": ["Sonic.Telugu.in"],
}


def india_language_v7(row: dict[str, Any]) -> str:
    context = canonical_text(f"{row.get('category_name', '')} {row.get('channel_name', '')}")
    for language, pattern in INDIA_LANGUAGE_PATTERNS:
        if pattern.search(context):
            return language
    return "hindi"


def india_language_match_v7(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region != "IN":
        return None
    name = identity_name(row.get("channel_name", ""))
    language = india_language_v7(row)

    if re.fullmatch(r"(?:nick )?sonic(?: kids?| k ids)?", name):
        ids = SONIC_LANGUAGE_IDS.get(language, SONIC_LANGUAGE_IDS["hindi"])
        result = _rule_result(ids, f"Sonic {language.title()} selected from India language context")
        if result:
            return result

    # Colors Cinema/Movies is handled by direct category rules first. This
    # fallback covers provider variants such as TS Colors Cinema.
    if re.fullmatch(r"(?:ts )?colors (?:cinema|movies?)", name):
        ids_by_language = {
            "kannada": ["COLORS.KANNADA.MOVIES.in", "Colors.Kannada.Cinema.in"],
            "bangla": ["COLORS.BANGLA.CINEMA.in", "Colors.Bangla.Cinema.in"],
            "gujarati": ["COLORS.GUJARATI.CINEMA.in", "Colors.Gujarati.Cinema.in"],
        }
        ids = ids_by_language.get(language)
        if ids:
            result = _rule_result(ids, f"Colors {language.title()} Cinema selected from category language")
            if result:
                return result
    return None


# -----------------------------------------------------------------------------
# Catalog-backed alias identity layer
# -----------------------------------------------------------------------------
V7_ALIAS_INDEX: dict[str, dict[str, list[dict[str, str]]]] = {}
V7_DIRECTIONAL_INDEX: dict[str, dict[str, list[dict[str, str]]]] = {}
V7_US_LOCAL_CALLSIGN_INDEX: dict[str, list[dict[str, Any]]] = {}


def alias_identity_key_v7(value: object, *, is_epg: bool = False) -> str:
    text = strong_identity_key_v6(value, is_epg=is_epg)
    repairs = (
        (r"\bhome and garden(?: television)?\b", "hgtv"),
        (r"\boprah winfrey network\b", "own"),
        (r"\bnational geographic\b", "nat geo"),
        (r"\bthe discovery channel\b|\bdiscovery channel\b", "discovery"),
        (r"\bcbs news national stream\b", "cbs news"),
        (r"\bsony entertainment(?: television)?\b", "sony tv"),
        (r"\bgame plus game plus\b", "game plus"),
        (r"\badult swim com cartoon(?: network)?\b", "adult swim"),
        (r"\bhallmark movies? and mysteries\b", "hallmark mystery"),
        (r"\bbbc world news\b|\bbbc news north america\b", "bbc news"),
        (r"\bone america news oan\b", "one america news"),
        (r"\bi 24 news english\b", "i 24 news"),
        (r"\breal nosey\b|\bnosey on peacock\b", "nosey"),
        (r"\bcheddar business\b", "cheddar"),
        (r"\beternal word television network\b", "ewtn"),
        (r"\bhome shopping network\b", "hsn"),
        (r"\bstarz kids and family\b", "starz kids"),
        (r"\bbbc one\b", "bbc 1"),
        (r"\bbbc two\b", "bbc 2"),
        (r"\bctv two\b", "ctv 2"),
        (r"\bnews 18\b", "news18"),
        (r"\b(?:national|live) stream\b", ""),
    )
    for pattern, replacement in repairs:
        text = re.sub(pattern, replacement, text)
    tokens = text.split()
    if len(tokens) % 2 == 0 and tokens[: len(tokens) // 2] == tokens[len(tokens) // 2 :]:
        tokens = tokens[: len(tokens) // 2]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def alias_exact_match_v7(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    if region == "ALL":
        return None
    key = alias_identity_key_v7(row.get("channel_name", ""))
    if not key or len(key) < 3:
        return None
    group = [
        item for item in V7_ALIAS_INDEX.get(region, {}).get(key, [])
        if semantic_markers_safe_v6(row, item)
        and direction_safe(row, item)
        and numbers_safe(row, item)
        and languages_safe(row, item)
    ]
    if not group:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in group:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    family = next(iter(families.values()))
    primary = choose_preferred(family, row)
    alternatives = [
        item for item in sorted(family, key=lambda item: candidate_preference(item, row))
        if item is not primary
    ]
    alternate = alternatives[0] if alternatives else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": "Exact catalog identity after safe brand/abbreviation expansion",
    }


def directional_identity_key_v7(value: object, *, is_epg: bool = False) -> str:
    """Identity key with East/West words removed for direction-aware matching."""
    text = alias_identity_key_v7(value, is_epg=is_epg)
    text = re.sub(r"\b(?:east|eastern|west|western|pacific)\b", " ", text)
    text = re.sub(r"\b(?:a feed|feed|default)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def directional_exact_match_v7(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    """Match East/West only when the catalog contains the corresponding direction."""
    query_tokens = all_tokens(row.get("channel_name", ""))
    wants_west = bool({"west", "pacific"} & query_tokens)
    wants_east = "east" in query_tokens
    if not (wants_west or wants_east) or region == "ALL":
        return None
    key = directional_identity_key_v7(row.get("channel_name", ""))
    if not key or len(key) < 2:
        return None
    group = [
        item for item in V7_DIRECTIONAL_INDEX.get(region, {}).get(key, [])
        if direction_safe(row, item)
        and semantic_markers_safe_v6(row, item)
        and numbers_safe(row, item)
        and languages_safe(row, item)
    ]
    if not group:
        return None
    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in group:
        families[item["family_key"]].append(item)
    if len(families) != 1:
        return None
    family = next(iter(families.values()))
    primary = choose_preferred(family, row)
    alternatives = [
        item for item in sorted(family, key=lambda item: candidate_preference(item, row))
        if item is not primary
    ]
    alternate = alternatives[0] if alternatives else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": "Exact channel identity with a catalog-confirmed East/West schedule",
    }


# -----------------------------------------------------------------------------
# Code-first US local station matching
# -----------------------------------------------------------------------------
V7_ROW_CALLSIGN_RE = re.compile(
    r"(?<![A-Z0-9])([KW][A-Z]{2,4})(?:-?(TV|DT|LD|CD))?(?:[-.]?(\d+))?(?![A-Z0-9])"
)
V7_CANDIDATE_CALLSIGN_RE = re.compile(
    r"(?:^|[.(])([KW][A-Z]{2,4})-(TV|DT|LD|CD)(?:[-.]?(\d+))?(?=[.)]|$)",
    re.IGNORECASE,
)


def _candidate_callsign_parts_v7(candidate: dict[str, str]) -> tuple[str, str, str] | None:
    raw = SOURCE_SUFFIX_RE.sub("", candidate.get("epg_id", ""))
    match = V7_CANDIDATE_CALLSIGN_RE.search(raw)
    if not match:
        return None
    base, station_class, sub = match.group(1).upper(), match.group(2).upper(), match.group(3) or ""
    # EPGShare has one identifier such as WUNC-DT.20 where the dotted number
    # is the station's virtual channel, not a DT20 subchannel. Treat dotted
    # two-digit numbers as metadata so a plain WUNC/PBS North Carolina name can
    # still select the only WUNC schedule. Hyphen/attached DT2 forms remain
    # protected as real subchannels.
    if sub and re.search(rf"-{re.escape(station_class)}\.{re.escape(sub)}$", raw, re.IGNORECASE) and len(sub) >= 2:
        sub = ""
    return base, station_class, sub


def extract_row_callsigns_v7(value: object) -> list[tuple[str, str, str]]:
    raw = str(value or "")
    found: list[tuple[str, str, str]] = []
    # US call signs are conventionally written in capitals. Keeping the scan
    # case-sensitive prevents ordinary title words such as "Who" or "Kidz"
    # from being mistaken for WHO-DT or KIDZ-LD. Compact forms such as KUSIDT
    # and WNETDT are still recognized because they are uppercase in the source.
    for match in V7_ROW_CALLSIGN_RE.finditer(raw):
        base, station_class, sub = match.group(1).upper(), (match.group(2) or "").upper(), match.group(3) or ""
        if base not in V7_US_LOCAL_CALLSIGN_INDEX:
            continue
        item = (base, station_class, sub)
        if item not in found:
            found.append(item)
    return found


def _station_result_v7(
    row: dict[str, Any],
    base: str,
    station_class: str = "",
    sub: str = "",
    *,
    reason: str,
) -> dict[str, Any] | None:
    options = list(V7_US_LOCAL_CALLSIGN_INDEX.get(base.upper(), []))
    if sub:
        options = [item for item in options if item["sub"] == sub]
    else:
        options = [item for item in options if not item["sub"]]
    if station_class and options:
        same_class = [item for item in options if item["station_class"] == station_class.upper()]
        if same_class:
            options = same_class
    if not station_class and not sub:
        full_power = [item for item in options if item["station_class"] in {"DT", "TV"}]
        if full_power:
            options = full_power
    if not options:
        return None

    families: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in options:
        candidate = item["candidate"]
        families[candidate["family_key"]].append(candidate)
    if len(families) != 1:
        return None
    family = next(iter(families.values()))
    primary = choose_preferred(family, row)
    alternatives = [
        item for item in sorted(family, key=lambda item: candidate_preference(item, row))
        if item is not primary
    ]
    alternate = alternatives[0] if alternatives else None
    return {
        "action": "AUTO_EPGSHARE", "source": "epgshare",
        "epg_id": primary["epg_id"], "epg_feed": primary["feed"],
        "best_score": 100.0,
        "second_epg_id": alternate["epg_id"] if alternate else "",
        "second_epg_feed": alternate["feed"] if alternate else "",
        "second_score": 100.0 if alternate else "",
        "score_margin": 0.0 if alternate else 100.0,
        "reason": reason,
    }


def callsign_match_v7(row: dict[str, Any]) -> dict[str, Any] | None:
    signs = extract_row_callsigns_v7(
        f"{row.get('channel_name', '')} {row.get('panel_epg_id', '')}"
    )
    # Duplicated forms such as KUSI and KUSIDT collapse to one base/sub.
    reduced: dict[tuple[str, str], tuple[str, str, str]] = {}
    for base, station_class, sub in signs:
        key = (base, sub)
        current = reduced.get(key)
        if current is None or (not current[1] and station_class):
            reduced[key] = (base, station_class, sub)
    signs = list(reduced.values())
    if len(signs) != 1:
        return None
    base, station_class, sub = signs[0]
    return _station_result_v7(
        row, base, station_class, sub,
        reason="Exact US local call sign/subchannel code matched before name similarity",
    )


# -----------------------------------------------------------------------------
# PBS brand/market aliases. Source reference used to build this table:
# https://en.wikipedia.org/wiki/List_of_PBS_member_stations#Member_stations
# A target is accepted only when that call sign is actually present in the
# downloaded US_LOCALS1 catalog for the current run.
# -----------------------------------------------------------------------------
PBS_BRAND_CALLSIGNS_V7: list[tuple[re.Pattern[str], list[str], str]] = [
    (_r(r"^pbs michiana$"), ["WNIT"], "Michiana PBS"),
    (_r(r"^pbs fort wayne$"), ["WFWA"], "Fort Wayne PBS"),
    (_r(r"^ball state pbs(?: indianapolis)?$"), ["WIPB"], "Ball State PBS"),
    (_r(r"^lakeshore pbs$"), ["WYIN"], "Lakeshore PBS"),
    (_r(r"^pbs mountain lake$"), ["WCFE"], "Mountain Lake PBS"),
    (_r(r"^pbs hawai(?:i)?$"), ["KHET"], "PBS Hawai'i"),
    (_r(r"^(?:pbs )?think tv 14(?: network)?(?: dayton)?$"), ["WPTD"], "Think TV 14"),
    (_r(r"^(?:pbs )?think tv 16(?: network)?(?: dayton)?$"), ["WPTO"], "Think TV 16"),
    (_r(r"^pbs cet$|^cet pbs$"), ["WCET"], "CET PBS"),
    (_r(r"^pbs alaska public media$"), ["KAKM"], "Alaska Public Media"),
    (_r(r"^pbs vermont public$"), ["WETK"], "Vermont Public"),
    (_r(r"^pbs west virginia public broadcasting$"), ["WVPB"], "West Virginia Public Broadcasting"),
    (_r(r"^pbs utah$"), ["KUED"], "PBS Utah"),
    (_r(r"^pbs nashville public television$|^nashville pbs$"), ["WNPT"], "Nashville PBS"),
    (_r(r"^pbs oklahoma educational television authority$"), ["KETA"], "Oklahoma Educational Television Authority"),
    (_r(r"^pbs nj$|^nj pbs$"), ["WNJN"], "NJ PBS"),
    (_r(r"^pbs nebraska public media$"), ["KHNE"], "Nebraska Public Media"),
    (_r(r"^pbs maine public$"), ["WMEA"], "Maine Public"),
    (_r(r"^pbs thirteen$"), ["WNET"], "THIRTEEN/WNET"),
    (_r(r"^pbs oregon public broadcasting$"), ["KOPB"], "Oregon Public Broadcasting"),
    (_r(r"^pbs arkansas$"), ["KETS"], "Arkansas PBS"),
    (_r(r"^pbs prairie public$"), ["KFME"], "Prairie Public"),
    (_r(r"^iowa pbs sioux city$"), ["KSIN"], "Iowa PBS Sioux City"),
    (_r(r"^pbs north carolina$"), ["WUNC"], "PBS North Carolina"),
    (_r(r"^pbs mississippi public broadcasting$"), ["WMPN"], "Mississippi Public Broadcasting"),
    (_r(r"^pbs mpt maryland public television$"), ["WMPB"], "Maryland Public Television"),
    (_r(r"^pbs rocky mountain$|^rocky mountain pbs$"), ["KRMA"], "Rocky Mountain PBS"),
    (_r(r"^pbs georgia public broadcasting$"), ["WGTV"], "Georgia Public Broadcasting"),
    (_r(r"^pbs apt$|^alabama public television$"), ["WAIQ"], "Alabama Public Television"),
    (_r(r"^pbs idaho public television$"), ["KAID"], "Idaho Public Television"),
    (_r(r"^pbs lpb louisiana public broadcasting$"), ["WLPB"], "Louisiana Public Broadcasting"),
    (_r(r"^pbs twin cities$|^twin cities pbs$"), ["KTCA"], "Twin Cities PBS"),
    (_r(r"^pbs connecticut public$"), ["WEDH"], "Connecticut Public"),
    (_r(r"^pbs scetv$|^scetv$"), ["WITV"], "South Carolina ETV"),
    (_r(r"^pbs rhode island$"), ["WPVD", "WSBE"], "Rhode Island PBS/Ocean State Media"),
    (_r(r"^pbs new england public media$"), ["WGBY"], "New England Public Media"),
    (_r(r"^pbs gbh boston$|^gbh boston$"), ["WGBH"], "GBH Boston"),
    (_r(r"^pbs western reserve$"), ["WNEO"], "PBS Western Reserve"),
    (_r(r"^pbs so ?cal$"), ["KOCE"], "PBS SoCal"),
    (_r(r"^pbs 39 (?:h)?iladelphia$|^pbs39 (?:h)?iladelphia$"), ["WLVT"], "PBS39 Philadelphia"),
    (_r(r"^pbs wisconsin green bay$"), ["WPNE"], "PBS Wisconsin Green Bay"),
    (_r(r"^pbs vpm richmond$"), ["WCVE"], "VPM PBS Richmond"),
    (_r(r"^vegas pbs(?: las vegas)?$"), ["KLVX"], "Vegas PBS"),
    (_r(r"^valley pbs(?: fresno)?$"), ["KVPT"], "Valley PBS"),
    (_r(r"^southern oregon pbs(?: medford)?$"), ["KSYS"], "Southern Oregon PBS"),
    (_r(r"^pbs north(?: duluth)?$"), ["WDSE"], "PBS North Duluth"),
    (_r(r"^pioneer pbs(?: sioux falls)?$"), ["KWCM"], "Pioneer PBS"),
    (_r(r"^(?:us )?a3 dothan 65 pbs(?: virt)?$|^pbs dothan$"), ["WDIQ"], "Alabama Public Television Dothan"),
    (_r(r"^pbs kansas public television(?: wichita)?$|^pbs kansas$"), ["KPTS"], "PBS Kansas"),
    (_r(r"^pbs el paso$"), ["KCOS"], "PBS El Paso"),
    (_r(r"^pbs panhandle amarillo$|^panhandle pbs$"), ["KACV"], "Panhandle PBS"),
    (_r(r"^pbs ozarks public television(?: springfield)?$"), ["KOZK"], "Ozarks Public Television"),
    (_r(r"^pbs northern california public media(?: san francisco)?$"), ["KRCB"], "Northern California Public Media"),
    (_r(r"^jax pbs(?: jacksonville)?$"), ["WJCT"], "Jax PBS"),
    (_r(r"^east tennessee pbs(?: knoxville)?$"), ["WETP"], "East Tennessee PBS"),
    (_r(r"^blue ridge pbs(?: roanoke)?$"), ["WBRA"], "Blue Ridge PBS"),
]


def pbs_brand_match_v7(row: dict[str, Any]) -> dict[str, Any] | None:
    name = identity_name(row.get("channel_name", ""))
    if "pbs" not in name and not any(token in name for token in ("gbh", "scetv", "think tv")):
        return None
    # Thematic/FAST streams are not the main member-station schedule.
    if re.search(r"\b(?:kids|nature|food|antiques roadshow)\b", name):
        return None
    for pattern, callsigns, label in PBS_BRAND_CALLSIGNS_V7:
        if not pattern.search(name):
            continue
        for base in callsigns:
            result = _station_result_v7(
                row, base,
                reason=f"PBS member-station brand/market resolved to call sign {base} ({label})",
            )
            if result:
                return result
        return None
    return None


# -----------------------------------------------------------------------------
# v7 matcher preparation and resolution
# -----------------------------------------------------------------------------
def _smart_v7_prepare_matcher(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> None:
    global V7_ALIAS_INDEX, V7_DIRECTIONAL_INDEX, V7_US_LOCAL_CALLSIGN_INDEX
    _smart_v6_prepare_matcher(real_candidates, dummy_ids)

    V7_ALIAS_INDEX = {}
    for region, items in list(BY_REGION.items()) + [("ALL", CANDIDATES)]:
        index: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in items:
            key = alias_identity_key_v7(item.get("epg_id", ""), is_epg=True)
            if key:
                index[key].append(item)
        V7_ALIAS_INDEX[region] = dict(index)

    V7_DIRECTIONAL_INDEX = {}
    for region, items in list(BY_REGION.items()) + [("ALL", CANDIDATES)]:
        index: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in items:
            key = directional_identity_key_v7(item.get("epg_id", ""), is_epg=True)
            if key:
                index[key].append(item)
        V7_DIRECTIONAL_INDEX[region] = dict(index)

    station_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in BY_REGION.get("US", []):
        if candidate.get("feed") != "US_LOCALS1":
            continue
        parts = _candidate_callsign_parts_v7(candidate)
        if not parts:
            continue
        base, station_class, sub = parts
        station_index[base].append({
            "candidate": candidate,
            "station_class": station_class,
            "sub": sub,
        })
    V7_US_LOCAL_CALLSIGN_INDEX = dict(station_index)


# -----------------------------------------------------------------------------
# Final v7 direct-alias refinement
# -----------------------------------------------------------------------------
# identity_name() intentionally removes country words. That is useful for fuzzy
# matching but can over-strip real brands such as "USA Network" and can split
# beIN/SportsNet/70s. Direct aliases therefore also use a conservative literal
# identity that removes only the leading provider wrapper and technical labels.
V7_FINAL_REFINEMENT_RULES: list[dict[str, Any]] = [
    {"region": "US", "pattern": _r(r"^daystar$"), "ids": ["Daystar.Television.Network.HD.[CHARTER].us2"], "label": "Daystar English"},
    {"region": "US", "pattern": _r(r"^showtime east$|^showtime$"), "ids": ["Paramount+.with.Showtime.HD.us2"], "label": "Showtime East/current Paramount+ with Showtime schedule"},
    {"region": "US", "pattern": _r(r"^showtime west$"), "ids": ["Paramount+.with.Showtime.HD.(Pacific).us2"], "label": "Showtime West/current Paramount+ with Showtime Pacific schedule"},
    {"region": "US", "pattern": _r(r"^great american country$|^gac family$"), "ids": ["Great.American.Family.HD.us2"], "label": "Great American Family / former GAC Family name"},
    {"region": "CA", "pattern": _r(r"^atn cricket plus$"), "ids": ["ATN.Cricket.Plus.us2"], "label": "ATN Cricket Plus North American feed"},
    {"region": "US", "pattern": _r(r"^circle$"), "ids": ["Circle.Country.us2"], "label": "Circle Country / Circle Network"},
    {"region": "US", "pattern": _r(r"^lifetime movies?$|^lifetime movie network lmn$|^lmn$"), "ids": ["LMN.HD.us2"], "label": "Lifetime Movie Network (LMN)"},
    {"region": "US", "pattern": _r(r"^shoxbet(?: east)?$|^sho x bet(?: east)?$"), "ids": ["SHO.x.BET.HD.us2"], "label": "SHO x BET East/default"},
    {"region": "US", "pattern": _r(r"^showtime family zone east$"), "ids": ["Showtime.Familyzone.HD.us2"], "label": "Showtime Family Zone East/default"},
    {"region": "US", "pattern": _r(r"^(?:tcm|turner classic movies) east$"), "ids": ["Turner.Classic.Movies.HD.us2"], "label": "Turner Classic Movies East/default"},
    {"region": "US", "pattern": _r(r"^latin paramount network$|^paramount network east$"), "ids": ["Paramount.Network.HD.us2"], "label": "Paramount Network East/default"},
    {"region": "CA", "pattern": _r(r"^ntv st john s cjon newfoundland tv$|^ntv st johns cjon newfoundland tv$"), "ids": ["NTV.HD.ca2", "NTV.ca2"], "label": "NTV St. John's / CJON"},
    {"region": "US", "pattern": _r(r"^bally sports carolinas$|^fanduel sports carolinas$"), "ids": ["FanDuel.Sports.Network.South-.Carolinas.us"], "label": "Bally/FanDuel Sports Carolinas"},
    {"region": "US", "pattern": _r(r"^(?:bally|fanduel) sports new orleans(?: plus| extra)?$"), "ids": ["FanDuel.Sports.New.Orleans.us"], "label": "Bally/FanDuel Sports New Orleans"},
    {"region": "US", "pattern": _r(r"^bein sports 4$|^bein4 sports$|^bein 4 sports$"), "ids": ["beIN_SPORTS4_DIGITAL_Mono_EN.bein"], "label": "beIN Sports 4 English"},
    {"region": "US", "pattern": _r(r"^bein sports 5$|^bein5 sports$|^bein 5 sports$"), "ids": ["beIN_SPORTS5_DIGITAL_Mono_EN.bein"], "label": "beIN Sports 5 English"},
    {"region": "US", "pattern": _r(r"^bein sports 6$|^bein6 sports$|^bein 6 sports$"), "ids": ["beIN_SPORTS66_DIGITAL_Mono-01_EN.bein"], "label": "beIN Sports 6 English"},
    {"region": "US", "pattern": _r(r"^bein sports 7$|^bein7 sports$|^bein 7 sports$"), "ids": ["beIN_SPORTS7_DIGITAL_Mono_EN.bein"], "label": "beIN Sports 7 English"},
    {"region": "US", "pattern": _r(r"^bein sports 8$|^bein8 sports$|^bein 8 sports$"), "ids": ["logos-_beINSPORTS8_EN.bein"], "label": "beIN Sports 8 English"},
    {"region": "US", "pattern": _r(r"^bein sports 9$|^bein9 sports$|^bein 9 sports$"), "ids": ["logos-_beINSPORTS9_EN.bein"], "label": "beIN Sports 9 English"},
    {"region": "US", "pattern": _r(r"^sportsnet new york$|^sports net new york$"), "ids": ["SNY.SportsNet.New.York.HD.us2"], "label": "SNY SportsNet New York"},
    {"region": "US", "pattern": _r(r"^fuse music(?: fm)?$"), "ids": ["FM.Fuse.Music.us2"], "label": "Fuse Music"},
    {"region": "US", "pattern": _r(r"^spectrum sportsnet la$|^spectrum sports net la$"), "ids": ["Spectrum.SportsNet.LA.Dodgers.HD.us2"], "label": "Spectrum SportsNet LA Dodgers"},
    {"region": "US", "pattern": _r(r"^spectrum sportsnet$|^spectrum sports net$"), "ids": ["Spectrum.SportsNet.Lakers.HD.us2"], "label": "Spectrum SportsNet Lakers"},
    {"region": "US", "pattern": _r(r"^(?:english )?bein sports$|^(?:english )?be in sports$"), "ids": ["beIN.Sports.USA.HD.us2"], "label": "beIN Sports USA English"},
    {"region": "US", "pattern": _r(r"^(?:spanish )?bein sports en espanol$|^(?:spanish )?be in sports en espanol$|^bein sports espanol$"), "ids": ["beIN.Sports.En.Español.HD.us2"], "label": "beIN Sports en Español"},
    {"region": "US", "pattern": _r(r"^abc national feed(?: central| east)?$"), "ids": ["ABC.National.Feed.us2"], "label": "ABC National Feed East/Central"},
    {"region": "US", "pattern": _r(r"^abc national feed(?: west| pacific)$"), "ids": ["ABC.National.Feed.Pacific.us2"], "label": "ABC National Feed Pacific"},
    {"region": "US", "pattern": _r(r"^nbc sports bay area plus alternate$"), "ids": ["NBC.Sports.Bay.Area.Plus.us2"], "label": "NBC Sports Bay Area Plus alternate"},
    {"region": "US", "pattern": _r(r"^mtv 2 east$|^mtv2 east$"), "ids": ["MTV2:.Music.Television.HD.us2"], "label": "MTV2 East/default"},
    {"region": "US", "pattern": _r(r"^usa network east$"), "ids": ["USA.Network.HD.us2"], "label": "USA Network East/default"},
    {"region": "US", "pattern": _r(r"^black voices plus$"), "ids": ["Black.Voices.us2"], "label": "Black Voices Plus provider feed"},
    {"region": "CA", "pattern": _r(r"^hollywood suite 70s$|^hollywood suite 70 s$"), "ids": ["Hollywood.Suite.70s+.ca2"], "label": "Hollywood Suite 70s+"},
    {"region": "CA", "pattern": _r(r"^hollywood suite (?:80s|90s)$|^hollywood suite (?:80 s|90 s)$"), "ids": ["Hollywood.Suite.80s.and.90s.ca2"], "label": "Hollywood Suite 80s/90s"},
    {"region": "CA", "pattern": _r(r"^ici tele 2$"), "ids": ["ICI.Tele.HD.ca2", "ICI.Tele.ca2"], "label": "ICI Télé provider duplicate 2"},
]
DIRECT_RULES = V7_FINAL_REFINEMENT_RULES + DIRECT_RULES


V7_LITERAL_TRAILING_TAG_RE = re.compile(
    r"(?:\s*\((?:A|B|C|D|E|F|F2|FL|FM|H|L|R|S|X|CX|PC|SD|HD|FHD|UHD|4K|DZ|HX|ST|HG)\))+\s*$",
    re.IGNORECASE,
)
V7_LEADING_PROVIDER_CODE_RE = re.compile(
    r"^\s*\((?:SP2?|F2|FD|CX|PC|DZ|HX|ST|HG|UV|PH|CN|GR|IT|FR|MX|PA|TB)\)\s*",
    re.IGNORECASE,
)
V7_LEADING_REGION_RE = re.compile(
    r"^\s*(?:US|USA|CA|CANADA|UK|GB|INR?|INDIA|PK|PT|BEIN|DSTV|XXX)"
    r"(?:\s*\([^)]{1,16}\))?\s*(?:[|:/\\-]+\s*|\s+)",
    re.IGNORECASE,
)


def literal_channel_identity_v7(value: object) -> str:
    """Conservative identity for exact rules; preserves brands like USA/beIN."""
    text = repair_utf8_mojibake(value)
    text = clean_provider_variant(text)
    for _ in range(3):
        updated = V7_LEADING_PROVIDER_CODE_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated

    # Route/provider segments before a vertical bar are safe to remove when the
    # left side contains only a short country/provider label such as IN-NEWS.
    if "|" in text:
        left, right = text.split("|", 1)
        left_key = re.sub(r"[^a-z0-9]+", " ", repair_utf8_mojibake(left).casefold()).strip()
        if re.fullmatch(
            r"(?:us|usa|ca|canada|uk|gb|in|inr|india|pk|pt|bein|dstv|xxx|live|sports|news|"
            r"(?:in|inr) (?:news|guj|gujrati|gujarati|punj|punjabi|kn|kannada|tm|tamil|my|malayalam|bangla|bengali))",
            left_key,
        ):
            text = right.strip()

    text = V7_LEADING_REGION_RE.sub("", text).strip()
    text = V7_LITERAL_TRAILING_TAG_RE.sub("", text).strip()
    text = canonical_text(text)
    text = re.sub(r"\bbe\s+in\b", "bein", text)
    text = re.sub(r"\bsports\s+net\b", "sportsnet", text)
    text = re.sub(r"\bsh\s+ox\s+bet\b", "shoxbet", text)
    text = re.sub(r"\b(\d{2})\s+s\b", r"\1s", text)
    words = [word for word in text.split() if word not in TECH_WORDS]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


_DIRECT_RULE_V7_IDENTITY_BASE = direct_rule


def direct_rule(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    """Run existing rules, then retry exact aliases with literal identities."""
    base = _DIRECT_RULE_V7_IDENTITY_BASE(row, region)
    if base:
        return base

    variants = {
        literal_channel_identity_v7(row.get("channel_name", "")),
        alias_identity_key_v7(row.get("channel_name", "")),
        strong_identity_key_v6(row.get("channel_name", "")),
    }
    variants.discard("")
    category_identity = canonical_text(row.get("category_name", ""))
    for rule in DIRECT_RULES:
        if region != rule["region"]:
            continue
        if not any(rule["pattern"].search(identity) for identity in variants):
            continue
        category_pattern = rule.get("category_pattern")
        if category_pattern is not None and not category_pattern.search(category_identity):
            continue
        result = _rule_result(rule["ids"], rule["label"])
        if result:
            result = dict(result)
            result["reason"] = f"Exact/known channel alias using literal provider-safe identity: {rule['label']}"
            return result
    return None


def _resolve_pre_panel_v7(row: dict[str, Any], region: str) -> dict[str, Any] | None:
    for resolver in (
        lambda r: special_rule(r),
        lambda r: direct_rule(r, region),
        lambda r: india_language_match_v7(r, region),
        lambda r: fanduel_match(r, region),
        lambda r: league_team_match_v5(r, region),
    ):
        match = resolver(row)
        if match:
            return match
    return None


def _resolve_post_panel_v7(row: dict[str, Any], region: str, explicit_region: bool) -> dict[str, Any] | None:
    call = callsign_match_v7(row)
    if call:
        return call
    pbs = pbs_brand_match_v7(row)
    if pbs:
        return pbs

    # Toronto-labelled channels use Canadian schedules only. Do not let a weak
    # fuzzy score attach an adult, sports, or unrelated Canadian channel.
    if is_toronto_diaspora_v7(row):
        for resolver in (
            lambda r: direct_rule(r, "CA"),
            lambda r: directional_exact_match_v7(r, "CA"),
            lambda r: alias_exact_match_v7(r, "CA"),
        ):
            match = resolver(row)
            if match:
                return match
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": "Toronto diaspora channel has no verified exact Canadian EPGShare schedule",
        }

    # US (IN) means an Indian service carried in the US. Prefer an exact US
    # identity; when it is absent, use an exact Canadian South-Asian feed.
    if is_us_indian_diaspora_v7(row):
        for target_region in ("US", "CA"):
            for resolver in (
                lambda r, tr=target_region: direct_rule(r, tr),
                lambda r, tr=target_region: directional_exact_match_v7(r, tr),
                lambda r, tr=target_region: alias_exact_match_v7(r, tr),
            ):
                match = resolver(row)
                if match:
                    if target_region == "CA":
                        match = dict(match)
                        match["reason"] = f"Canadian fallback for US Indian channel: {match.get('reason', 'exact identity')}"
                    return match
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": "US Indian diaspora channel has no verified exact US or Canadian EPGShare schedule",
        }

    if explicit_region and region not in active_regions() and region != "ALL":
        return {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": f"No enabled EPGShare source is configured for {region}; unrelated countries are not searched",
        }

    directional = directional_exact_match_v7(row, region)
    if directional:
        return directional
    exact = alias_exact_match_v7(row, region)
    if exact:
        return exact
    return _resolve_post_panel_v6(row, region, explicit_region)


def make_matching_report_v7(
    server_id: str,
    channels: list[dict[str, Any]],
    category_names: dict[str, str],
    panel_quality: dict[str, dict[str, Any]],
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Build a fresh mapping report using Smart Rules v7."""
    _smart_v7_prepare_matcher(real_candidates, dummy_ids)
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

        row = {
            "category_name": category_name,
            "channel_name": channel_name,
            "panel_epg_id": panel_epg_id,
        }
        region, explicit_region, _route_reason = detect_route(row)
        record: dict[str, Any] = {
            "server_id": server_id, "stream_id": stream_id, "category_id": category_id,
            "category_name": category_name, "channel_number": channel_number,
            "channel_name": channel_name, "normalized_name": identity_name(channel_name),
            "detected_region": region, "panel_epg_id": panel_epg_id,
            "panel_epg_status": str(panel_info.get("status", "")),
            "panel_usable_programmes": int(panel_info.get("current_or_future_informative_rows", 0) or 0),
            "panel_latest_stop_utc": str(panel_info.get("latest_stop_utc", "")),
            "action": "", "source": "", "epg_id": "", "epg_feed": "",
            "best_score": "", "second_epg_id": "", "second_epg_feed": "",
            "second_score": "", "score_margin": "", "reason": "",
        }

        match = _resolve_pre_panel_v7(row, region)
        if match:
            _smart_v3_apply_match(record, match)
            records.append(record)
            continue

        if panel_epg_id and panel_info.get("status") == "USABLE":
            record.update({
                "action": "KEEP_PANEL", "source": "panel", "epg_id": panel_epg_id,
                "epg_feed": "server xmltv.php",
                "reason": "Panel XMLTV contains useful current/future programme information",
            })
            records.append(record)
            continue

        match = _resolve_post_panel_v7(row, region, explicit_region)
        if match:
            _smart_v3_apply_match(record, match)
        else:
            record.update({
                "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
                "reason": _smart_v3_unmatched_reason(region),
            })
        records.append(record)

    return pd.DataFrame(records)


def classify_review_row_v7(row: dict[str, Any]) -> dict[str, Any]:
    region, explicit_region, _route_reason = detect_route(row)
    match = _resolve_pre_panel_v7(row, region)
    if not match:
        match = _resolve_post_panel_v7(row, region, explicit_region)
    if not match:
        match = {
            "action": "UNMATCHED", "source": "", "epg_id": "", "epg_feed": "",
            "reason": _smart_v3_unmatched_reason(region),
        }
    return {
        "proposed_action": match.get("action", ""),
        "proposed_source": match.get("source", ""),
        "proposed_epg_id": match.get("epg_id", ""),
        "proposed_epg_feed": match.get("epg_feed", ""),
        "proposed_best_score": match.get("best_score", ""),
        "proposed_second_epg_id": match.get("second_epg_id", ""),
        "proposed_second_epg_feed": match.get("second_epg_feed", ""),
        "proposed_second_score": match.get("second_score", ""),
        "proposed_score_margin": match.get("score_margin", ""),
        "proposed_reason": match.get("reason", ""),
        "proposed_region": region,
    }


def run_smart_rules_v7_self_test(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> pd.DataFrame:
    """Catalog-backed regression and safety tests for the active v7 matcher."""
    positive_cases = [
        ("Zee Punjab Haryana Himachal", "INR | Punjabi", "IN-NEWS | Zee Punjab Haryana Himachal Pradesh", "", "AUTO_EPGSHARE", "ZEE.PUNJAB.HARYANA.HIMACHAL.in"),
        ("Colors Kannada Movies", "INR | Kannada", "IN-KN | Colors Cinema", "shemaroo.in", "AUTO_EPGSHARE", "COLORS.KANNADA.MOVIES.in"),
        ("Sony Entertainment Television", "IN | Entertainment", "UK-SONY TV FHD", "sonytv.uk", "AUTO_EPGSHARE", "Sony.Entertainment.Television.in"),
        ("And Flix Hindi", "IN | Entertainment", "IN | & Flix Hindi FHD", "", "AUTO_EPGSHARE", "and.Flix.HD.in"),
        ("Sonic Hindi default", "IN | Entertainment", "IN | Sonic", "", "AUTO_EPGSHARE", "Sonic.Hindi.in"),
        ("Sonic Malayalam category", "INR | Malayalam", "IN-MY | Sonic KIds", "", "AUTO_EPGSHARE", "Sonic.Malayalam.in"),
        ("News18 UP Uttarakhand", "IN | News", "IN-NEWS | News18 Up/Uttarkand", "news18upuk.in", "AUTO_EPGSHARE", "News18.UP.in"),
        ("Udaya provider noise", "INR | Kannada", "BD | TS Udaya-Tv-Hdxxxxxx", "", "AUTO_EPGSHARE", "Udaya.HD.in"),
        ("Utsav Gold UK schedule", "IN | Entertainment", "IN | Utsav Gold", "", "AUTO_EPGSHARE", "Utsav.Gold.HD.uk"),
        ("Babulnaath exact devotional ID", "IN | Religious", "IN | Shri Babulnaath Temple Mumbai", "", "AUTO_EPGSHARE", "Babulnaath.Mumbai.in"),
        ("Altitude Sports alternate", "US | Sports", "US Altitude Sports Alternate", "", "AUTO_EPGSHARE", "Altitude.Sports.us2"),
        ("KPBS call sign", "US | Entertainment", "US KPBS (SAN DIEGO)", "", "AUTO_EPGSHARE", "KPBS-DT.us_locals1"),
        ("KUSIDT compact call sign", "US | Entertainment", "US KUSI (KUSIDT)", "", "AUTO_EPGSHARE", "KUSI-DT.us_locals1"),
        ("KTLA call sign", "US | Locals", "US KTLA 5 Los Angeles (L)", "", "AUTO_EPGSHARE", "KTLA-DT.us_locals1"),
        ("Discovery West", "US | Entertainment", "US Discovery (West)", "", "AUTO_EPGSHARE", "The.Discovery.Channel.HD.(Pacific).us2"),
        ("Discovery East", "US | Entertainment", "US Discovery (East)", "", "AUTO_EPGSHARE", "Discovery.Channel.HD.us2"),
        ("GAME plus", "US | Entertainment", "US GAME+", "", "AUTO_EPGSHARE", "Game+.Game.Plus.HD.us2"),
        ("HGTV East", "US | Entertainment", "US HGTV (East)", "", "AUTO_EPGSHARE", "Home.and.Garden.Television.HD.us2"),
        ("HGTV West", "US | Entertainment", "US HGTV (West)", "", "AUTO_EPGSHARE", "Home.and.Garden.Television.HD.(Pacific).us2"),
        ("CBS News", "US | CBS", "US CBS News", "", "AUTO_EPGSHARE", "CBS.News.National.Stream.us2"),
        ("SNY literal SportsNet identity", "US | Sports", "US SportsNet New York", "", "AUTO_EPGSHARE", "SNY.SportsNet.New.York.HD.us2"),
        ("Spectrum SportsNet LA", "US | Spectrum", "(SP2) US Spectrum SportsNet LA", "", "AUTO_EPGSHARE", "Spectrum.SportsNet.LA.Dodgers.HD.us2"),
        ("USA Network brand preserved", "US | Entertainment", "US USA Network (East)", "", "AUTO_EPGSHARE", "USA.Network.HD.us2"),
        ("MTV2 East", "US | Entertainment", "US MTV2 (East)", "", "AUTO_EPGSHARE", "MTV2:.Music.Television.HD.us2"),
        ("ABC National Feed Central", "US | Entertainment", "US ABC National Feed (Central)", "", "AUTO_EPGSHARE", "ABC.National.Feed.us2"),
        ("Showtime West current schedule", "US | Movies", "US Showtime (West)", "", "AUTO_EPGSHARE", "Paramount+.with.Showtime.HD.(Pacific).us2"),
        ("Lifetime Movie Network", "US | Movies", "US Lifetime Movie Network (LMN)", "", "AUTO_EPGSHARE", "LMN.HD.us2"),
        ("Daystar English", "US | Entertainment", "US Daystar", "", "AUTO_EPGSHARE", "Daystar.Television.Network.HD.[CHARTER].us2"),
        ("beIN Spanish UTF-8", "US | Sports", "US (Spanish) beIN SPORTS en EspaÃ±ol", "", "AUTO_EPGSHARE", "beIN.Sports.En.Español.HD.us2"),
        ("beIN Sports 4 custom source", "US | Sports", "US beIN SPORTS 4", "", "AUTO_EPGSHARE", "beIN_SPORTS4_DIGITAL_Mono_EN.bein"),
        ("Bally Carolinas to FanDuel", "US | Sports", "US Bally Sports Carolinas", "", "AUTO_EPGSHARE", "FanDuel.Sports.Network.South-.Carolinas.us"),
        ("PBS Michiana", "US | Entertainment", "US PBS Michiana", "", "AUTO_EPGSHARE", "WNIT-DT.us_locals1"),
        ("PBS North Carolina", "US | Entertainment", "US PBS North Carolina", "", "AUTO_EPGSHARE", "WUNC-DT.20.us_locals1"),
        ("Think TV 16 PBS call sign", "US | Entertainment", "US Think TV16", "", "AUTO_EPGSHARE", "WPTO-DT.us_locals1"),
        ("Toronto Prime Asia", "US | Entertainment", "IN (TO) Prime Asia", "", "AUTO_EPGSHARE", "Prime.Asia.TV.SD.ca2"),
        ("Toronto Rai Italia", "US | Entertainment", "IT (TO) Rai Italia", "", "AUTO_EPGSHARE", "Rai.Italia.ca2"),
        ("US Indian Star Plus CA fallback", "US | Entertainment", "US (IN) Star Plus", "", "AUTO_EPGSHARE", "ATN.Star.Plus.ca2"),
        ("Hollywood Suite 70s", "CA | Movies", "CA Hollywood Suite 70s", "", "AUTO_EPGSHARE", "Hollywood.Suite.70s+.ca2"),
        ("ICI Tele provider duplicate", "CA | General", "CA (FR) ICI Tele 2 HD", "", "AUTO_EPGSHARE", "ICI.Tele.HD.ca2"),
        ("TSN5 provider duplicate", "CA | Sports", "CA TSN 5 Blackout 2", "", "AUTO_EPGSHARE", "TSN.5.HD.ca2"),
        ("CBC numbered event", "CA | Sports", "CBC 10:", "CBC 10:", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("Music Choice dummy", "US | Entertainment", "US Music Choice: Kidz Only!", "", "AUTO_DUMMY", "Music.Choice.Dummy.us"),
        ("Adult regression", "XXX | Adults", "+18 | Private", "", "AUTO_DUMMY", "Adult.Programming.Dummy.us"),
        ("WNBA regression", "US | WNBA", "WNBA 7", "", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("MLB Big Inning event", "US | Entertainment", "US MLB Big Inning", "", "AUTO_DUMMY", "PPV.EVENTS.Dummy.us"),
        ("CTV2 regression", "CA | General", "CA CTV2 TORONTO HD", "", "AUTO_EPGSHARE", "CTV.Two.-.Toronto.ca2"),
        ("NOW 90s and 00s", "UK | Music", "Now 90s & 00s", "", "AUTO_EPGSHARE", "NOW.90s00s.uk"),
        ("Spectrum Worcester", "US | Spectrum", "US Spectrum News 1 Worcester", "", "AUTO_EPGSHARE", "Spectrum.News.1.-.(MA).Worcester.-.STVA.us2"),
    ]

    safety_cases = [
        ("Do not treat Doctor Who as WHO call sign", "US | Entertainment", "US BBC Classic Doctor Who", "", {"WHO-DT.us_locals1"}),
        ("Do not treat NTV Bangla as Newfoundland NTV", "CA | Canada", "CA (Asian) NTV BANGLA", "", {"NTV.ca2", "NTV.HD.ca2"}),
        ("Do not force VH1 West to the East/default guide", "US | Music", "US VH1 (West)", "", {"VH1.HD.us2"}),
        ("Do not reduce MTV Beats to generic MTV", "IN | Music", "IN | MTV Beats FHD", "mtvbeatshd.in", {"MTV.in"}),
        ("Do not turn CTV Ottawa into CTV2 Ottawa", "CA | Canada", "CA CTV Ottawa HD", "", {"CTV.Two.-.Ottawa.ca2"}),
        ("Do not use generic Starz Encore Pacific for Action West", "US | Movies", "US Starz Encore Action (West)", "", {"Starz.Encore.(Pacific).us2"}),
    ]

    all_cases: list[tuple[str, str, str, str]] = []
    for label, category, channel, panel_id, _action, _epg_id in positive_cases:
        all_cases.append((label, category, channel, panel_id))
    for label, category, channel, panel_id, _forbidden in safety_cases:
        all_cases.append((label, category, channel, panel_id))

    channels: list[dict[str, str]] = []
    categories: dict[str, str] = {}
    for index, (_label, category, channel, panel_id) in enumerate(all_cases, start=1):
        cid = str(index)
        categories[cid] = category
        channels.append({
            "stream_id": str(index), "category_id": cid,
            "name": channel, "epg_channel_id": panel_id,
        })

    report = make_matching_report_v7("self_test", channels, categories, {}, real_candidates, dummy_ids)
    rows: list[dict[str, Any]] = []

    for index, case in enumerate(positive_cases):
        label, _category, _channel, _panel, expected_action, expected_id = case
        actual = report.iloc[index]
        passed = (
            actual["action"] == expected_action
            and str(actual["epg_id"]).casefold() == expected_id.casefold()
        )
        rows.append({
            "test": label, "check_type": "positive", "passed": passed,
            "expected_action": expected_action, "actual_action": actual["action"],
            "expected_epg_id": expected_id, "forbidden_epg_ids": "",
            "actual_epg_id": actual["epg_id"], "reason": actual["reason"],
        })

    offset = len(positive_cases)
    for safety_index, case in enumerate(safety_cases):
        label, _category, _channel, _panel, forbidden_ids = case
        actual = report.iloc[offset + safety_index]
        actual_id = str(actual["epg_id"] or "")
        forbidden_folded = {item.casefold() for item in forbidden_ids}
        forced_actions = {"AUTO_EPGSHARE", "KEEP_PANEL", "MANUAL", "APPROVED"}
        passed = not (str(actual["action"]) in forced_actions and actual_id.casefold() in forbidden_folded)
        rows.append({
            "test": label, "check_type": "safety", "passed": passed,
            "expected_action": "not forced to forbidden mapping",
            "actual_action": actual["action"], "expected_epg_id": "",
            "forbidden_epg_ids": " | ".join(sorted(forbidden_ids)),
            "actual_epg_id": actual_id, "reason": actual["reason"],
        })

    result = pd.DataFrame(rows)
    failures = result.loc[~result["passed"]]
    if not failures.empty:
        display(failures)
        raise RuntimeError("Smart Rules v7 self-test failed. Stop before matching the server.")
    return result

make_matching_report_v7.smart_rules_version = SMART_RULES_VERSION
make_matching_report_v7.matcher_build_id = MATCHER_BUILD_ID
globals().pop("make_matching_report", None)
print(f"ACTIVE MATCHER: Smart Rules v{SMART_RULES_VERSION} ({MATCHER_BUILD_ID})")
