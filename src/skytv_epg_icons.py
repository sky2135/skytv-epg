"""Deterministic XMLTV channel-icon enrichment for SKY TV EPG builds.

The icon layer is intentionally independent from Smart Rules matching.  It never
changes a channel-to-EPG decision.  It only adds XMLTV ``<icon src="..."/>``
metadata after the approved mapping has been built.

Priority, highest first:
1. Explicit URL carried in an approved mapping row (when present).
2. Exact override from ``config/channel_icons.csv``.
3. Exact icon already present on the selected source XMLTV channel.

No fuzzy logo matching is performed.  A wrong logo is more harmful than a
missing logo, so unresolved channels remain without an icon until an exact
source or override is available.
"""
from __future__ import annotations

import csv
import gzip
import html
import io
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urljoin, urlparse

import pandas as pd
from lxml import etree

ICON_CONFIG_COLUMNS = (
    "enabled",
    "server_id",
    "epg_id",
    "channel_name",
    "icon_url",
    "local_file",
    "priority",
    "notes",
)
MAPPING_ICON_COLUMNS = (
    "icon_url",
    "stream_icon",
    "tvg_logo",
    "tvg-logo",
    "channel_icon",
    "logo",
)
SUPPORTED_LOGO_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
}
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disabled"}
_CHANNEL_LINE_RE = re.compile(r"^(?P<indent>\s*)<channel\s+id=(?P<quote>['\"])(?P<id>.*?)(?P=quote)>\s*$")
_ICON_LINE_RE = re.compile(r"^\s*<icon\s+[^>]*\bsrc=")


def _key(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_enabled(value: object) -> bool:
    text = _key(value)
    if not text:
        return True
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    return True


def safe_icon_url(value: object, *, base_url: str = "") -> str:
    """Return a safe absolute HTTP(S) URL or an empty string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(ord(char) < 32 for char in raw):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif base_url:
        raw = urljoin(base_url, raw)
    parsed = urlparse(raw)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    if not parsed.netloc or parsed.username or parsed.password:
        return ""
    return raw


def _safe_local_logo_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return ""
    if path.suffix.casefold() not in SUPPORTED_LOGO_EXTENSIONS:
        return ""
    return str(path)


def local_logo_url(public_base_url: str, local_file: object) -> str:
    relative = _safe_local_logo_path(local_file)
    base = str(public_base_url or "").strip().rstrip("/")
    if not relative or not base:
        return ""
    encoded = "/".join(quote(part) for part in PurePosixPath(relative).parts)
    return safe_icon_url(f"{base}/logos/{encoded}")


@dataclass(frozen=True)
class IconChoice:
    url: str
    source: str
    priority: int
    matched_by: str


@dataclass
class IconOverrides:
    by_epg_id: dict[tuple[str, str], IconChoice]
    by_channel_name: dict[tuple[str, str], IconChoice]
    rows_loaded: int = 0
    rows_rejected: int = 0

    def lookup(self, server_id: str, epg_id: str, channel_name: str) -> IconChoice | None:
        server = _key(server_id)
        scopes = (server, "*")
        epg = _key(epg_id)
        channel = _key(channel_name)
        for scope in scopes:
            if epg and (scope, epg) in self.by_epg_id:
                return self.by_epg_id[(scope, epg)]
        for scope in scopes:
            if channel and (scope, channel) in self.by_channel_name:
                return self.by_channel_name[(scope, channel)]
        return None


def _empty_overrides() -> IconOverrides:
    return IconOverrides(by_epg_id={}, by_channel_name={})


def load_icon_overrides(
    path: Path | str,
    *,
    public_base_url: str = "",
) -> IconOverrides:
    """Load exact icon overrides from CSV.

    The CSV may target an EPG ID, a visible channel name, or both.  ``server_id``
    may be ``server_1``/``server_2``/``server_3`` or ``*`` for all servers.
    Higher numeric priority wins when duplicate exact keys are present.
    """
    config_path = Path(path)
    if not config_path.is_file():
        return _empty_overrides()

    result = _empty_overrides()
    with config_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            if not _is_enabled(row.get("enabled", "")):
                continue
            server = _key(row.get("server_id", "")) or "*"
            if server not in {"*", "server_1", "server_2", "server_3"}:
                result.rows_rejected += 1
                continue
            epg = _key(row.get("epg_id", ""))
            channel = _key(row.get("channel_name", ""))
            if not epg and not channel:
                result.rows_rejected += 1
                continue
            url = safe_icon_url(row.get("icon_url", ""))
            source = "config_icon_url"
            if not url:
                url = local_logo_url(public_base_url, row.get("local_file", ""))
                source = "config_local_file"
            if not url:
                result.rows_rejected += 1
                continue
            try:
                priority = int(str(row.get("priority", "100") or "100").strip())
            except ValueError:
                priority = 100
            # Row number is a stable final tie-breaker; earlier rows win.
            effective_priority = priority * 1_000_000 - row_number
            choice = IconChoice(
                url=url,
                source=source,
                priority=effective_priority,
                matched_by="epg_id" if epg else "channel_name",
            )
            if epg:
                key = (server, epg)
                previous = result.by_epg_id.get(key)
                if previous is None or choice.priority > previous.priority:
                    result.by_epg_id[key] = choice
            if channel:
                key = (server, channel)
                previous = result.by_channel_name.get(key)
                channel_choice = IconChoice(
                    url=url,
                    source=source,
                    priority=effective_priority,
                    matched_by="channel_name",
                )
                if previous is None or channel_choice.priority > previous.priority:
                    result.by_channel_name[key] = channel_choice
            result.rows_loaded += 1
    return result


def _open_maybe_gzip(path: Path):
    with path.open("rb") as probe:
        magic = probe.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else path.open("rb")


def _local_name(tag: object) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def extract_source_icons(
    path: Path | str,
    wanted_ids: Iterable[str],
    *,
    base_url: str = "",
) -> dict[str, str]:
    """Read exact channel icons for wanted XMLTV IDs from one source file."""
    source_path = Path(path)
    wanted = {_key(value): str(value) for value in wanted_ids if _key(value)}
    if not source_path.is_file() or not wanted:
        return {}

    found: dict[str, str] = {}
    with _open_maybe_gzip(source_path) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            if _local_name(element.tag) != "channel":
                continue
            source_id = (element.get("id") or "").strip()
            lookup = _key(source_id)
            if lookup in wanted and lookup not in found:
                for child in element:
                    if _local_name(child.tag) != "icon":
                        continue
                    url = safe_icon_url(child.get("src"), base_url=base_url)
                    if url:
                        found[lookup] = url
                        break
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
        del context
    return found


def extract_icons_by_feed(
    wanted_by_feed: Mapping[str, set[str]],
    source_paths: Mapping[str, Path],
    source_base_urls: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    source_base_urls = source_base_urls or {}
    result: dict[str, dict[str, str]] = {}
    for feed in sorted(wanted_by_feed):
        path = source_paths.get(feed)
        if path is None:
            continue
        result[feed] = extract_source_icons(
            path,
            wanted_by_feed[feed],
            base_url=str(source_base_urls.get(feed, "")),
        )
    return result


def _mapping_icon(row: Mapping[str, Any]) -> str:
    for column in MAPPING_ICON_COLUMNS:
        if column in row:
            value = safe_icon_url(row.get(column, ""))
            if value:
                return value
    return ""


def resolve_icon_assignments(
    prepared: pd.DataFrame,
    *,
    server_id: str,
    source_icons_by_feed: Mapping[str, Mapping[str, str]],
    overrides: IconOverrides | None = None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Resolve icons for both name-based and canonical XMLTV channel IDs."""
    overrides = overrides or _empty_overrides()
    assignments: dict[str, IconChoice] = {}
    report: list[dict[str, str]] = []

    for row in prepared.to_dict("records"):
        epg_id = str(row.get("epg_id", "")).strip()
        channel_name = str(row.get("channel_name", "")).strip()
        feed = str(row.get("feed_key", row.get("epg_feed", ""))).strip()
        if not epg_id or not channel_name:
            continue

        choice: IconChoice | None = None
        mapping_url = _mapping_icon(row)
        if mapping_url:
            choice = IconChoice(mapping_url, "mapping_row", 3_000_000_000, "mapping_row")
        if choice is None:
            override = overrides.lookup(server_id, epg_id, channel_name)
            if override is not None:
                choice = IconChoice(
                    override.url,
                    override.source,
                    2_000_000_000 + override.priority,
                    override.matched_by,
                )
        if choice is None:
            source_url = source_icons_by_feed.get(feed, {}).get(_key(epg_id), "")
            source_url = safe_icon_url(source_url)
            if source_url:
                choice = IconChoice(source_url, f"source_xmltv:{feed}", 1_000_000_000, "epg_id")
        if choice is None:
            report.append({
                "server_id": server_id,
                "channel_name": channel_name,
                "epg_id": epg_id,
                "feed": feed,
                "icon_url": "",
                "icon_source": "unresolved",
                "matched_by": "",
            })
            continue

        # The generated XMLTV contains a visible name-based channel entry and,
        # for real feeds, a canonical EPG-ID entry.  Give both the same exact logo.
        for output_channel_id in (channel_name, epg_id):
            current = assignments.get(output_channel_id)
            if current is None or choice.priority > current.priority:
                assignments[output_channel_id] = choice
        report.append({
            "server_id": server_id,
            "channel_name": channel_name,
            "epg_id": epg_id,
            "feed": feed,
            "icon_url": choice.url,
            "icon_source": choice.source,
            "matched_by": choice.matched_by,
        })

    return {key: value.url for key, value in assignments.items()}, report


def _xml_unescape(value: str) -> str:
    return html.unescape(value)


def _xml_quote(value: str) -> str:
    from xml.sax.saxutils import quoteattr

    return quoteattr(value)


def inject_icons_into_xmltv(
    path: Path | str,
    assignments: Mapping[str, str],
) -> dict[str, int]:
    """Insert icons into a generated gzip XMLTV file without loading it all in RAM."""
    xmltv_path = Path(path)
    temp_path = xmltv_path.with_name(xmltv_path.name + ".icons.tmp")
    exact = {str(key): safe_icon_url(value) for key, value in assignments.items()}
    folded = {_key(key): value for key, value in exact.items() if value}

    channels = 0
    existing_icons = 0
    inserted_icons = 0
    current_id = ""
    current_has_icon = False

    try:
        with gzip.open(xmltv_path, "rt", encoding="utf-8", newline="") as source:
            with temp_path.open("wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                        for line in source:
                            stripped = line.rstrip("\r\n")
                            match = _CHANNEL_LINE_RE.match(stripped)
                            if match:
                                channels += 1
                                current_id = _xml_unescape(match.group("id"))
                                current_has_icon = False
                                output.write(stripped + "\n")
                                continue
                            if current_id and _ICON_LINE_RE.match(stripped):
                                current_has_icon = True
                                existing_icons += 1
                            if current_id and stripped.strip() == "</channel>":
                                if not current_has_icon:
                                    url = exact.get(current_id) or folded.get(_key(current_id), "")
                                    if url:
                                        indent = line[: len(line) - len(line.lstrip())]
                                        output.write(f"{indent}  <icon src={_xml_quote(url)}/>\n")
                                        inserted_icons += 1
                                output.write(stripped + "\n")
                                current_id = ""
                                current_has_icon = False
                                continue
                            output.write(stripped + "\n")
        os.replace(temp_path, xmltv_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "xmltv_channels": channels,
        "icons_already_present": existing_icons,
        "icons_inserted": inserted_icons,
        "channels_with_icons": existing_icons + inserted_icons,
        "channels_without_icons": max(0, channels - existing_icons - inserted_icons),
    }


def stage_logo_assets(source_dir: Path | str, public_dir: Path | str) -> dict[str, int]:
    """Copy permitted local logo assets into the Pages ``public/logos`` folder."""
    source = Path(source_dir)
    destination = Path(public_dir)
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    bytes_copied = 0
    if not source.is_dir():
        return {"files": 0, "bytes": 0, "skipped": 0}

    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if item.name in {"README.md", "ATTRIBUTION.md", "ATTRIBUTION.txt"}:
            target = destination / relative.with_suffix(".txt")
        elif item.suffix.casefold() in SUPPORTED_LOGO_EXTENSIONS:
            target = destination / relative
        else:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1
        bytes_copied += target.stat().st_size
    return {"files": copied, "bytes": bytes_copied, "skipped": skipped}
