#!/usr/bin/env python3
"""Discover every live provider channel and safely add new rows to Google Sheets.

This inventory phase runs before the EPG build. It reads the complete live-
channel inventory for each configured Xtream server, compares exact
``(server_id, stream_id)`` identities with the private Google Sheet, and applies
the fail-closed Version 1 matcher only to previously unseen channels. Exact
matches with a strong guide are enabled; every uncertain result stays disabled
as ``REVIEW``. Existing Sheet rows are never edited or deleted.

Severe stream-ID reuse warnings are appended to the private ``Sync Alerts``
tab. An ``OPEN`` alert keeps that stream quarantined from effective builds until
the mapping is corrected and the user marks the alert ``RESOLVED``.

The production workflow enables Sheet writes. Omitting ``--write-to-sheet``
keeps a useful diagnostic/dry-run mode for manual checks and tests.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urljoin, urlparse, urlunparse

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_epg_streaming as streaming  # noqa: E402
import auto_match_inventory as automatch  # noqa: E402


SYNC_VERSION = "1.0"
DEFAULT_SERVERS = ("server_1", "server_2", "server_3")
MAX_PANEL_JSON_BYTES = 64 * 1024 * 1024
MAX_M3U_BYTES = 128 * 1024 * 1024
MAX_CHANNELS_PER_SERVER = 250_000
MAX_SHEET_BYTES = 80 * 1024 * 1024
MAX_GOOGLE_MAPPING_ROWS = 150_000
MAX_SYNC_ALERT_ROWS = 10_000
MAX_SYNC_ALERT_BYTES = 8 * 1024 * 1024
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
        "epg_channel_id": streaming.clean_identifier(
            first_present(
                item,
                (
                    "epg_channel_id",
                    "epgChannelId",
                    "epg_id",
                    "epgId",
                    "tvg_id",
                    "tvg-id",
                ),
            ),
            300,
        ),
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
        result[match.group(1).casefold()] = value.strip()
    return result


def m3u_category_id(group_title: str) -> str:
    digest = hashlib.sha1(group_title.casefold().encode("utf-8")).hexdigest()[:12]
    return f"m3u_group_{digest}"


def provider_text_variants(value: object) -> frozenset[str]:
    """Return raw/URL-rendered forms used only for in-memory comparisons."""
    text = str(value or "")
    if not text:
        return frozenset()
    variants = {
        text,
        unquote(text),
        unquote_plus(text),
        quote(text, safe=""),
        quote_plus(text, safe=""),
    }
    return frozenset(variant.casefold() for variant in variants if variant)


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
    username_values = provider_text_variants(username)
    if distinctive_provider_value(username):
        global_values.update(username_values)
    for base in service_bases:
        normalized_base = str(base or "").rstrip("/")
        global_values.update(provider_text_variants(normalized_base))
        parsed = urlparse(normalized_base)
        global_values.update(provider_text_variants(parsed.netloc))
        global_values.update(provider_text_variants(parsed.hostname or ""))

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
                folded = text.casefold()
                if folded and (
                    any(needle in folded for needle in policy.global_needles)
                    or any(
                        pattern.search(text)
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
        tvg_id = streaming.clean_identifier(
            attributes.get("tvg-id") or attributes.get("epg-id") or "", 300
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
        if len(rows) > int(maximum_rows):
            raise SyncError(
                f"The mapping table exceeds its configured {int(maximum_rows):,}-row limit."
            )
    return MappingTable(raw_headers=raw_headers, headers=headers, rows=rows)


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


def comparison_name_key(value: object) -> str:
    """Normalize case/space and display-quality suffixes for drift reporting."""
    text = streaming.clean_identifier(value, 300)
    text = QUALITY_SUFFIX_RE.sub("", text).strip()
    return " ".join(text.casefold().split())


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


def pending_sync_alert_rows(
    changed_rows: Sequence[Mapping[str, str]],
    existing_alerts: Sequence[Mapping[str, str]],
    *,
    detected_at: str,
) -> list[dict[str, str]]:
    seen = {
        sync_alert_identity(row)
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
        identity = sync_alert_identity(row)
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
    quarantine_keys = {
        (str(row.get("server_id", "")), str(row.get("stream_id", "")))
        for row in changed_rows
        if str(row.get("risk", "")).startswith("POSSIBLE_")
    }
    quarantine_keys.update(
        (streaming.normalize_server_id(server_id), streaming.clean_identifier(stream_id, 120))
        for server_id, stream_id in persistent_quarantine_keys
    )
    effective_rows: list[dict[str, str]] = []
    for original in table.rows:
        row = dict(original)
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
            row["reason"] = (
                "Effective snapshot quarantine: OPEN Sync Alerts stream-ID reuse "
                "review; the Google Sheet Mappings row was not changed."
            )
        effective_rows.append(row)
    write_csv_report(path, effective_rows, streaming.SHEET_COLUMNS)


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
    # Copy/format/filter updates are idempotent, unlike row appends. Retry only
    # this finalization batch so a transient quota/server fault cannot strand
    # newly appended rows outside the filter.
    # Three range writes at most, plus one final read-only confirmation pass.
    for attempt in range(4):
        response = None
        retryable = False
        try:
            response = session.post(url, json=body, timeout=(20, 180))
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


def run_sync(
    *,
    table: MappingTable,
    inventories: Sequence[PanelInventory],
    output_dir: Path,
    generated_at: str,
    snapshot_out: Path,
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
) -> dict[str, Any]:
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
    auto_match_summary: dict[str, Any] = {
        "auto_match_considered_rows": 0,
        "auto_match_provisional_rows": 0,
        "auto_matched_rows": 0,
        "auto_match_review_rows": len(new_rows),
        "auto_match_rejected_programme_gates": 0,
    }
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
                    all_source_file=Path(all_source_file),
                    all_source_catalog_file=Path(all_source_catalog_file),
                    spool_out=Path(epgshare_spool_out),
                    generated_at=generated_at,
                )
            except automatch.AutoMatchError as exc:
                raise SyncError(str(exc)) from exc
            new_rows = list(outcome.rows)
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
        write_mapping_snapshot(
            Path(snapshot_out),
            authoritative_table,
            changed_rows=changed_rows,
            persistent_quarantine_keys=persistent_quarantine_keys,
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
        try:
            summary["appended_rows"] = append_google_sheet_rows(
                google_session,
                validate_sheet_id(sheet_id),
                sheet_tab,
                authoritative_table,
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
                write_mapping_snapshot(
                    Path(snapshot_out),
                    final_table,
                    changed_rows=changed_rows,
                    persistent_quarantine_keys=persistent_quarantine_keys,
                )
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
        write_mapping_snapshot(
            Path(snapshot_out),
            final_table,
            changed_rows=changed_rows,
            persistent_quarantine_keys=persistent_quarantine_keys,
        )
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
    parser.add_argument("--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", ""))
    parser.add_argument("--sheet-tab", default="Mappings")
    parser.add_argument(
        "--alerts-tab",
        default=os.environ.get("GOOGLE_SYNC_ALERTS_TAB", "Sync Alerts"),
    )
    parser.add_argument("--write-to-sheet", action="store_true")
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
    if args.write_to_sheet and not args.all_source_file:
        raise SyncError(
            "Sheet writes require the downloaded ALL_SOURCES1 file so new channels "
            "can be checked safely before they are appended."
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
        if args.write_to_sheet:
            raise SyncError("--mapping-file cannot be combined with --write-to-sheet.")
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
        summary = run_sync(
            table=table,
            inventories=inventories,
            output_dir=output_dir,
            generated_at=generated_at,
            snapshot_out=snapshot_out,
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
