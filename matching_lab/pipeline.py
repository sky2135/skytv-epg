"""One-pass shadow/proposal pipeline for unresolved EPGShare mappings."""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import LAB_VERSION, PROPOSAL_SCHEMA, RUN_MANIFEST_SCHEMA
from .ai import (
    DEFAULT_MODEL as DEFAULT_AI_MODEL,
    MAX_ADVISORY_REQUESTS,
    MAX_ADVISORY_SHARDS,
    PROMPT_VERSION as AI_PROMPT_VERSION,
    AdvisoryCacheError,
    AdvisoryDisposition,
    attach_advisory,
    proposals_for_advisory_shard,
    replay_cache_namespace_sha256,
    requests_from_proposals,
    review_advisories,
    validate_advisory_shard,
    validate_model_name,
)
from .artifacts import (
    package_code_sha256,
    read_stable_regular_file,
    write_private_bundle,
)
from .compat import automatch, catalog_stream, streaming, sync
from .models import (
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    RunManifest,
    retained_exact_tier_families,
    sha256_bytes,
    sha256_json,
)
from .policy import DEFAULT_POLICY, MatchingPolicy
from .normalization import lexical_cohort_risk_codes
from .retrieval import (
    CandidateIndex,
    LabSubject,
    RankedCandidates,
    human_alias_edges,
    subject_from_mapping_row,
)
from .state import LedgerError, ObservationLedger


MAX_PROPOSALS = 30_000
SUPPORTED_SERVERS = ("server_1", "server_2", "server_3")


@dataclass(frozen=True, slots=True)
class ShadowRunResult:
    output_dir: Path
    run_id: str
    proposal_count: int
    counts: Mapping[str, int]


def _ai_config_sha256(
    *,
    enabled: bool,
    model: str,
    maximum_requests: int,
    shard_count: int,
    shard_index: int,
    cache_namespace_sha256: str,
) -> str:
    """Bind the complete local advisory selection configuration to a run."""

    return sha256_json(
        {
            "enabled": enabled,
            "model": str(model or "") if enabled else "",
            "prompt_version": AI_PROMPT_VERSION if enabled else "",
            "maximum_requests": int(maximum_requests) if enabled else 0,
            "shard_count": int(shard_count) if enabled else 0,
            "shard_index": int(shard_index) if enabled else 0,
            "cache_namespace_sha256": cache_namespace_sha256,
        }
    )


def _parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("--as-of must be one ISO-8601 UTC timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError("--as-of must include the UTC timezone.")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_alerts(path: Path | None) -> tuple[list[dict[str, str]], str]:
    if path is None:
        return [], sha256_bytes(b"")
    content, digest = read_stable_regular_file(path, maximum_bytes=sync.MAX_SYNC_ALERT_BYTES)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("The Sync Alerts CSV is not UTF-8.") from exc
    values = list(csv.reader(io.StringIO(text, newline="")))
    try:
        return sync.parse_sync_alert_values(values), digest
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc


def _eligible_review_row(row: Mapping[str, Any]) -> tuple[bool, str]:
    action = streaming.clean_text(row.get("action", ""), 40).upper()
    try:
        enabled = streaming.parse_bool(
            row.get("enabled", ""), default=False, field_name="enabled"
        )
    except streaming.BuildError:
        return False, "INVALID_REVIEW_STATE"
    if enabled or action != "REVIEW":
        return False, "ROW_NOT_DISABLED_REVIEW"
    return True, ""


def _cohort_risk_codes(subject: LabSubject) -> tuple[str, ...]:
    """Return explicit reasons which keep a name-only match conservative.

    These are identity dimensions, not scoring penalties.  A candidate must
    still pass the symmetric protected-semantics gate; the codes additionally
    prevent a high fuzzy score from silently promoting a sensitive cohort.
    """

    semantics = subject.semantics
    risks = set(
        lexical_cohort_risk_codes(
            subject.channel_name,
            subject.category_name,
            subject.market,
        )
    )
    if semantics.numbers:
        risks.add("RISK_EXPLICIT_NUMBER")
    if semantics.has_plus:
        risks.add("RISK_PLUS_VARIANT")
    if semantics.direction:
        risks.add("RISK_DIRECTION_VARIANT")
    if semantics.timeshift:
        risks.add("RISK_TIMESHIFT_VARIANT")
    if semantics.has_extra or semantics.has_alternate:
        risks.add("RISK_EDITION_VARIANT")
    return tuple(sorted(risks))


def _prefilled_id_reason_codes(
    row: Mapping[str, Any],
    *,
    corroborated_ids: frozenset[str],
    collision_keys: frozenset[str],
) -> tuple[str, ...]:
    """Describe a REVIEW-row EPG ID without using it as ranking bias."""

    epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
    if not epg_id:
        return ()
    status = (
        "PREFILLED_ID_CATALOG_REVALIDATED"
        if epg_id in corroborated_ids and epg_id.casefold() not in collision_keys
        else "PREFILLED_ID_NOT_CORROBORATED"
    )
    return ("PREFILLED_ID_UNTRUSTED_EVIDENCE", status)


def _unsupported_channel(row: Mapping[str, Any]) -> bool:
    name = streaming.clean_identifier(row.get("channel_name", ""), 300)
    category = streaming.clean_text(row.get("category_name", ""), 200)
    return bool(
        automatch._matcher_has_adult_evidence(name, category)
        or automatch._matcher_is_generic_numbered_or_blank(name)
    )


def _programme_candidate(
    candidate: CandidateEvidence,
    gate: Any | None,
) -> CandidateEvidence:
    if gate is None:
        return replace(
            candidate,
            programme_state=ProgrammeState.NOT_CHECKED,
            programme_reason="Candidate was not selected for programme verification.",
        )
    return replace(
        candidate,
        programme_state=(ProgrammeState.PASS if bool(gate.passed) else ProgrammeState.FAIL),
        programme_count=int(gate.distinct_informative_programmes),
        programme_first_start_epoch=gate.first_start_epoch,
        programme_latest_stop_epoch=gate.latest_stop_epoch,
        programme_reason=str(gate.reason or ""),
    )


def _decision(
    *,
    subject: LabSubject,
    ranking: RankedCandidates,
    candidates: tuple[CandidateEvidence, ...],
    blocked_alert: bool,
    eligibility_reason: str,
    unsupported: bool,
    policy: MatchingPolicy,
    alert_snapshot_present: bool,
    cohort_risk_codes: tuple[str, ...] = (),
    evidence_reason_codes: tuple[str, ...] = (),
) -> tuple[DecisionState, tuple[str, ...], str, bool]:
    reasons: set[str] = set()
    if blocked_alert:
        return DecisionState.BLOCKED_ALERT, ("OPEN_SYNC_ALERT",), "", False
    if eligibility_reason:
        return DecisionState.CONFLICT, (eligibility_reason,), "", False
    reasons.update(evidence_reason_codes)
    reasons.update(cohort_risk_codes)
    reasons.add("COHORT_CONSERVATIVE" if cohort_risk_codes else "COHORT_STANDARD")
    if unsupported:
        reasons.update(("LANE_ABSTAIN", "UNSUPPORTED_CHANNEL_CLASS"))
        return DecisionState.ABSTAIN, tuple(sorted(reasons)), "", False
    frozen_exact_conflicts = tuple(
        candidate
        for candidate in candidates
        if "FROZEN_RESOLVER_EXACT" in candidate.methods and candidate.conflicts
    )
    if frozen_exact_conflicts:
        reasons.update(
            conflict
            for candidate in frozen_exact_conflicts
            for conflict in candidate.conflicts
        )
        reasons.update(
            (
                "FROZEN_RESOLVER_EXACT_SEMANTIC_CONFLICT",
                "LANE_CONFLICT",
                "PROTECTED_SEMANTICS_CONFLICT",
            )
        )
        return DecisionState.CONFLICT, tuple(sorted(reasons)), "", False
    storage_alias_conflicts = tuple(
        candidate
        for candidate in candidates
        if "CURATED_STORAGE_ALIAS_EXACT" in candidate.methods and candidate.conflicts
    )
    if storage_alias_conflicts:
        reasons.update(
            conflict
            for candidate in storage_alias_conflicts
            for conflict in candidate.conflicts
        )
        reasons.update(
            (
                "CURATED_STORAGE_ALIAS_SEMANTIC_CONFLICT",
                "LANE_CONFLICT",
                "PROTECTED_SEMANTICS_CONFLICT",
            )
        )
        return DecisionState.CONFLICT, tuple(sorted(reasons)), "", False
    compatible = tuple(candidate for candidate in candidates if not candidate.conflicts)
    if not compatible:
        if candidates:
            reasons.update(conflict for candidate in candidates for conflict in candidate.conflicts)
            reasons.update(("LANE_CONFLICT", "PROTECTED_SEMANTICS_CONFLICT"))
            return DecisionState.CONFLICT, tuple(sorted(reasons)), "", False
        reasons.update(("LANE_ABSTAIN", "NO_CANDIDATE"))
        return DecisionState.ABSTAIN, tuple(sorted(reasons)), "", False
    top = compatible[0]
    frozen_resolver_exact_top = "FROZEN_RESOLVER_EXACT" in top.methods
    curated_storage_alias_top = "CURATED_STORAGE_ALIAS_EXACT" in top.methods
    repository_curated_alias_top = (
        "REPOSITORY_CURATED_ALIAS_EXACT" in top.methods
    )
    one_explicit_route = bool(
        subject.route_explicit and subject.market and len(subject.route_plan) == 1
    )
    target_route_agrees = bool(
        one_explicit_route
        and str(top.region or "").upper() == str(subject.route_plan[0]).upper()
        and str(subject.market or "").upper() == str(subject.route_plan[0]).upper()
    )
    curated_alias_candidates = tuple(
        candidate
        for candidate in candidates
        if "CURATED_ALIAS_EXACT" in candidate.methods
    )
    competing_frozen_exact = any(
        candidate.candidate_key != top.candidate_key
        and "FROZEN_RESOLVER_EXACT" in candidate.methods
        and candidate.programme_state is not ProgrammeState.FAIL
        for candidate in compatible
    )
    repository_curated_alias_gates_passed = bool(
        repository_curated_alias_top
        and "CURATED_ALIAS_EXACT" in top.methods
        and len(curated_alias_candidates) == 1
        and curated_alias_candidates[0].candidate_key == top.candidate_key
        and not competing_frozen_exact
        and one_explicit_route
        and target_route_agrees
        and alert_snapshot_present
        and top.programme_state is ProgrammeState.PASS
    )
    repository_curated_alias_override_ready = bool(
        repository_curated_alias_gates_passed
        and (
            top.score_ppm < policy.minimum_candidate_score_ppm
            or ranking.margin_ppm < policy.strong_margin_ppm
        )
    )
    if (
        top.score_ppm < policy.minimum_candidate_score_ppm
        and not frozen_resolver_exact_top
        and not curated_storage_alias_top
        and not repository_curated_alias_override_ready
    ):
        reasons.update(("LANE_ABSTAIN", "LOW_CALIBRATED_CONFIDENCE"))
        return DecisionState.ABSTAIN, tuple(sorted(reasons)), "", False
    selected = top.candidate_key
    reasons.add("PROVIDER_REVALIDATION_REQUIRED")
    if not alert_snapshot_present:
        reasons.add("ALERT_SNAPSHOT_MISSING")
    if not one_explicit_route:
        reasons.add("MARKET_UNKNOWN")
    else:
        reasons.add("MARKET_ROUTE_EXPLICIT")
    strong = (
        top.score_ppm >= policy.strong_proposal_score_ppm
        and ranking.margin_ppm >= policy.strong_margin_ppm
        and one_explicit_route
        and alert_snapshot_present
    )
    alias_exact = bool(
        set(top.methods).intersection(
            {
                "HUMAN_ALIAS_EXACT",
                "CURATED_ALIAS_EXACT",
                "CURATED_STORAGE_ALIAS_EXACT",
            }
        )
    )
    trusted_alias_ready = (
        alias_exact
        and ranking.margin_ppm >= policy.strong_margin_ppm
        and one_explicit_route
        and (
            not curated_storage_alias_top
            or bool(
                subject.market
                and str(subject.market).upper()
                == str(subject.route_plan[0]).upper()
            )
        )
        and alert_snapshot_present
    )
    unique_strict_exact = bool(
        "STRICT_EXACT" in top.methods and ranking.compatible_count == 1
    )
    exact_tier_families = retained_exact_tier_families(compatible)
    unique_retained_exact_family = bool(
        len(exact_tier_families) == 1
        and exact_tier_families[0].candidate_key == top.candidate_key
    )
    untrusted_prefilled_id = bool(
        {
            "PREFILLED_ID_NOT_CORROBORATED",
            "PREFILLED_ID_UNTRUSTED_EVIDENCE",
        }.intersection(evidence_reason_codes)
    )
    legacy_single_compatible_exact = ranking.compatible_count == 1
    expanded_exact_family_ready = bool(
        ranking.compatible_count > 1
        and unique_retained_exact_family
        and ranking.margin_ppm >= policy.strong_margin_ppm
        and not cohort_risk_codes
        and not untrusted_prefilled_id
    )
    supplemental_exact_route_ready = bool(
        not frozen_resolver_exact_top or target_route_agrees
    )
    trusted_alias_lane_ready = bool(
        trusted_alias_ready and supplemental_exact_route_ready
    )
    conservative_strict_lane_ready = bool(
        strong
        and cohort_risk_codes
        and unique_strict_exact
        and supplemental_exact_route_ready
    )
    standard_strong_lane_ready = bool(
        strong and not cohort_risk_codes and supplemental_exact_route_ready
    )
    independent_auto_lane_ready = bool(
        trusted_alias_lane_ready
        or conservative_strict_lane_ready
        or standard_strong_lane_ready
    )
    if frozen_resolver_exact_top:
        reasons.add("FROZEN_RESOLVER_EXACT_TOP")
        reasons.add(
            "TARGET_ROUTE_AGREES"
            if target_route_agrees
            else "TARGET_ROUTE_MISMATCH"
        )
    if repository_curated_alias_override_ready:
        # A unique exact target from the pinned repository CSV does not depend
        # on lexical score or fuzzy-candidate separation.
        pass
    elif frozen_resolver_exact_top:
        if independent_auto_lane_ready:
            # The independent lanes have already passed their ordinary margin
            # threshold; exact resolver evidence is only supplemental here.
            reasons.add("MARGIN_GATE_PASSED")
        elif not legacy_single_compatible_exact:
            if not unique_retained_exact_family:
                reasons.add("AMBIGUOUS_CANDIDATES")
            if ranking.margin_ppm < policy.strong_margin_ppm:
                reasons.add("LOW_MARGIN")
            else:
                reasons.add("MARGIN_GATE_PASSED")
    elif len(compatible) > 1 and ranking.margin_ppm < policy.strong_margin_ppm:
        reasons.update(("AMBIGUOUS_CANDIDATES", "LOW_MARGIN"))
    else:
        reasons.add("MARGIN_GATE_PASSED")
    reasons.add("CATALOG_CORROBORATED")
    reasons.add("PROTECTED_SEMANTICS_COMPATIBLE")
    if top.programme_state is not ProgrammeState.PASS:
        reasons.add(
            "PROGRAMME_GATE_PENDING"
            if top.programme_state is ProgrammeState.NOT_CHECKED
            else "PROGRAMME_GATE_FAILED"
        )
        reasons.add("LANE_PROGRAMME_VERIFICATION")
        return DecisionState.PENDING_PROGRAMME, tuple(sorted(reasons)), selected, False
    reasons.add("PROGRAMME_GATE_PASSED")
    if repository_curated_alias_override_ready:
        reasons.update(
            (
                "LANE_REPOSITORY_CURATED_ALIAS",
                "REPOSITORY_CURATED_ALIAS_ALLOWLISTED",
                "SHADOW_ONLY_NO_WRITE_AUTHORITY",
            )
        )
        if top.score_ppm < policy.minimum_candidate_score_ppm:
            reasons.add("REPOSITORY_CURATED_ALIAS_SCORE_OVERRIDE")
        if ranking.margin_ppm < policy.strong_margin_ppm:
            reasons.add("REPOSITORY_CURATED_ALIAS_MARGIN_OVERRIDE")
        return DecisionState.AUTO_ELIGIBLE, tuple(sorted(reasons)), selected, False

    if trusted_alias_lane_ready:
        if curated_storage_alias_top:
            reasons.update(
                ("CURATED_STORAGE_ALIAS_EXACT", "STATIC_STORAGE_ROUTE_ALLOWLISTED")
            )
        else:
            reasons.add(
                "CURATED_ALIAS_EXACT"
                if "CURATED_ALIAS_EXACT" in top.methods
                else "LEARNED_ALIAS_EXACT"
            )
        if top.score_ppm < policy.strong_proposal_score_ppm:
            reasons.add("TRUSTED_ALIAS_SCORE_OVERRIDE")
        # The shadow package has no Sheet writer.  AUTO_ELIGIBLE describes the
        # evidence tier; auto_apply_eligible stays false until a signed,
        # calibrated production policy is installed in the guarded writer.
        reasons.update(("LANE_TRUSTED_ALIAS", "SHADOW_ONLY_NO_WRITE_AUTHORITY"))
        return DecisionState.AUTO_ELIGIBLE, tuple(sorted(reasons)), selected, False

    if conservative_strict_lane_ready:
        # Conservative cohorts may cross the shadow evidence boundary only for
        # a genuinely unique strict identity.  Fuzzy, relaxed, compact and bag
        # matches remain human-reviewable even when their aggregate score is high.
        reasons.update(
            (
                "LANE_CONSERVATIVE_STRICT_EXACT",
                "SHADOW_ONLY_NO_WRITE_AUTHORITY",
                "STRICT_EXACT_UNIQUE_CANDIDATE",
            )
        )
        return DecisionState.AUTO_ELIGIBLE, tuple(sorted(reasons)), selected, False

    if standard_strong_lane_ready:
        reasons.update(
            (
                "LANE_STANDARD_STRONG",
                "SHADOW_ONLY_NO_WRITE_AUTHORITY",
                "STANDARD_LANE_THRESHOLD_PASSED",
            )
        )
        return DecisionState.AUTO_ELIGIBLE, tuple(sorted(reasons)), selected, False

    if frozen_resolver_exact_top:
        if (
            alert_snapshot_present
            and one_explicit_route
            and target_route_agrees
            and (
                legacy_single_compatible_exact
                or expanded_exact_family_ready
            )
        ):
            if top.score_ppm < policy.strong_proposal_score_ppm:
                reasons.add("FROZEN_RESOLVER_EXACT_SCORE_OVERRIDE")
            reasons.update(
                (
                    "LANE_FROZEN_RESOLVER_EXACT",
                    "SHADOW_ONLY_NO_WRITE_AUTHORITY",
                )
            )
            return DecisionState.AUTO_ELIGIBLE, tuple(sorted(reasons)), selected, False
        # Supplemental exact evidence cannot downgrade an independently safe
        # lane. Preserve the original one-compatible-target path; expansion to
        # a fuzzy shortlist requires one exact family and the additional
        # margin, cohort, and prefilled-ID gates above.
        reasons.update(("FROZEN_RESOLVER_EXACT_GATES_FAILED", "LANE_HUMAN_REVIEW"))
        return DecisionState.NEEDS_REVIEW, tuple(sorted(reasons)), selected, False

    reasons.add("LANE_HUMAN_REVIEW")
    if strong:
        reasons.add("MULTI_SIGNAL_STRONG_PROPOSAL")
        if cohort_risk_codes:
            reasons.add("NON_STRICT_AUTO_BLOCKED_CONSERVATIVE_COHORT")
    elif top.score_ppm < policy.human_review_score_ppm:
        reasons.add("LOW_CALIBRATED_CONFIDENCE")
    return DecisionState.NEEDS_REVIEW, tuple(sorted(reasons)), selected, False


def _proposal(
    *,
    run_id: str,
    created_at: str,
    expires_at: str,
    subject: LabSubject,
    state: DecisionState,
    reasons: tuple[str, ...],
    selected_key: str,
    ranking: RankedCandidates,
    candidates: tuple[CandidateEvidence, ...],
    source_sha256: str,
    text_catalog_sha256: str,
    catalog_fingerprint_sha256: str,
    catalog_generation_token: str,
    policy: MatchingPolicy,
    lab_code_sha256: str,
    auto_apply_eligible: bool,
) -> ProposalRecord:
    draft = ProposalRecord(
        schema=PROPOSAL_SCHEMA,
        proposal_id="0" * 64,
        run_id=run_id,
        created_at=created_at,
        expires_at=expires_at,
        server_id=subject.server_id,
        stream_id=subject.stream_id,
        channel_name=subject.channel_name,
        category_name=subject.category_name,
        row_guard_sha256=subject.row_guard_sha256,
        provider_identity_sha256=subject.provider_identity_sha256,
        state=state,
        reason_codes=reasons,
        route_explicit=subject.route_explicit,
        market=subject.market,
        selected_candidate_key=selected_key,
        score_ppm=ranking.score_ppm,
        margin_ppm=ranking.margin_ppm,
        candidates=candidates,
        source_sha256=source_sha256,
        text_catalog_sha256=text_catalog_sha256,
        catalog_fingerprint_sha256=catalog_fingerprint_sha256,
        catalog_generation_token=catalog_generation_token,
        policy_sha256=policy.sha256,
        lab_code_sha256=lab_code_sha256,
        auto_apply_eligible=auto_apply_eligible,
    )
    result = replace(draft, proposal_id=draft.computed_id())
    result.verify_id()
    return result


def run_shadow(
    *,
    mappings_csv: Path,
    alerts_csv: Path | None,
    all_source_file: Path,
    all_source_catalog_file: Path,
    output_dir: Path,
    as_of: str,
    servers: Sequence[str] = SUPPORTED_SERVERS,
    policy: MatchingPolicy = DEFAULT_POLICY,
    minimum_unique_channels: int = catalog_stream.DEFAULT_MINIMUM_UNIQUE_CATALOG_CHANNELS,
    ai_api_key: str = "",
    ai_model: str = DEFAULT_AI_MODEL,
    ai_cache_path: Path | None = None,
    ai_max_requests: int = MAX_ADVISORY_REQUESTS,
    ai_shard_count: int = 1,
    ai_shard_index: int = 0,
    ai_transport: Any | None = None,
    ai_sensitive_values: Sequence[str] = (),
    ledger_path: Path | None = None,
) -> ShadowRunResult:
    """Generate a private deterministic proposal bundle and write no remote state."""

    if not isinstance(policy, MatchingPolicy) or policy.sha256 != DEFAULT_POLICY.sha256:
        raise ContractError(
            "Matching Lab v1 only emits the fixed, validator-supported shadow policy."
        )
    try:
        resolved_output = Path(output_dir).resolve(strict=False)
        resolved_ai_cache = (
            Path(ai_cache_path).resolve(strict=False)
            if ai_cache_path is not None
            else None
        )
        resolved_ledger = (
            Path(ledger_path).resolve(strict=False) if ledger_path is not None else None
        )
        resolved_inputs = {
            Path(path).resolve(strict=False)
            for path in (
                mappings_csv,
                alerts_csv,
                all_source_file,
                all_source_catalog_file,
            )
            if path is not None
        }
    except (OSError, RuntimeError) as exc:
        raise ContractError("Matching Lab output/state paths are invalid.") from exc
    for state_path in (resolved_ai_cache, resolved_ledger):
        if state_path is not None and state_path.is_relative_to(resolved_output):
            raise ContractError(
                "AI cache and ledger files must remain outside the bundle directory."
            )
        if state_path is not None and state_path in resolved_inputs:
            raise ContractError(
                "AI cache and ledger files must not overlap an input snapshot."
            )
    if (
        resolved_ai_cache is not None
        and resolved_ledger is not None
        and resolved_ai_cache == resolved_ledger
    ):
        raise ContractError("The AI cache and observation ledger need separate files.")
    try:
        selected_servers = tuple(
            sorted({streaming.normalize_server_id(value) for value in servers})
        )
    except streaming.BuildError as exc:
        raise ContractError("The Matching Lab server scope is invalid.") from exc
    if not selected_servers:
        raise ContractError("At least one server must be selected.")
    unsupported_servers = set(selected_servers).difference(SUPPORTED_SERVERS)
    if unsupported_servers:
        raise ContractError("The Matching Lab server scope is unsupported.")
    as_of_dt = _parse_utc(as_of)
    created_at = _utc_text(as_of_dt)
    expires_at = _utc_text(as_of_dt + timedelta(seconds=policy.expiry_seconds))
    as_of_epoch = int(as_of_dt.timestamp())
    ai_enabled = bool(str(ai_api_key or "").strip())
    ai_shard_count, ai_shard_index = validate_advisory_shard(
        ai_shard_count,
        ai_shard_index,
    )
    if not ai_enabled and (ai_shard_count != 1 or ai_shard_index != 0):
        raise ContractError("AI sharding is only valid when AI review is enabled.")
    if ai_enabled and ai_cache_path is None:
        raise ContractError("AI review requires a durable --ai-cache for replay safety.")
    ai_cache_namespace_sha256 = ""
    if ai_enabled:
        ai_model = validate_model_name(ai_model)
    if isinstance(ai_max_requests, bool) or not 1 <= int(ai_max_requests) <= MAX_ADVISORY_REQUESTS:
        raise ContractError(
            f"ai_max_requests must be between 1 and {MAX_ADVISORY_REQUESTS}."
        )
    if ai_enabled:
        try:
            ai_cache_namespace_sha256 = replay_cache_namespace_sha256(
                Path(ai_cache_path)
            )
        except AdvisoryCacheError as exc:
            raise ContractError("The AI replay cache failed closed.") from exc

    mapping_content, mapping_file_sha256 = read_stable_regular_file(
        mappings_csv, maximum_bytes=streaming.MAX_MAPPING_BYTES
    )
    try:
        table = sync.parse_mapping_csv(mapping_content)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    alerts, alerts_file_sha256 = _read_alerts(alerts_csv)
    try:
        open_alerts = sync.open_alert_quarantine_keys(alerts)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    text_bytes, text_catalog_sha256 = read_stable_regular_file(
        all_source_catalog_file,
        maximum_bytes=catalog_stream.MAX_CATALOG_TEXT_BYTES,
    )
    try:
        text_catalog = catalog_stream.parse_all_sources_text(
            text_bytes, minimum_ids=int(minimum_unique_channels)
        )
        text_catalog.validate_for_unattended_matching()
        automatch._validate_text_catalog_generation(
            text_catalog.generated_token, now_epoch=as_of_epoch
        )
    except (catalog_stream.CatalogStreamError, automatch.AutoMatchError) as exc:
        raise ContractError(str(exc)) from exc

    target_rows = [
        row
        for row in table.rows
        if row.get("server_id") in selected_servers
        and streaming.clean_text(row.get("action", ""), 40).upper() == "REVIEW"
    ]
    if len(target_rows) > MAX_PROPOSALS:
        raise ContractError("The selected REVIEW backlog exceeds the proposal limit.")
    aliases = human_alias_edges(table.rows, excluded_keys=open_alerts)
    rankings: dict[tuple[str, str], RankedCandidates] = {}
    subjects: dict[tuple[str, str], LabSubject] = {}
    eligibility: dict[tuple[str, str], str] = {}
    cohort_risks: dict[tuple[str, str], tuple[str, ...]] = {}
    prefilled_evidence: dict[tuple[str, str], tuple[str, ...]] = {}
    unsupported: set[tuple[str, str]] = set()
    corroboration_box: list[Any] = []
    runtime_box: list[Any] = []

    def select_provisional_ids(source_catalog: Any) -> Iterable[str]:
        xml_ids = frozenset(channel.epg_id for channel in source_catalog.channels)
        text_ids = frozenset(entry.epg_id for entry in text_catalog.entries)
        corroboration = automatch._corroborate_catalog_ids(
            xml_ids,
            text_ids,
            minimum_unique_channels=int(minimum_unique_channels),
        )
        real_candidates, dummy_ids = text_catalog.matcher_inputs()
        shared_candidates = [
            candidate
            for candidate in real_candidates
            if candidate.get("epg_id") in corroboration.exact_ids
            and str(candidate.get("epg_id") or "").casefold()
            not in corroboration.union_casefold_collision_keys
        ]
        shared_dummy_ids = {
            folded: epg_id
            for folded, epg_id in dummy_ids.items()
            if epg_id in corroboration.exact_ids
            and epg_id.casefold() not in corroboration.union_casefold_collision_keys
        }
        runtime = automatch.prepare_matcher_runtime(shared_candidates, shared_dummy_ids)
        if not runtime.identity.is_expected or not runtime.preflight.ready:
            raise ContractError("The frozen matcher failed its integrity preflight.")
        xml_names = {
            channel.epg_id: channel.display_name
            for channel in source_catalog.channels
            if channel.epg_id in corroboration.exact_ids
        }
        index = CandidateIndex(
            engine=runtime.resolver.engine,
            candidates=shared_candidates,
            xml_display_names=xml_names,
            aliases=aliases,
            repository_aliases_sha256=runtime.approved_aliases_sha256,
            policy=policy,
        )
        index.attach_resolver(runtime.resolver)
        selected_ids: set[str] = set()
        for row in target_rows:
            subject = subject_from_mapping_row(runtime.resolver.engine, row)
            subjects[subject.key] = subject
            allowed, reason = _eligible_review_row(row)
            eligibility[subject.key] = "" if allowed else reason
            cohort_risks[subject.key] = _cohort_risk_codes(subject)
            prefilled_evidence[subject.key] = _prefilled_id_reason_codes(
                row,
                corroborated_ids=frozenset(corroboration.exact_ids),
                collision_keys=frozenset(corroboration.union_casefold_collision_keys),
            )
            if _unsupported_channel(row):
                unsupported.add(subject.key)
                rankings[subject.key] = RankedCandidates((), 0, 0, 0, 0, 0)
                continue
            if subject.key in open_alerts or not allowed:
                rankings[subject.key] = RankedCandidates((), 0, 0, 0, 0, 0)
                continue
            ranked = index.rank(subject)
            rankings[subject.key] = ranked
            selected_ids.update(candidate.epg_id for candidate in ranked.candidates)
        if any(source_catalog.exact(epg_id) is None for epg_id in selected_ids):
            raise catalog_stream.CatalogStreamError(
                "The Matching Lab changed or escaped an exact source-catalog ID."
            )
        corroboration_box.append(corroboration)
        runtime_box.append(runtime)
        return tuple(sorted(selected_ids, key=lambda value: (value.casefold(), value)))

    try:
        one_pass = catalog_stream.stream_catalog_and_programmes_once(
            path=Path(all_source_file),
            fixed_wanted_ids=(),
            select_provisional_ids=select_provisional_ids,
            window_start=as_of_epoch - 86_400,
            now_epoch=as_of_epoch,
            minimum_unique_channels=int(minimum_unique_channels),
        )
    except (catalog_stream.CatalogStreamError, ContractError) as exc:
        raise ContractError(str(exc)) from exc
    if len(corroboration_box) != 1 or len(runtime_box) != 1:
        raise ContractError("The one-pass catalog selector did not complete exactly once.")

    lab_code_sha256 = package_code_sha256(Path(__file__).resolve().parent)
    mapping_table_sha256 = sync.mapping_table_fingerprint(table)
    open_alert_sha256 = sha256_json(
        [[server_id, stream_id] for server_id, stream_id in sorted(open_alerts)]
    )
    input_hashes = {
        "APPROVED_ALIASES": runtime_box[0].approved_aliases_sha256,
        "MAPPING_FILE": mapping_file_sha256,
        "MAPPING_TABLE": mapping_table_sha256,
        "ALERTS_FILE": alerts_file_sha256,
        "OPEN_ALERT_KEYS": open_alert_sha256,
        "EPG_XML": one_pass.source_sha256,
        "EPG_XML_CATALOG": one_pass.catalog.fingerprint_sha256,
        "EPG_TEXT": text_catalog_sha256,
        "EPG_TEXT_CATALOG": text_catalog.fingerprint_sha256,
        "AI_CONFIG": _ai_config_sha256(
            enabled=ai_enabled,
            model=ai_model,
            maximum_requests=int(ai_max_requests),
            shard_count=ai_shard_count,
            shard_index=ai_shard_index,
            cache_namespace_sha256=ai_cache_namespace_sha256,
        ),
    }
    run_id = sha256_json(
        {
            "schema": RUN_MANIFEST_SCHEMA,
            "mode": "shadow",
            "created_at": created_at,
            "servers": list(selected_servers),
            "inputs": input_hashes,
            "policy_sha256": policy.sha256,
            "lab_code_sha256": lab_code_sha256,
        }
    )

    proposals: list[ProposalRecord] = []
    for key in sorted(subjects, key=lambda value: (value[0], streaming.stream_sort_key(value[1]))):
        subject = subjects[key]
        ranking = rankings[key]
        candidates = tuple(
            _programme_candidate(candidate, one_pass.programme_gates.get(candidate.epg_id))
            for candidate in ranking.candidates
        )
        state, reasons, selected_key, auto_eligible = _decision(
            subject=subject,
            ranking=ranking,
            candidates=candidates,
            blocked_alert=key in open_alerts,
            eligibility_reason=eligibility[key],
            unsupported=key in unsupported,
            policy=policy,
            alert_snapshot_present=alerts_csv is not None,
            cohort_risk_codes=cohort_risks[key],
            evidence_reason_codes=prefilled_evidence[key],
        )
        proposals.append(
            _proposal(
                run_id=run_id,
                created_at=created_at,
                expires_at=expires_at,
                subject=subject,
                state=state,
                reasons=reasons,
                selected_key=selected_key,
                ranking=ranking,
                candidates=candidates,
                source_sha256=one_pass.source_sha256,
                text_catalog_sha256=text_catalog_sha256,
                catalog_fingerprint_sha256=text_catalog.fingerprint_sha256,
                catalog_generation_token=text_catalog.generated_token,
                policy=policy,
                lab_code_sha256=lab_code_sha256,
                auto_apply_eligible=auto_eligible,
            )
        )

    ai_counts: Counter[str] = Counter(
        {
            "AI_ELIGIBLE": 0,
            "AI_REQUESTED": 0,
            "AI_DEFERRED": 0,
            "AI_SUPPORTED": 0,
            "AI_REQUIRES_REVIEW": 0,
        }
    )
    if ai_enabled:
        eligible_requests = requests_from_proposals(proposals)
        if (
            ai_shard_count > 1
            and len(eligible_requests) > ai_shard_count * int(ai_max_requests)
        ):
            minimum_shards = (
                len(eligible_requests) + int(ai_max_requests) - 1
            ) // int(ai_max_requests)
            raise ContractError(
                "The AI shard plan cannot cover every eligible proposal; increase "
                f"ai_shard_count to at least {minimum_shards} "
                f"(maximum {MAX_ADVISORY_SHARDS})."
            )
        eligible_request_ids = {request.review_id for request in eligible_requests}
        shard_proposals = proposals_for_advisory_shard(
            (
                proposal
                for proposal in proposals
                if proposal.proposal_id in eligible_request_ids
            ),
            shard_count=ai_shard_count,
            shard_index=ai_shard_index,
        )
        shard_proposal_ids = {
            proposal.proposal_id for proposal in shard_proposals
        }
        shard_requests = tuple(
            request
            for request in eligible_requests
            if request.review_id in shard_proposal_ids
        )
        server_by_proposal = {
            proposal.proposal_id: proposal.server_id for proposal in proposals
        }
        request_queues = {
            server_id: deque(
                request
                for request in shard_requests
                if server_by_proposal[request.review_id] == server_id
            )
            for server_id in selected_servers
        }
        balanced_requests: list[Any] = []
        while any(request_queues.values()):
            for server_id in selected_servers:
                if request_queues[server_id]:
                    balanced_requests.append(request_queues[server_id].popleft())
        if balanced_requests:
            rotation = int(run_id[:16], 16) % len(balanced_requests)
            balanced_requests = (
                balanced_requests[rotation:] + balanced_requests[:rotation]
            )
        selected_requests = tuple(balanced_requests[: int(ai_max_requests)])
        ai_counts.update(
            {
                "AI_ELIGIBLE": len(eligible_requests),
                "AI_REQUESTED": len(selected_requests),
                "AI_DEFERRED": len(eligible_requests) - len(selected_requests),
            }
        )
        if selected_requests:
            try:
                advisory = review_advisories(
                    selected_requests,
                    api_key=ai_api_key,
                    model=ai_model,
                    cache_path=ai_cache_path,
                    transport=ai_transport,
                    sensitive_values=ai_sensitive_values,
                )
            except AdvisoryCacheError as exc:
                raise ContractError("The AI replay cache failed closed.") from exc
            outcomes = {outcome.review_id: outcome for outcome in advisory.outcomes}
            proposals = [
                attach_advisory(proposal, outcomes[proposal.proposal_id])
                if proposal.proposal_id in outcomes
                else proposal
                for proposal in proposals
            ]
            ai_counts.update(
                {
                    "AI_SUPPORTED": sum(
                        outcome.disposition is AdvisoryDisposition.SUPPORTS_LOCAL
                        for outcome in advisory.outcomes
                    ),
                    "AI_REQUIRES_REVIEW": sum(
                        outcome.requires_review for outcome in advisory.outcomes
                    ),
                }
            )

    counts = Counter(proposal.state.value for proposal in proposals)
    counts.update(
        {
            "REVIEW_ROWS": len(target_rows),
            "CATALOG_CANDIDATES": len(one_pass.catalog.channels),
            "PROGRAMME_IDS_CHECKED": len(one_pass.programme_gates),
        }
    )
    counts.update(ai_counts)
    summary = {
        "schema": "skytv.matching-lab-summary.v1",
        "mode": "shadow",
        "run_id": run_id,
        "generated_at": created_at,
        "lab_version": LAB_VERSION,
        "policy_id": policy.policy_id,
        "private_details_emitted": False,
        "counts": dict(sorted(counts.items())),
    }

    proposal_dicts = [proposal.public_dict() for proposal in proposals]

    manifest_box: list[RunManifest] = []

    def manifest_factory(*, proposals_sha256: str, summary_sha256: str) -> dict[str, object]:
        manifest = RunManifest(
            schema=RUN_MANIFEST_SCHEMA,
            run_id=run_id,
            mode="shadow",
            generated_at=created_at,
            expires_at=expires_at,
            input_sha256=input_hashes,
            policy_sha256=policy.sha256,
            lab_code_sha256=lab_code_sha256,
            proposal_count=len(proposals),
            proposals_sha256=proposals_sha256,
            summary_sha256=summary_sha256,
            counts=dict(counts),
        )
        manifest_box.append(manifest)
        return manifest.public_dict()

    write_private_bundle(
        output_dir,
        proposals=proposal_dicts,
        summary=summary,
        manifest_factory=manifest_factory,
    )
    if len(manifest_box) != 1:
        raise ContractError("The run manifest was not created exactly once.")
    if ledger_path is not None:
        try:
            with ObservationLedger(ledger_path) as ledger:
                ledger.record_run(manifest_box[0])
                for proposal in proposals:
                    ledger.record_proposal(proposal)
                ledger.verify_chain()
        except (LedgerError, sqlite3.Error, OSError) as exc:
            raise ContractError("The Matching Lab observation ledger failed closed.") from exc
    return ShadowRunResult(
        output_dir=Path(output_dir),
        run_id=run_id,
        proposal_count=len(proposals),
        counts=dict(sorted(counts.items())),
    )


__all__ = ("ShadowRunResult", "run_shadow")
