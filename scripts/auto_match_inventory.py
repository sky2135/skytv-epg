#!/usr/bin/env python3
"""Strict Version 1 auto-matching and one-pass EPGShare spool orchestration.

This module deliberately has no Google Sheets or provider-network code.  It
receives an already validated mapping table and provider inventories, proposes
matches for newly discovered identities and an explicit allowlist of existing
disabled REVIEW identities, validates those proposals against programmes from
the same ALL_SOURCES1 byte snapshot, and seals the selected rows for the
production builder.  The caller may perform external writes only after this
function returns successfully.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from rapidfuzz import fuzz


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
SRC_DIR = REPOSITORY_ROOT / "src"
for import_path in (SCRIPT_DIR, SRC_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import build_epg_streaming as streaming  # noqa: E402
import epg_catalog_stream as catalog_stream  # noqa: E402
from epg_selection_spool import EpgSelectionSpoolWriter, SpoolError  # noqa: E402
from skytv_epg_auto_match_v1 import (  # noqa: E402
    CatalogSnapshot as MatcherCatalogSnapshot,
    DUMMY_REVIEW_METHODS,
    MatcherIdentity,
    MatcherPreflight,
    ScheduleEvidence,
    _has_adult_evidence as _matcher_has_adult_evidence,
    _is_generic_numbered_or_blank as _matcher_is_generic_numbered_or_blank,
    _market_code as _matcher_market_code,
    finalize_proposal,
    prepare_resolver_strict,
    propose_new_channel_matches,
)
from skytv_epg_contextual_v8 import (  # noqa: E402
    install_contextual_v8,
    parse_channel_context_v8,
)


AUTO_MATCH_INTEGRATION_VERSION = "1.2"
SUPPORTED_SERVERS = frozenset({"server_1", "server_2", "server_3"})
DEFAULT_EPG_HISTORY_DAYS = 3
MINIMUM_CORROBORATED_CATALOG_IDS = 25_000
# EPGShare publishes the large XML guide and its companion text catalog as
# separate files. Their replacement is not atomic, so a small number of IDs
# can legitimately differ while one file is newer than the other. Automatic
# matching may use only their exact, case-sensitive intersection. These two
# independent ceilings keep that availability allowance from accepting a
# stale, partial, or unrelated catalog pair.
MAX_CATALOG_ID_DRIFT = 64
# At most one drifting ID per 400 union IDs (0.25%). Keeping this as an
# integer denominator makes the boundary exact and deterministic.
MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR = 400
SOURCE_BASE_URL = "https://epgshare01.online/epgshare01/"
ENGINE_SOURCE_PATH = SRC_DIR / "skytv_epg_engine.py"
CONTEXTUAL_SOURCE_PATH = SRC_DIR / "skytv_epg_contextual_v8.py"
APPROVED_ALIASES_PATH = REPOSITORY_ROOT / "knowledge" / "approved_channel_aliases.csv"
SCHEDULE_EQUIVALENCES_PATH = (
    REPOSITORY_ROOT / "knowledge" / "schedule_equivalence_groups.json"
)
APPROVED_ALIASES_SHA256 = (
    "f99a1d53ecfb2487d441aabe720f62f458299751de49f2d63b779bdf4c5b83e1"
)
SCHEDULE_EQUIVALENCES_SHA256 = (
    "a0de16a12d00d6aca96c7f7a54ed444a47467115266f467521880cc8a56b6edc"
)
LARGE_BATCH_THRESHOLD = 100
MAX_LARGE_BATCH_APPROVAL_FRACTION = 0.35
MAX_LARGE_BATCH_APPROVALS = 5_000
MAX_LARGE_BATCH_APPROVALS_PER_SERVER = 2_500
MAX_AI_REVIEW_SHORTLISTS = 50
MAX_AI_REVIEW_ATTEMPTED_ROWS = 250
MAX_AI_REVIEW_COMPARISONS = 1_000_000
MAX_AI_REVIEW_ROTATION = 1_000_000
MIN_AI_REVIEW_CANDIDATES = 2
MAX_AI_REVIEW_CANDIDATES = 8
MIN_AI_REVIEW_FUZZY_SCORE = 55.0
_METADATA_COLUMNS = frozenset(
    {
        "region_code",
        "genre",
        "primary_language",
        "country_codes",
        "language_codes",
        "subgenres",
        "sport_codes",
        "religion_codes",
        "audience_codes",
        "content_rating",
        "channel_role",
        "tags",
        "metadata_status",
        "metadata_source",
        "metadata_confidence",
        "metadata_locked",
    }
)


class AutoMatchError(RuntimeError):
    """A controlled integration failure safe to surface in workflow logs."""


@dataclass(frozen=True)
class MatcherRuntime:
    resolver: Any
    identity: MatcherIdentity
    preflight: MatcherPreflight
    approved_aliases_sha256: str
    schedule_equivalences_sha256: str


@dataclass(frozen=True)
class _CatalogCorroboration:
    exact_ids: frozenset[str]
    xml_only_ids: frozenset[str]
    text_only_ids: frozenset[str]
    union_casefold_collision_keys: frozenset[str]
    mode: str
    drift_sha256: str


@dataclass(frozen=True, slots=True)
class _LearnedAliasMemory:
    """Run-local, catalog-revalidated knowledge learned from human approvals."""

    evidence_rows: int
    considered_groups: int
    registered_aliases: int
    static_reused_groups: int
    rejected_groups: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AiReviewCandidate:
    """One exact real EPGShare identity offered for suggestion-only review."""

    candidate_key: str
    epg_id: str
    display_name: str
    feed: str
    region: str
    local_score: int


@dataclass(frozen=True, slots=True)
class AiReviewShortlist:
    """A bounded, programme-verified candidate set for one unresolved row."""

    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    market: str
    candidates: tuple[AiReviewCandidate, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True, slots=True)
class _AiReviewStaging:
    """Bounded shortlist output plus exact fuzzy-work counters."""

    shortlists: tuple[AiReviewShortlist, ...]
    attempted_rows: int
    comparisons: int


@dataclass(frozen=True)
class AutoMatchOutcome:
    rows: tuple[dict[str, str], ...]
    considered_rows: int
    provisional_rows: int
    approved_rows: int
    review_rows: int
    new_considered_rows: int
    new_provisional_rows: int
    new_approved_rows: int
    new_review_rows: int
    new_rejected_programme_gates: int
    recheck_considered_rows: int
    recheck_provisional_rows: int
    recheck_approved_rows: int
    recheck_review_rows: int
    recheck_rejected_programme_gates: int
    rejected_programme_gates: int
    fixed_requested_ids: int
    catalog_channels: int
    text_catalog_channels: int
    corroborated_catalog_channels: int
    xml_only_catalog_channels: int
    text_only_catalog_channels: int
    catalog_drift_channels: int
    catalog_corroboration_mode: str
    catalog_drift_sha256: str
    source_sha256: str
    catalog_sha256: str
    text_catalog_generated_token: str
    text_catalog_fingerprint_sha256: str
    text_catalog_file_sha256: str
    matcher_version: str
    matcher_build_id: str
    matcher_sha256: str
    matcher_engine_sha256: str
    approved_aliases_sha256: str
    schedule_equivalences_sha256: str
    learned_alias_evidence_rows: int
    learned_alias_considered_groups: int
    learned_alias_registered: int
    learned_alias_static_reused_groups: int
    learned_alias_rejected_groups: int
    learned_alias_sha256: str
    ai_review_rotation: int
    ai_review_attempted_rows: int
    ai_review_comparisons: int
    ai_review_shortlists: tuple[AiReviewShortlist, ...]
    # Current-run, exact XML/TXT-corroborated dummy proposals.  These remain
    # REVIEW-only and are intentionally omitted from public sync summaries;
    # the read-only backlog analyzer uses the identities only in memory to
    # produce an aggregate count.
    verified_placeholder_keys: frozenset[tuple[str, str]]

    def summary_fields(self) -> dict[str, Any]:
        return {
            # Preserve the original public summary meaning: these fields refer
            # only to newly discovered rows. Existing REVIEW rechecks have
            # dedicated fields below so the two populations cannot be mixed.
            "auto_match_considered_rows": self.new_considered_rows,
            "auto_match_provisional_rows": self.new_provisional_rows,
            "auto_matched_rows": self.new_approved_rows,
            "auto_match_review_rows": self.new_review_rows,
            "review_recheck_considered_rows": self.recheck_considered_rows,
            "review_recheck_provisional_rows": self.recheck_provisional_rows,
            "review_recheck_safe_matches": self.recheck_approved_rows,
            "review_recheck_still_review_rows": self.recheck_review_rows,
            "review_recheck_rejected_programme_gates": (
                self.recheck_rejected_programme_gates
            ),
            "ai_review_attempted_rows": self.ai_review_attempted_rows,
            "ai_review_fuzzy_comparisons": self.ai_review_comparisons,
            "ai_review_rotation": self.ai_review_rotation,
            "ai_review_shortlisted_rows": len(self.ai_review_shortlists),
            "ai_review_shortlisted_candidates": sum(
                len(shortlist.candidates) for shortlist in self.ai_review_shortlists
            ),
            "cross_server_alias_evidence_rows": self.learned_alias_evidence_rows,
            "cross_server_alias_groups_considered": (
                self.learned_alias_considered_groups
            ),
            "cross_server_aliases_registered": self.learned_alias_registered,
            "cross_server_alias_static_reused_groups": (
                self.learned_alias_static_reused_groups
            ),
            "cross_server_alias_groups_rejected": (
                self.learned_alias_rejected_groups
            ),
            "cross_server_alias_memory_sha256": self.learned_alias_sha256,
            "auto_match_rejected_programme_gates": (
                self.new_rejected_programme_gates
            ),
            "epgshare_spool_fixed_ids": self.fixed_requested_ids,
            "epgshare_catalog_channels": self.catalog_channels,
            "epgshare_text_catalog_channels": self.text_catalog_channels,
            "epgshare_corroborated_catalog_channels": (
                self.corroborated_catalog_channels
            ),
            "epgshare_shared_catalog_channels": self.corroborated_catalog_channels,
            "epgshare_xml_only_catalog_channels": self.xml_only_catalog_channels,
            "epgshare_text_only_catalog_channels": self.text_only_catalog_channels,
            "epgshare_catalog_drift_channels": self.catalog_drift_channels,
            "epgshare_catalog_corroboration_mode": self.catalog_corroboration_mode,
            "epgshare_catalog_drift_sha256": self.catalog_drift_sha256,
            "epgshare_source_sha256": self.source_sha256,
            "epgshare_catalog_sha256": self.catalog_sha256,
            "epgshare_text_catalog_generated_token": self.text_catalog_generated_token,
            "epgshare_text_catalog_fingerprint_sha256": (
                self.text_catalog_fingerprint_sha256
            ),
            "epgshare_text_catalog_file_sha256": self.text_catalog_file_sha256,
            "auto_match_provenance": {
                "version": AUTO_MATCH_INTEGRATION_VERSION,
                "matcher_version": self.matcher_version,
                "matcher_build_id": self.matcher_build_id,
                "matcher_sha256": self.matcher_sha256,
                "matcher_engine_sha256": self.matcher_engine_sha256,
                "approved_aliases_sha256": self.approved_aliases_sha256,
                "schedule_equivalences_sha256": self.schedule_equivalences_sha256,
            },
        }


MatcherRuntimeFactory = Callable[
    [list[dict[str, str]], dict[str, str]], MatcherRuntime
]


def _catalog_drift_sha256(
    xml_only_ids: Iterable[str], text_only_ids: Iterable[str]
) -> str:
    """Fingerprint the exact directional difference without logging every ID."""

    digest = hashlib.sha256()
    for side, values in (
        (b"xml", xml_only_ids),
        (b"text", text_only_ids),
    ):
        for value in sorted(values, key=lambda item: (item.casefold(), item)):
            encoded = value.encode("utf-8")
            digest.update(side)
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _runtime_shadow_id(value: str) -> str:
    """Return the exact safe identity the frozen matcher can retain.

    The pinned engine repairs non-ASCII whitespace before indexing.  Supplying
    the original opaque ID would therefore fail strict live-catalog preflight.
    A shadow is deliberately non-approvable, so it may use that deterministic
    ASCII-space form solely to preserve ambiguity in the runtime competition.
    """

    return "".join(" " if character.isspace() else character for character in value)


def _strong_runtime_identity_keys(resolver: Any, context: Any) -> frozenset[tuple[str, str]]:
    """Return conservative exact-identity keys for cross-route ambiguity vetoes."""

    keys: set[tuple[str, str]] = set()

    def add(label: str, value: object, *, minimum_compact: int = 3) -> None:
        normalized = " ".join(str(value or "").casefold().split())
        if len("".join(character for character in normalized if character.isalnum())) >= minimum_compact:
            keys.add((label, normalized))

    # The frozen strict path accepts even one-character exact identities.
    add("strict", getattr(context, "strict_key", ""), minimum_compact=1)
    add("relaxed", getattr(context, "relaxed_key", ""))
    add("compact", getattr(context, "compact_key", ""), minimum_compact=5)
    add("edition", getattr(context, "edition_key", ""))
    add("bag", getattr(context, "bag_key", ""))
    for value in tuple(getattr(context, "identity_keys", ()) or ()):
        add("identity", value)

    candidate = dict(getattr(context, "candidate", {}) or {})
    callsign_parser = getattr(
        getattr(resolver, "engine", None), "_candidate_callsign_parts_v7", None
    )
    if not callable(callsign_parser):
        raise AutoMatchError(
            "The Version 1 matcher cross-route station parser is unavailable."
        )
    try:
        parts = callsign_parser(candidate)
        if parts is not None:
            base, _station_class, subchannel = parts
            base_text = str(base or "").upper()
            sub_text = str(subchannel or "")
            if base_text:
                keys.add(("callsign", f"{base_text}:{sub_text}"))
    except Exception as exc:
        raise AutoMatchError(
            "The Version 1 matcher could not verify cross-route station identities."
        ) from exc
    return frozenset(keys)


def _all_route_ambiguity_labels(
    runtime: MatcherRuntime,
) -> Mapping[str, frozenset[str]]:
    """Map routed candidates to identity labels shadowed in region ALL.

    The frozen resolver deliberately searches one explicit market. An official
    real candidate whose route is ``ALL`` would otherwise be invisible to that
    market's indexes and could manufacture false uniqueness. Such a candidate
    remains usable for exact existing mappings, but any colliding new-channel
    proposal must stay in REVIEW.
    """

    state = getattr(runtime.resolver, "state", None)
    by_region = getattr(state, "by_region", None)
    all_contexts = by_region.get("ALL") if isinstance(by_region, dict) else None
    if not isinstance(all_contexts, list):
        raise AutoMatchError("The Version 1 matcher cross-route index is unavailable.")

    blocker_keys: set[tuple[str, str]] = set()
    routed: list[tuple[str, frozenset[tuple[str, str]]]] = []
    for context in all_contexts:
        candidate = dict(getattr(context, "candidate", {}) or {})
        epg_id = str(candidate.get("epg_id") or "")
        region = str(candidate.get("region") or "").upper()
        if not epg_id or not region:
            raise AutoMatchError("The Version 1 matcher cross-route index is invalid.")
        keys = _strong_runtime_identity_keys(runtime.resolver, context)
        if region == "ALL":
            blocker_keys.update(keys)
        else:
            routed.append((epg_id, keys))
    if not blocker_keys:
        return {}
    return {
        epg_id: frozenset(label for label, _value in keys.intersection(blocker_keys))
        for epg_id, keys in routed
        if keys.intersection(blocker_keys)
    }


def _proposal_blocked_by_all_route(
    proposal: Any,
    ambiguity_labels: Mapping[str, frozenset[str]],
) -> bool:
    """Apply only the exact identity family used by an allowlisted method."""

    labels = ambiguity_labels.get(str(proposal.target_epg_id or ""), frozenset())
    method = str(proposal.match_method or "").casefold()
    if method == "verified_station_identity":
        return "callsign" in labels
    if method == "strict":
        return "strict" in labels
    if method == "approved_knowledge":
        # Approved aliases select a specific target, but a catalog identity
        # that is exact under any of the matcher's strong canonical forms is
        # still material competing evidence and requires human review.
        return bool(
            labels.intersection(
                {"callsign", "strict", "relaxed", "compact", "edition", "identity", "bag"}
            )
        )
    return False


def _corroborate_catalog_ids(
    xml_ids: Iterable[str],
    text_ids: Iterable[str],
    *,
    minimum_unique_channels: int,
) -> _CatalogCorroboration:
    """Accept only a small, bounded publication skew between official files.

    XMLTV IDs are opaque. The intersection therefore remains exact and
    case-sensitive: a capitalization-only rename is deliberately represented
    as one XML-only ID and one text-only ID, never silently reconciled.
    """

    xml_exact = frozenset(xml_ids)
    text_exact = frozenset(text_ids)
    minimum = int(minimum_unique_channels)
    if minimum < 1:
        raise catalog_stream.CatalogStreamError(
            "The catalog corroboration completeness floor is invalid."
        )
    if len(xml_exact) < minimum or len(text_exact) < minimum:
        raise catalog_stream.CatalogStreamError(
            "An ALL_SOURCES1 catalog is below its corroboration completeness floor."
        )

    exact_ids = xml_exact.intersection(text_exact)
    xml_only_ids = xml_exact.difference(text_exact)
    text_only_ids = text_exact.difference(xml_exact)
    if len(exact_ids) < minimum:
        raise catalog_stream.CatalogStreamError(
            "The XML/text exact-ID intersection is below its completeness floor "
            f"(xml={len(xml_exact):,}, text={len(text_exact):,}, "
            f"corroborated={len(exact_ids):,})."
        )

    drift_ids = xml_only_ids.union(text_only_ids)
    if any(
        not value.isascii()
        or not value.isprintable()
        or any(character.isspace() for character in value)
        for value in drift_ids
    ):
        raise catalog_stream.CatalogStreamError(
            "Bounded XML/text catalog drift contains a non-ASCII, whitespace, "
            "or control-character ID."
        )

    variants_by_fold: dict[str, set[str]] = {}
    for value in xml_exact.union(text_exact):
        variants_by_fold.setdefault(value.casefold(), set()).add(value)
    union_casefold_collision_keys = frozenset(
        folded for folded, variants in variants_by_fold.items() if len(variants) > 1
    )

    drift_count = len(drift_ids)
    union_count = len(xml_exact.union(text_exact))
    drift_sha256 = _catalog_drift_sha256(xml_only_ids, text_only_ids)
    if (
        drift_count > MAX_CATALOG_ID_DRIFT
        # Use integer arithmetic for the 0.25% production boundary so an
        # exactly-on-the-limit catalog cannot fail because of float rounding.
        or drift_count * MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR > union_count
    ):
        raise catalog_stream.CatalogStreamError(
            "The XML/text catalog ID drift exceeds its safety limit "
            f"(xml={len(xml_exact):,}, text={len(text_exact):,}, "
            f"corroborated={len(exact_ids):,}, xml_only={len(xml_only_ids):,}, "
            f"text_only={len(text_only_ids):,}, drift_sha256={drift_sha256})."
        )
    return _CatalogCorroboration(
        exact_ids=frozenset(exact_ids),
        xml_only_ids=frozenset(xml_only_ids),
        text_only_ids=frozenset(text_only_ids),
        union_casefold_collision_keys=union_casefold_collision_keys,
        mode="exact" if drift_count == 0 else "bounded-drift",
        drift_sha256=drift_sha256,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AutoMatchError("A required Version 1 matcher file is unavailable.") from exc
    return digest.hexdigest()


def _load_frozen_engine() -> Any:
    module_name = "_skytv_epg_frozen_engine_auto_match_v1"
    spec = importlib.util.spec_from_file_location(module_name, ENGINE_SOURCE_PATH)
    if spec is None or spec.loader is None:
        raise AutoMatchError("The frozen Version 1 matcher could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise AutoMatchError("The frozen Version 1 matcher could not be loaded.") from exc
    return module


def prepare_matcher_runtime(
    real_candidates: list[dict[str, str]], dummy_ids: dict[str, str]
) -> MatcherRuntime:
    """Load pinned code/knowledge and run the matcher against the live catalog."""
    for path in (
        ENGINE_SOURCE_PATH,
        CONTEXTUAL_SOURCE_PATH,
        APPROVED_ALIASES_PATH,
        SCHEDULE_EQUIVALENCES_PATH,
    ):
        if not path.is_file() or path.is_symlink():
            raise AutoMatchError("A required Version 1 matcher file is unavailable.")
    aliases_hash = _sha256(APPROVED_ALIASES_PATH)
    equivalences_hash = _sha256(SCHEDULE_EQUIVALENCES_PATH)
    if (
        aliases_hash != APPROVED_ALIASES_SHA256
        or equivalences_hash != SCHEDULE_EQUIVALENCES_SHA256
    ):
        raise AutoMatchError("The Version 1 matcher knowledge integrity check failed.")
    engine = _load_frozen_engine()
    try:
        resolver = install_contextual_v8(engine)
        resolver.load_approved_aliases(APPROVED_ALIASES_PATH)
        resolver.load_schedule_equivalences(SCHEDULE_EQUIVALENCES_PATH)
        identity = MatcherIdentity.from_resolver(
            resolver, CONTEXTUAL_SOURCE_PATH, ENGINE_SOURCE_PATH
        )
    except Exception as exc:
        raise AutoMatchError("The Version 1 matcher setup failed safely.") from exc
    if not identity.is_expected:
        raise AutoMatchError("The frozen Version 1 matcher integrity check failed.")
    preflight = prepare_resolver_strict(resolver, real_candidates, dummy_ids)
    if not preflight.ready:
        raise AutoMatchError("The Version 1 matcher self-test failed safely.")
    return MatcherRuntime(
        resolver=resolver,
        identity=identity,
        preflight=preflight,
        approved_aliases_sha256=aliases_hash,
        schedule_equivalences_sha256=equivalences_hash,
    )


def _canonical_key(server_id: object, stream_id: object) -> tuple[str, str]:
    try:
        server = streaming.normalize_server_id(server_id)
    except streaming.BuildError as exc:
        raise AutoMatchError("A channel inventory identity is invalid.") from exc
    stream = streaming.clean_identifier(stream_id, 120)
    if server not in SUPPORTED_SERVERS or not stream:
        raise AutoMatchError("A channel inventory identity is invalid.")
    return server, stream


def _mapping_is_enabled(row: Mapping[str, Any], action: str) -> bool:
    try:
        return streaming.parse_bool(
            row.get("enabled", ""),
            default=action not in {"SKIP", "IGNORE", "REJECTED"},
            field_name="mapping enabled",
        )
    except streaming.BuildError as exc:
        raise AutoMatchError("An existing mapping row has an invalid enabled value.") from exc


def active_combined_source_ids(rows: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Mirror the builder's runtime selection without materializing its model."""
    result: set[str] = set()
    for row in rows:
        try:
            server_id = streaming.normalize_server_id(row.get("server_id", ""))
        except streaming.BuildError as exc:
            raise AutoMatchError("An existing mapping row has an invalid server.") from exc
        action = streaming.clean_text(row.get("action", "APPROVED"), 40).upper() or "APPROVED"
        if action not in streaming.ALLOWED_ACTIONS:
            raise AutoMatchError("An existing mapping row has an invalid action.")
        if not _mapping_is_enabled(row, action) or action in streaming.REJECTED_ACTIONS:
            continue
        epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
        if not epg_id:
            # The builder will provide the row-level validation message later;
            # it is not a source request and therefore cannot enter this spool.
            continue
        try:
            requested_source = streaming.normalize_requested_source(
                str(row.get("source", "")),
                str(row.get("epg_feed", "")),
                row_number=0,
            )
        except streaming.BuildError as exc:
            raise AutoMatchError("An existing mapping row has an invalid EPG source.") from exc
        required_source = {
            "KEEP_PANEL": "panel",
            "AUTO_EPGSHARE": "epgshare01",
            "AUTO_DUMMY": "dummy",
        }.get(action)
        if required_source and requested_source != required_source:
            raise AutoMatchError("An existing mapping row has an invalid action/source pair.")
        if server_id == "server_1" and requested_source == "panel":
            continue
        if requested_source != "panel":
            result.add(epg_id)
    return frozenset(result)


def _safe_matcher_inventory(inventory: Any) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Copy only non-secret identity fields; never pass panel IDs or URLs."""
    categories: dict[str, str] = {}
    for raw in tuple(getattr(inventory, "categories", ()) or ()):
        category_id = streaming.clean_identifier(raw.get("category_id", ""), 120)
        if category_id:
            categories[category_id] = streaming.clean_text(
                raw.get("category_name", ""), 200
            )
    channels: list[dict[str, str]] = []
    for raw in tuple(getattr(inventory, "channels", ()) or ()):
        category_id = streaming.clean_identifier(raw.get("category_id", ""), 120)
        category_name = streaming.clean_text(raw.get("category_name", ""), 200)
        if category_id and category_id not in categories and category_name:
            categories[category_id] = category_name
        channels.append(
            {
                "stream_id": streaming.clean_identifier(raw.get("stream_id", ""), 120),
                "category_id": category_id,
                "name": streaming.clean_identifier(raw.get("name", ""), 300),
                # The adapter accepts this field, but production always supplies
                # a blank value so a native panel identity cannot drive an
                # unattended match on any server.
                "epg_channel_id": "",
            }
        )
    return channels, categories


def _timestamp_epoch(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AutoMatchError("The inventory timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = int(parsed.astimezone(timezone.utc).timestamp())
    if epoch <= 0:
        raise AutoMatchError("The inventory timestamp is invalid.")
    return epoch


def _validate_candidate_rows(
    *,
    new_rows: Sequence[Mapping[str, Any]],
    review_rows: Sequence[Mapping[str, Any]],
    existing_rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str], dict[str, str]],
    frozenset[tuple[str, str]],
    frozenset[tuple[str, str]],
]:
    """Validate the exact new/recheck identity allowlists.

    Existing rows are accepted only when they are byte-equivalent to the
    authoritative mapping input, explicitly disabled, and exactly ``REVIEW``.
    The Google-Sheets caller applies additional current-inventory, drift and
    OPEN-alert exclusions before invoking this boundary.
    """

    result: dict[tuple[str, str], dict[str, str]] = {}
    new_keys: set[tuple[str, str]] = set()
    review_keys: set[tuple[str, str]] = set()
    for raw in new_rows:
        key = _canonical_key(raw.get("server_id", ""), raw.get("stream_id", ""))
        if key in existing_rows or key in result:
            raise AutoMatchError("The new-channel set contains an existing or duplicate identity.")
        row = {str(column): str(value or "") for column, value in raw.items()}
        result[key] = row
        new_keys.add(key)
    for raw in review_rows:
        key = _canonical_key(raw.get("server_id", ""), raw.get("stream_id", ""))
        if key in result or key not in existing_rows:
            raise AutoMatchError(
                "The REVIEW recheck set contains a new, duplicate, or unknown identity."
            )
        authoritative = {
            str(column): str(value or "")
            for column, value in existing_rows[key].items()
        }
        row = {str(column): str(value or "") for column, value in raw.items()}
        if row != authoritative:
            raise AutoMatchError(
                "A REVIEW recheck row differs from the authoritative mapping input."
            )
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        if action != "REVIEW" or _mapping_is_enabled(row, action):
            raise AutoMatchError(
                "Only explicitly disabled REVIEW rows may enter the recheck boundary."
            )
        result[key] = row
        review_keys.add(key)
    return result, frozenset(new_keys), frozenset(review_keys)


def _enforce_approval_blast_radius(
    approved_by_server: Mapping[str, int], *, total_new_rows: int
) -> None:
    """Stop an unexpectedly broad first-run change before it reaches Sheets."""
    total_approved = sum(int(value) for value in approved_by_server.values())
    if int(total_new_rows) < LARGE_BATCH_THRESHOLD:
        return
    total_limit = min(
        MAX_LARGE_BATCH_APPROVALS,
        int(math.ceil(int(total_new_rows) * MAX_LARGE_BATCH_APPROVAL_FRACTION)),
    )
    excessive_servers = {
        server_id: count
        for server_id, count in approved_by_server.items()
        if int(count) > MAX_LARGE_BATCH_APPROVALS_PER_SERVER
    }
    if total_approved > total_limit or excessive_servers:
        server_counts = ", ".join(
            f"{server_id}={int(count):,}"
            for server_id, count in sorted(approved_by_server.items())
        )
        raise AutoMatchError(
            "Automatic matching stopped before Sheet writes because the approval "
            f"batch exceeded its Version 1 safety limit: approved={total_approved:,}, "
            f"allowed={total_limit:,}, by_server=[{server_counts}]."
        )


def _previous_ai_review_keys(
    review_keys: Iterable[tuple[str, str]],
    rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> frozenset[tuple[str, str]]:
    """Return REVIEW identities that already carry an AI suggestion marker."""

    return frozenset(
        key
        for key in review_keys
        if "ai-review-v1" in str(rows_by_key[key].get("notes", "")).casefold()
    )


def _rotated_round_robin_review_keys(
    review_keys: Iterable[tuple[str, str]], *, rotation: int
) -> tuple[tuple[str, str], ...]:
    """Interleave servers and rotate the bounded queue deterministically."""

    grouped: dict[str, list[tuple[str, str]]] = {}
    for key in set(review_keys):
        grouped.setdefault(key[0], []).append(key)
    if not grouped:
        return ()
    for keys in grouped.values():
        keys.sort(key=lambda item: streaming.stream_sort_key(item[1]))

    servers = sorted(grouped)
    server_offset, row_offset = divmod(rotation, len(servers))
    # Changing the rotation by one changes the first server.  After one full
    # server cycle it also advances every server's local backlog, so repeated
    # bounded runs cannot permanently starve rows beyond the first window.
    servers = servers[row_offset:] + servers[:row_offset]
    for server_id in servers:
        keys = grouped[server_id]
        offset = server_offset % len(keys)
        grouped[server_id] = keys[offset:] + keys[:offset]

    ordered: list[tuple[str, str]] = []
    maximum = max(len(grouped[server_id]) for server_id in servers)
    for index in range(maximum):
        for server_id in servers:
            keys = grouped[server_id]
            if index < len(keys):
                ordered.append(keys[index])
    return tuple(ordered)


def _stage_ai_review_shortlists_with_metrics(
    *,
    proposals: Mapping[tuple[str, str], Any],
    review_keys: Iterable[tuple[str, str]],
    ai_excluded_keys: Iterable[tuple[str, str]] = (),
    corroborated_real_candidates: Sequence[Mapping[str, str]],
    rotation: int = 0,
) -> _AiReviewStaging:
    """Build deterministic fuzzy shortlists without making a mapping decision.

    The frozen Smart Rules proposal is always evaluated first.  This fallback
    is intentionally limited to existing REVIEW rows which Smart Rules could
    not safely finalize from exact evidence.  A shortlist is data for an
    optional reviewer; it never changes the proposal or enables a row.
    """

    if (
        isinstance(rotation, bool)
        or not isinstance(rotation, int)
        or not 0 <= rotation <= MAX_AI_REVIEW_ROTATION
    ):
        raise AutoMatchError(
            f"AI review rotation must be an integer from 0 to "
            f"{MAX_AI_REVIEW_ROTATION:,}."
        )

    candidates_by_market: dict[str, list[dict[str, str]]] = {}
    normalized_counts: dict[tuple[str, str], int] = {}
    for raw in corroborated_real_candidates:
        epg_id = str(raw.get("epg_id") or "")
        display_name = str(raw.get("display_name") or "").strip()
        feed = str(raw.get("feed") or "").strip().upper()
        region = _matcher_market_code(raw.get("region", ""))
        normalized = catalog_stream._simple_match_key(
            raw.get("normalized") or display_name
        )
        if (
            not epg_id
            or not display_name
            or not feed
            or not region
            or region in {"ALL", "UNKNOWN", "AMBIGUOUS"}
            or not normalized
            or catalog_stream._is_dummy_xmltv_id(epg_id)
            or _matcher_has_adult_evidence(display_name, "")
            or _matcher_is_generic_numbered_or_blank(display_name)
        ):
            continue
        candidate = {
            "epg_id": epg_id,
            "display_name": display_name,
            "feed": feed,
            "region": region,
            "normalized": normalized,
        }
        candidates_by_market.setdefault(region, []).append(candidate)
        normalized_counts[(region, normalized)] = (
            normalized_counts.get((region, normalized), 0) + 1
        )

    # Two distinct exact IDs with the same local identity are not useful AI
    # choices.  Remove the entire ambiguous family instead of asking a model to
    # choose between source variants it cannot verify.
    for market, candidates in tuple(candidates_by_market.items()):
        candidates_by_market[market] = [
            candidate
            for candidate in candidates
            if normalized_counts[(market, candidate["normalized"])] == 1
        ]

    # Rows which already hold an AI suggestion still run through Smart Rules,
    # but must not consume the bounded AI backlog again.  Filtering the key set
    # before iteration prevents the first 50 previously reviewed rows from
    # starving every later unresolved row forever.
    eligible_review_keys = set(review_keys).difference(ai_excluded_keys)
    shortlists: list[AiReviewShortlist] = []
    attempted_rows = 0
    comparisons = 0
    for key in _rotated_round_robin_review_keys(
        eligible_review_keys, rotation=rotation
    ):
        if len(shortlists) >= MAX_AI_REVIEW_SHORTLISTS:
            break
        if attempted_rows >= MAX_AI_REVIEW_ATTEMPTED_ROWS:
            break
        attempted_rows += 1
        proposal = proposals.get(key)
        if proposal is None:
            raise AutoMatchError("A REVIEW shortlist proposal is missing.")
        # An exact deterministic proposal proceeds to the programme gate.  If
        # that exact schedule is empty, fuzzy AI must not silently redirect the
        # channel to a different station.
        if bool(proposal.eligible_for_finalization):
            continue
        market = _matcher_market_code(proposal.explicit_market)
        route_plan = tuple(
            _matcher_market_code(value) for value in proposal.route_plan
        )
        if (
            not proposal.route_explicit
            or market in {"", "ALL", "UNKNOWN", "AMBIGUOUS", "NA_DIASPORA"}
            or len(route_plan) != 1
            or route_plan[0] != market
            or _matcher_has_adult_evidence(
                proposal.channel_name, proposal.category_name
            )
            or _matcher_is_generic_numbered_or_blank(proposal.channel_name)
        ):
            continue
        query = catalog_stream._simple_match_key(proposal.channel_name)
        if not query:
            continue
        market_candidates = candidates_by_market.get(market, ())
        # Never score a partial market. A partial candidate universe could
        # make the shortlist depend on catalog order and omit the best match.
        # Stop this bounded run and let a later rotation retry instead.
        if len(market_candidates) > MAX_AI_REVIEW_COMPARISONS - comparisons:
            break
        ranked: list[tuple[float, str, str, dict[str, str]]] = []
        for candidate in market_candidates:
            comparisons += 1
            score = float(fuzz.WRatio(query, candidate["normalized"]))
            if score < MIN_AI_REVIEW_FUZZY_SCORE:
                continue
            ranked.append(
                (
                    score,
                    candidate["normalized"],
                    candidate["epg_id"],
                    candidate,
                )
            )
        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1],
                item[2].casefold(),
                item[2],
            )
        )
        selected = ranked[:MAX_AI_REVIEW_CANDIDATES]
        if len(selected) < MIN_AI_REVIEW_CANDIDATES:
            continue
        shortlist_candidates = tuple(
            AiReviewCandidate(
                candidate_key=f"c{index:02d}",
                epg_id=candidate["epg_id"],
                display_name=candidate["display_name"],
                feed=candidate["feed"],
                region=candidate["region"],
                local_score=int(round(score)),
            )
            for index, (score, _normalized, _epg_id, candidate) in enumerate(
                selected, start=1
            )
        )
        shortlists.append(
            AiReviewShortlist(
                server_id=proposal.server_id,
                stream_id=proposal.stream_id,
                channel_name=proposal.channel_name,
                category_name=proposal.category_name,
                market=market,
                candidates=shortlist_candidates,
            )
        )
    return _AiReviewStaging(
        shortlists=tuple(shortlists),
        attempted_rows=attempted_rows,
        comparisons=comparisons,
    )


def _stage_ai_review_shortlists(
    *,
    proposals: Mapping[tuple[str, str], Any],
    review_keys: Iterable[tuple[str, str]],
    ai_excluded_keys: Iterable[tuple[str, str]] = (),
    corroborated_real_candidates: Sequence[Mapping[str, str]],
    rotation: int = 0,
) -> tuple[AiReviewShortlist, ...]:
    """Compatibility wrapper returning only bounded shortlist rows."""

    return _stage_ai_review_shortlists_with_metrics(
        proposals=proposals,
        review_keys=review_keys,
        ai_excluded_keys=ai_excluded_keys,
        corroborated_real_candidates=corroborated_real_candidates,
        rotation=rotation,
    ).shortlists


def _finalize_ai_review_shortlists(
    staged: Sequence[AiReviewShortlist],
    *,
    unresolved_keys: frozenset[tuple[str, str]],
    programme_gates: Mapping[str, Any],
    resolved_source_ids: Mapping[str, str],
) -> tuple[AiReviewShortlist, ...]:
    """Keep only same-snapshot candidates with the strong programme gate."""

    result: list[AiReviewShortlist] = []
    for shortlist in staged:
        if shortlist.key not in unresolved_keys:
            continue
        viable: list[AiReviewCandidate] = []
        for candidate in shortlist.candidates:
            gate = programme_gates.get(candidate.epg_id)
            if (
                gate is None
                or not bool(gate.passed)
                or resolved_source_ids.get(candidate.epg_id) != candidate.epg_id
            ):
                continue
            viable.append(candidate)
        if not MIN_AI_REVIEW_CANDIDATES <= len(viable) <= MAX_AI_REVIEW_CANDIDATES:
            continue
        # Reissue opaque keys after programme filtering so every shortlist is
        # contiguous and has no externally observable gap from a rejected ID.
        finalized = tuple(
            replace(candidate, candidate_key=f"c{index:02d}")
            for index, candidate in enumerate(viable, start=1)
        )
        result.append(replace(shortlist, candidates=finalized))
    return tuple(result)


def _learn_cross_server_approved_aliases(
    *,
    resolver: Any,
    mapping_rows: Sequence[Mapping[str, Any]],
    corroborated_real_candidates: Sequence[Mapping[str, str]],
    quarantined_keys: frozenset[tuple[str, str]],
    current_unchanged_keys: frozenset[tuple[str, str]],
) -> _LearnedAliasMemory:
    """Register only independently repeated, human-approved mapping memory.

    This knowledge exists for one run only.  It is derived after both official
    catalogs have been corroborated, and it can therefore help Smart Rules
    without turning one provider's spelling (or an AI suggestion) into global
    unattended policy.
    """

    candidates_by_id: dict[str, list[Mapping[str, str]]] = {}
    case_variants: dict[str, set[str]] = {}
    for candidate in corroborated_real_candidates:
        epg_id = str(candidate.get("epg_id") or "")
        if not epg_id:
            continue
        candidates_by_id.setdefault(epg_id, []).append(candidate)
        case_variants.setdefault(epg_id.casefold(), set()).add(epg_id)

    evidence: dict[tuple[str, str], dict[str, set[str]]] = {}
    evidence_rows = 0
    for row in mapping_rows:
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        source = streaming.clean_text(row.get("source", ""), 40).casefold()
        if action not in {"MANUAL", "APPROVED"} or source not in {
            "epgshare",
            "epgshare01",
        }:
            continue
        if not _mapping_is_enabled(row, action):
            continue
        key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        if key in quarantined_keys or key not in current_unchanged_keys:
            continue
        epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
        candidate_rows = candidates_by_id.get(epg_id, ())
        # Exact case is part of the opaque XMLTV identity.  A case-colliding
        # catalog family is never safe learning evidence.
        if (
            not candidate_rows
            or len(case_variants.get(epg_id.casefold(), ())) != 1
        ):
            continue
        regions = {
            str(candidate.get("region") or "").strip().upper()
            for candidate in candidate_rows
        }
        if len(regions) != 1 or "ALL" in regions or "" in regions:
            continue
        try:
            context = parse_channel_context_v8(
                resolver.engine,
                streaming.clean_identifier(row.get("channel_name", ""), 300),
                streaming.clean_text(row.get("category_name", ""), 200),
            )
        except Exception:
            # A single unusual legacy label must not stop normal publishing;
            # it simply cannot become shared matcher knowledge in this run.
            continue
        alias = str(getattr(context, "strict_key", "") or "").strip()
        if not alias:
            continue
        region = next(iter(regions))
        evidence.setdefault((alias, region), {}).setdefault(epg_id, set()).add(key[0])
        evidence_rows += 1

    static_aliases = tuple(getattr(resolver, "approved_aliases", ()) or ())
    accepted: list[dict[str, str]] = []
    static_reused_groups = 0
    rejected_groups = 0
    for (alias, region), targets in sorted(evidence.items()):
        # One spelling must point to one exact schedule, and independent
        # approval on at least two servers is required to defeat one-provider
        # systematic naming mistakes.
        if len(targets) != 1:
            rejected_groups += 1
            continue
        epg_id, supporting_servers = next(iter(targets.items()))
        if len(supporting_servers) < 2:
            rejected_groups += 1
            continue

        static_targets: set[str] = set()
        for static in static_aliases:
            static_alias = " ".join(
                str(static.get("alias") or "").casefold().split()
            )
            static_regions = {
                str(value).strip().upper()
                for value in tuple(static.get("regions", ("ALL",)) or ("ALL",))
            }
            if static_alias != alias or (
                "ALL" not in static_regions and region not in static_regions
            ):
                continue
            static_targets.update(
                str(value).strip()
                for value in tuple(static.get("epg_ids", ()) or ())
                if str(value).strip()
            )
        if static_targets and static_targets != {epg_id}:
            rejected_groups += 1
            continue
        if static_targets == {epg_id}:
            # The frozen/pinned knowledge already provides the same target in
            # this market (or globally). Do not overwrite its provenance or
            # report a duplicate as a new learned registration.
            static_reused_groups += 1
            continue
        accepted.append(
            {
                "alias": alias,
                "regions": region,
                "epg_ids": epg_id,
                "relationship": "cross_server_approved_memory",
                "note": (
                    "Learned in memory from the same human-approved EPGShare "
                    "target on at least two servers and revalidated against the "
                    "current corroborated catalog."
                ),
            }
        )

    if accepted:
        register = getattr(resolver, "register_approved_aliases", None)
        if not callable(register):
            raise AutoMatchError(
                "The Version 1 matcher cannot register safe cross-server memory."
            )
        try:
            registered_count = int(register(accepted))
        except Exception as exc:
            raise AutoMatchError(
                "The Version 1 matcher rejected safe cross-server memory."
            ) from exc
        if registered_count != len(accepted):
            raise AutoMatchError(
                "The Version 1 matcher did not register the exact safe "
                "cross-server memory set."
            )

    digest = hashlib.sha256()
    for learned in accepted:
        for field in ("alias", "regions", "epg_ids", "relationship"):
            encoded = learned[field].encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return _LearnedAliasMemory(
        evidence_rows=evidence_rows,
        considered_groups=len(evidence),
        registered_aliases=len(accepted),
        static_reused_groups=static_reused_groups,
        rejected_groups=rejected_groups,
        sha256=digest.hexdigest(),
    )


def _current_unchanged_mapping_keys(
    *,
    mapping_rows: Sequence[Mapping[str, Any]],
    safe_inventories: Sequence[
        tuple[str, Sequence[Mapping[str, str]], Mapping[str, str]]
    ],
) -> frozenset[tuple[str, str]]:
    """Return exact mapping identities still present under the same label.

    Cross-server alias memory is stronger than an ordinary match hint, so its
    human evidence uses exact current provider labels and categories. Missing
    rows and even non-severe provider drift are excluded. This is deliberately
    stricter than the display-oriented inventory drift normalization.
    """

    current: dict[tuple[str, str], tuple[str, str]] = {}
    for server_id, channels, categories in safe_inventories:
        for channel in channels:
            key = _canonical_key(server_id, channel.get("stream_id", ""))
            if key in current:
                raise AutoMatchError(
                    "Provider inventories contain a duplicate stream identity."
                )
            category_id = streaming.clean_identifier(
                channel.get("category_id", ""), 120
            )
            current[key] = (
                streaming.clean_identifier(channel.get("name", ""), 300),
                streaming.clean_text(categories.get(category_id, ""), 200),
            )

    result: set[tuple[str, str]] = set()
    for row in mapping_rows:
        key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        provider_identity = current.get(key)
        if provider_identity is None:
            continue
        sheet_identity = (
            streaming.clean_identifier(row.get("channel_name", ""), 300),
            streaming.clean_text(row.get("category_name", ""), 200),
        )
        if sheet_identity == provider_identity:
            result.add(key)
    return frozenset(result)


def auto_match_and_spool(
    *,
    mapping_rows: Sequence[Mapping[str, Any]],
    inventories: Sequence[Any],
    new_rows: Sequence[Mapping[str, Any]],
    review_rows: Sequence[Mapping[str, Any]] = (),
    quarantined_keys: Iterable[tuple[str, str]] = (),
    all_source_file: Path,
    all_source_catalog_file: Path,
    spool_out: Path,
    generated_at: str,
    enable_ai_review: bool = False,
    ai_review_rotation: int | None = None,
    minimum_unique_channels: int = MINIMUM_CORROBORATED_CATALOG_IDS,
    runtime_factory: MatcherRuntimeFactory = prepare_matcher_runtime,
) -> AutoMatchOutcome:
    """Return patched candidate rows after one successful sealed source parse.

    ``review_rows`` is an explicit allowlist supplied by the synchronization
    layer. Unmatched REVIEW rows are returned for reporting only; callers must
    never write them. Only finalized approved rows may leave REVIEW.
    """
    if not isinstance(enable_ai_review, bool):
        raise AutoMatchError("enable_ai_review must be exactly true or false.")
    if ai_review_rotation is not None and (
        isinstance(ai_review_rotation, bool)
        or not isinstance(ai_review_rotation, int)
        or not 0 <= ai_review_rotation <= MAX_AI_REVIEW_ROTATION
    ):
        raise AutoMatchError(
            f"AI review rotation must be an integer from 0 to "
            f"{MAX_AI_REVIEW_ROTATION:,}."
        )

    source_path = Path(all_source_file)
    text_catalog_path = Path(all_source_catalog_file)
    spool_path = Path(spool_out)
    try:
        distinct_paths = {
            source_path.resolve(strict=False),
            text_catalog_path.resolve(strict=False),
            spool_path.resolve(strict=False),
        }
    except (OSError, RuntimeError) as exc:
        raise AutoMatchError("The ALL_SOURCES1 file paths are invalid.") from exc
    if len(distinct_paths) != 3:
        raise AutoMatchError(
            "The ALL_SOURCES1 inputs and selection-spool output must be different files."
        )
    if spool_path.is_symlink() or (spool_path.exists() and not spool_path.is_file()):
        raise AutoMatchError("The EPG selection-spool output path is invalid.")
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    # A failed run must never leave a prior spool that a later step could
    # mistake for this run's successful hand-off.
    try:
        spool_path.unlink(missing_ok=True)
    except OSError as exc:
        raise AutoMatchError("The EPG selection spool could not be prepared.") from exc
    if source_path.is_symlink() or not source_path.is_file():
        raise AutoMatchError("The downloaded ALL_SOURCES1 file is unavailable.")
    if text_catalog_path.is_symlink() or not text_catalog_path.is_file():
        raise AutoMatchError("The official ALL_SOURCES1 text catalog is unavailable.")
    try:
        text_catalog_size = text_catalog_path.stat().st_size
        if text_catalog_size < 1 or text_catalog_size > catalog_stream.MAX_CATALOG_TEXT_BYTES:
            raise AutoMatchError("The official ALL_SOURCES1 text catalog has an invalid size.")
        text_catalog_bytes = text_catalog_path.read_bytes()
        if len(text_catalog_bytes) != text_catalog_size:
            raise AutoMatchError("The official ALL_SOURCES1 text catalog changed while reading.")
        text_catalog = catalog_stream.parse_all_sources_text(
            text_catalog_bytes,
            minimum_ids=int(minimum_unique_channels),
        )
        text_catalog.validate_for_unattended_matching()
    except AutoMatchError:
        raise
    except (OSError, catalog_stream.CatalogStreamError) as exc:
        raise AutoMatchError(str(exc)) from exc

    existing_rows_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in mapping_rows:
        key = _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        if key in existing_rows_by_key:
            raise AutoMatchError("The authoritative mapping contains duplicate identities.")
        existing_rows_by_key[key] = row
    existing_keys = frozenset(existing_rows_by_key)
    try:
        canonical_quarantined_keys = frozenset(
            _canonical_key(server_id, stream_id)
            for server_id, stream_id in quarantined_keys
        )
    except (TypeError, ValueError) as exc:
        raise AutoMatchError("The quarantined mapping identity set is invalid.") from exc
    rows_by_key, new_keys, review_keys = _validate_candidate_rows(
        new_rows=new_rows,
        review_rows=review_rows,
        existing_rows=existing_rows_by_key,
    )
    fixed_ids = active_combined_source_ids(mapping_rows)
    now_epoch = _timestamp_epoch(generated_at)
    window_start = now_epoch - DEFAULT_EPG_HISTORY_DAYS * 86400
    # A caller may pin the rotation for exact replay. Otherwise advance it
    # once per UTC minute, deterministically from this run's generated_at.
    # Completed AI rows are excluded separately; this rotation also prevents
    # permanently unshortlistable rows at the head of a large backlog from
    # starving later servers and streams.
    effective_ai_review_rotation = (
        0
        if not enable_ai_review
        else (
            int(ai_review_rotation)
            if ai_review_rotation is not None
            else (now_epoch // 60) % (MAX_AI_REVIEW_ROTATION + 1)
        )
    )

    safe_inventories: list[tuple[str, list[dict[str, str]], dict[str, str]]] = []
    for inventory in inventories:
        server_id = _canonical_key(getattr(inventory, "server_id", ""), "probe")[0]
        channels, categories = _safe_matcher_inventory(inventory)
        safe_inventories.append((server_id, channels, categories))
    current_unchanged_keys = _current_unchanged_mapping_keys(
        mapping_rows=mapping_rows,
        safe_inventories=safe_inventories,
    )

    proposals: dict[tuple[str, str], Any] = {}
    runtime_box: list[MatcherRuntime] = []
    matcher_catalog_box: list[MatcherCatalogSnapshot] = []
    corroboration_box: list[_CatalogCorroboration] = []
    staged_shortlists_box: list[_AiReviewStaging] = []
    finalized_shortlists_box: list[tuple[AiReviewShortlist, ...]] = []
    learned_alias_box: list[_LearnedAliasMemory] = []

    def select_provisional_ids(
        source_catalog: catalog_stream.CatalogSnapshot,
    ) -> Iterable[str]:
        xml_ids = frozenset(channel.epg_id for channel in source_catalog.channels)
        text_ids = frozenset(entry.epg_id for entry in text_catalog.entries)
        corroboration = _corroborate_catalog_ids(
            xml_ids,
            text_ids,
            minimum_unique_channels=int(minimum_unique_channels),
        )

        xml_by_id = {channel.epg_id: channel for channel in source_catalog.channels}
        text_by_id = {entry.epg_id: entry for entry in text_catalog.entries}
        if any(
            xml_by_id[epg_id].route.region == "ALL"
            for epg_id in corroboration.xml_only_ids
        ) or any(
            (
                (route := catalog_stream._text_catalog_match_route(text_by_id[epg_id]))
                is None
                or route.region == "ALL"
            )
            for epg_id in corroboration.text_only_ids
        ):
            raise AutoMatchError(
                "Bounded XML/text catalog drift contains an ID without one "
                "deterministic market."
            )

        text_real_candidates, text_dummy_ids = text_catalog.matcher_inputs()
        corroborated_real_candidates = [
            candidate
            for candidate in text_real_candidates
            if candidate.get("epg_id") in corroboration.exact_ids
            and str(candidate.get("epg_id") or "").casefold()
            not in corroboration.union_casefold_collision_keys
        ]
        corroborated_dummy_ids = {
            folded: epg_id
            for folded, epg_id in text_dummy_ids.items()
            if epg_id in corroboration.exact_ids
            and epg_id.casefold()
            not in corroboration.union_casefold_collision_keys
        }

        # Keep every official TXT identity represented in the runtime
        # competition, including TXT-only entries and conservative shadows for
        # case-collision or Unicode-whitespace identities which may not earn an
        # approval. Add XML-only shadows as well. An ineligible shadow can
        # block false uniqueness, but the approval catalog below cannot approve
        # it because it lacks exact corroboration.
        runtime_candidates = list(text_real_candidates)
        runtime_dummy_ids = dict(text_dummy_ids)
        occupied_folds = {
            str(candidate.get("epg_id") or "").casefold()
            for candidate in runtime_candidates
        }.union(runtime_dummy_ids)
        for entry in text_catalog.entries:
            original_epg_id = entry.epg_id
            epg_id = _runtime_shadow_id(original_epg_id)
            folded = epg_id.casefold()
            if folded in occupied_folds:
                continue
            match_route = catalog_stream._text_catalog_match_route(entry)
            if match_route is None:
                # validate_for_unattended_matching() already rejects this;
                # retain a defensive assertion at the use boundary.
                raise AutoMatchError(
                    "The official text catalog has unsafe country evidence."
                )
            display_name = catalog_stream._epg_id_match_name(original_epg_id)
            runtime_candidates.append(
                {
                    "epg_id": epg_id,
                    "feed": match_route.feed,
                    "region": match_route.region,
                    "display_name": display_name,
                    "normalized": catalog_stream._simple_match_key(display_name),
                }
            )
            occupied_folds.add(folded)
        for channel in source_catalog.channels:
            epg_id = channel.epg_id
            folded = epg_id.casefold()
            if (
                epg_id not in corroboration.xml_only_ids
                or folded in occupied_folds
            ):
                continue
            # Keep one deterministic, non-approvable representative even for
            # an XML-only case-collision family. Omitting every member of such
            # a family could manufacture false uniqueness for a similar shared
            # station. Corroborated kind/region metadata is intentionally not
            # added below, so a shadow can only block an approval, never earn
            # one. Bounded-drift validation has already restricted these IDs
            # to printable ASCII without whitespace or controls.
            display_name = catalog_stream._epg_id_match_name(epg_id)
            runtime_candidates.append(
                {
                    "epg_id": epg_id,
                    "feed": channel.route.feed,
                    "region": channel.route.region,
                    "display_name": display_name,
                    "normalized": catalog_stream._simple_match_key(display_name),
                }
            )
            occupied_folds.add(folded)
        runtime = runtime_factory(runtime_candidates, runtime_dummy_ids)
        if not runtime.identity.is_expected or not runtime.preflight.ready:
            raise AutoMatchError("The Version 1 matcher verification failed safely.")
        learned_alias_memory = _learn_cross_server_approved_aliases(
            resolver=runtime.resolver,
            mapping_rows=mapping_rows,
            corroborated_real_candidates=corroborated_real_candidates,
            quarantined_keys=canonical_quarantined_keys,
            current_unchanged_keys=current_unchanged_keys,
        )
        all_route_ambiguity = (
            _all_route_ambiguity_labels(runtime)
            if any(
                str(candidate.get("region") or "").upper() == "ALL"
                for candidate in runtime_candidates
            )
            else {}
        )
        matcher_catalog = MatcherCatalogSnapshot.from_matcher_catalog(
            (channel.epg_id for channel in source_catalog.channels),
            real_candidates=corroborated_real_candidates,
            dummy_ids=corroborated_dummy_ids,
            # This digest was computed from the same still-open descriptor
            # which supplied the XML catalog and will supply its programmes.
            # Never bind matcher provenance to a separate pathname pre-read.
            source_sha256=source_catalog.source_sha256,
        )
        if not matcher_catalog.usable:
            raise AutoMatchError("The ALL_SOURCES1 catalog is not safe for matching.")
        for server_id, channels, categories in safe_inventories:
            server_proposals = propose_new_channel_matches(
                runtime.resolver,
                server_id=server_id,
                channels=channels,
                category_names=categories,
                existing_keys=existing_keys,
                target_keys=(
                    key for key in rows_by_key if key[0] == server_id
                ),
                catalog=matcher_catalog,
                matcher_identity=runtime.identity,
                preflight=runtime.preflight,
            )
            server_proposals = {
                key: replace(
                    proposal,
                    eligible_for_finalization=False,
                    decision_reason=(
                        "An unscoped ALL-market EPG candidate shares a strong "
                        "identity with the proposed target"
                    ),
                )
                if proposal.eligible_for_finalization
                and _proposal_blocked_by_all_route(proposal, all_route_ambiguity)
                else proposal
                for key, proposal in server_proposals.items()
            }
            overlap = set(proposals).intersection(server_proposals)
            if overlap:
                raise AutoMatchError("The matcher returned duplicate stream identities.")
            proposals.update(server_proposals)
        if set(proposals) != set(rows_by_key):
            raise AutoMatchError("The matcher result did not cover the exact candidate set.")
        staged_shortlists = (
            _stage_ai_review_shortlists_with_metrics(
                proposals=proposals,
                review_keys=review_keys,
                ai_excluded_keys=_previous_ai_review_keys(
                    review_keys, rows_by_key
                ),
                corroborated_real_candidates=corroborated_real_candidates,
                rotation=effective_ai_review_rotation,
            )
            if enable_ai_review and review_keys
            else _AiReviewStaging((), 0, 0)
        )
        runtime_box.append(runtime)
        matcher_catalog_box.append(matcher_catalog)
        corroboration_box.append(corroboration)
        staged_shortlists_box.append(staged_shortlists)
        learned_alias_box.append(learned_alias_memory)
        return sorted(
            {
                proposal.target_epg_id
                for proposal in proposals.values()
                if proposal.eligible_for_finalization
                and proposal.target_source == "epgshare01"
                and proposal.target_epg_id
            }.union(
                candidate.epg_id
                for shortlist in staged_shortlists.shortlists
                for candidate in shortlist.candidates
            ),
            key=lambda item: (item.casefold(), item),
        )

    try:
        with EpgSelectionSpoolWriter(spool_path) as spool:
            result = catalog_stream.stream_catalog_and_programmes_once(
                path=source_path,
                fixed_wanted_ids=fixed_ids,
                select_provisional_ids=select_provisional_ids,
                window_start=window_start,
                now_epoch=now_epoch,
                channel_sink=spool.add_channel,
                programme_sink=spool.add_programme,
                source_base_url=SOURCE_BASE_URL,
                minimum_unique_channels=int(minimum_unique_channels),
            )
            if (
                len(runtime_box) != 1
                or len(matcher_catalog_box) != 1
                or len(corroboration_box) != 1
                or len(staged_shortlists_box) != 1
                or len(learned_alias_box) != 1
            ):
                raise AutoMatchError("The ALL_SOURCES1 catalog boundary was not verified.")
            if (
                result.catalog.source_sha256 != result.source_sha256
                or matcher_catalog_box[0].source_sha256 != result.source_sha256
            ):
                raise AutoMatchError(
                    "The ALL_SOURCES1 catalog provenance was not descriptor-bound."
                )
            gates = result.programme_gates
            evidence = ScheduleEvidence(
                declared_ids=frozenset(
                    channel.epg_id for channel in result.catalog.channels
                ),
                informative_future_programmes={
                    epg_id: gate.distinct_informative_programmes
                    for epg_id, gate in gates.items()
                },
                latest_informative_future_stop={
                    epg_id: int(gate.latest_stop_epoch or 0)
                    for epg_id, gate in gates.items()
                },
                gate_passed_by_id={
                    epg_id: bool(gate.passed) for epg_id, gate in gates.items()
                },
                checked_at_epoch=now_epoch,
                source_sha256=result.source_sha256,
            )
            patched_rows: list[dict[str, str]] = []
            approved_ids: set[str] = set()
            approved_count = 0
            approved_by_server = {server_id: 0 for server_id in SUPPORTED_SERVERS}
            for key in sorted(
                rows_by_key,
                key=lambda item: (item[0], streaming.stream_sort_key(item[1])),
            ):
                original = rows_by_key[key]
                metadata_before = {
                    column: original.get(column, "") for column in _METADATA_COLUMNS
                }
                final = finalize_proposal(proposals[key], evidence)
                patch = final.sheet_patch(existing_notes=original.get("notes", ""))
                row = dict(original)
                row.update(patch)
                if final.approved:
                    if (
                        row.get("action") != "AUTO_EPGSHARE"
                        or row.get("source") != "epgshare01"
                        or row.get("epg_feed") != "ALL_SOURCES1"
                        or row.get("enabled") != "TRUE"
                        or not row.get("epg_id")
                    ):
                        raise AutoMatchError("An approved match violated the EPGShare-only policy.")
                    approved_count += 1
                    approved_ids.add(row["epg_id"])
                    approved_by_server[key[0]] += 1
                else:
                    row["action"] = "REVIEW"
                    row["enabled"] = "FALSE"
                if any(
                    row.get(column, "") != metadata_before[column]
                    for column in _METADATA_COLUMNS
                ):
                    raise AutoMatchError("Automatic EPG matching attempted to approve metadata.")
                patched_rows.append(row)

            # Preserve the original new-channel blast-radius policy. Existing
            # REVIEW approvals are capped independently by the Sheet caller.
            new_approved_by_server = {
                server_id: sum(
                    1
                    for key, row in zip(
                        sorted(
                            rows_by_key,
                            key=lambda item: (
                                item[0], streaming.stream_sort_key(item[1])
                            ),
                        ),
                        patched_rows,
                    )
                    if key in new_keys
                    and key[0] == server_id
                    and row.get("enabled") == "TRUE"
                )
                for server_id in SUPPORTED_SERVERS
            }
            _enforce_approval_blast_radius(
                new_approved_by_server, total_new_rows=len(new_keys)
            )

            resolved_targets = frozenset(result.resolved_source_ids.values())
            if not approved_ids.issubset(resolved_targets):
                raise AutoMatchError("A newly approved EPG ID was not resolved in ALL_SOURCES1.")
            for epg_id in approved_ids:
                gate = gates.get(epg_id)
                if gate is None or not gate.passed:
                    raise AutoMatchError("A newly approved EPG ID lacks the strong programme gate.")
            patched_by_key_for_review = {
                _canonical_key(row.get("server_id", ""), row.get("stream_id", "")): row
                for row in patched_rows
            }
            unresolved_review_keys = frozenset(
                key
                for key in review_keys
                if patched_by_key_for_review[key].get("enabled") != "TRUE"
            )
            finalized_shortlists_box.append(
                _finalize_ai_review_shortlists(
                    staged_shortlists_box[0].shortlists,
                    unresolved_keys=unresolved_review_keys,
                    programme_gates=gates,
                    resolved_source_ids=result.resolved_source_ids,
                )
            )
            spool.seal(
                source_sha256=result.source_sha256,
                source_bytes=result.stats.source_bytes,
                catalog_sha256=result.catalog.fingerprint_sha256,
                window_start=window_start,
                created_at_epoch=now_epoch,
                fixed_requested_ids=result.requested_fixed_ids,
                provisional_requested_ids=result.requested_provisional_ids,
                resolved_source_ids=result.resolved_source_ids,
                stats=result.stats,
            )
    except AutoMatchError:
        spool_path.unlink(missing_ok=True)
        raise
    except (catalog_stream.CatalogStreamError, SpoolError) as exc:
        spool_path.unlink(missing_ok=True)
        raise AutoMatchError(str(exc)) from exc
    except Exception as exc:
        spool_path.unlink(missing_ok=True)
        raise AutoMatchError("Automatic EPG matching stopped safely before any Sheet write.") from exc

    runtime = runtime_box[0]
    corroboration = corroboration_box[0]
    learned_alias_memory = learned_alias_box[0]
    if len(finalized_shortlists_box) != 1:
        spool_path.unlink(missing_ok=True)
        raise AutoMatchError("AI review candidate verification did not complete safely.")
    provisional_keys = frozenset(
        key
        for key, proposal in proposals.items()
        if proposal.eligible_for_finalization and proposal.target_epg_id
    )
    rejected_gate_keys = frozenset(
        key
        for key in provisional_keys
        for proposal in (proposals[key],)
        if proposal.target_epg_id not in result.programme_gates
        or not result.programme_gates[proposal.target_epg_id].passed
    )
    patched_by_key = {
        _canonical_key(row.get("server_id", ""), row.get("stream_id", "")): row
        for row in patched_rows
    }
    new_approved_count = sum(
        1 for key in new_keys if patched_by_key[key].get("enabled") == "TRUE"
    )
    recheck_approved_count = sum(
        1 for key in review_keys if patched_by_key[key].get("enabled") == "TRUE"
    )
    matcher_catalog = matcher_catalog_box[0]
    verified_placeholder_keys = frozenset(
        key
        for key, proposal in proposals.items()
        if key in review_keys
        and proposal.matcher_action == "AUTO_DUMMY"
        and proposal.match_method in DUMMY_REVIEW_METHODS
        and bool(proposal.target_epg_id)
        and matcher_catalog.contains_unambiguous_exact(proposal.target_epg_id)
        and matcher_catalog.target_kinds(proposal.target_epg_id)
        == frozenset({"dummy"})
    )
    return AutoMatchOutcome(
        rows=tuple(patched_rows),
        considered_rows=len(proposals),
        provisional_rows=len(provisional_keys),
        approved_rows=approved_count,
        review_rows=len(patched_rows) - approved_count,
        new_considered_rows=len(new_keys),
        new_provisional_rows=len(provisional_keys.intersection(new_keys)),
        new_approved_rows=new_approved_count,
        new_review_rows=len(new_keys) - new_approved_count,
        new_rejected_programme_gates=len(rejected_gate_keys.intersection(new_keys)),
        recheck_considered_rows=len(review_keys),
        recheck_provisional_rows=len(provisional_keys.intersection(review_keys)),
        recheck_approved_rows=recheck_approved_count,
        recheck_review_rows=len(review_keys) - recheck_approved_count,
        recheck_rejected_programme_gates=len(
            rejected_gate_keys.intersection(review_keys)
        ),
        rejected_programme_gates=len(rejected_gate_keys),
        fixed_requested_ids=len(result.requested_fixed_ids),
        catalog_channels=len(result.catalog.channels),
        text_catalog_channels=len(text_catalog.entries),
        corroborated_catalog_channels=len(corroboration.exact_ids),
        xml_only_catalog_channels=len(corroboration.xml_only_ids),
        text_only_catalog_channels=len(corroboration.text_only_ids),
        catalog_drift_channels=(
            len(corroboration.xml_only_ids) + len(corroboration.text_only_ids)
        ),
        catalog_corroboration_mode=corroboration.mode,
        catalog_drift_sha256=corroboration.drift_sha256,
        source_sha256=result.source_sha256,
        catalog_sha256=result.catalog.fingerprint_sha256,
        text_catalog_generated_token=text_catalog.generated_token,
        text_catalog_fingerprint_sha256=text_catalog.fingerprint_sha256,
        text_catalog_file_sha256=hashlib.sha256(text_catalog_bytes).hexdigest(),
        matcher_version=runtime.identity.version,
        matcher_build_id=runtime.identity.build_id,
        matcher_sha256=runtime.identity.source_sha256,
        matcher_engine_sha256=runtime.identity.engine_source_sha256,
        approved_aliases_sha256=runtime.approved_aliases_sha256,
        schedule_equivalences_sha256=runtime.schedule_equivalences_sha256,
        learned_alias_evidence_rows=learned_alias_memory.evidence_rows,
        learned_alias_considered_groups=learned_alias_memory.considered_groups,
        learned_alias_registered=learned_alias_memory.registered_aliases,
        learned_alias_static_reused_groups=(
            learned_alias_memory.static_reused_groups
        ),
        learned_alias_rejected_groups=learned_alias_memory.rejected_groups,
        learned_alias_sha256=learned_alias_memory.sha256,
        ai_review_rotation=effective_ai_review_rotation,
        ai_review_attempted_rows=staged_shortlists_box[0].attempted_rows,
        ai_review_comparisons=staged_shortlists_box[0].comparisons,
        ai_review_shortlists=finalized_shortlists_box[0],
        verified_placeholder_keys=verified_placeholder_keys,
    )


__all__ = [
    "AUTO_MATCH_INTEGRATION_VERSION",
    "AiReviewCandidate",
    "AiReviewShortlist",
    "AutoMatchError",
    "AutoMatchOutcome",
    "MatcherRuntime",
    "active_combined_source_ids",
    "auto_match_and_spool",
    "prepare_matcher_runtime",
]
