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
import os
import re
import stat
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
    MatcherIdentity,
    MatcherPreflight,
    SAFE_REAL_METHODS,
    ScheduleEvidence,
    STRICT_ENGINE_SOURCE_SHA256,
    STRICT_MATCHER_BUILD_ID,
    STRICT_MATCHER_SOURCE_SHA256,
    STRICT_MATCHER_VERSION,
    _has_adult_evidence as _matcher_has_adult_evidence,
    _is_generic_numbered_or_blank as _matcher_is_generic_numbered_or_blank,
    _market_code as _matcher_market_code,
    automatic_mapping_binding_sha256,
    automatic_mapping_provenance_note,
    finalize_proposal,
    prepare_resolver_strict,
    propose_new_channel_matches,
)
from skytv_epg_contextual_v8 import (  # noqa: E402
    _is_decorative_heading_v84 as _matcher_is_decorative_heading,
    install_contextual_v8,
    parse_candidate_context_v8,
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
# The official TXT file carries its own UTC generation token.  It may lag the
# XML guide during EPGShare's non-atomic rollover, but an old or future-dated
# catalog must never supply market/feed semantics to an AI approval.
MAX_TEXT_CATALOG_AGE_SECONDS = 48 * 60 * 60
MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS = 6 * 60 * 60
SOURCE_BASE_URL = "https://epgshare01.online/epgshare01/"
ENGINE_SOURCE_PATH = SRC_DIR / "skytv_epg_engine.py"
CONTEXTUAL_SOURCE_PATH = SRC_DIR / "skytv_epg_contextual_v8.py"
APPROVED_ALIASES_PATH = REPOSITORY_ROOT / "knowledge" / "approved_channel_aliases.csv"
SCHEDULE_EQUIVALENCES_PATH = (
    REPOSITORY_ROOT / "knowledge" / "schedule_equivalence_groups.json"
)
APPROVED_ALIASES_SHA256 = (
    "354e66933fdb0adc8dfea5f9203bf914ebd1814a892fdf9d3d2e2da158043cf5"
)
SCHEDULE_EQUIVALENCES_SHA256 = (
    "a0de16a12d00d6aca96c7f7a54ed444a47467115266f467521880cc8a56b6edc"
)
LARGE_BATCH_THRESHOLD = 100
MAX_LARGE_BATCH_APPROVAL_FRACTION = 0.35
MAX_LARGE_BATCH_APPROVALS = 5_000
MAX_LARGE_BATCH_APPROVALS_PER_SERVER = 2_500
# A local synthetic guide is materially safer than guessing a real schedule,
# but it still enables a Mapping row.  Keep the opt-in lane bounded to the
# same maximum number of deterministic REVIEW updates the sync writer can
# commit and revalidate in one run.
MAX_COVERAGE_FALLBACK_ROWS = 5_000
MAX_AI_REVIEW_SHORTLISTS = 200
MAX_AI_REVIEW_ATTEMPTED_ROWS = 1_000
MAX_AI_REVIEW_COMPARISONS = 4_000_000
MAX_AI_REVIEW_ROTATION = 1_000_000
MIN_AI_REVIEW_CANDIDATES = 2
MAX_AI_REVIEW_CANDIDATES = 8
MIN_AI_REVIEW_FUZZY_SCORE = 55.0
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_AUTOMATIC_MEMORY_ACTION = "AUTO_EPGSHARE"
_HUMAN_MEMORY_ACTIONS = frozenset({"MANUAL", "APPROVED"})
_AI_VERIFIED_V2_GATE = "smart+gemini-high+catalog+programme"
_AI_VERIFIED_METHODS = SAFE_REAL_METHODS.union({"dual_rank_consensus"})
_AUTO_MAP_V1_PROVENANCE_RE = re.compile(
    r"\Aauto-map-v1 method=(?P<method>[a-z0-9][a-z0-9_.+\-]{0,79}); "
    r"market=(?P<market>[A-Za-z0-9][A-Za-z0-9_\-]{0,31}); "
    r"matcher=(?P<matcher>[0-9]+(?:\.[0-9]+){1,3}); "
    r"matcher_build=(?P<matcher_build>[A-Z0-9][A-Z0-9_.+\-]{0,127}); "
    r"matcher_sha256=(?P<matcher_sha256>[0-9a-f]{64}); "
    r"engine_sha256=(?P<engine_sha256>[0-9a-f]{64}); "
    r"catalog_sha256=(?P<catalog_sha256>[0-9a-f]{64}); "
    r"source_sha256=(?P<source_sha256>[0-9a-f]{64})(?: \| |\Z)"
)
_LEGACY_SERVER1_REASON = (
    "Legacy Server 1 native ID requires reviewed EPGShare replacement"
)
_LEGACY_SERVER1_NOTES = frozenset(
    {
        (
            "Legacy reason: Panel XMLTV contains useful current/future programme "
            "information; Quarantined during the Version 1 migration; replace "
            "with a reviewed exact EPGShare ALL ID before changing action from REVIEW"
        ),
        (
            "Legacy reason: Panel XMLTV supplies the unsupported market's useful "
            "current/future schedule; Quarantined during the Version 1 migration; "
            "replace with a reviewed exact EPGShare ALL ID before changing action "
            "from REVIEW"
        ),
    }
)
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
    # Exact private Mapping rows which supported aliases actually registered
    # into this run's resolver. This never enters summary_fields(); the sync
    # layer uses it only to reacquire the teaching provider identities before
    # any dependent Sheet mutation.
    support_rows: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class AiReviewCandidate:
    """One exact real EPGShare identity offered for strict AI verification."""

    candidate_key: str
    epg_id: str
    display_name: str
    feed: str
    region: str
    local_score: int
    contextual_score: int = 0
    direction: str = ""
    timeshift: str = ""
    has_plus: bool = False
    has_extra: bool = False
    has_alternate: bool = False
    numbers: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    content: tuple[str, ...] = ()
    programme_count: int = 0
    programme_first_start_epoch: int | None = None
    programme_latest_stop_epoch: int | None = None
    programme_checked_at_epoch: int = 0
    programme_source_sha256: str = ""


@dataclass(frozen=True, slots=True)
class AiReviewShortlist:
    """A bounded, programme-verified candidate set for one unresolved row."""

    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    market: str
    candidates: tuple[AiReviewCandidate, ...]
    normalized_name: str = ""
    normalized_category: str = ""
    strict_identity: str = ""
    bag_identity: str = ""
    route_plan: tuple[str, ...] = ()
    route_explicit: bool = False
    direction: str = ""
    timeshift: str = ""
    has_plus: bool = False
    has_extra: bool = False
    has_alternate: bool = False
    numbers: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    content: tuple[str, ...] = ()
    smart_epg_id: str = ""
    smart_match_method: str = "dual_rank_consensus"
    source_sha256: str = ""
    text_catalog_file_sha256: str = ""
    text_catalog_fingerprint_sha256: str = ""
    text_catalog_generated_token: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True, slots=True)
class _AiReviewStaging:
    """Bounded shortlist output plus exact fuzzy-work counters."""

    shortlists: tuple[AiReviewShortlist, ...]
    attempted_rows: int
    comparisons: int


@dataclass(frozen=True, slots=True)
class VerificationSemanticsEvidence:
    """Protected station semantics parsed from one corroborated catalog ID."""

    direction: str = ""
    timeshift: str = ""
    has_plus: bool = False
    has_extra: bool = False
    has_alternate: bool = False
    numbers: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    content: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationCatalogCandidateEvidence:
    """Exact current-run metadata for one XML/TXT-corroborated real ID."""

    epg_id: str
    market: str
    feed: str
    semantics: VerificationSemanticsEvidence
    is_real: bool


@dataclass(frozen=True, slots=True)
class VerificationProgrammeGateEvidence:
    """One unmodified current-run programme-gate result plus its provenance."""

    channel_key: str
    distinct_informative_programmes: int
    first_start_epoch: int | None
    latest_stop_epoch: int | None
    checked_at_epoch: int
    source_sha256: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CurrentRunVerificationEvidence:
    """Immutable source-bound evidence for a later local approval boundary.

    This object deliberately retains the complete exact-ID sets and every
    programme gate returned by the one-pass parser. It is private run state,
    not part of the compact JSON workflow summary.
    """

    source_sha256: str
    text_catalog_file_sha256: str
    text_catalog_fingerprint_sha256: str
    text_catalog_generated_token: str
    checked_at_epoch: int
    xml_catalog_ids: frozenset[str]
    text_catalog_ids: frozenset[str]
    catalog_candidates: tuple[VerificationCatalogCandidateEvidence, ...]
    programme_gates: tuple[VerificationProgrammeGateEvidence, ...]

    def __post_init__(self) -> None:
        xml_ids = frozenset(self.xml_catalog_ids)
        text_ids = frozenset(self.text_catalog_ids)
        candidates = tuple(self.catalog_candidates)
        gates = tuple(self.programme_gates)
        object.__setattr__(self, "xml_catalog_ids", xml_ids)
        object.__setattr__(self, "text_catalog_ids", text_ids)
        object.__setattr__(self, "catalog_candidates", candidates)
        object.__setattr__(self, "programme_gates", gates)
        if _SHA256_RE.fullmatch(self.source_sha256) is None:
            raise AutoMatchError("Current-run verification evidence has an invalid source hash.")
        if (
            _SHA256_RE.fullmatch(self.text_catalog_file_sha256) is None
            or _SHA256_RE.fullmatch(self.text_catalog_fingerprint_sha256) is None
        ):
            raise AutoMatchError(
                "Current-run verification evidence has invalid text-catalog hashes."
            )
        if int(self.checked_at_epoch) <= 0:
            raise AutoMatchError("Current-run verification evidence has an invalid check time.")
        _validate_text_catalog_generation(
            self.text_catalog_generated_token,
            now_epoch=int(self.checked_at_epoch),
        )
        exact_ids = xml_ids.intersection(text_ids)
        if any(candidate.epg_id not in exact_ids for candidate in candidates):
            raise AutoMatchError(
                "Current-run candidate evidence is not exactly XML/TXT corroborated."
            )
        if any(
            gate.source_sha256 != self.source_sha256
            or gate.checked_at_epoch != self.checked_at_epoch
            for gate in gates
        ):
            raise AutoMatchError(
                "Current-run programme evidence is not bound to one source snapshot."
            )


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
    # Internal terminal-write dependency. Do not add these private rows to
    # public reports or artifacts.
    learned_alias_support_rows: tuple[dict[str, str], ...]
    ai_review_rotation: int
    ai_review_attempted_rows: int
    ai_review_comparisons: int
    ai_review_shortlists: tuple[AiReviewShortlist, ...]
    verification_evidence: CurrentRunVerificationEvidence
    # Current-run, exact XML/TXT-corroborated dummy proposals.  These remain
    # REVIEW-only and are intentionally omitted from public sync summaries;
    # the read-only backlog analyzer uses the identities only in memory to
    # produce an aggregate count.
    verified_placeholder_keys: frozenset[tuple[str, str]]
    new_dummy_rows: int = 0
    recheck_dummy_rows: int = 0
    new_ignored_rows: int = 0
    recheck_ignored_rows: int = 0
    coverage_fallback_limit: int = 0
    coverage_fallback_candidate_rows: int = 0
    coverage_fallback_applied_rows: int = 0
    coverage_fallback_deferred_rows: int = 0
    coverage_fallback_ai_deferred_rows: int = 0
    coverage_fallback_suppressed_unwritten_new_rows: int = 0
    new_coverage_fallback_rows: int = 0
    recheck_coverage_fallback_rows: int = 0
    coverage_fallback_machine_prefilled_candidate_rows: int = 0
    coverage_fallback_machine_prefilled_applied_rows: int = 0
    coverage_fallback_legacy_server1_candidate_rows: int = 0
    coverage_fallback_legacy_server1_applied_rows: int = 0
    coverage_fallback_protected_manual_rows: int = 0
    coverage_fallback_protected_native_rows: int = 0

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
            "new_channels_classified_as_placeholders": self.new_dummy_rows,
            "review_recheck_classified_as_placeholders": self.recheck_dummy_rows,
            "new_channel_headings_ignored": self.new_ignored_rows,
            "review_recheck_headings_ignored": self.recheck_ignored_rows,
            "coverage_fallback_enabled": self.coverage_fallback_limit > 0,
            "coverage_fallback_limit": self.coverage_fallback_limit,
            "coverage_fallback_candidate_rows": (
                self.coverage_fallback_candidate_rows
            ),
            "coverage_fallback_applied_rows": self.coverage_fallback_applied_rows,
            "coverage_fallback_deferred_rows": (
                self.coverage_fallback_deferred_rows
            ),
            "coverage_fallback_ai_deferred_rows": (
                self.coverage_fallback_ai_deferred_rows
            ),
            "coverage_fallback_suppressed_unwritten_new_rows": (
                self.coverage_fallback_suppressed_unwritten_new_rows
            ),
            "new_channel_coverage_fallback_rows": (
                self.new_coverage_fallback_rows
            ),
            "review_recheck_coverage_fallback_rows": (
                self.recheck_coverage_fallback_rows
            ),
            "coverage_fallback_machine_prefilled_candidate_rows": (
                self.coverage_fallback_machine_prefilled_candidate_rows
            ),
            "coverage_fallback_machine_prefilled_applied_rows": (
                self.coverage_fallback_machine_prefilled_applied_rows
            ),
            "coverage_fallback_legacy_server1_candidate_rows": (
                self.coverage_fallback_legacy_server1_candidate_rows
            ),
            "coverage_fallback_legacy_server1_applied_rows": (
                self.coverage_fallback_legacy_server1_applied_rows
            ),
            "coverage_fallback_protected_manual_rows": (
                self.coverage_fallback_protected_manual_rows
            ),
            "coverage_fallback_protected_native_rows": (
                self.coverage_fallback_protected_native_rows
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
    structural_label = {
        "canonical_identity": "identity",
        "category_language_default": "bag",
        "descriptor_relaxed": "relaxed",
        "edition_aware": "edition",
        "spacing_compact": "compact",
        "strict": "strict",
        "token_multiset": "bag",
    }.get(method)
    if structural_label is not None:
        return structural_label in labels
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


# Country/market words can appear in an official display name even though the
# provider already supplied the same market out-of-band.  Removing only the
# words for that candidate's own route gives us a deliberately coarse station
# family used as a veto, never as a positive matcher.  For example, the CA
# catalog identities ``History`` and ``History Television (Canada)`` both
# reduce to ``history`` and therefore cannot be selected unattended merely
# because a provider writes ``CA History Channel``.
_MARKET_FAMILY_TOKENS: Mapping[str, frozenset[str]] = {
    "US": frozenset({"us", "usa", "american", "united", "states"}),
    "CA": frozenset({"ca", "can", "canada", "canadian"}),
    "UK": frozenset({"uk", "gb", "britain", "british", "england", "united", "kingdom"}),
    "IN": frozenset({"in", "india", "indian"}),
    "AU": frozenset({"au", "australia", "australian"}),
    "NZ": frozenset({"nz", "zealand", "newzealand"}),
}


def _same_market_family_ambiguity_ids(runtime: MatcherRuntime) -> frozenset[str]:
    """Return targets shadowed by a second coarse family in the same market.

    This is an abstention-only catalog check.  It never creates a candidate and
    it deliberately retains the matcher's protected edition/language/content
    dimensions.  Approved knowledge (human approval or cross-server durable
    memory) may still identify one exact member; unlearned structural matches
    must stay in REVIEW when this veto fires.
    """

    state = getattr(runtime.resolver, "state", None)
    by_region = getattr(state, "by_region", None)
    contexts = by_region.get("ALL") if isinstance(by_region, dict) else None
    if not isinstance(contexts, list):
        # Dependency-injected unit runtimes predating this abstention index do
        # not expose contextual state.  The pinned production runtime does, as
        # enforced by prepare_resolver_strict().
        return frozenset()

    grouped: dict[tuple[object, ...], set[str]] = {}
    for context in contexts:
        candidate = dict(getattr(context, "candidate", {}) or {})
        epg_id = str(candidate.get("epg_id") or "")
        market = _matcher_market_code(candidate.get("region", ""))
        if not epg_id or not market or market in {"ALL", "UNKNOWN", "AMBIGUOUS"}:
            continue
        bag_key = getattr(context, "bag_key", "")
        # Lightweight test/runtime doubles may omit the contextual identity
        # fields.  They cannot manufacture a collision and are skipped; the
        # pinned production preflight supplies every field below.
        if not isinstance(bag_key, str) or not bag_key.strip():
            continue
        raw_family = (
            bag_key,
            str(getattr(context, "direction", "") or ""),
            bool(getattr(context, "has_plus", False)),
            bool(getattr(context, "has_extra", False)),
            bool(getattr(context, "has_alternate", False)),
            str(getattr(context, "timeshift", "") or ""),
            tuple(sorted(getattr(context, "numbers", ()) or ())),
            tuple(sorted(getattr(context, "languages", ()) or ())),
            tuple(sorted(getattr(context, "content", ()) or ())),
        )
        route_tokens = _MARKET_FAMILY_TOKENS.get(market, frozenset({market.casefold()}))
        stable_words = tuple(
            word
            for word in raw_family[0].casefold().split()
            if word and word not in route_tokens
        )
        if len("".join(stable_words)) < 4:
            continue
        key = (market, " ".join(stable_words), *raw_family[1:])
        grouped.setdefault(key, set()).add(epg_id)

    return frozenset(
        epg_id
        for members in grouped.values()
        if len(members) > 1
        for epg_id in members
    )


def _proposal_blocked_by_same_market_family(
    proposal: Any, ambiguous_ids: frozenset[str]
) -> bool:
    """Keep unlearned structural matches out of an ambiguous station family."""

    if str(proposal.target_epg_id or "") not in ambiguous_ids:
        return False
    return str(proposal.match_method or "").casefold() not in {
        "approved_knowledge",
        "verified_station_identity",
    }


def _apply_catalog_ambiguity_veto(
    proposal: Any,
    *,
    all_route_labels: Mapping[str, frozenset[str]],
    same_market_ids: frozenset[str],
) -> Any:
    if not proposal.eligible_for_finalization:
        return proposal
    if _proposal_blocked_by_all_route(proposal, all_route_labels):
        return replace(
            proposal,
            eligible_for_finalization=False,
            decision_reason=(
                "An unscoped ALL-market EPG candidate shares a strong "
                "identity with the proposed target"
            ),
        )
    if _proposal_blocked_by_same_market_family(proposal, same_market_ids):
        return replace(
            proposal,
            eligible_for_finalization=False,
            decision_reason=(
                "A second same-market EPG identity belongs to the same "
                "coarse station family"
            ),
        )
    return proposal


def _apply_ai_shortlist_catalog_ambiguity_veto(
    staged: _AiReviewStaging,
    *,
    all_route_labels: Mapping[str, frozenset[str]],
    same_market_ids: frozenset[str],
) -> _AiReviewStaging:
    """Keep catalog competitors omitted by market-scoped AI from becoming invisible."""

    if not staged.shortlists or (not all_route_labels and not same_market_ids):
        return staged
    return _AiReviewStaging(
        shortlists=tuple(
            shortlist
            for shortlist in staged.shortlists
            if shortlist.smart_epg_id not in all_route_labels
            and shortlist.smart_epg_id not in same_market_ids
        ),
        attempted_rows=staged.attempted_rows,
        comparisons=staged.comparisons,
    )


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
        # Dummy mappings are now rendered as local, per-stream synthetic
        # guides.  They must not request or depend on an EPGShare placeholder
        # ID in the one-pass source spool.
        if requested_source == "epgshare01":
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


def _read_bound_text_catalog(path: Path) -> tuple[bytes, str]:
    """Read one immutable regular-file view of the official TXT catalog.

    A pathname can be replaced between ``stat`` and ``read_bytes``.  Keep one
    descriptor open, bound it to the pathname before and after the read, and
    return the exact byte hash used by all later AI evidence.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise AutoMatchError(
            "The official ALL_SOURCES1 text catalog cannot be opened safely."
        ) from exc
    try:
        initial = os.fstat(descriptor)
        path_state = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(initial.st_mode)
            or not stat.S_ISREG(path_state.st_mode)
            or (initial.st_dev, initial.st_ino)
            != (path_state.st_dev, path_state.st_ino)
        ):
            raise AutoMatchError(
                "The official ALL_SOURCES1 text catalog is not a stable regular file."
            )
        if (
            initial.st_size < 1
            or initial.st_size > catalog_stream.MAX_CATALOG_TEXT_BYTES
        ):
            raise AutoMatchError(
                "The official ALL_SOURCES1 text catalog has an invalid size."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(catalog_stream.MAX_CATALOG_TEXT_BYTES + 1)
        final = os.fstat(descriptor)
        final_path = os.stat(path, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(content) != initial.st_size
            or any(
                getattr(final, field) != getattr(initial, field)
                for field in stable_fields
            )
            or any(
                getattr(final_path, field) != getattr(initial, field)
                for field in stable_fields
            )
        ):
            raise AutoMatchError(
                "The official ALL_SOURCES1 text catalog changed while reading."
            )
        return content, hashlib.sha256(content).hexdigest()
    except AutoMatchError:
        raise
    except OSError as exc:
        raise AutoMatchError(
            "The official ALL_SOURCES1 text catalog could not be verified."
        ) from exc
    finally:
        os.close(descriptor)


def _text_catalog_generation_epoch(generated_token: str) -> int:
    """Parse one strict official UTC generation token."""

    token = str(generated_token or "")
    formats = {12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    format_string = formats.get(len(token))
    if format_string is None or not token.isascii() or not token.isdigit():
        raise AutoMatchError(
            "The official ALL_SOURCES1 text catalog has an invalid generation token."
        )
    try:
        generated_epoch = int(
            datetime.strptime(token, format_string)
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError as exc:
        raise AutoMatchError(
            "The official ALL_SOURCES1 text catalog has an invalid generation token."
        ) from exc
    return generated_epoch


def _validate_text_catalog_generation(generated_token: str, *, now_epoch: int) -> int:
    """Return the TXT generation epoch only when it is current enough to trust."""

    generated_epoch = _text_catalog_generation_epoch(generated_token)
    age = int(now_epoch) - generated_epoch
    if (
        age > MAX_TEXT_CATALOG_AGE_SECONDS
        or age < -MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS
    ):
        raise AutoMatchError(
            "The official ALL_SOURCES1 text catalog generation is not current."
        )
    return generated_epoch


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


def _coverage_fallback_marker(row: Mapping[str, Any]) -> str:
    """Return an audit-only local marker for a per-stream synthetic guide.

    The production builder keys synthetic schedules by server/stream identity,
    not by this value.  A descriptive local marker keeps the Mapping truthful
    and makes aggregate audits useful without pretending that EPGShare
    supplied a real schedule.
    """

    channel_name = streaming.clean_identifier(row.get("channel_name", ""), 300)
    category_name = streaming.clean_text(row.get("category_name", ""), 200)
    genre = streaming.clean_text(row.get("genre", ""), 40).casefold()
    evidence = f"{channel_name}\n{category_name}\n{genre}".casefold()
    if _matcher_has_adult_evidence(channel_name, category_name):
        family = "Adult"
    elif genre == "movies" or re.search(
        r"\b(?:movie|movies|cinema|films?)\b", evidence
    ):
        family = "Movie"
    elif genre == "music" or re.search(
        r"\b(?:music|songs?|singer|radio|concert|karaoke)\b", evidence
    ):
        family = "Music"
    elif genre == "kids" or re.search(r"\b(?:kids?|children|cartoon)\b", evidence):
        family = "Kids"
    elif genre == "news" or re.search(r"\bnews\b", evidence):
        family = "News"
    elif genre == "weather" or re.search(r"\bweather\b", evidence):
        family = "Weather"
    elif genre == "religion" or re.search(
        r"\b(?:religion|religious|faith|church|spiritual)\b", evidence
    ):
        family = "Religion"
    elif genre == "shopping" or re.search(r"\b(?:shopping|shop|retail)\b", evidence):
        family = "Shopping"
    elif genre == "sports" or re.search(
        r"\b(?:sport|sports|ppv|live events?|espn\+|flo(?:sports)?|ufc|wwe)\b",
        evidence,
    ):
        family = "Event"
    else:
        family = "Channel"
    return f"Synthetic.{family}.local"


def _strict_auto_map_v1_provenance(row: Mapping[str, Any]) -> bool:
    """Accept only a complete current-build machine provenance prefix."""

    notes = streaming.clean_text(row.get("notes", ""), 2_000)
    match = _AUTO_MAP_V1_PROVENANCE_RE.match(notes)
    if match is None:
        return False
    values = match.groupdict()
    return (
        values["matcher"] == STRICT_MATCHER_VERSION
        and values["matcher_build"] == STRICT_MATCHER_BUILD_ID
        and values["matcher_sha256"] == STRICT_MATCHER_SOURCE_SHA256
        and values["engine_sha256"] == STRICT_ENGINE_SOURCE_SHA256
        and values["catalog_sha256"] == values["source_sha256"]
    )


def _exact_legacy_server1_migration(row: Mapping[str, Any]) -> bool:
    """Recognize only the fixed Version 1 Server 1 migration preimage."""

    return (
        streaming.normalize_server_id(row.get("server_id", "")) == "server_1"
        and streaming.clean_text(row.get("source", ""), 40).casefold() == "panel"
        and streaming.clean_text(row.get("epg_feed", ""), 80).casefold()
        in {"panel", "server xmltv.php"}
        and streaming.clean_text(row.get("reason", ""), 500)
        == _LEGACY_SERVER1_REASON
        and streaming.clean_text(row.get("notes", ""), 2_000)
        in _LEGACY_SERVER1_NOTES
    )


def _coverage_fallback_rollback_record(
    row: Mapping[str, Any],
) -> str | None:
    """Encode the exact prior target in at most one 500-character Sheet cell."""

    values = tuple(str(row.get(field, "") or "") for field in (
        "source",
        "epg_feed",
        "epg_id",
    ))
    lengths = ":".join(str(len(value)) for value in values)
    record = f"coverage-fallback-rollback-v1 {lengths}:" + "".join(values)
    return record if len(record) <= 500 else None


def _parse_coverage_fallback_rollback_record(
    value: object,
) -> tuple[str, str, str] | None:
    """Decode a rollback record without delimiter ambiguity."""

    text = str(value or "")
    prefix = "coverage-fallback-rollback-v1 "
    if not text.startswith(prefix):
        return None
    remainder = text[len(prefix):]
    parts = remainder.split(":", 3)
    if len(parts) != 4 or any(not part.isdigit() for part in parts[:3]):
        return None
    lengths = tuple(int(part) for part in parts[:3])
    payload = parts[3]
    if sum(lengths) != len(payload):
        return None
    offset = 0
    decoded: list[str] = []
    for length in lengths:
        decoded.append(payload[offset:offset + length])
        offset += length
    return decoded[0], decoded[1], decoded[2]


def _coverage_fallback_prior_target_sha256(row: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for field in ("source", "epg_feed", "epg_id"):
        encoded = str(row.get(field, "") or "").encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _coverage_fallback_classification(
    *,
    key: tuple[str, str],
    original: Mapping[str, Any],
    patched: Mapping[str, Any],
    proposal: Any,
    quarantined_keys: frozenset[tuple[str, str]],
) -> str:
    """Classify one row at the synthetic fallback safety boundary."""

    original_action = streaming.clean_text(original.get("action", ""), 40).upper()
    patched_action = streaming.clean_text(patched.get("action", ""), 40).upper()
    if original_action != "REVIEW" or patched_action != "REVIEW":
        return "ineligible"
    if _mapping_is_enabled(original, original_action) or key in quarantined_keys:
        return "ineligible"
    channel_name = streaming.clean_identifier(original.get("channel_name", ""), 300)
    if not channel_name or _matcher_is_decorative_heading(channel_name):
        return "ineligible"
    proposal_action = streaming.clean_text(
        getattr(proposal, "matcher_action", ""), 40
    ).upper()
    if proposal_action == "IGNORE":
        return "ineligible"
    if _coverage_fallback_rollback_record(original) is None:
        return "protected_manual"

    epg_id = streaming.clean_identifier(original.get("epg_id", ""), 300)
    if not epg_id:
        return "blank"
    source = streaming.clean_text(original.get("source", ""), 40).casefold()
    # Rows reaching this boundary through the normal sync selector with a
    # Server 2/3 panel source are exact current native candidates. They must
    # remain available to the native schedule verifier and are never replaced.
    if key[0] in {"server_2", "server_3"} and source == "panel":
        return "protected_native"
    if _strict_auto_map_v1_provenance(original):
        return "machine_prefilled"
    if _exact_legacy_server1_migration(original):
        return "legacy_server1"
    return "protected_manual"


def _coverage_fallback_candidate(
    *,
    key: tuple[str, str],
    original: Mapping[str, Any],
    patched: Mapping[str, Any],
    proposal: Any,
    quarantined_keys: frozenset[tuple[str, str]],
) -> bool:
    """Return whether a row may receive the opt-in local synthetic guide."""

    return _coverage_fallback_classification(
        key=key,
        original=original,
        patched=patched,
        proposal=proposal,
        quarantined_keys=quarantined_keys,
    ) in {"blank", "machine_prefilled", "legacy_server1"}


def _coverage_fallback_binding_sha256(
    *,
    key: tuple[str, str],
    row: Mapping[str, Any],
    marker: str,
    source_sha256: str,
    prior_target_sha256: str,
) -> str:
    digest = hashlib.sha256()
    for value in (
        "coverage-fallback-v1",
        key[0],
        key[1],
        streaming.clean_identifier(row.get("channel_name", ""), 300),
        streaming.clean_text(row.get("category_name", ""), 200),
        marker,
        source_sha256,
        prior_target_sha256,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _apply_coverage_fallback(
    *,
    key: tuple[str, str],
    original: Mapping[str, Any],
    patched: dict[str, str],
    source_sha256: str,
) -> None:
    marker = _coverage_fallback_marker(original)
    rollback = _coverage_fallback_rollback_record(original)
    if rollback is None:
        raise AutoMatchError(
            "A coverage fallback could not preserve its exact rollback target."
        )
    prior_target_sha256 = _coverage_fallback_prior_target_sha256(original)
    binding = _coverage_fallback_binding_sha256(
        key=key,
        row=original,
        marker=marker,
        source_sha256=source_sha256,
        prior_target_sha256=prior_target_sha256,
    )
    provenance = (
        "coverage-fallback-v1 source=local-synthetic; "
        f"marker={marker}; epg_checked_sha256={source_sha256}; "
        f"prior_target_sha256={prior_target_sha256}; "
        f"binding_sha256={binding} | auto-map-v1 method=coverage_fallback"
    )
    existing_notes = streaming.clean_text(original.get("notes", ""), 500)
    patched.update(
        {
            "action": "AUTO_DUMMY",
            "enabled": "TRUE",
            "source": "dummy",
            "epg_feed": "DUMMY_CHANNELS",
            "epg_id": marker,
            "reason": rollback,
            # Provenance is retained first if the legacy discovery note would
            # exceed the Sheet's bounded notes cell.
            "notes": streaming.clean_text(
                f"{provenance} | {existing_notes}" if existing_notes else provenance,
                500,
            ),
        }
    )
    expected_prior = tuple(
        str(original.get(field, "") or "")
        for field in ("source", "epg_feed", "epg_id")
    )
    if _parse_coverage_fallback_rollback_record(patched["reason"]) != expected_prior:
        raise AutoMatchError(
            "A coverage fallback failed to retain its exact rollback target."
        )


def _previous_ai_review_keys(
    review_keys: Iterable[tuple[str, str]],
    rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> frozenset[tuple[str, str]]:
    """Return rows already finalized by the strict dual-verifier AI gate.

    Legacy ``ai-review-v1`` notes were advisory-only.  Excluding those rows
    forever would make an old abstention or suggestion impossible to improve
    after the catalog, Smart Rules, or clustering policy changes.  The rotated
    bounded queue is the rate limiter; only a valid v2 approval is terminal.
    """

    return frozenset(
        key
        for key in review_keys
        if "ai-verified-v2" in str(rows_by_key[key].get("notes", "")).casefold()
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


def _interleave_coverage_fallback_lanes(
    new_keys: Sequence[tuple[str, str]],
    review_keys: Sequence[tuple[str, str]],
    *,
    rotation: int,
) -> tuple[tuple[str, str], ...]:
    """Fairly merge new and existing-REVIEW fallback queues.

    Each input is already rotated and round-robin across servers. Alternating
    the two populations prevents a large new-channel discovery from consuming
    every shared synthetic slot. The first population also rotates by day so a
    one-row allowance cannot permanently favor the same lane.
    """

    lanes = [tuple(new_keys), tuple(review_keys)]
    if rotation % 2:
        lanes.reverse()
    ordered: list[tuple[str, str]] = []
    maximum = max((len(lane) for lane in lanes), default=0)
    for index in range(maximum):
        for lane in lanes:
            if index < len(lane):
                ordered.append(lane[index])
    return tuple(ordered)


def _stage_ai_review_shortlists_with_metrics(
    *,
    resolver: Any | None = None,
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

    contextual_resolver = (
        resolver
        if resolver is not None
        and callable(getattr(resolver, "_compatible", None))
        and callable(getattr(resolver, "_fuzzy_score", None))
        else None
    )
    candidates_by_market: dict[str, list[dict[str, Any]]] = {}
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
            "context": (
                parse_candidate_context_v8(contextual_resolver.engine, raw)
                if contextual_resolver is not None
                else None
            ),
        }
        candidates_by_market.setdefault(region, []).append(candidate)

    # Preserve every corroborated identity in the ranking universe, including
    # two IDs with the same normalized display name.  Their tie is material
    # negative evidence: both rankers must retain it so the independent margin
    # policy abstains.  Deleting the tied leaders here could promote a weaker
    # third candidate into a manufactured unique winner.

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
        query_context = None
        if contextual_resolver is not None:
            try:
                query_context = parse_channel_context_v8(
                    contextual_resolver.engine,
                    proposal.channel_name,
                    proposal.category_name,
                )
            except Exception as exc:
                raise AutoMatchError(
                    "The Smart Rules AI-review context could not be parsed."
                ) from exc
        ranked: list[tuple[float, float, str, str, dict[str, Any]]] = []
        for candidate in market_candidates:
            comparisons += 1
            target_context = candidate["context"]
            if (
                contextual_resolver is not None
                and not contextual_resolver._compatible(
                    query_context, target_context
                )
            ):
                continue
            name_score = float(fuzz.WRatio(query, candidate["normalized"]))
            contextual_score = (
                float(
                    contextual_resolver._fuzzy_score(
                        query_context, target_context
                    )
                )
                if contextual_resolver is not None
                else name_score
            )
            if (
                name_score < MIN_AI_REVIEW_FUZZY_SCORE
                or contextual_score < MIN_AI_REVIEW_FUZZY_SCORE
            ):
                continue
            ranked.append(
                (
                    contextual_score,
                    name_score,
                    candidate["normalized"],
                    candidate["epg_id"],
                    candidate,
                )
            )
        # Use the exact rounded scores and ID tie-break used by the independent
        # approval policy.  Selecting with extra secondary sort fields here
        # could otherwise omit the policy's real runner-up after rounding.
        by_context = sorted(
            ranked,
            key=lambda item: (
                -int(round(item[0])),
                item[3].casefold(),
                item[3],
            ),
        )
        by_name = sorted(
            ranked,
            key=lambda item: (
                -int(round(item[1])),
                item[3].casefold(),
                item[3],
            ),
        )

        # Form a genuinely bounded union of both independent rankings.  Each
        # ranker's top and runner-up are reserved first; remaining capacity is
        # filled round-robin.  A context-first fill could consume all eight
        # slots, hide the name runner-up, and manufacture an inflated margin.
        selected: list[tuple[float, float, str, str, dict[str, Any]]] = []
        selected_ids: set[str] = set()

        def add_selected(item: tuple[float, float, str, str, dict[str, Any]]) -> None:
            epg_id = item[3]
            if epg_id in selected_ids:
                return
            selected.append(item)
            selected_ids.add(epg_id)

        for ranking in (by_context, by_name):
            for item in ranking[:2]:
                add_selected(item)

        rank_index = 2
        while (
            len(selected) < MAX_AI_REVIEW_CANDIDATES
            and rank_index < max(len(by_context), len(by_name))
        ):
            for ranking in (by_context, by_name):
                if rank_index < len(ranking):
                    add_selected(ranking[rank_index])
                if len(selected) >= MAX_AI_REVIEW_CANDIDATES:
                    break
            rank_index += 1

        # Identical/overlapping rankings can leave room after the balanced
        # pass only when one ranking is shorter.  Fill deterministically from
        # the complete rankings without weakening the reserved top-two rule.
        if len(selected) < MAX_AI_REVIEW_CANDIDATES:
            for item in (*by_context, *by_name):
                add_selected(item)
                if len(selected) >= MAX_AI_REVIEW_CANDIDATES:
                    break
        if len(selected) > MAX_AI_REVIEW_CANDIDATES:
            # Defensive invariant for future constant changes.
            selected = selected[:MAX_AI_REVIEW_CANDIDATES]
        if len(selected) < MIN_AI_REVIEW_CANDIDATES:
            continue
        shortlist_candidates = tuple(
            AiReviewCandidate(
                candidate_key=f"c{index:02d}",
                epg_id=candidate["epg_id"],
                display_name=candidate["display_name"],
                feed=candidate["feed"],
                region=candidate["region"],
                local_score=int(round(name_score)),
                contextual_score=int(round(contextual_score)),
                direction=str(
                    getattr(candidate["context"], "direction", "") or ""
                ),
                timeshift=str(
                    getattr(candidate["context"], "timeshift", "") or ""
                ),
                has_plus=bool(
                    getattr(candidate["context"], "has_plus", False)
                ),
                has_extra=bool(
                    getattr(candidate["context"], "has_extra", False)
                ),
                has_alternate=bool(
                    getattr(candidate["context"], "has_alternate", False)
                ),
                numbers=tuple(
                    sorted(getattr(candidate["context"], "numbers", ()) or ())
                ),
                languages=tuple(
                    sorted(getattr(candidate["context"], "languages", ()) or ())
                ),
                content=tuple(
                    sorted(getattr(candidate["context"], "content", ()) or ())
                ),
            )
            for index, (
                contextual_score,
                name_score,
                _normalized,
                _epg_id,
                candidate,
            ) in enumerate(
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
                normalized_name=query,
                normalized_category=catalog_stream._simple_match_key(
                    proposal.category_name
                ),
                strict_identity=str(
                    getattr(query_context, "strict_key", query) or query
                ),
                bag_identity=str(
                    getattr(query_context, "bag_key", query) or query
                ),
                route_plan=route_plan,
                route_explicit=bool(proposal.route_explicit),
                direction=str(
                    getattr(query_context, "direction", "") or ""
                ),
                timeshift=str(
                    getattr(query_context, "timeshift", "") or ""
                ),
                has_plus=bool(getattr(query_context, "has_plus", False)),
                has_extra=bool(getattr(query_context, "has_extra", False)),
                has_alternate=bool(
                    getattr(query_context, "has_alternate", False)
                ),
                numbers=tuple(
                    sorted(getattr(query_context, "numbers", ()) or ())
                ),
                languages=tuple(
                    sorted(getattr(query_context, "languages", ()) or ())
                ),
                content=tuple(
                    sorted(getattr(query_context, "content", ()) or ())
                ),
                smart_epg_id=(
                    by_context[0][3]
                    if by_context
                    and by_name
                    and by_context[0][3] == by_name[0][3]
                    else ""
                ),
                source_sha256=str(
                    getattr(proposal, "catalog_source_sha256", "") or ""
                ),
            )
        )
    return _AiReviewStaging(
        shortlists=tuple(shortlists),
        attempted_rows=attempted_rows,
        comparisons=comparisons,
    )


def _stage_ai_review_shortlists(
    *,
    resolver: Any | None = None,
    proposals: Mapping[tuple[str, str], Any],
    review_keys: Iterable[tuple[str, str]],
    ai_excluded_keys: Iterable[tuple[str, str]] = (),
    corroborated_real_candidates: Sequence[Mapping[str, str]],
    rotation: int = 0,
) -> tuple[AiReviewShortlist, ...]:
    """Compatibility wrapper returning only bounded shortlist rows."""

    return _stage_ai_review_shortlists_with_metrics(
        resolver=resolver,
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
    checked_at_epoch: int,
    source_sha256: str,
    text_catalog_file_sha256: str,
    text_catalog_fingerprint_sha256: str,
    text_catalog_generated_token: str,
) -> tuple[AiReviewShortlist, ...]:
    """Keep only same-snapshot candidates with the strong programme gate."""

    if (
        _SHA256_RE.fullmatch(str(source_sha256)) is None
        or _SHA256_RE.fullmatch(str(text_catalog_file_sha256)) is None
        or _SHA256_RE.fullmatch(str(text_catalog_fingerprint_sha256)) is None
    ):
        raise AutoMatchError("AI shortlist source provenance is invalid.")
    _validate_text_catalog_generation(
        text_catalog_generated_token,
        now_epoch=int(checked_at_epoch),
    )

    result: list[AiReviewShortlist] = []
    for shortlist in staged:
        if shortlist.key not in unresolved_keys:
            continue
        # Programme availability is a gate, never another ranking pass.  Bind
        # the decision to the original top and runner-up under both rankers so
        # removing a stronger candidate cannot promote a weaker survivor or
        # enlarge either approval margin.
        prefilter_name_ranked = sorted(
            shortlist.candidates,
            key=lambda candidate: (
                -candidate.local_score,
                candidate.epg_id.casefold(),
                candidate.epg_id,
            ),
        )
        prefilter_contextual_ranked = sorted(
            shortlist.candidates,
            key=lambda candidate: (
                -candidate.contextual_score,
                candidate.epg_id.casefold(),
                candidate.epg_id,
            ),
        )
        if (
            len(prefilter_name_ranked) < MIN_AI_REVIEW_CANDIDATES
            or len(prefilter_contextual_ranked) < MIN_AI_REVIEW_CANDIDATES
        ):
            continue
        prefilter_smart_epg_id = (
            prefilter_name_ranked[0].epg_id
            if prefilter_name_ranked[0].epg_id
            == prefilter_contextual_ranked[0].epg_id
            else ""
        )
        if shortlist.smart_epg_id != prefilter_smart_epg_id:
            # Internal shortlist state is inconsistent with its own scores.
            continue
        material_competitor_ids = {
            candidate.epg_id
            for candidate in (
                *prefilter_name_ranked[:2],
                *prefilter_contextual_ranked[:2],
            )
        }
        viable: list[AiReviewCandidate] = []
        for candidate in shortlist.candidates:
            gate = programme_gates.get(candidate.epg_id)
            if (
                gate is None
                or not bool(gate.passed)
                or resolved_source_ids.get(candidate.epg_id) != candidate.epg_id
            ):
                continue
            viable.append(
                replace(
                    candidate,
                    programme_count=int(
                        gate.distinct_informative_programmes
                    ),
                    programme_first_start_epoch=gate.first_start_epoch,
                    programme_latest_stop_epoch=gate.latest_stop_epoch,
                    programme_checked_at_epoch=int(checked_at_epoch),
                    programme_source_sha256=str(source_sha256),
                )
            )
        if not MIN_AI_REVIEW_CANDIDATES <= len(viable) <= MAX_AI_REVIEW_CANDIDATES:
            continue
        if not material_competitor_ids.issubset(
            {candidate.epg_id for candidate in viable}
        ):
            continue
        # Reissue opaque keys after programme filtering so every shortlist is
        # contiguous and has no externally observable gap from a rejected ID.
        finalized = tuple(
            replace(candidate, candidate_key=f"c{index:02d}")
            for index, candidate in enumerate(viable, start=1)
        )
        result.append(
            replace(
                shortlist,
                candidates=finalized,
                smart_epg_id=prefilter_smart_epg_id,
                source_sha256=source_sha256,
                text_catalog_file_sha256=text_catalog_file_sha256,
                text_catalog_fingerprint_sha256=text_catalog_fingerprint_sha256,
                text_catalog_generated_token=text_catalog_generated_token,
            )
        )
    return tuple(result)


_AUTO_MAP_V2_PROVENANCE_RE = re.compile(
    r"(?:\A| \| )auto-map-v2 "
    r"method=(?P<method>[a-z0-9_]+); "
    r"market=(?P<market>[A-Z0-9_]+); "
    r"source_sha256=(?P<source_sha256>[0-9a-f]{64}); "
    r"binding_sha256=(?P<binding_sha256>[0-9a-f]{64})(?= \| |\Z)"
)
_AI_VERIFIED_V2_PROVENANCE_RE = re.compile(
    r"(?:\A| \| )ai-verified-v2 "
    r"gate=(?P<gate>[a-z0-9+_-]+); "
    r"method=(?P<method>[a-z0-9_]+); "
    r"market=(?P<market>[A-Z0-9_]+); "
    r"source_sha256=(?P<source_sha256>[0-9a-f]{64}); "
    r"text_file_sha256=(?P<text_file_sha256>[0-9a-f]{64}); "
    r"text_fingerprint_sha256=(?P<text_fingerprint_sha256>[0-9a-f]{64}); "
    r"text_generated=(?P<text_generated>[0-9]{12}(?:[0-9]{2})?); "
    r"binding_sha256=(?P<binding_sha256>[0-9a-f]{64})(?= \| |\Z)"
)


def _digest_fields(*values: object) -> str:
    """Hash an unambiguous sequence of provenance fields."""

    digest = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _ai_verified_v2_binding(
    row: Mapping[str, Any],
    *,
    match_method: str,
    market: str,
    source_sha256: str,
    text_catalog_file_sha256: str,
    text_catalog_fingerprint_sha256: str,
    text_catalog_generated_token: str,
) -> str:
    """Bind a future dual-gate AI approval to one exact private Sheet row.

    This is deliberately a tamper-evident preimage, not an authorization
    signature. The private Mappings sheet remains the trusted durable store;
    the binding prevents a provenance marker from being copied to a different
    stream, label, market, or target by accident.
    """

    server_id, stream_id = _canonical_key(
        row.get("server_id", ""), row.get("stream_id", "")
    )
    method = streaming.clean_text(match_method, 80).casefold()
    normalized_market = _matcher_market_code(market)
    source_hash = streaming.clean_identifier(source_sha256, 64).casefold()
    text_file_hash = streaming.clean_identifier(
        text_catalog_file_sha256, 64
    ).casefold()
    text_fingerprint_hash = streaming.clean_identifier(
        text_catalog_fingerprint_sha256, 64
    ).casefold()
    text_generated = streaming.clean_identifier(
        text_catalog_generated_token, 14
    )
    epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
    if (
        method not in _AI_VERIFIED_METHODS
        or normalized_market in {"", "ALL", "UNKNOWN", "AMBIGUOUS"}
        or not epg_id
        or _SHA256_RE.fullmatch(source_hash) is None
        or _SHA256_RE.fullmatch(text_file_hash) is None
        or _SHA256_RE.fullmatch(text_fingerprint_hash) is None
        or re.fullmatch(r"[0-9]{12}(?:[0-9]{2})?", text_generated) is None
    ):
        raise AutoMatchError("The ai-verified-v2 provenance fields are invalid.")
    _text_catalog_generation_epoch(text_generated)
    return _digest_fields(
        "ai-verified-v2",
        _AI_VERIFIED_V2_GATE,
        server_id,
        stream_id,
        streaming.clean_identifier(row.get("channel_name", ""), 300),
        streaming.clean_text(row.get("category_name", ""), 200),
        epg_id,
        method,
        normalized_market,
        source_hash,
        text_file_hash,
        text_fingerprint_hash,
        text_generated,
        STRICT_MATCHER_VERSION,
        STRICT_MATCHER_BUILD_ID,
        STRICT_MATCHER_SOURCE_SHA256,
        STRICT_ENGINE_SOURCE_SHA256,
    )


def _ai_verified_v2_provenance_note(
    row: Mapping[str, Any],
    *,
    match_method: str,
    market: str,
    source_sha256: str,
    text_catalog_file_sha256: str,
    text_catalog_fingerprint_sha256: str,
    text_catalog_generated_token: str,
) -> str:
    """Return the exact compact note format reserved for a future v2 gate.

    The future writer must call this only after the deterministic Smart-Rules
    target and Gemini HIGH target are identical and the exact catalog,
    programme, and market checks pass. Merely typing ``ai-verified-v2`` into a
    Sheet note is intentionally insufficient for learned memory.
    """

    method = streaming.clean_text(match_method, 80).casefold()
    normalized_market = _matcher_market_code(market)
    source_hash = streaming.clean_identifier(source_sha256, 64).casefold()
    text_file_hash = streaming.clean_identifier(
        text_catalog_file_sha256, 64
    ).casefold()
    text_fingerprint_hash = streaming.clean_identifier(
        text_catalog_fingerprint_sha256, 64
    ).casefold()
    text_generated = streaming.clean_identifier(
        text_catalog_generated_token, 14
    )
    binding = _ai_verified_v2_binding(
        row,
        match_method=method,
        market=normalized_market,
        source_sha256=source_hash,
        text_catalog_file_sha256=text_file_hash,
        text_catalog_fingerprint_sha256=text_fingerprint_hash,
        text_catalog_generated_token=text_generated,
    )
    return (
        f"ai-verified-v2 gate={_AI_VERIFIED_V2_GATE}; "
        f"method={method}; market={normalized_market}; "
        f"source_sha256={source_hash}; "
        f"text_file_sha256={text_file_hash}; "
        f"text_fingerprint_sha256={text_fingerprint_hash}; "
        f"text_generated={text_generated}; "
        f"binding_sha256={binding}"
    )


def _automatic_memory_provenance(
    row: Mapping[str, Any],
    *,
    expected_market: str,
) -> str | None:
    """Return the verified automatic evidence tier, or fail closed.

    Deterministic and AI v2 evidence both bind their complete preimage to the
    exact private mapping row.  Historical unbound ``auto-map-v1`` records,
    suggestion-only ``ai-review-v1`` notes and partial/forged v2 markers never
    become matcher knowledge.
    """

    notes = streaming.clean_text(row.get("notes", ""), 2000)
    normalized_market = _matcher_market_code(expected_market)

    auto_matches = tuple(_AUTO_MAP_V2_PROVENANCE_RE.finditer(notes))
    if notes.count("auto-map-v2") == 1 and len(auto_matches) == 1:
        fields = auto_matches[0].groupdict()
        if (
            fields["method"] in SAFE_REAL_METHODS
            and _matcher_market_code(fields["market"]) == normalized_market
            and _SHA256_RE.fullmatch(fields["source_sha256"]) is not None
        ):
            try:
                expected_binding = automatic_mapping_binding_sha256(
                    server_id=row.get("server_id", ""),
                    stream_id=row.get("stream_id", ""),
                    channel_name=streaming.clean_identifier(
                        row.get("channel_name", ""), 300
                    ),
                    category_name=streaming.clean_text(
                        row.get("category_name", ""), 200
                    ),
                    epg_id=streaming.clean_identifier(row.get("epg_id", ""), 300),
                    match_method=fields["method"],
                    market=fields["market"],
                    source_sha256=fields["source_sha256"],
                )
            except (streaming.BuildError, ValueError):
                expected_binding = ""
            if fields["binding_sha256"] == expected_binding:
                return "deterministic_bound_v2"

    ai_matches = tuple(_AI_VERIFIED_V2_PROVENANCE_RE.finditer(notes))
    if notes.count("ai-verified-v2") == 1 and len(ai_matches) == 1:
        fields = ai_matches[0].groupdict()
        if (
            fields["gate"] == _AI_VERIFIED_V2_GATE
            and fields["method"] in _AI_VERIFIED_METHODS
            and _matcher_market_code(fields["market"]) == normalized_market
            and _SHA256_RE.fullmatch(fields["source_sha256"]) is not None
        ):
            try:
                expected_binding = _ai_verified_v2_binding(
                    row,
                    match_method=fields["method"],
                    market=fields["market"],
                    source_sha256=fields["source_sha256"],
                    text_catalog_file_sha256=fields["text_file_sha256"],
                    text_catalog_fingerprint_sha256=(
                        fields["text_fingerprint_sha256"]
                    ),
                    text_catalog_generated_token=fields["text_generated"],
                )
            except (AutoMatchError, streaming.BuildError, ValueError):
                expected_binding = ""
            if fields["binding_sha256"] == expected_binding:
                return "ai_verified_v2"
    return None


def _learn_cross_server_approved_aliases(
    *,
    resolver: Any,
    mapping_rows: Sequence[Mapping[str, Any]],
    corroborated_real_candidates: Sequence[Mapping[str, str]],
    quarantined_keys: frozenset[tuple[str, str]],
    current_unchanged_keys: frozenset[tuple[str, str]],
) -> _LearnedAliasMemory:
    """Rebuild safe durable alias memory from the private Mappings rows.

    The Sheet is already the persistent authority, so no learned file is
    written to the public repository. A current human ``MANUAL``/``APPROVED``
    row may teach one exact alias/market target. Automatic evidence is weaker:
    it needs strong deterministic or dual-gate AI provenance and the identical
    target on at least two distinct current server identities. Every target is
    revalidated against this run's exact corroborated real catalog. Conflicts
    reject the complete alias/market group, while pinned static rules are never
    overwritten.
    """

    candidates_by_id: dict[str, list[Mapping[str, str]]] = {}
    case_variants: dict[str, set[str]] = {}
    for candidate in corroborated_real_candidates:
        epg_id = str(candidate.get("epg_id") or "")
        if not epg_id:
            continue
        candidates_by_id.setdefault(epg_id, []).append(candidate)
        case_variants.setdefault(epg_id.casefold(), set()).add(epg_id)

    evidence: dict[
        tuple[str, str],
        dict[str, dict[str, set[str]]],
    ] = {}
    evidence_support_keys: dict[
        tuple[str, str, str], set[tuple[str, str]]
    ] = {}
    support_rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    evidence_rows = 0
    for row in mapping_rows:
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        source = streaming.clean_text(row.get("source", ""), 40).casefold()
        if action not in _HUMAN_MEMORY_ACTIONS.union({_AUTOMATIC_MEMORY_ACTION}) or source not in {
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
        route_plan = tuple(
            _matcher_market_code(value)
            for value in tuple(getattr(context, "route_plan", ()) or ())
        )
        explicit_market = _matcher_market_code(
            getattr(context, "explicit_market", "")
        )
        if (
            not alias
            or not bool(getattr(context, "route_explicit", False))
            or explicit_market in {"", "ALL", "UNKNOWN", "AMBIGUOUS"}
        ):
            continue
        region = next(iter(regions))
        if region not in route_plan:
            # An exact alias is not enough when the approved target belongs to
            # a market outside the provider label's current deterministic route.
            continue
        if action in _HUMAN_MEMORY_ACTIONS:
            tier = "human"
        else:
            tier = _automatic_memory_provenance(
                row,
                expected_market=explicit_market,
            )
            if tier is None:
                continue
        target_evidence = evidence.setdefault((alias, region), {}).setdefault(
            epg_id,
            {
                "human_servers": set(),
                "automatic_servers": set(),
                "automatic_tiers": set(),
            },
        )
        if tier == "human":
            target_evidence["human_servers"].add(key[0])
        else:
            target_evidence["automatic_servers"].add(key[0])
            target_evidence["automatic_tiers"].add(tier)
        evidence_support_keys.setdefault((alias, region, epg_id), set()).add(key)
        support_rows_by_key[key] = {
            column: str(row.get(column, ""))
            for column in streaming.SHEET_COLUMNS
        }
        evidence_rows += 1

    static_aliases = tuple(getattr(resolver, "approved_aliases", ()) or ())
    accepted: list[dict[str, str]] = []
    accepted_support_keys: set[tuple[str, str]] = set()
    static_reused_groups = 0
    rejected_groups = 0
    for (alias, region), targets in sorted(evidence.items()):
        # One exact alias/market spelling must point to one exact schedule.
        # Weak or unknown rows never enter ``evidence`` and therefore cannot
        # poison a legitimate group merely by containing a suggestive marker.
        if len(targets) != 1:
            rejected_groups += 1
            continue
        epg_id, support = next(iter(targets.items()))
        human_servers = support["human_servers"]
        automatic_servers = support["automatic_servers"]
        if human_servers:
            relationship = "durable_human_approved_memory"
            evidence_note = (
                "Rebuilt from a current human-approved private Mapping and "
                "revalidated against the current corroborated catalog."
            )
        elif len(automatic_servers) >= 2:
            relationship = "durable_automatic_verified_memory"
            evidence_note = (
                "Rebuilt from the same strongly proven automatic target on at "
                "least two distinct current server identities and revalidated "
                "against the current corroborated catalog."
            )
        else:
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
                "relationship": relationship,
                "note": evidence_note,
            }
        )
        accepted_support_keys.update(
            evidence_support_keys.get((alias, region, epg_id), ())
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
        support_rows=tuple(
            support_rows_by_key[key]
            for key in sorted(
                accepted_support_keys,
                key=lambda item: (
                    item[0],
                    streaming.stream_sort_key(item[1]),
                ),
            )
        ),
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
    coverage_fallback_limit: int = 0,
    include_new_coverage_fallback: bool = True,
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
    if not isinstance(include_new_coverage_fallback, bool):
        raise AutoMatchError(
            "include_new_coverage_fallback must be exactly true or false."
        )
    if (
        isinstance(coverage_fallback_limit, bool)
        or not isinstance(coverage_fallback_limit, int)
        or not 0 <= coverage_fallback_limit <= MAX_COVERAGE_FALLBACK_ROWS
    ):
        raise AutoMatchError(
            "Coverage fallback limit must be an integer from 0 to "
            f"{MAX_COVERAGE_FALLBACK_ROWS:,}."
        )
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
    now_epoch = _timestamp_epoch(generated_at)
    try:
        text_catalog_bytes, text_catalog_file_sha256 = _read_bound_text_catalog(
            text_catalog_path
        )
        text_catalog = catalog_stream.parse_all_sources_text(
            text_catalog_bytes,
            minimum_ids=int(minimum_unique_channels),
        )
        text_catalog.validate_for_unattended_matching()
        _validate_text_catalog_generation(
            text_catalog.generated_token,
            now_epoch=now_epoch,
        )
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
    verification_candidates_box: list[
        tuple[VerificationCatalogCandidateEvidence, ...]
    ] = []
    coverage_fallback_candidate_keys: frozenset[tuple[str, str]] = frozenset()
    coverage_fallback_keys: frozenset[tuple[str, str]] = frozenset()
    coverage_fallback_ai_deferred_keys: frozenset[tuple[str, str]] = frozenset()
    coverage_fallback_suppressed_new_keys: frozenset[
        tuple[str, str]
    ] = frozenset()
    coverage_fallback_machine_prefilled_keys: frozenset[
        tuple[str, str]
    ] = frozenset()
    coverage_fallback_legacy_server1_keys: frozenset[
        tuple[str, str]
    ] = frozenset()
    coverage_fallback_protected_manual_keys: frozenset[
        tuple[str, str]
    ] = frozenset()
    coverage_fallback_protected_native_keys: frozenset[
        tuple[str, str]
    ] = frozenset()

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
        verification_candidates: list[VerificationCatalogCandidateEvidence] = []
        for candidate in sorted(
            corroborated_real_candidates,
            key=lambda value: (
                str(value.get("epg_id") or "").casefold(),
                str(value.get("epg_id") or ""),
            ),
        ):
            context = parse_candidate_context_v8(runtime.resolver.engine, candidate)
            verification_candidates.append(
                VerificationCatalogCandidateEvidence(
                    epg_id=str(candidate.get("epg_id") or ""),
                    market=_matcher_market_code(candidate.get("region", "")),
                    feed=str(candidate.get("feed") or "").strip().upper(),
                    semantics=VerificationSemanticsEvidence(
                        direction=str(context.direction or ""),
                        timeshift=str(context.timeshift or ""),
                        has_plus=bool(context.has_plus),
                        has_extra=bool(context.has_extra),
                        has_alternate=bool(context.has_alternate),
                        numbers=tuple(sorted(context.numbers)),
                        languages=tuple(sorted(context.languages)),
                        content=tuple(sorted(context.content)),
                    ),
                    is_real=True,
                )
            )
        verification_candidates_box.append(tuple(verification_candidates))
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
        same_market_family_ambiguity = _same_market_family_ambiguity_ids(runtime)
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
                key: _apply_catalog_ambiguity_veto(
                    proposal,
                    all_route_labels=all_route_ambiguity,
                    same_market_ids=same_market_family_ambiguity,
                )
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
                resolver=runtime.resolver,
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
        # Gemini cannot resolve either a routed target shadowed by an unscoped
        # ALL-market identity or two official schedules that collapse to the
        # same coarse station family.  Those competitors are deliberately
        # absent from the explicit-market shortlist, so allowing its Smart top
        # choice through would manufacture a false local margin and bypass the
        # deterministic catalog veto above.  Only explicit human/cross-server
        # memory may disambiguate either family on a future run.
        staged_shortlists = _apply_ai_shortlist_catalog_ambiguity_veto(
            staged_shortlists,
            all_route_labels=all_route_ambiguity,
            same_market_ids=same_market_family_ambiguity,
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
                or len(verification_candidates_box) != 1
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
                    action = row.get("action")
                    if action == "AUTO_EPGSHARE":
                        if (
                            row.get("source") != "epgshare01"
                            or row.get("epg_feed") != "ALL_SOURCES1"
                            or row.get("enabled") != "TRUE"
                            or not row.get("epg_id")
                        ):
                            raise AutoMatchError(
                                "An approved real match violated the EPGShare policy."
                            )
                        approved_ids.add(row["epg_id"])
                    elif action == "AUTO_DUMMY":
                        if (
                            row.get("source") != "dummy"
                            or row.get("enabled") != "TRUE"
                            or not row.get("epg_id")
                        ):
                            raise AutoMatchError(
                                "An approved placeholder violated the dummy-source policy."
                            )
                    else:
                        raise AutoMatchError(
                            "An approved match returned an unsupported action."
                        )
                    approved_count += 1
                    approved_by_server[key[0]] += 1
                else:
                    # Decorative headings are deliberately classified as
                    # IGNORE so they leave the unresolved queue. Every other
                    # unapproved proposal remains disabled REVIEW.
                    if row.get("action") != "IGNORE":
                        row["action"] = "REVIEW"
                    row["enabled"] = "FALSE"
                if any(
                    row.get(column, "") != metadata_before[column]
                    for column in _METADATA_COLUMNS
                ):
                    raise AutoMatchError("Automatic EPG matching attempted to approve metadata.")
                patched_rows.append(row)

            patched_by_key_during_run = {
                _canonical_key(
                    row.get("server_id", ""), row.get("stream_id", "")
                ): row
                for row in patched_rows
            }
            if coverage_fallback_limit:
                fallback_classifications = {
                    key: _coverage_fallback_classification(
                        key=key,
                        original=rows_by_key[key],
                        patched=patched_by_key_during_run[key],
                        proposal=proposals[key],
                        quarantined_keys=canonical_quarantined_keys,
                    )
                    for key in rows_by_key
                }
                all_candidate_keys = frozenset(
                    key
                    for key, classification in fallback_classifications.items()
                    if classification
                    in {"blank", "machine_prefilled", "legacy_server1"}
                )
                coverage_fallback_suppressed_new_keys = (
                    frozenset()
                    if include_new_coverage_fallback
                    else all_candidate_keys.intersection(new_keys)
                )
                candidate_keys = all_candidate_keys.difference(
                    coverage_fallback_suppressed_new_keys
                )
                coverage_fallback_machine_prefilled_keys = frozenset(
                    key
                    for key, classification in fallback_classifications.items()
                    if key in candidate_keys
                    and classification in {"machine_prefilled", "legacy_server1"}
                )
                coverage_fallback_legacy_server1_keys = frozenset(
                    key
                    for key, classification in fallback_classifications.items()
                    if key in candidate_keys and classification == "legacy_server1"
                )
                coverage_fallback_protected_manual_keys = frozenset(
                    key
                    for key, classification in fallback_classifications.items()
                    if classification == "protected_manual"
                )
                coverage_fallback_protected_native_keys = frozenset(
                    key
                    for key, classification in fallback_classifications.items()
                    if classification == "protected_native"
                )
                staged_ai_keys = frozenset(
                    shortlist.key
                    for shortlist in staged_shortlists_box[0].shortlists
                )
                coverage_fallback_candidate_keys = candidate_keys
                coverage_fallback_ai_deferred_keys = candidate_keys.intersection(
                    staged_ai_keys
                )
                selectable = candidate_keys.difference(
                    coverage_fallback_ai_deferred_keys
                )
                rotation = now_epoch // 86400
                ordered_fallback_keys = _interleave_coverage_fallback_lanes(
                    _rotated_round_robin_review_keys(
                        selectable.intersection(new_keys), rotation=rotation
                    ),
                    _rotated_round_robin_review_keys(
                        selectable.intersection(review_keys), rotation=rotation
                    ),
                    rotation=rotation,
                )
                coverage_fallback_keys = frozenset(
                    ordered_fallback_keys[:coverage_fallback_limit]
                )
                for key in coverage_fallback_keys:
                    row = patched_by_key_during_run[key]
                    _apply_coverage_fallback(
                        key=key,
                        original=rows_by_key[key],
                        patched=row,
                        source_sha256=result.source_sha256,
                    )
                    if (
                        row.get("action") != "AUTO_DUMMY"
                        or row.get("source") != "dummy"
                        or row.get("epg_feed") != "DUMMY_CHANNELS"
                        or row.get("enabled") != "TRUE"
                        or not row.get("epg_id", "").startswith("Synthetic.")
                    ):
                        raise AutoMatchError(
                            "A coverage fallback violated the local synthetic policy."
                        )
                    approved_count += 1
                    approved_by_server[key[0]] += 1

            # Preserve the original new-channel blast-radius policy. Existing
            # REVIEW approvals and the separately bounded local-synthetic lane
            # are capped independently by the Sheet caller.
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
                    and key not in coverage_fallback_keys
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
                    checked_at_epoch=now_epoch,
                    source_sha256=result.source_sha256,
                    text_catalog_file_sha256=text_catalog_file_sha256,
                    text_catalog_fingerprint_sha256=(
                        text_catalog.fingerprint_sha256
                    ),
                    text_catalog_generated_token=text_catalog.generated_token,
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
        if proposal.eligible_for_finalization
        and proposal.target_epg_id
    )
    real_provisional_keys = frozenset(
        key
        for key in provisional_keys
        if proposals[key].target_source == "epgshare01"
    )
    rejected_gate_keys = frozenset(
        key
        for key in real_provisional_keys
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
    new_dummy_count = sum(
        1 for key in new_keys if patched_by_key[key].get("action") == "AUTO_DUMMY"
    )
    recheck_dummy_count = sum(
        1
        for key in review_keys
        if patched_by_key[key].get("action") == "AUTO_DUMMY"
    )
    new_ignored_count = sum(
        1 for key in new_keys if patched_by_key[key].get("action") == "IGNORE"
    )
    recheck_ignored_count = sum(
        1 for key in review_keys if patched_by_key[key].get("action") == "IGNORE"
    )
    verified_placeholder_keys = frozenset(
        key for key in review_keys
        if key not in coverage_fallback_keys
        if patched_by_key[key].get("action") in {"AUTO_DUMMY", "IGNORE"}
    )
    return AutoMatchOutcome(
        rows=tuple(patched_rows),
        considered_rows=len(proposals),
        provisional_rows=len(provisional_keys),
        approved_rows=approved_count,
        review_rows=sum(
            1 for row in patched_rows if row.get("action") == "REVIEW"
        ),
        new_considered_rows=len(new_keys),
        new_provisional_rows=len(provisional_keys.intersection(new_keys)),
        new_approved_rows=new_approved_count,
        new_review_rows=sum(
            1 for key in new_keys if patched_by_key[key].get("action") == "REVIEW"
        ),
        new_rejected_programme_gates=len(rejected_gate_keys.intersection(new_keys)),
        recheck_considered_rows=len(review_keys),
        recheck_provisional_rows=len(provisional_keys.intersection(review_keys)),
        recheck_approved_rows=recheck_approved_count,
        recheck_review_rows=sum(
            1
            for key in review_keys
            if patched_by_key[key].get("action") == "REVIEW"
        ),
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
        text_catalog_file_sha256=text_catalog_file_sha256,
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
        learned_alias_support_rows=learned_alias_memory.support_rows,
        ai_review_rotation=effective_ai_review_rotation,
        ai_review_attempted_rows=staged_shortlists_box[0].attempted_rows,
        ai_review_comparisons=staged_shortlists_box[0].comparisons,
        ai_review_shortlists=finalized_shortlists_box[0],
        verification_evidence=CurrentRunVerificationEvidence(
            source_sha256=result.source_sha256,
            text_catalog_file_sha256=text_catalog_file_sha256,
            text_catalog_fingerprint_sha256=text_catalog.fingerprint_sha256,
            text_catalog_generated_token=text_catalog.generated_token,
            checked_at_epoch=now_epoch,
            xml_catalog_ids=frozenset(
                channel.epg_id for channel in result.catalog.channels
            ),
            text_catalog_ids=frozenset(
                entry.epg_id for entry in text_catalog.entries
            ),
            catalog_candidates=verification_candidates_box[0],
            programme_gates=tuple(
                VerificationProgrammeGateEvidence(
                    channel_key=gate.channel_key,
                    distinct_informative_programmes=(
                        gate.distinct_informative_programmes
                    ),
                    first_start_epoch=gate.first_start_epoch,
                    latest_stop_epoch=gate.latest_stop_epoch,
                    checked_at_epoch=now_epoch,
                    source_sha256=result.source_sha256,
                    passed=bool(gate.passed),
                    reason=str(gate.reason),
                )
                for _requested_id, gate in sorted(
                    result.programme_gates.items(),
                    key=lambda item: (item[0].casefold(), item[0]),
                )
            ),
        ),
        verified_placeholder_keys=verified_placeholder_keys,
        new_dummy_rows=new_dummy_count,
        recheck_dummy_rows=recheck_dummy_count,
        new_ignored_rows=new_ignored_count,
        recheck_ignored_rows=recheck_ignored_count,
        coverage_fallback_limit=coverage_fallback_limit,
        coverage_fallback_candidate_rows=len(coverage_fallback_candidate_keys),
        coverage_fallback_applied_rows=len(coverage_fallback_keys),
        coverage_fallback_deferred_rows=(
            len(coverage_fallback_candidate_keys) - len(coverage_fallback_keys)
        ),
        coverage_fallback_ai_deferred_rows=len(
            coverage_fallback_ai_deferred_keys
        ),
        coverage_fallback_suppressed_unwritten_new_rows=len(
            coverage_fallback_suppressed_new_keys
        ),
        new_coverage_fallback_rows=len(
            coverage_fallback_keys.intersection(new_keys)
        ),
        recheck_coverage_fallback_rows=len(
            coverage_fallback_keys.intersection(review_keys)
        ),
        coverage_fallback_machine_prefilled_candidate_rows=len(
            coverage_fallback_machine_prefilled_keys
        ),
        coverage_fallback_machine_prefilled_applied_rows=len(
            coverage_fallback_keys.intersection(
                coverage_fallback_machine_prefilled_keys
            )
        ),
        coverage_fallback_legacy_server1_candidate_rows=len(
            coverage_fallback_legacy_server1_keys
        ),
        coverage_fallback_legacy_server1_applied_rows=len(
            coverage_fallback_keys.intersection(
                coverage_fallback_legacy_server1_keys
            )
        ),
        coverage_fallback_protected_manual_rows=len(
            coverage_fallback_protected_manual_keys
        ),
        coverage_fallback_protected_native_rows=len(
            coverage_fallback_protected_native_keys
        ),
    )


__all__ = [
    "AUTO_MATCH_INTEGRATION_VERSION",
    "AiReviewCandidate",
    "AiReviewShortlist",
    "AutoMatchError",
    "AutoMatchOutcome",
    "CurrentRunVerificationEvidence",
    "MAX_COVERAGE_FALLBACK_ROWS",
    "MatcherRuntime",
    "VerificationCatalogCandidateEvidence",
    "VerificationProgrammeGateEvidence",
    "VerificationSemanticsEvidence",
    "active_combined_source_ids",
    "auto_match_and_spool",
    "prepare_matcher_runtime",
]
