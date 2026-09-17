#!/usr/bin/env python3
"""Fail-closed policy for clustered Gemini-assisted EPG review.

The module is deliberately pure: it performs no network, Google Sheet, or
filesystem I/O.  It groups only exact compatible REVIEW rows, builds stable
opaque Gemini choices, bounds work, and independently verifies an untrusted
Gemini result against a current local catalog snapshot and a terminal provider
row snapshot.

AI is a second verifier here, never a matcher or rule author.  A row can be
returned as ``APPROVE`` only when two independent local rankings agree on the
same candidate with strong scores/margins and Gemini HIGH selects that exact
opaque candidate.  Every other path returns ``REVIEW``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import ai_review_gemini as gemini


POLICY_VERSION = "ai-review-policy-v2"
AI_VERIFIED_GATE = "smart+gemini-high+catalog+programme"

MIN_CANDIDATES = 2
MAX_CANDIDATES = 8
MAX_ROWS_PER_GEMINI_CALL = 50
MAX_ROWS_PER_RUN = 200

MIN_LOCAL_SCORE = 96.0
MIN_LOCAL_MARGIN = 8.0
NAME_RANKER_ID = "rapidfuzz-wratio-v1"
CONTEXTUAL_RANKER_ID = "smart-context-v8.4"
MIN_INFORMATIVE_PROGRAMMES = 2
MIN_FUTURE_HORIZON_SECONDS = 6 * 60 * 60
MAX_INITIAL_PROGRAMME_GAP_SECONDS = 6 * 60 * 60
MAX_VERIFICATION_AGE_SECONDS = 60 * 60
MAX_TEXT_CATALOG_AGE_SECONDS = 48 * 60 * 60
MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS = 6 * 60 * 60
MIN_REASONABLE_EPOCH = 1_600_000_000

# A newly learned/AI-confirmed target with unusually broad fan-out is useful
# evidence, but it is not safe to write automatically on its first appearance.
MAX_CLUSTER_ROWS_PER_SERVER = 25
MAX_CLUSTER_ROWS_GLOBAL = 50

REVIEW_ACTIONS = frozenset({"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED"})
UNKNOWN_MARKETS = frozenset({"", "ALL", "UNKNOWN", "AMBIGUOUS", "NA_DIASPORA"})
SAFE_PROVENANCE_METHODS = frozenset(
    {
        "approved_knowledge",
        "canonical_identity",
        "category_language_default",
        "descriptor_relaxed",
        "dual_rank_consensus",
        "edition_aware",
        "spacing_compact",
        "strict",
        "token_multiset",
        "verified_station_identity",
    }
)

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_METHOD_RE = re.compile(r"\A[a-z0-9_]{1,80}\Z")
_GENERIC_IDENTITY_TOKENS = frozenset(
    {"channel", "feed", "stream", "slot", "source", "live", "test", "backup", "raw"}
)


class PolicyInputError(ValueError):
    """Trusted caller supplied an internally inconsistent policy input."""


@dataclass(frozen=True, slots=True)
class ProtectedSemantics:
    """Identity-bearing fields which quality normalization may never erase."""

    direction: str = ""
    timeshift: str = ""
    has_plus: bool = False
    has_extra: bool = False
    has_alternate: bool = False
    numbers: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()
    content: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """One locally discovered candidate and two independent ranking scores."""

    epg_id: str
    display_name: str
    market: str
    feed: str
    name_score: float
    contextual_score: float
    semantics: ProtectedSemantics = ProtectedSemantics()


@dataclass(frozen=True, slots=True)
class ReviewRowEvidence:
    """One unresolved row frozen before an AI request is prepared."""

    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    normalized_name: str
    normalized_category: str
    strict_identity: str
    bag_identity: str
    market: str
    route_plan: tuple[str, ...]
    route_explicit: bool
    semantics: ProtectedSemantics
    smart_epg_id: str
    smart_match_method: str
    name_ranker_id: str
    contextual_ranker_id: str
    source_sha256: str
    text_catalog_file_sha256: str
    text_catalog_fingerprint_sha256: str
    text_catalog_generated_token: str
    candidates: tuple[CandidateEvidence, ...]
    action: str = "REVIEW"
    enabled: bool = False
    has_open_alert: bool = False
    provider_identity_unchanged: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True, slots=True)
class ReviewCluster:
    """Exact context/candidate-equivalent rows sharing one AI decision."""

    cluster_id: str
    rows: tuple[ReviewRowEvidence, ...]
    cross_server: bool
    server_scoped: bool
    shadow_only: bool


@dataclass(frozen=True, slots=True)
class LocalRankingEvidence:
    epg_id: str
    name_score: float
    name_margin: float
    contextual_score: float
    contextual_margin: float
    name_ranker_id: str
    contextual_ranker_id: str


@dataclass(frozen=True, slots=True)
class PreparedClusterReview:
    """Gemini request plus the private key/target binding kept locally."""

    cluster: ReviewCluster
    request: gemini.ReviewRequest | None
    opaque_key_bindings: tuple[tuple[str, str], ...]
    ranking: LocalRankingEvidence | None
    blocked_reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.request is not None and not self.blocked_reason

    def epg_id_for_key(self, candidate_key: str) -> str | None:
        return dict(self.opaque_key_bindings).get(candidate_key)


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """Stable bounded selection; each batch can be sent in one caller action."""

    selected: tuple[PreparedClusterReview, ...]
    deferred: tuple[PreparedClusterReview, ...]
    batches: tuple[tuple[PreparedClusterReview, ...], ...]


@dataclass(frozen=True, slots=True)
class CatalogCandidateState:
    """Current exact catalog metadata for one real EPG identity."""

    epg_id: str
    market: str
    feed: str
    semantics: ProtectedSemantics = ProtectedSemantics()
    is_real: bool = True


@dataclass(frozen=True, slots=True)
class ProgrammeGateEvidence:
    """Current programme evidence independently checked after AI returns."""

    channel_key: str
    distinct_informative_programmes: int
    first_start_epoch: int | None
    latest_stop_epoch: int | None
    checked_at_epoch: int
    source_sha256: str
    passed: bool


@dataclass(frozen=True, slots=True)
class LocalVerificationSnapshot:
    """Exact current source/catalog/programme state used for final approval."""

    source_sha256: str
    text_catalog_file_sha256: str
    text_catalog_fingerprint_sha256: str
    text_catalog_generated_token: str
    xml_catalog_ids: frozenset[str]
    text_catalog_ids: frozenset[str]
    catalog_candidates: tuple[CatalogCandidateState, ...]
    programme_gates: tuple[ProgrammeGateEvidence, ...]
    _candidates_by_id: Mapping[str, tuple[CatalogCandidateState, ...]] = field(
        init=False, repr=False, compare=False
    )
    _gates_by_id: Mapping[str, tuple[ProgrammeGateEvidence, ...]] = field(
        init=False, repr=False, compare=False
    )
    _case_variants: Mapping[str, frozenset[str]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not _valid_sha256(self.source_sha256):
            raise PolicyInputError("The verification source hash is invalid.")
        if (
            not _valid_sha256(self.text_catalog_file_sha256)
            or not _valid_sha256(self.text_catalog_fingerprint_sha256)
        ):
            raise PolicyInputError(
                "The verification text-catalog hashes are invalid."
            )
        text_generated_epoch = _text_catalog_generated_epoch(
            self.text_catalog_generated_token
        )
        xml_ids = _freeze_catalog_ids(self.xml_catalog_ids, label="XML")
        text_ids = _freeze_catalog_ids(self.text_catalog_ids, label="text")
        candidate_items = _freeze_snapshot_items(
            self.catalog_candidates,
            label="catalog candidates",
        )
        gate_items = _freeze_snapshot_items(
            self.programme_gates,
            label="programme gates",
        )

        candidates: dict[str, list[CatalogCandidateState]] = defaultdict(list)
        for candidate in candidate_items:
            _validate_catalog_candidate_state(candidate)
            candidates[candidate.epg_id].append(candidate)
        gates: dict[str, list[ProgrammeGateEvidence]] = defaultdict(list)
        for gate in gate_items:
            _validate_programme_gate_evidence(gate)
            age = gate.checked_at_epoch - text_generated_epoch
            if (
                age > MAX_TEXT_CATALOG_AGE_SECONDS
                or age < -MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS
            ):
                raise PolicyInputError(
                    "The verification text catalog is not generation-compatible."
                )
            gates[gate.channel_key].append(gate)
        variants: dict[str, set[str]] = defaultdict(set)
        for epg_id in xml_ids | text_ids:
            variants[epg_id.casefold()].add(epg_id)

        object.__setattr__(self, "xml_catalog_ids", xml_ids)
        object.__setattr__(self, "text_catalog_ids", text_ids)
        object.__setattr__(self, "catalog_candidates", candidate_items)
        object.__setattr__(self, "programme_gates", gate_items)
        object.__setattr__(
            self,
            "_candidates_by_id",
            MappingProxyType({key: tuple(value) for key, value in candidates.items()}),
        )
        object.__setattr__(
            self,
            "_gates_by_id",
            MappingProxyType({key: tuple(value) for key, value in gates.items()}),
        )
        object.__setattr__(
            self,
            "_case_variants",
            MappingProxyType(
                {key: frozenset(value) for key, value in variants.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class TerminalRowState:
    """Provider/Sheet identity reread immediately before a possible write."""

    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    normalized_name: str
    normalized_category: str
    strict_identity: str
    bag_identity: str
    market: str
    route_plan: tuple[str, ...]
    route_explicit: bool
    semantics: ProtectedSemantics
    action: str
    enabled: bool
    has_open_alert: bool

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True, slots=True)
class ApprovalProvenance:
    """Private fields sufficient for the caller's ai-verified-v2 note."""

    gate: str
    policy_version: str
    cluster_id: str
    selected_candidate_key: str
    epg_id: str
    match_method: str
    market: str
    source_sha256: str
    text_catalog_file_sha256: str
    text_catalog_fingerprint_sha256: str
    text_catalog_generated_token: str
    name_score: float
    name_margin: float
    contextual_score: float
    contextual_margin: float
    name_ranker_id: str
    contextual_ranker_id: str
    programme_count: int
    programme_checked_at_epoch: int
    programme_latest_stop_epoch: int
    supporting_servers: tuple[str, ...]
    row_binding_sha256: str


@dataclass(frozen=True, slots=True)
class RowApprovalDecision:
    server_id: str
    stream_id: str
    decision: str
    reason_code: str
    epg_id: str = ""
    provenance: ApprovalProvenance | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVE"


def _valid_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _text_catalog_generated_epoch(value: object) -> int:
    if type(value) is not str:
        raise PolicyInputError("The verification text generation token is invalid.")
    formats = {12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    format_string = formats.get(len(value))
    if format_string is None or not value.isascii() or not value.isdigit():
        raise PolicyInputError("The verification text generation token is invalid.")
    try:
        return int(
            datetime.strptime(value, format_string)
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        raise PolicyInputError(
            "The verification text generation token is invalid."
        ) from None


def _valid_epg_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 300:
        return False
    if value != value.strip():
        return False
    return not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)


def _freeze_snapshot_items(value: object, *, label: str) -> tuple[Any, ...]:
    """Freeze a trusted snapshot collection without accepting string/mapping iterables."""

    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise PolicyInputError(f"The verification {label} collection is malformed.")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except Exception:
        raise PolicyInputError(
            f"The verification {label} collection is malformed."
        ) from None


def _freeze_catalog_ids(value: object, *, label: str) -> frozenset[str]:
    items = _freeze_snapshot_items(value, label=f"{label} catalog")
    if any(not _valid_epg_id(item) for item in items):
        raise PolicyInputError(
            f"The verification {label} catalog contains an invalid EPG ID."
        )
    return frozenset(items)


def _valid_plain_field(value: object, *, maximum: int, allow_empty: bool) -> bool:
    if type(value) is not str or len(value) > maximum or value != value.strip():
        return False
    if not allow_empty and not value:
        return False
    return not any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )


def _validate_catalog_candidate_state(value: object) -> None:
    if not isinstance(value, CatalogCandidateState):
        raise PolicyInputError("A verification catalog candidate is malformed.")
    if not _valid_epg_id(value.epg_id):
        raise PolicyInputError("A verification catalog candidate has an invalid EPG ID.")
    if not _valid_plain_field(value.market, maximum=40, allow_empty=True):
        raise PolicyInputError("A verification catalog candidate has an invalid market.")
    if not _valid_plain_field(value.feed, maximum=80, allow_empty=False):
        raise PolicyInputError("A verification catalog candidate has an invalid feed.")
    if type(value.is_real) is not bool:
        raise PolicyInputError(
            "A verification catalog candidate real/dummy flag must be boolean."
        )
    _semantics_signature(value.semantics)


def _strict_optional_epoch(value: object) -> bool:
    return value is None or (
        type(value) is int and value >= MIN_REASONABLE_EPOCH
    )


def _validate_programme_gate_evidence(value: object) -> None:
    if not isinstance(value, ProgrammeGateEvidence):
        raise PolicyInputError("A verification programme gate is malformed.")
    if not _valid_epg_id(value.channel_key):
        raise PolicyInputError("A verification programme gate has an invalid EPG ID.")
    if (
        type(value.distinct_informative_programmes) is not int
        or value.distinct_informative_programmes < 0
    ):
        raise PolicyInputError(
            "Programme-count evidence must be a non-negative integer."
        )
    if not _strict_optional_epoch(value.first_start_epoch):
        raise PolicyInputError("The first programme timestamp is malformed.")
    if not _strict_optional_epoch(value.latest_stop_epoch):
        raise PolicyInputError("The latest programme timestamp is malformed.")
    if (
        type(value.checked_at_epoch) is not int
        or value.checked_at_epoch < MIN_REASONABLE_EPOCH
    ):
        raise PolicyInputError("The programme check timestamp is malformed.")
    if not _valid_sha256(value.source_sha256):
        raise PolicyInputError("The programme source hash is invalid.")
    if type(value.passed) is not bool:
        raise PolicyInputError("The programme gate result must be boolean.")


def _canonical_text(value: object, *, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")[: limit * 4])
    text = " ".join(text.split()).casefold()
    return text[:limit]


def _canonical_market(value: object) -> str:
    market = str(value or "").strip().upper()
    return "UK" if market == "GB" else market


def _canonical_set(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _canonical_text(value, limit=80)
                for value in values
                if _canonical_text(value, limit=80)
            }
        )
    )


def _semantics_signature(value: ProtectedSemantics) -> tuple[Any, ...]:
    if not isinstance(value, ProtectedSemantics):
        raise PolicyInputError("Protected semantics are missing or malformed.")
    if type(value.direction) is not str or type(value.timeshift) is not str:
        raise PolicyInputError("Protected semantic text fields must be strings.")
    if not all(
        type(flag) is bool
        for flag in (value.has_plus, value.has_extra, value.has_alternate)
    ):
        raise PolicyInputError("Protected semantic flags must be booleans.")
    for label, items in (
        ("numbers", value.numbers),
        ("languages", value.languages),
        ("content", value.content),
    ):
        if not isinstance(items, frozenset) or any(type(item) is not str for item in items):
            raise PolicyInputError(
                f"Protected semantic {label} must be a frozen string set."
            )
    direction = _canonical_text(value.direction, limit=16)
    if direction not in {"", "east", "west"}:
        raise PolicyInputError("The protected direction is invalid.")
    timeshift = str(value.timeshift or "").strip().removeprefix("+")
    if timeshift and (not timeshift.isdigit() or len(timeshift) > 4):
        raise PolicyInputError("The protected timeshift is invalid.")
    return (
        direction,
        timeshift,
        value.has_plus,
        value.has_extra,
        value.has_alternate,
        _canonical_set(value.numbers),
        _canonical_set(value.languages),
        _canonical_set(value.content),
    )


def _row_context_signature(row: ReviewRowEvidence | TerminalRowState) -> tuple[Any, ...]:
    if not isinstance(row.route_explicit, bool):
        raise PolicyInputError("Route-explicit evidence must be boolean.")
    market = _canonical_market(row.market)
    route_plan = tuple(_canonical_market(value) for value in row.route_plan)
    return (
        _canonical_text(row.normalized_name, limit=300),
        _canonical_text(row.normalized_category, limit=200),
        _canonical_text(row.strict_identity, limit=300),
        _canonical_text(row.bag_identity, limit=300),
        market,
        route_plan,
        row.route_explicit,
        _semantics_signature(row.semantics),
    )


def _row_terminal_binding(row: ReviewRowEvidence | TerminalRowState) -> tuple[Any, ...]:
    return (
        str(row.server_id),
        str(row.stream_id),
        str(row.channel_name),
        str(row.category_name),
        *_row_context_signature(row),
    )


def _candidate_signature(candidate: CandidateEvidence) -> tuple[Any, ...]:
    if not _valid_epg_id(candidate.epg_id):
        raise PolicyInputError("A candidate has an invalid opaque EPG ID.")
    if not isinstance(candidate.display_name, str) or not candidate.display_name.strip():
        raise PolicyInputError("A candidate display name is empty.")
    normalized_scores: list[float] = []
    for score in (candidate.name_score, candidate.contextual_score):
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 100.0
        ):
            raise PolicyInputError("A candidate ranking score is invalid.")
        numeric_score = float(score)
        # IEEE signed zero compares equal but JSON encodes it differently.
        # Canonicalize it so input order cannot alter a cluster digest.
        normalized_scores.append(0.0 if numeric_score == 0.0 else numeric_score)
    return (
        candidate.epg_id,
        _canonical_text(candidate.display_name, limit=300),
        _canonical_market(candidate.market),
        str(candidate.feed or "").strip().upper(),
        normalized_scores[0],
        normalized_scores[1],
        _semantics_signature(candidate.semantics),
    )


def _candidate_fingerprint(candidates: Sequence[CandidateEvidence]) -> tuple[Any, ...]:
    if not MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES:
        raise PolicyInputError(
            f"A Gemini shortlist must contain {MIN_CANDIDATES}-{MAX_CANDIDATES} candidates."
        )
    signatures = tuple(sorted((_candidate_signature(value) for value in candidates)))
    ids = [value[0] for value in signatures]
    if len(ids) != len(set(ids)) or len(ids) != len({value.casefold() for value in ids}):
        raise PolicyInputError("Candidate IDs must be exact and case-family unique.")
    return signatures


def _server_scope_required(row: ReviewRowEvidence) -> bool:
    strict_identity = _canonical_text(row.strict_identity, limit=300)
    bag_identity = _canonical_text(row.bag_identity, limit=300)
    identity = bag_identity or strict_identity
    tokens = identity.split()
    if not identity:
        return True
    if all(token.isdigit() or token in _GENERIC_IDENTITY_TOKENS for token in tokens):
        return True
    unknown_context = (
        tuple(_canonical_market(value) for value in row.route_plan) in {(), ("ALL",)}
        and not _semantics_signature(row.semantics)[6]
    )
    return len(tokens) == 1 and len(tokens[0]) <= 4 and unknown_context


def _stable_digest(label: str, value: object, *, size: int = 32) -> str:
    encoded = json.dumps(
        {"label": label, "value": value},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=list,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:size]


def _cluster_shadow_only(rows: Sequence[ReviewRowEvidence]) -> bool:
    per_server = Counter(row.server_id for row in rows)
    return len(rows) > MAX_CLUSTER_ROWS_GLOBAL or any(
        count > MAX_CLUSTER_ROWS_PER_SERVER for count in per_server.values()
    )


def cluster_exact_compatible_rows(
    rows: Sequence[ReviewRowEvidence],
) -> tuple[ReviewCluster, ...]:
    """Group only exact normalized context/candidate-equivalent rows.

    Quality is intentionally absent from the key.  Market, route, language,
    East/West, timeshift, plus/extra/alternate, numbers and content remain
    identity-bearing.  Empty/generic and short contextless identities cannot
    form cross-server clusters.
    """

    seen: set[tuple[str, str]] = set()
    grouped: dict[tuple[Any, ...], list[ReviewRowEvidence]] = defaultdict(list)
    scope_by_key: dict[tuple[Any, ...], bool] = {}
    for row in rows:
        if not isinstance(row, ReviewRowEvidence):
            raise PolicyInputError("Every clustered item must be ReviewRowEvidence.")
        if not row.server_id or not row.stream_id:
            raise PolicyInputError("A REVIEW row is missing its provider identity.")
        if row.key in seen:
            raise PolicyInputError("The REVIEW input contains a duplicate provider identity.")
        seen.add(row.key)
        if not _valid_sha256(row.source_sha256):
            raise PolicyInputError("A REVIEW row has an invalid source snapshot hash.")
        if (
            not _valid_sha256(row.text_catalog_file_sha256)
            or not _valid_sha256(row.text_catalog_fingerprint_sha256)
        ):
            raise PolicyInputError(
                "A REVIEW row has invalid text-catalog snapshot hashes."
            )
        _text_catalog_generated_epoch(row.text_catalog_generated_token)
        if (
            row.name_ranker_id != NAME_RANKER_ID
            or row.contextual_ranker_id != CONTEXTUAL_RANKER_ID
        ):
            raise PolicyInputError(
                "The two fixed independent local rankers are required."
            )
        method = str(row.smart_match_method or "").strip().casefold()
        if _SAFE_METHOD_RE.fullmatch(method) is None:
            raise PolicyInputError("The Smart Rules method is invalid.")

        candidate_fingerprint = _candidate_fingerprint(row.candidates)
        server_scoped = _server_scope_required(row)
        context = _row_context_signature(row)
        if not context[0] or not context[1]:
            # An empty normalized name/category cannot safely share an AI
            # decision even with another row on the same server.
            scope = ("row", row.server_id, row.stream_id)
            server_scoped = True
        elif server_scoped:
            scope = ("server", row.server_id)
        else:
            scope = ("cross_server",)
        group_key = (
            context,
            row.source_sha256,
            row.text_catalog_file_sha256,
            row.text_catalog_fingerprint_sha256,
            row.text_catalog_generated_token,
            row.smart_epg_id,
            method,
            row.name_ranker_id,
            row.contextual_ranker_id,
            candidate_fingerprint,
            scope,
        )
        grouped[group_key].append(row)
        scope_by_key[group_key] = server_scoped

    result: list[ReviewCluster] = []
    for group_key, members in grouped.items():
        ordered = tuple(
            sorted(
                members,
                key=lambda row: (
                    row.server_id,
                    _canonical_text(row.stream_id, limit=120),
                    row.stream_id,
                ),
            )
        )
        cluster_id = "cl_" + _stable_digest("exact-review-cluster-v2", group_key, size=24)
        servers = {row.server_id for row in ordered}
        result.append(
            ReviewCluster(
                cluster_id=cluster_id,
                rows=ordered,
                cross_server=len(servers) >= 2 and not scope_by_key[group_key],
                server_scoped=scope_by_key[group_key],
                shadow_only=_cluster_shadow_only(ordered),
            )
        )
    return tuple(sorted(result, key=lambda cluster: cluster.cluster_id))


def _semantics_compatible(
    query: ProtectedSemantics, candidate: ProtectedSemantics
) -> bool:
    q = _semantics_signature(query)
    c = _semantics_signature(candidate)
    # Every protected field is exact.  Missing language/content evidence is
    # unknown, not permission to collapse a regional or edition variant.
    return q == c


def _ranking_evidence(row: ReviewRowEvidence) -> LocalRankingEvidence | None:
    candidates = tuple(row.candidates)
    if len(candidates) < 2:
        return None

    name_ranked = sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate.name_score),
            candidate.epg_id.casefold(),
            candidate.epg_id,
        ),
    )
    contextual_ranked = sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate.contextual_score),
            candidate.epg_id.casefold(),
            candidate.epg_id,
        ),
    )
    name_top = name_ranked[0]
    contextual_top = contextual_ranked[0]
    if name_top.epg_id != contextual_top.epg_id:
        return None
    if name_top.epg_id != row.smart_epg_id:
        return None
    name_margin = float(name_top.name_score) - float(name_ranked[1].name_score)
    contextual_margin = float(contextual_top.contextual_score) - float(
        contextual_ranked[1].contextual_score
    )
    if (
        float(name_top.name_score) < MIN_LOCAL_SCORE
        or float(contextual_top.contextual_score) < MIN_LOCAL_SCORE
        or name_margin < MIN_LOCAL_MARGIN
        or contextual_margin < MIN_LOCAL_MARGIN
    ):
        return None
    return LocalRankingEvidence(
        epg_id=name_top.epg_id,
        name_score=float(name_top.name_score),
        name_margin=name_margin,
        contextual_score=float(contextual_top.contextual_score),
        contextual_margin=contextual_margin,
        name_ranker_id=row.name_ranker_id,
        contextual_ranker_id=row.contextual_ranker_id,
    )


def _blocked(cluster: ReviewCluster, reason: str) -> PreparedClusterReview:
    return PreparedClusterReview(
        cluster=cluster,
        request=None,
        opaque_key_bindings=(),
        ranking=None,
        blocked_reason=reason,
    )


def _is_unresolved_state(action: object, enabled: object) -> bool:
    return (
        type(action) is str
        and action.strip().upper() in REVIEW_ACTIONS
        and enabled is False
    )


def prepare_cluster_review(cluster: ReviewCluster) -> PreparedClusterReview:
    """Build one stable, shuffled Gemini request or a fail-closed block."""

    if not cluster.rows:
        return _blocked(cluster, "EMPTY_CLUSTER")
    try:
        rebuilt = cluster_exact_compatible_rows(cluster.rows)
    except Exception:
        return _blocked(cluster, "INVALID_CLUSTER_BINDING")
    if (
        len(rebuilt) != 1
        or rebuilt[0].cluster_id != cluster.cluster_id
        or rebuilt[0].cross_server != cluster.cross_server
        or rebuilt[0].server_scoped != cluster.server_scoped
        or rebuilt[0].shadow_only != cluster.shadow_only
    ):
        return _blocked(cluster, "INVALID_CLUSTER_BINDING")
    if cluster.shadow_only:
        return _blocked(cluster, "CLUSTER_FANOUT_SHADOW_ONLY")
    reference = cluster.rows[0]
    if any(
        not _is_unresolved_state(row.action, row.enabled)
        for row in cluster.rows
    ):
        return _blocked(cluster, "ROW_NOT_UNRESOLVED")
    if any(row.has_open_alert is not False for row in cluster.rows):
        return _blocked(cluster, "OPEN_ALERT")
    if any(row.provider_identity_unchanged is not True for row in cluster.rows):
        return _blocked(cluster, "PROVIDER_IDENTITY_CHANGED")

    market = _canonical_market(reference.market)
    route_plan = tuple(_canonical_market(value) for value in reference.route_plan)
    if (
        market in UNKNOWN_MARKETS
        or route_plan != (market,)
        or reference.route_explicit is not True
    ):
        return _blocked(cluster, "UNSAFE_MARKET")
    method = reference.smart_match_method.strip().casefold()
    if method not in SAFE_PROVENANCE_METHODS:
        return _blocked(cluster, "UNSAFE_SMART_METHOD")

    ranking = _ranking_evidence(reference)
    if ranking is None:
        return _blocked(cluster, "LOCAL_RANKING_UNSAFE")
    if any(_ranking_evidence(row) != ranking for row in cluster.rows[1:]):
        return _blocked(cluster, "CLUSTER_RANKING_CONFLICT")

    for candidate in reference.candidates:
        if _canonical_market(candidate.market) != market:
            return _blocked(cluster, "CANDIDATE_MARKET_CONFLICT")
        if not _semantics_compatible(reference.semantics, candidate.semantics):
            return _blocked(cluster, "CANDIDATE_SEMANTICS_CONFLICT")

    keyed: list[tuple[str, str, CandidateEvidence]] = []
    for candidate in reference.candidates:
        signature = _candidate_signature(candidate)
        opaque = "k_" + _stable_digest(
            "opaque-candidate-v2",
            (cluster.cluster_id, signature[0], signature[1], signature[2], signature[3]),
            size=24,
        )
        order = _stable_digest(
            "candidate-order-v2",
            (cluster.cluster_id, signature[0], signature[1], signature[2], signature[3]),
            size=64,
        )
        keyed.append((order, opaque, candidate))
    if len({item[1] for item in keyed}) != len(keyed):
        return _blocked(cluster, "OPAQUE_KEY_COLLISION")
    keyed.sort(key=lambda item: (item[0], item[1]))

    representative = min(
        cluster.rows,
        key=lambda row: (
            _canonical_text(row.channel_name, limit=300),
            row.channel_name,
            _canonical_text(row.category_name, limit=200),
            row.category_name,
            row.server_id,
            row.stream_id,
        ),
    )
    request = gemini.ReviewRequest(
        review_id=cluster.cluster_id,
        channel_name=representative.channel_name,
        category=representative.category_name,
        market=market,
        candidates=tuple(
            gemini.ReviewCandidate(
                candidate_key=opaque,
                epg_id=candidate.epg_id,
                display_name=candidate.display_name,
                region=market,
                feed=candidate.feed,
            )
            for _order, opaque, candidate in keyed
        ),
    )
    return PreparedClusterReview(
        cluster=cluster,
        request=request,
        opaque_key_bindings=tuple((opaque, candidate.epg_id) for _order, opaque, candidate in keyed),
        ranking=ranking,
    )


def prepare_cluster_reviews(
    clusters: Sequence[ReviewCluster],
) -> tuple[PreparedClusterReview, ...]:
    return tuple(prepare_cluster_review(cluster) for cluster in clusters)


def partition_review_requests(
    prepared: Sequence[PreparedClusterReview],
    *,
    run_limit: int,
    batch_size: int = MAX_ROWS_PER_GEMINI_CALL,
    rotation: int = 0,
) -> ReviewPlan:
    """Select at most 200 stable requests and split calls at 50 or fewer."""

    if isinstance(run_limit, bool) or not isinstance(run_limit, int) or not 1 <= run_limit <= MAX_ROWS_PER_RUN:
        raise PolicyInputError(
            f"The Gemini run limit must be from 1 to {MAX_ROWS_PER_RUN}."
        )
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_ROWS_PER_GEMINI_CALL:
        raise PolicyInputError(
            f"A Gemini call may contain at most {MAX_ROWS_PER_GEMINI_CALL} clusters."
        )
    if isinstance(rotation, bool) or not isinstance(rotation, int) or rotation < 0:
        raise PolicyInputError("The Gemini review rotation must be a non-negative integer.")

    eligible = sorted(
        (item for item in prepared if item.eligible),
        key=lambda item: item.cluster.cluster_id,
    )
    cluster_ids = [item.cluster.cluster_id for item in eligible]
    if len(cluster_ids) != len(set(cluster_ids)):
        raise PolicyInputError("The Gemini review plan contains a duplicate cluster ID.")
    if eligible:
        offset = rotation % len(eligible)
        eligible = eligible[offset:] + eligible[:offset]
    selected_list: list[PreparedClusterReview] = []
    deferred_list: list[PreparedClusterReview] = []
    selected_rows = 0
    for item in eligible:
        represented_rows = len(item.cluster.rows)
        if (
            represented_rows < 1
            or represented_rows > batch_size
            or selected_rows + represented_rows > run_limit
        ):
            deferred_list.append(item)
            continue
        selected_list.append(item)
        selected_rows += represented_rows

    batches_list: list[tuple[PreparedClusterReview, ...]] = []
    current: list[PreparedClusterReview] = []
    current_rows = 0
    for item in selected_list:
        represented_rows = len(item.cluster.rows)
        if current and (
            current_rows + represented_rows > batch_size
            or len(current) >= MAX_ROWS_PER_GEMINI_CALL
        ):
            batches_list.append(tuple(current))
            current = []
            current_rows = 0
        current.append(item)
        current_rows += represented_rows
    if current:
        batches_list.append(tuple(current))

    selected = tuple(selected_list)
    deferred = tuple(deferred_list)
    batches = tuple(batches_list)
    return ReviewPlan(selected=selected, deferred=deferred, batches=batches)


def _review_all(
    prepared: PreparedClusterReview, reason: str
) -> tuple[RowApprovalDecision, ...]:
    return tuple(
        RowApprovalDecision(
            server_id=row.server_id,
            stream_id=row.stream_id,
            decision="REVIEW",
            reason_code=reason,
        )
        for row in prepared.cluster.rows
    )


def _result_value(value: object) -> str | None:
    """Return a strict enum/string token without coercing hostile objects."""

    try:
        raw = getattr(value, "value", value)
    except Exception:
        return None
    if type(raw) is not str:
        return None
    token = raw.strip().upper()
    return token if token else ""


def _catalog_candidate_for(
    snapshot: LocalVerificationSnapshot, epg_id: str
) -> CatalogCandidateState | None:
    candidates = snapshot._candidates_by_id.get(epg_id, ())
    return candidates[0] if len(candidates) == 1 else None


def _programme_gate_for(
    snapshot: LocalVerificationSnapshot, epg_id: str
) -> ProgrammeGateEvidence | None:
    gates = snapshot._gates_by_id.get(epg_id, ())
    return gates[0] if len(gates) == 1 else None


def _strong_programme_gate(
    gate: ProgrammeGateEvidence, *, epg_id: str, source_sha256: str
) -> bool:
    return (
        isinstance(gate, ProgrammeGateEvidence)
        and gate.channel_key == epg_id
        and gate.source_sha256 == source_sha256
        and type(gate.passed) is bool
        and gate.passed is True
        and type(gate.distinct_informative_programmes) is int
        and gate.distinct_informative_programmes >= MIN_INFORMATIVE_PROGRAMMES
        and type(gate.checked_at_epoch) is int
        and gate.checked_at_epoch >= MIN_REASONABLE_EPOCH
        and type(gate.first_start_epoch) is int
        and gate.first_start_epoch
        <= gate.checked_at_epoch + MAX_INITIAL_PROGRAMME_GAP_SECONDS
        and type(gate.latest_stop_epoch) is int
        and gate.latest_stop_epoch > gate.first_start_epoch
        and gate.latest_stop_epoch
        >= gate.checked_at_epoch + MIN_FUTURE_HORIZON_SECONDS
    )


_TERMINAL_TEXT_FIELDS = (
    "server_id",
    "stream_id",
    "channel_name",
    "category_name",
    "normalized_name",
    "normalized_category",
    "strict_identity",
    "bag_identity",
    "market",
    "action",
)


def _terminal_identity_key(value: object) -> tuple[str, str] | None:
    """Extract only a safe exact key; malformed unknown keys poison the snapshot."""

    if not isinstance(value, TerminalRowState):
        return None
    server_id = value.server_id
    stream_id = value.stream_id
    if (
        type(server_id) is not str
        or type(stream_id) is not str
        or not server_id
        or not stream_id
        or server_id != server_id.strip()
        or stream_id != stream_id.strip()
    ):
        return None
    return server_id, stream_id


def _validated_terminal_binding(value: TerminalRowState) -> tuple[Any, ...]:
    """Validate every terminal field before comparing it with proposal evidence."""

    if any(type(getattr(value, field)) is not str for field in _TERMINAL_TEXT_FIELDS):
        raise PolicyInputError("A terminal row contains a malformed text field.")
    if type(value.route_plan) is not tuple or any(
        type(item) is not str for item in value.route_plan
    ):
        raise PolicyInputError("A terminal route plan is malformed.")
    if type(value.route_explicit) is not bool:
        raise PolicyInputError("A terminal route-explicit flag is malformed.")
    if type(value.enabled) is not bool or type(value.has_open_alert) is not bool:
        raise PolicyInputError("A terminal row state flag is malformed.")
    _semantics_signature(value.semantics)
    return _row_terminal_binding(value)


def _row_binding_sha256(
    row: ReviewRowEvidence,
    *,
    epg_id: str,
    market: str,
    source_sha256: str,
    text_catalog_file_sha256: str,
    text_catalog_fingerprint_sha256: str,
    text_catalog_generated_token: str,
    cluster_id: str,
) -> str:
    return _stable_digest(
        "ai-verified-row-binding-v2",
        (
            row.server_id,
            row.stream_id,
            row.channel_name,
            row.category_name,
            epg_id,
            market,
            source_sha256,
            text_catalog_file_sha256,
            text_catalog_fingerprint_sha256,
            text_catalog_generated_token,
            cluster_id,
            POLICY_VERSION,
            AI_VERIFIED_GATE,
        ),
        size=64,
    )


def _validate_high_agreement_impl(
    prepared: PreparedClusterReview,
    result: object,
    *,
    verification: LocalVerificationSnapshot,
    terminal_rows: Sequence[TerminalRowState],
    decision_epoch: int,
) -> tuple[RowApprovalDecision, ...]:
    """Return per-row APPROVE only after every independent gate passes.

    ``result`` is always treated as hostile.  Unknown keys, invented targets,
    wrong review IDs, non-HIGH confidence, malformed objects and exceptions in
    their properties all collapse to a fixed REVIEW reason without reflecting
    model-controlled text.
    """

    if not prepared.eligible or prepared.request is None or prepared.ranking is None:
        return _review_all(prepared, prepared.blocked_reason or "POLICY_BLOCKED")
    if (
        isinstance(decision_epoch, bool)
        or not isinstance(decision_epoch, int)
        or decision_epoch < MIN_REASONABLE_EPOCH
    ):
        return _review_all(prepared, "DECISION_TIME_INVALID")

    try:
        review_id = getattr(result, "review_id", None)
        decision = _result_value(getattr(result, "decision", None))
        confidence = _result_value(getattr(result, "confidence", None))
        candidate_key = getattr(result, "candidate_key", None)
        error_code = getattr(result, "error_code", None)
    except Exception:
        return _review_all(prepared, "AI_RESULT_INVALID")
    if (
        type(review_id) is not str
        or decision is None
        or confidence is None
        or type(candidate_key) is not str
        or not (error_code is None or type(error_code) is str)
    ):
        return _review_all(prepared, "AI_RESULT_INVALID")
    if review_id != prepared.request.review_id:
        return _review_all(prepared, "AI_REVIEW_ID_MISMATCH")
    if (
        decision != "SUGGEST"
        or confidence != "HIGH"
        or error_code not in (None, "")
    ):
        return _review_all(prepared, "AI_NOT_HIGH_AGREEMENT")
    selected_epg_id = prepared.epg_id_for_key(candidate_key)
    if selected_epg_id is None:
        return _review_all(prepared, "AI_UNKNOWN_CANDIDATE_KEY")
    ranking = prepared.ranking
    if selected_epg_id != ranking.epg_id:
        return _review_all(prepared, "AI_DISAGREES_WITH_LOCAL_TOP")

    source_sha256 = verification.source_sha256
    text_catalog_file_sha256 = verification.text_catalog_file_sha256
    text_catalog_fingerprint_sha256 = (
        verification.text_catalog_fingerprint_sha256
    )
    text_catalog_generated_token = verification.text_catalog_generated_token
    reference = prepared.cluster.rows[0]
    if source_sha256 != reference.source_sha256:
        return _review_all(prepared, "SOURCE_SNAPSHOT_CHANGED")
    if any(row.source_sha256 != source_sha256 for row in prepared.cluster.rows):
        return _review_all(prepared, "CLUSTER_SOURCE_CONFLICT")
    if (
        text_catalog_file_sha256 != reference.text_catalog_file_sha256
        or text_catalog_fingerprint_sha256
        != reference.text_catalog_fingerprint_sha256
        or text_catalog_generated_token != reference.text_catalog_generated_token
    ):
        return _review_all(prepared, "TEXT_CATALOG_SNAPSHOT_CHANGED")
    if any(
        row.text_catalog_file_sha256 != text_catalog_file_sha256
        or row.text_catalog_fingerprint_sha256
        != text_catalog_fingerprint_sha256
        or row.text_catalog_generated_token != text_catalog_generated_token
        for row in prepared.cluster.rows
    ):
        return _review_all(prepared, "CLUSTER_TEXT_CATALOG_CONFLICT")
    if (
        selected_epg_id not in verification.xml_catalog_ids
        or selected_epg_id not in verification.text_catalog_ids
    ):
        return _review_all(prepared, "CATALOG_ID_NOT_EXACTLY_CORROBORATED")
    if verification._case_variants.get(selected_epg_id.casefold(), frozenset()) != frozenset({selected_epg_id}):
        return _review_all(prepared, "CATALOG_CASE_COLLISION")

    catalog_candidate = _catalog_candidate_for(verification, selected_epg_id)
    if catalog_candidate is None or catalog_candidate.is_real is not True:
        return _review_all(prepared, "CATALOG_CANDIDATE_NOT_UNIQUE_REAL")
    offered = tuple(
        candidate
        for candidate in reference.candidates
        if candidate.epg_id == selected_epg_id
    )
    if (
        len(offered) != 1
        or not str(catalog_candidate.feed or "").strip()
        or str(catalog_candidate.feed or "").strip().upper()
        != str(offered[0].feed or "").strip().upper()
    ):
        return _review_all(prepared, "CATALOG_FEED_BINDING_FAILED")
    market = _canonical_market(reference.market)
    if (
        market in UNKNOWN_MARKETS
        or reference.route_explicit is not True
        or any(row.route_explicit is not True for row in prepared.cluster.rows)
        or tuple(_canonical_market(value) for value in reference.route_plan) != (market,)
        or _canonical_market(catalog_candidate.market) != market
    ):
        return _review_all(prepared, "MARKET_GATE_FAILED")
    if not _semantics_compatible(reference.semantics, catalog_candidate.semantics):
        return _review_all(prepared, "PROTECTED_SEMANTICS_GATE_FAILED")

    gate = _programme_gate_for(verification, selected_epg_id)
    if gate is None or not _strong_programme_gate(
        gate, epg_id=selected_epg_id, source_sha256=source_sha256
    ):
        return _review_all(prepared, "PROGRAMME_GATE_FAILED")
    if not 0 <= decision_epoch - gate.checked_at_epoch <= MAX_VERIFICATION_AGE_SECONDS:
        return _review_all(prepared, "PROGRAMME_EVIDENCE_STALE")

    terminal_by_key: dict[
        tuple[str, str],
        list[tuple[TerminalRowState, tuple[Any, ...] | None]],
    ] = defaultdict(list)
    terminal_snapshot_invalid = False
    try:
        if isinstance(terminal_rows, (str, bytes, bytearray, Mapping)):
            raise TypeError
        for current in terminal_rows:
            key = _terminal_identity_key(current)
            if key is None:
                terminal_snapshot_invalid = True
                break
            assert isinstance(current, TerminalRowState)
            try:
                binding = _validated_terminal_binding(current)
            except Exception:
                binding = None
            terminal_by_key[key].append((current, binding))
    except Exception:
        terminal_snapshot_invalid = True
    if terminal_snapshot_invalid:
        return _review_all(prepared, "TERMINAL_SNAPSHOT_INVALID")

    terminal_failures: dict[tuple[str, str], str] = {}
    for row in prepared.cluster.rows:
        current_group = terminal_by_key.get(row.key, ())
        if len(current_group) != 1:
            terminal_failures[row.key] = "TERMINAL_ROW_MISSING_OR_DUPLICATE"
            continue
        current, current_binding = current_group[0]
        if current_binding is None:
            terminal_failures[row.key] = "TERMINAL_ROW_INVALID"
            continue
        if current_binding != _row_terminal_binding(row):
            terminal_failures[row.key] = "TERMINAL_PROVIDER_IDENTITY_CHANGED"
            continue
        if current.has_open_alert is not False:
            terminal_failures[row.key] = "TERMINAL_OPEN_ALERT"
            continue
        if (
            not _is_unresolved_state(current.action, current.enabled)
        ):
            terminal_failures[row.key] = "TERMINAL_ROW_NO_LONGER_UNRESOLVED"
            continue
        if not _semantics_compatible(row.semantics, catalog_candidate.semantics):
            terminal_failures[row.key] = "ROW_PROTECTED_SEMANTICS_GATE_FAILED"

    supporting_servers = tuple(
        sorted(
            {
                row.server_id
                for row in prepared.cluster.rows
                if row.key not in terminal_failures
            }
        )
    )
    decisions: list[RowApprovalDecision] = []
    for row in prepared.cluster.rows:
        failure = terminal_failures.get(row.key)
        if failure:
            decisions.append(
                RowApprovalDecision(
                    row.server_id,
                    row.stream_id,
                    "REVIEW",
                    failure,
                )
            )
            continue

        provenance = ApprovalProvenance(
            gate=AI_VERIFIED_GATE,
            policy_version=POLICY_VERSION,
            cluster_id=prepared.cluster.cluster_id,
            selected_candidate_key=candidate_key,
            epg_id=selected_epg_id,
            match_method=row.smart_match_method.strip().casefold(),
            market=market,
            source_sha256=source_sha256,
            text_catalog_file_sha256=text_catalog_file_sha256,
            text_catalog_fingerprint_sha256=text_catalog_fingerprint_sha256,
            text_catalog_generated_token=text_catalog_generated_token,
            name_score=ranking.name_score,
            name_margin=ranking.name_margin,
            contextual_score=ranking.contextual_score,
            contextual_margin=ranking.contextual_margin,
            name_ranker_id=ranking.name_ranker_id,
            contextual_ranker_id=ranking.contextual_ranker_id,
            programme_count=gate.distinct_informative_programmes,
            programme_checked_at_epoch=gate.checked_at_epoch,
            programme_latest_stop_epoch=int(gate.latest_stop_epoch or 0),
            supporting_servers=supporting_servers,
            row_binding_sha256=_row_binding_sha256(
                row,
                epg_id=selected_epg_id,
                market=market,
                source_sha256=source_sha256,
                text_catalog_file_sha256=text_catalog_file_sha256,
                text_catalog_fingerprint_sha256=(
                    text_catalog_fingerprint_sha256
                ),
                text_catalog_generated_token=text_catalog_generated_token,
                cluster_id=prepared.cluster.cluster_id,
            ),
        )
        decisions.append(
            RowApprovalDecision(
                row.server_id,
                row.stream_id,
                "APPROVE",
                "SMART_AND_GEMINI_HIGH_LOCALLY_VERIFIED",
                epg_id=selected_epg_id,
                provenance=provenance,
            )
        )
    return tuple(decisions)


def validate_high_agreement(
    prepared: PreparedClusterReview,
    result: object,
    *,
    verification: LocalVerificationSnapshot,
    terminal_rows: Sequence[TerminalRowState],
    decision_epoch: int,
) -> tuple[RowApprovalDecision, ...]:
    """Fail-closed public wrapper around strict local verification.

    Trusted integration-shape mistakes should be covered by tests, but model
    output and provider-derived values remain hostile.  No malformed value may
    escape as an exception that a caller could accidentally reinterpret.
    """

    try:
        return _validate_high_agreement_impl(
            prepared,
            result,
            verification=verification,
            terminal_rows=terminal_rows,
            decision_epoch=decision_epoch,
        )
    except Exception:
        try:
            return _review_all(prepared, "LOCAL_VERIFICATION_INVALID")
        except Exception as exc:  # trusted caller supplied no usable row identity
            raise PolicyInputError(
                "The prepared review is too malformed to return row decisions."
            ) from exc


__all__ = [
    "AI_VERIFIED_GATE",
    "ApprovalProvenance",
    "CandidateEvidence",
    "CatalogCandidateState",
    "LocalRankingEvidence",
    "LocalVerificationSnapshot",
    "CONTEXTUAL_RANKER_ID",
    "MAX_ROWS_PER_GEMINI_CALL",
    "MAX_ROWS_PER_RUN",
    "MAX_VERIFICATION_AGE_SECONDS",
    "NAME_RANKER_ID",
    "POLICY_VERSION",
    "PolicyInputError",
    "PreparedClusterReview",
    "ProgrammeGateEvidence",
    "ProtectedSemantics",
    "ReviewCluster",
    "ReviewPlan",
    "ReviewRowEvidence",
    "RowApprovalDecision",
    "TerminalRowState",
    "cluster_exact_compatible_rows",
    "partition_review_requests",
    "prepare_cluster_review",
    "prepare_cluster_reviews",
    "validate_high_agreement",
]
