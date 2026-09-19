"""Read-only mining of conservative rule candidates from a validated bundle.

The analyzer is intentionally downstream of :func:`validate_bundle`.  It has
no provider, Google, or other network integration and emits evidence only.  A
candidate produced here never grants write authority and is never added to the
approved knowledge base automatically.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import (
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    canonical_json_bytes,
    sha256_json,
)
from .validation import ValidationResult, validate_bundle


RULE_CANDIDATE_SCHEMA = "skytv.matching-lab-rule-candidate.v1"
RULE_SUMMARY_SCHEMA = "skytv.matching-lab-rule-analysis-summary.v1"
MAX_RULE_CANDIDATES = 20_000
MAX_EVIDENCE_ROWS = 30_000
MAX_RULE_LINE_BYTES = 2 * 1024 * 1024
MAX_RULE_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
_NON_SUPPORTING_AI_REASON_CODES = frozenset(
    {
        "AI_DISAGREES",
        "AI_LOW_CONFIDENCE",
        "AI_ABSTAINED",
        "AI_REVIEW_ERROR",
    }
)
_SUMMARY_COUNT_NAMES = (
    "PROPOSALS_VALIDATED",
    "PROPOSALS_SELECTED",
    "ELIGIBLE_EVIDENCE_ROWS",
    "EXCLUDED_ALERT_OR_UNKNOWN",
    "EXCLUDED_CONFLICT",
    "EXCLUDED_AI_NON_SUPPORT",
    "EXCLUDED_NO_SELECTION",
    "EXCLUDED_PROGRAMME_NOT_PASS",
    "EXCLUDED_BLANK_IDENTITY",
    "IDENTITY_GROUPS",
    "SINGLETON_GROUPS",
    "RECURRING_GROUPS",
    "EXCLUDED_SINGLE_SERVER_CONSISTENT_GROUPS",
    "CONSISTENT_TARGET_GROUPS",
    "CONTRADICTORY_TARGET_GROUPS",
    "RULE_CANDIDATES_EMITTED",
    "EVIDENCE_ROWS_EMITTED",
    "CURRENT_MAPPINGS_CHECKED",
    "CURRENT_ALERTS_CHECKED",
)


@dataclass(frozen=True, slots=True)
class RuleAnalysisResult:
    """Small public result for one completed private rule analysis."""

    output_dir: Path
    candidate_count: int
    consistent_count: int
    contradictory_count: int
    evidence_row_count: int


@dataclass(frozen=True, slots=True)
class _EligibleEvidence:
    proposal: ProposalRecord
    candidate: CandidateEvidence
    normalized_identity: str


def _normalized_identity(value: object) -> str:
    """Normalize separators and case while preserving Unicode distinctions."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    pieces: list[str] = []
    separated = True
    for character in text:
        if unicodedata.category(character)[0] in {"L", "N"}:
            pieces.append(character)
            separated = False
        elif not separated:
            pieces.append(" ")
            separated = True
    return "".join(pieces).strip()


def _semantics_key(semantics: ProtectedSemantics) -> tuple[object, ...]:
    return (
        semantics.market,
        semantics.direction,
        semantics.timeshift,
        semantics.has_plus,
        semantics.has_extra,
        semantics.has_alternate,
        semantics.numbers,
        semantics.languages,
        semantics.content,
    )


def _group_key(evidence: _EligibleEvidence) -> tuple[object, ...]:
    return (
        evidence.normalized_identity,
        evidence.proposal.market,
        *_semantics_key(evidence.candidate.semantics),
    )


def _selected_candidate(proposal: ProposalRecord) -> CandidateEvidence | None:
    if not proposal.selected_candidate_key:
        return None
    return next(
        (
            candidate
            for candidate in proposal.candidates
            if candidate.candidate_key == proposal.selected_candidate_key
        ),
        None,
    )


def _eligible_evidence(
    proposals: Sequence[ProposalRecord],
) -> tuple[list[_EligibleEvidence], Counter[str]]:
    evidence: list[_EligibleEvidence] = []
    counts: Counter[str] = Counter()
    selected_states = {
        DecisionState.AUTO_ELIGIBLE,
        DecisionState.NEEDS_REVIEW,
        DecisionState.PENDING_PROGRAMME,
    }
    for proposal in proposals:
        counts["PROPOSALS_VALIDATED"] += 1
        reasons = set(proposal.reason_codes)
        if (
            proposal.state is DecisionState.BLOCKED_ALERT
            or "OPEN_SYNC_ALERT" in reasons
            or "ALERT_SNAPSHOT_MISSING" in reasons
        ):
            counts["EXCLUDED_ALERT_OR_UNKNOWN"] += 1
            continue
        if (
            proposal.state is DecisionState.CONFLICT
            or "PROTECTED_SEMANTICS_CONFLICT" in reasons
        ):
            counts["EXCLUDED_CONFLICT"] += 1
            continue
        if reasons.intersection(_NON_SUPPORTING_AI_REASON_CODES) or (
            proposal.ai is not None and "AI_SUPPORTS_LOCAL" not in reasons
        ):
            # A validated advisory can only demote a local result.  Do not let
            # a disagreement, abstention, low-confidence answer, or model
            # error become positive evidence for a reusable rule.
            counts["EXCLUDED_AI_NON_SUPPORT"] += 1
            continue
        candidate = _selected_candidate(proposal)
        if proposal.state not in selected_states or candidate is None:
            counts["EXCLUDED_NO_SELECTION"] += 1
            continue
        counts["PROPOSALS_SELECTED"] += 1
        if candidate.conflicts:
            counts["EXCLUDED_CONFLICT"] += 1
            continue
        if candidate.programme_state is not ProgrammeState.PASS:
            counts["EXCLUDED_PROGRAMME_NOT_PASS"] += 1
            continue
        identity = _normalized_identity(proposal.channel_name)
        if not identity:
            counts["EXCLUDED_BLANK_IDENTITY"] += 1
            continue
        evidence.append(
            _EligibleEvidence(
                proposal=proposal,
                candidate=candidate,
                normalized_identity=identity,
            )
        )
        counts["ELIGIBLE_EVIDENCE_ROWS"] += 1
    return evidence, counts


def _evidence_dict(item: _EligibleEvidence) -> dict[str, object]:
    proposal = item.proposal
    candidate = item.candidate
    return {
        "proposal_id": proposal.proposal_id,
        "server_id": proposal.server_id,
        "row_guard_sha256": proposal.row_guard_sha256,
        "provider_identity_sha256": proposal.provider_identity_sha256,
        "selected_candidate_key": candidate.candidate_key,
        "target": {
            "epg_id": candidate.epg_id,
            "feed": candidate.feed,
            "region": candidate.region,
        },
        "decision": {
            "state": proposal.state.value,
            "route_explicit": proposal.route_explicit,
            "score_ppm": proposal.score_ppm,
            "margin_ppm": proposal.margin_ppm,
            "reason_codes": list(proposal.reason_codes),
        },
        "retrieval_methods": list(candidate.methods),
        "programme": {
            "state": candidate.programme_state.value,
            "count": candidate.programme_count,
            "first_start_epoch": candidate.programme_first_start_epoch,
            "latest_stop_epoch": candidate.programme_latest_stop_epoch,
        },
    }


def _target_dict(
    target: tuple[str, str], items: Sequence[_EligibleEvidence]
) -> dict[str, object]:
    feed, epg_id = target
    display_names = sorted({item.candidate.display_name for item in items})
    regions = sorted({item.candidate.region for item in items})
    methods = sorted(
        {
            method
            for item in items
            for method in item.candidate.methods
        }
    )
    return {
        "epg_id": epg_id,
        "feed": feed,
        "regions": regions,
        "display_names": display_names,
        "evidence_count": len(items),
        "distinct_server_count": len(
            {item.proposal.server_id for item in items}
        ),
        "retrieval_methods": methods,
    }


def _rule_candidate(
    items: Sequence[_EligibleEvidence], *, source_run_id: str
) -> dict[str, object]:
    first = items[0]
    group = {
        "normalized_identity": first.normalized_identity,
        "market": first.proposal.market,
        "protected_semantics": first.candidate.semantics.public_dict(),
    }
    by_target: dict[tuple[str, str], list[_EligibleEvidence]] = defaultdict(list)
    for item in items:
        by_target[(item.candidate.feed, item.candidate.epg_id)].append(item)
    targets = [
        _target_dict(target, by_target[target])
        for target in sorted(by_target, key=lambda value: (value[0], value[1]))
    ]
    status = (
        "CONSISTENT_TARGET" if len(targets) == 1 else "CONTRADICTORY_TARGETS"
    )
    evidence = sorted(
        (_evidence_dict(item) for item in items),
        key=lambda value: (
            str(value["server_id"]),
            str(value["proposal_id"]),
        ),
    )
    identity = {
        "schema": RULE_CANDIDATE_SCHEMA,
        "source_run_id": source_run_id,
        "group": group,
        "targets": [
            [target["feed"], target["epg_id"]]
            for target in targets
        ],
    }
    return {
        "schema": RULE_CANDIDATE_SCHEMA,
        "rule_candidate_id": sha256_json(identity),
        "source_run_id": source_run_id,
        "private_artifact": True,
        "write_authority": False,
        "requires_human_review": True,
        "status": status,
        "group": group,
        "evidence_count": len(evidence),
        "distinct_server_count": len(
            {item.proposal.server_id for item in items}
        ),
        "targets": targets,
        "evidence": evidence,
    }


def _build_analysis(
    validation: ValidationResult,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    eligible, counts = _eligible_evidence(validation.proposals)
    if len(eligible) > MAX_EVIDENCE_ROWS:
        raise ContractError("Rule analysis exceeds its evidence-row safety limit.")
    grouped: dict[tuple[object, ...], list[_EligibleEvidence]] = defaultdict(list)
    for item in eligible:
        grouped[_group_key(item)].append(item)
    counts["IDENTITY_GROUPS"] = len(grouped)
    records: list[dict[str, object]] = []
    for key in sorted(grouped, key=lambda value: tuple(str(part) for part in value)):
        items = grouped[key]
        if len(items) < 2:
            counts["SINGLETON_GROUPS"] += 1
            continue
        counts["RECURRING_GROUPS"] += 1
        record = _rule_candidate(items, source_run_id=validation.run_id)
        if (
            record["status"] == "CONSISTENT_TARGET"
            and int(record["distinct_server_count"]) < 2
        ):
            # Repeated rows from one provider are correlated evidence, not
            # independent corroboration.  Keep the aggregate count visible,
            # but do not emit an alias candidate from one server alone.
            counts["EXCLUDED_SINGLE_SERVER_CONSISTENT_GROUPS"] += 1
            continue
        records.append(record)
        counts["EVIDENCE_ROWS_EMITTED"] += len(items)
        if record["status"] == "CONSISTENT_TARGET":
            counts["CONSISTENT_TARGET_GROUPS"] += 1
        else:
            counts["CONTRADICTORY_TARGET_GROUPS"] += 1
    records.sort(key=lambda value: str(value["rule_candidate_id"]))
    if len(records) > MAX_RULE_CANDIDATES:
        raise ContractError("Rule analysis exceeds its candidate safety limit.")
    counts["RULE_CANDIDATES_EMITTED"] = len(records)
    counts["CURRENT_MAPPINGS_CHECKED"] = int(validation.mappings_checked)
    counts["CURRENT_ALERTS_CHECKED"] = int(validation.alerts_checked)
    normalized_counts = {
        name: int(counts.get(name, 0)) for name in _SUMMARY_COUNT_NAMES
    }
    summary = {
        "schema": RULE_SUMMARY_SCHEMA,
        "mode": "read_only_rule_analysis",
        "write_authority": False,
        "private_details_emitted": False,
        "validated_as_of": validation.validated_as_of,
        "counts": dict(sorted(normalized_counts.items())),
    }
    return records, summary


def _jsonl_content(records: Iterable[Mapping[str, object]]) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for record in records:
        line = canonical_json_bytes(record) + b"\n"
        if len(line) > MAX_RULE_LINE_BYTES:
            raise ContractError("One rule candidate exceeds its line-size limit.")
        size += len(line)
        if size > MAX_RULE_OUTPUT_BYTES:
            raise ContractError("Rule candidate output exceeds its size limit.")
        chunks.append(line)
    return b"".join(chunks)


def _require_separate_output(bundle_dir: Path, output_dir: Path) -> Path:
    bundle = bundle_dir.resolve(strict=True)
    output = output_dir.resolve(strict=False)
    if output == bundle or bundle in output.parents:
        raise ContractError(
            "Rule-analysis output must be separate from the canonical bundle."
        )
    if output.exists():
        raise ContractError("Rule-analysis output directory must be new.")
    return output


def _write_new_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ContractError("A rule-analysis output artifact already exists.") from exc
    except OSError as exc:
        raise ContractError("A rule-analysis artifact could not be written safely.") from exc


def _write_outputs(
    output_dir: Path,
    *,
    records: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
) -> None:
    candidate_content = _jsonl_content(records)
    summary_content = canonical_json_bytes(summary) + b"\n"
    if len(summary_content) > MAX_SUMMARY_BYTES:
        raise ContractError("Rule-analysis summary exceeds its size limit.")
    try:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(output_dir, 0o700)
    except FileExistsError as exc:
        raise ContractError("Rule-analysis output directory must be new.") from exc
    except OSError as exc:
        raise ContractError(
            "Rule-analysis output directory could not be prepared safely."
        ) from exc
    metadata = output_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or output_dir.is_symlink():
        raise ContractError("Rule-analysis output must be a real directory.")
    _write_new_file(output_dir / "rule_candidates.jsonl", candidate_content)
    _write_new_file(output_dir / "summary.json", summary_content)


def analyze_rule_candidates(
    *,
    bundle_dir: Path,
    output_dir: Path,
    as_of: str,
    mappings_csv: Path | None = None,
    alerts_csv: Path | None = None,
) -> RuleAnalysisResult:
    """Validate, mine, and write a deterministic private evidence report."""

    # Full bundle validation intentionally happens before any output operation.
    validation = validate_bundle(
        Path(bundle_dir),
        mappings_csv=Path(mappings_csv) if mappings_csv is not None else None,
        alerts_csv=Path(alerts_csv) if alerts_csv is not None else None,
        as_of=as_of,
    )
    target = _require_separate_output(validation.bundle_dir, Path(output_dir))
    records, summary = _build_analysis(validation)
    _write_outputs(target, records=records, summary=summary)
    counts = summary["counts"]
    assert isinstance(counts, dict)
    return RuleAnalysisResult(
        output_dir=target,
        candidate_count=len(records),
        consistent_count=int(counts.get("CONSISTENT_TARGET_GROUPS", 0)),
        contradictory_count=int(counts.get("CONTRADICTORY_TARGET_GROUPS", 0)),
        evidence_row_count=int(counts.get("EVIDENCE_ROWS_EMITTED", 0)),
    )


__all__ = (
    "MAX_EVIDENCE_ROWS",
    "MAX_RULE_CANDIDATES",
    "RULE_CANDIDATE_SCHEMA",
    "RULE_SUMMARY_SCHEMA",
    "RuleAnalysisResult",
    "analyze_rule_candidates",
)
