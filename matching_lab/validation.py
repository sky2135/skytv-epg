"""Strict, read-only validation for private Matching Lab shadow bundles.

This module deliberately has no Google or other remote-write capability.  It
accepts only the canonical bundle currently emitted by :mod:`matching_lab.pipeline`
and can optionally bind that bundle to current private Mappings and Sync Alerts
CSV snapshots.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import LAB_VERSION, PROPOSAL_SCHEMA, RUN_MANIFEST_SCHEMA
from .artifacts import (
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSAL_LINE_BYTES,
    package_code_sha256,
    read_stable_regular_file,
    strict_json_loads,
)
from .models import (
    AIReviewEvidence,
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    RunManifest,
    canonical_json_bytes,
    require_code,
    safe_display_text,
    safe_text,
    sha256_bytes,
    sha256_json,
)
from .normalization import lexical_cohort_risk_codes
from .policy import DEFAULT_POLICY


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
MAX_PROPOSALS = 30_000
SUPPORTED_SERVERS = ("server_1", "server_2", "server_3")
AI_PROMPT_VERSION = "matching-lab-advisory-v1"
PROGRAMME_MINIMUM_COUNT = 2
PROGRAMME_MINIMUM_FUTURE_SECONDS = 6 * 60 * 60
PROGRAMME_MAXIMUM_INITIAL_GAP_SECONDS = 6 * 60 * 60
PROGRAMME_PASS_REASON = (
    "Exact ID has a non-placeholder current/future programme guide."
)

# These fields are the versioned row preimage used by the current proposal
# producer.  Keeping the list here also lets bundle-only validation run without
# importing the large matcher/runtime dependency graph.
_MATCHER_INPUT_FIELDS = (
    "server_id",
    "stream_id",
    "channel_name",
    "canonical_name",
    "category_id",
    "category_name",
    "channel_number",
    "country_codes",
    "language_codes",
    "genre",
    "subgenres",
    "sport_codes",
    "religion_codes",
    "tags",
    "enabled",
    "action",
    "source",
    "epg_feed",
    "epg_id",
    "reason",
    "notes",
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "mode",
        "generated_at",
        "expires_at",
        "input_sha256",
        "policy_sha256",
        "lab_code_sha256",
        "proposal_count",
        "proposals_sha256",
        "summary_sha256",
        "counts",
        "private_artifact",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "run_id",
        "generated_at",
        "lab_version",
        "policy_id",
        "private_details_emitted",
        "counts",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {
        "proposal_id",
        "schema",
        "run_id",
        "created_at",
        "expires_at",
        "identity",
        "decision",
        "candidates",
        "ai",
        "evidence",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "server_id",
        "stream_id",
        "channel_name",
        "category_name",
        "row_guard_sha256",
        "provider_identity_sha256",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "state",
        "reason_codes",
        "route_explicit",
        "market",
        "selected_candidate_key",
        "score_ppm",
        "margin_ppm",
        "auto_apply_eligible",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_key",
        "epg_id",
        "display_name",
        "feed",
        "region",
        "score_ppm",
        "methods",
        "conflicts",
        "features_ppm",
        "semantics",
        "programme",
    }
)
_SEMANTICS_FIELDS = frozenset(
    {
        "market",
        "direction",
        "timeshift",
        "has_plus",
        "has_extra",
        "has_alternate",
        "numbers",
        "languages",
        "content",
    }
)
_PROGRAMME_FIELDS = frozenset(
    {"state", "count", "first_start_epoch", "latest_stop_epoch", "reason"}
)
_AI_FIELDS = frozenset(
    {
        "request_sha256",
        "model",
        "prompt_version",
        "decision",
        "candidate_key",
        "confidence",
        "error_code",
        "cached",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "source_sha256",
        "text_catalog_sha256",
        "catalog_fingerprint_sha256",
        "catalog_generation_token",
        "policy_sha256",
        "lab_code_sha256",
    }
)
_INPUT_HASH_FIELDS = frozenset(
    {
        "MAPPING_FILE",
        "MAPPING_TABLE",
        "ALERTS_FILE",
        "OPEN_ALERT_KEYS",
        "EPG_XML",
        "EPG_XML_CATALOG",
        "EPG_TEXT",
        "EPG_TEXT_CATALOG",
        "AI_CONFIG",
    }
)
_METRIC_COUNT_FIELDS = frozenset(
    {"REVIEW_ROWS", "CATALOG_CANDIDATES", "PROGRAMME_IDS_CHECKED"}
)
_AI_COUNT_FIELDS = frozenset(
    {
        "AI_ELIGIBLE",
        "AI_REQUESTED",
        "AI_DEFERRED",
        "AI_SUPPORTED",
        "AI_REQUIRES_REVIEW",
    }
)
_STATE_COUNT_FIELDS = frozenset(state.value for state in DecisionState)
_COUNT_FIELDS = _METRIC_COUNT_FIELDS | _AI_COUNT_FIELDS | _STATE_COUNT_FIELDS

_REASON_CODES = frozenset(
    {
        "ALERT_SNAPSHOT_MISSING",
        "AMBIGUOUS_CANDIDATES",
        "CATALOG_CORROBORATED",
        "CURATED_ALIAS_EXACT",
        "INVALID_REVIEW_STATE",
        "LEARNED_ALIAS_EXACT",
        "LOW_CALIBRATED_CONFIDENCE",
        "LOW_MARGIN",
        "COHORT_CONSERVATIVE",
        "COHORT_STANDARD",
        "LANE_ABSTAIN",
        "LANE_CONFLICT",
        "LANE_CONSERVATIVE_STRICT_EXACT",
        "LANE_HUMAN_REVIEW",
        "LANE_PROGRAMME_VERIFICATION",
        "LANE_STANDARD_STRONG",
        "LANE_TRUSTED_ALIAS",
        "MARGIN_GATE_PASSED",
        "MARKET_ROUTE_EXPLICIT",
        "MARKET_UNKNOWN",
        "MULTI_SIGNAL_STRONG_PROPOSAL",
        "NO_CANDIDATE",
        "OPEN_SYNC_ALERT",
        "PROGRAMME_GATE_FAILED",
        "PROGRAMME_GATE_PASSED",
        "PROGRAMME_GATE_PENDING",
        "PROVIDER_REVALIDATION_REQUIRED",
        "PROTECTED_SEMANTICS_COMPATIBLE",
        "PROTECTED_SEMANTICS_CONFLICT",
        "ROW_NOT_DISABLED_REVIEW",
        "SHADOW_ONLY_NO_WRITE_AUTHORITY",
        "STANDARD_LANE_THRESHOLD_PASSED",
        "STRICT_EXACT_UNIQUE_CANDIDATE",
        "NON_STRICT_AUTO_BLOCKED_CONSERVATIVE_COHORT",
        "PREFILLED_ID_CATALOG_REVALIDATED",
        "PREFILLED_ID_NOT_CORROBORATED",
        "PREFILLED_ID_UNTRUSTED_EVIDENCE",
        "RISK_DIRECTION_VARIANT",
        "RISK_EDITION_VARIANT",
        "RISK_EXPLICIT_LANGUAGE",
        "RISK_EXPLICIT_NUMBER",
        "RISK_PLUS_VARIANT",
        "RISK_SOUTH_ASIA_COHORT",
        "RISK_TIMESHIFT_VARIANT",
        "UNSUPPORTED_CHANNEL_CLASS",
        "AI_SUPPORTS_LOCAL",
        "AI_DISAGREES",
        "AI_LOW_CONFIDENCE",
        "AI_ABSTAINED",
        "AI_REVIEW_ERROR",
        "ALTERNATE_VARIANT_MISMATCH",
        "CONTENT_FAMILY_MISMATCH",
        "DIRECTION_MISMATCH",
        "EXTRA_VARIANT_MISMATCH",
        "LANGUAGE_MISMATCH",
        "MARKET_MISMATCH",
        "NUMBER_MISMATCH",
        "PLUS_VARIANT_MISMATCH",
        "TIMESHIFT_MISMATCH",
    }
)
_CANDIDATE_METHODS = frozenset(
    {
        "ACRONYM_RETRIEVAL",
        "BAG_EXACT",
        "CHAR_NGRAM_RETRIEVAL",
        "COMPACT_EXACT",
        "CURATED_ALIAS_EXACT",
        "HUMAN_ALIAS_EXACT",
        "RELAXED_EXACT",
        "STRICT_EXACT",
        "TOKEN_RETRIEVAL",
        "TRANSLITERATION_RETRIEVAL",
        "XML_DISPLAY_RETRIEVAL",
    }
)
_CANDIDATE_CONFLICTS = frozenset(
    {
        "ALTERNATE_VARIANT_MISMATCH",
        "CONTENT_FAMILY_MISMATCH",
        "DIRECTION_MISMATCH",
        "EXTRA_VARIANT_MISMATCH",
        "LANGUAGE_MISMATCH",
        "MARKET_MISMATCH",
        "NUMBER_MISMATCH",
        "PLUS_VARIANT_MISMATCH",
        "TIMESHIFT_MISMATCH",
    }
)
_CANDIDATE_FEATURES = frozenset(
    {
        "ACRONYM_SCORE",
        "CONTEXTUAL_SCORE",
        "NGRAM_SCORE",
        "TOKEN_SCORE",
        "TRANSLITERATION_SCORE",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """A fully verified, non-mutating view of one private shadow bundle."""

    bundle_dir: Path
    run_id: str
    generated_at: str
    expires_at: str
    validated_as_of: str
    proposal_count: int
    counts: Mapping[str, int]
    proposals: tuple[ProposalRecord, ...]
    mappings_checked: bool
    alerts_checked: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "counts", MappingProxyType(dict(sorted(dict(self.counts).items())))
        )
        object.__setattr__(self, "proposals", tuple(self.proposals))


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = str(value or "")
    if text.isdigit():
        return 0, int(text), text
    return 1, text.casefold(), text


def _mapping_row_guard(row: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "skytv-matching-row-guard-v1",
            "values": [
                [field, str(row.get(field, ""))] for field in _MATCHER_INPUT_FIELDS
            ],
        }
    )


def _provider_identity_guard(row: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "skytv-provider-identity-v1",
            "server_id": str(row.get("server_id", "")),
            "stream_id": str(row.get("stream_id", "")),
            "channel_name": str(row.get("channel_name", "")),
            "category_id": str(row.get("category_id", "")),
            "category_name": str(row.get("category_name", "")),
        }
    )


def _sheet_runtime() -> tuple[Any, Any]:
    """Load the legacy Sheet parsers only for optional current-state checks."""

    try:
        from .compat import streaming, sync
    except ImportError as exc:
        raise ContractError(
            "Current Mappings/alerts validation requires the pinned sync dependencies."
        ) from exc
    return streaming, sync


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{label} must be a JSON object.")
    return value


def _list(value: object, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise ContractError(f"{label} must be a JSON array.")
    return value


def _string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ContractError(f"{label} must be a string.")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label} must be an integer of at least {minimum}.")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{label} must be exactly boolean.")
    return value


def _optional_integer(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label=label)


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        extra = sorted(actual.difference(expected))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("unknown=" + ",".join(extra))
        raise ContractError(f"{label} has invalid fields ({'; '.join(detail)}).")


def _canonical_document(
    path: Path, *, maximum_bytes: int, label: str
) -> tuple[dict[str, Any], bytes, str]:
    content, digest = read_stable_regular_file(path, maximum_bytes=maximum_bytes)
    parsed = strict_json_loads(content)
    value = _mapping(parsed, label=label)
    if content != canonical_json_bytes(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON with one final newline.")
    return value, content, digest


def _parse_utc(value: object, *, label: str) -> datetime:
    text = _string(value, label=label)
    if not text.endswith("Z"):
        raise ContractError(f"{label} must be canonical UTC ending in Z.")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is not an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError(f"{label} must use UTC.")
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if parsed.microsecond or text != canonical:
        raise ContractError(f"{label} must use canonical whole-second UTC form.")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_as_of(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ContractError("Validation as-of must include the UTC timezone.")
        if value.microsecond:
            raise ContractError("Validation as-of must use whole seconds.")
        return value.astimezone(timezone.utc)
    return _parse_utc(value, label="validation as-of")


def _parse_counts(value: object, *, label: str) -> dict[str, int]:
    raw = _mapping(value, label=label)
    unknown = set(raw).difference(_COUNT_FIELDS)
    if unknown:
        raise ContractError(f"{label} contains an unknown count name.")
    if not (_METRIC_COUNT_FIELDS | _AI_COUNT_FIELDS).issubset(raw):
        raise ContractError(f"{label} is missing required aggregate counts.")
    result = {
        _string(name, label=f"{label} key"): _integer(
            count, label=f"{label} value"
        )
        for name, count in raw.items()
    }
    return dict(sorted(result.items()))


def _parse_manifest(raw: Mapping[str, Any]) -> RunManifest:
    _exact_fields(raw, _MANIFEST_FIELDS, label="manifest")
    if _string(raw["schema"], label="manifest schema") != RUN_MANIFEST_SCHEMA:
        raise ContractError("The bundle manifest schema is unsupported.")
    if _string(raw["mode"], label="manifest mode") != "shadow":
        raise ContractError("Only read-only shadow bundles are supported.")
    inputs = _mapping(raw["input_sha256"], label="manifest input hashes")
    if frozenset(inputs) != _INPUT_HASH_FIELDS:
        raise ContractError("The manifest input-hash set is incomplete or unknown.")
    counts = _parse_counts(raw["counts"], label="manifest counts")
    manifest = RunManifest(
        schema=_string(raw["schema"], label="manifest schema"),
        run_id=_string(raw["run_id"], label="manifest run_id"),
        mode=_string(raw["mode"], label="manifest mode"),
        generated_at=_string(raw["generated_at"], label="manifest generated_at"),
        expires_at=_string(raw["expires_at"], label="manifest expires_at"),
        input_sha256={
            _string(name, label="manifest input name"): _string(
                digest, label="manifest input hash"
            )
            for name, digest in inputs.items()
        },
        policy_sha256=_string(raw["policy_sha256"], label="manifest policy hash"),
        lab_code_sha256=_string(
            raw["lab_code_sha256"], label="manifest lab-code hash"
        ),
        proposal_count=_integer(
            raw["proposal_count"], label="manifest proposal_count"
        ),
        proposals_sha256=_string(
            raw["proposals_sha256"], label="manifest proposals hash"
        ),
        summary_sha256=_string(
            raw["summary_sha256"], label="manifest summary hash"
        ),
        counts=counts,
        private_artifact=_boolean(
            raw["private_artifact"], label="manifest private_artifact"
        ),
    )
    if manifest.proposal_count > MAX_PROPOSALS:
        raise ContractError("The manifest proposal count exceeds its safety limit.")
    if manifest.policy_sha256 != DEFAULT_POLICY.sha256:
        raise ContractError("The bundle uses an unsupported Matching Lab policy.")
    if manifest.lab_code_sha256 != package_code_sha256(Path(__file__).resolve().parent):
        raise ContractError("The bundle was produced by different Matching Lab code.")
    if manifest.public_dict() != dict(raw):
        raise ContractError("The manifest is not in normalized schema form.")
    return manifest


def _parse_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(raw, _SUMMARY_FIELDS, label="summary")
    if _string(raw["schema"], label="summary schema") != "skytv.matching-lab-summary.v1":
        raise ContractError("The bundle summary schema is unsupported.")
    if _string(raw["mode"], label="summary mode") != "shadow":
        raise ContractError("The bundle summary is not in shadow mode.")
    if _string(raw["lab_version"], label="summary lab_version") != LAB_VERSION:
        raise ContractError("The bundle was produced by an unsupported lab version.")
    if _boolean(
        raw["private_details_emitted"], label="summary private_details_emitted"
    ):
        raise ContractError("The aggregate summary claims private details were emitted.")
    policy_id = safe_text(
        _string(raw["policy_id"], label="summary policy_id"), maximum=100
    )
    if not policy_id or policy_id != raw["policy_id"]:
        raise ContractError("The summary policy_id is blank or non-normalized.")
    if policy_id != DEFAULT_POLICY.policy_id:
        raise ContractError("The summary uses an unsupported Matching Lab policy.")
    result = dict(raw)
    result["counts"] = _parse_counts(raw["counts"], label="summary counts")
    return result


def _parse_semantics(raw: object, *, label: str) -> ProtectedSemantics:
    value = _mapping(raw, label=label)
    _exact_fields(value, _SEMANTICS_FIELDS, label=label)
    semantics = ProtectedSemantics(
        market=_string(value["market"], label=f"{label} market"),
        direction=_string(value["direction"], label=f"{label} direction"),
        timeshift=_string(value["timeshift"], label=f"{label} timeshift"),
        has_plus=_boolean(value["has_plus"], label=f"{label} has_plus"),
        has_extra=_boolean(value["has_extra"], label=f"{label} has_extra"),
        has_alternate=_boolean(
            value["has_alternate"], label=f"{label} has_alternate"
        ),
        numbers=tuple(
            _string(item, label=f"{label} number")
            for item in _list(value["numbers"], label=f"{label} numbers")
        ),
        languages=tuple(
            _string(item, label=f"{label} language")
            for item in _list(value["languages"], label=f"{label} languages")
        ),
        content=tuple(
            _string(item, label=f"{label} content")
            for item in _list(value["content"], label=f"{label} content")
        ),
    )
    if semantics.public_dict() != value:
        raise ContractError(f"{label} is not in normalized schema form.")
    return semantics


def _parse_candidate(raw: object, *, line_number: int) -> CandidateEvidence:
    label = f"proposal line {line_number} candidate"
    value = _mapping(raw, label=label)
    _exact_fields(value, _CANDIDATE_FIELDS, label=label)
    methods = [
        _string(item, label=f"{label} method")
        for item in _list(value["methods"], label=f"{label} methods")
    ]
    conflicts = [
        _string(item, label=f"{label} conflict")
        for item in _list(value["conflicts"], label=f"{label} conflicts")
    ]
    if not methods or not set(methods).issubset(_CANDIDATE_METHODS):
        raise ContractError(f"{label} has an empty or unknown retrieval method.")
    if not set(conflicts).issubset(_CANDIDATE_CONFLICTS):
        raise ContractError(f"{label} has an unknown protected-semantics conflict.")
    features = _mapping(value["features_ppm"], label=f"{label} features")
    if frozenset(features) != _CANDIDATE_FEATURES:
        raise ContractError(f"{label} has an incomplete or unknown feature set.")
    programme = _mapping(value["programme"], label=f"{label} programme")
    _exact_fields(programme, _PROGRAMME_FIELDS, label=f"{label} programme")
    try:
        programme_state = ProgrammeState(
            _string(programme["state"], label=f"{label} programme state")
        )
    except ValueError as exc:
        raise ContractError(f"{label} has an unsupported programme state.") from exc
    candidate = CandidateEvidence(
        candidate_key=_string(value["candidate_key"], label=f"{label} key"),
        epg_id=_string(value["epg_id"], label=f"{label} EPG ID"),
        display_name=_string(
            value["display_name"], label=f"{label} display name"
        ),
        feed=_string(value["feed"], label=f"{label} feed"),
        region=_string(value["region"], label=f"{label} region"),
        score_ppm=_integer(value["score_ppm"], label=f"{label} score"),
        methods=tuple(methods),
        conflicts=tuple(conflicts),
        features_ppm=tuple(
            (
                _string(name, label=f"{label} feature name"),
                _integer(score, label=f"{label} feature score"),
            )
            for name, score in features.items()
        ),
        semantics=_parse_semantics(value["semantics"], label=f"{label} semantics"),
        programme_state=programme_state,
        programme_count=_integer(
            programme["count"], label=f"{label} programme count"
        ),
        programme_first_start_epoch=_optional_integer(
            programme["first_start_epoch"],
            label=f"{label} programme first_start_epoch",
        ),
        programme_latest_stop_epoch=_optional_integer(
            programme["latest_stop_epoch"],
            label=f"{label} programme latest_stop_epoch",
        ),
        programme_reason=_string(
            programme["reason"], label=f"{label} programme reason"
        ),
    )
    feature_scores = dict(candidate.features_ppm)
    expected_score = (
        feature_scores["CONTEXTUAL_SCORE"] * DEFAULT_POLICY.contextual_weight_ppm
        + feature_scores["TOKEN_SCORE"] * DEFAULT_POLICY.token_weight_ppm
        + feature_scores["NGRAM_SCORE"] * DEFAULT_POLICY.ngram_weight_ppm
        + feature_scores["TRANSLITERATION_SCORE"]
        * DEFAULT_POLICY.transliteration_weight_ppm
        + feature_scores["ACRONYM_SCORE"] * DEFAULT_POLICY.acronym_weight_ppm
    ) // 1_000_000
    if candidate.score_ppm != expected_score:
        raise ContractError(f"{label} score does not match its feature evidence.")
    if candidate.public_dict() != value:
        raise ContractError(f"{label} is not in normalized schema form.")
    return candidate


def _parse_ai(
    raw: object, *, line_number: int, candidate_keys: frozenset[str]
) -> AIReviewEvidence | None:
    if raw is None:
        return None
    label = f"proposal line {line_number} AI evidence"
    value = _mapping(raw, label=label)
    _exact_fields(value, _AI_FIELDS, label=label)
    evidence = AIReviewEvidence(
        request_sha256=_string(
            value["request_sha256"], label=f"{label} request hash"
        ),
        model=_string(value["model"], label=f"{label} model"),
        prompt_version=_string(
            value["prompt_version"], label=f"{label} prompt version"
        ),
        decision=_string(value["decision"], label=f"{label} decision"),
        candidate_key=_string(
            value["candidate_key"], label=f"{label} candidate key"
        ),
        confidence=_string(value["confidence"], label=f"{label} confidence"),
        error_code=_string(value["error_code"], label=f"{label} error code"),
        cached=_boolean(value["cached"], label=f"{label} cached"),
    )
    if not evidence.model or not evidence.prompt_version:
        raise ContractError(f"{label} lacks its model or prompt version.")
    if evidence.prompt_version != AI_PROMPT_VERSION:
        raise ContractError(f"{label} has an unsupported prompt version.")
    if evidence.cached:
        raise ContractError(f"{label} contains non-deterministic cache metadata.")
    if evidence.decision not in {"SUGGEST", "ABSTAIN", "ERROR"}:
        raise ContractError(f"{label} has an unsupported decision.")
    if evidence.confidence not in {"HIGH", "MEDIUM", "LOW", "NONE"}:
        raise ContractError(f"{label} has an unsupported confidence.")
    if evidence.candidate_key and evidence.candidate_key not in candidate_keys:
        raise ContractError(f"{label} selects a missing candidate key.")
    if evidence.decision == "SUGGEST":
        if (
            not evidence.candidate_key
            or evidence.confidence == "NONE"
            or evidence.error_code
        ):
            raise ContractError(f"{label} has an inconsistent suggestion.")
    elif evidence.decision == "ABSTAIN":
        if evidence.candidate_key or evidence.confidence != "NONE" or evidence.error_code:
            raise ContractError(f"{label} has an inconsistent abstention.")
    elif (
        evidence.candidate_key
        or evidence.confidence != "NONE"
        or not evidence.error_code
    ):
        raise ContractError(f"{label} has inconsistent error evidence.")
    if evidence.error_code:
        require_code(evidence.error_code, label=f"{label} error code")
    if evidence.public_dict() != value:
        raise ContractError(f"{label} is not in normalized schema form.")
    return evidence


def _parse_proposal(raw: object, *, line_number: int) -> ProposalRecord:
    label = f"proposal line {line_number}"
    value = _mapping(raw, label=label)
    _exact_fields(value, _PROPOSAL_FIELDS, label=label)
    if _string(value["schema"], label=f"{label} schema") != PROPOSAL_SCHEMA:
        raise ContractError(f"{label} has an unsupported schema.")
    identity = _mapping(value["identity"], label=f"{label} identity")
    decision = _mapping(value["decision"], label=f"{label} decision")
    evidence = _mapping(value["evidence"], label=f"{label} evidence")
    _exact_fields(identity, _IDENTITY_FIELDS, label=f"{label} identity")
    _exact_fields(decision, _DECISION_FIELDS, label=f"{label} decision")
    _exact_fields(evidence, _EVIDENCE_FIELDS, label=f"{label} evidence")
    reason_codes = [
        _string(item, label=f"{label} reason code")
        for item in _list(decision["reason_codes"], label=f"{label} reason codes")
    ]
    if not reason_codes or not set(reason_codes).issubset(_REASON_CODES):
        raise ContractError(f"{label} has an empty or unknown reason code.")
    candidate_values = _list(value["candidates"], label=f"{label} candidates")
    candidates = tuple(
        _parse_candidate(candidate, line_number=line_number)
        for candidate in candidate_values
    )
    expected_candidate_keys = tuple(
        f"c{index:03d}" for index in range(1, len(candidates) + 1)
    )
    actual_candidate_keys = tuple(candidate.candidate_key for candidate in candidates)
    if actual_candidate_keys != expected_candidate_keys:
        raise ContractError(f"{label} candidate keys are not contiguous and ordered.")
    epg_ids = [candidate.epg_id for candidate in candidates]
    if len(epg_ids) != len(set(epg_ids)):
        raise ContractError(f"{label} repeats an EPG ID in its shortlist.")
    ai = _parse_ai(
        value["ai"],
        line_number=line_number,
        candidate_keys=frozenset(actual_candidate_keys),
    )
    try:
        state = DecisionState(
            _string(decision["state"], label=f"{label} decision state")
        )
    except ValueError as exc:
        raise ContractError(f"{label} has an unsupported decision state.") from exc
    record = ProposalRecord(
        schema=_string(value["schema"], label=f"{label} schema"),
        proposal_id=_string(value["proposal_id"], label=f"{label} proposal_id"),
        run_id=_string(value["run_id"], label=f"{label} run_id"),
        created_at=_string(value["created_at"], label=f"{label} created_at"),
        expires_at=_string(value["expires_at"], label=f"{label} expires_at"),
        server_id=_string(identity["server_id"], label=f"{label} server_id"),
        stream_id=_string(identity["stream_id"], label=f"{label} stream_id"),
        channel_name=_string(
            identity["channel_name"], label=f"{label} channel_name"
        ),
        category_name=_string(
            identity["category_name"], label=f"{label} category_name"
        ),
        row_guard_sha256=_string(
            identity["row_guard_sha256"], label=f"{label} row guard"
        ),
        provider_identity_sha256=_string(
            identity["provider_identity_sha256"],
            label=f"{label} provider identity",
        ),
        state=state,
        reason_codes=tuple(reason_codes),
        route_explicit=_boolean(
            decision["route_explicit"], label=f"{label} route_explicit"
        ),
        market=_string(decision["market"], label=f"{label} market"),
        selected_candidate_key=_string(
            decision["selected_candidate_key"],
            label=f"{label} selected candidate",
        ),
        score_ppm=_integer(decision["score_ppm"], label=f"{label} score"),
        margin_ppm=_integer(decision["margin_ppm"], label=f"{label} margin"),
        candidates=candidates,
        source_sha256=_string(
            evidence["source_sha256"], label=f"{label} source hash"
        ),
        text_catalog_sha256=_string(
            evidence["text_catalog_sha256"], label=f"{label} text-catalog hash"
        ),
        catalog_fingerprint_sha256=_string(
            evidence["catalog_fingerprint_sha256"],
            label=f"{label} catalog fingerprint",
        ),
        catalog_generation_token=_string(
            evidence["catalog_generation_token"],
            label=f"{label} catalog generation token",
        ),
        policy_sha256=_string(
            evidence["policy_sha256"], label=f"{label} policy hash"
        ),
        lab_code_sha256=_string(
            evidence["lab_code_sha256"], label=f"{label} lab-code hash"
        ),
        auto_apply_eligible=_boolean(
            decision["auto_apply_eligible"],
            label=f"{label} auto_apply_eligible",
        ),
        ai=ai,
    )
    if record.auto_apply_eligible:
        raise ContractError(
            f"{label} requests auto-apply from an unsigned shadow bundle."
        )
    record.verify_id()
    if record.public_dict() != value:
        raise ContractError(f"{label} is not in normalized schema form.")
    _validate_proposal_decision(record, line_number=line_number)
    return record


def _validate_proposal_decision(
    proposal: ProposalRecord, *, line_number: int
) -> None:
    label = f"proposal line {line_number}"

    def evidence_tier(candidate: CandidateEvidence) -> int:
        methods = set(candidate.methods)
        if methods.intersection({"HUMAN_ALIAS_EXACT", "CURATED_ALIAS_EXACT"}):
            return 5
        if "STRICT_EXACT" in methods:
            return 4
        if methods.intersection({"RELAXED_EXACT", "COMPACT_EXACT", "BAG_EXACT"}):
            return 3
        fuzzy = methods.intersection(
            {
                "TOKEN_RETRIEVAL",
                "CHAR_NGRAM_RETRIEVAL",
                "TRANSLITERATION_RETRIEVAL",
                "ACRONYM_RETRIEVAL",
                "XML_DISPLAY_RETRIEVAL",
            }
        )
        return 2 if len(fuzzy) >= 2 else 1

    expected_order = tuple(
        sorted(
            proposal.candidates,
            key=lambda candidate: (
                1 if candidate.conflicts else 0,
                -evidence_tier(candidate),
                -candidate.score_ppm,
                candidate.epg_id.casefold(),
                candidate.epg_id,
            ),
        )
    )
    if proposal.candidates != expected_order:
        raise ContractError(f"{label} candidates are not in deterministic rank order.")
    generated_epoch = int(
        _parse_utc(proposal.created_at, label=f"{label} created_at").timestamp()
    )
    for candidate in proposal.candidates:
        first = candidate.programme_first_start_epoch
        latest = candidate.programme_latest_stop_epoch
        passes_gate = (
            first is not None
            and latest is not None
            and first <= latest
            and first <= generated_epoch + PROGRAMME_MAXIMUM_INITIAL_GAP_SECONDS
            and candidate.programme_count >= PROGRAMME_MINIMUM_COUNT
            and latest >= generated_epoch + PROGRAMME_MINIMUM_FUTURE_SECONDS
        )
        if candidate.programme_state is ProgrammeState.PASS and (
            not passes_gate or candidate.programme_reason != PROGRAMME_PASS_REASON
        ):
            raise ContractError(f"{label} contains invalid passing programme evidence.")
        if candidate.programme_state is ProgrammeState.FAIL and passes_gate:
            raise ContractError(f"{label} contains inconsistent failed programme evidence.")
        if candidate.programme_state is ProgrammeState.NOT_CHECKED and (
            candidate.programme_count
            or first is not None
            or latest is not None
            or candidate.programme_reason
            != "Candidate was not selected for programme verification."
        ):
            raise ContractError(f"{label} contains inconsistent unchecked programme evidence.")
    compatible = tuple(
        candidate for candidate in proposal.candidates if not candidate.conflicts
    )
    expected_score = compatible[0].score_ppm if compatible else 0
    second_score = compatible[1].score_ppm if len(compatible) > 1 else 0
    expected_margin = max(0, expected_score - second_score)
    if proposal.score_ppm != expected_score or proposal.margin_ppm != expected_margin:
        raise ContractError(f"{label} aggregate score or margin is inconsistent.")
    selected = next(
        (
            candidate
            for candidate in proposal.candidates
            if candidate.candidate_key == proposal.selected_candidate_key
        ),
        None,
    )
    if proposal.selected_candidate_key:
        if selected is None or selected.conflicts or not compatible or selected != compatible[0]:
            raise ContractError(f"{label} did not select its top compatible candidate.")
    if proposal.state in {
        DecisionState.AUTO_ELIGIBLE,
        DecisionState.NEEDS_REVIEW,
        DecisionState.PENDING_PROGRAMME,
    } and selected is None:
        raise ContractError(f"{label} state requires a selected candidate.")
    if proposal.state in {
        DecisionState.ABSTAIN,
        DecisionState.BLOCKED_ALERT,
        DecisionState.CONFLICT,
    } and proposal.selected_candidate_key:
        raise ContractError(f"{label} state cannot select a candidate.")
    selected_states = {
        DecisionState.AUTO_ELIGIBLE,
        DecisionState.NEEDS_REVIEW,
        DecisionState.PENDING_PROGRAMME,
    }
    reasons = set(proposal.reason_codes)
    risk_codes = {code for code in reasons if code.startswith("RISK_")}
    expected_risk_codes = set(
        lexical_cohort_risk_codes(
            proposal.channel_name,
            proposal.category_name,
            proposal.market,
        )
    )
    if selected is not None:
        semantics = selected.semantics
        if semantics.numbers:
            expected_risk_codes.add("RISK_EXPLICIT_NUMBER")
        if semantics.has_plus:
            expected_risk_codes.add("RISK_PLUS_VARIANT")
        if semantics.direction:
            expected_risk_codes.add("RISK_DIRECTION_VARIANT")
        if semantics.timeshift:
            expected_risk_codes.add("RISK_TIMESHIFT_VARIANT")
        if semantics.has_extra or semantics.has_alternate:
            expected_risk_codes.add("RISK_EDITION_VARIANT")
    cohort_codes = {"COHORT_STANDARD", "COHORT_CONSERVATIVE"}.intersection(
        reasons
    )
    if proposal.state in selected_states and (
        risk_codes != expected_risk_codes
        or len(cohort_codes) != 1
        or (bool(risk_codes) != ("COHORT_CONSERVATIVE" in cohort_codes))
    ):
        raise ContractError(f"{label} cohort evidence is inconsistent.")
    if proposal.state is DecisionState.NEEDS_REVIEW and "LANE_HUMAN_REVIEW" not in reasons:
        raise ContractError(f"{label} NEEDS_REVIEW lane is missing.")
    if proposal.state is DecisionState.PENDING_PROGRAMME and (
        "LANE_PROGRAMME_VERIFICATION" not in reasons
    ):
        raise ContractError(f"{label} programme-verification lane is missing.")
    if proposal.state is DecisionState.AUTO_ELIGIBLE:
        alias_methods = set(selected.methods if selected is not None else ()).intersection(
            {"HUMAN_ALIAS_EXACT", "CURATED_ALIAS_EXACT"}
        )
        expected_alias_reason = (
            "CURATED_ALIAS_EXACT"
            if "CURATED_ALIAS_EXACT" in alias_methods
            else "LEARNED_ALIAS_EXACT"
        )
        trusted_alias_lane = bool(alias_methods) and "LANE_TRUSTED_ALIAS" in reasons
        standard_lane = (
            not alias_methods
            and not risk_codes
            and "COHORT_STANDARD" in reasons
            and "LANE_STANDARD_STRONG" in reasons
            and "STANDARD_LANE_THRESHOLD_PASSED" in reasons
        )
        conservative_strict_lane = (
            not alias_methods
            and bool(risk_codes)
            and "COHORT_CONSERVATIVE" in reasons
            and "LANE_CONSERVATIVE_STRICT_EXACT" in reasons
            and "STRICT_EXACT_UNIQUE_CANDIDATE" in reasons
            and selected is not None
            and "STRICT_EXACT" in selected.methods
            and len(compatible) == 1
        )
        auto_lanes = {
            "LANE_TRUSTED_ALIAS",
            "LANE_STANDARD_STRONG",
            "LANE_CONSERVATIVE_STRICT_EXACT",
        }.intersection(reasons)
        if (
            selected is None
            or selected.programme_state is not ProgrammeState.PASS
            or not (trusted_alias_lane or standard_lane or conservative_strict_lane)
            or len(auto_lanes) != 1
            or proposal.score_ppm < DEFAULT_POLICY.strong_proposal_score_ppm
            or proposal.margin_ppm < DEFAULT_POLICY.strong_margin_ppm
            or not proposal.route_explicit
            or not proposal.market
            or "PROGRAMME_GATE_PASSED" not in proposal.reason_codes
            or "MARKET_ROUTE_EXPLICIT" not in proposal.reason_codes
            or "MARGIN_GATE_PASSED" not in proposal.reason_codes
            or "CATALOG_CORROBORATED" not in proposal.reason_codes
            or "PROTECTED_SEMANTICS_COMPATIBLE" not in proposal.reason_codes
            or "PROVIDER_REVALIDATION_REQUIRED" not in proposal.reason_codes
            or "SHADOW_ONLY_NO_WRITE_AUTHORITY" not in proposal.reason_codes
            or (
                trusted_alias_lane
                and expected_alias_reason not in proposal.reason_codes
            )
            or "ALERT_SNAPSHOT_MISSING" in proposal.reason_codes
            or "PROGRAMME_GATE_FAILED" in proposal.reason_codes
            or "PROGRAMME_GATE_PENDING" in proposal.reason_codes
            or "MARKET_UNKNOWN" in proposal.reason_codes
            or "LOW_MARGIN" in proposal.reason_codes
            or "AMBIGUOUS_CANDIDATES" in proposal.reason_codes
            or (
                trusted_alias_lane
                and {"CURATED_ALIAS_EXACT", "LEARNED_ALIAS_EXACT"}.intersection(
                    proposal.reason_codes
                )
                != {expected_alias_reason}
            )
            or (
                not trusted_alias_lane
                and {"CURATED_ALIAS_EXACT", "LEARNED_ALIAS_EXACT"}.intersection(
                    proposal.reason_codes
                )
            )
            or "LANE_HUMAN_REVIEW" in reasons
            or "MULTI_SIGNAL_STRONG_PROPOSAL" in reasons
        ):
            raise ContractError(f"{label} AUTO_ELIGIBLE evidence is incomplete.")
    if proposal.state is DecisionState.NEEDS_REVIEW and (
        selected is None
        or selected.programme_state is not ProgrammeState.PASS
        or proposal.score_ppm < DEFAULT_POLICY.minimum_candidate_score_ppm
        or "PROGRAMME_GATE_PASSED" not in proposal.reason_codes
        or "CATALOG_CORROBORATED" not in proposal.reason_codes
        or "PROTECTED_SEMANTICS_COMPATIBLE" not in proposal.reason_codes
        or "PROVIDER_REVALIDATION_REQUIRED" not in proposal.reason_codes
        or "PROGRAMME_GATE_FAILED" in proposal.reason_codes
        or "PROGRAMME_GATE_PENDING" in proposal.reason_codes
    ):
        raise ContractError(f"{label} NEEDS_REVIEW evidence is incomplete.")
    expected_programme_reason = (
        "PROGRAMME_GATE_PENDING"
        if selected is not None
        and selected.programme_state is ProgrammeState.NOT_CHECKED
        else "PROGRAMME_GATE_FAILED"
    )
    if proposal.state is DecisionState.PENDING_PROGRAMME and (
        selected is None
        or selected.programme_state is ProgrammeState.PASS
        or proposal.score_ppm < DEFAULT_POLICY.minimum_candidate_score_ppm
        or "CATALOG_CORROBORATED" not in proposal.reason_codes
        or "PROTECTED_SEMANTICS_COMPATIBLE" not in proposal.reason_codes
        or "PROVIDER_REVALIDATION_REQUIRED" not in proposal.reason_codes
        or expected_programme_reason not in proposal.reason_codes
        or "PROGRAMME_GATE_PASSED" in proposal.reason_codes
    ):
        raise ContractError(f"{label} PENDING_PROGRAMME evidence is inconsistent.")
    if proposal.state is DecisionState.BLOCKED_ALERT and (
        tuple(proposal.reason_codes) != ("OPEN_SYNC_ALERT",)
        or proposal.candidates
        or proposal.score_ppm
        or proposal.margin_ppm
    ):
        raise ContractError(f"{label} BLOCKED_ALERT evidence is inconsistent.")
    ai_reason_codes = {
        "AI_SUPPORTS_LOCAL",
        "AI_DISAGREES",
        "AI_LOW_CONFIDENCE",
        "AI_ABSTAINED",
        "AI_REVIEW_ERROR",
    }
    present_ai_codes = ai_reason_codes.intersection(proposal.reason_codes)
    if proposal.ai is None:
        if present_ai_codes:
            raise ContractError(f"{label} has an AI reason without AI evidence.")
        return
    compatible_keys = {candidate.candidate_key for candidate in compatible}
    if not 2 <= len(compatible_keys) <= 8:
        raise ContractError(f"{label} AI evidence lacks a valid compatible shortlist.")
    if proposal.ai.candidate_key and proposal.ai.candidate_key not in compatible_keys:
        raise ContractError(f"{label} AI selected a protected-conflict candidate.")
    if proposal.ai.decision == "ERROR":
        expected_ai_code = "AI_REVIEW_ERROR"
    elif proposal.ai.decision == "ABSTAIN":
        expected_ai_code = "AI_ABSTAINED"
    elif proposal.ai.confidence != "HIGH":
        expected_ai_code = "AI_LOW_CONFIDENCE"
    elif proposal.ai.candidate_key != proposal.selected_candidate_key:
        expected_ai_code = "AI_DISAGREES"
    else:
        expected_ai_code = "AI_SUPPORTS_LOCAL"
    if present_ai_codes != {expected_ai_code}:
        raise ContractError(f"{label} AI reason code is inconsistent with its evidence.")
    if expected_ai_code != "AI_SUPPORTS_LOCAL" and proposal.state is DecisionState.AUTO_ELIGIBLE:
        raise ContractError(f"{label} non-supporting AI evidence did not demote the row.")


def _read_proposals(path: Path) -> tuple[tuple[ProposalRecord, ...], bytes, str]:
    content, digest = read_stable_regular_file(path, maximum_bytes=MAX_PROPOSAL_BYTES)
    if not content:
        return (), content, digest
    if not content.endswith(b"\n"):
        raise ContractError("proposals.jsonl must end with one canonical newline.")
    proposals: list[ProposalRecord] = []
    for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
        if len(line) > MAX_PROPOSAL_LINE_BYTES:
            raise ContractError("One proposal record exceeds its line-size limit.")
        if line == b"\n" or not line.endswith(b"\n"):
            raise ContractError("proposals.jsonl contains a blank or unterminated line.")
        raw_content = line[:-1]
        raw = strict_json_loads(raw_content)
        if raw_content != canonical_json_bytes(raw):
            raise ContractError(
                f"Proposal line {line_number} is not canonical JSON."
            )
        proposals.append(_parse_proposal(raw, line_number=line_number))
        if len(proposals) > MAX_PROPOSALS:
            raise ContractError("The proposal artifact exceeds its record limit.")
    proposal_ids = [proposal.proposal_id for proposal in proposals]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ContractError("The proposal artifact contains a duplicate proposal_id.")
    identities = [(proposal.server_id, proposal.stream_id) for proposal in proposals]
    if len(identities) != len(set(identities)):
        raise ContractError("The proposal artifact contains a duplicate stream identity.")
    expected_order = sorted(
        identities, key=lambda key: (key[0], _stream_sort_key(key[1]))
    )
    if identities != expected_order:
        raise ContractError("Proposal records are not in deterministic stream order.")
    return tuple(proposals), content, digest


def _bound_server_scope(
    manifest: RunManifest, proposals: Sequence[ProposalRecord]
) -> tuple[str, ...]:
    observed_servers = frozenset(proposal.server_id for proposal in proposals)
    if not observed_servers.issubset(SUPPORTED_SERVERS):
        raise ContractError("A proposal contains an unsupported server ID.")
    matching_server_sets: list[tuple[str, ...]] = []
    for count in range(1, len(SUPPORTED_SERVERS) + 1):
        for candidate in combinations(SUPPORTED_SERVERS, count):
            if not observed_servers.issubset(candidate):
                continue
            computed = sha256_json(
                {
                    "schema": RUN_MANIFEST_SCHEMA,
                    "mode": "shadow",
                    "created_at": manifest.generated_at,
                    "servers": list(candidate),
                    "inputs": dict(manifest.input_sha256),
                    "policy_sha256": manifest.policy_sha256,
                    "lab_code_sha256": manifest.lab_code_sha256,
                }
            )
            if computed == manifest.run_id:
                matching_server_sets.append(candidate)
    if len(matching_server_sets) != 1:
        raise ContractError("The manifest run_id does not bind one valid server scope.")
    return matching_server_sets[0]


def _validate_cross_artifact_contract(
    manifest: RunManifest,
    summary: Mapping[str, Any],
    proposals: Sequence[ProposalRecord],
    *,
    as_of: datetime,
) -> None:
    generated = _parse_utc(manifest.generated_at, label="manifest generated_at")
    expires = _parse_utc(manifest.expires_at, label="manifest expires_at")
    lifetime = int((expires - generated).total_seconds())
    if not 300 <= lifetime <= 24 * 60 * 60:
        raise ContractError("The shadow bundle has an invalid expiry interval.")
    if as_of < generated:
        raise ContractError("The shadow bundle was generated after validation as-of.")
    if as_of >= expires:
        raise ContractError("The shadow bundle has expired.")
    if summary["run_id"] != manifest.run_id:
        raise ContractError("The summary and manifest run IDs differ.")
    if summary["generated_at"] != manifest.generated_at:
        raise ContractError("The summary and manifest generation times differ.")
    if dict(summary["counts"]) != dict(manifest.counts):
        raise ContractError("The summary and manifest counts differ.")
    if manifest.proposal_count != len(proposals):
        raise ContractError("The manifest proposal count is incorrect.")
    if manifest.counts.get("REVIEW_ROWS") != len(proposals):
        raise ContractError("The REVIEW_ROWS count does not match proposals.")
    actual_states = Counter(proposal.state.value for proposal in proposals)
    reported_states = {
        name: count
        for name, count in manifest.counts.items()
        if name in _STATE_COUNT_FIELDS
    }
    if reported_states != dict(sorted(actual_states.items())):
        raise ContractError("The reported proposal-state counts are incorrect.")
    ai_proposals = [proposal for proposal in proposals if proposal.ai is not None]
    ai_supported = sum(
        "AI_SUPPORTS_LOCAL" in proposal.reason_codes for proposal in ai_proposals
    )
    ai_expected = {
        "AI_REQUESTED": len(ai_proposals),
        "AI_SUPPORTED": ai_supported,
        "AI_REQUIRES_REVIEW": len(ai_proposals) - ai_supported,
    }
    for name, expected in ai_expected.items():
        if manifest.counts.get(name) != expected:
            raise ContractError(f"The reported {name} count is incorrect.")
    if manifest.counts.get("AI_ELIGIBLE") != (
        manifest.counts.get("AI_REQUESTED", 0)
        + manifest.counts.get("AI_DEFERRED", 0)
    ):
        raise ContractError("The reported AI eligibility counts do not reconcile.")
    programme_ids = {candidate.epg_id for proposal in proposals for candidate in proposal.candidates}
    if manifest.counts.get("PROGRAMME_IDS_CHECKED") != len(programme_ids):
        raise ContractError("PROGRAMME_IDS_CHECKED does not match proposal evidence.")
    if manifest.counts.get("CATALOG_CANDIDATES", 0) < len(programme_ids):
        raise ContractError("The catalog count is smaller than retained evidence.")
    generation_tokens: set[str] = set()
    for proposal in proposals:
        if proposal.run_id != manifest.run_id:
            raise ContractError("A proposal belongs to another run_id.")
        if proposal.created_at != manifest.generated_at:
            raise ContractError("A proposal has a different creation timestamp.")
        if proposal.expires_at != manifest.expires_at:
            raise ContractError("A proposal has a different expiry timestamp.")
        if proposal.policy_sha256 != manifest.policy_sha256:
            raise ContractError("A proposal has a different policy hash.")
        if proposal.lab_code_sha256 != manifest.lab_code_sha256:
            raise ContractError("A proposal has a different lab-code hash.")
        if proposal.source_sha256 != manifest.input_sha256["EPG_XML"]:
            raise ContractError("A proposal has a different EPG XML hash.")
        if proposal.text_catalog_sha256 != manifest.input_sha256["EPG_TEXT"]:
            raise ContractError("A proposal has a different EPG text hash.")
        if (
            proposal.catalog_fingerprint_sha256
            != manifest.input_sha256["EPG_TEXT_CATALOG"]
        ):
            raise ContractError("A proposal has a different text-catalog fingerprint.")
        if not proposal.catalog_generation_token.isdigit() or len(
            proposal.catalog_generation_token
        ) not in {12, 14}:
            raise ContractError("A proposal has an invalid catalog generation token.")
        generation_tokens.add(proposal.catalog_generation_token)
    if len(generation_tokens) > 1:
        raise ContractError("Proposals contain more than one catalog generation token.")
    _bound_server_scope(manifest, proposals)


def _read_mappings(path: Path) -> tuple[Any, str]:
    streaming, sync = _sheet_runtime()
    content, digest = read_stable_regular_file(
        path, maximum_bytes=streaming.MAX_MAPPING_BYTES
    )
    try:
        table = sync.parse_mapping_csv(content)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    return table, digest


def _validate_current_mappings(
    path: Path,
    manifest: RunManifest,
    proposals: Sequence[ProposalRecord],
) -> None:
    streaming, sync = _sheet_runtime()
    table, digest = _read_mappings(path)
    if digest != manifest.input_sha256["MAPPING_FILE"]:
        raise ContractError("The current Mappings file differs from the bundle input.")
    if sync.mapping_table_fingerprint(table) != manifest.input_sha256["MAPPING_TABLE"]:
        raise ContractError("The current Mappings table fingerprint differs from the bundle.")
    rows_by_key = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): row
        for row in table.rows
    }
    server_scope = set(_bound_server_scope(manifest, proposals))
    expected_keys = [
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        for row in table.rows
        if streaming.normalize_server_id(row.get("server_id", "")) in server_scope
        and streaming.clean_text(row.get("action", ""), 40).upper() == "REVIEW"
    ]
    proposal_keys = [(proposal.server_id, proposal.stream_id) for proposal in proposals]
    if len(expected_keys) != len(set(expected_keys)) or set(expected_keys) != set(
        proposal_keys
    ):
        raise ContractError(
            "The bundle does not contain exactly one proposal for every scoped REVIEW row."
        )
    for proposal in proposals:
        row = rows_by_key.get((proposal.server_id, proposal.stream_id))
        if row is None:
            raise ContractError("A proposal target is absent from current Mappings.")
        if streaming.clean_text(row.get("action", ""), 40).upper() != "REVIEW":
            raise ContractError("A proposal target is no longer a REVIEW row.")
        if proposal.channel_name != safe_display_text(
            streaming.clean_identifier(row.get("channel_name", ""), 300),
            maximum=300,
        ) or proposal.category_name != safe_display_text(
            streaming.clean_text(row.get("category_name", ""), 200),
            maximum=200,
        ):
            raise ContractError("A proposal target identity differs from current Mappings.")
        if _mapping_row_guard(row) != proposal.row_guard_sha256:
            raise ContractError("A proposal row guard differs from current Mappings.")
        if _provider_identity_guard(row) != proposal.provider_identity_sha256:
            raise ContractError("A proposal provider-identity guard is stale.")
        try:
            enabled = streaming.parse_bool(
                row.get("enabled", ""), default=False, field_name="enabled"
            )
            state_reason = "ROW_NOT_DISABLED_REVIEW" if enabled else ""
        except streaming.BuildError:
            state_reason = "INVALID_REVIEW_STATE"
        state_codes = {
            "ROW_NOT_DISABLED_REVIEW",
            "INVALID_REVIEW_STATE",
        }
        present_state_codes = state_codes.intersection(proposal.reason_codes)
        # OPEN-alert quarantine has intentional precedence over ordinary row
        # eligibility.  The exact alert snapshot is verified separately below,
        # so a blocked row does not also claim a conflicting review-state code.
        expected_state_codes = (
            set()
            if proposal.state is DecisionState.BLOCKED_ALERT
            else ({state_reason} if state_reason else set())
        )
        if present_state_codes != expected_state_codes:
            raise ContractError("A proposal's REVIEW-state reason is inconsistent.")
        if state_reason and proposal.state not in {
            DecisionState.BLOCKED_ALERT,
            DecisionState.CONFLICT,
        }:
            raise ContractError("An ineligible REVIEW row is not marked CONFLICT.")
        prefilled = streaming.clean_identifier(row.get("epg_id", ""), 300)
        prefilled_status_codes = {
            "PREFILLED_ID_CATALOG_REVALIDATED",
            "PREFILLED_ID_NOT_CORROBORATED",
        }
        present_prefilled_status = prefilled_status_codes.intersection(
            proposal.reason_codes
        )
        prefilled_evidence_present = (
            "PREFILLED_ID_UNTRUSTED_EVIDENCE" in proposal.reason_codes
        )
        should_describe_prefill = bool(prefilled) and not state_reason and (
            proposal.state is not DecisionState.BLOCKED_ALERT
        )
        if should_describe_prefill != prefilled_evidence_present or (
            should_describe_prefill and len(present_prefilled_status) != 1
        ) or (not should_describe_prefill and present_prefilled_status):
            raise ContractError("A proposal's prefilled-ID evidence is inconsistent.")


def _read_alerts(path: Path) -> tuple[list[dict[str, str]], str]:
    _streaming, sync = _sheet_runtime()
    content, digest = read_stable_regular_file(
        path, maximum_bytes=sync.MAX_SYNC_ALERT_BYTES
    )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("The Sync Alerts CSV is not UTF-8.") from exc
    try:
        alerts = sync.parse_sync_alert_values(
            list(csv.reader(io.StringIO(text, newline="")))
        )
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    return alerts, digest


def _validate_current_alerts(
    path: Path,
    manifest: RunManifest,
    proposals: Sequence[ProposalRecord],
) -> None:
    _streaming, sync = _sheet_runtime()
    alerts, digest = _read_alerts(path)
    if digest != manifest.input_sha256["ALERTS_FILE"]:
        raise ContractError("The current Sync Alerts file differs from the bundle input.")
    try:
        open_keys = sync.open_alert_quarantine_keys(alerts)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    open_digest = sha256_json(
        [[server_id, stream_id] for server_id, stream_id in sorted(open_keys)]
    )
    if open_digest != manifest.input_sha256["OPEN_ALERT_KEYS"]:
        raise ContractError("The current OPEN-alert set differs from the bundle input.")
    for proposal in proposals:
        is_open = (proposal.server_id, proposal.stream_id) in open_keys
        is_blocked = (
            proposal.state is DecisionState.BLOCKED_ALERT
            and "OPEN_SYNC_ALERT" in proposal.reason_codes
        )
        if is_open != is_blocked:
            raise ContractError("A proposal's OPEN-alert decision is inconsistent.")
        if "ALERT_SNAPSHOT_MISSING" in proposal.reason_codes:
            raise ContractError("A proposal contradicts the supplied alert snapshot.")


def validate_bundle(
    bundle_dir: Path,
    *,
    mappings_csv: Path | None = None,
    alerts_csv: Path | None = None,
    as_of: str | datetime | None = None,
) -> ValidationResult:
    """Validate one private shadow bundle without performing any write.

    ``mappings_csv`` and ``alerts_csv`` are optional authoritative snapshots.
    When supplied, their complete hashes and all proposal row/alert guards must
    still match.  ``as_of`` defaults to current UTC but may be pinned for an
    exact historical replay.
    """

    target = Path(bundle_dir)
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ContractError("The Matching Lab bundle directory is unavailable.") from exc
    if target.is_symlink() or not target.is_dir():
        raise ContractError("The Matching Lab bundle path must be a real directory.")
    del metadata
    expected_names = {"manifest.json", "proposals.jsonl", "summary.json"}
    try:
        initial_names = {entry.name for entry in target.iterdir()}
    except OSError as exc:
        raise ContractError("The Matching Lab bundle directory cannot be listed.") from exc
    if initial_names != expected_names:
        raise ContractError("The Matching Lab bundle must contain exactly three artifacts.")

    manifest_raw, manifest_content, _manifest_digest = _canonical_document(
        target / "manifest.json",
        maximum_bytes=MAX_MANIFEST_BYTES,
        label="manifest.json",
    )
    summary_raw, summary_content, summary_digest = _canonical_document(
        target / "summary.json",
        maximum_bytes=MAX_SUMMARY_BYTES,
        label="summary.json",
    )
    proposals, proposal_content, proposal_digest = _read_proposals(
        target / "proposals.jsonl"
    )
    manifest = _parse_manifest(manifest_raw)

    if sha256_bytes(proposal_content) != proposal_digest:
        raise ContractError("The proposals file changed during validation.")
    if proposal_digest != manifest.proposals_sha256:
        raise ContractError("The proposals file hash differs from the manifest.")
    if sha256_bytes(summary_content) != summary_digest:
        raise ContractError("The summary file changed during validation.")
    if summary_digest != manifest.summary_sha256:
        raise ContractError("The summary file hash differs from the manifest.")
    summary = _parse_summary(summary_raw)

    validated_at = _parse_as_of(as_of)
    _validate_cross_artifact_contract(
        manifest, summary, proposals, as_of=validated_at
    )

    alert_snapshot_present = (
        manifest.input_sha256["ALERTS_FILE"] != sha256_bytes(b"")
    )
    selected_states = {
        DecisionState.AUTO_ELIGIBLE,
        DecisionState.NEEDS_REVIEW,
        DecisionState.PENDING_PROGRAMME,
    }
    for proposal in proposals:
        missing_reason = "ALERT_SNAPSHOT_MISSING" in proposal.reason_codes
        if alert_snapshot_present and missing_reason:
            raise ContractError(
                "A proposal contradicts the manifest's supplied alert snapshot."
            )
        if (
            not alert_snapshot_present
            and proposal.state in selected_states
            and not missing_reason
        ):
            raise ContractError(
                "A selected proposal does not record the missing alert snapshot."
            )
        if not alert_snapshot_present and proposal.state is DecisionState.AUTO_ELIGIBLE:
            raise ContractError(
                "AUTO_ELIGIBLE requires a supplied alert snapshot."
            )

    if mappings_csv is not None:
        _validate_current_mappings(Path(mappings_csv), manifest, proposals)
    if alerts_csv is not None:
        _validate_current_alerts(Path(alerts_csv), manifest, proposals)
    elif manifest.input_sha256["ALERTS_FILE"] == sha256_bytes(b""):
        if manifest.input_sha256["OPEN_ALERT_KEYS"] != sha256_json([]):
            raise ContractError("A missing alert snapshot has a non-empty OPEN set.")
        if any(proposal.state is DecisionState.BLOCKED_ALERT for proposal in proposals):
            raise ContractError("A proposal is alert-blocked without an alert snapshot.")

    terminal_manifest, _ = read_stable_regular_file(
        target / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES
    )
    terminal_summary, _ = read_stable_regular_file(
        target / "summary.json", maximum_bytes=MAX_SUMMARY_BYTES
    )
    terminal_proposals, _ = read_stable_regular_file(
        target / "proposals.jsonl", maximum_bytes=MAX_PROPOSAL_BYTES
    )
    if (
        terminal_manifest != manifest_content
        or terminal_summary != summary_content
        or terminal_proposals != proposal_content
    ):
        raise ContractError("The Matching Lab bundle changed during validation.")

    try:
        terminal_names = {entry.name for entry in target.iterdir()}
    except OSError as exc:
        raise ContractError("The Matching Lab bundle directory cannot be relisted.") from exc
    if terminal_names != expected_names:
        raise ContractError("The Matching Lab bundle changed during validation.")

    return ValidationResult(
        bundle_dir=target.resolve(),
        run_id=manifest.run_id,
        generated_at=manifest.generated_at,
        expires_at=manifest.expires_at,
        validated_as_of=_utc_text(validated_at),
        proposal_count=len(proposals),
        counts=manifest.counts,
        proposals=proposals,
        mappings_checked=mappings_csv is not None,
        alerts_checked=alerts_csv is not None,
    )


__all__ = ("ValidationResult", "validate_bundle")
