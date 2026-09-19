"""Immutable data contracts for private matching-lab artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
SAFE_CODE_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,79}\Z")
SAFE_KEY_RE = re.compile(r"\A[a-zA-Z0-9._:-]{1,96}\Z")
MAX_TEXT = 2_000
BIDI_CONTROL_CHARACTERS = frozenset(
    {
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069",
    }
)


class ContractError(ValueError):
    """A private artifact failed its fixed schema or integrity contract."""


class DecisionState(str, Enum):
    AUTO_ELIGIBLE = "AUTO_ELIGIBLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PENDING_PROGRAMME = "PENDING_PROGRAMME"
    ABSTAIN = "ABSTAIN"
    BLOCKED_ALERT = "BLOCKED_ALERT"
    CONFLICT = "CONFLICT"


class ProgrammeState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_CHECKED = "NOT_CHECKED"


def safe_text(value: object, *, maximum: int = MAX_TEXT) -> str:
    """Return bounded NFKC text and reject controls or bidi overrides."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    if len(text) > int(maximum):
        raise ContractError("A proposal text field exceeds its size limit.")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"} and character not in {"\t", "\n", "\r"}:
            raise ContractError("A proposal text field contains a control character.")
        if character in BIDI_CONTROL_CHARACTERS:
            raise ContractError("A proposal text field contains bidi control data.")
    return text


def safe_display_text(value: object, *, maximum: int = MAX_TEXT) -> str:
    """Canonicalize human-readable provider text without retaining bidi controls."""

    raw = str(value or "")
    without_bidi = "".join(
        character
        for character in raw
        if character not in BIDI_CONTROL_CHARACTERS
    )
    return safe_text(without_bidi, maximum=maximum)


def require_opaque_identifier(
    value: object, *, label: str, maximum: int
) -> str:
    """Validate an opaque identity without changing any Unicode code point."""

    text = str(value or "")
    if not text:
        raise ContractError(f"{label} is blank.")
    if len(text) > int(maximum):
        raise ContractError(f"{label} exceeds its size limit.")
    if text != text.strip():
        raise ContractError(f"{label} contains leading or trailing whitespace.")
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cs"}:
            raise ContractError(f"{label} contains a control character.")
        if character in BIDI_CONTROL_CHARACTERS:
            raise ContractError(f"{label} contains bidi control data.")
    return text


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one value with a stable, NaN-free UTF-8 representation."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("A value cannot be canonically serialized.") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def require_sha256(value: object, *, label: str) -> str:
    text = str(value or "").casefold()
    if SHA256_RE.fullmatch(text) is None:
        raise ContractError(f"{label} is not a SHA-256 digest.")
    return text


def require_code(value: object, *, label: str) -> str:
    text = str(value or "").upper()
    if SAFE_CODE_RE.fullmatch(text) is None:
        raise ContractError(f"{label} is not a fixed reason code.")
    return text


def require_score(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer basis-point score.")
    if not 0 <= value <= 1_000_000:
        raise ContractError(f"{label} is outside 0..1000000.")
    return value


@dataclass(frozen=True, slots=True)
class ProtectedSemantics:
    market: str = ""
    direction: str = ""
    timeshift: str = ""
    quality: str = ""
    has_plus: bool = False
    has_extra: bool = False
    has_alternate: bool = False
    numbers: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    content: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("market", "direction", "timeshift", "quality"):
            value = safe_text(getattr(self, name), maximum=80).upper()
            object.__setattr__(self, name, value)
        for name in ("has_plus", "has_extra", "has_alternate"):
            if type(getattr(self, name)) is not bool:
                raise ContractError(f"{name} must be exactly boolean.")
        for name in ("numbers", "languages", "content"):
            values = tuple(
                sorted(
                    {
                        safe_text(item, maximum=80).casefold()
                        for item in tuple(getattr(self, name))
                        if safe_text(item, maximum=80)
                    }
                )
            )
            object.__setattr__(self, name, values)

    def public_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "direction": self.direction,
            "timeshift": self.timeshift,
            "quality": self.quality,
            "has_plus": self.has_plus,
            "has_extra": self.has_extra,
            "has_alternate": self.has_alternate,
            "numbers": list(self.numbers),
            "languages": list(self.languages),
            "content": list(self.content),
        }


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    candidate_key: str
    epg_id: str
    display_name: str
    feed: str
    region: str
    score_ppm: int
    methods: tuple[str, ...]
    conflicts: tuple[str, ...]
    features_ppm: tuple[tuple[str, int], ...]
    semantics: ProtectedSemantics
    programme_state: ProgrammeState = ProgrammeState.NOT_CHECKED
    programme_count: int = 0
    programme_first_start_epoch: int | None = None
    programme_latest_stop_epoch: int | None = None
    programme_reason: str = ""

    def __post_init__(self) -> None:
        if SAFE_KEY_RE.fullmatch(str(self.candidate_key or "")) is None:
            raise ContractError("candidate_key is invalid.")
        object.__setattr__(
            self,
            "epg_id",
            require_opaque_identifier(
                self.epg_id, label="Candidate epg_id", maximum=300
            ),
        )
        for name, maximum in (
            ("display_name", 300), ("feed", 80), ("region", 40)
        ):
            value = safe_text(getattr(self, name), maximum=maximum)
            if not value:
                raise ContractError(f"Candidate {name} is blank.")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "region", self.region.upper())
        object.__setattr__(self, "feed", self.feed.upper())
        require_score(self.score_ppm, label="candidate score")
        methods = tuple(sorted({require_code(value, label="candidate method") for value in self.methods}))
        conflicts = tuple(sorted({require_code(value, label="candidate conflict") for value in self.conflicts}))
        features: list[tuple[str, int]] = []
        seen_features: set[str] = set()
        for name, score in self.features_ppm:
            feature = require_code(name, label="candidate feature")
            if feature in seen_features:
                raise ContractError("Candidate features contain a duplicate name.")
            seen_features.add(feature)
            features.append((feature, require_score(score, label="feature score")))
        object.__setattr__(self, "methods", methods)
        object.__setattr__(self, "conflicts", conflicts)
        object.__setattr__(self, "features_ppm", tuple(sorted(features)))
        if not isinstance(self.programme_state, ProgrammeState):
            object.__setattr__(self, "programme_state", ProgrammeState(str(self.programme_state)))
        if isinstance(self.programme_count, bool) or int(self.programme_count) < 0:
            raise ContractError("programme_count is invalid.")
        object.__setattr__(self, "programme_count", int(self.programme_count))
        for name in ("programme_first_start_epoch", "programme_latest_stop_epoch"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) < 0):
                raise ContractError(f"{name} is invalid.")
            if value is not None:
                object.__setattr__(self, name, int(value))
        object.__setattr__(
            self, "programme_reason", safe_text(self.programme_reason, maximum=500)
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "candidate_key": self.candidate_key,
            "epg_id": self.epg_id,
            "display_name": self.display_name,
            "feed": self.feed,
            "region": self.region,
            "score_ppm": self.score_ppm,
            "methods": list(self.methods),
            "conflicts": list(self.conflicts),
            "features_ppm": {name: score for name, score in self.features_ppm},
            "semantics": self.semantics.public_dict(),
            "programme": {
                "state": self.programme_state.value,
                "count": self.programme_count,
                "first_start_epoch": self.programme_first_start_epoch,
                "latest_stop_epoch": self.programme_latest_stop_epoch,
                "reason": self.programme_reason,
            },
        }


EXACT_TIER_METHODS = frozenset(
    {
        "BAG_EXACT",
        "COMPACT_EXACT",
        "CURATED_ALIAS_EXACT",
        "CURATED_STORAGE_ALIAS_EXACT",
        "FROZEN_RESOLVER_EXACT",
        "HUMAN_ALIAS_EXACT",
        "REPOSITORY_CURATED_ALIAS_EXACT",
        "RELAXED_EXACT",
        "STRICT_EXACT",
    }
)


def retained_exact_tier_families(
    candidates: Sequence[CandidateEvidence],
) -> tuple[CandidateEvidence, ...]:
    """Return compatible retained targets carrying exact-tier evidence.

    Each retained target is deliberately treated as a separate family.  This
    keeps parallel SD/HD IDs or duplicate exact catalog IDs review-only even
    when the frozen resolver selected one of them.
    """

    return tuple(
        candidate
        for candidate in candidates
        if not candidate.conflicts
        and EXACT_TIER_METHODS.intersection(candidate.methods)
    )


@dataclass(frozen=True, slots=True)
class AIReviewEvidence:
    request_sha256: str
    model: str
    prompt_version: str
    decision: str
    candidate_key: str = ""
    confidence: str = "NONE"
    error_code: str = ""
    cached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_sha256", require_sha256(self.request_sha256, label="AI request hash")
        )
        for name, maximum in (
            ("model", 120), ("prompt_version", 80), ("decision", 20),
            ("candidate_key", 96), ("confidence", 20), ("error_code", 80),
        ):
            object.__setattr__(self, name, safe_text(getattr(self, name), maximum=maximum))
        if type(self.cached) is not bool:
            raise ContractError("AI cached must be exactly boolean.")

    def public_dict(self) -> dict[str, object]:
        return {
            "request_sha256": self.request_sha256,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "decision": self.decision,
            "candidate_key": self.candidate_key,
            "confidence": self.confidence,
            "error_code": self.error_code,
            "cached": self.cached,
        }


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    schema: str
    proposal_id: str
    run_id: str
    created_at: str
    expires_at: str
    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    row_guard_sha256: str
    provider_identity_sha256: str
    state: DecisionState
    reason_codes: tuple[str, ...]
    route_explicit: bool
    market: str
    selected_candidate_key: str
    score_ppm: int
    margin_ppm: int
    candidates: tuple[CandidateEvidence, ...]
    source_sha256: str
    text_catalog_sha256: str
    catalog_fingerprint_sha256: str
    catalog_generation_token: str
    policy_sha256: str
    lab_code_sha256: str
    auto_apply_eligible: bool = False
    ai: AIReviewEvidence | None = None

    def __post_init__(self) -> None:
        for name in (
            "proposal_id", "run_id", "row_guard_sha256", "provider_identity_sha256",
            "source_sha256", "text_catalog_sha256", "catalog_fingerprint_sha256",
            "policy_sha256", "lab_code_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), label=name))
        object.__setattr__(
            self,
            "stream_id",
            require_opaque_identifier(
                self.stream_id, label="stream_id", maximum=120
            ),
        )
        for name, maximum in (
            ("schema", 80), ("created_at", 40), ("expires_at", 40),
            ("server_id", 40), ("market", 40),
            ("selected_candidate_key", 96), ("catalog_generation_token", 80),
        ):
            object.__setattr__(self, name, safe_text(getattr(self, name), maximum=maximum))
        object.__setattr__(
            self,
            "channel_name",
            safe_display_text(self.channel_name, maximum=300),
        )
        object.__setattr__(
            self,
            "category_name",
            safe_display_text(self.category_name, maximum=200),
        )
        if not isinstance(self.state, DecisionState):
            object.__setattr__(self, "state", DecisionState(str(self.state)))
        reasons = tuple(sorted({require_code(value, label="proposal reason") for value in self.reason_codes}))
        object.__setattr__(self, "reason_codes", reasons)
        if type(self.route_explicit) is not bool or type(self.auto_apply_eligible) is not bool:
            raise ContractError("Proposal boolean fields must be exactly boolean.")
        require_score(self.score_ppm, label="proposal score")
        require_score(self.margin_ppm, label="proposal margin")
        candidates = tuple(self.candidates)
        if len(candidates) > 8:
            raise ContractError("A proposal may retain at most eight candidates.")
        keys = [candidate.candidate_key for candidate in candidates]
        if len(keys) != len(set(keys)):
            raise ContractError("Proposal candidate keys must be unique.")
        if self.selected_candidate_key and self.selected_candidate_key not in set(keys):
            raise ContractError("Selected candidate is absent from the proposal shortlist.")
        if self.auto_apply_eligible and self.state is not DecisionState.AUTO_ELIGIBLE:
            raise ContractError("Only AUTO_ELIGIBLE proposals may enter an apply lane.")
        object.__setattr__(self, "candidates", candidates)

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "identity": {
                "server_id": self.server_id,
                "stream_id": self.stream_id,
                "channel_name": self.channel_name,
                "category_name": self.category_name,
                "row_guard_sha256": self.row_guard_sha256,
                "provider_identity_sha256": self.provider_identity_sha256,
            },
            "decision": {
                "state": self.state.value,
                "reason_codes": list(self.reason_codes),
                "route_explicit": self.route_explicit,
                "market": self.market,
                "selected_candidate_key": self.selected_candidate_key,
                "score_ppm": self.score_ppm,
                "margin_ppm": self.margin_ppm,
                "auto_apply_eligible": self.auto_apply_eligible,
            },
            "candidates": [candidate.public_dict() for candidate in self.candidates],
            "ai": self.ai.public_dict() if self.ai is not None else None,
            "evidence": {
                "source_sha256": self.source_sha256,
                "text_catalog_sha256": self.text_catalog_sha256,
                "catalog_fingerprint_sha256": self.catalog_fingerprint_sha256,
                "catalog_generation_token": self.catalog_generation_token,
                "policy_sha256": self.policy_sha256,
                "lab_code_sha256": self.lab_code_sha256,
            },
        }

    def computed_id(self) -> str:
        return sha256_json(self.unsigned_dict())

    def public_dict(self) -> dict[str, object]:
        value = self.unsigned_dict()
        return {"proposal_id": self.proposal_id, **value}

    def verify_id(self) -> None:
        if self.proposal_id != self.computed_id():
            raise ContractError("Proposal content does not match proposal_id.")


@dataclass(frozen=True, slots=True)
class RunManifest:
    schema: str
    run_id: str
    mode: str
    generated_at: str
    expires_at: str
    input_sha256: Mapping[str, str]
    policy_sha256: str
    lab_code_sha256: str
    proposal_count: int
    proposals_sha256: str
    summary_sha256: str
    counts: Mapping[str, int] = field(default_factory=dict)
    private_artifact: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_sha256(self.run_id, label="run_id"))
        object.__setattr__(self, "policy_sha256", require_sha256(self.policy_sha256, label="policy hash"))
        object.__setattr__(self, "lab_code_sha256", require_sha256(self.lab_code_sha256, label="lab code hash"))
        object.__setattr__(self, "proposals_sha256", require_sha256(self.proposals_sha256, label="proposals hash"))
        object.__setattr__(self, "summary_sha256", require_sha256(self.summary_sha256, label="summary hash"))
        if type(self.private_artifact) is not bool or not self.private_artifact:
            raise ContractError("Matching Lab runs must remain private artifacts.")
        if isinstance(self.proposal_count, bool) or int(self.proposal_count) < 0:
            raise ContractError("proposal_count is invalid.")
        object.__setattr__(self, "proposal_count", int(self.proposal_count))
        inputs = {
            safe_text(name, maximum=100): require_sha256(digest, label=f"input {name}")
            for name, digest in dict(self.input_sha256).items()
        }
        if not inputs:
            raise ContractError("A run manifest requires input hashes.")
        object.__setattr__(self, "input_sha256", MappingProxyType(dict(sorted(inputs.items()))))
        counts: dict[str, int] = {}
        for name, value in dict(self.counts).items():
            key = require_code(name, label="summary count")
            if isinstance(value, bool) or int(value) < 0:
                raise ContractError("A summary count is invalid.")
            counts[key] = int(value)
        object.__setattr__(self, "counts", MappingProxyType(dict(sorted(counts.items()))))

    def public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "input_sha256": dict(self.input_sha256),
            "policy_sha256": self.policy_sha256,
            "lab_code_sha256": self.lab_code_sha256,
            "proposal_count": self.proposal_count,
            "proposals_sha256": self.proposals_sha256,
            "summary_sha256": self.summary_sha256,
            "counts": dict(self.counts),
            "private_artifact": self.private_artifact,
        }


def ensure_finite_scores(values: Sequence[float]) -> None:
    if any(not math.isfinite(float(value)) for value in values):
        raise ContractError("A score contains NaN or infinity.")


__all__ = (
    "AIReviewEvidence",
    "CandidateEvidence",
    "ContractError",
    "DecisionState",
    "ProgrammeState",
    "ProposalRecord",
    "ProtectedSemantics",
    "RunManifest",
    "EXACT_TIER_METHODS",
    "canonical_json_bytes",
    "ensure_finite_scores",
    "retained_exact_tier_families",
    "require_code",
    "require_score",
    "require_sha256",
    "safe_text",
    "sha256_bytes",
    "sha256_json",
)
