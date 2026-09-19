#!/usr/bin/env python3
"""Build an exact-stream channel-icon inventory and person research queue.

The command is intentionally read-only with respect to the mapping and XMLTV
inputs.  Every output record is keyed by ``server_id`` plus ``stream_id``;
channel names and shared/dummy EPG IDs are never used as identity keys.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlparse

from lxml import etree


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPOSITORY_ROOT / "config" / "channel_icons.csv"
DEFAULT_CATALOG = REPOSITORY_ROOT / "assets" / "logos" / "icon_catalog.csv"
DEFAULT_NAMED_FALLBACK_CATALOG = (
    REPOSITORY_ROOT / "assets" / "logos" / "named_person_fallback_catalog.csv"
)
DEFAULT_LOGO_ROOT = REPOSITORY_ROOT / "assets" / "logos"
DEFAULT_INVENTORY = (
    REPOSITORY_ROOT
    / ".build"
    / "private-icon-audit"
    / "channel_icon_inventory.csv"
)
DEFAULT_RESEARCH_QUEUE = (
    REPOSITORY_ROOT
    / ".build"
    / "private-icon-audit"
    / "named_person_research.csv"
)

TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "n", "off"})
REQUIRED_MAPPING_COLUMNS = frozenset(
    {
        "server_id",
        "stream_id",
        "enabled",
        "channel_name",
        "category_name",
        "genre",
        "channel_role",
        "action",
        "epg_id",
        "logo_url",
    }
)
FALLBACK_GENRES = frozenset(
    {
        "adult",
        "documentary",
        "education",
        "entertainment",
        "events",
        "general",
        "kids",
        "lifestyle",
        "movies",
        "music",
        "news",
        "radio",
        "religion",
        "shopping",
        "sports",
    }
)
SUPPORTED_LOGO_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
)
PERSON_CATEGORIES = {
    "bollywood singers 24/7": "singer",
    "punjabi singers 24/7": "singer",
    "pakistani singers 24/7": "singer",
    "bollywood movies/actors 24/7": "actor",
}
INVENTORY_COLUMNS = (
    "server_id",
    "stream_id",
    "channel_name",
    "category_name",
    "genre",
    "channel_role",
    "action",
    "epg_id",
    "person_role",
    "person_subject_candidate",
    "current_icon_origin",
    "current_icon_status",
    "current_icon_reference",
    "current_asset_id",
    "rights_status",
    "rights_review_status",
    "suggested_asset_id",
    "suggested_local_file",
    "next_action",
)
RESEARCH_COLUMNS = (
    "server_id",
    "stream_id",
    "channel_name",
    "category_name",
    "person_role",
    "subject_candidate",
    "preferred_asset_kind",
    "current_icon_origin",
    "current_icon_reference",
    "current_asset_id",
    "current_icon_status",
    "current_rights_status",
    "current_rights_review_status",
    "research_status",
    "license_requirement",
    "fallback_asset_id",
    "fallback_local_file",
    "notes",
)


class IconInventoryError(RuntimeError):
    """Raised when an input cannot safely produce a deterministic inventory."""


@dataclass(frozen=True)
class CatalogAsset:
    asset_id: str
    local_file: str
    subject_type: str
    asset_kind: str
    license_id: str
    review_status: str


@dataclass(frozen=True)
class ExactOverride:
    server_id: str
    stream_id: str
    epg_id: str
    channel_name: str
    reference: str
    local_file: str
    priority: int

    def matches(self, row: Mapping[str, str]) -> bool:
        if self.server_id != clean(row.get("server_id")):
            return False
        if self.stream_id != clean(row.get("stream_id")):
            return False
        if self.epg_id and self.epg_id.casefold() != clean(row.get("epg_id")).casefold():
            return False
        if self.channel_name and self.channel_name.casefold() != clean(
            row.get("channel_name")
        ).casefold():
            return False
        return True


@dataclass(frozen=True)
class SourceIconIndex:
    icons_by_exact_id: Mapping[str, str]
    variants_by_fold: Mapping[str, frozenset[str]]

    def lookup(self, epg_id: object) -> str:
        exact_id = clean(epg_id)
        if exact_id in self.icons_by_exact_id:
            return self.icons_by_exact_id[exact_id]
        variants = self.variants_by_fold.get(canonical_label(exact_id), frozenset())
        if len(variants) != 1:
            return ""
        return self.icons_by_exact_id.get(next(iter(variants)), "")


def clean(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def canonical_label(value: object) -> str:
    return " ".join(clean(value).casefold().split())


def parse_boolean(value: object, *, field: str, line_number: int) -> bool:
    normalized = clean(value).casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise IconInventoryError(
        f"Invalid {field!r} value on CSV line {line_number}: {value!r}."
    )


def safe_web_url(value: object, *, base_url: str = "") -> str:
    candidate = clean(value)
    if not candidate:
        return ""
    if base_url:
        candidate = urljoin(base_url, candidate)
    if len(candidate) > 4096 or any(character.isspace() for character in candidate):
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return candidate


def safe_local_file(value: object) -> str:
    candidate = clean(value).replace("\\", "/")
    if not candidate:
        return ""
    path = PurePosixPath(candidate)
    if path.is_absolute() or ".." in path.parts or candidate.startswith("./"):
        raise IconInventoryError(f"Unsafe local icon path: {value!r}.")
    return str(path)


def open_xml(path: Path) -> BinaryIO:
    with path.open("rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if compressed else path.open("rb")


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def extract_source_icons(
    xmltv_path: Path | None,
    wanted_epg_ids: Iterable[str],
    *,
    base_url: str = "",
) -> SourceIconIndex:
    """Return safe XMLTV icon URLs for only the requested channel IDs.

    Aggregate guides may interleave each upstream source's channels and
    programmes.  The full document is therefore streamed, while completed
    top-level records are immediately released from memory.
    """

    wanted = {canonical_label(value) for value in wanted_epg_ids if clean(value)}
    if xmltv_path is None or not wanted:
        return SourceIconIndex({}, {})
    if not xmltv_path.is_file():
        raise IconInventoryError(f"XMLTV file does not exist: {xmltv_path}")

    result: dict[str, str] = {}
    variants: dict[str, set[str]] = {}
    try:
        with open_xml(xmltv_path) as handle:
            context = etree.iterparse(
                handle,
                events=("start", "end"),
                recover=False,
                huge_tree=True,
                load_dtd=False,
                no_network=True,
                resolve_entities=False,
                remove_comments=True,
                remove_pis=True,
            )
            root_seen = False
            for event, element in context:
                tag = local_tag(element.tag)
                if not root_seen and event == "start":
                    root_seen = True
                    if tag != "tv":
                        raise IconInventoryError("XMLTV root element must be <tv>.")
                    if clean(element.getroottree().docinfo.doctype):
                        raise IconInventoryError("XMLTV DTD declarations are not allowed.")
                if event != "end" or tag not in {"channel", "programme"}:
                    continue
                parent = element.getparent()
                if tag == "channel" and (
                    parent is None or local_tag(parent.tag) == "tv"
                ):
                    channel_id = clean(element.attrib.get("id"))
                    folded = canonical_label(channel_id)
                    if channel_id and folded in wanted:
                        variants.setdefault(folded, set()).add(channel_id)
                        if channel_id not in result:
                            for child in element:
                                if local_tag(child.tag) != "icon":
                                    continue
                                icon_url = safe_web_url(
                                    child.attrib.get("src"), base_url=base_url
                                )
                                if icon_url:
                                    result[channel_id] = icon_url
                                    break
                element.clear()
                while parent is not None and element.getprevious() is not None:
                    del parent[0]
            del context
            if not root_seen:
                raise IconInventoryError("XMLTV document is empty.")
    except IconInventoryError:
        raise
    except (OSError, UnicodeError, etree.XMLSyntaxError) as exc:
        raise IconInventoryError(f"Could not read XMLTV channel icons: {exc}") from exc
    return SourceIconIndex(
        result,
        {folded: frozenset(values) for folded, values in variants.items()},
    )


def load_enabled_mapping(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise IconInventoryError(f"Mapping CSV does not exist: {path}")
    rows: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_MAPPING_COLUMNS.difference(reader.fieldnames or ())
        if missing:
            raise IconInventoryError(
                "Mapping CSV is missing required columns: " + ", ".join(sorted(missing))
            )
        for line_number, raw in enumerate(reader, start=2):
            if not parse_boolean(
                raw.get("enabled"), field="enabled", line_number=line_number
            ):
                continue
            row = {key: clean(value) for key, value in raw.items() if key is not None}
            key = (row["server_id"], row["stream_id"])
            if not all(key):
                raise IconInventoryError(
                    f"Enabled row on CSV line {line_number} has a blank exact identity."
                )
            if key in identities:
                raise IconInventoryError(
                    "Duplicate enabled exact identity on CSV line "
                    f"{line_number}: {key[0]}/{key[1]}."
                )
            identities.add(key)
            rows.append(row)
    return rows


def load_catalogs(paths: Iterable[Path | None]) -> dict[str, CatalogAsset]:
    result: dict[str, CatalogAsset] = {}
    sources: dict[str, Path] = {}
    for path in paths:
        if path is None or not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {
                "asset_id",
                "local_file",
                "subject_type",
                "asset_kind",
                "license_id",
                "review_status",
            }
            missing = required.difference(reader.fieldnames or ())
            if missing:
                raise IconInventoryError(
                    f"Icon catalog {path} is missing required columns: "
                    + ", ".join(sorted(missing))
                )
            for line_number, row in enumerate(reader, start=2):
                local_file = safe_local_file(row.get("local_file"))
                if not local_file:
                    continue
                asset = CatalogAsset(
                    asset_id=clean(row.get("asset_id")),
                    local_file=local_file,
                    subject_type=canonical_label(row.get("subject_type")),
                    asset_kind=canonical_label(row.get("asset_kind")),
                    license_id=clean(row.get("license_id")) or "UNRECORDED",
                    review_status=canonical_label(row.get("review_status"))
                    or "unreviewed",
                )
                existing = result.get(local_file)
                if existing is not None and existing != asset:
                    raise IconInventoryError(
                        "Conflicting catalog records for "
                        f"{local_file!r} in {sources[local_file]} and {path} "
                        f"(line {line_number})."
                    )
                result[local_file] = asset
                sources[local_file] = path
    return result


def load_exact_overrides(
    path: Path | None,
    *,
    logo_root: Path | None = None,
) -> dict[tuple[str, str], tuple[ExactOverride, ...]]:
    """Load only overrides carrying a complete server/stream identity."""

    if path is None or not path.is_file():
        return {}
    choices: dict[tuple[str, str], list[ExactOverride]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"enabled", "server_id", "stream_id", "icon_url", "local_file"}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise IconInventoryError(
                "Icon override CSV is missing required columns: "
                + ", ".join(sorted(missing))
            )
        for line_number, row in enumerate(reader, start=2):
            if not parse_boolean(
                row.get("enabled"), field="enabled", line_number=line_number
            ):
                continue
            server_id = clean(row.get("server_id"))
            stream_id = clean(row.get("stream_id"))
            if not server_id or server_id == "*" or not stream_id:
                # Broad rules are deliberately excluded from this audit.  They
                # cannot prove which one stream the artwork belongs to.
                continue
            icon_url = safe_web_url(row.get("icon_url"))
            if clean(row.get("icon_url")) and not icon_url:
                raise IconInventoryError(
                    f"Unsafe icon_url in override CSV line {line_number}."
                )
            # A direct URL has documented precedence.  Do not inspect an
            # unused local_file value when a valid direct URL is present.
            local_file = "" if icon_url else safe_local_file(row.get("local_file"))
            if local_file:
                if PurePosixPath(local_file).suffix.casefold() not in SUPPORTED_LOGO_EXTENSIONS:
                    raise IconInventoryError(
                        f"Unsupported local icon extension on CSV line {line_number}."
                    )
                if logo_root is not None:
                    resolved_root = logo_root.resolve()
                    source = (resolved_root / local_file).resolve()
                    if resolved_root not in source.parents or not source.is_file():
                        raise IconInventoryError(
                            f"Local icon does not exist on CSV line {line_number}."
                        )
            reference = icon_url or local_file
            if not reference:
                continue
            priority_text = clean(row.get("priority"))
            try:
                priority = int(priority_text or "0")
            except ValueError as exc:
                raise IconInventoryError(
                    f"Invalid override priority on CSV line {line_number}."
                ) from exc
            choice = ExactOverride(
                server_id=server_id,
                stream_id=stream_id,
                epg_id=clean(row.get("epg_id")),
                channel_name=clean(row.get("channel_name")),
                reference=reference,
                local_file="" if icon_url else local_file,
                priority=priority,
            )
            key = (server_id, stream_id)
            if choice not in choices.setdefault(key, []):
                choices[key].append(choice)
    return {
        key: tuple(sorted(group, key=lambda choice: choice.priority, reverse=True))
        for key, group in choices.items()
    }


def choose_exact_override(
    row: Mapping[str, str],
    overrides: Mapping[tuple[str, str], Sequence[ExactOverride]],
) -> ExactOverride | None:
    key = (clean(row.get("server_id")), clean(row.get("stream_id")))
    matching = [choice for choice in overrides.get(key, ()) if choice.matches(row)]
    if not matching:
        return None
    winning_priority = max(choice.priority for choice in matching)
    winners = [choice for choice in matching if choice.priority == winning_priority]
    references = {(choice.reference, choice.local_file) for choice in winners}
    if len(references) > 1:
        raise IconInventoryError(
            "Conflicting matching icon overrides share the winning priority for "
            f"{key[0]}/{key[1]}."
        )
    return winners[0]


def load_named_fallback_overrides(
    path: Path | None,
    *,
    logo_root: Path | None = None,
) -> dict[tuple[str, str], tuple[ExactOverride, ...]]:
    """Load a private exact-stream map made by the named fallback generator."""

    if path is None:
        return {}
    if not path.is_file():
        raise IconInventoryError(f"Named fallback map does not exist: {path}")
    choices: dict[tuple[str, str], list[ExactOverride]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "server_id",
            "stream_id",
            "channel_name",
            "local_file",
            "priority",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise IconInventoryError(
                "Named fallback map is missing required columns: "
                + ", ".join(sorted(missing))
            )
        for line_number, row in enumerate(reader, start=2):
            server_id = clean(row.get("server_id"))
            stream_id = clean(row.get("stream_id"))
            if not server_id or server_id == "*" or not stream_id:
                raise IconInventoryError(
                    f"Named fallback map line {line_number} lacks an exact identity."
                )
            local_file = safe_local_file(row.get("local_file"))
            if not local_file:
                raise IconInventoryError(
                    f"Named fallback map line {line_number} has no local_file."
                )
            if PurePosixPath(local_file).suffix.casefold() not in SUPPORTED_LOGO_EXTENSIONS:
                raise IconInventoryError(
                    f"Unsupported named fallback extension on CSV line {line_number}."
                )
            if logo_root is not None:
                resolved_root = logo_root.resolve()
                source = (resolved_root / local_file).resolve()
                if resolved_root not in source.parents or not source.is_file():
                    raise IconInventoryError(
                        f"Named fallback file does not exist on CSV line {line_number}."
                    )
            try:
                priority = int(clean(row.get("priority")) or "300")
            except ValueError as exc:
                raise IconInventoryError(
                    f"Invalid named fallback priority on CSV line {line_number}."
                ) from exc
            choice = ExactOverride(
                server_id=server_id,
                stream_id=stream_id,
                epg_id="",
                channel_name=clean(row.get("channel_name")),
                reference=local_file,
                local_file=local_file,
                priority=priority,
            )
            key = (server_id, stream_id)
            if choice not in choices.setdefault(key, []):
                choices[key].append(choice)
    return {
        key: tuple(sorted(group, key=lambda choice: choice.priority, reverse=True))
        for key, group in choices.items()
    }


def merge_override_groups(
    *groups: Mapping[tuple[str, str], Sequence[ExactOverride]],
) -> dict[tuple[str, str], tuple[ExactOverride, ...]]:
    merged: dict[tuple[str, str], list[ExactOverride]] = {}
    for group in groups:
        for key, choices in group.items():
            destination = merged.setdefault(key, [])
            for choice in choices:
                if choice not in destination:
                    destination.append(choice)
    return {
        key: tuple(sorted(choices, key=lambda choice: choice.priority, reverse=True))
        for key, choices in merged.items()
    }


def fallback_name(row: Mapping[str, str]) -> str:
    if canonical_label(row.get("channel_role")) == "radio":
        return "radio"
    genre = canonical_label(row.get("genre"))
    return genre if genre in FALLBACK_GENRES else "general"


def fallback_asset(row: Mapping[str, str]) -> tuple[str, str]:
    name = fallback_name(row)
    return f"category-{name}", f"generated/category-{name}.png"


def can_use_xmltv_source_icon(row: Mapping[str, str]) -> bool:
    """Match the production route that can actually inherit source artwork."""

    return clean(row.get("action")).upper() == "AUTO_EPGSHARE"


def classify_person_channel(row: Mapping[str, str]) -> tuple[str, str]:
    role = PERSON_CATEGORIES.get(canonical_label(row.get("category_name")), "")
    if not role:
        return "", ""

    original = clean(row.get("channel_name"))
    candidate = original
    if role == "singer":
        prefixes = (
            r"^HINDI\s*[-|]\s*(?:SINGER\s+)?",
            r"^PAKISTANI\s+SINGER\s*(?:[-|]\s*)?",
            r"^PAKISTANI\s*[-|]\s*",
            r"^PUNJABI\s*[-|]\s*SINGER\s*[-|]?\s*",
        )
    else:
        prefixes = (r"^HINDI\s*[-|]\s*(?:ACTOR\s+)?",)

    matched = False
    for pattern in prefixes:
        stripped, substitutions = re.subn(pattern, "", candidate, flags=re.IGNORECASE)
        if substitutions:
            candidate = stripped
            matched = True
            break
    if not matched:
        # A broad 24/7 category can contain a generic music/movie channel.
        # Only a channel with the category's anchored person-name grammar is
        # allowed into the portrait research queue.
        return "", ""

    if role == "singer":
        candidate = re.sub(
            r"\s+(?:SONGS?|SNOGS)\s*(?:HD|UHD|4K)?$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    else:
        candidate = re.sub(
            r"\s+MOVIES?\s*(?:HD|UHD|4K)?$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    candidate = re.sub(r"\s+(?:HD|UHD|4K)$", "", candidate, flags=re.IGNORECASE)
    candidate = " ".join(candidate.split()).strip(" -|")
    if not candidate or candidate.casefold() in {"bollywood", "hindi", "singer", "actor"}:
        return role, ""
    return role, candidate


def approved_person_asset(asset: CatalogAsset | None) -> bool:
    if asset is None:
        return False
    return (
        asset.subject_type == "person"
        and asset.asset_kind == "person_photo"
        and asset.review_status == "approved"
        and asset.license_id.casefold() not in {"", "unknown", "unrecorded"}
    )


def catalog_asset_for_local_file(
    catalog: Mapping[str, CatalogAsset], local_file: str
) -> CatalogAsset | None:
    asset = catalog.get(local_file)
    if asset is not None or not local_file.casefold().endswith(".png"):
        return asset
    vector_file = str(PurePosixPath(local_file).with_suffix(".svg"))
    vector_asset = catalog.get(vector_file)
    if (
        vector_asset is not None
        and vector_asset.asset_kind == "original_vector"
        and vector_asset.license_id == "ORIGINAL"
        and vector_asset.review_status == "approved"
    ):
        # Category PNGs are deterministic raster exports of their cataloged
        # original SVGs.  They inherit the same project-owned rights record.
        return vector_asset
    return None


def build_outputs(
    rows: Sequence[Mapping[str, str]],
    *,
    source_icons: SourceIconIndex,
    overrides: Mapping[tuple[str, str], Sequence[ExactOverride]],
    catalog: Mapping[str, CatalogAsset],
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
    inventory: list[dict[str, str]] = []
    research: list[dict[str, str]] = []
    summary = {
        "enabled_channels": len(rows),
        "current_exact_overrides": 0,
        "current_mapping_urls": 0,
        "current_xmltv_icons": 0,
        "current_missing": 0,
        "invalid_mapping_urls": 0,
        "named_person_channels": 0,
        "named_person_complete": 0,
        "named_person_needing_license_research": 0,
        "named_person_needing_subject_review": 0,
    }

    for row in rows:
        server_id = clean(row.get("server_id"))
        stream_id = clean(row.get("stream_id"))
        exact = choose_exact_override(row, overrides)
        mapping_url_raw = clean(row.get("logo_url"))
        mapping_url = safe_web_url(mapping_url_raw)
        source_url = ""
        if can_use_xmltv_source_icon(row):
            source_url = source_icons.lookup(row.get("epg_id"))
        if mapping_url_raw and not mapping_url:
            summary["invalid_mapping_urls"] += 1

        asset: CatalogAsset | None = None
        if mapping_url:
            origin = "mapping_url"
            reference = mapping_url
            asset_id = ""
            rights_status = "LICENSE_NOT_CHECKED"
            rights_review = "unreviewed"
            summary["current_mapping_urls"] += 1
        elif exact is not None:
            origin = "exact_override"
            reference = exact.reference
            asset = (
                catalog_asset_for_local_file(catalog, exact.local_file)
                if exact.local_file
                else None
            )
            asset_id = asset.asset_id if asset else ""
            rights_status = asset.license_id if asset else "UNRECORDED"
            rights_review = asset.review_status if asset else "unreviewed"
            summary["current_exact_overrides"] += 1
        elif source_url:
            origin = "xmltv_source"
            reference = source_url
            asset_id = ""
            rights_status = "LICENSE_NOT_CHECKED"
            rights_review = "unreviewed"
            summary["current_xmltv_icons"] += 1
        else:
            origin = "none"
            reference = ""
            asset_id = ""
            rights_status = "NOT_APPLICABLE"
            rights_review = "not_started"
            summary["current_missing"] += 1

        role, subject = classify_person_channel(row)
        suggested_asset_id, suggested_local_file = fallback_asset(row)
        if approved_person_asset(asset):
            current_status = "portrait_ready"
        elif (
            asset is not None
            and asset.asset_kind
            in {"generated_named_fallback", "original_named_fallback"}
            and asset.review_status in {"approved", "fallback_ready"}
        ):
            current_status = "fallback_ready"
        elif origin == "none":
            current_status = "missing"
        elif rights_review == "approved":
            current_status = "ready"
        else:
            current_status = "available_unverified"
        if role and not approved_person_asset(asset):
            next_action = "research_free_portrait" if subject else "review_person_name"
        elif origin == "none":
            next_action = "apply_original_fallback"
        elif rights_review != "approved":
            next_action = "verify_license"
        else:
            next_action = "none"

        inventory.append(
            {
                "server_id": server_id,
                "stream_id": stream_id,
                "channel_name": clean(row.get("channel_name")),
                "category_name": clean(row.get("category_name")),
                "genre": clean(row.get("genre")),
                "channel_role": clean(row.get("channel_role")),
                "action": clean(row.get("action")),
                "epg_id": clean(row.get("epg_id")),
                "person_role": role,
                "person_subject_candidate": subject,
                "current_icon_origin": origin,
                "current_icon_status": current_status,
                "current_icon_reference": reference,
                "current_asset_id": asset_id,
                "rights_status": rights_status,
                "rights_review_status": rights_review,
                "suggested_asset_id": suggested_asset_id,
                "suggested_local_file": suggested_local_file,
                "next_action": next_action,
            }
        )

        if not role:
            continue
        summary["named_person_channels"] += 1
        if approved_person_asset(asset):
            research_status = "complete"
            notes = "Approved exact-stream person asset is recorded in the catalog."
            summary["named_person_complete"] += 1
        elif not subject:
            research_status = "needs_subject_review"
            notes = "The provider name did not contain a clear person name."
            summary["named_person_needing_subject_review"] += 1
        else:
            research_status = "needs_license_research"
            notes = "Find a reusable real portrait; use the original fallback if unavailable."
            summary["named_person_needing_license_research"] += 1
        research_fallback_asset_id = suggested_asset_id
        research_fallback_local_file = suggested_local_file
        if current_status == "fallback_ready" and asset is not None:
            research_fallback_asset_id = asset.asset_id
            research_fallback_local_file = asset.local_file
        research.append(
            {
                "server_id": server_id,
                "stream_id": stream_id,
                "channel_name": clean(row.get("channel_name")),
                "category_name": clean(row.get("category_name")),
                "person_role": role,
                "subject_candidate": subject,
                "preferred_asset_kind": "freely_licensed_real_portrait",
                "current_icon_origin": origin,
                "current_icon_reference": reference,
                "current_asset_id": asset_id,
                "current_icon_status": current_status,
                "current_rights_status": rights_status,
                "current_rights_review_status": rights_review,
                "research_status": research_status,
                "license_requirement": "Public domain, CC0, CC BY, or compatible CC BY-SA",
                "fallback_asset_id": research_fallback_asset_id,
                "fallback_local_file": research_fallback_local_file,
                "notes": notes,
            }
        )

    return inventory, research, summary


def write_csv_atomic(
    path: Path, rows: Sequence[Mapping[str, str]], fieldnames: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fieldnames, extrasaction="raise", lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        # These two reports contain the private server/stream inventory.
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def run(
    *,
    mapping_csv: Path,
    xmltv_path: Path | None,
    config_csv: Path | None,
    catalog_csvs: Sequence[Path],
    logo_root: Path | None,
    named_fallback_map: Path | None,
    inventory_out: Path,
    research_out: Path,
    xmltv_base_url: str = "",
) -> dict[str, int]:
    if inventory_out.resolve() == research_out.resolve():
        raise IconInventoryError("Inventory and research outputs must be different files.")
    rows = load_enabled_mapping(mapping_csv)
    source_icons = extract_source_icons(
        xmltv_path,
        (row.get("epg_id", "") for row in rows),
        base_url=xmltv_base_url,
    )
    overrides = merge_override_groups(
        load_exact_overrides(config_csv, logo_root=logo_root),
        load_named_fallback_overrides(named_fallback_map, logo_root=logo_root),
    )
    catalog = load_catalogs(catalog_csvs)
    inventory, research, summary = build_outputs(
        rows, source_icons=source_icons, overrides=overrides, catalog=catalog
    )
    write_csv_atomic(inventory_out, inventory, INVENTORY_COLUMNS)
    write_csv_atomic(research_out, research, RESEARCH_COLUMNS)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-csv", type=Path, required=True)
    parser.add_argument("--xmltv", type=Path)
    parser.add_argument(
        "--xmltv-base-url",
        default="",
        help="Optional source URL used to resolve relative XMLTV icon URLs.",
    )
    parser.add_argument("--config-csv", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--catalog-csv",
        type=Path,
        action="append",
        help=(
            "Repeatable public asset catalog. Defaults to icon_catalog.csv and "
            "named_person_fallback_catalog.csv when omitted."
        ),
    )
    parser.add_argument("--logo-root", type=Path, default=DEFAULT_LOGO_ROOT)
    parser.add_argument(
        "--named-fallback-map",
        type=Path,
        help="Optional private exact-stream map for original named fallbacks.",
    )
    parser.add_argument("--inventory-out", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--research-out", type=Path, default=DEFAULT_RESEARCH_QUEUE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(
            mapping_csv=args.mapping_csv,
            xmltv_path=args.xmltv,
            config_csv=args.config_csv,
            catalog_csvs=args.catalog_csv
            or [DEFAULT_CATALOG, DEFAULT_NAMED_FALLBACK_CATALOG],
            logo_root=args.logo_root,
            named_fallback_map=args.named_fallback_map,
            inventory_out=args.inventory_out,
            research_out=args.research_out,
            xmltv_base_url=args.xmltv_base_url,
        )
    except IconInventoryError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
