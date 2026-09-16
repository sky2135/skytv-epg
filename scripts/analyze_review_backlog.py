#!/usr/bin/env python3
"""Measure the private EPG review backlog without changing external state.

This command reads the current Google Sheet and provider inventories, runs the
same frozen EPGShare matcher used by channel synchronization, validates native
Server 2/3 XMLTV schedules, and emits one aggregate-only JSON report.  It does
not call any Google write API, commit files, invoke AI, or expose channel names,
stream IDs, XMLTV IDs, provider addresses, credentials, or candidate lists.

The report is advisory.  Native and placeholder findings remain REVIEW-only;
only strict EPGShare matches already satisfy Workflow 1's approval boundary.
Review clusters are deliberately server-local.  Cross-provider learning stays
inside the sealed Smart Rules alias memory and never relies on name clustering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests
SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
SRC_DIR = REPOSITORY_ROOT / "src"
for import_path in (SCRIPT_DIR, SRC_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import auto_match_inventory as automatch  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
import epg_catalog_stream as catalog_stream  # noqa: E402
import native_epg_review as native_review  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from skytv_epg_auto_match_v1 import MatcherIdentity  # noqa: E402
from skytv_epg_contextual_v8 import (  # noqa: E402
    install_contextual_v8,
    parse_channel_context_v8,
)


ANALYZER_VERSION = "1.0"
GOOGLE_SHEETS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/spreadsheets.readonly"
)
SUPPORTED_SERVERS = ("server_1", "server_2", "server_3")
MAX_ANALYSIS_REVIEW_ROWS = 60_000
MAX_PUBLIC_SUMMARY_BYTES = 64 * 1024
MAX_PUBLIC_SERVER_COUNT = 250_000
MAX_PUBLIC_TOTAL_COUNT = MAX_PUBLIC_SERVER_COUNT * len(SUPPORTED_SERVERS)
MAX_PUBLIC_CATALOG_COUNT = 250_000
# Retain these public aliases for the analyzer's compatibility tests and any
# external read-only tooling while keeping the implementation in one module.
MIN_NATIVE_CATALOG_IDS = native_review.MIN_NATIVE_CATALOG_IDS
NativeValidation = native_review.NativeValidation
SERVER_PUBLIC_FIELDS = frozenset(
    {
        "provider_available",
        "provider_channels",
        "review_rows",
        "review_clusters",
        "safety_blocked",
        "not_eligible",
        "analysis_eligible",
        "matcher_eligible",
        "native_candidates",
        "native_source_status",
        "native_verified",
        "strict_epgshare",
        "verified_placeholder",
        "no_verified_candidate",
    }
)
TOTAL_PUBLIC_FIELDS = frozenset(
    {
        "provider_channels",
        "review_rows",
        "review_clusters",
        "safety_blocked",
        "not_eligible",
        "analysis_eligible",
        "matcher_eligible",
        "native_candidates",
        "native_verified",
        "strict_epgshare",
        "verified_placeholder",
        "no_verified_candidate",
    }
)
CATALOG_PUBLIC_FIELDS = frozenset(
    {"alignment_mode", "confirmed_ids", "xml_only_ids", "text_only_ids"}
)
TOP_LEVEL_PUBLIC_FIELDS = frozenset(
    {"schema_version", "mode", "generated_at", "catalog", "totals", "servers"}
)


class BacklogAnalysisError(RuntimeError):
    """A controlled failure whose text contains no private channel data."""


@dataclass(frozen=True)
class ReviewItem:
    server_id: str
    stream_id: str
    row: Mapping[str, str] | None
    channel: Mapping[str, Any]
    cluster_key: tuple[Any, ...]
    safety_blocked: bool

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


def _canonical_key(server_id: object, stream_id: object) -> tuple[str, str]:
    try:
        server = streaming.normalize_server_id(server_id)
    except streaming.BuildError as exc:
        raise BacklogAnalysisError("A private row has an invalid server identity.") from exc
    stream = streaming.clean_identifier(stream_id, 120)
    if not stream:
        raise BacklogAnalysisError("A private row has a blank stream identity.")
    return server, stream


def _load_cluster_engine() -> Any:
    """Load and verify the pinned Smart Rules engine used for cluster parsing."""
    try:
        engine = automatch._load_frozen_engine()
        resolver = install_contextual_v8(engine)
        identity = MatcherIdentity.from_resolver(
            resolver, automatch.CONTEXTUAL_SOURCE_PATH, automatch.ENGINE_SOURCE_PATH
        )
    except Exception as exc:
        raise BacklogAnalysisError(
            "The frozen Smart Rules identity parser could not be loaded."
        ) from exc
    if not identity.is_expected:
        raise BacklogAnalysisError(
            "The frozen Smart Rules identity parser failed its integrity check."
        )
    return engine


def _cluster_key_from_context(
    *, engine: Any, server_id: str, channel: Mapping[str, Any], category_name: str,
    category_profile: Mapping[str, Any] | None
) -> tuple[Any, ...]:
    """Return a conservative station identity; never expose it in public output."""
    channel_name = streaming.clean_identifier(channel.get("name", ""), 300)
    try:
        context = parse_channel_context_v8(
            engine,
            channel_name,
            category_name,
            category_profile=category_profile,
        )
    except Exception as exc:
        raise BacklogAnalysisError("Smart Rules could not parse a provider identity.") from exc

    identity = str(context.bag_key or context.strict_key or "").strip()
    if not identity:
        # An exact Unicode fallback prevents every non-Latin/unknown station
        # from collapsing into one empty cluster.  It remains server-scoped.
        identity = unicodedata.normalize("NFKC", channel_name).casefold().strip()
        if not identity:
            identity = f"stream:{streaming.clean_identifier(channel.get('stream_id', ''), 120)}"

    # The analyzer groups spelling/quality variants only within one provider.
    # Cross-provider identity is never inferred from a name cluster: generic
    # banks can look identical on unrelated services. The sealed Smart Rules
    # engine may still use its independently verified human-approved alias
    # memory during strict EPGShare matching.
    # Ordinary SD/HD variants can share one review decision.  UHD/4K remains
    # distinct because it can be a separately scheduled service.
    protected_quality = "uhd" if str(context.quality) == "uhd" else ""
    semantic: tuple[Any, ...] = (
        identity,
        tuple(context.route_plan),
        tuple(sorted(context.languages)),
        str(context.direction),
        str(context.timeshift),
        bool(context.has_plus),
        bool(context.has_extra),
        bool(context.has_alternate),
        tuple(sorted(context.numbers)),
        tuple(sorted(context.content)),
        protected_quality,
    )
    return semantic + (("server", server_id),)


def build_cluster_keys(
    inventories: Sequence[sync.PanelInventory],
) -> dict[tuple[str, str], tuple[Any, ...]]:
    engine = _load_cluster_engine()
    result: dict[tuple[str, str], tuple[Any, ...]] = {}
    for inventory in inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        categories = {
            streaming.clean_identifier(row.get("category_id", ""), 120):
            streaming.clean_text(row.get("category_name", ""), 200)
            for row in inventory.categories
            if streaming.clean_identifier(row.get("category_id", ""), 120)
        }
        try:
            profiles, _signals = engine.build_inventory_profiles_v8(
                inventory.channels, categories
            )
        except Exception as exc:
            raise BacklogAnalysisError(
                "Smart Rules could not build provider identity profiles."
            ) from exc
        for channel in inventory.channels:
            key = _canonical_key(server_id, channel.get("stream_id", ""))
            if key in result:
                raise BacklogAnalysisError(
                    "A provider inventory contains a duplicate stream identity."
                )
            category_id = streaming.clean_identifier(
                channel.get("category_id", ""), 120
            )
            category_name = categories.get(
                category_id,
                streaming.clean_text(channel.get("category_name", ""), 200),
            )
            result[key] = _cluster_key_from_context(
                engine=engine,
                server_id=server_id,
                channel=channel,
                category_name=category_name,
                category_profile=profiles.get(category_id),
            )
    return result


def validate_native_xmltv(
    path: Path,
    *,
    server_id: str,
    requested_ids: Iterable[str],
    now_epoch: int,
) -> NativeValidation:
    """Run the shared exact native-ID validator under analyzer-safe errors."""
    try:
        return native_review.validate_native_xmltv(
            path,
            server_id=server_id,
            requested_ids=requested_ids,
            now_epoch=now_epoch,
        )
    except native_review.NativeReviewError as exc:
        # Shared validator messages are fixed and contain no provider data.
        raise BacklogAnalysisError(str(exc)) from exc


def _mapping_rows_by_key(
    table: sync.MappingTable,
) -> dict[tuple[str, str], Mapping[str, str]]:
    result: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in table.rows:
        key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        if key in result:
            raise BacklogAnalysisError("The private mapping contains duplicate identities.")
        result[key] = row
    return result


def _inventory_channels_by_key(
    inventories: Sequence[sync.PanelInventory],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for inventory in inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        for channel in inventory.channels:
            key = _canonical_key(server_id, channel.get("stream_id", ""))
            if key in result:
                raise BacklogAnalysisError(
                    "Provider inventories contain a duplicate stream identity."
                )
            result[key] = channel
    return result


def collect_review_items(
    *,
    table: sync.MappingTable,
    inventories: Sequence[sync.PanelInventory],
    cluster_keys: Mapping[tuple[str, str], tuple[Any, ...]],
    quarantined_keys: Iterable[tuple[str, str]],
) -> list[ReviewItem]:
    mappings = _mapping_rows_by_key(table)
    channels = _inventory_channels_by_key(inventories)
    blocked = frozenset(quarantined_keys)
    items: list[ReviewItem] = []
    for key in sorted(channels, key=lambda item: (item[0], streaming.stream_sort_key(item[1]))):
        row = mappings.get(key)
        action = ""
        if row is not None:
            action = (
                streaming.clean_text(row.get("action", "APPROVED"), 40).upper()
                or "APPROVED"
            )
            if action not in streaming.ALLOWED_ACTIONS:
                raise BacklogAnalysisError("A current mapping has an invalid action.")
        needs_review = (
            row is None
            or key in blocked
            or action in sync.REVIEW_QUEUE_ACTIONS
        )
        if not needs_review:
            continue
        cluster_key = cluster_keys.get(key)
        if cluster_key is None:
            raise BacklogAnalysisError("A current provider identity was not clustered.")
        items.append(
            ReviewItem(
                server_id=key[0],
                stream_id=key[1],
                row=row,
                channel=channels[key],
                cluster_key=cluster_key,
                safety_blocked=key in blocked,
            )
        )
    if len(items) > MAX_ANALYSIS_REVIEW_ROWS:
        raise BacklogAnalysisError(
            "The review backlog exceeds the read-only analyzer safety limit."
        )
    return items


def select_native_advisory_candidates(
    *,
    review_items: Sequence[ReviewItem],
    inventory_channels: Mapping[tuple[str, str], Mapping[str, Any]],
    changed_keys: Iterable[tuple[str, str]],
) -> dict[str, frozenset[tuple[str, str]]]:
    """Select fresh exact panel IDs for read-only validation only.

    A newly discovered row can legitimately have no stored native ID when the
    provider's API omitted it and the current authenticated M3U supplied it
    later.  Accept that original blank state, or an unchanged stored native
    ID, but never replace a conflicting/manual candidate.
    """
    changed = frozenset(changed_keys)
    result: dict[str, set[tuple[str, str]]] = {
        "server_1": set(),
        "server_2": set(),
        "server_3": set(),
    }
    for item in review_items:
        if (
            item.server_id == "server_1"
            or item.row is None
            or item.safety_blocked
            or item.key in changed
        ):
            continue
        row = item.row
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        if action != "REVIEW":
            continue
        try:
            enabled = streaming.parse_bool(
                row.get("enabled", ""),
                default=False,
                field_name="mapping enabled",
            )
        except streaming.BuildError as exc:
            raise BacklogAnalysisError(
                "A native-advisory mapping has invalid EPG controls."
            ) from exc
        if enabled:
            continue
        notes = streaming.clean_text(row.get("notes", ""), 2000)
        if not re.fullmatch(
            r"Automatically discovered "
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z; "
            r"existing Sheet rows were not changed\.",
            notes,
        ):
            continue
        channel = inventory_channels[item.key]
        if (
            channel.get(
                "_native_epg_id_raw_present",
                bool(channel.get("epg_channel_id", "")),
            )
            and channel.get("_native_epg_id_exact", True) is not True
        ):
            continue
        raw_stored_id = str(row.get("epg_id", "") or "")
        stored_id = streaming.clean_identifier(raw_stored_id, 300)
        raw_current_id = str(channel.get("epg_channel_id", "") or "")
        current_id = streaming.clean_identifier(raw_current_id, 300)
        if stored_id != raw_stored_id or current_id != raw_current_id:
            continue
        if not current_id or (stored_id and stored_id != current_id):
            continue

        # These are the only two untouched discovery states produced by
        # new_mapping_row().  Requiring the matching source/feed pair prevents
        # a partially edited/manual REVIEW row from entering the native lane.
        source = streaming.clean_text(row.get("source", ""), 40).casefold()
        feed = streaming.clean_text(row.get("epg_feed", ""), 80).casefold()
        if stored_id:
            untouched_controls = (
                source == "panel" and feed in {"panel", "server xmltv.php"}
            )
        else:
            # Mirror the writer exactly: new_mapping_row() creates this one
            # untouched blank-ID state. A blank legacy/manual panel row is not
            # silently repurposed even when the current provider supplies an ID.
            untouched_controls = (
                source == "epgshare01" and feed == "all_sources1"
            )
        if untouched_controls:
            result[item.server_id].add(item.key)
    return {
        server_id: frozenset(values) for server_id, values in result.items()
    }


def _identity_verified_native_keys(
    *,
    candidate_keys: Iterable[tuple[str, str]],
    inventory_channels: Mapping[tuple[str, str], Mapping[str, Any]],
    validation: NativeValidation,
) -> frozenset[tuple[str, str]]:
    """Bind verified native schedules to one unambiguous station identity."""

    candidates = frozenset(candidate_keys)
    requested_ids = frozenset(
        streaming.clean_identifier(
            inventory_channels[key].get("epg_channel_id", ""), 300
        )
        for key in candidates
    )
    if (
        "" in requested_ids
        or int(validation.requested_ids) != len(requested_ids)
        or not validation.verified_ids.issubset(requested_ids)
    ):
        raise BacklogAnalysisError("Native EPG validation did not reconcile.")

    names_by_id = validation.display_names_by_id
    if not isinstance(names_by_id, Mapping):
        return frozenset()

    ids_by_normalized_name: dict[str, set[str]] = {}
    for epg_id in requested_ids:
        display_names = names_by_id.get(epg_id, ())
        if isinstance(display_names, (str, bytes)):
            continue
        for display_name in display_names:
            name_key = native_review.native_display_name_key(display_name)
            if name_key:
                ids_by_normalized_name.setdefault(name_key, set()).add(epg_id)

    verified: set[tuple[str, str]] = set()
    for key in candidates:
        channel = inventory_channels[key]
        epg_id = streaming.clean_identifier(channel.get("epg_channel_id", ""), 300)
        if epg_id not in validation.verified_ids:
            continue
        provider_name = streaming.clean_identifier(channel.get("name", ""), 300)
        provider_key = native_review.native_display_name_key(provider_name)
        display_names = names_by_id.get(epg_id, ())
        if (
            provider_key
            and not isinstance(display_names, (str, bytes))
            and native_review.native_names_compatible(provider_name, display_names)
            and ids_by_normalized_name.get(provider_key) == {epg_id}
        ):
            verified.add(key)
    return frozenset(verified)


def run_epgshare_analysis(
    *,
    table: sync.MappingTable,
    inventories: Sequence[sync.PanelInventory],
    changed_rows: Sequence[Mapping[str, str]],
    quarantined_keys: frozenset[tuple[str, str]],
    all_source_file: Path,
    all_source_catalog_file: Path,
    spool_out: Path,
    generated_at: str,
) -> tuple[
    frozenset[tuple[str, str]],
    frozenset[tuple[str, str]],
    frozenset[tuple[str, str]],
    dict[str, int | str],
]:
    changed_keys = frozenset(
        _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        for row in changed_rows
    )
    evidence_excluded_keys = quarantined_keys.union(changed_keys)
    selected: list[dict[str, str]] = []
    selection_keys: set[tuple[str, str]] = set()
    for server_id in SUPPORTED_SERVERS:
        rows, _summary = sync.select_review_recheck_rows(
            table,
            inventories,
            selected_servers=(server_id,),
            quarantined_keys=evidence_excluded_keys,
            changed_rows=changed_rows,
        )
        for row in rows:
            key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
            if key in selection_keys:
                raise BacklogAnalysisError("The matcher eligibility set contains duplicates.")
            selection_keys.add(key)
            selected.append(row)
    if len(selected) > MAX_ANALYSIS_REVIEW_ROWS:
        raise BacklogAnalysisError("The matcher eligibility set is unexpectedly large.")

    try:
        outcome = automatch.auto_match_and_spool(
            mapping_rows=table.rows,
            inventories=inventories,
            new_rows=(),
            review_rows=selected,
            quarantined_keys=evidence_excluded_keys,
            all_source_file=Path(all_source_file),
            all_source_catalog_file=Path(all_source_catalog_file),
            spool_out=Path(spool_out),
            generated_at=generated_at,
            enable_ai_review=False,
        )
    except automatch.AutoMatchError as exc:
        raise BacklogAnalysisError(
            "The sealed EPGShare matching pass failed safely."
        ) from exc
    finally:
        # The spool contains private programme/channel details and is never a
        # public artifact.  It is unnecessary after aggregate decisions exist.
        Path(spool_out).unlink(missing_ok=True)

    approved: set[tuple[str, str]] = set()
    for row in outcome.rows:
        key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        if (
            row.get("enabled") == "TRUE"
            and streaming.clean_text(row.get("action", ""), 40).upper()
            == "AUTO_EPGSHARE"
            and streaming.clean_text(row.get("source", ""), 40).casefold()
            == "epgshare01"
        ):
            approved.add(key)
    catalog = {
        "alignment_mode": outcome.catalog_corroboration_mode,
        "confirmed_ids": int(outcome.corroborated_catalog_channels),
        "xml_only_ids": int(outcome.xml_only_catalog_channels),
        "text_only_ids": int(outcome.text_only_catalog_channels),
    }
    return (
        frozenset(selection_keys),
        frozenset(approved),
        frozenset(outcome.verified_placeholder_keys).intersection(selection_keys),
        catalog,
    )


def _server_template(*, native_source_status: str) -> dict[str, int | bool | str]:
    return {
        "provider_available": True,
        "provider_channels": 0,
        "review_rows": 0,
        "review_clusters": 0,
        "safety_blocked": 0,
        "not_eligible": 0,
        "analysis_eligible": 0,
        "matcher_eligible": 0,
        "native_candidates": 0,
        "native_source_status": native_source_status,
        "native_verified": 0,
        "strict_epgshare": 0,
        "verified_placeholder": 0,
        "no_verified_candidate": 0,
    }


def build_public_summary(
    *,
    generated_at: str,
    inventories: Sequence[sync.PanelInventory],
    review_items: Sequence[ReviewItem],
    analysis_eligible: frozenset[tuple[str, str]],
    matcher_eligible: frozenset[tuple[str, str]],
    native_candidates: Mapping[str, frozenset[tuple[str, str]]],
    native_verified: Mapping[str, frozenset[tuple[str, str]]],
    native_status: Mapping[str, str],
    strict_epgshare: frozenset[tuple[str, str]],
    verified_placeholders: frozenset[tuple[str, str]],
    catalog: Mapping[str, int | str],
) -> dict[str, Any]:
    provider_counts = {
        streaming.normalize_server_id(inventory.server_id): len(inventory.channels)
        for inventory in inventories
    }
    servers = {
        server_id: _server_template(
            native_source_status=(
                "forbidden"
                if server_id == "server_1"
                else str(native_status.get(server_id, "unavailable"))
            )
        )
        for server_id in SUPPORTED_SERVERS
    }
    review_clusters: set[tuple[Any, ...]] = set()
    for item in review_items:
        review_clusters.add(item.cluster_key)
        stats = servers[item.server_id]
        stats["review_rows"] = int(stats["review_rows"]) + 1
        key = item.key
        if item.safety_blocked:
            lane = "safety_blocked"
        elif key in strict_epgshare:
            lane = "strict_epgshare"
        elif key in native_verified.get(item.server_id, frozenset()):
            lane = "native_verified"
        elif key in verified_placeholders:
            lane = "verified_placeholder"
        elif key not in analysis_eligible:
            lane = "not_eligible"
        else:
            lane = "no_verified_candidate"
        stats[lane] = int(stats[lane]) + 1

    for server_id in SUPPORTED_SERVERS:
        stats = servers[server_id]
        stats["provider_channels"] = int(provider_counts.get(server_id, 0))
        stats["provider_available"] = server_id in provider_counts
        stats["review_clusters"] = len(
            {item.cluster_key for item in review_items if item.server_id == server_id}
        )
        stats["analysis_eligible"] = sum(
            1 for key in analysis_eligible if key[0] == server_id
        )
        stats["matcher_eligible"] = sum(
            1 for key in matcher_eligible if key[0] == server_id
        )
        stats["native_candidates"] = len(native_candidates.get(server_id, frozenset()))

    totals: dict[str, int] = {
        "provider_channels": sum(int(servers[s]["provider_channels"]) for s in SUPPORTED_SERVERS),
        "review_rows": len(review_items),
        "review_clusters": len(review_clusters),
        "safety_blocked": sum(int(servers[s]["safety_blocked"]) for s in SUPPORTED_SERVERS),
        "not_eligible": sum(int(servers[s]["not_eligible"]) for s in SUPPORTED_SERVERS),
        "analysis_eligible": len(analysis_eligible),
        "matcher_eligible": len(matcher_eligible),
        "native_candidates": sum(len(native_candidates.get(s, frozenset())) for s in SUPPORTED_SERVERS),
        "native_verified": sum(int(servers[s]["native_verified"]) for s in SUPPORTED_SERVERS),
        "strict_epgshare": sum(int(servers[s]["strict_epgshare"]) for s in SUPPORTED_SERVERS),
        "verified_placeholder": sum(int(servers[s]["verified_placeholder"]) for s in SUPPORTED_SERVERS),
        "no_verified_candidate": sum(int(servers[s]["no_verified_candidate"]) for s in SUPPORTED_SERVERS),
    }
    payload = {
        "schema_version": 1,
        "mode": "read-only",
        "generated_at": generated_at,
        "catalog": dict(catalog),
        "totals": totals,
        "servers": servers,
    }
    validate_public_summary(payload)
    return payload


def _public_integer(
    container: Mapping[str, Any], key: str, *, maximum: int
) -> int:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BacklogAnalysisError(f"Aggregate field {key} is not an integer.")
    if value < 0 or value > maximum:
        raise BacklogAnalysisError(f"Aggregate field {key} is out of range.")
    return value


def validate_public_summary(payload: object) -> dict[str, Any]:
    """Validate an exact count-only public schema before display or upload."""
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_PUBLIC_FIELDS:
        raise BacklogAnalysisError("The aggregate summary has an invalid top-level schema.")
    if payload.get("schema_version") != 1 or payload.get("mode") != "read-only":
        raise BacklogAnalysisError("The aggregate summary has an invalid safety mode.")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", generated_at
    ):
        raise BacklogAnalysisError("The aggregate summary timestamp is invalid.")

    catalog = payload.get("catalog")
    if not isinstance(catalog, dict) or set(catalog) != CATALOG_PUBLIC_FIELDS:
        raise BacklogAnalysisError("The aggregate catalog summary is invalid.")
    if catalog.get("alignment_mode") not in {"exact", "bounded-drift"}:
        raise BacklogAnalysisError("The aggregate catalog mode is invalid.")
    catalog_values = {
        key: _public_integer(catalog, key, maximum=MAX_PUBLIC_CATALOG_COUNT)
        for key in CATALOG_PUBLIC_FIELDS - {"alignment_mode"}
    }
    drift = catalog_values["xml_only_ids"] + catalog_values["text_only_ids"]
    if catalog_values["confirmed_ids"] < automatch.MINIMUM_CORROBORATED_CATALOG_IDS:
        raise BacklogAnalysisError("The aggregate catalog completeness is invalid.")
    if (catalog["alignment_mode"] == "exact") != (drift == 0):
        raise BacklogAnalysisError("The aggregate catalog alignment is inconsistent.")
    union_count = catalog_values["confirmed_ids"] + drift
    if (
        drift > automatch.MAX_CATALOG_ID_DRIFT
        or drift * automatch.MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR > union_count
        or catalog_values["confirmed_ids"] + catalog_values["xml_only_ids"]
        > catalog_stream.MAX_CATALOG_CHANNEL_ELEMENTS
        or catalog_values["confirmed_ids"] + catalog_values["text_only_ids"]
        > catalog_stream.MAX_CATALOG_CHANNEL_ELEMENTS
    ):
        raise BacklogAnalysisError("The aggregate catalog drift is inconsistent.")

    totals = payload.get("totals")
    if not isinstance(totals, dict) or set(totals) != TOTAL_PUBLIC_FIELDS:
        raise BacklogAnalysisError("The aggregate totals schema is invalid.")
    total_values = {
        key: _public_integer(
            totals,
            key,
            maximum=(
                MAX_PUBLIC_TOTAL_COUNT
                if key == "provider_channels"
                else MAX_ANALYSIS_REVIEW_ROWS
            ),
        )
        for key in TOTAL_PUBLIC_FIELDS
    }

    servers = payload.get("servers")
    if not isinstance(servers, dict) or set(servers) != set(SUPPORTED_SERVERS):
        raise BacklogAnalysisError("The aggregate server schema is invalid.")
    summed = defaultdict(int)
    for server_id in SUPPORTED_SERVERS:
        stats = servers.get(server_id)
        if not isinstance(stats, dict) or set(stats) != SERVER_PUBLIC_FIELDS:
            raise BacklogAnalysisError("An aggregate server row has an invalid schema.")
        if not isinstance(stats.get("provider_available"), bool):
            raise BacklogAnalysisError(
                "Aggregate server field provider_available is not boolean."
            )
        native_status = stats.get("native_source_status")
        if native_status not in {
            "available", "unavailable", "not-checked", "forbidden"
        }:
            raise BacklogAnalysisError("Aggregate native source status is invalid.")
        values = {
            key: _public_integer(
                stats,
                key,
                maximum=(
                    MAX_PUBLIC_SERVER_COUNT
                    if key == "provider_channels"
                    else MAX_ANALYSIS_REVIEW_ROWS
                ),
            )
            for key in SERVER_PUBLIC_FIELDS
            if key not in {"provider_available", "native_source_status"}
        }
        if not stats["provider_available"] or values["provider_channels"] == 0:
            raise BacklogAnalysisError("A required provider aggregate is unavailable.")
        lanes = (
            values["safety_blocked"]
            + values["not_eligible"]
            + values["native_verified"]
            + values["strict_epgshare"]
            + values["verified_placeholder"]
            + values["no_verified_candidate"]
        )
        if lanes != values["review_rows"]:
            raise BacklogAnalysisError("Aggregate review lanes do not reconcile.")
        if values["review_rows"] > values["provider_channels"]:
            raise BacklogAnalysisError("Aggregate review rows exceed provider rows.")
        if values["review_clusters"] > values["review_rows"]:
            raise BacklogAnalysisError("Aggregate cluster counts do not reconcile.")
        if (values["review_rows"] == 0) != (values["review_clusters"] == 0):
            raise BacklogAnalysisError("Aggregate cluster coverage is inconsistent.")
        if (
            values["analysis_eligible"]
            + values["safety_blocked"]
            + values["not_eligible"]
            != values["review_rows"]
        ):
            raise BacklogAnalysisError("Aggregate analysis counts do not reconcile.")
        if values["matcher_eligible"] > values["analysis_eligible"]:
            raise BacklogAnalysisError("Aggregate matcher counts do not reconcile.")
        if values["native_candidates"] > values["analysis_eligible"]:
            raise BacklogAnalysisError("Aggregate native-candidate counts do not reconcile.")
        if values["analysis_eligible"] > (
            values["matcher_eligible"] + values["native_candidates"]
        ):
            raise BacklogAnalysisError("Aggregate analysis eligibility is inconsistent.")
        if values["native_verified"] > values["native_candidates"]:
            raise BacklogAnalysisError("Aggregate native verification is inconsistent.")
        actionable_lanes = (
            values["native_verified"]
            + values["strict_epgshare"]
            + values["verified_placeholder"]
        )
        if actionable_lanes + values["no_verified_candidate"] != values["analysis_eligible"]:
            raise BacklogAnalysisError("Aggregate candidate lanes are inconsistent.")
        if values["strict_epgshare"] + values["verified_placeholder"] > values["matcher_eligible"]:
            raise BacklogAnalysisError("Aggregate Smart Rules lanes are inconsistent.")
        if server_id == "server_1" and (
            values["native_candidates"] or values["native_verified"]
            or native_status != "forbidden"
        ):
            raise BacklogAnalysisError("Server 1 native EPG policy was violated.")
        if server_id != "server_1":
            if values["native_candidates"] == 0 and native_status != "not-checked":
                raise BacklogAnalysisError("Aggregate native source status is inconsistent.")
            if values["native_candidates"] > 0 and native_status not in {
                "available", "unavailable"
            }:
                raise BacklogAnalysisError("Aggregate native source status is inconsistent.")
            if values["native_verified"] and native_status != "available":
                raise BacklogAnalysisError("Aggregate native source status is inconsistent.")
        for key, value in values.items():
            summed[key] += value

    for key in (
        "provider_channels",
        "review_rows",
        "safety_blocked",
        "not_eligible",
        "analysis_eligible",
        "matcher_eligible",
        "native_candidates",
        "native_verified",
        "strict_epgshare",
        "verified_placeholder",
        "no_verified_candidate",
    ):
        if total_values[key] != summed[key]:
            raise BacklogAnalysisError("Aggregate server and total counts do not reconcile.")
    total_lanes = sum(
        total_values[key]
        for key in (
            "safety_blocked",
            "not_eligible",
            "native_verified",
            "strict_epgshare",
            "verified_placeholder",
            "no_verified_candidate",
        )
    )
    if total_lanes != total_values["review_rows"]:
        raise BacklogAnalysisError("Aggregate total review lanes do not reconcile.")
    if total_values["analysis_eligible"] > (
        total_values["matcher_eligible"] + total_values["native_candidates"]
    ):
        raise BacklogAnalysisError("Aggregate total analysis eligibility is inconsistent.")
    if total_values["review_clusters"] > total_values["review_rows"]:
        raise BacklogAnalysisError("Aggregate total cluster counts do not reconcile.")
    if (total_values["review_rows"] == 0) != (
        total_values["review_clusters"] == 0
    ):
        raise BacklogAnalysisError("Aggregate total cluster coverage is inconsistent.")
    server_cluster_sum = sum(
        int(servers[server_id]["review_clusters"])
        for server_id in SUPPORTED_SERVERS
    )
    if total_values["review_clusters"] != server_cluster_sum:
        raise BacklogAnalysisError("Aggregate server-local cluster totals are inconsistent.")
    return payload


def validate_public_summary_file(path: Path) -> dict[str, Any]:
    summary_path = Path(path)
    try:
        mode = summary_path.lstat().st_mode
        size = summary_path.stat().st_size
    except OSError as exc:
        raise BacklogAnalysisError("The aggregate summary file is unavailable.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or not 1 <= size <= MAX_PUBLIC_SUMMARY_BYTES:
        raise BacklogAnalysisError("The aggregate summary file is invalid.")
    if any(item.name != summary_path.name for item in summary_path.parent.iterdir()):
        raise BacklogAnalysisError("The aggregate output directory contains private files.")
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BacklogAnalysisError("The aggregate summary file is malformed.") from exc
    return validate_public_summary(payload)


def _write_public_summary(path: Path, payload: Mapping[str, Any]) -> None:
    validate_public_summary(dict(payload))
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_PUBLIC_SUMMARY_BYTES:
        raise BacklogAnalysisError("The aggregate summary exceeds its size limit.")
    try:
        sync.atomic_write_bytes(path, encoded)
    except OSError as exc:
        raise BacklogAnalysisError("The aggregate summary could not be written.") from exc


def _prepare_public_output(output_dir: Path) -> Path:
    directory = Path(output_dir)
    if directory.is_symlink():
        raise BacklogAnalysisError("The aggregate output directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / "summary.json"
    summary_path.unlink(missing_ok=True)
    for item in directory.iterdir():
        if item.name != summary_path.name:
            raise BacklogAnalysisError(
                "The aggregate output directory must contain no private files."
            )
    return summary_path


def _parse_generated_epoch(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacklogAnalysisError("The analyzer timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _alerts_fingerprint(alerts: Sequence[Mapping[str, str]]) -> str:
    """Fingerprint the exact parsed alert snapshot without exposing its values."""
    digest = hashlib.sha256()
    digest.update(b"skytv-sync-alerts-v1\0")
    for row in alerts:
        encoded = json.dumps(
            {str(key): str(value) for key, value in row.items()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _load_sheet_snapshot(
    args: argparse.Namespace,
) -> tuple[sync.MappingTable, list[dict[str, str]]]:
    google_session = _authorized_readonly_google_session(
        os.environ.get("GOOGLE_SHEETS_READONLY_TOKEN", "")
    )
    try:
        sheet_id = sync.validate_sheet_id(args.sheet_id)
        table = sync.parse_table_values(
            sync.google_sheet_values(google_session, sheet_id, args.sheet_tab),
            maximum_rows=sync.MAX_GOOGLE_MAPPING_ROWS,
        )
        alerts = sync.parse_sync_alert_values(
            sync.google_sync_alert_values(google_session, sheet_id, args.alerts_tab)
        )
    except (sync.SyncError, streaming.BuildError, ValueError, TypeError) as exc:
        raise BacklogAnalysisError(
            "The private Google Sheet snapshot could not be read safely."
        ) from exc
    finally:
        close = getattr(google_session, "close", None)
        if callable(close):
            close()
    return table, alerts


def _assert_sheet_snapshot_unchanged(
    args: argparse.Namespace,
    *,
    initial_table: sync.MappingTable,
    initial_alerts: Sequence[Mapping[str, str]],
) -> None:
    terminal_table, terminal_alerts = _load_sheet_snapshot(args)
    if (
        sync.mapping_table_fingerprint(terminal_table)
        != sync.mapping_table_fingerprint(initial_table)
        or _alerts_fingerprint(terminal_alerts) != _alerts_fingerprint(initial_alerts)
    ):
        raise BacklogAnalysisError(
            "The private Google Sheet changed during analysis; rerun the analyzer."
        )


def _load_live_inputs(
    args: argparse.Namespace,
) -> tuple[
    sync.MappingTable,
    list[dict[str, str]],
    list[sync.ServerConfig],
    list[sync.PanelInventory],
]:
    table, alerts = _load_sheet_snapshot(args)

    configs = sync.load_server_configs(SUPPORTED_SERVERS)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"SKYTV-Backlog-Analyzer/{ANALYZER_VERSION}",
            "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
        }
    )
    inventories: list[sync.PanelInventory] = []
    try:
        for config in configs:
            inventory = sync.fetch_panel_inventory(
                session, config, allow_insecure_http=bool(args.allow_insecure_http)
            )
            sync.validate_provider_inventory_secret_safe(
                inventory.categories,
                inventory.channels,
                sync.provider_reflection_needles(config, [config.base_url]),
            )
            inventories.append(inventory)
        inventories, _native_hint_summary = sync.enrich_review_inventories_from_m3u(
            session,
            inventories,
            configs,
            selected_servers=("server_2", "server_3"),
            allow_insecure_http=bool(args.allow_insecure_http),
        )
    except sync.SyncError as exc:
        raise BacklogAnalysisError(
            "A provider inventory was unavailable or failed validation."
        ) from exc
    finally:
        session.close()
    return table, alerts, configs, inventories


def _authorized_readonly_google_session(access_token: str) -> requests.Session:
    """Use only a pre-minted read-only token; the private key stays out-of-process."""
    token = str(access_token or "").strip()
    if (
        not 20 <= len(token) <= 4096
        or any(character.isspace() for character in token)
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
    ):
        raise BacklogAnalysisError("The read-only Google Sheets token is missing or invalid.")
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"SKYTV-Backlog-Analyzer/{ANALYZER_VERSION}",
        }
    )
    return session


def run_analysis(args: argparse.Namespace) -> dict[str, Any]:
    summary_path = _prepare_public_output(Path(args.output_dir))
    private_dir = Path(args.private_work_dir)
    if private_dir.is_symlink():
        raise BacklogAnalysisError("The private analyzer work directory is invalid.")
    private_dir.mkdir(parents=True, exist_ok=True)
    if summary_path.resolve().is_relative_to(private_dir.resolve()) or private_dir.resolve().is_relative_to(summary_path.parent.resolve()):
        raise BacklogAnalysisError("Public and private analyzer paths must be separate.")

    generated_at = sync.generated_timestamp(args.now_utc)
    now_epoch = _parse_generated_epoch(generated_at)
    os.environ["ALLOW_INSECURE_PANEL_HTTP"] = (
        "true" if args.allow_insecure_http else "false"
    )
    table, alerts, _configs, inventories = _load_live_inputs(args)
    minimums = sync.parse_server_minimums(args.minimum_server_channels)
    counts = {
        streaming.normalize_server_id(inventory.server_id): len(inventory.channels)
        for inventory in inventories
    }
    if set(counts) != set(SUPPORTED_SERVERS) or any(
        counts.get(server_id, 0) < minimums.get(server_id, 1)
        for server_id in SUPPORTED_SERVERS
    ):
        raise BacklogAnalysisError(
            "Provider inventories failed the configured completeness floors."
        )

    discovered, changed_rows, _missing = sync.compare_inventory(
        table, inventories, discovered_at=generated_at
    )
    overlap = sync.inventory_overlap_issues(table, inventories)
    if overlap:
        raise BacklogAnalysisError("Provider inventory overlap checks failed.")
    # New identities are review work too, but normal Workflow 1 must append
    # them first.  The current production queue is expected to be fully listed.
    if discovered:
        raise BacklogAnalysisError(
            "Untracked provider channels exist; run Workflow 1 before backlog analysis."
        )
    persistent = sync.open_alert_quarantine_keys(alerts)
    quarantined = frozenset(sync.snapshot_quarantine_keys(changed_rows, persistent))
    changed_keys = frozenset(
        _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        for row in changed_rows
    )
    cluster_keys = build_cluster_keys(inventories)
    review_items = collect_review_items(
        table=table,
        inventories=inventories,
        cluster_keys=cluster_keys,
        quarantined_keys=quarantined,
    )

    # Workflow 1's selector remains the single authority for automatic
    # EPGShare decisions. Native-panel validation is a separate read-only lane
    # restricted to exact, disabled, auto-discovered REVIEW rows.
    matcher_eligible, strict_matches, placeholders, catalog = run_epgshare_analysis(
        table=table,
        inventories=inventories,
        changed_rows=changed_rows,
        quarantined_keys=quarantined,
        all_source_file=Path(args.all_source_file),
        all_source_catalog_file=Path(args.all_source_catalog_file),
        spool_out=private_dir / "selected_epg.sqlite3",
        generated_at=generated_at,
    )
    inventory_channels = _inventory_channels_by_key(inventories)
    native_candidate_keys = select_native_advisory_candidates(
        review_items=review_items,
        inventory_channels=inventory_channels,
        changed_keys=changed_keys,
    )
    # Match Workflow 1's precedence exactly: a verified EPGShare result wins
    # and is never also counted, validated, or written through KEEP_PANEL.
    native_candidate_keys = {
        server_id: frozenset(keys).difference(strict_matches)
        for server_id, keys in native_candidate_keys.items()
    }
    native_verified_keys: dict[str, frozenset[tuple[str, str]]] = {
        "server_1": frozenset()
    }
    native_status: dict[str, str] = {"server_1": "forbidden"}
    items_by_key = {item.key: item for item in review_items}
    for server_id in ("server_2", "server_3"):
        candidates = native_candidate_keys[server_id]
        if not candidates:
            native_status[server_id] = "not-checked"
            native_verified_keys[server_id] = frozenset()
            continue
        requested_ids = frozenset(
            streaming.clean_identifier(
                inventory_channels[key].get("epg_channel_id", ""), 300
            )
            for key in candidates
        )
        try:
            panel_path, _details = streaming.download_panel_xmltv(
                server_id, private_dir / f"{server_id}_panel.xmltv"
            )
            validation = validate_native_xmltv(
                panel_path,
                server_id=server_id,
                requested_ids=requested_ids,
                now_epoch=now_epoch,
            )
            native_status[server_id] = "available"
            native_verified_keys[server_id] = _identity_verified_native_keys(
                candidate_keys=candidates,
                inventory_channels=inventory_channels,
                validation=validation,
            )
        except (BacklogAnalysisError, streaming.BuildError):
            # Native EPG is an optional lane.  A fixed status records its
            # unavailability without reflecting a provider response or URL.
            native_status[server_id] = "unavailable"
            native_verified_keys[server_id] = frozenset()
            print(f"Native EPG validation unavailable for {server_id}.", flush=True)
        finally:
            (private_dir / f"{server_id}_panel.xmltv").unlink(missing_ok=True)

    analysis_eligible = frozenset(matcher_eligible).union(
        *(native_candidate_keys[server_id] for server_id in SUPPORTED_SERVERS)
    )

    # Defensive intersection: every decision must refer to the exact review
    # population measured in this run, and each verification result must stay
    # inside the eligibility boundary that produced it.
    review_keys = frozenset(items_by_key)
    if not (
        matcher_eligible.issubset(review_keys)
        and analysis_eligible.issubset(review_keys)
        and strict_matches.issubset(matcher_eligible)
        and placeholders.issubset(matcher_eligible)
        and not strict_matches.intersection(
            *(native_candidate_keys[server_id] for server_id in SUPPORTED_SERVERS)
        )
        and all(values.issubset(review_keys) for values in native_candidate_keys.values())
        and all(
            native_verified_keys[server_id].issubset(native_candidate_keys[server_id])
            for server_id in SUPPORTED_SERVERS
        )
    ):
        raise BacklogAnalysisError("Analyzer decision sets did not reconcile.")

    payload = build_public_summary(
        generated_at=generated_at,
        inventories=inventories,
        review_items=review_items,
        analysis_eligible=analysis_eligible,
        matcher_eligible=matcher_eligible,
        native_candidates=native_candidate_keys,
        native_verified=native_verified_keys,
        native_status=native_status,
        strict_epgshare=strict_matches,
        verified_placeholders=placeholders,
        catalog=catalog,
    )
    _assert_sheet_snapshot_unchanged(
        args,
        initial_table=table,
        initial_alerts=alerts,
    )
    _write_public_summary(summary_path, payload)
    validate_public_summary_file(summary_path)
    print(
        "Backlog analysis complete: "
        f"{payload['totals']['review_rows']:,} review rows in "
        f"{payload['totals']['review_clusters']:,} conservative clusters; "
        f"{payload['totals']['no_verified_candidate']:,} eligible rows have no "
        "verified candidate.",
        flush=True,
    )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", ""))
    parser.add_argument("--sheet-tab", default=os.environ.get("GOOGLE_SHEET_TAB", "Mappings"))
    parser.add_argument(
        "--alerts-tab", default=os.environ.get("GOOGLE_SYNC_ALERTS_TAB", "Sync Alerts")
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".build/backlog-analysis")
    )
    parser.add_argument(
        "--private-work-dir", type=Path, required=True,
        help="Ephemeral non-artifact directory for XMLTV and SQLite files.",
    )
    parser.add_argument("--all-source-file", type=Path, required=True)
    parser.add_argument("--all-source-catalog-file", type=Path, required=True)
    parser.add_argument(
        "--minimum-server-channels", nargs="*", default=[], metavar="SERVER=COUNT"
    )
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--now-utc", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_analysis(args)
    return 0


def _public_error_message(error: BaseException) -> str:
    if isinstance(error, BacklogAnalysisError):
        return str(error)
    return "Backlog analysis stopped safely before publishing a report."


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BacklogAnalysisError as exc:
        print(f"ERROR: {_public_error_message(exc)}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        # Upstream parser/network exceptions may contain a private stream ID,
        # URL or provider response. Never echo their text to public Actions
        # logs; the controlled exceptions above are the only allowlisted text.
        print(
            f"ERROR: {_public_error_message(exc)}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = [
    "ANALYZER_VERSION",
    "BacklogAnalysisError",
    "NativeValidation",
    "ReviewItem",
    "build_cluster_keys",
    "build_public_summary",
    "collect_review_items",
    "main",
    "run_analysis",
    "select_native_advisory_candidates",
    "validate_native_xmltv",
    "validate_public_summary",
    "validate_public_summary_file",
]
