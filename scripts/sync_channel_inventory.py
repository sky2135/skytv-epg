#!/usr/bin/env python3
"""Discover live provider channels and safely maintain private Google Sheets.

This inventory phase runs before the EPG build. It reads the complete live-
channel inventory for each configured Xtream server, compares exact
``(server_id, stream_id)`` identities with the private Google Sheet, and applies
the fail-closed Version 1 matcher to previously unseen channels and, only when
explicitly requested, an allowlist of existing disabled ``REVIEW`` rows. Exact
matches with a strong guide may be enabled; every uncertain result stays
disabled as ``REVIEW``. Existing non-REVIEW rows are never edited or deleted.

Severe stream-ID reuse warnings are appended to the private ``Sync Alerts``
tab. An ``OPEN`` alert keeps that stream quarantined from effective builds until
the mapping is corrected and the user marks the alert ``RESOLVED``.

The production workflow controls new-row appends and existing REVIEW updates
separately. Both default to non-destructive behavior.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urljoin, urlparse, urlunparse

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_epg_streaming as streaming  # noqa: E402
import auto_match_inventory as automatch  # noqa: E402
import ai_review_gemini as gemini_review  # noqa: E402
import ai_review_policy as ai_policy  # noqa: E402

try:  # Optional unless the explicit native REVIEW lane is enabled.
    import native_epg_review as native_review  # type: ignore[import-not-found]  # noqa: E402
except ImportError:  # pragma: no cover - exercised through the controlled gate.
    native_review = None


SYNC_VERSION = "1.1"
DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
MAX_PANEL_JSON_BYTES = 64 * 1024 * 1024
MAX_M3U_BYTES = 128 * 1024 * 1024
MAX_CHANNELS_PER_SERVER = 250_000
MAX_SHEET_BYTES = 80 * 1024 * 1024
MAX_GOOGLE_MAPPING_ROWS = 150_000
MAX_SYNC_ALERT_ROWS = 10_000
MAX_SYNC_ALERT_BYTES = 8 * 1024 * 1024
# The current three-server backlog is roughly 25,000 eligible rows.  Keep a
# hard memory/CPU bound while allowing the workflow's explicit ``all`` scope
# to analyze that backlog in one pass.
MAX_RECHECK_CANDIDATES = 30_000
# Deterministic approvals are resumable, so one run may drain a substantial
# verified backlog without relying on repeated manual workflow dispatches.
# Google mutations remain independently bounded below: no request may carry
# more than 500 rows or exceed the encoded 2 MiB ceiling.
MAX_RECHECK_APPLIES_PER_RUN = 5_000
MAX_RECHECK_UPDATE_ROWS_PER_BATCH = 500
MAX_AI_REVIEW_ROWS = 200
MAX_RECHECK_TOTAL_UPDATES = MAX_RECHECK_APPLIES_PER_RUN + MAX_AI_REVIEW_ROWS
MAX_RECHECK_UPDATE_REQUEST_BYTES = 2 * 1024 * 1024
# Retained as a public compatibility name for tests/integrations which inspect
# the conservative request ceiling. The limit now applies to every batch, not
# cumulatively to the entire run.
MAX_RECHECK_DETERMINISTIC_REQUEST_BYTES = MAX_RECHECK_UPDATE_REQUEST_BYTES
MAX_PROVIDER_TEXT_INPUT_CHARS = 4_096
MAX_PROVIDER_TEXT_VARIANT_CHARS = 16_384
MAX_PROVIDER_PERCENT_DECODE_ROUNDS = 4
MAX_PROVIDER_TEXT_VARIANTS = 128
REVIEW_QUEUE_ACTIONS = frozenset({"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED"})
RECHECK_PATCH_COLUMNS = (
    "enabled",
    "action",
    "source",
    "epg_feed",
    "epg_id",
    "reason",
    "notes",
)
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
GOOGLE_CANONICAL_ERROR_STATUSES = frozenset(
    {
        "ABORTED",
        "ALREADY_EXISTS",
        "CANCELLED",
        "DATA_LOSS",
        "DEADLINE_EXCEEDED",
        "FAILED_PRECONDITION",
        "INTERNAL",
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "OUT_OF_RANGE",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "UNAUTHENTICATED",
        "UNAVAILABLE",
        "UNIMPLEMENTED",
        "UNKNOWN",
    }
)
ALERT_COLUMNS = (
    "detected_at",
    "server_id",
    "stream_id",
    "alert_type",
    "sheet_channel_name",
    "provider_channel_name",
    "sheet_category_name",
    "provider_category_name",
    "action_taken",
    "status",
    "review_notes",
)


class SyncError(RuntimeError):
    """A controlled error whose text must never contain provider credentials."""


class SheetWriteError(SyncError):
    """A Google Sheet write failure with conservative committed-row accounting."""

    def __init__(self, message: str, appended_count: int = 0):
        super().__init__(message)
        self.appended_count = int(appended_count)


@dataclass(frozen=True)
class ServerConfig:
    server_id: str
    server_label: str
    base_url: str
    username: str
    password: str


@dataclass
class PanelProbe:
    payload: Any = None
    issue: str = ""
    status_code: int | None = None


@dataclass
class PanelInventory:
    server_id: str
    server_label: str
    categories: list[dict[str, Any]]
    channels: list[dict[str, Any]]
    source: str
    diagnostics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderReflectionPolicy:
    """Pre-persistence checks with asymmetric username/password handling."""

    global_needles: tuple[str, ...]
    structured_username_patterns: tuple[re.Pattern[str], ...]


@dataclass
class MappingTable:
    raw_headers: list[str]
    headers: list[str]
    rows: list[dict[str, str]]
    row_numbers: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.row_numbers:
            self.row_numbers = list(range(2, len(self.rows) + 2))
        if len(self.row_numbers) != len(self.rows):
            raise ValueError("Mapping rows and physical row numbers must align.")

    @property
    def keys(self) -> set[tuple[str, str]]:
        return {
            (
                streaming.normalize_server_id(row.get("server_id", "")),
                streaming.clean_identifier(row.get("stream_id", ""), 120),
            )
            for row in self.rows
        }


@dataclass(frozen=True)
class GoogleSheetLayout:
    numeric_sheet_id: int
    title: str
    table_id: str | None
    table_end_row: int | None
    grid_row_count: int | None
    grid_column_count: int | None
    table_has_footer: bool
    basic_filter: dict[str, Any] | None


def first_present(item: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in item:
            value = item.get(key)
            if value is not None and str(value).strip():
                return value
    return ""


def normalize_panel_base_url(value: str, *, allow_insecure_http: bool) -> str:
    """Return a credential-free Xtream base URL while retaining port/path."""
    cleaned = str(value or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise SyncError("A server base URL is invalid; include http:// or https://.")
    if parsed.scheme.casefold() == "http" and not allow_insecure_http:
        raise SyncError(
            "A server uses unencrypted HTTP. Use HTTPS or explicitly enable "
            "ALLOW_INSECURE_PANEL_HTTP after accepting the credential-exposure risk."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SyncError(
            "A server base URL must not contain a username, password, query, or fragment."
        )
    path = re.sub(
        r"/(?:player_api\.php|panel_api\.php|xmltv\.php|get\.php)/?$",
        "",
        parsed.path or "",
        flags=re.IGNORECASE,
    ).rstrip("/")
    return urlunparse((parsed.scheme.casefold(), parsed.netloc, path, "", "", ""))


def panel_base_candidates(base_url: str, *, allow_insecure_http: bool) -> list[str]:
    normalized = normalize_panel_base_url(
        base_url, allow_insecure_http=allow_insecure_http
    )
    parsed = urlparse(normalized)
    result = [normalized]
    if parsed.path and parsed.path != "/":
        origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        if origin not in result:
            result.append(origin)
    return result


def advertised_same_host_bases(
    payload: Any,
    current_base: str,
    *,
    allow_insecure_http: bool,
) -> list[str]:
    """Use only same-host ports advertised by authenticated account metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("server_info"), dict):
        return []
    server_info = payload["server_info"]
    current = urlparse(current_base)
    current_host = (current.hostname or "").casefold()
    advertised_raw = streaming.clean_text(
        server_info.get("url")
        or server_info.get("host")
        or server_info.get("hostname")
        or "",
        300,
    )
    if advertised_raw:
        advertised = urlparse(
            advertised_raw if "://" in advertised_raw else f"//{advertised_raw}",
            scheme=current.scheme,
        )
        advertised_host = advertised.hostname or ""
    else:
        advertised_host = current.hostname or ""
    if not advertised_host or advertised_host.casefold() != current_host:
        return []

    protocol = streaming.clean_text(
        server_info.get("server_protocol") or current.scheme, 10
    ).casefold()
    if protocol not in {"http", "https"}:
        protocol = current.scheme
    candidates: list[str] = []
    for scheme, raw_port in (
        ("https", server_info.get("https_port")),
        (protocol, server_info.get("port")),
    ):
        if scheme == "http" and not allow_insecure_http:
            continue
        port_text = str(raw_port or "").strip()
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            continue
        port = int(port_text)
        default_port = (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        )
        netloc = advertised_host if default_port else f"{advertised_host}:{port}"
        candidate = urlunparse(
            (scheme, netloc, current.path.rstrip("/"), "", "", "")
        )
        if candidate != current_base and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def response_body_limited(response: Any, maximum_bytes: int) -> bytes:
    declared = str(getattr(response, "headers", {}).get("Content-Length", "")).strip()
    if declared.isdigit() and int(declared) > maximum_bytes:
        raise SyncError("A provider response exceeded its configured size limit.")
    chunks: list[bytes] = []
    total = 0
    if callable(getattr(response, "iter_content", None)):
        iterator = response.iter_content(chunk_size=1024 * 1024)
    else:
        iterator = [bytes(getattr(response, "content", b"") or b"")]
    for chunk in iterator:
        if not chunk:
            continue
        data = bytes(chunk)
        total += len(data)
        if total > maximum_bytes:
            raise SyncError("A provider response exceeded its configured size limit.")
        chunks.append(data)
    return b"".join(chunks)


def close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def safe_credentialed_get(
    session: Any,
    endpoint: str,
    *,
    params: Mapping[str, str],
    maximum_bytes: int,
    timeout: tuple[int, int],
    allow_insecure_http: bool,
) -> tuple[int, bytes]:
    """GET a credentialed endpoint without following an unsafe redirect."""
    current_url = endpoint
    original_host = (urlparse(endpoint).hostname or "").casefold()
    for _redirect in range(4):
        response = None
        try:
            response = session.get(
                current_url,
                params=dict(params),
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status not in REDIRECT_STATUSES:
                body = response_body_limited(response, maximum_bytes)
                return status, body
            location = str(getattr(response, "headers", {}).get("Location", "")).strip()
            if not location:
                raise SyncError("A provider returned an invalid redirect.")
            next_url = urljoin(current_url, location)
            parsed = urlparse(next_url)
            if (
                parsed.scheme not in {"http", "https"}
                or (parsed.hostname or "").casefold() != original_host
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.scheme == "http" and not allow_insecure_http)
            ):
                raise SyncError("A provider attempted an unsafe credential redirect.")
            current_url = next_url
        except SyncError:
            raise
        except Exception:
            # Network exception strings commonly include the full credential URL.
            raise SyncError("A provider network request failed.") from None
        finally:
            if response is not None:
                close_response(response)
    raise SyncError("A provider exceeded the safe redirect limit.")


def safe_payload_shape(payload: Any) -> str:
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    if isinstance(payload, dict):
        return "object"
    return type(payload).__name__


def auth_problem(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    user_info = payload.get("user_info")
    if isinstance(user_info, dict):
        auth = str(user_info.get("auth") or "").strip().casefold()
        if auth in {"0", "false", "no", "denied", "invalid"}:
            return "The panel rejected its configured credentials."
        status = str(user_info.get("status") or "").strip().casefold()
        if status and status not in {"active", "enabled", "ok", "trial", "true", "1"}:
            return "The panel account is not active."
    auth = str(payload.get("auth") or "").strip().casefold()
    if auth in {"0", "false", "no", "denied", "invalid"}:
        return "The panel rejected its configured credentials."
    if payload.get("error") not in (None, "", 0, False, "0", "false", "False"):
        return "The panel returned an account or API error."
    return ""


def normalize_category_row(
    item: Mapping[str, Any], *, allow_generic_id: bool
) -> dict[str, Any] | None:
    category_id = first_present(
        item, ("category_id", "categoryId", "categoryID", "group_id", "groupId")
    )
    if not category_id and allow_generic_id:
        category_id = first_present(item, ("id",))
    category_name = first_present(
        item,
        ("category_name", "categoryName", "group_name", "groupName", "name", "title"),
    )
    if not str(category_id).strip():
        return None
    return {
        "category_id": streaming.clean_identifier(category_id, 120),
        "category_name": streaming.clean_text(category_name, 200),
    }


def normalize_stream_row(
    item: Mapping[str, Any], *, allow_generic_id: bool
) -> dict[str, Any] | None:
    stream_id = first_present(
        item,
        ("stream_id", "streamId", "channel_id", "channelId", "live_id", "liveId"),
    )
    if not stream_id and allow_generic_id:
        stream_id = first_present(item, ("id",))
    name = first_present(
        item,
        (
            "name",
            "stream_display_name",
            "streamDisplayName",
            "channel_name",
            "channelName",
            "title",
        ),
    )
    stream_id_text = streaming.clean_identifier(stream_id, 120)
    name_text = streaming.clean_identifier(name, 300)
    if bool(stream_id_text) != bool(name_text):
        raise SyncError(
            "A provider live-stream row has an ID or name but not both; "
            "inventory sync stopped rather than silently dropping it."
        )
    if not stream_id_text:
        if allow_generic_id and item:
            raise SyncError(
                "A provider live-stream collection contained a non-channel row; "
                "inventory sync stopped rather than silently dropping it."
            )
        return None
    epg_id_keys = (
        "epg_channel_id",
        "epgChannelId",
        "epg_id",
        "epgId",
        "tvg_id",
        "tvg-id",
    )
    raw_epg_values = [
        str(item.get(key))
        for key in epg_id_keys
        if key in item
        and item.get(key) is not None
        and str(item.get(key)).strip()
    ]
    raw_epg_channel_id = first_present(item, epg_id_keys)
    raw_epg_channel_id_text = str(raw_epg_channel_id or "")
    conflicting_epg_aliases = len(set(raw_epg_values)) > 1
    epg_channel_id = (
        ""
        if conflicting_epg_aliases
        else streaming.clean_identifier(raw_epg_channel_id_text, 300)
    )
    return {
        "stream_id": stream_id_text,
        "name": name_text,
        "category_id": streaming.clean_identifier(
            first_present(item, ("category_id", "categoryId", "group_id", "groupId")),
            120,
        ),
        "category_name": streaming.clean_text(
            first_present(
                item,
                ("category_name", "categoryName", "group_title", "group-title"),
            ),
            200,
        ),
        "epg_channel_id": epg_channel_id,
        # Private integrity marker: native auto-approval may use the ID only
        # when ingestion did not trim, clean, or truncate the provider value.
        "_native_epg_id_exact": (
            not conflicting_epg_aliases
            and raw_epg_channel_id_text == epg_channel_id
        ),
        "_native_epg_id_raw_present": bool(raw_epg_channel_id_text),
        "num": streaming.clean_text(
            first_present(
                item,
                (
                    "num",
                    "channel_number",
                    "channelNumber",
                    "chno",
                    "tvg_chno",
                    "tvg-chno",
                ),
            ),
            40,
        ),
        # Provider icon URLs sometimes embed usernames/passwords in their path.
        # Never copy them into a Sheet whose values later feed public app metadata.
        "stream_icon": "",
    }


def preferred_payload_keys(action: str) -> tuple[str, ...]:
    if action == "get_live_categories":
        return (
            "categories",
            "live_categories",
            "liveCategories",
            "data",
            "results",
            "items",
        )
    return (
        "streams",
        "live_streams",
        "liveStreams",
        "channels",
        "data",
        "results",
        "items",
    )


def normalize_panel_action_rows(payload: Any, action: str) -> list[dict[str, Any]]:
    """Extract schema-valid rows and reject account metadata masquerading as data."""
    if action not in {"get_live_categories", "get_live_streams"}:
        raise ValueError(f"Unsupported panel action: {action}")
    collections: list[tuple[list[Any], bool, bool]] = []
    if isinstance(payload, list):
        collections.append((payload, True, True))
    elif isinstance(payload, dict):
        for key in preferred_payload_keys(action):
            value = payload.get(key)
            if isinstance(value, list):
                collections.append((value, True, True))
            elif isinstance(value, dict):
                nested = list(value.values())
                if nested and all(isinstance(row, dict) for row in nested):
                    collections.append((nested, True, True))
        collections.append(([payload], False, False))
        if payload and all(isinstance(value, dict) for value in payload.values()):
            keys = [str(key) for key in payload]
            if all(re.fullmatch(r"[A-Za-z]*\d+", key) for key in keys):
                collections.append((list(payload.values()), True, True))

    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for collection, allow_generic_id, explicit_collection in collections:
        seen_in_collection: set[str] = set()
        for raw in collection:
            if not isinstance(raw, Mapping):
                if action == "get_live_streams" and explicit_collection:
                    raise SyncError(
                        "A provider live-stream collection contained a non-object item; "
                        "the API result was rejected so a safe fallback can be tried."
                    )
                continue
            if action == "get_live_streams" and explicit_collection and not raw:
                raise SyncError(
                    "A provider live-stream collection contained an empty item; "
                    "the API result was rejected so a safe fallback can be tried."
                )
            if action == "get_live_categories":
                normalized = normalize_category_row(raw, allow_generic_id=allow_generic_id)
                identity_key = "category_id"
            else:
                normalized = normalize_stream_row(raw, allow_generic_id=allow_generic_id)
                identity_key = "stream_id"
            if normalized is None:
                if action == "get_live_streams" and explicit_collection:
                    raise SyncError(
                        "A provider live-stream collection contained an unparseable item; "
                        "the API result was rejected so a safe fallback can be tried."
                    )
                continue
            identity = str(normalized[identity_key]).casefold()
            if identity in seen_in_collection:
                raise SyncError(
                    f"A provider returned duplicate {identity_key} values in one inventory."
                )
            seen_in_collection.add(identity)
            previous = seen.get(identity)
            if previous is not None:
                # Some panels expose the same collection through more than one
                # wrapper alias (for example both ``streams`` and ``data``).
                # Identical aliases are harmless, but selecting the first of
                # two conflicting records would silently discard provider data.
                if previous != normalized:
                    raise SyncError(
                        f"A provider returned conflicting {identity_key} values "
                        "across inventory collections."
                    )
                continue
            seen[identity] = normalized
            rows.append(normalized)
            if len(rows) > MAX_CHANNELS_PER_SERVER:
                raise SyncError("A provider returned too many inventory rows.")
    return rows


def request_panel_json(
    session: Any,
    endpoint: str,
    username: str,
    password: str,
    action: str,
    *,
    allow_insecure_http: bool,
) -> PanelProbe:
    try:
        status, content = safe_credentialed_get(
            session,
            endpoint,
            params={"username": username, "password": password, "action": action},
            maximum_bytes=MAX_PANEL_JSON_BYTES,
            timeout=(20, 180),
            allow_insecure_http=allow_insecure_http,
        )
    except SyncError as exc:
        return PanelProbe(issue=f"{action}: {exc}")
    if status != 200:
        suffix = " (credentials or API access rejected)" if status in {401, 403} else ""
        return PanelProbe(issue=f"{action}: HTTP {status}{suffix}", status_code=status)
    try:
        payload = json.loads(content.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return PanelProbe(
            issue=f"{action}: panel returned non-JSON data", status_code=status
        )
    return PanelProbe(payload=payload, status_code=status)


def derive_categories_from_channels(
    channels: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for channel in channels:
        category_id = streaming.clean_identifier(channel.get("category_id", ""), 120)
        if not category_id or category_id.casefold() in seen:
            continue
        result.append(
            {
                "category_id": category_id,
                "category_name": streaming.clean_text(
                    channel.get("category_name", ""), 200
                ),
            }
        )
        seen.add(category_id.casefold())
    return result


def split_extinf(line: str) -> tuple[str, str]:
    quote_character = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote_character == character:
                quote_character = ""
            elif not quote_character:
                quote_character = character
            continue
        if character == "," and not quote_character:
            return line[:index], line[index + 1 :]
    return line, ""


M3U_ATTRIBUTE_RE = re.compile(
    r"([A-Za-z0-9_.:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s,]+))"
)


def m3u_attributes(metadata: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in M3U_ATTRIBUTE_RE.finditer(metadata):
        value = next((group for group in match.groups()[1:] if group is not None), "")
        key = match.group(1).casefold()
        if key in result:
            raise SyncError(
                "An authenticated M3U entry contains a duplicate attribute."
            )
        # Preserve the quoted bytes as decoded text. Downstream display fields
        # clean their own values, while native EPG IDs must retain evidence of
        # any trimming/normalization so they can fail closed.
        result[key] = value
    return result


def m3u_category_id(group_title: str) -> str:
    digest = hashlib.sha1(group_title.casefold().encode("utf-8")).hexdigest()[:12]
    return f"m3u_group_{digest}"


def provider_text_variants(value: object) -> frozenset[str]:
    """Return bounded raw, recursively decoded, and URL-rendered forms.

    Provider payloads have been observed to percent-encode credentials more
    than once, and HTML entities are another transport spelling which can hide
    the same private value. Decode RFC percent escapes, form-style ``+``
    escapes, and HTML entities as one bounded closure so mixed encodings (for
    example, a percent-encoded ``&amp;``) cannot bypass persistence guards.
    Fail closed if another layer remains. The fixed input, variant, and
    per-value character ceilings prevent a hostile provider field from turning
    this safety check into unbounded work.
    """
    text = str(value or "")
    if not text:
        return frozenset()
    if len(text) > MAX_PROVIDER_TEXT_INPUT_CHARS:
        raise SyncError(
            "A provider credential, URL, or returned field exceeds the "
            "fixed safety-scan size limit."
        )

    decoded_forms: set[str] = {text}
    frontier: set[str] = {text}
    def decode_html(current: str) -> str:
        decoded = html.unescape(current)
        # ``html.unescape`` deliberately substitutes U+FFFD for invalid
        # numeric references such as an out-of-range Unicode code point. Such
        # malformed transport data is not safe evidence to persist.
        if decoded != current and (
            decoded.count("\ufffd") > current.count("\ufffd")
            or "\x00" in decoded
        ):
            raise SyncError(
                "A provider credential, URL, or returned field contains a "
                "malformed HTML entity."
            )
        return decoded

    def decode_percent(current: str) -> str:
        return unquote(current, encoding="utf-8", errors="strict")

    def decode_form(current: str) -> str:
        return unquote_plus(current, encoding="utf-8", errors="strict")

    decoders = (decode_percent, decode_form, decode_html)
    for _round in range(MAX_PROVIDER_PERCENT_DECODE_ROUNDS):
        next_frontier: set[str] = set()
        for current in sorted(frontier):
            for decoder in decoders:
                try:
                    decoded = decoder(current)
                except (UnicodeDecodeError, ValueError):
                    raise SyncError(
                        "A provider credential, URL, or returned field contains "
                        "malformed percent-encoded text."
                    ) from None
                if decoded == current or decoded in decoded_forms:
                    continue
                if len(decoded) > MAX_PROVIDER_TEXT_VARIANT_CHARS:
                    raise SyncError(
                        "A provider text variant exceeds the fixed safety-scan "
                        "size limit."
                    )
                decoded_forms.add(decoded)
                next_frontier.add(decoded)
                if len(decoded_forms) > MAX_PROVIDER_TEXT_VARIANTS:
                    raise SyncError(
                        "A provider field exceeds the fixed safety-scan variant "
                        "limit."
                    )
        if not next_frontier:
            frontier = set()
            break
        frontier = next_frontier

    # More layers than the bounded decoder accepts must stop the run rather
    # than becoming a credential-reflection bypass.
    for current in sorted(frontier):
        for decoder in decoders:
            try:
                decoded = decoder(current)
            except (UnicodeDecodeError, ValueError):
                raise SyncError(
                    "A provider credential, URL, or returned field contains "
                    "malformed percent-encoded text."
                ) from None
            if decoded != current and decoded not in decoded_forms:
                raise SyncError(
                    "A provider field exceeds the transport-decoding safety limit."
                )

    variants = set(decoded_forms)
    for decoded in tuple(decoded_forms):
        for rendered in (quote(decoded, safe=""), quote_plus(decoded, safe="")):
            if len(rendered) > MAX_PROVIDER_TEXT_VARIANT_CHARS:
                raise SyncError(
                    "A provider text variant exceeds the fixed safety-scan size "
                    "limit."
                )
            variants.add(rendered)
            if len(variants) > MAX_PROVIDER_TEXT_VARIANTS:
                raise SyncError(
                    "A provider field exceeds the fixed safety-scan variant limit."
                )
    return frozenset(variant.casefold() for variant in variants if variant)


def gemini_provider_sensitive_values(
    server_configs: Sequence[ServerConfig],
) -> tuple[str, ...]:
    """Return bounded exact provider values for Gemini's final wire guard."""

    values: list[str] = []
    seen: set[str] = set()
    for config in server_configs:
        for raw_value in (config.username, config.password, config.base_url):
            value = str(raw_value or "")
            if not value or value in seen:
                continue
            if len(value) > gemini_review.MAX_SENSITIVE_VALUE_CHARS:
                raise SyncError(
                    "A configured provider value exceeds the Gemini safety-guard "
                    "size limit."
                )
            seen.add(value)
            values.append(value)
            if len(values) > gemini_review.MAX_SENSITIVE_VALUES:
                raise SyncError(
                    "Too many configured provider values were supplied to the "
                    "Gemini safety guard."
                )
    return tuple(values)


def _base64_provider_variants(value: str) -> frozenset[str]:
    """Return bounded nested base64 spellings for provider-reflection scans."""

    raw = str(value or "")
    if not raw:
        return frozenset()
    result: set[str] = set()
    frontier = {raw}
    for _round in range(MAX_PROVIDER_PERCENT_DECODE_ROUNDS):
        next_frontier: set[str] = set()
        for current in frontier:
            encoded = current.encode("utf-8")
            for rendered in (
                base64.b64encode(encoded).decode("ascii"),
                base64.urlsafe_b64encode(encoded).decode("ascii"),
            ):
                for variant in (rendered, rendered.rstrip("=")):
                    if variant and variant not in result:
                        result.add(variant)
                        next_frontier.add(variant)
        frontier = next_frontier
    return frozenset(result)


def distinctive_provider_value(value: str) -> bool:
    """Match the workflow's conservative high-entropy username heuristic."""
    raw = str(value or "").encode("utf-8")
    if len(raw) < 10:
        return False
    counts = Counter(raw)
    entropy_bits = -sum(
        count * math.log2(count / len(raw)) for count in counts.values()
    )
    text = str(value)
    classes = sum(
        (
            any(character.islower() for character in text),
            any(character.isupper() for character in text),
            any(character.isdigit() for character in text),
            any(not character.isalnum() for character in text),
        )
    )
    return entropy_bits >= 40 and (classes >= 2 or len(raw) >= 16)


def provider_reflection_needles(
    config: ServerConfig, service_bases: Sequence[str]
) -> ProviderReflectionPolicy:
    """Build an asymmetric provider-output reflection policy.

    Passwords and private provider hosts/URLs are always substring-scanned.
    Usernames receive global substring matching only when sufficiently
    distinctive. Common usernames are rejected only in credential-shaped
    labels and URL locations, so values such as ``News`` or stream ID ``1`` do
    not collide with equally ordinary provider data.
    """
    username = str(config.username or "").strip()
    password = str(config.password or "")
    if not username:
        raise SyncError("A configured provider username is empty.")
    if len(password) < 4:
        # Global password scanning is intentionally fail-safe. A shorter value
        # cannot be scanned usefully without matching ubiquitous text.
        raise SyncError(
            "A configured provider password is too short for safe output scanning."
        )

    global_values: set[str] = set(provider_text_variants(password))
    global_values.update(
        variant.casefold() for variant in _base64_provider_variants(password)
    )
    username_values = provider_text_variants(username)
    if distinctive_provider_value(username):
        global_values.update(username_values)
    # Even a common username is highly distinctive once encoded.  Scan only its
    # base64 forms globally; retain the existing structured check for its raw
    # spelling so ordinary channel names such as "News" are not rejected.
    global_values.update(
        variant.casefold() for variant in _base64_provider_variants(username)
    )
    for base in service_bases:
        normalized_base = str(base or "").rstrip("/")
        global_values.update(provider_text_variants(normalized_base))
        global_values.update(
            variant.casefold()
            for variant in _base64_provider_variants(normalized_base)
        )
        parsed = urlparse(normalized_base)
        global_values.update(provider_text_variants(parsed.netloc))
        global_values.update(provider_text_variants(parsed.hostname or ""))
        global_values.update(
            variant.casefold()
            for component in (parsed.netloc, parsed.hostname or "")
            for variant in _base64_provider_variants(component)
        )

    username_alternative = "(?:" + "|".join(
        re.escape(value)
        for value in sorted(username_values, key=lambda item: (-len(item), item))
    ) + ")"
    boundary = r"(?![A-Za-z0-9._~%+-])"
    label = r"(?:username|user|login|user_name)"
    patterns = [
        re.compile(
            r"[\"']?" + label + r"[\"']?[ \t\r\n]{0,16}[:=]"
            r"[ \t\r\n]{0,16}[\"']?" + username_alternative + boundary,
            re.IGNORECASE,
        ),
        re.compile(
            r"<[ \t]*" + label + r"(?:[ \t][^>]{0,64})?>"
            r"[ \t\r\n]{0,16}" + username_alternative + boundary,
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://" + username_alternative + r"(?::|@)",
            re.IGNORECASE,
        ),
        re.compile(
            r"/(?:live|movie|series)/" + username_alternative + r"(?:/|$)",
            re.IGNORECASE,
        ),
    ]
    basic_value = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    patterns.append(
        re.compile(r"\bbasic[ \t]+" + re.escape(basic_value) + boundary, re.IGNORECASE)
    )
    return ProviderReflectionPolicy(
        global_needles=tuple(
            sorted(global_values, key=lambda value: (-len(value), value))
        ),
        structured_username_patterns=tuple(patterns),
    )


def validate_provider_inventory_secret_safe(
    categories: Sequence[Mapping[str, Any]],
    channels: Sequence[Mapping[str, Any]],
    policy: ProviderReflectionPolicy,
) -> None:
    """Reject provider-reflected credentials/URLs before any persistence."""
    for collection in (categories, channels):
        for row in collection:
            for value in row.values():
                text = str(value if value is not None else "")
                folded_variants = provider_text_variants(text)
                if folded_variants and (
                    any(
                        needle in variant
                        for variant in folded_variants
                        for needle in policy.global_needles
                    )
                    or any(
                        pattern.search(variant)
                        for variant in folded_variants
                        for pattern in policy.structured_username_patterns
                    )
                ):
                    raise SyncError(
                        "A provider response reflected configured credentials or its "
                        "private service URL; inventory persistence was blocked."
                    )


def m3u_stream_id(
    url: str,
    name: str,
    tvg_id: str,
    *,
    username: str,
    password: str,
) -> str:
    parsed_path = urlparse(url).path
    decoded_segments = [unquote(part) for part in parsed_path.split("/") if part]
    # Xtream's stable stream identity, when present, is the terminal numeric
    # filename. Never scan earlier segments: a numeric username/password must
    # not accidentally become a public stream ID.
    if decoded_segments:
        terminal_stem = re.sub(
            r"\.[A-Za-z0-9]{1,6}$", "", decoded_segments[-1]
        )
        if (
            re.fullmatch(r"\d+", terminal_stem)
            and terminal_stem != str(username)
            and terminal_stem != str(password)
        ):
            return terminal_stem

    # Configured credentials are already logical text values; only URL path
    # segments need percent-decoding before the exact comparison.
    decoded_username = str(username)
    decoded_password = str(password)
    scan_embedded_username = distinctive_provider_value(decoded_username)
    safe_segments: list[str] = []
    for index, segment in enumerate(decoded_segments):
        segment_stem = (
            re.sub(r"\.[A-Za-z0-9]{1,6}$", "", segment)
            if index == len(decoded_segments) - 1
            else segment
        )
        if (
            (decoded_username and segment == decoded_username)
            or (decoded_password and segment == decoded_password)
            or (decoded_username and segment_stem == decoded_username)
            or (decoded_password and segment_stem == decoded_password)
        ):
            # A role-neutral marker also remains stable when overlapping
            # username/password values change roles during credential rotation.
            safe_segments.append("{xtream-credential}")
        elif (
            (
                scan_embedded_username
                and decoded_username
                and decoded_username in segment
            )
            or (decoded_password and decoded_password in segment)
        ):
            # Reject nonstandard embedded credentials rather than deriving a
            # public identifier from any secret-bearing fragment. The error is
            # intentionally generic so it cannot echo the credential.
            raise SyncError(
                "An authenticated M3U path embeds credentials in an unsafe segment."
            )
        else:
            safe_segments.append(segment)
    safe_path = "/" + "/".join(safe_segments)
    digest = hashlib.sha256(
        (safe_path + "\0" + name + "\0" + tvg_id).encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:20]
    return f"m3u_{digest}"


def parse_xtream_m3u(
    text: str, *, username: str, password: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse live EXTINF entries without retaining or reporting stream URLs."""
    categories: dict[str, dict[str, Any]] = {}
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()
    pending: tuple[dict[str, str], str] | None = None
    for raw_line in str(text or "").lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            if pending is not None:
                raise SyncError(
                    "An authenticated M3U playlist contained an EXTINF row "
                    "without its stream URL."
                )
            metadata, display_name = split_extinf(line)
            pending = (m3u_attributes(metadata), display_name.strip())
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        attributes, display_name = pending
        pending = None
        parsed_stream_url = urlparse(line)
        if parsed_stream_url.scheme not in {"http", "https"} or not parsed_stream_url.netloc:
            raise SyncError(
                "An authenticated M3U live entry has an invalid stream URL."
            )
        path = parsed_stream_url.path.casefold()
        if "/movie/" in path or "/series/" in path or re.search(
            r"\.(?:mp4|mkv|avi|mov|wmv|m4v)$", path
        ):
            continue
        name = streaming.clean_identifier(
            display_name or attributes.get("tvg-name", ""), 300
        )
        if not name:
            raise SyncError(
                "An authenticated M3U live entry has no channel name; "
                "inventory sync stopped rather than silently dropping it."
            )
        group = streaming.clean_text(
            attributes.get("group-title") or attributes.get("group") or "Uncategorized",
            200,
        ) or "Uncategorized"
        category_id = m3u_category_id(group)
        categories.setdefault(
            category_id, {"category_id": category_id, "category_name": group}
        )
        raw_tvg_attribute = str(attributes.get("tvg-id") or "")
        raw_epg_attribute = str(attributes.get("epg-id") or "")
        conflicting_id_aliases = bool(
            raw_tvg_attribute
            and raw_epg_attribute
            and raw_tvg_attribute != raw_epg_attribute
        )
        raw_tvg_id = raw_tvg_attribute or raw_epg_attribute
        tvg_id = (
            ""
            if conflicting_id_aliases
            else streaming.clean_identifier(raw_tvg_id, 300)
        )
        stream_id = m3u_stream_id(
            line,
            name,
            tvg_id,
            username=username,
            password=password,
        )
        if stream_id.casefold() in seen:
            raise SyncError(
                "An authenticated M3U playlist returned duplicate live stream IDs."
            )
        seen.add(stream_id.casefold())
        channels.append(
            {
                "stream_id": stream_id,
                "name": name,
                "category_id": category_id,
                "category_name": group,
                "epg_channel_id": tvg_id,
                "_native_epg_id_exact": (
                    not conflicting_id_aliases and raw_tvg_id == tvg_id
                ),
                "_native_epg_id_raw_present": bool(raw_tvg_id),
                "num": streaming.clean_text(
                    attributes.get("tvg-chno")
                    or attributes.get("channel-number")
                    or attributes.get("ch-number")
                    or "",
                    40,
                ),
                # Never propagate a credential-bearing provider URL.
                "stream_icon": "",
            }
        )
        if len(channels) > MAX_CHANNELS_PER_SERVER:
            raise SyncError("An M3U playlist returned too many live channels.")
    if pending is not None:
        raise SyncError(
            "An authenticated M3U playlist ended after EXTINF without a stream URL."
        )
    return list(categories.values()), channels


def decode_m3u_content(content: bytes) -> str:
    data = bytes(content or b"")
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                data = handle.read(MAX_M3U_BYTES + 1)
        except OSError:
            return ""
    if len(data) > MAX_M3U_BYTES:
        return ""
    return data.decode("utf-8-sig", errors="replace")


def fetch_m3u_inventory(
    session: Any,
    config: ServerConfig,
    service_bases: Sequence[str],
    diagnostics: list[str],
    *,
    allow_insecure_http: bool,
) -> PanelInventory | None:
    for base_number, service_base in enumerate(service_bases, start=1):
        endpoint = f"{service_base}/get.php"
        for playlist_type, output in (
            ("m3u_plus", "ts"),
            ("m3u_plus", "m3u8"),
            ("m3u", "ts"),
        ):
            try:
                status, content = safe_credentialed_get(
                    session,
                    endpoint,
                    params={
                        "username": config.username,
                        "password": config.password,
                        "type": playlist_type,
                        "output": output,
                    },
                    maximum_bytes=MAX_M3U_BYTES,
                    timeout=(25, 240),
                    allow_insecure_http=allow_insecure_http,
                )
            except SyncError:
                diagnostics.append(
                    f"M3U candidate {base_number} ({playlist_type}/{output}): request failed"
                )
                continue
            if status != 200:
                diagnostics.append(
                    f"M3U candidate {base_number} ({playlist_type}/{output}): HTTP {status}"
                )
                continue
            text = decode_m3u_content(content)
            if not text:
                diagnostics.append(
                    f"M3U candidate {base_number} ({playlist_type}/{output}): invalid response"
                )
                continue
            if text.lstrip().startswith("{"):
                try:
                    problem = auth_problem(json.loads(text))
                except json.JSONDecodeError:
                    problem = ""
                if problem:
                    raise SyncError(problem)
                diagnostics.append(
                    f"M3U candidate {base_number} ({playlist_type}/{output}): JSON, not playlist"
                )
                continue
            categories, channels = parse_xtream_m3u(
                text,
                username=config.username,
                password=config.password,
            )
            if not channels:
                diagnostics.append(
                    f"M3U candidate {base_number} ({playlist_type}/{output}): no live rows"
                )
                continue
            diagnostics.append("Authenticated M3U fallback supplied the live inventory.")
            return PanelInventory(
                server_id=config.server_id,
                server_label=config.server_label,
                categories=categories,
                channels=channels,
                source="authenticated_m3u",
                diagnostics=list(diagnostics),
            )
    return None


def fetch_panel_inventory(
    session: Any,
    config: ServerConfig,
    *,
    allow_insecure_http: bool,
) -> PanelInventory:
    """Fetch all live channels via validated player_api, then authenticated M3U."""
    if not config.username.strip() or not config.password:
        raise SyncError(f"{config.server_label}: username and password are required.")
    service_bases = panel_base_candidates(
        config.base_url, allow_insecure_http=allow_insecure_http
    )
    reflection_needles = provider_reflection_needles(config, service_bases)
    diagnostics: list[str] = []
    best_categories: list[dict[str, Any]] = []
    offset = 0
    while offset < len(service_bases):
        service_base = service_bases[offset]
        candidate_number = offset + 1
        offset += 1
        endpoint = f"{service_base}/player_api.php"
        category_probe = request_panel_json(
            session,
            endpoint,
            config.username,
            config.password,
            "get_live_categories",
            allow_insecure_http=allow_insecure_http,
        )
        if category_probe.issue:
            diagnostics.append(f"API candidate {candidate_number}: {category_probe.issue}")
            categories: list[dict[str, Any]] = []
        else:
            problem = auth_problem(category_probe.payload)
            if problem:
                raise SyncError(f"{config.server_label}: {problem}")
            try:
                categories = normalize_panel_action_rows(
                    category_probe.payload, "get_live_categories"
                )
            except SyncError:
                categories = []
                diagnostics.append(
                    f"API candidate {candidate_number}: category schema validation failed"
                )
            if categories and not best_categories:
                best_categories = categories
            elif not categories:
                diagnostics.append(
                    f"API candidate {candidate_number}: categories were not valid rows"
                )
            for advertised in advertised_same_host_bases(
                category_probe.payload,
                service_base,
                allow_insecure_http=allow_insecure_http,
            ):
                if advertised not in service_bases and len(service_bases) < 6:
                    service_bases.append(advertised)
                    diagnostics.append("The same host advertised another service port.")

        stream_probe = request_panel_json(
            session,
            endpoint,
            config.username,
            config.password,
            "get_live_streams",
            allow_insecure_http=allow_insecure_http,
        )
        if stream_probe.issue:
            diagnostics.append(f"API candidate {candidate_number}: {stream_probe.issue}")
            channels: list[dict[str, Any]] = []
        else:
            problem = auth_problem(stream_probe.payload)
            if problem:
                raise SyncError(f"{config.server_label}: {problem}")
            try:
                channels = normalize_panel_action_rows(
                    stream_probe.payload, "get_live_streams"
                )
            except SyncError:
                channels = []
                diagnostics.append(
                    f"API candidate {candidate_number}: live-stream schema validation "
                    "failed; authenticated M3U fallback will be tried"
                )
            if not channels:
                diagnostics.append(
                    "API candidate "
                    f"{candidate_number}: {safe_payload_shape(stream_probe.payload)} "
                    "was not a live-channel list"
                )
            for advertised in advertised_same_host_bases(
                stream_probe.payload,
                service_base,
                allow_insecure_http=allow_insecure_http,
            ):
                if advertised not in service_bases and len(service_bases) < 6:
                    service_bases.append(advertised)
                    diagnostics.append("The same host advertised another service port.")

        if channels:
            categories = categories or best_categories or derive_categories_from_channels(channels)
            category_names = {
                streaming.clean_identifier(row.get("category_id", ""), 120):
                streaming.clean_text(row.get("category_name", ""), 200)
                for row in categories
            }
            for channel in channels:
                if not channel.get("category_name"):
                    channel["category_name"] = category_names.get(
                        streaming.clean_identifier(channel.get("category_id", ""), 120),
                        "",
                    )
            validate_provider_inventory_secret_safe(
                categories, channels, reflection_needles
            )
            return PanelInventory(
                server_id=config.server_id,
                server_label=config.server_label,
                categories=categories,
                channels=channels,
                source="player_api",
                diagnostics=list(diagnostics),
            )

    fallback = fetch_m3u_inventory(
        session,
        config,
        service_bases,
        diagnostics,
        allow_insecure_http=allow_insecure_http,
    )
    if fallback is not None:
        validate_provider_inventory_secret_safe(
            fallback.categories, fallback.channels, reflection_needles
        )
        return fallback
    safe_diagnostics = "; ".join(diagnostics[-8:])
    raise SyncError(
        f"{config.server_label}: no valid live inventory was returned. "
        "Check the exact base URL/port and account status."
        + (f" Safe diagnostics: {safe_diagnostics}" if safe_diagnostics else "")
    )


def parse_table_values(
    values: Sequence[Sequence[Any]],
    *,
    require_v1_order: bool = True,
    maximum_rows: int = streaming.MAX_MAPPING_ROWS,
) -> MappingTable:
    if not values:
        raise SyncError("The mapping table is empty.")
    raw_headers = [streaming.clean_text(value, 100) for value in values[0]]
    headers = [streaming.normalized_header(value) for value in raw_headers]
    if not headers or any(not header for header in headers):
        raise SyncError("The mapping table contains a blank header.")
    duplicates = sorted(
        {header for header in headers if headers.count(header) > 1}
    )
    if duplicates:
        raise SyncError("The mapping table contains duplicate normalized headers.")
    required = {"server_id", "stream_id", "channel_name"}
    missing = sorted(required - set(headers))
    if missing:
        raise SyncError("The mapping table is missing required columns: " + ", ".join(missing))
    if require_v1_order and tuple(headers) != tuple(streaming.SHEET_COLUMNS):
        raise SyncError(
            "The Mappings tab columns are not in the required Version 1 order. "
            "Import the supplied Version 1 workbook without rearranging columns."
        )
    rows: list[dict[str, str]] = []
    row_numbers: list[int] = []
    keys: set[tuple[str, str]] = set()
    for row_number, row_values in enumerate(values[1:], start=2):
        values_list = ["" if value is None else str(value) for value in row_values]
        if not any(value.strip() for value in values_list):
            continue
        if len(values_list) > len(headers):
            raise SyncError(f"Mapping row {row_number} has more cells than headers.")
        values_list.extend([""] * (len(headers) - len(values_list)))
        row = {
            headers[index]: streaming.unescape_spreadsheet_text(value)
            for index, value in enumerate(values_list)
        }
        try:
            server_id = streaming.normalize_server_id(row.get("server_id", ""))
        except streaming.BuildError as exc:
            raise SyncError(f"Mapping row {row_number} has an invalid server ID.") from exc
        stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
        channel_name = streaming.clean_identifier(row.get("channel_name", ""), 300)
        if not stream_id or not channel_name:
            raise SyncError(
                f"Mapping row {row_number} requires stream_id and channel_name."
            )
        key = (server_id, stream_id)
        if key in keys:
            raise SyncError(
                f"Mapping row {row_number} duplicates ({server_id}, {stream_id})."
            )
        keys.add(key)
        row["server_id"] = server_id
        row["stream_id"] = stream_id
        row["channel_name"] = channel_name
        rows.append(row)
        row_numbers.append(row_number)
        if len(rows) > int(maximum_rows):
            raise SyncError(
                f"The mapping table exceeds its configured {int(maximum_rows):,}-row limit."
            )
    return MappingTable(
        raw_headers=raw_headers,
        headers=headers,
        rows=rows,
        row_numbers=row_numbers,
    )


def parse_mapping_csv(content: bytes, *, require_v1_order: bool = True) -> MappingTable:
    if len(content) > streaming.MAX_MAPPING_BYTES:
        raise SyncError("The mapping CSV exceeds its configured size limit.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SyncError("The mapping CSV must be UTF-8.") from exc
    if text.lstrip()[:100].casefold().startswith(("<!doctype html", "<html")):
        raise SyncError("The offline mapping file contains HTML instead of CSV data.")
    return parse_table_values(
        list(csv.reader(io.StringIO(text, newline=""))),
        require_v1_order=require_v1_order,
    )


def pipe(values: Iterable[str]) -> str:
    return "|".join(str(value) for value in values if str(value))


def format_confidence(value: float) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def new_mapping_row(
    inventory: PanelInventory,
    channel: Mapping[str, Any],
    *,
    discovered_at: str,
) -> dict[str, str]:
    server_id = streaming.normalize_server_id(inventory.server_id)
    channel_name = streaming.clean_identifier(channel.get("name", ""), 300)
    native_epg_id = streaming.clean_identifier(channel.get("epg_channel_id", ""), 300)
    if server_id != "server_1" and native_epg_id:
        source = "panel"
        epg_feed = "panel"
        epg_id = native_epg_id
        reason = "Native panel EPG candidate found; verify it before changing action."
    else:
        source = "epgshare01"
        epg_feed = "ALL_SOURCES1"
        epg_id = ""
        reason = "New provider channel requires an exact EPG review."

    raw: dict[str, str] = {
        "server_id": server_id,
        "server_label": inventory.server_label,
        "stream_id": streaming.clean_identifier(channel.get("stream_id", ""), 120),
        # Discovery never publishes content. A human must verify the exact
        # identity, guide source, and metadata before explicitly enabling it.
        "enabled": "FALSE",
        "channel_name": channel_name,
        "canonical_name": channel_name,
        "category_id": streaming.clean_identifier(channel.get("category_id", ""), 120),
        "category_name": streaming.clean_text(channel.get("category_name", ""), 200),
        "channel_number": streaming.clean_text(channel.get("num", ""), 40),
        "sort_priority": "1000",
        "action": "REVIEW",
        "source": source,
        "epg_feed": epg_feed,
        "epg_id": epg_id,
        # Provider/M3U icon URLs can hide credentials in any path segment.
        # Only a later reviewed manual or EPGShare logo may enter public output.
        "logo_url": "",
        "metadata_status": "review",
        "metadata_locked": "FALSE",
        "reason": reason,
        "notes": (
            f"Automatically discovered {discovered_at}; existing Sheet rows were not changed."
        ),
    }
    try:
        metadata = streaming.build_metadata(raw, 0)
    except streaming.BuildError as exc:
        raise SyncError(f"Could not classify a newly discovered {inventory.server_label} row.") from exc
    result = {column: "" for column in streaming.SHEET_COLUMNS}
    result.update(raw)
    result.update(
        {
            "region_code": metadata.region,
            "genre": metadata.genre,
            "primary_language": metadata.primary_language,
            "country_codes": pipe(metadata.countries),
            "language_codes": pipe(metadata.languages),
            "subgenres": pipe(metadata.subgenres),
            "sport_codes": pipe(metadata.sports),
            "religion_codes": pipe(metadata.religions),
            "audience_codes": pipe(metadata.audiences),
            "content_rating": metadata.content_rating,
            "channel_role": metadata.channel_role,
            "tags": pipe(metadata.tags),
            "metadata_status": metadata.status,
            "metadata_source": metadata.source,
            "metadata_confidence": format_confidence(metadata.confidence),
            "metadata_locked": "TRUE" if metadata.locked else "FALSE",
        }
    )
    return result


def inventory_rows(inventories: Sequence[PanelInventory]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for inventory in inventories:
        for channel in inventory.channels:
            result.append(
                {
                    "server_id": inventory.server_id,
                    "server_label": inventory.server_label,
                    "inventory_source": inventory.source,
                    "stream_id": streaming.clean_identifier(channel.get("stream_id", ""), 120),
                    "channel_name": streaming.clean_identifier(channel.get("name", ""), 300),
                    "category_id": streaming.clean_identifier(channel.get("category_id", ""), 120),
                    "category_name": streaming.clean_text(channel.get("category_name", ""), 200),
                    "channel_number": streaming.clean_text(channel.get("num", ""), 40),
                    "native_epg_id_present": "TRUE" if channel.get("epg_channel_id") else "FALSE",
                }
            )
    return sorted(
        result,
        key=lambda row: (
            row["server_id"],
            streaming.stream_sort_key(row["stream_id"]),
        ),
    )


QUALITY_SUFFIX_RE = re.compile(
    r"(?:[\s._|+\-]*(?:uhd|fhd|hd|sd|4k|8k|2160p|1080p|720p|"
    r"h\.?26[45]|hevc|backup|raw))+$",
    flags=re.IGNORECASE,
)
AUTO_DISCOVERY_PROVENANCE_RE = re.compile(
    r"Automatically discovered "
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z); "
    r"existing Sheet rows were not changed\."
)


def comparison_name_key(value: object) -> str:
    """Normalize case/space and display-quality suffixes for drift reporting."""
    text = streaming.clean_identifier(value, 300)
    text = QUALITY_SUFFIX_RE.sub("", text).strip()
    return " ".join(text.casefold().split())


def exact_inventory_name_key(value: object) -> str:
    """Normalize Unicode, case and whitespace without discarding name tokens."""

    text = unicodedata.normalize(
        "NFKC", streaming.clean_identifier(value, 300)
    ).casefold()
    return " ".join(text.split())


def has_exact_auto_discovery_provenance(value: object) -> bool:
    notes = streaming.clean_text(value, 2000)
    matched = AUTO_DISCOVERY_PROVENANCE_RE.fullmatch(notes)
    if matched is None:
        return False
    try:
        parsed = datetime.fromisoformat(
            matched.group("timestamp").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def enrich_review_inventories_from_m3u(
    session: Any,
    inventories: Sequence[PanelInventory],
    server_configs: Sequence[ServerConfig],
    *,
    selected_servers: Iterable[str],
    allow_insecure_http: bool,
) -> tuple[list[PanelInventory], dict[str, int]]:
    """Add only corroborated M3U EPG IDs to private REVIEW-mode copies.

    Xtream's otherwise-authoritative ``get_live_streams`` response commonly
    omits ``epg_channel_id``.  The authenticated M3U is therefore consulted as
    a narrow secondary attribute source.  It can never replace API identity,
    name, category, ordering, or availability: an ID is copied only after an
    exact numeric stream-ID join and the same normalized channel name.
    """

    allowed_servers = {
        streaming.normalize_server_id(value) for value in selected_servers
    }.intersection({"server_2", "server_3"})
    configs = {config.server_id: config for config in server_configs}
    stats = {
        "native_review_provider_ids_present": 0,
        "native_review_m3u_servers_attempted": 0,
        "native_review_m3u_servers_available": 0,
        "native_review_m3u_ids_recovered": 0,
        "native_review_m3u_ids_corroborated": 0,
        "native_review_m3u_id_conflicts": 0,
        "native_review_m3u_name_mismatches": 0,
        "native_review_m3u_missing_rows": 0,
        "native_review_invalid_provider_ids": 0,
    }
    enriched: list[PanelInventory] = []
    for inventory in inventories:
        copied = PanelInventory(
            server_id=inventory.server_id,
            server_label=inventory.server_label,
            categories=[dict(row) for row in inventory.categories],
            channels=[dict(row) for row in inventory.channels],
            source=inventory.source,
            diagnostics=list(inventory.diagnostics),
        )
        config = configs.get(inventory.server_id)
        if inventory.server_id in allowed_servers:
            for channel in copied.channels:
                if (
                    channel.get(
                        "_native_epg_id_raw_present",
                        bool(
                            streaming.clean_identifier(
                                channel.get("epg_channel_id", ""), 300
                            )
                        ),
                    )
                    and channel.get("_native_epg_id_exact", True) is not True
                ):
                    channel["epg_channel_id"] = ""
                    channel["_native_epg_id_conflict"] = True
                    stats["native_review_invalid_provider_ids"] += 1
        if (
            inventory.server_id not in allowed_servers
            or inventory.source != "player_api"
            or config is None
        ):
            enriched.append(copied)
            continue

        stats["native_review_m3u_servers_attempted"] += 1
        diagnostics: list[str] = []
        try:
            m3u_inventory = fetch_m3u_inventory(
                session,
                config,
                panel_base_candidates(
                    config.base_url, allow_insecure_http=allow_insecure_http
                ),
                diagnostics,
                allow_insecure_http=allow_insecure_http,
            )
        except SyncError:
            m3u_inventory = None
        if m3u_inventory is None:
            copied.diagnostics.append(
                "Authenticated M3U EPG-ID enrichment was unavailable."
            )
            enriched.append(copied)
            continue
        validate_provider_inventory_secret_safe(
            m3u_inventory.categories,
            m3u_inventory.channels,
            provider_reflection_needles(
                config,
                panel_base_candidates(
                    config.base_url, allow_insecure_http=allow_insecure_http
                ),
            ),
        )
        stats["native_review_m3u_servers_available"] += 1
        m3u_by_stream: dict[str, Mapping[str, Any]] = {}
        for channel in m3u_inventory.channels:
            stream_id = streaming.clean_identifier(
                channel.get("stream_id", ""), 120
            )
            if re.fullmatch(r"[0-9]+", stream_id):
                m3u_by_stream[stream_id] = channel

        for channel in copied.channels:
            stream_id = streaming.clean_identifier(
                channel.get("stream_id", ""), 120
            )
            if not re.fullmatch(r"[0-9]+", stream_id):
                continue
            m3u_channel = m3u_by_stream.get(stream_id)
            if m3u_channel is None:
                stats["native_review_m3u_missing_rows"] += 1
                continue
            if channel.get("_native_epg_id_conflict") is True:
                continue
            if exact_inventory_name_key(
                channel.get("name", "")
            ) != exact_inventory_name_key(m3u_channel.get("name", "")):
                # A same-ID M3U row describing another channel is fresh,
                # contradictory identity evidence.  Do not auto-approve even
                # when the API supplied a native ID of its own.
                if streaming.clean_identifier(
                    channel.get("epg_channel_id", ""), 300
                ):
                    channel["epg_channel_id"] = ""
                stats["native_review_m3u_name_mismatches"] += 1
                continue
            m3u_epg_id = streaming.clean_identifier(
                m3u_channel.get("epg_channel_id", ""), 300
            )
            if (
                m3u_channel.get(
                    "_native_epg_id_raw_present", bool(m3u_epg_id)
                )
                and m3u_channel.get("_native_epg_id_exact", True) is not True
            ):
                channel["epg_channel_id"] = ""
                channel["_native_epg_id_conflict"] = True
                stats["native_review_invalid_provider_ids"] += 1
                continue
            if not m3u_epg_id:
                continue
            api_epg_id = streaming.clean_identifier(
                channel.get("epg_channel_id", ""), 300
            )
            if not api_epg_id:
                channel["epg_channel_id"] = m3u_epg_id
                stats["native_review_m3u_ids_recovered"] += 1
            elif api_epg_id == m3u_epg_id:
                stats["native_review_m3u_ids_corroborated"] += 1
            else:
                # Contradictory fresh provider evidence is never eligible for
                # unattended approval, even though API name/category identity
                # remains authoritative for ordinary inventory reporting.
                channel["epg_channel_id"] = ""
                stats["native_review_m3u_id_conflicts"] += 1
        copied.diagnostics.append(
            "Authenticated M3U was used only for exact REVIEW EPG-ID hints."
        )
        enriched.append(copied)
    stats["native_review_provider_ids_present"] = sum(
        1
        for inventory in enriched
        if inventory.server_id in allowed_servers
        for channel in inventory.channels
        if streaming.clean_identifier(channel.get("epg_channel_id", ""), 300)
    )
    return enriched, stats


def revalidate_native_review_updates(
    updates: Sequence[Mapping[str, str]],
    proposal_inventories: Sequence[PanelInventory],
    server_configs: Sequence[ServerConfig],
    *,
    allow_insecure_http: bool | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Refetch exact native target identities immediately before a Sheet write."""

    stats = {
        "native_review_revalidation_checked": len(updates),
        "native_review_revalidation_rejected": 0,
        "native_review_revalidation_unavailable": 0,
    }
    if not updates:
        return [], stats
    expected_channels: dict[tuple[str, str], Mapping[str, Any]] = {}
    for inventory in proposal_inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        for channel in inventory.channels:
            key = (
                server_id,
                streaming.clean_identifier(channel.get("stream_id", ""), 120),
            )
            if key in expected_channels:
                raise SyncError("Provider inventories contain a duplicate stream identity.")
            expected_channels[key] = channel

    updates_by_server: dict[str, list[dict[str, str]]] = {
        "server_2": [],
        "server_3": [],
    }
    for raw in updates:
        row = {column: str(raw.get(column, "")) for column in streaming.SHEET_COLUMNS}
        key = _row_identity(row)
        if key[0] not in updates_by_server or key not in expected_channels:
            stats["native_review_revalidation_rejected"] += 1
            continue
        updates_by_server[key[0]].append(row)

    configs = {config.server_id: config for config in server_configs}
    if allow_insecure_http is None:
        allow_insecure_http = (
            str(os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "")).strip().casefold()
            in streaming.TRUE_VALUES
        )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"SKYTV-Channel-Inventory/{SYNC_VERSION}",
            "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
        }
    )
    verified: list[dict[str, str]] = []
    try:
        for server_id in ("server_2", "server_3"):
            server_updates = updates_by_server[server_id]
            if not server_updates:
                continue
            config = configs.get(server_id)
            if config is None:
                stats["native_review_revalidation_unavailable"] += len(
                    server_updates
                )
                continue
            try:
                current_inventory = fetch_panel_inventory(
                    session,
                    config,
                    allow_insecure_http=allow_insecure_http,
                )
                enriched, _m3u_stats = enrich_review_inventories_from_m3u(
                    session,
                    [current_inventory],
                    [config],
                    selected_servers={server_id},
                    allow_insecure_http=allow_insecure_http,
                )
            except SyncError:
                stats["native_review_revalidation_unavailable"] += len(
                    server_updates
                )
                continue
            current_by_key = {
                (
                    server_id,
                    streaming.clean_identifier(channel.get("stream_id", ""), 120),
                ): channel
                for channel in enriched[0].channels
            }
            for row in server_updates:
                key = _row_identity(row)
                expected = expected_channels[key]
                current = current_by_key.get(key)
                if current is None:
                    stats["native_review_revalidation_rejected"] += 1
                    continue
                expected_tuple = (
                    streaming.clean_identifier(expected.get("name", ""), 300),
                    streaming.clean_identifier(expected.get("category_id", ""), 120),
                    streaming.clean_text(expected.get("category_name", ""), 200),
                    streaming.clean_identifier(expected.get("epg_channel_id", ""), 300),
                )
                current_tuple = (
                    streaming.clean_identifier(current.get("name", ""), 300),
                    streaming.clean_identifier(current.get("category_id", ""), 120),
                    streaming.clean_text(current.get("category_name", ""), 200),
                    streaming.clean_identifier(current.get("epg_channel_id", ""), 300),
                )
                if (
                    expected_tuple != current_tuple
                    or current_tuple[3]
                    != streaming.clean_identifier(row.get("epg_id", ""), 300)
                    or exact_inventory_name_key(row.get("channel_name", ""))
                    != exact_inventory_name_key(current.get("name", ""))
                    or current.get("_native_epg_id_conflict") is True
                    or current.get("_native_epg_id_exact", True) is not True
                ):
                    stats["native_review_revalidation_rejected"] += 1
                    continue
                verified.append(row)
    finally:
        session.close()
    verified.sort(
        key=lambda row: (
            row["server_id"], streaming.stream_sort_key(row["stream_id"])
        )
    )
    return verified, stats


def revalidate_provider_identity_updates(
    updates: Sequence[Mapping[str, str]],
    proposal_inventories: Sequence[PanelInventory],
    server_configs: Sequence[ServerConfig],
    *,
    allow_insecure_http: bool = False,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Refetch exact provider identities adjacent to a REVIEW write batch.

    This is deliberately independent of the selected EPG source.  A real,
    dummy, ignored, native, or AI-assisted decision is stale if the provider
    reused the stream ID after proposal generation.  Returning only unchanged
    rows lets the caller fail closed before the mutating request.
    """

    stats = {
        "provider_batch_revalidation_checked": len(updates),
        "provider_batch_revalidation_rejected": 0,
        "provider_batch_revalidation_unavailable": 0,
    }
    if not updates:
        return [], stats
    expected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for inventory in proposal_inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        for channel in inventory.channels:
            key = (
                server_id,
                streaming.clean_identifier(channel.get("stream_id", ""), 120),
            )
            if key in expected:
                raise SyncError(
                    "Provider inventories contain a duplicate stream identity."
                )
            expected[key] = channel

    rows_by_server: dict[str, list[dict[str, str]]] = {
        server_id: [] for server_id in DEFAULT_SERVERS
    }
    for raw in updates:
        row = {
            column: str(raw.get(column, ""))
            for column in streaming.SHEET_COLUMNS
        }
        key = _row_identity(row)
        if key not in expected:
            stats["provider_batch_revalidation_rejected"] += 1
            continue
        rows_by_server[key[0]].append(row)

    configs = {config.server_id: config for config in server_configs}
    verified: list[dict[str, str]] = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"SKYTV-Channel-Inventory/{SYNC_VERSION}",
            "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
        }
    )
    try:
        for server_id in DEFAULT_SERVERS:
            server_rows = rows_by_server[server_id]
            if not server_rows:
                continue
            config = configs.get(server_id)
            if config is None:
                stats["provider_batch_revalidation_unavailable"] += len(
                    server_rows
                )
                continue
            try:
                current_inventory = fetch_panel_inventory(
                    session,
                    config,
                    allow_insecure_http=bool(allow_insecure_http),
                )
            except SyncError:
                stats["provider_batch_revalidation_unavailable"] += len(
                    server_rows
                )
                continue
            current_by_key = {
                (
                    server_id,
                    streaming.clean_identifier(
                        channel.get("stream_id", ""), 120
                    ),
                ): channel
                for channel in current_inventory.channels
            }
            for row in server_rows:
                key = _row_identity(row)
                old = expected[key]
                current = current_by_key.get(key)
                if current is None:
                    stats["provider_batch_revalidation_rejected"] += 1
                    continue
                old_tuple = (
                    streaming.clean_identifier(old.get("name", ""), 300),
                    streaming.clean_identifier(old.get("category_id", ""), 120),
                    streaming.clean_text(old.get("category_name", ""), 200),
                )
                current_tuple = (
                    streaming.clean_identifier(current.get("name", ""), 300),
                    streaming.clean_identifier(
                        current.get("category_id", ""), 120
                    ),
                    streaming.clean_text(
                        current.get("category_name", ""), 200
                    ),
                )
                if (
                    old_tuple != current_tuple
                    or exact_inventory_name_key(row.get("channel_name", ""))
                    != exact_inventory_name_key(current.get("name", ""))
                ):
                    stats["provider_batch_revalidation_rejected"] += 1
                    continue
                verified.append(row)
    finally:
        session.close()
    verified.sort(
        key=lambda row: (
            row["server_id"], streaming.stream_sort_key(row["stream_id"])
        )
    )
    return verified, stats


def _deduplicate_provider_revalidation_rows(
    *row_groups: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Return one exact provider-identity row per canonical stream key."""

    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for group in row_groups:
        for raw in group:
            row = {
                column: str(raw.get(column, ""))
                for column in streaming.SHEET_COLUMNS
            }
            key = _row_identity(row)
            prior = by_key.get(key)
            if prior is not None:
                prior_identity = (
                    streaming.clean_identifier(
                        prior.get("channel_name", ""), 300
                    ),
                    streaming.clean_identifier(
                        prior.get("category_id", ""), 120
                    ),
                    streaming.clean_text(prior.get("category_name", ""), 200),
                )
                row_identity = (
                    streaming.clean_identifier(row.get("channel_name", ""), 300),
                    streaming.clean_identifier(row.get("category_id", ""), 120),
                    streaming.clean_text(row.get("category_name", ""), 200),
                )
                if prior_identity != row_identity:
                    raise SyncError(
                        "Provider revalidation received conflicting identities "
                        "for one stream key."
                    )
                continue
            by_key[key] = row
    return [
        by_key[key]
        for key in sorted(
            by_key,
            key=lambda item: (item[0], streaming.stream_sort_key(item[1])),
        )
    ]


def _learned_alias_support_rows_from_outcome(
    outcome: Any,
) -> tuple[dict[str, str], ...]:
    """Validate the private provider identities behind run-local alias memory."""

    registered = getattr(outcome, "learned_alias_registered", 0)
    if isinstance(registered, bool) or not isinstance(registered, int):
        raise SyncError("Smart Rules returned invalid learned-alias metadata.")
    raw_support = getattr(outcome, "learned_alias_support_rows", ())
    if raw_support is None:
        raw_rows: tuple[Mapping[str, str], ...] = ()
    elif not isinstance(raw_support, (tuple, list)) or any(
        not isinstance(row, Mapping) for row in raw_support
    ):
        raise SyncError("Smart Rules returned invalid learned-alias support rows.")
    else:
        raw_rows = tuple(raw_support)
    if registered < 0 or (registered == 0 and raw_rows):
        raise SyncError("Smart Rules returned inconsistent learned-alias metadata.")
    if registered == 0:
        return ()
    if not raw_rows:
        raise SyncError(
            "Smart Rules registered learned aliases without their provider "
            "support rows."
        )
    return tuple(_deduplicate_provider_revalidation_rows(raw_rows))


def revalidate_auto_enabled_new_rows(
    rows: Sequence[Mapping[str, str]],
    proposal_inventories: Sequence[PanelInventory],
    server_configs: Sequence[ServerConfig],
    *,
    allow_insecure_http: bool = False,
    learned_alias_support_rows: Sequence[Mapping[str, str]] = (),
) -> tuple[bool, dict[str, int]]:
    """Reacquire provider identity for new approvals and their rule teachers.

    Disabled REVIEW/IGNORE rows cannot publish a stale schedule, so this narrow
    terminal gate normally checks only new rows which Smart Rules would make
    output-eligible. If this run registered cross-server alias memory, its
    exact teaching rows are also checked before any dependent append.
    """

    candidates: list[dict[str, str]] = []
    for raw in rows:
        action = streaming.clean_text(raw.get("action", ""), 40).upper()
        if action not in {"AUTO_EPGSHARE", "AUTO_DUMMY"}:
            continue
        try:
            enabled = streaming.parse_bool(
                raw.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        except streaming.BuildError as exc:
            raise SyncError(
                "A newly automatic mapping has an invalid enabled value."
            ) from exc
        if not enabled:
            raise SyncError(
                "A newly automatic mapping must be enabled before provider "
                "revalidation."
            )
        candidates.append(
            {
                column: str(raw.get(column, ""))
                for column in streaming.SHEET_COLUMNS
            }
        )

    dependencies = _deduplicate_provider_revalidation_rows(
        candidates,
        learned_alias_support_rows,
    )
    stats = {
        "new_auto_provider_revalidation_checked": len(dependencies),
        "new_auto_provider_revalidation_rejected": 0,
        "new_auto_provider_revalidation_unavailable": 0,
    }
    if not dependencies:
        return True, stats

    verified, provider_stats = revalidate_provider_identity_updates(
        dependencies,
        proposal_inventories,
        server_configs,
        allow_insecure_http=allow_insecure_http,
    )
    stats["new_auto_provider_revalidation_rejected"] = int(
        provider_stats.get("provider_batch_revalidation_rejected", 0)
    )
    stats["new_auto_provider_revalidation_unavailable"] = int(
        provider_stats.get("provider_batch_revalidation_unavailable", 0)
    )
    wanted_by_key = {_row_identity(row): row for row in dependencies}
    verified_by_key = {_row_identity(row): row for row in verified}
    exact = (
        len(wanted_by_key) == len(dependencies)
        and set(verified_by_key) == set(wanted_by_key)
        and all(
            verified_by_key[key] == wanted
            for key, wanted in wanted_by_key.items()
        )
    )
    return exact, stats


NAME_NOISE_TOKENS = frozenset(
    {
        "tv", "channel", "network", "live", "backup", "raw", "sd", "hd", "fhd",
        "uhd", "4k", "8k", "hevc", "h264", "h265", "1080p", "720p",
    }
)

GENERIC_NUMBERED_NAME_RE = re.compile(
    r"^(?:channel|feed)\s*(?:(?:no|number)\.?\s*|[#:]\s*)?\d+$",
    flags=re.IGNORECASE,
)

# These are provider-controlled *slot* names rather than durable channel
# brands. The text following the matched prefix is the event currently
# assigned to that slot and is expected to rotate. Keep the grammar narrow:
# only banks observed with an explicit, zero-padded slot number and canonical
# delimiter qualify. A loose ``ESPN``/``PPV`` token check would hide ordinary
# channel renames and could publish a stale guide under a recycled stream ID.
PROVIDER_EVENT_SLOT_PATTERNS = (
    (
        "us_espn_plus",
        re.compile(
            r"^(?P<prefix>US \(ESPN\+ [0-9]{3}\) \|)(?: .*)?$",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "ppv_event",
        re.compile(
            r"^(?P<prefix>PPV EVENT [0-9]{2}:)(?: .*)?$",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "live_event",
        re.compile(
            r"^(?P<prefix>LIVE EVENT [0-9]{2} -)(?: .*)?$",
            flags=re.IGNORECASE,
        ),
    ),
)


def provider_event_slot_identity(value: object) -> tuple[str, str] | None:
    """Return a strict event-bank slot identity, excluding its event payload.

    Matching uses the complete anchored name grammar, so brand-like names that
    merely contain ``ESPN+`` or ``PPV`` cannot enter this exception. Unicode,
    case, and whitespace normalization is limited to comparing the same
    canonical prefix; the required zero padding and delimiter remain part of
    the identity.
    """

    text = unicodedata.normalize(
        "NFKC", streaming.clean_identifier(value, 300)
    )
    for family, pattern in PROVIDER_EVENT_SLOT_PATTERNS:
        matched = pattern.fullmatch(text)
        if matched is not None:
            return family, exact_inventory_name_key(matched.group("prefix"))
    return None


def is_same_provider_event_slot(
    old_name: object,
    new_name: object,
    old_category: object,
    new_category: object,
) -> bool:
    """Whether a name change is only rotating payload on one proven slot."""

    old_category_key = exact_inventory_name_key(old_category)
    if not old_category_key or old_category_key != exact_inventory_name_key(
        new_category
    ):
        return False
    old_identity = provider_event_slot_identity(old_name)
    return old_identity is not None and old_identity == provider_event_slot_identity(
        new_name
    )


def core_name_tokens(value: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", comparison_name_key(value))
        if token not in NAME_NOISE_TOKENS and (len(token) > 1 or token.isdigit())
    }


def possible_id_reuse(old_name: str, new_name: str, old_category: str, new_category: str) -> bool:
    # An adult-boundary category change is identity-bearing even when a panel
    # reuses a generic display name.  Adult Swim remains non-adult because the
    # builder's conservative adult classifier explicitly excludes that brand.
    old_is_adult = streaming.infer_genre(str(old_category or ""))[0] == "adult"
    new_is_adult = streaming.infer_genre(str(new_category or ""))[0] == "adult"
    if old_is_adult != new_is_adult:
        return True
    old_name_key = comparison_name_key(old_name)
    new_name_key = comparison_name_key(new_name)
    old_category_key = comparison_name_key(old_category)
    new_category_key = comparison_name_key(new_category)
    # A numbered placeholder carries no durable brand identity.  A provider
    # moving that same placeholder between materially different folders is a
    # conservative reuse signal (including regional East/West-style folders).
    # Branded channels do not use this rule, so ordinary folder cleanup remains
    # a non-severe drift report.
    if (
        old_name_key == new_name_key
        and GENERIC_NUMBERED_NAME_RE.fullmatch(old_name_key)
        and old_category_key != new_category_key
        and (old_category_key or new_category_key)
    ):
        return True
    if old_name_key == new_name_key:
        return False
    # Event banks intentionally rotate the event payload while their stable
    # numbered slot keeps the same stream identity. Treat only an exact
    # supported prefix in the same non-empty category as ordinary name drift.
    # Existing OPEN alerts remain authoritative and are not resolved here.
    if is_same_provider_event_slot(
        old_name, new_name, old_category, new_category
    ):
        return False
    old_tokens = core_name_tokens(old_name)
    new_tokens = core_name_tokens(new_name)
    if old_tokens and new_tokens and old_tokens == new_tokens:
        return False
    # Any core-token change—including an empty/non-empty transition—is treated
    # conservatively as a possible recycled stream ID. False positives are
    # reviewable; a false negative could publish the old channel's guide under
    # a newly reused provider stream identity.
    return True


def compare_inventory(
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    *,
    discovered_at: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    existing = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): row
        for row in table.rows
    }
    inventory_by_key: dict[tuple[str, str], tuple[PanelInventory, Mapping[str, Any]]] = {}
    new_rows: list[dict[str, str]] = []
    changed_rows: list[dict[str, str]] = []
    for inventory in inventories:
        for channel in inventory.channels:
            key = (
                inventory.server_id,
                streaming.clean_identifier(channel.get("stream_id", ""), 120),
            )
            if key in inventory_by_key:
                raise SyncError(
                    f"{inventory.server_label} returned duplicate stream ID {key[1]!r}."
                )
            inventory_by_key[key] = (inventory, channel)
            current = existing.get(key)
            if current is None:
                new_rows.append(
                    new_mapping_row(
                        inventory,
                        channel,
                        discovered_at=discovered_at,
                    )
                )
                continue
            provider_name = streaming.clean_identifier(channel.get("name", ""), 300)
            provider_category = streaming.clean_text(channel.get("category_name", ""), 200)
            sheet_name = streaming.clean_identifier(current.get("channel_name", ""), 300)
            sheet_category = streaming.clean_text(current.get("category_name", ""), 200)
            differences: list[str] = []
            if comparison_name_key(provider_name) != comparison_name_key(sheet_name):
                differences.append("channel_name")
            if comparison_name_key(provider_category) != comparison_name_key(sheet_category):
                differences.append("category_name")
            if differences:
                reuse_risk = possible_id_reuse(
                    sheet_name, provider_name, sheet_category, provider_category
                )
                changed_rows.append(
                    {
                        "server_id": key[0],
                        "stream_id": key[1],
                        "different_fields": "|".join(differences),
                        "sheet_channel_name": sheet_name,
                        "provider_channel_name": provider_name,
                        "sheet_category_name": sheet_category,
                        "provider_category_name": provider_category,
                        "risk": (
                            "POSSIBLE_STREAM_ID_REUSE_REVIEW_REQUIRED"
                            if reuse_risk
                            else "NAME_OR_CATEGORY_DRIFT"
                        ),
                        "action_taken": "NONE_EXISTING_ROW_PRESERVED",
                    }
                )

    missing_rows: list[dict[str, str]] = []
    selected_servers = {inventory.server_id for inventory in inventories}
    for key, current in existing.items():
        if key[0] not in selected_servers or key in inventory_by_key:
            continue
        missing_rows.append(
            {
                "server_id": key[0],
                "stream_id": key[1],
                "channel_name": streaming.clean_identifier(
                    current.get("channel_name", ""), 300
                ),
                "action": streaming.clean_text(current.get("action", ""), 40),
                "action_taken": "NONE_EXISTING_ROW_PRESERVED",
            }
        )
    new_rows.sort(
        key=lambda row: (row["server_id"], streaming.stream_sort_key(row["stream_id"]))
    )
    changed_rows.sort(
        key=lambda row: (row["server_id"], streaming.stream_sort_key(row["stream_id"]))
    )
    missing_rows.sort(
        key=lambda row: (row["server_id"], streaming.stream_sort_key(row["stream_id"]))
    )
    return new_rows, changed_rows, missing_rows


def select_review_recheck_rows(
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    *,
    selected_servers: Iterable[str],
    quarantined_keys: Iterable[tuple[str, str]] = (),
    changed_rows: Sequence[Mapping[str, str]] = (),
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Select the exact existing rows eligible for a safe Smart-Rules recheck."""

    servers = frozenset(
        streaming.normalize_server_id(value) for value in selected_servers
    )
    if not servers or not servers.issubset(DEFAULT_SERVERS):
        raise SyncError("The REVIEW recheck server selection is invalid.")
    present_keys = {
        (
            streaming.normalize_server_id(inventory.server_id),
            streaming.clean_identifier(channel.get("stream_id", ""), 120),
        )
        for inventory in inventories
        for channel in inventory.channels
    }
    current_native_channels = {
        (
            streaming.normalize_server_id(inventory.server_id),
            streaming.clean_identifier(channel.get("stream_id", ""), 120),
        ): channel
        for inventory in inventories
        for channel in inventory.channels
    }
    blocked_alerts = frozenset(quarantined_keys)
    changed_keys = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        for row in changed_rows
    }
    stats = {
        "review_recheck_selected_server_rows": 0,
        "review_recheck_eligible_rows": 0,
        "review_recheck_excluded_not_review": 0,
        "review_recheck_excluded_enabled": 0,
        "review_recheck_excluded_missing_provider": 0,
        "review_recheck_excluded_open_alert": 0,
        "review_recheck_excluded_changed_identity": 0,
        "review_recheck_excluded_manual_candidate": 0,
    }
    selected: list[dict[str, str]] = []
    for row in table.rows:
        server_id = streaming.normalize_server_id(row.get("server_id", ""))
        if server_id not in servers:
            continue
        stats["review_recheck_selected_server_rows"] += 1
        stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
        key = (server_id, stream_id)
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        if action != "REVIEW":
            stats["review_recheck_excluded_not_review"] += 1
            continue
        try:
            enabled = streaming.parse_bool(
                row.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        except streaming.BuildError as exc:
            raise SyncError("A REVIEW row has an invalid enabled value.") from exc
        if enabled:
            stats["review_recheck_excluded_enabled"] += 1
            continue
        if key not in present_keys:
            stats["review_recheck_excluded_missing_provider"] += 1
            continue
        if key in blocked_alerts:
            stats["review_recheck_excluded_open_alert"] += 1
            continue
        if key in changed_keys:
            stats["review_recheck_excluded_changed_identity"] += 1
            continue

        # Do not overwrite an operator's untracked provisional EPG choice.
        # Auto-map and AI provenance are safe to reconsider, while legacy
        # Server 1 panel candidates are explicitly allowed only so Smart Rules
        # can replace them with EPGShare.
        epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
        notes = streaming.clean_text(row.get("notes", ""), 2000).casefold()
        source = streaming.clean_text(row.get("source", ""), 40).casefold()
        feed = streaming.clean_text(row.get("epg_feed", ""), 80).casefold()
        provenance_known = "auto-map-v1" in notes or "ai-review-v1" in notes
        legacy_server1_panel = server_id == "server_1" and source == "panel"
        current_native_channel = current_native_channels.get(key, {})
        current_native_id = streaming.clean_identifier(
            current_native_channel.get("epg_channel_id", ""), 300
        )
        exact_current_native_id = (
            server_id in {"server_2", "server_3"}
            and bool(current_native_id)
            and epg_id == current_native_id
            and source == "panel"
            and feed in {"panel", "server xmltv.php"}
            and exact_inventory_name_key(row.get("channel_name", ""))
            == exact_inventory_name_key(current_native_channel.get("name", ""))
        )
        if (
            epg_id
            and not provenance_known
            and not legacy_server1_panel
            and not exact_current_native_id
        ):
            stats["review_recheck_excluded_manual_candidate"] += 1
            continue
        selected.append(dict(row))

    selected.sort(
        key=lambda row: (
            row["server_id"], streaming.stream_sort_key(row["stream_id"])
        )
    )
    if len(selected) > MAX_RECHECK_CANDIDATES:
        raise SyncError(
            "The REVIEW recheck candidate set exceeds its conservative "
            f"{MAX_RECHECK_CANDIDATES:,}-row limit. Select one server at a time."
        )
    stats["review_recheck_eligible_rows"] = len(selected)
    # ``excluded_not_review`` describes ordinary mapped rows in the selected
    # server scope, not REVIEW-backlog rows. The user-facing skipped count is
    # therefore the sum of REVIEW rows held back by a safety/eligibility gate.
    stats["review_recheck_skipped_rows"] = sum(
        stats[field]
        for field in (
            "review_recheck_excluded_enabled",
            "review_recheck_excluded_missing_provider",
            "review_recheck_excluded_open_alert",
            "review_recheck_excluded_changed_identity",
            "review_recheck_excluded_manual_candidate",
        )
    )
    return selected, stats


def current_channel_status_by_server(
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    *,
    quarantined_keys: Iterable[tuple[str, str]] = (),
) -> dict[str, dict[str, int | bool]]:
    """Return a compact, identity-safe view of current provider coverage.

    Counts are limited to identities present in the provider inventories from
    this run.  Old Sheet rows for channels no longer returned by a provider do
    not inflate coverage.  A channel counts as EPG-enabled only when the same
    mapping would be runtime-eligible for a real EPGShare or native-panel
    source and is not quarantined by a current safety alert.
    """

    mapping_by_key = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): row
        for row in table.rows
    }
    blocked = {
        (
            streaming.normalize_server_id(server_id),
            streaming.clean_identifier(stream_id, 120),
        )
        for server_id, stream_id in quarantined_keys
    }
    status: dict[str, dict[str, int | bool]] = {
        server_id: {
            "provider_available": False,
            "provider_channels": 0,
            "epgshare_enabled": 0,
            "native_enabled": 0,
            "needs_review": 0,
            "excluded_or_placeholder": 0,
            "safety_excluded": 0,
        }
        for server_id in DEFAULT_SERVERS
    }
    seen_provider_keys: set[tuple[str, str]] = set()

    for inventory in inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        if server_id not in status:
            raise SyncError("A provider inventory has an unsupported server ID.")
        stats = status[server_id]
        stats["provider_available"] = True
        for channel in inventory.channels:
            stream_id = streaming.clean_identifier(channel.get("stream_id", ""), 120)
            if not stream_id:
                raise SyncError("A provider inventory channel is missing its stream ID.")
            key = (server_id, stream_id)
            if key in seen_provider_keys:
                raise SyncError("A provider inventory contains a duplicate channel identity.")
            seen_provider_keys.add(key)
            stats["provider_channels"] = int(stats["provider_channels"]) + 1

            row = mapping_by_key.get(key)
            if row is None:
                stats["needs_review"] = int(stats["needs_review"]) + 1
                continue

            action = (
                streaming.clean_text(row.get("action", "APPROVED"), 40).upper()
                or "APPROVED"
            )
            if action not in streaming.ALLOWED_ACTIONS:
                raise SyncError("A current mapping row has an invalid action.")
            if key in blocked:
                stats["needs_review"] = int(stats["needs_review"]) + 1
                stats["safety_excluded"] = int(stats["safety_excluded"]) + 1
                continue
            if action in REVIEW_QUEUE_ACTIONS:
                stats["needs_review"] = int(stats["needs_review"]) + 1
                continue

            try:
                enabled = streaming.parse_bool(
                    row.get("enabled", ""),
                    default=action not in streaming.REJECTED_ACTIONS,
                    field_name="mapping enabled",
                )
                requested_source = streaming.normalize_requested_source(
                    row.get("source", ""),
                    row.get("epg_feed", ""),
                    row_number=0,
                )
            except streaming.BuildError as exc:
                raise SyncError("A current mapping row has invalid EPG controls.") from exc

            epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
            if enabled and action not in streaming.REJECTED_ACTIONS and not epg_id:
                raise SyncError("An enabled current mapping row is missing its EPG ID.")
            runtime_eligible = (
                enabled
                and action not in streaming.REJECTED_ACTIONS
                and bool(epg_id)
                and not (server_id == "server_1" and requested_source == "panel")
            )
            if runtime_eligible and requested_source == "epgshare01":
                stats["epgshare_enabled"] = int(stats["epgshare_enabled"]) + 1
            elif runtime_eligible and requested_source == "panel":
                stats["native_enabled"] = int(stats["native_enabled"]) + 1
            else:
                stats["excluded_or_placeholder"] = (
                    int(stats["excluded_or_placeholder"]) + 1
                )

        accounted = (
            int(stats["epgshare_enabled"])
            + int(stats["native_enabled"])
            + int(stats["needs_review"])
            + int(stats["excluded_or_placeholder"])
        )
        if accounted != int(stats["provider_channels"]):
            raise SyncError("The compact channel-status counts do not reconcile.")

    return status


def add_channel_status_to_summary(
    summary: dict[str, Any],
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    *,
    quarantined_keys: Iterable[tuple[str, str]] = (),
) -> None:
    summary["channel_status_by_server"] = current_channel_status_by_server(
        table,
        inventories,
        quarantined_keys=quarantined_keys,
    )


def parse_sync_alert_values(values: Sequence[Sequence[Any]]) -> list[dict[str, str]]:
    if not values:
        raise SyncError(
            "The Sync Alerts tab is empty. Import the supplied Version 1 workbook."
        )
    headers = [streaming.normalized_header(value) for value in values[0]]
    if tuple(headers) != ALERT_COLUMNS:
        raise SyncError(
            "The Sync Alerts tab headers are missing or out of order. "
            "Import the supplied Version 1 workbook without rearranging them."
        )
    rows: list[dict[str, str]] = []
    for row_values in values[1:]:
        cells = ["" if value is None else str(value) for value in row_values]
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(ALERT_COLUMNS):
            raise SyncError("The Sync Alerts tab contains a row with extra cells.")
        cells.extend([""] * (len(ALERT_COLUMNS) - len(cells)))
        rows.append(
            {
                header: streaming.unescape_spreadsheet_text(cells[index])
                for index, header in enumerate(ALERT_COLUMNS)
            }
        )
        status = streaming.clean_text(rows[-1].get("status", ""), 20).upper()
        if status not in {"OPEN", "RESOLVED"}:
            raise SyncError(
                "Every Sync Alerts data row must have status OPEN or RESOLVED."
            )
        rows[-1]["status"] = status
        if len(rows) > MAX_SYNC_ALERT_ROWS:
            raise SyncError(
                f"The Sync Alerts tab exceeds its configured "
                f"{MAX_SYNC_ALERT_ROWS:,}-row limit."
            )
    return rows


def sync_alert_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    try:
        server_id = streaming.normalize_server_id(row.get("server_id", ""))
    except streaming.BuildError:
        server_id = streaming.clean_text(row.get("server_id", ""), 40).casefold()
    return (
        server_id,
        streaming.clean_identifier(row.get("stream_id", ""), 120),
        streaming.clean_text(row.get("alert_type", ""), 80).upper(),
        comparison_name_key(row.get("sheet_channel_name", "")),
        comparison_name_key(row.get("provider_channel_name", "")),
        comparison_name_key(row.get("sheet_category_name", "")),
        comparison_name_key(row.get("provider_category_name", "")),
    )


def sync_alert_dedup_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the stable incident key while leaving evidence fields intact."""

    try:
        server_id = streaming.normalize_server_id(row.get("server_id", ""))
    except streaming.BuildError:
        server_id = streaming.clean_text(row.get("server_id", ""), 40).casefold()
    return (
        server_id,
        streaming.clean_identifier(row.get("stream_id", ""), 120),
        streaming.clean_text(row.get("alert_type", ""), 80).upper(),
    )


def pending_sync_alert_rows(
    changed_rows: Sequence[Mapping[str, str]],
    existing_alerts: Sequence[Mapping[str, str]],
    *,
    detected_at: str,
) -> list[dict[str, str]]:
    # One unresolved incident owns one OPEN row. Provider names/categories may
    # continue to drift while that incident awaits review; those changes must
    # not create an unbounded series of duplicate OPEN alerts. The first row
    # still retains the complete evidence observed when it was opened.
    seen = {
        sync_alert_dedup_key(row)
        for row in existing_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    }
    pending: list[dict[str, str]] = []
    for changed in changed_rows:
        if not str(changed.get("risk", "")).startswith("POSSIBLE_"):
            continue
        row = {
            "detected_at": detected_at,
            "server_id": streaming.normalize_server_id(
                changed.get("server_id", "")
            ),
            "stream_id": streaming.clean_identifier(
                changed.get("stream_id", ""), 120
            ),
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": streaming.clean_identifier(
                changed.get("sheet_channel_name", ""), 300
            ),
            "provider_channel_name": streaming.clean_identifier(
                changed.get("provider_channel_name", ""), 300
            ),
            "sheet_category_name": streaming.clean_text(
                changed.get("sheet_category_name", ""), 200
            ),
            "provider_category_name": streaming.clean_text(
                changed.get("provider_category_name", ""), 200
            ),
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
            "review_notes": "",
        }
        identity = sync_alert_dedup_key(row)
        if identity in seen:
            continue
        seen.add(identity)
        pending.append(row)
    return pending


def open_alert_quarantine_keys(
    existing_alerts: Sequence[Mapping[str, str]],
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in existing_alerts:
        if streaming.clean_text(row.get("status", ""), 20).upper() != "OPEN":
            continue
        if (
            streaming.clean_text(row.get("alert_type", ""), 80).upper()
            != "POSSIBLE_STREAM_ID_REUSE"
        ):
            continue
        try:
            server_id = streaming.normalize_server_id(row.get("server_id", ""))
        except streaming.BuildError as exc:
            raise SyncError("An OPEN Sync Alerts row has an invalid server_id.") from exc
        stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
        if not stream_id:
            raise SyncError("An OPEN Sync Alerts row has a blank stream_id.")
        keys.add((server_id, stream_id))
    return keys


def verify_pending_alerts_are_open(
    pending_alerts: Sequence[Mapping[str, str]],
    authoritative_alerts: Sequence[Mapping[str, str]],
) -> None:
    """Require durable OPEN alert identities before any mapping/build proceeds."""
    open_identities = {
        sync_alert_identity(row)
        for row in authoritative_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
        and streaming.clean_text(row.get("alert_type", ""), 80).upper()
        == "POSSIBLE_STREAM_ID_REUSE"
    }
    missing = [
        row
        for row in pending_alerts
        if sync_alert_identity(row) not in open_identities
    ]
    if missing:
        raise SyncError(
            "Google Sheets did not durably store every new OPEN stream-ID reuse "
            "alert; mapping append and build snapshot were blocked."
        )


def verify_appended_mapping_rows(
    expected_rows: Sequence[Mapping[str, str]],
    authoritative_table: MappingTable,
) -> None:
    """Require every appended Version 1 cell to survive the authoritative read."""
    by_key = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): row
        for row in authoritative_table.rows
    }
    for expected_source in expected_rows:
        expected = {
            header: streaming.unescape_spreadsheet_text(
                str(expected_source.get(header, ""))
            )
            for header in streaming.SHEET_COLUMNS
        }
        try:
            expected["server_id"] = streaming.normalize_server_id(
                expected.get("server_id", "")
            )
        except streaming.BuildError as exc:
            raise SyncError("An appended mapping has an invalid server ID.") from exc
        expected["stream_id"] = streaming.clean_identifier(
            expected.get("stream_id", ""), 120
        )
        expected["channel_name"] = streaming.clean_identifier(
            expected.get("channel_name", ""), 300
        )
        key = (expected["server_id"], expected["stream_id"])
        actual = by_key.get(key)
        if actual is None or any(
            str(actual.get(header, "")) != expected[header]
            for header in streaming.SHEET_COLUMNS
        ):
            raise SyncError(
                "Google Sheets did not durably store every cell of every newly "
                "appended mapping; the build snapshot was blocked."
            )


def verify_mapping_append_transition(
    before_table: MappingTable,
    expected_rows: Sequence[Mapping[str, str]],
    after_table: MappingTable,
) -> None:
    """Require append-only growth with every pre-existing row unchanged."""

    before_by_key = _mapping_rows_by_key(before_table)
    after_by_key = _mapping_rows_by_key(after_table)
    expected_keys = {_row_identity(row) for row in expected_rows}
    if len(expected_keys) != len(expected_rows) or expected_keys.intersection(
        before_by_key
    ):
        raise SyncError("The proposed mapping append contains a duplicate identity.")
    if set(after_by_key) != set(before_by_key).union(expected_keys):
        raise SyncError(
            "The Mappings identities changed during the new-channel append; "
            "the build snapshot was blocked."
        )
    for key, (before_row_number, before) in before_by_key.items():
        after_row_number, after = after_by_key[key]
        if before_row_number != after_row_number or before != after:
            raise SyncError(
                "An unrelated Mapping row changed during the new-channel append; "
                "the build snapshot was blocked."
            )
    verify_appended_mapping_rows(expected_rows, after_table)


def inventory_overlap_issues(
    table: MappingTable, inventories: Sequence[PanelInventory]
) -> list[str]:
    """Detect incompatible/rotated stream-ID namespaces before mass append."""
    existing: dict[str, set[str]] = {server: set() for server in DEFAULT_SERVERS}
    for row in table.rows:
        server_id = streaming.normalize_server_id(row.get("server_id", ""))
        existing.setdefault(server_id, set()).add(
            streaming.clean_identifier(row.get("stream_id", ""), 120)
        )
    issues: list[str] = []
    for inventory in inventories:
        sheet_ids = existing.get(inventory.server_id, set())
        provider_ids = {
            streaming.clean_identifier(channel.get("stream_id", ""), 120)
            for channel in inventory.channels
        }
        if len(sheet_ids) < 100 or len(provider_ids) < 100:
            continue
        overlap = len(sheet_ids & provider_ids)
        baseline = min(len(sheet_ids), len(provider_ids))
        if overlap < 50 or overlap / baseline < 0.25:
            issues.append(
                f"{inventory.server_label} stream IDs overlap only {overlap:,} of "
                f"{baseline:,}; mass append was blocked to prevent duplicates."
            )
    return issues


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_csv_report(path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str]) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(
            [streaming.spreadsheet_safe_cell(str(row.get(header, ""))) for header in headers]
        )
    atomic_write_bytes(path, output.getvalue().encode("utf-8"))


class Utf8ByteCounter:
    def __init__(self, maximum: int, *, overflow_message: str):
        self.maximum = int(maximum)
        self.overflow_message = overflow_message
        self.count = 0

    def write(self, value: str) -> int:
        self.count += len(value.encode("utf-8"))
        if self.count > self.maximum:
            raise SyncError(self.overflow_message)
        return len(value)


def validate_projected_sheet_size(
    table: MappingTable, rows_to_append: Sequence[Mapping[str, str]]
) -> int:
    projected_rows = len(table.rows) + len(rows_to_append)
    if projected_rows > MAX_GOOGLE_MAPPING_ROWS:
        raise SyncError(
            "Appending these rows would exceed the conservative "
            f"{MAX_GOOGLE_MAPPING_ROWS:,}-row Google mapping limit."
        )
    counter = Utf8ByteCounter(
        streaming.MAX_MAPPING_BYTES,
        overflow_message=(
            "Appending these rows would make the Sheet snapshot exceed "
            "the EPG builder's 50 MiB input limit."
        ),
    )
    writer = csv.writer(counter, lineterminator="\n")
    writer.writerow(streaming.SHEET_COLUMNS)
    for collection in (table.rows, rows_to_append):
        for row in collection:
            writer.writerow(
                [
                    streaming.spreadsheet_safe_cell(str(row.get(header, "")))
                    for header in streaming.SHEET_COLUMNS
                ]
            )
    return counter.count


def validate_projected_alert_size(
    existing_alerts: Sequence[Mapping[str, str]],
    rows_to_append: Sequence[Mapping[str, str]],
) -> int:
    projected_rows = len(existing_alerts) + len(rows_to_append)
    if projected_rows > MAX_SYNC_ALERT_ROWS:
        raise SyncError(
            "Appending these alerts would exceed the conservative "
            f"{MAX_SYNC_ALERT_ROWS:,}-row Sync Alerts limit."
        )
    counter = Utf8ByteCounter(
        MAX_SYNC_ALERT_BYTES,
        overflow_message=(
            "Appending these alerts would exceed the conservative 8 MiB "
            "Sync Alerts limit."
        ),
    )
    writer = csv.writer(counter, lineterminator="\n")
    writer.writerow(ALERT_COLUMNS)
    for collection in (existing_alerts, rows_to_append):
        for row in collection:
            writer.writerow(
                [
                    streaming.spreadsheet_safe_cell(str(row.get(header, "")))
                    for header in ALERT_COLUMNS
                ]
            )
    return counter.count


def write_reports(
    output_dir: Path,
    *,
    inventories: Sequence[PanelInventory],
    new_rows: Sequence[Mapping[str, str]],
    changed_rows: Sequence[Mapping[str, str]],
    missing_rows: Sequence[Mapping[str, str]],
    summary: Mapping[str, Any],
    review_recheck_rows: Sequence[Mapping[str, str]] = (),
    review_recheck_results: Sequence[Mapping[str, str]] = (),
    ai_review_results: Sequence[Mapping[str, str]] = (),
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_inventory = inventory_rows(inventories)
    inventory_headers = (
        "server_id",
        "server_label",
        "inventory_source",
        "stream_id",
        "channel_name",
        "category_id",
        "category_name",
        "channel_number",
        "native_epg_id_present",
    )
    changed_headers = (
        "server_id",
        "stream_id",
        "different_fields",
        "sheet_channel_name",
        "provider_channel_name",
        "sheet_category_name",
        "provider_category_name",
        "risk",
        "action_taken",
    )
    missing_headers = (
        "server_id",
        "stream_id",
        "channel_name",
        "action",
        "action_taken",
    )
    write_csv_report(output_dir / "inventory.csv", all_inventory, inventory_headers)
    write_csv_report(output_dir / "new_channels.csv", new_rows, streaming.SHEET_COLUMNS)
    write_csv_report(output_dir / "changed_channels.csv", changed_rows, changed_headers)
    write_csv_report(
        output_dir / "possible_id_reuse.csv",
        [row for row in changed_rows if str(row.get("risk", "")).startswith("POSSIBLE_")],
        changed_headers,
    )
    write_csv_report(output_dir / "missing_channels.csv", missing_rows, missing_headers)
    write_csv_report(
        output_dir / "review_recheck_candidates.csv",
        review_recheck_rows,
        streaming.SHEET_COLUMNS,
    )
    write_csv_report(
        output_dir / "review_recheck_results.csv",
        review_recheck_results,
        streaming.SHEET_COLUMNS,
    )
    # Keep proposals inspectable in dry-run without implying they were saved.
    write_csv_report(
        output_dir / "ai_review_results.csv",
        ai_review_results,
        streaming.SHEET_COLUMNS,
    )
    atomic_write_bytes(
        output_dir / "summary.json",
        (json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def write_mapping_snapshot(
    path: Path,
    table: MappingTable,
    *,
    changed_rows: Sequence[Mapping[str, str]] = (),
    persistent_quarantine_keys: Iterable[tuple[str, str]] = (),
) -> None:
    """Write the build snapshot, quarantining severe ID reuse without editing Sheet."""
    quarantine_keys = snapshot_quarantine_keys(
        changed_rows, persistent_quarantine_keys
    )
    effective_rows: list[dict[str, str]] = []
    for original in table.rows:
        row = dict(original)
        if (
            streaming.clean_text(row.get("reason", ""), 500)
            == streaming.EFFECTIVE_QUARANTINE_REASON
        ):
            raise SyncError(
                "A Google Sheet row contains the reserved effective-snapshot "
                "quarantine reason. Remove that copied system-only reason and retry."
            )
        key = (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        if key in quarantine_keys:
            # The builder intentionally includes every enabled row in its
            # metadata output, even when the mapping action is REVIEW.  Keep
            # the private Mappings tab untouched, but disable this row in the
            # ephemeral build snapshot so a suspected recycled stream ID
            # cannot leak stale metadata or an old schedule into publication.
            row["enabled"] = "FALSE"
            row["action"] = "REVIEW"
            row["metadata_status"] = "review"
            row["reason"] = streaming.EFFECTIVE_QUARANTINE_REASON
        effective_rows.append(row)
    write_csv_report(path, effective_rows, streaming.SHEET_COLUMNS)


def snapshot_quarantine_keys(
    changed_rows: Sequence[Mapping[str, str]],
    persistent_quarantine_keys: Iterable[tuple[str, str]],
) -> set[tuple[str, str]]:
    keys = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        for row in changed_rows
        if str(row.get("risk", "")).startswith("POSSIBLE_")
    }
    keys.update(
        (
            streaming.normalize_server_id(server_id),
            streaming.clean_identifier(stream_id, 120),
        )
        for server_id, stream_id in persistent_quarantine_keys
    )
    return keys


def write_private_mapping_snapshot_bundle(
    *,
    effective_path: Path,
    authoritative_path: Path,
    manifest_path: Path,
    table: MappingTable,
    changed_rows: Sequence[Mapping[str, str]] = (),
    persistent_quarantine_keys: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Atomically create and cross-check the private pre/post quarantine hand-off."""
    raw_paths = (
        Path(effective_path),
        Path(authoritative_path),
        Path(manifest_path),
    )
    try:
        for path in raw_paths:
            streaming.reject_symlink_components(
                path, label="private snapshot bundle path"
            )
    except streaming.BuildError as exc:
        raise SyncError(str(exc)) from exc
    effective_path, authoritative_path, manifest_path = (
        path.resolve() for path in raw_paths
    )
    if len({effective_path, authoritative_path, manifest_path}) != 3:
        raise SyncError(
            "Authoritative, effective, and manifest snapshot paths must be different."
        )
    if any(
        streaming.clean_text(row.get("reason", ""), 500)
        == streaming.EFFECTIVE_QUARANTINE_REASON
        for row in table.rows
    ):
        raise SyncError(
            "A Google Sheet row contains the reserved effective-snapshot "
            "quarantine reason. Remove that copied system-only reason and retry."
        )

    persistent_keys = tuple(persistent_quarantine_keys)
    required_keys = snapshot_quarantine_keys(changed_rows, persistent_keys)
    orphan_keys = required_keys - table.keys
    if orphan_keys:
        counts = Counter(server_id for server_id, _stream_id in orphan_keys)
        detail = "; ".join(
            f"{server_id}={count:,}" for server_id, count in sorted(counts.items())
        )
        raise SyncError(
            "OPEN-alert quarantine identities are missing from the authoritative "
            f"Mappings snapshot ({detail}). Restore or resolve them individually."
        )
    applied_keys = required_keys
    write_csv_report(authoritative_path, table.rows, streaming.SHEET_COLUMNS)
    write_mapping_snapshot(
        effective_path,
        table,
        changed_rows=changed_rows,
        persistent_quarantine_keys=persistent_keys,
    )
    try:
        authoritative_content = authoritative_path.read_bytes()
        effective_content = effective_path.read_bytes()
    except OSError as exc:
        raise SyncError("The private mapping snapshot bundle could not be read.") from exc
    selected_servers = {
        streaming.normalize_server_id(row.get("server_id", ""))
        for row in table.rows
    }
    if not selected_servers:
        raise SyncError("The authoritative private mapping snapshot is empty.")
    try:
        validation = streaming.validate_private_mapping_snapshots(
            authoritative_content,
            effective_content,
            selected_servers,
        )
    except streaming.BuildError as exc:
        raise SyncError(str(exc)) from exc

    ordered_applied_keys = sorted(
        ([server_id, stream_id] for server_id, stream_id in applied_keys),
        key=lambda item: (item[0], streaming.stream_sort_key(item[1])),
    )
    applied_digest = hashlib.sha256(
        streaming.json_compact(ordered_applied_keys).encode("utf-8")
    ).hexdigest()
    if applied_digest != validation["quarantine_keys_sha256"]:
        raise SyncError(
            "The effective mapping snapshot did not quarantine exactly the "
            "required current-risk and OPEN-alert identities."
        )
    expected_counts = Counter(server_id for server_id, _stream_id in applied_keys)
    for server_id, stats in validation["servers"].items():
        if stats["quarantined_rows"] != expected_counts.get(server_id, 0):
            raise SyncError(
                "Private mapping quarantine count mismatch for " + server_id + "."
            )

    manifest = streaming.expected_mapping_snapshot_manifest(validation)
    manifest_content = (
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(manifest_path, manifest_content)
    try:
        manifest_check = manifest_path.read_bytes()
        streaming.parse_and_validate_mapping_snapshot_manifest(
            manifest_check, validation
        )
    except (OSError, streaming.BuildError) as exc:
        raise SyncError(str(exc)) from exc
    return validation


def add_snapshot_validation_to_summary(
    summary: dict[str, Any], validation: Mapping[str, Any]
) -> None:
    # The aggregate summary is retained as an artifact in the public
    # repository's manual workflow. Keep exact snapshot/key fingerprints only
    # in the ephemeral private manifest on the runner.
    summary["snapshot_integrity_servers"] = validation["servers"]


def decode_service_account_json(value: str) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        raise SyncError("GOOGLE_SERVICE_ACCOUNT_JSON is required for private Sheet access.")
    if len(raw) > 256 * 1024:
        raise SyncError("The Google service-account secret is unexpectedly large.")
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception:
            raise SyncError("The Google service-account secret is not valid JSON.") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise SyncError("The Google service-account secret is not valid JSON.") from None
    if not isinstance(payload, dict) or payload.get("type") != "service_account":
        raise SyncError("The Google credential must be a service-account JSON key.")
    return payload


def authorized_google_session(service_account_json: str) -> Any:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            decode_service_account_json(service_account_json), scopes=[SHEETS_SCOPE]
        )
        return AuthorizedSession(credentials)
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google Sheets authentication failed.") from None


def validate_sheet_id(value: str) -> str:
    sheet_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", sheet_id):
        raise SyncError("GOOGLE_SHEET_ID is missing or invalid.")
    return sheet_id


def quoted_a1(tab_name: str, suffix: str) -> str:
    tab = streaming.clean_text(tab_name, 100)
    if not tab or any(character in tab for character in "[]:*?/\\"):
        raise SyncError("The Google Sheet tab name is invalid.")
    escaped = tab.replace("'", "''")
    return quote(f"'{escaped}'!{suffix}", safe="!:$'")


def google_sheet_values(session: Any, sheet_id: str, tab_name: str) -> list[list[Any]]:
    # Read one row beyond the Google mapping ceiling so oversized Sheets fail
    # instead of being silently truncated.
    range_value = quoted_a1(
        tab_name, f"A1:AG{MAX_GOOGLE_MAPPING_ROWS + 2}"
    )
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_value}"
    response = None
    try:
        response = session.get(
            url,
            params={
                "majorDimension": "ROWS",
                "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
            },
            timeout=(20, 180),
            stream=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise SyncError(
                "Google Sheets could not read the Mappings tab; check sharing and API setup."
            )
        content = response_body_limited(response, MAX_SHEET_BYTES)
        payload = json.loads(content.decode("utf-8"))
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google Sheets could not read the Mappings tab.") from None
    finally:
        if response is not None:
            close_response(response)
    values = payload.get("values", []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        raise SyncError("Google Sheets returned an invalid values response.")
    return values


def google_sync_alert_values(
    session: Any, sheet_id: str, tab_name: str
) -> list[list[Any]]:
    # Resolve metadata first so a missing starter-workbook tab fails clearly
    # before any Mappings values are appended.
    google_sheet_layout(
        session, sheet_id, tab_name, column_count=len(ALERT_COLUMNS)
    )
    # Header + configured maximum + one sentinel row for truncation detection.
    range_value = quoted_a1(tab_name, f"A1:K{MAX_SYNC_ALERT_ROWS + 2}")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_value}"
    response = None
    try:
        response = session.get(
            url,
            params={
                "majorDimension": "ROWS",
                "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
            },
            timeout=(20, 180),
            stream=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise SyncError(
                "Google Sheets could not read the Sync Alerts tab; "
                "import the supplied Version 1 workbook."
            )
        content = response_body_limited(response, 32 * 1024 * 1024)
        payload = json.loads(content.decode("utf-8"))
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google Sheets could not read the Sync Alerts tab.") from None
    finally:
        if response is not None:
            close_response(response)
    values = payload.get("values", []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        raise SyncError("Google Sheets returned invalid Sync Alerts values.")
    return values


def google_sheet_layout(
    session: Any,
    sheet_id: str,
    tab_name: str,
    *,
    column_count: int,
    expected_used_rows: int | None = None,
) -> GoogleSheetLayout:
    """Return the tab's append/filter model without guessing its workbook type."""
    if not 1 <= int(column_count) <= 18_278:
        raise SyncError("The Google Sheet column count is invalid.")
    if expected_used_rows is not None and int(expected_used_rows) < 1:
        raise SyncError("The Google Sheet used-row count is invalid.")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    response = None
    try:
        response = session.get(
            url,
            params={
                "fields": (
                    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
                    "tables(tableId,name,range,rowsProperties(footerColorStyle)),"
                    "basicFilter)"
                )
            },
            timeout=(20, 180),
            stream=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise SyncError("Google Sheets could not read tab metadata.")
        content = response_body_limited(response, 2 * 1024 * 1024)
        payload = json.loads(content.decode("utf-8"))
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google Sheets could not read tab metadata.") from None
    finally:
        if response is not None:
            close_response(response)
    raw_sheets = payload.get("sheets", []) if isinstance(payload, dict) else []
    if not isinstance(raw_sheets, list):
        raise SyncError("Google Sheets returned invalid spreadsheet metadata.")
    for sheet in raw_sheets:
        if not isinstance(sheet, Mapping):
            raise SyncError("Google Sheets returned invalid spreadsheet metadata.")
        properties = sheet.get("properties", {})
        if not isinstance(properties, Mapping):
            raise SyncError("Google Sheets returned invalid sheet properties.")
        if str(properties.get("title", "")) == tab_name:
            try:
                numeric_sheet_id = int(properties["sheetId"])
            except (KeyError, TypeError, ValueError):
                break
            grid_properties = properties.get("gridProperties")
            grid_row_count: int | None = None
            grid_column_count: int | None = None
            if grid_properties is not None:
                if not isinstance(grid_properties, Mapping):
                    raise SyncError("Google Sheets returned invalid grid metadata.")
                try:
                    grid_row_count = int(grid_properties["rowCount"])
                    grid_column_count = int(grid_properties["columnCount"])
                except (KeyError, TypeError, ValueError):
                    raise SyncError("Google Sheets returned invalid grid metadata.") from None
                if grid_row_count < 1 or grid_column_count < column_count:
                    raise SyncError("The Google Sheet grid is smaller than its data model.")
            raw_tables = sheet.get("tables", [])
            if raw_tables is None:
                raw_tables = []
            if not isinstance(raw_tables, list):
                raise SyncError("Google Sheets returned invalid table metadata.")
            matching_tables: list[Mapping[str, Any]] = []
            for table in raw_tables:
                if not isinstance(table, Mapping):
                    raise SyncError("Google Sheets returned invalid table metadata.")
                table_range = table.get("range", {})
                if not isinstance(table_range, Mapping):
                    raise SyncError("Google Sheets returned invalid table metadata.")
                try:
                    range_sheet_id = int(
                        table_range.get("sheetId", numeric_sheet_id)
                    )
                    start_row = int(table_range.get("startRowIndex", 0))
                    end_row = int(table_range.get("endRowIndex", 0))
                    start_column = int(table_range.get("startColumnIndex", 0))
                    end_column = int(table_range.get("endColumnIndex", 0))
                except (TypeError, ValueError):
                    raise SyncError("Google Sheets returned invalid table metadata.") from None
                if (
                    range_sheet_id == numeric_sheet_id
                    and start_row == 0
                    and end_row >= 1
                    and start_column == 0
                    and end_column == column_count
                ):
                    matching_tables.append(table)
            if raw_tables and len(matching_tables) != 1:
                raise SyncError(
                    f"The Google Sheet tab {tab_name!r} contains a modern table, "
                    "but it does not uniquely cover the complete Version 1 columns. "
                    "Repair the table range before running sync."
                )
            table_id: str | None = None
            table_end_row: int | None = None
            table_has_footer = False
            if matching_tables:
                raw_table_id = matching_tables[0].get("tableId")
                if not isinstance(raw_table_id, str) or not raw_table_id.strip():
                    raise SyncError("Google Sheets returned a table without a tableId.")
                table_id = raw_table_id
                table_end_row = int(matching_tables[0]["range"]["endRowIndex"])
                rows_properties = matching_tables[0].get("rowsProperties", {})
                if not isinstance(rows_properties, Mapping):
                    raise SyncError("Google Sheets returned invalid table row metadata.")
                table_has_footer = "footerColorStyle" in rows_properties
            raw_filter = sheet.get("basicFilter")
            if raw_filter is not None and not isinstance(raw_filter, dict):
                raise SyncError("Google Sheets returned invalid filter metadata.")
            if isinstance(raw_filter, dict):
                filter_table_id = raw_filter.get("tableId")
                if table_id is not None:
                    if (
                        filter_table_id is not None
                        and filter_table_id != ""
                        and filter_table_id != table_id
                    ):
                        raise SyncError(
                            "The Google Sheet table and table-backed filter do not match."
                        )
                elif filter_table_id is not None and filter_table_id != "":
                    raise SyncError(
                        "The Google Sheet has a table-backed filter but no matching "
                        "Version 1 table metadata."
                    )
                elif raw_filter:
                    filter_range = raw_filter.get("range")
                    if not isinstance(filter_range, Mapping):
                        raise SyncError(
                            "The classic Google Sheet filter has no valid range."
                        )
                    try:
                        filter_sheet_id = int(
                            filter_range.get("sheetId", numeric_sheet_id)
                        )
                        filter_start_row = int(filter_range.get("startRowIndex", 0))
                        filter_start_column = int(
                            filter_range.get("startColumnIndex", 0)
                        )
                        filter_end_column = int(
                            filter_range.get("endColumnIndex", 0)
                        )
                        filter_end_row = (
                            int(filter_range["endRowIndex"])
                            if "endRowIndex" in filter_range
                            else None
                        )
                    except (TypeError, ValueError):
                        raise SyncError(
                            "Google Sheets returned invalid filter metadata."
                        ) from None
                    if (
                        filter_sheet_id != numeric_sheet_id
                        or filter_start_row != 0
                        or filter_start_column != 0
                        or filter_end_column != column_count
                        or (filter_end_row is not None and filter_end_row < 1)
                    ):
                        raise SyncError(
                            f"The classic filter on {tab_name!r} does not cover the "
                            "complete Version 1 columns from row 1. Repair it before sync."
                        )
            return GoogleSheetLayout(
                numeric_sheet_id=numeric_sheet_id,
                title=tab_name,
                table_id=table_id,
                table_end_row=table_end_row,
                grid_row_count=grid_row_count,
                grid_column_count=grid_column_count,
                table_has_footer=table_has_footer,
                basic_filter=dict(raw_filter) if raw_filter is not None else None,
            )
    raise SyncError(
        f"The configured Google Sheet tab {tab_name!r} was not found. "
        "Import the supplied Version 1 workbook."
    )


def google_sheet_numeric_id(session: Any, sheet_id: str, tab_name: str) -> int:
    """Backward-compatible metadata helper for the Version 1 Mappings tab."""
    return google_sheet_layout(
        session,
        sheet_id,
        tab_name,
        column_count=len(streaming.SHEET_COLUMNS),
        expected_used_rows=None,
    ).numeric_sheet_id


def parsed_updated_range(
    updated_range: object,
) -> tuple[str, str, int, str, int] | None:
    match = re.fullmatch(
        r"(?:'((?:[^']|'')*)'|([^!]+))!([A-Z]+)(\d+):([A-Z]+)(\d+)",
        str(updated_range or ""),
    )
    if match is None:
        return None
    tab_name = (match.group(1) or match.group(2) or "").replace("''", "'")
    start_column = match.group(3)
    start = int(match.group(4))
    end_column = match.group(5)
    end = int(match.group(6))
    if start < 1 or end < start:
        return None
    return tab_name, start_column, start, end_column, end


def appended_row_range(updated_range: object) -> tuple[int, int] | None:
    parsed = parsed_updated_range(updated_range)
    if parsed is None:
        return None
    return parsed[2], parsed[4]


def build_format_copy_requests(
    numeric_sheet_id: int,
    row_ranges: Sequence[tuple[int, int]],
    *,
    column_count: int = len(streaming.SHEET_COLUMNS),
) -> list[dict[str, Any]]:
    """Copy only format and validation from the workbook's model row 2."""
    requests_body: list[dict[str, Any]] = []
    source = {
        "sheetId": int(numeric_sheet_id),
        "startRowIndex": 1,
        "endRowIndex": 2,
        "startColumnIndex": 0,
        "endColumnIndex": int(column_count),
    }
    for first_row, last_row in row_ranges:
        destination = {
            "sheetId": int(numeric_sheet_id),
            "startRowIndex": int(first_row) - 1,
            "endRowIndex": int(last_row),
            "startColumnIndex": 0,
            "endColumnIndex": int(column_count),
        }
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests_body.append(
                {
                    "copyPaste": {
                        "source": dict(source),
                        "destination": dict(destination),
                        "pasteType": paste_type,
                        "pasteOrientation": "NORMAL",
                    }
                }
            )
    return requests_body


def a1_column_label(column_count: int) -> str:
    """Return the A1 label for a one-based column count (1 -> A, 33 -> AG)."""
    number = int(column_count)
    if not 1 <= number <= 18_278:
        raise SyncError("The Google Sheet column count is invalid.")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def google_write_rejection_message(
    failure_label: str,
    *,
    status: int,
    response_content: bytes,
    operation: str,
) -> str:
    """Describe a Google write failure without reflecting its response body."""
    canonical_status = ""
    try:
        payload = json.loads((response_content or b"{}").decode("utf-8"))
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        candidate = error.get("status", "") if isinstance(error, dict) else ""
        if candidate in GOOGLE_CANONICAL_ERROR_STATUSES:
            canonical_status = candidate
    except (UnicodeDecodeError, json.JSONDecodeError):
        canonical_status = ""

    if status == 400:
        category = "the request or Google table layout was rejected"
    elif status == 401:
        category = "Google authentication was rejected"
    elif status == 403:
        category = (
            "the service account lacks edit access, a protected range blocked the "
            "write, or a Google policy denied it"
        )
    elif status == 404:
        category = "the spreadsheet, tab, or table was not found"
    elif status == 409:
        category = "the spreadsheet changed concurrently"
    elif status == 429:
        category = "the Google Sheets write quota was exceeded"
    elif 500 <= status <= 599:
        category = "Google Sheets had a temporary server failure"
    else:
        category = "Google Sheets returned an unexpected failure"
    status_label = f"HTTP {status}"
    if canonical_status:
        status_label += f" {canonical_status}"
    return (
        f"Google Sheets rejected the {failure_label} {operation} "
        f"({status_label}: {category})."
    )


def build_basic_filter_request(
    layout: GoogleSheetLayout,
    *,
    final_used_row: int,
    column_count: int,
) -> dict[str, Any] | None:
    """Expand/create a classic filter while retaining its active specifications."""
    if final_used_row < 1:
        raise SyncError("The Google Sheet used-row count is invalid.")
    # Round-trip through JSON to avoid mutating the metadata object or nested
    # sort/filter specifications supplied by a caller or fake test session.
    if layout.basic_filter is not None:
        filter_body = json.loads(json.dumps(layout.basic_filter))
        filter_range = filter_body.get("range", {})
        if "endRowIndex" not in filter_range:
            # An omitted GridRange end is unbounded, so it already includes all
            # appended rows and must not be replaced with a smaller bound.
            return None
        existing_end = int(filter_range.get("endRowIndex", 0))
        expanded_end = max(existing_end, int(final_used_row))
        if expanded_end == existing_end:
            return None
        filter_range["endRowIndex"] = expanded_end
        filter_body["range"] = filter_range
    else:
        filter_body = {
            "range": {
                "sheetId": int(layout.numeric_sheet_id),
                "startRowIndex": 0,
                "endRowIndex": int(final_used_row),
                "startColumnIndex": 0,
                "endColumnIndex": int(column_count),
            }
        }
    return {"setBasicFilter": {"filter": filter_body}}


def post_google_batch_update(
    session: Any,
    sheet_id: str,
    requests_body: Sequence[Mapping[str, Any]],
    *,
    failure_message: str,
    appended_count: int,
) -> None:
    if not requests_body:
        return
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    body = {"requests": list(requests_body)}
    encoded_body = json.dumps(
        body, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded_body) > MAX_RECHECK_UPDATE_REQUEST_BYTES:
        raise SyncError("A Google batchUpdate finalization request is too large.")
    # Copy/format/filter updates are idempotent, unlike row appends. Retry only
    # this finalization batch so a transient quota/server fault cannot strand
    # newly appended rows outside the filter.
    # Three range writes at most, plus one final read-only confirmation pass.
    for attempt in range(3):
        response = None
        retryable = False
        try:
            # Send the exact bytes that were measured above. ``json=body``
            # would let requests reserialize Unicode/whitespace differently,
            # invalidating the conservative 2 MiB on-wire ceiling.
            response = session.post(
                url,
                data=encoded_body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=(20, 180),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            content = response_body_limited(response, 2 * 1024 * 1024)
            retryable = status == 429 or 500 <= status <= 599
            if status in {200, 201}:
                try:
                    payload = json.loads((content or b"{}").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if (
                    isinstance(payload, dict)
                    and str(payload.get("spreadsheetId", "")) == sheet_id
                    and isinstance(payload.get("replies"), list)
                    and len(payload["replies"]) == len(body["requests"])
                    and all(isinstance(reply, dict) for reply in payload["replies"])
                ):
                    return
                # A malformed 2xx response is safe to retry because every
                # request in this helper is idempotent.
                retryable = True
        except Exception:
            retryable = True
        finally:
            if response is not None:
                close_response(response)
        if retryable and attempt < 2:
            time.sleep(2**attempt)
        if not retryable or attempt == 2:
            break
    raise SheetWriteError(failure_message, appended_count)


def append_modern_table_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    layout: GoogleSheetLayout,
    values: Sequence[Sequence[object]],
    *,
    existing_data_rows: int,
    chunk_size: int,
    failure_label: str,
) -> int:
    """Append without fixed row addresses, then make the native table cover them.

    ``values.append`` with ``INSERT_ROWS`` is deliberate: Google chooses the
    first free logical row atomically, so an editor or another API caller
    cannot make this process overwrite a row by changing the tab after its
    metadata read.  Imported/header-only native tables do not consistently
    accept ``AppendCellsRequest(tableId=...)``, which is why the generic
    values endpoint is used here as the compatibility path.
    """
    if layout.table_id is None or layout.table_end_row is None:
        raise SyncError("A modern Google table write requires table metadata.")
    if layout.table_has_footer:
        raise SyncError(
            f"The {failure_label} native table has a footer. Remove the footer "
            "before running Version 1 sync so rows cannot be placed after it."
        )
    column_count = len(values[0]) if values else 0
    if column_count < 1 or any(len(row) != column_count for row in values):
        raise SyncError("A Google table write has inconsistent columns.")
    if (
        layout.grid_column_count is not None
        and column_count > layout.grid_column_count
    ):
        raise SyncError("The Google Sheet grid is narrower than the table write.")

    appended, appended_ranges = append_raw_sheet_rows(
        session,
        sheet_id,
        tab_name,
        values,
        existing_data_rows=existing_data_rows,
        column_count=column_count,
        chunk_size=chunk_size,
        failure_label=failure_label,
    )
    required_end_row = max(last_row for _first_row, last_row in appended_ranges)
    maximum_data_rows = (
        MAX_GOOGLE_MAPPING_ROWS
        if column_count == len(streaming.SHEET_COLUMNS)
        else MAX_SYNC_ALERT_ROWS
    )
    finalize_modern_table_range(
        session,
        sheet_id,
        tab_name,
        layout,
        required_end_row=required_end_row,
        column_count=column_count,
        maximum_data_rows=maximum_data_rows,
        failure_label=failure_label,
        appended_count=appended,
    )
    return appended


def google_column_a_used_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    *,
    maximum_data_rows: int,
) -> int:
    """Return the last definitely used row from the mandatory first column."""
    upper_row = int(maximum_data_rows) + 2
    range_value = quoted_a1(tab_name, f"A1:A{upper_row}")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_value}"
    response = None
    try:
        response = session.get(
            url,
            params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"},
            timeout=(20, 180),
            stream=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise SyncError("Google Sheets could not confirm the tab's used rows.")
        content = response_body_limited(response, 4 * 1024 * 1024)
        payload = json.loads(content.decode("utf-8"))
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google Sheets could not confirm the tab's used rows.") from None
    finally:
        if response is not None:
            close_response(response)
    values = payload.get("values", []) if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values:
        raise SyncError("Google Sheets returned invalid used-row values.")
    if len(values) > int(maximum_data_rows) + 1:
        raise SyncError("The Google Sheet exceeds its configured row limit.")
    if not isinstance(values[0], list) or not values[0] or not str(values[0][0]).strip():
        raise SyncError("The Google Sheet first-column header is missing.")
    for row in values[1:]:
        if not isinstance(row, list) or not row or not str(row[0]).strip():
            raise SyncError("The Google Sheet has a blank first-column identity cell.")
    return len(values)


def finalize_modern_table_range(
    session: Any,
    sheet_id: str,
    tab_name: str,
    original_layout: GoogleSheetLayout,
    *,
    required_end_row: int,
    column_count: int,
    maximum_data_rows: int,
    failure_label: str,
    appended_count: int,
) -> None:
    """Make a native table cover all used rows without persisting a stale shrink."""
    maximum_update_attempts = 3
    last_problem = (
        f"Google Sheets stored the {failure_label} rows, but could not confirm "
        "the native-table range; rerun is safe."
    )
    # Each of the first three rounds may issue one update.  The fourth round is
    # deliberately verification-only: a successful third update must be
    # observed authoritatively, but it must never lead to a fourth write.
    for verification_round in range(maximum_update_attempts + 1):
        try:
            latest = google_sheet_layout(
                session,
                sheet_id,
                tab_name,
                column_count=column_count,
                expected_used_rows=None,
            )
            used_end_row = google_column_a_used_rows(
                session,
                sheet_id,
                tab_name,
                maximum_data_rows=maximum_data_rows,
            )
        except SyncError:
            if verification_round < maximum_update_attempts:
                time.sleep(2**verification_round)
                continue
            raise SheetWriteError(last_problem, appended_count) from None
        if (
            latest.table_id != original_layout.table_id
            or latest.table_end_row is None
            or latest.table_has_footer
        ):
            raise SheetWriteError(
                f"Google Sheets changed the {failure_label} native table while "
                "rows were being appended; rerun is safe.",
                appended_count,
            )
        target_end_row = max(
            int(latest.table_end_row), int(required_end_row), int(used_end_row)
        )
        if latest.table_end_row >= target_end_row:
            return
        if (
            latest.grid_row_count is not None
            and target_end_row > latest.grid_row_count
        ):
            raise SheetWriteError(
                f"Google Sheets stored the {failure_label} rows outside the "
                "reported grid; rerun is safe.",
                appended_count,
            )
        if verification_round == maximum_update_attempts:
            raise SheetWriteError(last_problem, appended_count)

        # This target never shrinks either the freshly observed table or the
        # freshly observed data. If another writer wins the race, the next
        # iteration re-reads both high-water marks before doing anything else.
        request_payload = {
            "requests": [
                {
                    "updateTable": {
                        "table": {
                            "tableId": latest.table_id,
                            "range": {
                                "sheetId": latest.numeric_sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": target_end_row,
                                "startColumnIndex": 0,
                                "endColumnIndex": column_count,
                            },
                        },
                        "fields": "range",
                    }
                }
            ]
        }
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
        response = None
        response_content = b""
        try:
            response = session.post(url, json=request_payload, timeout=(20, 180))
            status = int(getattr(response, "status_code", 0) or 0)
            response_content = response_body_limited(response, 2 * 1024 * 1024)
        except Exception:
            last_problem = (
                f"Google Sheets stored the {failure_label} rows, but native-table "
                "range finalization had an uncertain result; rerun is safe."
            )
            if verification_round < maximum_update_attempts:
                time.sleep(2**verification_round)
                continue
            raise SheetWriteError(last_problem, appended_count) from None
        finally:
            if response is not None:
                close_response(response)
        if status not in {200, 201}:
            last_problem = google_write_rejection_message(
                failure_label,
                status=status,
                response_content=response_content,
                operation="table-range finalization",
            )
            if (
                status == 429 or 500 <= status <= 599
            ) and verification_round < maximum_update_attempts:
                time.sleep(2**verification_round)
                continue
            raise SheetWriteError(last_problem, appended_count)
        try:
            payload = json.loads((response_content or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not (
            isinstance(payload, dict)
            and str(payload.get("spreadsheetId", "")) == sheet_id
            and isinstance(payload.get("replies"), list)
            and len(payload["replies"]) == 1
            and isinstance(payload["replies"][0], dict)
        ):
            last_problem = (
                f"Google Sheets stored the {failure_label} rows, but returned an "
                "invalid table-range response; rerun is safe."
            )
            if verification_round < maximum_update_attempts:
                continue
            raise SheetWriteError(last_problem, appended_count)
    raise SheetWriteError(last_problem, appended_count)


def append_raw_sheet_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    values: Sequence[Sequence[object]],
    *,
    existing_data_rows: int,
    column_count: int,
    chunk_size: int,
    failure_label: str,
) -> tuple[int, list[tuple[int, int]]]:
    """Append RAW rows using Google's logical-table allocator.

    Returned row ranges may begin after the caller's expected row when another
    editor appended first.  They may never begin before it, overlap one of our
    earlier chunks, or report a partial write.
    """
    end_column = a1_column_label(column_count)
    range_value = quoted_a1(tab_name, f"A:{end_column}")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
        f"{range_value}:append"
    )
    appended = 0
    appended_ranges: list[tuple[int, int]] = []
    size = max(1, int(chunk_size))
    minimum_next_row = int(existing_data_rows) + 2
    for offset in range(0, len(values), size):
        chunk = values[offset : offset + size]
        if not chunk or any(len(row) != column_count for row in chunk):
            raise SyncError("A Google Sheet append chunk has inconsistent columns.")
        response = None
        response_content = b""
        try:
            response = session.post(
                url,
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": [list(row) for row in chunk]},
                timeout=(20, 180),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            response_content = response_body_limited(response, 2 * 1024 * 1024)
        except Exception:
            raise SheetWriteError(
                f"Google Sheets {failure_label} append had an uncertain "
                "result; rerun will reconcile the authoritative Sheet.",
                appended + len(chunk),
            ) from None
        finally:
            if response is not None:
                close_response(response)
        if status not in {200, 201}:
            raise SheetWriteError(
                google_write_rejection_message(
                    failure_label,
                    status=status,
                    response_content=response_content,
                    operation="append",
                ),
                appended,
            )
        try:
            payload = json.loads((response_content or b"{}").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        updates = payload.get("updates", {}) if isinstance(payload, dict) else None
        parsed_range = (
            parsed_updated_range(updates.get("updatedRange", ""))
            if isinstance(updates, Mapping)
            else None
        )
        parsed_table_range = (
            parsed_updated_range(payload.get("tableRange", ""))
            if isinstance(payload, Mapping)
            else None
        )
        row_range = (
            (parsed_range[2], parsed_range[4])
            if parsed_range is not None
            else None
        )
        try:
            updated_rows = (
                int(updates.get("updatedRows", 0))
                if isinstance(updates, Mapping)
                else 0
            )
            updated_columns = (
                int(updates.get("updatedColumns", 0))
                if isinstance(updates, Mapping)
                else 0
            )
            updated_cells = (
                int(updates.get("updatedCells", 0))
                if isinstance(updates, Mapping)
                else 0
            )
        except (TypeError, ValueError):
            updated_rows = 0
            updated_columns = 0
            updated_cells = 0
        range_rows = row_range[1] - row_range[0] + 1 if row_range else 0
        if (
            not isinstance(payload, dict)
            or str(payload.get("spreadsheetId", "")) != sheet_id
            or not isinstance(updates, dict)
            or str(updates.get("spreadsheetId", "")) != sheet_id
            or parsed_range is None
            or parsed_range[0] != tab_name
            or parsed_range[1] != "A"
            or parsed_range[3] != end_column
            or row_range is None
            or parsed_table_range is None
            or parsed_table_range[0] != tab_name
            or parsed_table_range[1] != "A"
            or parsed_table_range[2] != 1
            or parsed_table_range[3] != end_column
            or parsed_table_range[4] != row_range[0] - 1
            or updated_rows != len(chunk)
            or updated_columns != column_count
            or updated_cells != len(chunk) * column_count
            or range_rows != len(chunk)
            or row_range[0] < minimum_next_row
        ):
            raise SheetWriteError(
                f"Google Sheets reported an unexpected or partial {failure_label} "
                "append range; rerun will reconcile the authoritative Sheet.",
                appended + len(chunk),
            )
        appended_ranges.append(row_range)
        appended += len(chunk)
        minimum_next_row = row_range[1] + 1
    return appended, appended_ranges


def append_classic_sheet_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    layout: GoogleSheetLayout,
    values: Sequence[Sequence[object]],
    *,
    existing_data_rows: int,
    column_count: int,
    chunk_size: int,
    copy_format_validation: bool,
    failure_label: str,
) -> int:
    """Append to a classic tab, then reliably expand/create its basic filter."""
    appended, appended_ranges = append_raw_sheet_rows(
        session,
        sheet_id,
        tab_name,
        values,
        existing_data_rows=existing_data_rows,
        column_count=column_count,
        chunk_size=chunk_size,
        failure_label=failure_label,
    )

    final_used_row = max(
        1,
        int(existing_data_rows) + 1 + appended,
        max((last_row for _first_row, last_row in appended_ranges), default=1),
    )
    requests_body: list[dict[str, Any]] = []
    if copy_format_validation and appended_ranges:
        requests_body.extend(
            build_format_copy_requests(
                layout.numeric_sheet_id,
                appended_ranges,
                column_count=column_count,
            )
        )
    filter_request = build_basic_filter_request(
        layout,
        final_used_row=final_used_row,
        column_count=column_count,
    )
    if filter_request is not None:
        requests_body.append(filter_request)
    post_google_batch_update(
        session,
        sheet_id,
        requests_body,
        failure_message=(
            f"Google Sheets {failure_label} rows were reconciled, but filter/format "
            "expansion failed; rerun is safe."
        ),
        appended_count=appended,
    )
    return appended


def append_google_sheet_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    table: MappingTable,
    rows: Sequence[Mapping[str, str]],
    *,
    chunk_size: int = 500,
) -> int:
    """Append REVIEW rows through the tab's explicit table/filter model."""
    validate_projected_sheet_size(table, rows)
    missing_columns = sorted(set(streaming.SHEET_COLUMNS) - set(table.headers))
    if missing_columns:
        raise SyncError(
            "The Google Sheet is missing Version 1 columns: " + ", ".join(missing_columns)
        )
    if tuple(table.headers) != tuple(streaming.SHEET_COLUMNS):
        raise SyncError("The Google Sheet headers are not in Version 1 order.")
    if not rows:
        return 0
    layout = google_sheet_layout(
        session,
        sheet_id,
        tab_name,
        column_count=len(streaming.SHEET_COLUMNS),
        expected_used_rows=len(table.rows) + 1,
    )
    values = [
        [str(row.get(header, "")) for header in streaming.SHEET_COLUMNS]
        for row in rows
    ]
    if layout.table_id is not None:
        return append_modern_table_rows(
            session,
            sheet_id,
            tab_name,
            layout,
            values,
            existing_data_rows=len(table.rows),
            chunk_size=chunk_size,
            failure_label="Mappings",
        )
    return append_classic_sheet_rows(
        session,
        sheet_id,
        tab_name,
        layout,
        values,
        existing_data_rows=len(table.rows),
        column_count=len(streaming.SHEET_COLUMNS),
        chunk_size=chunk_size,
        copy_format_validation=True,
        failure_label="Mappings",
    )


def mapping_table_fingerprint(table: MappingTable) -> str:
    """Fingerprint exact row order, physical locations and Version 1 values."""

    digest = hashlib.sha256()
    digest.update(b"skytv-mapping-table-v1\0")
    for header in table.headers:
        encoded = header.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    for row_number, row in zip(table.row_numbers, table.rows):
        digest.update(int(row_number).to_bytes(8, "big"))
        for header in table.headers:
            encoded = str(row.get(header, "")).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _mapping_rows_by_key(
    table: MappingTable,
) -> dict[tuple[str, str], tuple[int, dict[str, str]]]:
    return {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): (row_number, row)
        for row_number, row in zip(table.row_numbers, table.rows)
    }


def _validate_review_update(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    verified_native_updates: Mapping[
        tuple[str, str], Mapping[str, str]
    ] | None = None,
    verified_ai_updates: Mapping[
        tuple[str, str], Mapping[str, str]
    ] | None = None,
) -> None:
    changed = {
        header
        for header in streaming.SHEET_COLUMNS
        if str(before.get(header, "")) != str(after.get(header, ""))
    }
    if not changed or not changed.issubset(RECHECK_PATCH_COLUMNS):
        raise SyncError("A REVIEW update attempted to change unsupported columns.")
    before_action = streaming.clean_text(before.get("action", ""), 40).upper()
    if before_action != "REVIEW":
        raise SyncError("A REVIEW update no longer targets a REVIEW row.")
    try:
        before_enabled = streaming.parse_bool(
            before.get("enabled", ""), default=False, field_name="mapping enabled"
        )
        after_enabled = streaming.parse_bool(
            after.get("enabled", ""), default=False, field_name="mapping enabled"
        )
    except streaming.BuildError as exc:
        raise SyncError("A REVIEW update contains an invalid enabled value.") from exc
    if before_enabled:
        raise SyncError("An enabled mapping cannot enter REVIEW recheck updates.")
    server_id = streaming.normalize_server_id(before.get("server_id", ""))
    after_action = streaming.clean_text(after.get("action", ""), 40).upper()
    source = streaming.clean_text(after.get("source", ""), 40).casefold()
    feed = streaming.clean_text(after.get("epg_feed", ""), 80).upper()
    epg_id = streaming.clean_identifier(after.get("epg_id", ""), 300)
    key = (
        server_id,
        streaming.clean_identifier(before.get("stream_id", ""), 120),
    )
    is_verified_native = False
    is_verified_ai = False
    is_verified_placeholder = False
    is_verified_ignore = False
    if after_action == "AUTO_EPGSHARE":
        if not after_enabled:
            raise SyncError("A verified Smart-Rules approval must be enabled.")
        notes = streaming.clean_text(after.get("notes", ""), 2000).casefold()
        if "ai-verified-v2" in notes:
            expected_ai = (verified_ai_updates or {}).get(key)
            if expected_ai is None or any(
                str(expected_ai.get(header, "")) != str(after.get(header, ""))
                for header in streaming.SHEET_COLUMNS
            ):
                raise SyncError(
                    "An AI-assisted REVIEW approval lacks its exact verified "
                    "policy allowlist entry."
                )
            is_verified_ai = True
        elif "auto-map-v1" not in notes:
            raise SyncError(
                "A Smart-Rules REVIEW approval lacks exact matcher provenance."
            )
    elif after_action == "AUTO_DUMMY":
        if (
            not after_enabled
            or source != "dummy"
            or feed != "DUMMY_CHANNELS"
            or not epg_id
            or "auto-map-v1" not in streaming.clean_text(
                after.get("notes", ""), 2000
            ).casefold()
        ):
            raise SyncError(
                "A verified no-schedule classification violates the dummy policy."
            )
        is_verified_placeholder = True
    elif after_action == "IGNORE":
        if (
            after_enabled
            or source != "dummy"
            or "method=heading_placeholder" not in streaming.clean_text(
                after.get("notes", ""), 2000
            ).casefold()
        ):
            raise SyncError(
                "An ignored heading lacks its exact Smart-Rules classification."
            )
        is_verified_ignore = True
    elif after_action == "KEEP_PANEL":
        expected_native = (verified_native_updates or {}).get(key)
        if expected_native is None or any(
            str(expected_native.get(header, "")) != str(after.get(header, ""))
            for header in streaming.SHEET_COLUMNS
        ):
            raise SyncError(
                "A native REVIEW approval lacks its exact verified allowlist entry."
            )
        if (
            server_id not in {"server_2", "server_3"}
            or not after_enabled
            or source != "panel"
            or feed != "PANEL"
            or not epg_id
            or "native-review-v1" not in streaming.clean_text(
                after.get("notes", ""), 2000
            ).casefold()
        ):
            raise SyncError("A native REVIEW approval violates the panel policy.")
        is_verified_native = True
    elif after_action == "REVIEW":
        if after_enabled:
            raise SyncError("An AI suggestion must remain disabled in REVIEW.")
    else:
        raise SyncError(
            "A REVIEW recheck may only approve, classify, ignore, or retain REVIEW."
        )
    has_exact_ai_target = (
        source == "epgshare01" and feed == "ALL_SOURCES1" and bool(epg_id)
    )
    is_ai_abstention_marker = (
        after_action == "REVIEW"
        and "ai-review-v1" in streaming.clean_text(
            after.get("notes", ""), 2000
        ).casefold()
        and all(
            str(after.get(column, "")) == str(before.get(column, ""))
            for column in ("source", "epg_feed", "epg_id")
        )
    )
    if (
        not is_verified_native
        and not is_verified_ai
        and not is_verified_placeholder
        and not is_verified_ignore
        and not has_exact_ai_target
        and not is_ai_abstention_marker
    ):
        raise SyncError(
            "A REVIEW update requires an exact EPGShare target or a bounded "
            "Gemini abstention marker."
        )
    if (
        server_id == "server_1"
        and source == "panel"
        and not is_ai_abstention_marker
    ):
        raise SyncError("Server 1 native panel EPG is forbidden.")


def _review_update_requests(
    *,
    numeric_sheet_id: int,
    row_number: int,
    row: Mapping[str, str],
) -> list[dict[str, Any]]:
    column_indexes = {header: index for index, header in enumerate(streaming.SHEET_COLUMNS)}
    groups = (
        ("enabled",),
        ("action", "source", "epg_feed", "epg_id"),
        ("reason", "notes"),
    )
    requests_body: list[dict[str, Any]] = []
    for columns in groups:
        start_column = column_indexes[columns[0]]
        values = [
            {"userEnteredValue": {"stringValue": str(row.get(column, ""))}}
            for column in columns
        ]
        requests_body.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": int(numeric_sheet_id),
                        "startRowIndex": int(row_number) - 1,
                        "endRowIndex": int(row_number),
                        "startColumnIndex": start_column,
                        "endColumnIndex": start_column + len(columns),
                    },
                    "rows": [{"values": values}],
                    "fields": "userEnteredValue",
                }
            }
        )
    return requests_body


def _bounded_deterministic_review_updates(
    epgshare_updates: Sequence[dict[str, str]],
    native_updates: Sequence[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Select a count-bounded deterministic write prefix.

    EPGShare keeps its established priority. Native updates use the remaining
    run capacity. The writer splits this prefix into independently verified,
    count- and byte-bounded Google batches so one large request is never
    required and an interrupted run can resume from the remaining REVIEW rows.
    """

    selected_epgshare: list[dict[str, str]] = []
    selected_native: list[dict[str, str]] = []
    for lane, rows in (("epgshare", epgshare_updates), ("native", native_updates)):
        for row in rows:
            if (
                len(selected_epgshare) + len(selected_native)
                >= MAX_RECHECK_APPLIES_PER_RUN
            ):
                return selected_epgshare, selected_native
            # Prove that even a single maximum-sized row can form a legal
            # batch. Cumulative sizing happens later with real row locations.
            requests_for_row = _review_update_requests(
                numeric_sheet_id=2_147_483_647,
                row_number=MAX_GOOGLE_MAPPING_ROWS + 1,
                row=row,
            )
            row_estimate = len(
                json.dumps(
                    {"requests": requests_for_row},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if row_estimate > MAX_RECHECK_DETERMINISTIC_REQUEST_BYTES:
                raise SyncError(
                    "A deterministic REVIEW update cannot fit in one "
                    "conservative Google request."
                )
            if lane == "epgshare":
                selected_epgshare.append(row)
            else:
                selected_native.append(row)
    return selected_epgshare, selected_native


def _verify_review_update_result(
    before_table: MappingTable,
    after_table: MappingTable,
    updates: Mapping[tuple[str, str], Mapping[str, str]],
) -> None:
    before_by_key = _mapping_rows_by_key(before_table)
    after_by_key = _mapping_rows_by_key(after_table)
    if set(before_by_key) != set(after_by_key):
        raise SyncError("The Mappings identities changed while REVIEW rows were updated.")
    for key, (before_row_number, before) in before_by_key.items():
        after_row_number, after = after_by_key[key]
        if before_row_number != after_row_number:
            raise SyncError("The Mappings row order changed during REVIEW updates.")
        expected = updates.get(key)
        if expected is None:
            if before != after:
                raise SyncError("An unrelated Mapping row changed during REVIEW updates.")
            continue
        for header in streaming.SHEET_COLUMNS:
            wanted = (
                str(expected.get(header, ""))
                if header in RECHECK_PATCH_COLUMNS
                else str(before.get(header, ""))
            )
            if str(after.get(header, "")) != wanted:
                raise SyncError("Google Sheets did not durably store a complete REVIEW update.")


def _review_update_targets_match(
    before_table: MappingTable,
    after_table: MappingTable,
    updates: Mapping[tuple[str, str], Mapping[str, str]],
) -> bool:
    """Return whether every requested target is durably present.

    This deliberately ignores unrelated rows.  It is used only to report an
    accurate committed prefix after the stricter whole-table transition guard
    has already failed; unrelated concurrent edits still stop the run.
    """

    before_by_key = _mapping_rows_by_key(before_table)
    after_by_key = _mapping_rows_by_key(after_table)
    for key, expected in updates.items():
        before_entry = before_by_key.get(key)
        after_entry = after_by_key.get(key)
        if before_entry is None or after_entry is None:
            return False
        before_row_number, before = before_entry
        after_row_number, after = after_entry
        if before_row_number != after_row_number:
            return False
        for header in streaming.SHEET_COLUMNS:
            wanted = (
                str(expected.get(header, ""))
                if header in RECHECK_PATCH_COLUMNS
                else str(before.get(header, ""))
            )
            if str(after.get(header, "")) != wanted:
                return False
    return True


def update_google_sheet_review_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    base_table: MappingTable,
    rows: Sequence[Mapping[str, str]],
    *,
    pre_write_check: Callable[[], None] | None = None,
    batch_pre_write_check: (
        Callable[[Sequence[Mapping[str, str]]], None] | None
    ) = None,
    verified_native_rows: Sequence[Mapping[str, str]] = (),
    verified_ai_rows: Sequence[Mapping[str, str]] = (),
) -> tuple[int, MappingTable]:
    """Patch REVIEW rows in verified, resumable Google batches.

    Each batch has its own authoritative pre-read, OPEN-alert authority check,
    mutating request, and authoritative post-read. Earlier confirmed batches
    therefore remain a safe resume point if a later request is interrupted.
    """

    if not rows:
        return 0, base_table
    if len(rows) > MAX_RECHECK_TOTAL_UPDATES:
        raise SyncError("The REVIEW update batch exceeds its conservative limit.")
    desired: dict[tuple[str, str], dict[str, str]] = {}
    verified_native_updates: dict[tuple[str, str], dict[str, str]] = {}
    verified_ai_updates: dict[tuple[str, str], dict[str, str]] = {}
    for raw in verified_native_rows:
        key = (
            streaming.normalize_server_id(raw.get("server_id", "")),
            streaming.clean_identifier(raw.get("stream_id", ""), 120),
        )
        if key in verified_native_updates:
            raise SyncError("The native REVIEW allowlist contains a duplicate identity.")
        verified_native_updates[key] = {
            header: str(raw.get(header, "")) for header in streaming.SHEET_COLUMNS
        }
    for raw in verified_ai_rows:
        key = (
            streaming.normalize_server_id(raw.get("server_id", "")),
            streaming.clean_identifier(raw.get("stream_id", ""), 120),
        )
        if key in verified_ai_updates:
            raise SyncError("The AI REVIEW allowlist contains a duplicate identity.")
        verified_ai_updates[key] = {
            header: str(raw.get(header, "")) for header in streaming.SHEET_COLUMNS
        }
    base_by_key = _mapping_rows_by_key(base_table)
    for raw in rows:
        key = (
            streaming.normalize_server_id(raw.get("server_id", "")),
            streaming.clean_identifier(raw.get("stream_id", ""), 120),
        )
        if key in desired or key not in base_by_key:
            raise SyncError("A REVIEW update contains an unknown or duplicate identity.")
        _row_number, before = base_by_key[key]
        after = {header: str(raw.get(header, "")) for header in streaming.SHEET_COLUMNS}
        _validate_review_update(
            before,
            after,
            verified_native_updates=verified_native_updates,
            verified_ai_updates=verified_ai_updates,
        )
        desired[key] = after
    native_desired_keys = {
        key
        for key, row in desired.items()
        if streaming.clean_text(row.get("action", ""), 40).upper()
        == "KEEP_PANEL"
    }
    if set(verified_native_updates) != native_desired_keys:
        raise SyncError(
            "The native REVIEW allowlist does not exactly match native updates."
        )
    ai_desired_keys = {
        key
        for key, row in desired.items()
        if "ai-verified-v2"
        in streaming.clean_text(row.get("notes", ""), 2000).casefold()
    }
    if set(verified_ai_updates) != ai_desired_keys:
        raise SyncError(
            "The AI REVIEW allowlist does not exactly match AI-assisted updates."
        )

    layout = google_sheet_layout(
        session,
        sheet_id,
        tab_name,
        column_count=len(streaming.SHEET_COLUMNS),
        expected_used_rows=len(base_table.rows) + 1,
    )
    base_locations = _mapping_rows_by_key(base_table)
    ordered_keys = [
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        for row in rows
    ]
    batches: list[
        tuple[dict[tuple[str, str], dict[str, str]], list[dict[str, Any]]]
    ] = []
    batch_desired: dict[tuple[str, str], dict[str, str]] = {}
    batch_requests: list[dict[str, Any]] = []
    empty_body_bytes = len(b'{"requests":[]}')
    batch_bytes = empty_body_bytes
    for key in ordered_keys:
        row_number, _before = base_locations[key]
        row_requests = _review_update_requests(
            numeric_sheet_id=layout.numeric_sheet_id,
            row_number=row_number,
            row=desired[key],
        )
        serialized_requests = [
            len(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for request in row_requests
        ]
        row_bytes = sum(serialized_requests) + len(row_requests) - (
            1 if not batch_requests else 0
        )
        if batch_desired and (
            len(batch_desired) >= MAX_RECHECK_UPDATE_ROWS_PER_BATCH
            or batch_bytes + row_bytes > MAX_RECHECK_UPDATE_REQUEST_BYTES
        ):
            batches.append((batch_desired, batch_requests))
            batch_desired = {}
            batch_requests = []
            batch_bytes = empty_body_bytes
            row_bytes = sum(serialized_requests) + len(row_requests) - 1
        if batch_bytes + row_bytes > MAX_RECHECK_UPDATE_REQUEST_BYTES:
            raise SyncError(
                "A REVIEW update cannot fit in one conservative Google request."
            )
        batch_desired[key] = desired[key]
        batch_requests.extend(row_requests)
        batch_bytes += row_bytes
    if batch_desired:
        batches.append((batch_desired, batch_requests))

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    current_table = base_table
    committed_count = 0
    for current_desired, requests_body in batches:
        # Provider/native validation can require multiple network downloads.
        # Run that slower check first, then make the authoritative Sheet and
        # OPEN-alert reads the final preconditions immediately before POST.
        try:
            if batch_pre_write_check is not None:
                batch_pre_write_check(tuple(current_desired.values()))
            fresh = parse_table_values(
                google_sheet_values(
                    session, validate_sheet_id(sheet_id), tab_name
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
            if mapping_table_fingerprint(fresh) != mapping_table_fingerprint(
                current_table
            ):
                message = (
                    "The Mappings tab changed before REVIEW updates; no further "
                    "updates were sent."
                )
                if committed_count:
                    raise SheetWriteError(message, committed_count)
                raise SyncError(message)
            fresh_by_key = _mapping_rows_by_key(fresh)
            for key, wanted in current_desired.items():
                _row_number, before = fresh_by_key[key]
                _validate_review_update(
                    before,
                    wanted,
                    verified_native_updates=verified_native_updates,
                    verified_ai_updates=verified_ai_updates,
                )
            if pre_write_check is not None:
                # Recheck Sync Alerts after provider validation and adjacent to
                # every mutating request.
                pre_write_check()
        except SheetWriteError:
            raise
        except SyncError as exc:
            if committed_count:
                raise SheetWriteError(str(exc), committed_count) from None
            raise

        body = {"requests": requests_body}
        encoded_body = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(current_desired) > MAX_RECHECK_UPDATE_ROWS_PER_BATCH:
            raise SyncError("A REVIEW update batch exceeds its row limit.")
        if len(encoded_body) > MAX_RECHECK_UPDATE_REQUEST_BYTES:
            raise SyncError(
                "A REVIEW update request exceeds its conservative size limit."
            )

        response = None
        response_content = b""
        uncertain = False
        rejection_message = ""
        try:
            response = session.post(
                url,
                data=encoded_body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=(20, 180),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            response_content = response_body_limited(response, 2 * 1024 * 1024)
            if status not in {200, 201}:
                uncertain = True
                rejection_message = google_write_rejection_message(
                    "Mappings REVIEW",
                    status=status,
                    response_content=response_content,
                    operation="atomic update",
                )
            else:
                try:
                    payload = json.loads(
                        (response_content or b"{}").decode("utf-8")
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if not (
                    isinstance(payload, dict)
                    and str(payload.get("spreadsheetId", "")) == sheet_id
                    and isinstance(payload.get("replies"), list)
                    and len(payload["replies"]) == len(requests_body)
                    and all(isinstance(reply, dict) for reply in payload["replies"])
                ):
                    uncertain = True
        except Exception:
            uncertain = True
        finally:
            if response is not None:
                close_response(response)

        observed_table: MappingTable | None = None
        try:
            observed_table = parse_table_values(
                google_sheet_values(
                    session, validate_sheet_id(sheet_id), tab_name
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
            _verify_review_update_result(fresh, observed_table, current_desired)
        except SyncError as exc:
            committed_after_failure = committed_count
            if (
                observed_table is not None
                and _review_update_targets_match(
                    fresh, observed_table, current_desired
                )
            ):
                committed_after_failure += len(current_desired)
            if uncertain:
                raise SheetWriteError(
                    rejection_message
                    or (
                        "Google Sheets returned an uncertain REVIEW update result "
                        "and the authoritative re-read did not confirm every target."
                    ),
                    committed_after_failure,
                ) from None
            raise SheetWriteError(str(exc), committed_after_failure) from None
        committed_count += len(current_desired)
        current_table = observed_table

    return committed_count, current_table


def append_sync_alert_rows(
    session: Any,
    sheet_id: str,
    tab_name: str,
    existing_alerts: Sequence[Mapping[str, str]],
    rows: Sequence[Mapping[str, str]],
    *,
    chunk_size: int = 500,
) -> int:
    """Append deduplicated private alerts through its table/filter model."""
    validate_projected_alert_size(existing_alerts, rows)
    if not rows:
        return 0
    layout = google_sheet_layout(
        session,
        sheet_id,
        tab_name,
        column_count=len(ALERT_COLUMNS),
        expected_used_rows=len(existing_alerts) + 1,
    )
    values = [
        [str(row.get(header, "")) for header in ALERT_COLUMNS]
        for row in rows
    ]
    if layout.table_id is not None:
        return append_modern_table_rows(
            session,
            sheet_id,
            tab_name,
            layout,
            values,
            existing_data_rows=len(existing_alerts),
            chunk_size=chunk_size,
            failure_label="Sync Alerts",
        )
    return append_classic_sheet_rows(
        session,
        sheet_id,
        tab_name,
        layout,
        values,
        existing_data_rows=len(existing_alerts),
        column_count=len(ALERT_COLUMNS),
        chunk_size=chunk_size,
        copy_format_validation=True,
        failure_label="Sync Alerts",
    )


def load_server_configs(selected_servers: Sequence[str]) -> list[ServerConfig]:
    result: list[ServerConfig] = []
    seen: set[str] = set()
    for server_value in selected_servers:
        try:
            server_id = streaming.normalize_server_id(server_value)
        except streaming.BuildError as exc:
            raise SyncError(f"Invalid server selection: {server_value!r}.") from exc
        if server_id in seen:
            raise SyncError(f"Duplicate server selection: {server_id}.")
        seen.add(server_id)
        number = server_id.rsplit("_", 1)[-1]
        prefix = f"SERVER_{number}_"
        base_url = os.environ.get(prefix + "BASE_URL", "").strip()
        username = os.environ.get(prefix + "USERNAME", "").strip()
        password = os.environ.get(prefix + "PASSWORD", "")
        if not base_url or not username or not password:
            raise SyncError(
                f"Server {number} is missing BASE_URL, USERNAME, or PASSWORD secrets."
            )
        result.append(
            ServerConfig(
                server_id=server_id,
                server_label=f"Server {number}",
                base_url=base_url,
                username=username,
                password=password,
            )
        )
    return result


def parse_server_minimums(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise SyncError(
                "--minimum-server-channels must use server_1=4000 form."
            )
        server_text, count_text = value.split("=", 1)
        try:
            server_id = streaming.normalize_server_id(server_text)
            count = int(count_text)
        except (streaming.BuildError, ValueError) as exc:
            raise SyncError(f"Invalid minimum channel floor: {value!r}.") from exc
        if not 1 <= count <= MAX_CHANNELS_PER_SERVER:
            raise SyncError(f"Minimum channel floor for {server_id} is out of range.")
        if server_id in result:
            raise SyncError(f"Duplicate minimum channel floor for {server_id}.")
        result[server_id] = count
    return result


def generated_timestamp(value: str = "") -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SyncError("--now-utc must be an ISO-8601 timestamp.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        streaming.normalize_server_id(row.get("server_id", "")),
        streaming.clean_identifier(row.get("stream_id", ""), 120),
    )


def _native_review_summary_defaults() -> dict[str, int]:
    return {
        "native_review_candidates": 0,
        "native_review_verified": 0,
        "native_review_persisted": 0,
        "native_review_deferred": 0,
        "native_review_source_unavailable": 0,
        "native_review_rejected_stored_conflict": 0,
        "native_review_rejected_invalid_id": 0,
        "native_review_rejected_provenance": 0,
        "native_review_rejected_schedule": 0,
        "native_review_rejected_identity": 0,
        "native_review_revalidation_checked": 0,
        "native_review_revalidation_rejected": 0,
        "native_review_revalidation_unavailable": 0,
    }


def _parse_generated_epoch(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("The REVIEW recheck timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _build_verified_native_review_updates(
    *,
    review_input_rows: Sequence[Mapping[str, str]],
    review_result_rows: Sequence[Mapping[str, str]],
    inventories: Sequence[PanelInventory],
    generated_at: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Return exact Server 2/3 KEEP_PANEL updates after the native guide gate."""

    summary = _native_review_summary_defaults()
    if native_review is None:
        raise SyncError(
            "Native REVIEW validation was requested, but its verified module is missing."
        )
    before_by_key = {_row_identity(row): row for row in review_input_rows}
    if len(before_by_key) != len(review_input_rows):
        raise SyncError("The native REVIEW input contains duplicate identities.")
    channels_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for inventory in inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        for channel in inventory.channels:
            key = (
                server_id,
                streaming.clean_identifier(channel.get("stream_id", ""), 120),
            )
            if key in channels_by_key:
                raise SyncError("Provider inventories contain a duplicate stream identity.")
            channels_by_key[key] = channel

    candidates: dict[
        str, dict[tuple[str, str], tuple[dict[str, str], str, str]]
    ] = {"server_2": {}, "server_3": {}}
    for raw_result in review_result_rows:
        key = _row_identity(raw_result)
        before = before_by_key.get(key)
        if before is None or key[0] not in candidates:
            continue
        result_action = streaming.clean_text(
            raw_result.get("action", ""), 40
        ).upper()
        is_coverage_fallback = (
            result_action == "AUTO_DUMMY"
            and streaming.clean_text(raw_result.get("source", ""), 40).casefold()
            == "dummy"
            and streaming.clean_text(raw_result.get("epg_feed", ""), 80).upper()
            == "DUMMY_CHANNELS"
            and streaming.clean_text(raw_result.get("notes", ""), 2_000).startswith(
                "coverage-fallback-v1 "
            )
            and streaming.clean_text(raw_result.get("reason", ""), 500).startswith(
                "coverage-fallback-rollback-v1 "
            )
        )
        if result_action != "REVIEW" and not is_coverage_fallback:
            # A deterministic EPGShare approval wins and is never overwritten.
            continue
        try:
            enabled = streaming.parse_bool(
                raw_result.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        except streaming.BuildError as exc:
            raise SyncError("A native REVIEW result has invalid enabled state.") from exc
        if enabled and not is_coverage_fallback:
            raise SyncError("A native REVIEW candidate unexpectedly became enabled.")
        if not enabled and is_coverage_fallback:
            raise SyncError("A coverage fallback unexpectedly became disabled.")
        channel = channels_by_key.get(key)
        if channel is None:
            continue
        if (
            channel.get(
                "_native_epg_id_raw_present",
                bool(channel.get("epg_channel_id", "")),
            )
            and channel.get("_native_epg_id_exact", True) is not True
        ):
            summary["native_review_rejected_invalid_id"] += 1
            continue
        raw_current_id = str(channel.get("epg_channel_id", "") or "")
        current_id = streaming.clean_identifier(raw_current_id, 300)
        if not current_id:
            continue
        if current_id != raw_current_id:
            summary["native_review_rejected_invalid_id"] += 1
            continue
        raw_stored_id = str(before.get("epg_id", "") or "")
        stored_id = streaming.clean_identifier(raw_stored_id, 300)
        if stored_id != raw_stored_id:
            summary["native_review_rejected_invalid_id"] += 1
            continue
        source = streaming.clean_text(before.get("source", ""), 40).casefold()
        feed = streaming.clean_text(before.get("epg_feed", ""), 80).casefold()
        if stored_id and stored_id != current_id:
            summary["native_review_rejected_stored_conflict"] += 1
            continue
        # A populated historical ID remains operator-controlled unless it is
        # already a native-panel candidate which exactly agrees with the fresh
        # provider ID. Blank legacy/imported REVIEW rows need no generated-note
        # provenance: every fact used below is independently reacquired now.
        if stored_id and (
            source != "panel" or feed not in {"panel", "server xmltv.php"}
        ):
            summary["native_review_rejected_provenance"] += 1
            continue
        provider_name = streaming.clean_identifier(channel.get("name", ""), 300)
        if (
            not provider_name
            or exact_inventory_name_key(before.get("channel_name", ""))
            != exact_inventory_name_key(provider_name)
        ):
            summary["native_review_rejected_identity"] += 1
            continue
        candidate_row = {column: str(before.get(column, "")) for column in streaming.SHEET_COLUMNS}
        candidates[key[0]][key] = (candidate_row, current_id, provider_name)

    summary["native_review_candidates"] = sum(
        len(rows) for rows in candidates.values()
    )
    if not summary["native_review_candidates"]:
        return [], summary

    now_epoch = _parse_generated_epoch(generated_at)
    verified_updates: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="skytv-native-review-") as temporary:
        private_root = Path(temporary)
        for server_id in ("server_2", "server_3"):
            server_candidates = candidates[server_id]
            if not server_candidates:
                continue
            requested_ids = frozenset(
                epg_id for _row, epg_id, _name in server_candidates.values()
            )
            try:
                panel_path, _details = streaming.download_panel_xmltv(
                    server_id, private_root / f"{server_id}.xmltv"
                )
                validation = native_review.validate_native_xmltv(
                    panel_path,
                    server_id=server_id,
                    requested_ids=requested_ids,
                    now_epoch=now_epoch,
                )
            except (native_review.NativeReviewError, streaming.BuildError):
                summary["native_review_source_unavailable"] += 1
                continue

            names_by_id = getattr(validation, "display_names_by_id", None)
            if (
                int(getattr(validation, "requested_ids", -1))
                != len(requested_ids)
                or not set(validation.verified_ids).issubset(requested_ids)
                or not isinstance(names_by_id, Mapping)
                or not set(names_by_id).issubset(requested_ids)
            ):
                raise SyncError(
                    "Native EPG validation returned an inconsistent identity set."
                )
            compatible_ids_by_name: dict[str, set[str]] = {}
            for epg_id, display_names in names_by_id.items():
                for display_name in tuple(display_names or ()):
                    name_key = native_review.native_display_name_key(display_name)
                    if name_key:
                        compatible_ids_by_name.setdefault(name_key, set()).add(
                            str(epg_id)
                        )
            for key, (row, epg_id, provider_name) in server_candidates.items():
                if epg_id not in validation.verified_ids:
                    summary["native_review_rejected_schedule"] += 1
                    continue
                display_names = tuple(names_by_id.get(epg_id, ()))
                provider_key = native_review.native_display_name_key(provider_name)
                if (
                    not provider_key
                    or not native_review.native_names_compatible(
                        provider_name, display_names
                    )
                    or compatible_ids_by_name.get(provider_key) != {epg_id}
                ):
                    summary["native_review_rejected_identity"] += 1
                    continue
                marker = (
                    "native-review-v1; exact current provider EPG ID, XMLTV "
                    "display name, unchanged provider name, and current schedule verified"
                )
                prior_notes = streaming.clean_text(row.get("notes", ""), 2000)
                row.update(
                    {
                        "enabled": "TRUE",
                        "action": "KEEP_PANEL",
                        "source": "panel",
                        "epg_feed": "panel",
                        "epg_id": epg_id,
                        "reason": (
                            "Native panel EPG identity and current programme "
                            "schedule verified."
                        ),
                        "notes": (
                            prior_notes
                            if marker.casefold() in prior_notes.casefold()
                            else streaming.clean_text(
                                "; ".join(
                                    value for value in (prior_notes, marker) if value
                                ),
                                2000,
                            )
                        ),
                    }
                )
                if key[0] == "server_1":  # Defensive; candidates excludes it.
                    raise SyncError("Server 1 native panel EPG is forbidden.")
                verified_updates.append(row)

    verified_updates.sort(
        key=lambda row: (
            row["server_id"], streaming.stream_sort_key(row["stream_id"])
        )
    )
    summary["native_review_verified"] = len(verified_updates)
    return verified_updates, summary


def _ai_policy_semantics(value: Any) -> ai_policy.ProtectedSemantics:
    return ai_policy.ProtectedSemantics(
        direction=str(getattr(value, "direction", "") or ""),
        timeshift=str(getattr(value, "timeshift", "") or ""),
        has_plus=bool(getattr(value, "has_plus", False)),
        has_extra=bool(getattr(value, "has_extra", False)),
        has_alternate=bool(getattr(value, "has_alternate", False)),
        numbers=frozenset(getattr(value, "numbers", ()) or ()),
        languages=frozenset(getattr(value, "languages", ()) or ()),
        content=frozenset(getattr(value, "content", ()) or ()),
    )


def _ai_policy_row(
    shortlist: automatch.AiReviewShortlist,
    before: Mapping[str, str],
) -> ai_policy.ReviewRowEvidence:
    try:
        enabled = streaming.parse_bool(
            before.get("enabled", ""),
            default=False,
            field_name="mapping enabled",
        )
    except streaming.BuildError as exc:
        raise SyncError("An AI REVIEW row has invalid enabled state.") from exc
    return ai_policy.ReviewRowEvidence(
        server_id=str(shortlist.server_id),
        stream_id=str(shortlist.stream_id),
        channel_name=str(shortlist.channel_name),
        category_name=str(shortlist.category_name),
        normalized_name=str(shortlist.normalized_name),
        normalized_category=str(shortlist.normalized_category),
        strict_identity=str(shortlist.strict_identity),
        bag_identity=str(shortlist.bag_identity),
        market=str(shortlist.market),
        route_plan=tuple(shortlist.route_plan),
        route_explicit=bool(shortlist.route_explicit),
        semantics=_ai_policy_semantics(shortlist),
        smart_epg_id=str(shortlist.smart_epg_id),
        smart_match_method=str(shortlist.smart_match_method),
        name_ranker_id=ai_policy.NAME_RANKER_ID,
        contextual_ranker_id=ai_policy.CONTEXTUAL_RANKER_ID,
        source_sha256=str(shortlist.source_sha256),
        text_catalog_file_sha256=str(shortlist.text_catalog_file_sha256),
        text_catalog_fingerprint_sha256=str(
            shortlist.text_catalog_fingerprint_sha256
        ),
        text_catalog_generated_token=str(shortlist.text_catalog_generated_token),
        candidates=tuple(
            ai_policy.CandidateEvidence(
                epg_id=str(candidate.epg_id),
                display_name=str(candidate.display_name),
                market=str(candidate.region),
                feed=str(candidate.feed),
                name_score=float(candidate.local_score),
                contextual_score=float(candidate.contextual_score),
                semantics=_ai_policy_semantics(candidate),
            )
            for candidate in shortlist.candidates
        ),
        action=streaming.clean_text(before.get("action", ""), 40).upper(),
        enabled=enabled,
        has_open_alert=False,
        provider_identity_unchanged=True,
    )


def _ai_policy_terminal_rows(
    prepared: ai_policy.PreparedClusterReview,
    *,
    terminal_table: MappingTable,
    terminal_open_alert_keys: frozenset[tuple[str, str]],
    provider_verified_keys: frozenset[tuple[str, str]],
) -> tuple[ai_policy.TerminalRowState, ...]:
    """Bind policy decisions to an actual terminal Sheet/provider reread."""

    terminal_by_key = _mapping_rows_by_key(terminal_table)
    result: list[ai_policy.TerminalRowState] = []
    for proposal_row in prepared.cluster.rows:
        key = proposal_row.key
        if key not in provider_verified_keys:
            # A missing terminal row makes only this row remain REVIEW in the
            # pure policy. Never manufacture an "unchanged" provider state.
            continue
        terminal_entry = terminal_by_key.get(key)
        if terminal_entry is None:
            continue
        _row_number, current = terminal_entry
        try:
            enabled = streaming.parse_bool(
                current.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        except streaming.BuildError:
            continue
        result.append(
            ai_policy.TerminalRowState(
                server_id=key[0],
                stream_id=key[1],
                channel_name=str(current.get("channel_name", "")),
                category_name=str(current.get("category_name", "")),
                # These derived values are safe to retain only because the
                # provider verifier above proved that the raw channel/category
                # identity still equals the proposal-time inventory. The raw
                # terminal strings are also compared by the policy.
                normalized_name=proposal_row.normalized_name,
                normalized_category=proposal_row.normalized_category,
                strict_identity=proposal_row.strict_identity,
                bag_identity=proposal_row.bag_identity,
                market=proposal_row.market,
                route_plan=proposal_row.route_plan,
                route_explicit=proposal_row.route_explicit,
                semantics=proposal_row.semantics,
                action=streaming.clean_text(
                    current.get("action", ""), 40
                ).upper(),
                enabled=enabled,
                has_open_alert=key in terminal_open_alert_keys,
            )
        )
    return tuple(result)


def _ai_policy_verification(
    outcome: automatch.AutoMatchOutcome,
) -> ai_policy.LocalVerificationSnapshot:
    """Convert the parser's real same-descriptor evidence, never a shortlist."""

    evidence = outcome.verification_evidence
    return ai_policy.LocalVerificationSnapshot(
        source_sha256=str(evidence.source_sha256),
        text_catalog_file_sha256=str(evidence.text_catalog_file_sha256),
        text_catalog_fingerprint_sha256=str(
            evidence.text_catalog_fingerprint_sha256
        ),
        text_catalog_generated_token=str(evidence.text_catalog_generated_token),
        xml_catalog_ids=frozenset(evidence.xml_catalog_ids),
        text_catalog_ids=frozenset(evidence.text_catalog_ids),
        catalog_candidates=tuple(
            ai_policy.CatalogCandidateState(
                epg_id=str(candidate.epg_id),
                market=str(candidate.market),
                feed=str(candidate.feed),
                semantics=_ai_policy_semantics(candidate.semantics),
                is_real=candidate.is_real,
            )
            for candidate in evidence.catalog_candidates
        ),
        programme_gates=tuple(
            ai_policy.ProgrammeGateEvidence(
                channel_key=str(gate.channel_key),
                distinct_informative_programmes=(
                    gate.distinct_informative_programmes
                ),
                first_start_epoch=gate.first_start_epoch,
                latest_stop_epoch=gate.latest_stop_epoch,
                checked_at_epoch=gate.checked_at_epoch,
                source_sha256=str(gate.source_sha256),
                passed=gate.passed,
            )
            for gate in evidence.programme_gates
        ),
    )


def _gemini_review_updates(
    *,
    outcome: automatch.AutoMatchOutcome,
    authoritative_table: MappingTable,
    api_key: str,
    limit: int,
    sensitive_values: Sequence[str] = (),
    excluded_keys: Iterable[tuple[str, str]] = (),
    terminal_state_loader: (
        Callable[
            [Sequence[Mapping[str, str]]],
            tuple[
                MappingTable,
                frozenset[tuple[str, str]],
                frozenset[tuple[str, str]],
            ],
        ]
        | None
    ) = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Apply only Smart+Gemini HIGH agreements which pass every local gate."""

    excluded = frozenset(excluded_keys)
    available = tuple(
        shortlist
        for shortlist in (getattr(outcome, "ai_review_shortlists", ()) or ())
        if (str(shortlist.server_id), str(shortlist.stream_id)) not in excluded
    )
    summary: dict[str, Any] = {
        "ai_review_enabled": True,
        "ai_review_considered_rows": 0,
        "ai_review_considered_clusters": 0,
        "ai_review_deferred_clusters": 0,
        "ai_review_deferred_rows": 0,
        "ai_review_policy_blocked_clusters": 0,
        "ai_review_suggestion_rows": 0,
        "ai_review_high_suggestions_found": 0,
        "ai_review_high_suggestion_updates": 0,
        "ai_review_high_suggestions_persisted": 0,
        "ai_review_auto_enabled_rows": 0,
        "ai_review_abstained_rows": 0,
        "ai_review_error_rows": 0,
        "ai_review_batches_attempted": 0,
        "ai_review_batches_succeeded": 0,
        "ai_review_prompt_tokens": 0,
        "ai_review_candidate_tokens": 0,
        "ai_review_total_tokens": 0,
    }
    if not available:
        return [], summary

    authoritative_by_key = _mapping_rows_by_key(authoritative_table)
    shortlist_by_key: dict[tuple[str, str], automatch.AiReviewShortlist] = {}
    policy_rows: list[ai_policy.ReviewRowEvidence] = []
    for shortlist in available:
        key = (str(shortlist.server_id), str(shortlist.stream_id))
        if key in shortlist_by_key or key not in authoritative_by_key:
            summary["ai_review_error_rows"] += 1
            continue
        _row_number, before = authoritative_by_key[key]
        try:
            policy_row = _ai_policy_row(shortlist, before)
        except (SyncError, ai_policy.PolicyInputError, ValueError, TypeError):
            summary["ai_review_error_rows"] += 1
            continue
        shortlist_by_key[key] = shortlist
        policy_rows.append(policy_row)

    if not policy_rows:
        summary["ai_review_status"] = "failed_closed"
        return [], summary
    try:
        clusters = ai_policy.cluster_exact_compatible_rows(tuple(policy_rows))
        prepared = ai_policy.prepare_cluster_reviews(clusters)
        plan = ai_policy.partition_review_requests(
            prepared,
            run_limit=min(int(limit), MAX_AI_REVIEW_ROWS),
            batch_size=ai_policy.MAX_ROWS_PER_GEMINI_CALL,
            rotation=int(getattr(outcome, "ai_review_rotation", 0) or 0),
        )
    except (ai_policy.PolicyInputError, ValueError, TypeError):
        summary["ai_review_error_rows"] += len(policy_rows)
        summary["ai_review_status"] = "failed_closed"
        return [], summary

    summary["ai_review_policy_blocked_clusters"] = sum(
        1 for item in prepared if not item.eligible
    )

    # The user-facing limit controls AI clusters.  Keep an independent 200-row
    # mutation cap so clustering can save API calls without expanding the
    # Google write blast radius.
    selected: list[ai_policy.PreparedClusterReview] = []
    deferred = list(plan.deferred)
    selected_rows = 0
    for item in plan.selected:
        row_count = len(item.cluster.rows)
        if selected_rows + row_count > MAX_AI_REVIEW_ROWS:
            deferred.append(item)
            continue
        selected.append(item)
        selected_rows += row_count
    summary["ai_review_considered_clusters"] = len(selected)
    summary["ai_review_considered_rows"] = selected_rows
    summary["ai_review_deferred_clusters"] = len(deferred)
    summary["ai_review_deferred_rows"] = sum(
        len(item.cluster.rows) for item in deferred
    )
    if not selected:
        summary["ai_review_status"] = "no_safe_clusters"
        return [], summary

    batches = tuple(
        tuple(selected[index : index + ai_policy.MAX_ROWS_PER_GEMINI_CALL])
        for index in range(0, len(selected), ai_policy.MAX_ROWS_PER_GEMINI_CALL)
    )
    results_by_id: dict[str, gemini_review.ReviewResult] = {}
    for batch_index, batch in enumerate(batches):
        requests_to_review = tuple(
            item.request for item in batch if item.request is not None
        )
        try:
            reviewed = gemini_review.review_flagged_channels(
                requests_to_review,
                api_key=api_key,
                sensitive_values=sensitive_values,
            )
        except Exception:
            # AI is optional. Quota, timeout, or malformed output never blocks
            # deterministic decisions or the normal EPG build.
            remaining = batches[batch_index:]
            summary["ai_review_error_rows"] += sum(
                len(item.cluster.rows)
                for pending_batch in remaining
                for item in pending_batch
            )
            break
        summary["ai_review_batches_attempted"] += reviewed.batches_attempted
        summary["ai_review_batches_succeeded"] += reviewed.batches_succeeded
        summary["ai_review_prompt_tokens"] += reviewed.prompt_tokens
        summary["ai_review_candidate_tokens"] += reviewed.candidate_tokens
        summary["ai_review_total_tokens"] += reviewed.total_tokens
        for result in reviewed.results:
            if result.review_id in results_by_id:
                summary["ai_review_error_rows"] += 1
                continue
            results_by_id[result.review_id] = result

    result_items = tuple(
        item
        for item in selected
        if item.cluster.cluster_id in results_by_id
    )
    terminal_input_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for item in result_items:
        for policy_row in item.cluster.rows:
            base_entry = authoritative_by_key.get(policy_row.key)
            if base_entry is not None:
                terminal_input_by_key[policy_row.key] = dict(base_entry[1])
    if result_items and terminal_state_loader is None:
        summary["ai_review_error_rows"] += sum(
            len(item.cluster.rows) for item in result_items
        )
        summary["ai_review_status"] = "failed_closed"
        return [], summary
    try:
        if result_items:
            (
                terminal_table,
                terminal_open_alert_keys,
                provider_verified_keys,
            ) = terminal_state_loader(  # type: ignore[misc]
                tuple(terminal_input_by_key.values())
            )
        else:
            terminal_table = authoritative_table
            terminal_open_alert_keys = frozenset()
            provider_verified_keys = frozenset()
        verification = _ai_policy_verification(outcome)
        decision_epoch = int(time.time())
    except Exception:
        summary["ai_review_error_rows"] += sum(
            len(item.cluster.rows) for item in result_items
        )
        summary["ai_review_status"] = "failed_closed"
        return [], summary

    updates: list[dict[str, str]] = []
    updated_keys: set[tuple[str, str]] = set()
    for item in selected:
        result = results_by_id.get(item.cluster.cluster_id)
        if result is None:
            # A failed later API batch is already counted above.
            continue
        is_high = (
            result.decision is gemini_review.ReviewDecision.SUGGEST
            and result.confidence is gemini_review.ReviewConfidence.HIGH
            and bool(result.candidate_key)
        )
        if is_high:
            summary["ai_review_high_suggestions_found"] += len(
                item.cluster.rows
            )
            summary["ai_review_suggestion_rows"] += len(item.cluster.rows)
        try:
            decisions = ai_policy.validate_high_agreement(
                item,
                result,
                verification=verification,
                terminal_rows=_ai_policy_terminal_rows(
                    item,
                    terminal_table=terminal_table,
                    terminal_open_alert_keys=terminal_open_alert_keys,
                    provider_verified_keys=provider_verified_keys,
                ),
                decision_epoch=decision_epoch,
            )
        except (SyncError, ai_policy.PolicyInputError, ValueError, TypeError):
            summary["ai_review_error_rows"] += len(item.cluster.rows)
            continue
        for decision in decisions:
            key = (decision.server_id, decision.stream_id)
            if not decision.approved or decision.provenance is None:
                summary["ai_review_abstained_rows"] += 1
                continue
            base_entry = authoritative_by_key.get(key)
            if base_entry is None or key in updated_keys:
                summary["ai_review_error_rows"] += 1
                continue
            _row_number, before = base_entry
            row = dict(before)
            row.update(
                {
                    "enabled": "TRUE",
                    "action": "AUTO_EPGSHARE",
                    "source": "epgshare01",
                    "epg_feed": "ALL_SOURCES1",
                    "epg_id": decision.epg_id,
                    "reason": (
                        "Smart Rules and Gemini HIGH independently selected "
                        "the same locally verified EPGShare schedule."
                    ),
                }
            )
            marker = automatch._ai_verified_v2_provenance_note(
                row,
                match_method=decision.provenance.match_method,
                market=decision.provenance.market,
                source_sha256=decision.provenance.source_sha256,
                text_catalog_file_sha256=(
                    decision.provenance.text_catalog_file_sha256
                ),
                text_catalog_fingerprint_sha256=(
                    decision.provenance.text_catalog_fingerprint_sha256
                ),
                text_catalog_generated_token=(
                    decision.provenance.text_catalog_generated_token
                ),
            )
            prior_notes = streaming.clean_text(row.get("notes", ""), 2000)
            row["notes"] = streaming.clean_text(
                " | ".join(value for value in (marker, prior_notes) if value),
                2000,
            )
            _validate_review_update(
                before,
                row,
                verified_ai_updates={key: row},
            )
            updates.append(row)
            updated_keys.add(key)
            summary["ai_review_high_suggestion_updates"] += 1
            summary["ai_review_auto_enabled_rows"] += 1
    summary["ai_review_status"] = (
        "completed" if not summary["ai_review_error_rows"] else "partial_failed_closed"
    )
    return updates, summary


def _run_sync_review_mode(
    *,
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    output_dir: Path,
    generated_at: str,
    snapshot_out: Path,
    authoritative_snapshot_out: Path | None,
    snapshot_manifest_out: Path | None,
    write_to_sheet: bool,
    google_session: Any,
    sheet_id: str,
    sheet_tab: str,
    alerts_tab: str,
    provider_failures: Mapping[str, str] | None,
    server_configs: Sequence[ServerConfig],
    all_source_file: Path | None,
    all_source_catalog_file: Path | None,
    epgshare_spool_out: Path | None,
    review_recheck_mode: str,
    review_recheck_servers: Sequence[str],
    coverage_fallback_limit: int,
    use_gemini_ai: bool,
    gemini_api_key: str,
    ai_review_limit: int,
    validate_native_review: bool,
    native_hint_summary: Mapping[str, int] | None,
    allow_insecure_http: bool,
) -> dict[str, Any]:
    """Run the opt-in REVIEW backlog path without changing the legacy default."""

    mode = streaming.clean_text(review_recheck_mode, 20).casefold()
    if mode not in {"dry-run", "apply"}:
        raise SyncError("The REVIEW recheck mode must be off, dry-run, or apply.")
    selected_servers = tuple(
        dict.fromkeys(
            streaming.normalize_server_id(value)
            for value in (review_recheck_servers or ("server_1",))
        )
    )
    if not selected_servers or not set(selected_servers).issubset(DEFAULT_SERVERS):
        raise SyncError("The REVIEW recheck server selection is invalid.")
    if not 1 <= int(ai_review_limit) <= MAX_AI_REVIEW_ROWS:
        raise SyncError(
            f"The Gemini review limit must be between 1 and {MAX_AI_REVIEW_ROWS}."
        )
    mapping_write_requested = bool(write_to_sheet or mode == "apply")
    if mapping_write_requested and google_session is None:
        raise SyncError("A Google authorized session is required for Sheet writes.")
    if use_gemini_ai and not str(gemini_api_key or "").strip():
        raise SyncError(
            "Gemini review was selected, but the GEMINI_API_KEY secret is missing."
        )

    effective_snapshot_path = Path(snapshot_out)
    authoritative_snapshot_path = (
        Path(authoritative_snapshot_out)
        if authoritative_snapshot_out is not None
        else effective_snapshot_path.with_name("authoritative_mapping.csv")
    )
    snapshot_manifest_path = (
        Path(snapshot_manifest_out)
        if snapshot_manifest_out is not None
        else effective_snapshot_path.with_name("mapping_snapshot_manifest.json")
    )

    configs_by_server = {config.server_id: config for config in server_configs}
    for provider_inventory in inventories:
        config = configs_by_server.get(provider_inventory.server_id)
        if config is not None:
            validate_provider_inventory_secret_safe(
                provider_inventory.categories,
                provider_inventory.channels,
                provider_reflection_needles(config, [config.base_url]),
            )

    authoritative_table = table
    existing_alerts: list[dict[str, str]] = []
    if google_session is not None:
        if mapping_write_requested:
            authoritative_table = parse_table_values(
                google_sheet_values(
                    google_session, validate_sheet_id(sheet_id), sheet_tab
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
        existing_alerts = parse_sync_alert_values(
            google_sync_alert_values(
                google_session, validate_sheet_id(sheet_id), alerts_tab
            )
        )

    discovered_rows, changed_rows, missing_rows = compare_inventory(
        authoritative_table, inventories, discovered_at=generated_at
    )
    overlap_issues = inventory_overlap_issues(authoritative_table, inventories)
    pending_alerts = pending_sync_alert_rows(
        changed_rows, existing_alerts, detected_at=generated_at
    )
    persistent_quarantine_keys = open_alert_quarantine_keys(existing_alerts)
    pending_quarantine_keys = {_row_identity(row) for row in pending_alerts}
    # This exact set is part of the matcher input. Any added or removed OPEN
    # quarantine before a write invalidates not only target rows but also the
    # human evidence from which cross-server aliases may have been learned.
    match_time_quarantine_keys = persistent_quarantine_keys.union(
        pending_quarantine_keys
    )
    review_recheck_rows, recheck_selection_summary = select_review_recheck_rows(
        authoritative_table,
        inventories,
        selected_servers=selected_servers,
        quarantined_keys=match_time_quarantine_keys,
        changed_rows=changed_rows,
    )

    auto_match_inputs = (
        bool(all_source_file),
        bool(all_source_catalog_file),
        bool(epgshare_spool_out),
    )
    if not all(auto_match_inputs):
        raise SyncError(
            "REVIEW rechecking requires the ALL_SOURCES1 XML file, official "
            "text catalog, and spool output together."
        )

    new_rows = list(discovered_rows)
    review_results: list[dict[str, str]] = []
    auto_match_summary: dict[str, Any] = {
        "auto_match_considered_rows": 0,
        "auto_match_provisional_rows": 0,
        "auto_matched_rows": 0,
        "auto_match_review_rows": len(new_rows),
        "review_recheck_considered_rows": 0,
        "review_recheck_safe_matches": 0,
        "review_recheck_still_review_rows": len(review_recheck_rows),
        "auto_match_rejected_programme_gates": 0,
        "coverage_fallback_enabled": coverage_fallback_limit > 0,
        "coverage_fallback_limit": coverage_fallback_limit,
        "coverage_fallback_candidate_rows": 0,
        "coverage_fallback_applied_rows": 0,
        "coverage_fallback_deferred_rows": 0,
        "coverage_fallback_ai_deferred_rows": 0,
        "new_channel_coverage_fallback_rows": 0,
        "review_recheck_coverage_fallback_rows": 0,
        "coverage_fallback_machine_prefilled_candidate_rows": 0,
        "coverage_fallback_machine_prefilled_applied_rows": 0,
        "coverage_fallback_legacy_server1_candidate_rows": 0,
        "coverage_fallback_legacy_server1_applied_rows": 0,
        "coverage_fallback_protected_manual_rows": 0,
        "coverage_fallback_protected_native_rows": 0,
        "coverage_fallback_replaced_by_native_rows": 0,
    }
    outcome: automatch.AutoMatchOutcome | None = None
    learned_alias_support_rows: tuple[dict[str, str], ...] = ()
    if not overlap_issues:
        try:
            outcome = automatch.auto_match_and_spool(
                mapping_rows=authoritative_table.rows,
                inventories=inventories,
                new_rows=discovered_rows,
                review_rows=review_recheck_rows,
                quarantined_keys=match_time_quarantine_keys,
                all_source_file=Path(all_source_file),
                all_source_catalog_file=Path(all_source_catalog_file),
                spool_out=Path(epgshare_spool_out),
                generated_at=generated_at,
                enable_ai_review=bool(use_gemini_ai),
                coverage_fallback_limit=coverage_fallback_limit,
            )
        except automatch.AutoMatchError as exc:
            raise SyncError(str(exc)) from exc
        output_by_key = {_row_identity(row): dict(row) for row in outcome.rows}
        new_keys = {_row_identity(row) for row in discovered_rows}
        review_keys = {_row_identity(row) for row in review_recheck_rows}
        if set(output_by_key) != new_keys.union(review_keys):
            raise SyncError("Smart Rules did not return the exact requested identities.")
        new_rows = [output_by_key[_row_identity(row)] for row in discovered_rows]
        review_results = [
            output_by_key[_row_identity(row)] for row in review_recheck_rows
        ]
        learned_alias_support_rows = _learned_alias_support_rows_from_outcome(
            outcome
        )
        auto_match_summary = outcome.summary_fields()

    safe_review_matches = [
        row
        for row in review_results
        if streaming.clean_text(row.get("action", ""), 40).upper()
        in {"AUTO_EPGSHARE", "AUTO_DUMMY", "IGNORE"}
        and (
            streaming.clean_text(row.get("action", ""), 40).upper()
            == "IGNORE"
            or streaming.parse_bool(
                row.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        )
    ]
    safe_review_matches.sort(
        key=lambda row: (
            row["server_id"],
            streaming.stream_sort_key(row["stream_id"]),
        )
    )
    native_verified_updates: list[dict[str, str]] = []
    native_summary = _native_review_summary_defaults()
    if validate_native_review and set(selected_servers).intersection(
        {"server_2", "server_3"}
    ):
        native_verified_updates, native_summary = (
            _build_verified_native_review_updates(
                review_input_rows=review_recheck_rows,
                review_result_rows=review_results,
                inventories=inventories,
                generated_at=generated_at,
            )
        )

    native_verified_keys = {
        _row_identity(row) for row in native_verified_updates
    }
    fallback_replaced_by_native = sum(
        1
        for row in safe_review_matches
        if _row_identity(row) in native_verified_keys
        and streaming.clean_text(row.get("notes", ""), 2_000).startswith(
            "coverage-fallback-v1 "
        )
    )
    if native_verified_keys:
        # Native validation runs after the local coverage proposal. A verified
        # current Server 2/3 native schedule wins, and the synthetic proposal
        # for that identity must not enter the deterministic writer as a
        # duplicate update.
        safe_review_matches = [
            row
            for row in safe_review_matches
            if _row_identity(row) not in native_verified_keys
        ]
    auto_match_summary["coverage_fallback_replaced_by_native_rows"] = (
        fallback_replaced_by_native
    )

    (
        deterministic_epgshare_updates,
        deterministic_native_updates,
    ) = _bounded_deterministic_review_updates(
        safe_review_matches,
        native_verified_updates,
    )
    deterministic_updates = [
        *deterministic_epgshare_updates,
        *deterministic_native_updates,
    ]
    deferred_safe_matches = max(
        0,
        len(safe_review_matches)
        + len(native_verified_updates)
        - len(deterministic_updates),
    )
    native_summary["native_review_deferred"] = max(
        0, len(native_verified_updates) - len(deterministic_native_updates)
    )
    # This count means locally verified proposals. Persistence remains zero in
    # dry-run and is filled only after Google's authoritative post-write read.
    auto_match_summary["review_recheck_safe_matches"] = int(
        auto_match_summary.get("review_recheck_safe_matches", 0)
    ) - fallback_replaced_by_native + int(
        auto_match_summary.get("review_recheck_headings_ignored", 0)
    ) + len(native_verified_updates)
    auto_match_summary["review_recheck_still_review_rows"] = max(
        0,
        int(auto_match_summary.get("review_recheck_still_review_rows", 0))
        - (len(native_verified_updates) - fallback_replaced_by_native),
    )

    ai_updates: list[dict[str, str]] = []
    ai_summary: dict[str, Any] = {
        "ai_review_enabled": bool(use_gemini_ai),
        "ai_review_considered_rows": 0,
        "ai_review_suggestion_rows": 0,
        "ai_review_high_suggestions_found": 0,
        "ai_review_high_suggestion_updates": 0,
        "ai_review_high_suggestions_persisted": 0,
        "ai_review_abstained_rows": 0,
        "ai_review_error_rows": 0,
        "ai_review_batches_attempted": 0,
        "ai_review_batches_succeeded": 0,
    }
    ai_terminal_stats: dict[str, Any] = {}

    def load_ai_terminal_state(
        candidate_rows: Sequence[Mapping[str, str]],
    ) -> tuple[
        MappingTable,
        frozenset[tuple[str, str]],
        frozenset[tuple[str, str]],
    ]:
        """Reacquire Sheet, alert, and provider authority after AI returns."""

        if google_session is None:
            if mode != "dry-run":
                raise SyncError(
                    "AI-assisted approval requires an authoritative terminal "
                    "Sheet read."
                )
            # A library/test preview performs no mutation. Its already-fetched
            # provider inventory and supplied table may be used to report what
            # would be eligible, but apply mode can never use this fallback.
            latest_table = authoritative_table
            latest_open_keys = frozenset(match_time_quarantine_keys)
            ai_terminal_stats["ai_review_terminal_mode"] = "preview_snapshot"
            return (
                latest_table,
                latest_open_keys,
                frozenset(_row_identity(row) for row in candidate_rows),
            )
        latest_table = parse_table_values(
            google_sheet_values(
                google_session, validate_sheet_id(sheet_id), sheet_tab
            ),
            maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
        )
        latest_alerts = parse_sync_alert_values(
            google_sync_alert_values(
                google_session, validate_sheet_id(sheet_id), alerts_tab
            )
        )
        latest_open_keys = open_alert_quarantine_keys(latest_alerts).union(
            pending_quarantine_keys
        )
        verified_rows, provider_stats = revalidate_provider_identity_updates(
            candidate_rows,
            inventories,
            server_configs,
            allow_insecure_http=allow_insecure_http,
        )
        ai_terminal_stats["ai_terminal_provider_checked"] = int(
            provider_stats.get("provider_batch_revalidation_checked", 0)
        )
        ai_terminal_stats["ai_terminal_provider_rejected"] = int(
            provider_stats.get("provider_batch_revalidation_rejected", 0)
        )
        ai_terminal_stats["ai_terminal_provider_unavailable"] = int(
            provider_stats.get("provider_batch_revalidation_unavailable", 0)
        )
        ai_terminal_stats["ai_review_terminal_mode"] = "authoritative_reread"
        return (
            latest_table,
            frozenset(latest_open_keys),
            frozenset(_row_identity(row) for row in verified_rows),
        )

    if use_gemini_ai and outcome is not None:
        provider_sensitive_values = gemini_provider_sensitive_values(
            server_configs
        )
        ai_updates, ai_summary = _gemini_review_updates(
            outcome=outcome,
            authoritative_table=authoritative_table,
            api_key=gemini_api_key,
            limit=int(ai_review_limit),
            sensitive_values=provider_sensitive_values,
            excluded_keys=(_row_identity(row) for row in native_verified_updates),
            terminal_state_loader=load_ai_terminal_state,
        )
        ai_summary.update(ai_terminal_stats)
        auto_match_summary["review_recheck_safe_matches"] = int(
            auto_match_summary.get("review_recheck_safe_matches", 0)
        ) + len(ai_updates)
        auto_match_summary["review_recheck_still_review_rows"] = max(
            0,
            int(auto_match_summary.get("review_recheck_still_review_rows", 0))
            - len(ai_updates),
        )

    review_updates = [*deterministic_updates, *ai_updates]
    projected_sheet_bytes = validate_projected_sheet_size(
        authoritative_table, new_rows
    )
    possible_reuse_count = sum(
        1
        for row in changed_rows
        if str(row.get("risk", "")).startswith("POSSIBLE_")
    )
    projected_alert_bytes = validate_projected_alert_size(
        existing_alerts, pending_alerts
    )
    existing_open_alert_count = sum(
        1
        for row in existing_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    )
    summary: dict[str, Any] = {
        "version": SYNC_VERSION,
        "generated_at": generated_at,
        "sheet_mode": "write" if mapping_write_requested else "dry-run",
        "review_recheck_mode": mode,
        "review_recheck_servers": list(selected_servers),
        "inventory_rows": sum(len(item.channels) for item in inventories),
        "new_rows": len(new_rows),
        "appended_rows": 0,
        "changed_rows": len(changed_rows),
        "missing_rows": len(missing_rows),
        "possible_id_reuse_rows": possible_reuse_count,
        "new_sync_alerts": len(pending_alerts),
        "sync_alerts_appended": 0,
        "open_sync_alerts": existing_open_alert_count,
        "projected_sync_alert_rows": len(existing_alerts) + len(pending_alerts),
        "projected_sync_alert_bytes": projected_alert_bytes,
        "projected_sheet_rows": len(authoritative_table.rows) + len(new_rows),
        "projected_snapshot_bytes": projected_sheet_bytes,
        "safe_to_append": not overlap_issues,
        "overlap_issues": overlap_issues,
        "provider_failures": dict(provider_failures or {}),
        "review_recheck_deferred_rows": deferred_safe_matches,
        "review_recheck_safe_matches_persisted": 0,
        "review_recheck_rows_updated": 0,
        "servers": {
            item.server_id: {
                "source": item.source,
                "channel_rows": len(item.channels),
                "category_rows": len(item.categories),
            }
            for item in inventories
        },
        **recheck_selection_summary,
        **auto_match_summary,
        **ai_summary,
        **dict(native_hint_summary or {}),
        **native_summary,
    }
    status_table = authoritative_table
    status_quarantine_keys = match_time_quarantine_keys
    native_results_by_key = {
        _row_identity(row): row for row in native_verified_updates
    }
    reported_review_results = [
        native_results_by_key.get(_row_identity(row), row)
        for row in review_results
    ]

    def persist_reports() -> None:
        add_channel_status_to_summary(
            summary,
            status_table,
            inventories,
            quarantined_keys=status_quarantine_keys,
        )
        write_reports(
            output_dir,
            inventories=inventories,
            new_rows=new_rows,
            changed_rows=changed_rows,
            missing_rows=missing_rows,
            review_recheck_rows=review_recheck_rows,
            review_recheck_results=reported_review_results,
            ai_review_results=ai_updates,
            summary=summary,
        )

    persist_reports()
    write_mapping_snapshot(
        output_dir / "mapping_before_append.csv", authoritative_table
    )
    if overlap_issues:
        raise SyncError(" ".join(overlap_issues))

    if not mapping_write_requested:
        snapshot_validation = write_private_mapping_snapshot_bundle(
            effective_path=effective_snapshot_path,
            authoritative_path=authoritative_snapshot_path,
            manifest_path=snapshot_manifest_path,
            table=authoritative_table,
            changed_rows=changed_rows,
            persistent_quarantine_keys=persistent_quarantine_keys,
        )
        add_snapshot_validation_to_summary(summary, snapshot_validation)
        persist_reports()
        return summary

    alert_write_error: SheetWriteError | None = None
    # Close the concurrent-run duplicate window. Another run may have opened
    # the same stable incident after proposal generation but before this
    # append. Re-read, keep that row authoritative, and append only incidents
    # which are still absent. Any unrelated quarantine change still fails at
    # the exact-set check below before a Mapping write or snapshot.
    latest_alerts_before_append = parse_sync_alert_values(
        google_sync_alert_values(
            google_session, validate_sheet_id(sheet_id), alerts_tab
        )
    )
    latest_open_incidents = {
        sync_alert_dedup_key(row)
        for row in latest_alerts_before_append
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    }
    pending_alerts = [
        row
        for row in pending_alerts
        if sync_alert_dedup_key(row) not in latest_open_incidents
    ]
    existing_alerts = latest_alerts_before_append
    try:
        summary["sync_alerts_appended"] = append_sync_alert_rows(
            google_session,
            validate_sheet_id(sheet_id),
            alerts_tab,
            existing_alerts,
            pending_alerts,
        )
    except SheetWriteError as exc:
        summary["sync_alerts_appended"] = exc.appended_count
        summary["sync_alert_write_error"] = str(exc)
        alert_write_error = exc
    # Always reload Sync Alerts after the append attempt, even if this run
    # found no new alert. Another editor/run may have opened an alert while the
    # REVIEW proposals were being prepared.
    try:
        verified_alerts = parse_sync_alert_values(
            google_sync_alert_values(
                google_session, validate_sheet_id(sheet_id), alerts_tab
            )
        )
        if pending_alerts:
            verify_pending_alerts_are_open(pending_alerts, verified_alerts)
    except SyncError:
        summary["sync_alert_verification_error"] = (
            "New alerts were not confirmed by an authoritative re-read; "
            "Mappings and the build snapshot were not changed."
        )
        persist_reports()
        raise
    persistent_quarantine_keys = open_alert_quarantine_keys(verified_alerts)
    if persistent_quarantine_keys != match_time_quarantine_keys:
        summary["sync_alert_verification_error"] = (
            "The OPEN Sync Alerts quarantine set changed after matching; "
            "Mappings and the build snapshot were not changed."
        )
        persist_reports()
        raise SyncError(summary["sync_alert_verification_error"])
    existing_alerts = verified_alerts
    summary["open_sync_alerts"] = sum(
        1
        for row in verified_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    )
    if alert_write_error is not None and pending_alerts:
        summary["sync_alert_write_recovered_by_reread"] = True
    elif alert_write_error is not None:
        persist_reports()
        raise alert_write_error

    proposed_review_keys = {_row_identity(row) for row in review_updates}

    def refresh_alert_safety_before_mapping_write() -> None:
        """Re-read alerts at the last safe point before a Mappings mutation."""

        nonlocal existing_alerts, persistent_quarantine_keys
        try:
            latest_alerts = parse_sync_alert_values(
                google_sync_alert_values(
                    google_session, validate_sheet_id(sheet_id), alerts_tab
                )
            )
            # Alerts created by this exact run are a write precondition, not a
            # best-effort report. Missing/RESOLVED rows fail closed.
            if pending_alerts:
                verify_pending_alerts_are_open(pending_alerts, latest_alerts)
        except SyncError:
            summary["sync_alert_verification_error"] = (
                "Sync Alerts changed or could not be verified immediately "
                "before a Mappings write; no further Mappings write was sent."
            )
            persist_reports()
            raise
        latest_quarantine = open_alert_quarantine_keys(latest_alerts)
        if latest_quarantine != match_time_quarantine_keys:
            newly_blocked = latest_quarantine.difference(match_time_quarantine_keys)
            removed_blocks = match_time_quarantine_keys.difference(latest_quarantine)
            summary["review_recheck_quarantined_before_write"] = len(
                proposed_review_keys.intersection(latest_quarantine)
            )
            summary["sync_alert_quarantine_keys_added"] = len(newly_blocked)
            summary["sync_alert_quarantine_keys_removed"] = len(removed_blocks)
            persist_reports()
            raise SyncError(
                "The OPEN Sync Alerts quarantine set changed after matching; "
                "no Mappings write was sent."
            )
        existing_alerts = latest_alerts
        persistent_quarantine_keys = latest_quarantine
        summary["open_sync_alerts"] = sum(
            1
            for row in latest_alerts
            if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
        )

    proposal_base_table = authoritative_table
    final_table = authoritative_table
    intentional_mapping_write = False
    if write_to_sheet:
        if new_rows:
            new_rows_revalidated, new_row_revalidation_summary = (
                revalidate_auto_enabled_new_rows(
                    new_rows,
                    inventories,
                    server_configs,
                    allow_insecure_http=allow_insecure_http,
                    learned_alias_support_rows=learned_alias_support_rows,
                )
            )
            summary.update(new_row_revalidation_summary)
            if not new_rows_revalidated:
                summary["new_auto_provider_revalidation_error"] = (
                    "A newly auto-enabled provider identity or learned-alias "
                    "teaching identity changed or became unavailable before "
                    "append; no new Mapping row was sent."
                )
                persist_reports()
                raise SyncError(summary["new_auto_provider_revalidation_error"])
            try:
                pre_append_table = parse_table_values(
                    google_sheet_values(
                        google_session, validate_sheet_id(sheet_id), sheet_tab
                    ),
                    maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
                )
            except SyncError:
                summary["new_row_pre_append_mapping_error"] = (
                    "Mappings could not be authoritatively re-read immediately "
                    "before the new-channel append; no new Mapping row was sent."
                )
                persist_reports()
                raise
            if mapping_table_fingerprint(
                pre_append_table
            ) != mapping_table_fingerprint(authoritative_table):
                summary["new_row_pre_append_mapping_error"] = (
                    "The Mappings tab changed after new-channel proposals were "
                    "prepared; no new Mapping row was sent."
                )
                persist_reports()
                raise SyncError(summary["new_row_pre_append_mapping_error"])
            # Mappings is the primary proposal authority. Only after proving
            # that its complete table is unchanged do we reacquire the
            # OPEN-alert authority at the final safe point before the append.
            refresh_alert_safety_before_mapping_write()
            try:
                summary["appended_rows"] = append_google_sheet_rows(
                    google_session,
                    validate_sheet_id(sheet_id),
                    sheet_tab,
                    pre_append_table,
                    new_rows,
                )
            except SheetWriteError as exc:
                summary["appended_rows"] = exc.appended_count
                summary["write_error"] = str(exc)
                persist_reports()
                raise
            intentional_mapping_write = bool(summary["appended_rows"])
            final_table = parse_table_values(
                google_sheet_values(
                    google_session, validate_sheet_id(sheet_id), sheet_tab
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
            status_table = final_table
            if int(summary["appended_rows"]) != len(new_rows):
                raise SyncError(
                    "Google Sheets did not report the complete new-channel append; "
                    "the build snapshot was blocked."
                )
            verify_mapping_append_transition(
                pre_append_table, new_rows, final_table
            )

    if mode == "apply" and deterministic_native_updates:
        attempted_native_keys = {
            _row_identity(row) for row in deterministic_native_updates
        }
        revalidated_native_updates, revalidation_summary = (
            revalidate_native_review_updates(
                deterministic_native_updates,
                inventories,
                server_configs,
                allow_insecure_http=allow_insecure_http,
            )
        )
        summary.update(revalidation_summary)
        deterministic_native_updates = revalidated_native_updates
        deterministic_updates = [
            *deterministic_epgshare_updates,
            *deterministic_native_updates,
        ]
        review_updates = [*deterministic_updates, *ai_updates]
        proposed_review_keys = {_row_identity(row) for row in review_updates}
        revalidated_native_keys = {
            _row_identity(row) for row in deterministic_native_updates
        }
        rejected_native_keys = attempted_native_keys.difference(
            revalidated_native_keys
        )
        native_results_by_key = {
            key: row
            for key, row in native_results_by_key.items()
            if key not in rejected_native_keys
        }
        reported_review_results = [
            native_results_by_key.get(_row_identity(row), row)
            for row in review_results
        ]
        # Persist the aggregate revalidation outcome before the Sheet writer;
        # no provider response values enter reports.
        persist_reports()

    summary.setdefault("provider_batch_revalidation_checked", 0)
    summary.setdefault("provider_batch_revalidation_rejected", 0)
    summary.setdefault("provider_batch_revalidation_unavailable", 0)
    summary.setdefault("native_batch_revalidation_checked", 0)
    summary.setdefault("native_batch_revalidation_rejected", 0)
    summary.setdefault("native_batch_revalidation_unavailable", 0)

    def revalidate_review_batch(
        batch_rows: Sequence[Mapping[str, str]],
    ) -> None:
        """Require unchanged target and learned-rule evidence before one batch."""

        provider_dependencies = _deduplicate_provider_revalidation_rows(
            batch_rows,
            learned_alias_support_rows,
        )
        verified_rows, provider_stats = revalidate_provider_identity_updates(
            provider_dependencies,
            inventories,
            server_configs,
            allow_insecure_http=allow_insecure_http,
        )
        for field, value in provider_stats.items():
            summary[field] = int(summary.get(field, 0)) + int(value)
        wanted_by_key = {
            _row_identity(row): dict(row) for row in provider_dependencies
        }
        verified_by_key = {_row_identity(row): dict(row) for row in verified_rows}
        if set(verified_by_key) != set(wanted_by_key) or any(
            verified_by_key[key] != wanted
            for key, wanted in wanted_by_key.items()
        ):
            raise SyncError(
                "A provider identity changed or became unavailable immediately "
                "before a REVIEW update batch; that batch was not sent."
            )

        native_rows = [
            row
            for row in batch_rows
            if streaming.clean_text(row.get("action", ""), 40).upper()
            == "KEEP_PANEL"
        ]
        if not native_rows:
            return
        verified_native, native_stats = revalidate_native_review_updates(
            native_rows,
            inventories,
            server_configs,
            allow_insecure_http=allow_insecure_http,
        )
        summary["native_batch_revalidation_checked"] += int(
            native_stats.get("native_review_revalidation_checked", 0)
        )
        summary["native_batch_revalidation_rejected"] += int(
            native_stats.get("native_review_revalidation_rejected", 0)
        )
        summary["native_batch_revalidation_unavailable"] += int(
            native_stats.get("native_review_revalidation_unavailable", 0)
        )
        if {_row_identity(row) for row in verified_native} != {
            _row_identity(row) for row in native_rows
        }:
            raise SyncError(
                "Native provider evidence changed or became unavailable "
                "immediately before its REVIEW update batch; that batch was "
                "not sent."
            )

    if mode == "apply" and review_updates:
        refresh_alert_safety_before_mapping_write()
        try:
            writer_kwargs: dict[str, Any] = {
                "pre_write_check": refresh_alert_safety_before_mapping_write,
                "batch_pre_write_check": revalidate_review_batch,
            }
            if deterministic_native_updates:
                writer_kwargs["verified_native_rows"] = (
                    deterministic_native_updates
                )
            if ai_updates:
                writer_kwargs["verified_ai_rows"] = ai_updates
            updated_count, final_table = update_google_sheet_review_rows(
                google_session,
                validate_sheet_id(sheet_id),
                sheet_tab,
                final_table,
                review_updates,
                **writer_kwargs,
            )
        except SheetWriteError as exc:
            committed_rows = review_updates[: max(0, exc.appended_count)]
            committed_ai = sum(
                1
                for row in committed_rows
                if "ai-verified-v2" in streaming.clean_text(
                    row.get("notes", ""), 2000
                ).casefold()
            )
            committed_deterministic = len(committed_rows) - committed_ai
            committed_native = sum(
                1
                for row in committed_rows
                if streaming.clean_text(row.get("action", ""), 40).upper()
                == "KEEP_PANEL"
            )
            summary["review_recheck_rows_updated"] = len(committed_rows)
            summary["review_recheck_safe_matches_persisted"] = (
                len(committed_rows)
            )
            summary["native_review_persisted"] = committed_native
            summary["ai_review_high_suggestions_persisted"] = committed_ai
            summary["review_recheck_deferred_rows"] = int(
                summary.get("review_recheck_deferred_rows", 0)
            ) + (
                len(deterministic_updates) - committed_deterministic
            ) + (len(ai_updates) - committed_ai)
            summary["native_review_deferred"] = int(
                summary.get("native_review_deferred", 0)
            ) + len(deterministic_native_updates) - committed_native
            summary["review_recheck_write_error"] = str(exc)
            persist_reports()
            raise
        summary["review_recheck_rows_updated"] = updated_count
        summary["review_recheck_safe_matches_persisted"] = len(
            deterministic_updates
        ) + len(ai_updates)
        summary["native_review_persisted"] = len(
            deterministic_native_updates
        )
        intentional_mapping_write = intentional_mapping_write or bool(updated_count)
        summary["ai_review_high_suggestions_persisted"] = int(
            len(ai_updates)
        )

    # Every apply run ends with a fresh Mappings read, even when there were no
    # proposals. The terminal authoritative table—not a proposal-time copy—is
    # the only table allowed into the snapshot bundle.
    if mode == "apply":
        terminal_table = parse_table_values(
            google_sheet_values(
                google_session, validate_sheet_id(sheet_id), sheet_tab
            ),
            maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
        )
        expected_terminal_table = (
            final_table if intentional_mapping_write else proposal_base_table
        )
        if mapping_table_fingerprint(terminal_table) != mapping_table_fingerprint(
            expected_terminal_table
        ):
            summary["terminal_mapping_verification_error"] = (
                "Terminal Mappings verification found a change after the last "
                "confirmed operation; the build snapshot was blocked."
            )
            persist_reports()
            raise SyncError(summary["terminal_mapping_verification_error"])
        final_table = terminal_table
        status_table = terminal_table

    # Alerts are an equal authority to Mappings for snapshot eligibility. A
    # final read after the terminal Mappings read prevents a zero-update run,
    # or a race after the last write, from publishing a snapshot based on a
    # stale quarantine set.
    try:
        terminal_alerts = parse_sync_alert_values(
            google_sync_alert_values(
                google_session, validate_sheet_id(sheet_id), alerts_tab
            )
        )
        if pending_alerts:
            verify_pending_alerts_are_open(pending_alerts, terminal_alerts)
    except SyncError:
        summary["sync_alert_verification_error"] = (
            "Sync Alerts could not be verified immediately before the build "
            "snapshot; the snapshot was blocked."
        )
        persist_reports()
        raise
    terminal_quarantine_keys = open_alert_quarantine_keys(terminal_alerts)
    if terminal_quarantine_keys != match_time_quarantine_keys:
        summary["sync_alert_quarantine_keys_added"] = len(
            terminal_quarantine_keys.difference(match_time_quarantine_keys)
        )
        summary["sync_alert_quarantine_keys_removed"] = len(
            match_time_quarantine_keys.difference(terminal_quarantine_keys)
        )
        summary["sync_alert_verification_error"] = (
            "The OPEN Sync Alerts quarantine set changed immediately before "
            "the build snapshot; the snapshot was blocked."
        )
        persist_reports()
        raise SyncError(summary["sync_alert_verification_error"])
    existing_alerts = terminal_alerts
    persistent_quarantine_keys = terminal_quarantine_keys
    status_quarantine_keys = snapshot_quarantine_keys(
        changed_rows, terminal_quarantine_keys
    )
    summary["open_sync_alerts"] = sum(
        1
        for row in terminal_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    )

    snapshot_validation = write_private_mapping_snapshot_bundle(
        effective_path=effective_snapshot_path,
        authoritative_path=authoritative_snapshot_path,
        manifest_path=snapshot_manifest_path,
        table=final_table,
        changed_rows=changed_rows,
        persistent_quarantine_keys=persistent_quarantine_keys,
    )
    add_snapshot_validation_to_summary(summary, snapshot_validation)
    persist_reports()
    return summary


def run_sync(
    *,
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    output_dir: Path,
    generated_at: str,
    snapshot_out: Path,
    authoritative_snapshot_out: Path | None = None,
    snapshot_manifest_out: Path | None = None,
    write_to_sheet: bool = False,
    google_session: Any = None,
    sheet_id: str = "",
    sheet_tab: str = "Mappings",
    alerts_tab: str = "Sync Alerts",
    provider_failures: Mapping[str, str] | None = None,
    server_configs: Sequence[ServerConfig] = (),
    all_source_file: Path | None = None,
    all_source_catalog_file: Path | None = None,
    epgshare_spool_out: Path | None = None,
    review_recheck_mode: str = "off",
    review_recheck_servers: Sequence[str] = (),
    coverage_fallback_limit: int = 0,
    use_gemini_ai: bool = False,
    gemini_api_key: str = "",
    ai_review_limit: int = 25,
    validate_native_review: bool = False,
    native_hint_summary: Mapping[str, int] | None = None,
    allow_insecure_http: bool = False,
) -> dict[str, Any]:
    normalized_recheck_mode = streaming.clean_text(
        review_recheck_mode, 20
    ).casefold() or "off"
    if normalized_recheck_mode not in {"off", "dry-run", "apply"}:
        raise SyncError("The REVIEW recheck mode must be off, dry-run, or apply.")
    if (
        isinstance(coverage_fallback_limit, bool)
        or not isinstance(coverage_fallback_limit, int)
        or not (
            0
            <= coverage_fallback_limit
            <= automatch.MAX_COVERAGE_FALLBACK_ROWS
        )
    ):
        raise SyncError(
            "Coverage fallback limit must be an integer from 0 to "
            f"{automatch.MAX_COVERAGE_FALLBACK_ROWS:,}."
        )
    if validate_native_review and normalized_recheck_mode == "off":
        raise SyncError("Native REVIEW validation requires REVIEW recheck mode.")
    if normalized_recheck_mode != "off" or use_gemini_ai:
        if normalized_recheck_mode == "off":
            raise SyncError("Gemini review requires REVIEW recheck mode.")
        return _run_sync_review_mode(
            table=table,
            inventories=inventories,
            output_dir=output_dir,
            generated_at=generated_at,
            snapshot_out=snapshot_out,
            authoritative_snapshot_out=authoritative_snapshot_out,
            snapshot_manifest_out=snapshot_manifest_out,
            write_to_sheet=write_to_sheet,
            google_session=google_session,
            sheet_id=sheet_id,
            sheet_tab=sheet_tab,
            alerts_tab=alerts_tab,
            provider_failures=provider_failures,
            server_configs=server_configs,
            all_source_file=all_source_file,
            all_source_catalog_file=all_source_catalog_file,
            epgshare_spool_out=epgshare_spool_out,
            review_recheck_mode=normalized_recheck_mode,
            review_recheck_servers=review_recheck_servers,
            coverage_fallback_limit=coverage_fallback_limit,
            use_gemini_ai=use_gemini_ai,
            gemini_api_key=gemini_api_key,
            ai_review_limit=ai_review_limit,
            validate_native_review=validate_native_review,
            native_hint_summary=native_hint_summary,
            allow_insecure_http=allow_insecure_http,
        )
    effective_snapshot_path = Path(snapshot_out)
    authoritative_snapshot_path = (
        Path(authoritative_snapshot_out)
        if authoritative_snapshot_out is not None
        else effective_snapshot_path.with_name("authoritative_mapping.csv")
    )
    snapshot_manifest_path = (
        Path(snapshot_manifest_out)
        if snapshot_manifest_out is not None
        else effective_snapshot_path.with_name("mapping_snapshot_manifest.json")
    )
    authoritative_table = table
    existing_alerts: list[dict[str, str]] = []
    if write_to_sheet and google_session is None:
        raise SyncError("A Google authorized session is required for Sheet writes.")
    configs_by_server = {config.server_id: config for config in server_configs}
    for provider_inventory in inventories:
        config = configs_by_server.get(provider_inventory.server_id)
        if config is None:
            continue
        validate_provider_inventory_secret_safe(
            provider_inventory.categories,
            provider_inventory.channels,
            provider_reflection_needles(config, [config.base_url]),
        )
    if google_session is not None:
        if write_to_sheet:
            # Re-read Mappings immediately before its potential append.
            authoritative_table = parse_table_values(
                google_sheet_values(
                    google_session, validate_sheet_id(sheet_id), sheet_tab
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
        # Sync Alerts is a durable safety input even in a read-only refresh.
        # An OPEN event must continue quarantining its stream identity when the
        # corresponding provider is temporarily unavailable.
        existing_alerts = parse_sync_alert_values(
            google_sync_alert_values(
                google_session, validate_sheet_id(sheet_id), alerts_tab
            )
        )
    new_rows, changed_rows, missing_rows = compare_inventory(
        authoritative_table, inventories, discovered_at=generated_at
    )
    overlap_issues = inventory_overlap_issues(authoritative_table, inventories)
    pre_match_pending_alerts = pending_sync_alert_rows(
        changed_rows, existing_alerts, detected_at=generated_at
    )
    pre_match_quarantine_keys = open_alert_quarantine_keys(existing_alerts).union(
        {_row_identity(row) for row in pre_match_pending_alerts}
    )
    auto_match_summary: dict[str, Any] = {
        "auto_match_considered_rows": 0,
        "auto_match_provisional_rows": 0,
        "auto_matched_rows": 0,
        "auto_match_review_rows": len(new_rows),
        "review_recheck_considered_rows": 0,
        "review_recheck_safe_matches": 0,
        "review_recheck_still_review_rows": 0,
        "auto_match_rejected_programme_gates": 0,
        "coverage_fallback_enabled": coverage_fallback_limit > 0,
        "coverage_fallback_limit": coverage_fallback_limit,
        "coverage_fallback_candidate_rows": 0,
        "coverage_fallback_applied_rows": 0,
        "coverage_fallback_deferred_rows": 0,
        "coverage_fallback_ai_deferred_rows": 0,
        "new_channel_coverage_fallback_rows": 0,
        "review_recheck_coverage_fallback_rows": 0,
        "coverage_fallback_machine_prefilled_candidate_rows": 0,
        "coverage_fallback_machine_prefilled_applied_rows": 0,
        "coverage_fallback_legacy_server1_candidate_rows": 0,
        "coverage_fallback_legacy_server1_applied_rows": 0,
        "coverage_fallback_protected_manual_rows": 0,
        "coverage_fallback_protected_native_rows": 0,
        "coverage_fallback_replaced_by_native_rows": 0,
    }
    learned_alias_support_rows: tuple[dict[str, str], ...] = ()
    auto_match_inputs = (
        bool(all_source_file),
        bool(all_source_catalog_file),
        bool(epgshare_spool_out),
    )
    if any(auto_match_inputs) and not all(auto_match_inputs):
        raise SyncError(
            "ALL_SOURCES1 matching requires its XML file, official text catalog, "
            "and spool output together."
        )
    if (
        all_source_file is not None
        and all_source_catalog_file is not None
        and epgshare_spool_out is not None
    ):
        if overlap_issues:
            # A provider overlap is already a hard failure. Do not spend time
            # parsing the multi-gigabyte guide for a failed run.
            pass
        else:
            try:
                outcome = automatch.auto_match_and_spool(
                    mapping_rows=authoritative_table.rows,
                    inventories=inventories,
                    new_rows=new_rows,
                    quarantined_keys=pre_match_quarantine_keys,
                    all_source_file=Path(all_source_file),
                    all_source_catalog_file=Path(all_source_catalog_file),
                    spool_out=Path(epgshare_spool_out),
                    generated_at=generated_at,
                    coverage_fallback_limit=coverage_fallback_limit,
                )
            except automatch.AutoMatchError as exc:
                raise SyncError(str(exc)) from exc
            new_rows = list(outcome.rows)
            learned_alias_support_rows = _learned_alias_support_rows_from_outcome(
                outcome
            )
            auto_match_summary = outcome.summary_fields()
    projected_sheet_bytes = validate_projected_sheet_size(
        authoritative_table, new_rows
    )
    possible_reuse_count = sum(
        1
        for row in changed_rows
        if str(row.get("risk", "")).startswith("POSSIBLE_")
    )
    pending_alerts = pending_sync_alert_rows(
        changed_rows, existing_alerts, detected_at=generated_at
    )
    persistent_quarantine_keys = open_alert_quarantine_keys(existing_alerts)
    projected_alert_bytes = validate_projected_alert_size(
        existing_alerts, pending_alerts
    )
    existing_open_alert_count = sum(
        1
        for row in existing_alerts
        if streaming.clean_text(row.get("status", ""), 20).upper() == "OPEN"
    )
    summary: dict[str, Any] = {
        "version": SYNC_VERSION,
        "generated_at": generated_at,
        "sheet_mode": "write" if write_to_sheet else "dry-run",
        "review_recheck_mode": "off",
        "review_recheck_eligible_rows": 0,
        "review_recheck_deferred_rows": 0,
        "review_recheck_safe_matches_persisted": 0,
        "review_recheck_rows_updated": 0,
        "ai_review_enabled": False,
        "ai_review_considered_rows": 0,
        "ai_review_suggestion_rows": 0,
        "ai_review_abstained_rows": 0,
        "ai_review_error_rows": 0,
        **_native_review_summary_defaults(),
        **dict(native_hint_summary or {}),
        "inventory_rows": sum(len(inventory.channels) for inventory in inventories),
        "new_rows": len(new_rows),
        "appended_rows": 0,
        "changed_rows": len(changed_rows),
        "missing_rows": len(missing_rows),
        "possible_id_reuse_rows": possible_reuse_count,
        "new_sync_alerts": len(pending_alerts),
        "sync_alerts_appended": 0,
        "open_sync_alerts": existing_open_alert_count,
        "projected_sync_alert_rows": len(existing_alerts) + len(pending_alerts),
        "projected_sync_alert_bytes": projected_alert_bytes,
        "projected_sheet_rows": len(authoritative_table.rows) + len(new_rows),
        "projected_snapshot_bytes": projected_sheet_bytes,
        "safe_to_append": not overlap_issues,
        "overlap_issues": overlap_issues,
        "provider_failures": dict(provider_failures or {}),
        "servers": {
            inventory.server_id: {
                "source": inventory.source,
                "channel_rows": len(inventory.channels),
                "category_rows": len(inventory.categories),
            }
            for inventory in inventories
        },
        **auto_match_summary,
    }
    add_channel_status_to_summary(
        summary,
        authoritative_table,
        inventories,
        quarantined_keys=snapshot_quarantine_keys(
            changed_rows, persistent_quarantine_keys
        ),
    )
    write_reports(
        output_dir,
        inventories=inventories,
        new_rows=new_rows,
        changed_rows=changed_rows,
        missing_rows=missing_rows,
        summary=summary,
    )
    write_mapping_snapshot(output_dir / "mapping_before_append.csv", authoritative_table)
    if not write_to_sheet:
        snapshot_validation = write_private_mapping_snapshot_bundle(
            effective_path=effective_snapshot_path,
            authoritative_path=authoritative_snapshot_path,
            manifest_path=snapshot_manifest_path,
            table=authoritative_table,
            changed_rows=changed_rows,
            persistent_quarantine_keys=persistent_quarantine_keys,
        )
        add_snapshot_validation_to_summary(summary, snapshot_validation)
        write_reports(
            output_dir,
            inventories=inventories,
            new_rows=new_rows,
            changed_rows=changed_rows,
            missing_rows=missing_rows,
            summary=summary,
        )
    if overlap_issues:
        raise SyncError(" ".join(overlap_issues))
    if write_to_sheet:
        alert_write_error: SheetWriteError | None = None
        try:
            summary["sync_alerts_appended"] = append_sync_alert_rows(
                google_session,
                validate_sheet_id(sheet_id),
                alerts_tab,
                existing_alerts,
                pending_alerts,
            )
        except SheetWriteError as exc:
            summary["sync_alerts_appended"] = exc.appended_count
            summary["sync_alert_write_error"] = str(exc)
            alert_write_error = exc
        if pending_alerts:
            try:
                verified_alerts = parse_sync_alert_values(
                    google_sync_alert_values(
                        google_session,
                        validate_sheet_id(sheet_id),
                        alerts_tab,
                    )
                )
                verify_pending_alerts_are_open(pending_alerts, verified_alerts)
                persistent_quarantine_keys = open_alert_quarantine_keys(
                    verified_alerts
                )
                existing_alerts = verified_alerts
                summary["open_sync_alerts"] = sum(
                    1
                    for row in verified_alerts
                    if streaming.clean_text(row.get("status", ""), 20).upper()
                    == "OPEN"
                )
                if alert_write_error is not None:
                    summary["sync_alert_write_recovered_by_reread"] = True
            except SyncError:
                summary["sync_alert_verification_error"] = (
                    "New alerts were not confirmed by an authoritative re-read; "
                    "Mappings and the build snapshot were not changed."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise
        elif alert_write_error is not None:
            # Defensive only: an append helper cannot currently fail when its
            # input is empty, but never swallow a future write failure.
            raise alert_write_error
        if new_rows:
            new_rows_revalidated, new_row_revalidation_summary = (
                revalidate_auto_enabled_new_rows(
                    new_rows,
                    inventories,
                    server_configs,
                    allow_insecure_http=allow_insecure_http,
                    learned_alias_support_rows=learned_alias_support_rows,
                )
            )
            summary.update(new_row_revalidation_summary)
            if not new_rows_revalidated:
                summary["new_auto_provider_revalidation_error"] = (
                    "A newly auto-enabled provider identity or learned-alias "
                    "teaching identity changed or became unavailable before "
                    "append; no new Mapping row was sent."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise SyncError(summary["new_auto_provider_revalidation_error"])
        pre_append_table = authoritative_table
        if new_rows:
            try:
                pre_append_table = parse_table_values(
                    google_sheet_values(
                        google_session, validate_sheet_id(sheet_id), sheet_tab
                    ),
                    maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
                )
            except SyncError:
                summary["new_row_pre_append_mapping_error"] = (
                    "Mappings could not be authoritatively re-read immediately "
                    "before the new-channel append; no new Mapping row was sent."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise
            if mapping_table_fingerprint(
                pre_append_table
            ) != mapping_table_fingerprint(authoritative_table):
                summary["new_row_pre_append_mapping_error"] = (
                    "The Mappings tab changed after new-channel proposals were "
                    "prepared; no new Mapping row was sent."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise SyncError(summary["new_row_pre_append_mapping_error"])

        if new_rows:
            # Reacquire the alert authority only after the slower provider
            # check and exact Mappings pre-read, making it the final safety
            # condition adjacent to the append request.
            try:
                terminal_alerts = parse_sync_alert_values(
                    google_sync_alert_values(
                        google_session, validate_sheet_id(sheet_id), alerts_tab
                    )
                )
                if pending_alerts:
                    verify_pending_alerts_are_open(pending_alerts, terminal_alerts)
            except SyncError:
                summary["sync_alert_verification_error"] = (
                    "Sync Alerts changed or could not be verified immediately "
                    "before the new-channel append; no new Mapping row was sent."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise
            terminal_quarantine_keys = open_alert_quarantine_keys(terminal_alerts)
            if terminal_quarantine_keys != pre_match_quarantine_keys:
                summary["sync_alert_verification_error"] = (
                    "The OPEN Sync Alerts quarantine set changed after matching; "
                    "no new Mapping row was sent."
                )
                write_reports(
                    output_dir,
                    inventories=inventories,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    missing_rows=missing_rows,
                    summary=summary,
                )
                raise SyncError(summary["sync_alert_verification_error"])
            existing_alerts = terminal_alerts
            persistent_quarantine_keys = terminal_quarantine_keys
            summary["open_sync_alerts"] = sum(
                1
                for row in terminal_alerts
                if streaming.clean_text(row.get("status", ""), 20).upper()
                == "OPEN"
            )
        try:
            summary["appended_rows"] = append_google_sheet_rows(
                google_session,
                validate_sheet_id(sheet_id),
                sheet_tab,
                pre_append_table,
                new_rows,
            )
        except SheetWriteError as exc:
            summary["appended_rows"] = exc.appended_count
            summary["write_error"] = str(exc)
            try:
                final_table = parse_table_values(
                    google_sheet_values(
                        google_session, validate_sheet_id(sheet_id), sheet_tab
                    ),
                    maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
                )
                add_channel_status_to_summary(
                    summary,
                    final_table,
                    inventories,
                    quarantined_keys=snapshot_quarantine_keys(
                        changed_rows, persistent_quarantine_keys
                    ),
                )
                snapshot_validation = write_private_mapping_snapshot_bundle(
                    effective_path=effective_snapshot_path,
                    authoritative_path=authoritative_snapshot_path,
                    manifest_path=snapshot_manifest_path,
                    table=final_table,
                    changed_rows=changed_rows,
                    persistent_quarantine_keys=persistent_quarantine_keys,
                )
                add_snapshot_validation_to_summary(summary, snapshot_validation)
            except SyncError:
                summary["snapshot_after_write_error"] = "unavailable"
            write_reports(
                output_dir,
                inventories=inventories,
                new_rows=new_rows,
                changed_rows=changed_rows,
                missing_rows=missing_rows,
                summary=summary,
            )
            raise
        write_reports(
            output_dir,
            inventories=inventories,
            new_rows=new_rows,
            changed_rows=changed_rows,
            missing_rows=missing_rows,
            summary=summary,
        )
        # Re-read after append. This is the authoritative input used by the EPG
        # builder, never an optimistic in-memory merge.
        try:
            final_table = parse_table_values(
                google_sheet_values(
                    google_session, validate_sheet_id(sheet_id), sheet_tab
                ),
                maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
            )
        except SyncError:
            summary["post_append_read_error"] = (
                "Sheet append succeeded but authoritative re-read failed."
            )
            write_reports(
                output_dir,
                inventories=inventories,
                new_rows=new_rows,
                changed_rows=changed_rows,
                missing_rows=missing_rows,
                summary=summary,
            )
            raise
        try:
            verify_appended_mapping_rows(new_rows, final_table)
        except SyncError:
            summary["post_append_validation_error"] = (
                "Some appended mapping cells differed from the authoritative re-read."
            )
            write_reports(
                output_dir,
                inventories=inventories,
                new_rows=new_rows,
                changed_rows=changed_rows,
                missing_rows=missing_rows,
                summary=summary,
            )
            raise
        add_channel_status_to_summary(
            summary,
            final_table,
            inventories,
            quarantined_keys=snapshot_quarantine_keys(
                changed_rows, persistent_quarantine_keys
            ),
        )
        snapshot_validation = write_private_mapping_snapshot_bundle(
            effective_path=effective_snapshot_path,
            authoritative_path=authoritative_snapshot_path,
            manifest_path=snapshot_manifest_path,
            table=final_table,
            changed_rows=changed_rows,
            persistent_quarantine_keys=persistent_quarantine_keys,
        )
        add_snapshot_validation_to_summary(summary, snapshot_validation)
        write_reports(
            output_dir,
            inventories=inventories,
            new_rows=new_rows,
            changed_rows=changed_rows,
            missing_rows=missing_rows,
            summary=summary,
        )
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("bootstrap", "refresh"), default="refresh")
    parser.add_argument(
        "--mapping-file",
        type=Path,
        help="Offline/test input. Normal operation reads the private Google Sheet.",
    )
    parser.add_argument("--servers", nargs="+", default=list(DEFAULT_SERVERS))
    parser.add_argument("--output-dir", type=Path, default=Path(".build/inventory-sync"))
    parser.add_argument(
        "--snapshot-out",
        type=Path,
        default=Path(".build/inventory-sync/effective_mapping.csv"),
    )
    parser.add_argument(
        "--authoritative-snapshot-out",
        type=Path,
        help=(
            "Final private Sheet snapshot before OPEN-alert quarantine. Defaults "
            "beside --snapshot-out."
        ),
    )
    parser.add_argument(
        "--snapshot-manifest-out",
        type=Path,
        help=(
            "Hash-bound authoritative/effective snapshot manifest. Defaults "
            "beside --snapshot-out."
        ),
    )
    parser.add_argument("--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", ""))
    parser.add_argument("--sheet-tab", default="Mappings")
    parser.add_argument(
        "--alerts-tab",
        default=os.environ.get("GOOGLE_SYNC_ALERTS_TAB", "Sync Alerts"),
    )
    parser.add_argument("--write-to-sheet", action="store_true")
    parser.add_argument(
        "--review-recheck-mode",
        choices=("off", "dry-run", "apply"),
        default="off",
        help="Opt-in Smart-Rules pass over existing disabled REVIEW rows.",
    )
    parser.add_argument(
        "--review-recheck-servers",
        nargs="+",
        default=["server_1"],
        help="Server allowlist for the existing REVIEW recheck.",
    )
    parser.add_argument(
        "--coverage-fallback-limit",
        type=int,
        default=0,
        metavar="COUNT",
        help=(
            "Opt in to at most COUNT local channel-derived synthetic guides "
            "after verified real matching (0 disables the fallback)."
        ),
    )
    parser.add_argument(
        "--use-gemini-ai",
        action="store_true",
        help=(
            "Use Gemini only as a second verifier for unresolved, locally "
            "shortlisted candidates."
        ),
    )
    parser.add_argument(
        "--validate-native-review",
        action="store_true",
        help=(
            "Validate fresh Server 2/3 native IDs for eligible REVIEW rows; "
            "Server 1 native EPG remains forbidden."
        ),
    )
    parser.add_argument(
        "--ai-review-limit",
        type=int,
        choices=range(1, MAX_AI_REVIEW_ROWS + 1),
        default=25,
        metavar="COUNT",
    )
    parser.add_argument(
        "--all-source-file",
        type=Path,
        help="Downloaded EPGShare ALL_SOURCES1 XML or XML.GZ used for exact matching.",
    )
    parser.add_argument(
        "--all-source-catalog-file",
        type=Path,
        help="Official EPGShare ALL_SOURCES1 text catalog used to corroborate the XML.",
    )
    parser.add_argument(
        "--epgshare-spool-out",
        type=Path,
        help="Ephemeral sealed SQLite hand-off for the production EPG builder.",
    )
    parser.add_argument(
        "--minimum-server-channels",
        nargs="*",
        default=[],
        metavar="SERVER=COUNT",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        default=str(os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "")).strip().casefold()
        in streaming.TRUE_VALUES,
    )
    parser.add_argument("--now-utc", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    generated_at = generated_timestamp(args.now_utc)
    output_dir = Path(args.output_dir).resolve()
    snapshot_out = Path(args.snapshot_out).resolve()
    authoritative_snapshot_out = (
        Path(args.authoritative_snapshot_out).resolve()
        if args.authoritative_snapshot_out is not None
        else snapshot_out.with_name("authoritative_mapping.csv")
    )
    snapshot_manifest_out = (
        Path(args.snapshot_manifest_out).resolve()
        if args.snapshot_manifest_out is not None
        else snapshot_out.with_name("mapping_snapshot_manifest.json")
    )
    if len(
        {snapshot_out, authoritative_snapshot_out, snapshot_manifest_out}
    ) != 3:
        raise SyncError(
            "Authoritative, effective, and manifest snapshot paths must be different."
        )
    auto_match_args = (
        bool(args.all_source_file),
        bool(args.all_source_catalog_file),
        bool(args.epgshare_spool_out),
    )
    if any(auto_match_args) and not all(auto_match_args):
        raise SyncError(
            "Use --all-source-file, --all-source-catalog-file, and "
            "--epgshare-spool-out together."
        )
    if args.use_gemini_ai and args.review_recheck_mode == "off":
        raise SyncError("--use-gemini-ai requires REVIEW recheck mode.")
    if args.validate_native_review and args.review_recheck_mode == "off":
        raise SyncError("--validate-native-review requires REVIEW recheck mode.")
    if not (
        0
        <= args.coverage_fallback_limit
        <= automatch.MAX_COVERAGE_FALLBACK_ROWS
    ):
        raise SyncError(
            "--coverage-fallback-limit must be between 0 and "
            f"{automatch.MAX_COVERAGE_FALLBACK_ROWS:,}."
        )
    if (
        args.write_to_sheet or args.review_recheck_mode != "off"
    ) and not args.all_source_file:
        raise SyncError(
            "Sheet writes and REVIEW rechecking require the downloaded "
            "ALL_SOURCES1 files for safe verification."
        )
    all_source_file = (
        Path(args.all_source_file).resolve() if args.all_source_file else None
    )
    all_source_catalog_file = (
        Path(args.all_source_catalog_file).resolve()
        if args.all_source_catalog_file
        else None
    )
    epgshare_spool_out = (
        Path(args.epgshare_spool_out).resolve()
        if args.epgshare_spool_out
        else None
    )
    if (
        all_source_file is not None
        and all_source_catalog_file is not None
        and epgshare_spool_out is not None
        and len({all_source_file, all_source_catalog_file, epgshare_spool_out}) != 3
    ):
        raise SyncError("The ALL_SOURCES1 inputs and spool output must be different files.")
    selected_servers = [streaming.normalize_server_id(value) for value in args.servers]
    if len(selected_servers) != len(set(selected_servers)):
        raise SyncError("Server selections must be unique.")
    if args.mode == "bootstrap" and set(selected_servers) != set(DEFAULT_SERVERS):
        raise SyncError("Bootstrap mode requires Server 1, Server 2, and Server 3.")
    minimums = parse_server_minimums(args.minimum_server_channels)
    unknown_minimums = sorted(set(minimums) - set(selected_servers))
    if unknown_minimums:
        raise SyncError(
            "Minimum channel floors were supplied for unselected servers: "
            + ", ".join(unknown_minimums)
        )

    google_session = None
    if args.mapping_file is not None:
        if args.write_to_sheet or args.review_recheck_mode == "apply":
            raise SyncError(
                "--mapping-file cannot be combined with Google Sheet writes."
            )
        path = Path(args.mapping_file).resolve()
        if not path.is_file() or path.stat().st_size > streaming.MAX_MAPPING_BYTES:
            raise SyncError("The offline mapping file is missing or too large.")
        table = parse_mapping_csv(path.read_bytes())
    else:
        google_session = authorized_google_session(
            os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        )
        table = parse_table_values(
            google_sheet_values(
                google_session, validate_sheet_id(args.sheet_id), args.sheet_tab
            ),
            maximum_rows=MAX_GOOGLE_MAPPING_ROWS,
        )

    configs: list[ServerConfig] = []
    provider_failures: dict[str, str] = {}
    for server_id in selected_servers:
        try:
            configs.extend(load_server_configs([server_id]))
        except SyncError as exc:
            provider_failures[server_id] = str(exc)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"SKYTV-Channel-Inventory/{SYNC_VERSION}",
            "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
        }
    )
    try:
        inventories: list[PanelInventory] = []
        native_hint_summary: dict[str, int] = {}
        for config in configs:
            try:
                inventories.append(
                    fetch_panel_inventory(
                        session,
                        config,
                        allow_insecure_http=args.allow_insecure_http,
                    )
                )
            except SyncError as exc:
                provider_failures[config.server_id] = str(exc)
        if args.validate_native_review:
            inventories, native_hint_summary = enrich_review_inventories_from_m3u(
                session,
                inventories,
                configs,
                selected_servers=args.review_recheck_servers,
                allow_insecure_http=args.allow_insecure_http,
            )
    finally:
        session.close()

    floors_failed = [
        f"{inventory.server_id}={len(inventory.channels):,} (minimum {minimums[inventory.server_id]:,})"
        for inventory in inventories
        if inventory.server_id in minimums
        and len(inventory.channels) < minimums[inventory.server_id]
    ]
    missing_floor_servers = sorted(
        set(minimums) - {inventory.server_id for inventory in inventories}
    )
    if missing_floor_servers:
        floors_failed.extend(f"{server_id}=unavailable" for server_id in missing_floor_servers)
    bootstrap_error = ""
    if args.mode == "bootstrap" and provider_failures:
        bootstrap_error = "Bootstrap requires a valid inventory from all three servers."
    if floors_failed:
        bootstrap_error = (
            "Provider inventory failed the configured channel floors: "
            + "; ".join(floors_failed)
        )
    try:
        effective_recheck_mode = (
            "dry-run"
            if bootstrap_error and args.review_recheck_mode != "off"
            else args.review_recheck_mode
        )
        summary = run_sync(
            table=table,
            inventories=inventories,
            output_dir=output_dir,
            generated_at=generated_at,
            snapshot_out=snapshot_out,
            authoritative_snapshot_out=authoritative_snapshot_out,
            snapshot_manifest_out=snapshot_manifest_out,
            write_to_sheet=args.write_to_sheet and not bootstrap_error,
            google_session=google_session,
            sheet_id=args.sheet_id,
            sheet_tab=args.sheet_tab,
            alerts_tab=args.alerts_tab,
            provider_failures=provider_failures,
            server_configs=configs,
            all_source_file=all_source_file,
            all_source_catalog_file=all_source_catalog_file,
            epgshare_spool_out=epgshare_spool_out,
            review_recheck_mode=effective_recheck_mode,
            review_recheck_servers=args.review_recheck_servers,
            coverage_fallback_limit=args.coverage_fallback_limit,
            use_gemini_ai=args.use_gemini_ai and not bootstrap_error,
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            ai_review_limit=args.ai_review_limit,
            validate_native_review=args.validate_native_review,
            native_hint_summary=native_hint_summary,
            allow_insecure_http=args.allow_insecure_http,
        )
        if bootstrap_error:
            raise SyncError(bootstrap_error)
    finally:
        if google_session is not None:
            close = getattr(google_session, "close", None)
            if callable(close):
                close()
    print(
        "Inventory sync complete: "
        f"{summary['inventory_rows']:,} live channels, "
        f"{summary['new_rows']:,} new, "
        f"{summary['auto_matched_rows']:,} auto-matched, "
        f"{summary['appended_rows']:,} appended, "
        f"{summary['changed_rows']:,} changed-name reports, "
        f"{summary['missing_rows']:,} missing reports.",
        flush=True,
    )
    for server_id in sorted(provider_failures):
        label = server_id.replace("_", " ").title()
        print(
            f"WARNING: {label} inventory was unavailable; its existing Sheet rows "
            "were preserved and no missing-channel conclusion was made.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncError, streaming.BuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
