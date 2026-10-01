#!/usr/bin/env python3
"""Build a deterministic, private decision ledger from a validated shadow run.

This command is deliberately offline.  It does not contain a provider client,
Google client, HTTP transport, or write path.  Its only authority is to turn an
exact Mapping snapshot plus a strictly validated Matching Lab shadow bundle
into immutable private artifacts that a separate, explicit apply step may use.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import stat
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
for import_path in (SCRIPT_DIR, REPOSITORY_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import auto_match_inventory as automatch  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from matching_lab.artifacts import (  # noqa: E402
    MAX_PROPOSAL_BYTES,
    jsonl_bytes,
    package_code_sha256,
    read_stable_regular_file,
    strict_json_loads,
)
from matching_lab.models import (  # noqa: E402
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    canonical_json_bytes,
    require_opaque_identifier,
    require_sha256,
    sha256_bytes,
    sha256_json,
)
from matching_lab.retrieval import (  # noqa: E402
    mapping_row_guard,
    provider_identity_guard,
)
from matching_lab.validation import ValidationResult, validate_bundle  # noqa: E402


LEDGER_SCHEMA = "skytv.autonomous-decision-ledger-record.v1"
MANIFEST_SCHEMA = "skytv.autonomous-decision-ledger-manifest.v1"
PATCH_SCHEMA = "skytv.autonomous-mapping-patch.v1"
POLICY_VERSION = "autonomous-full-coverage-v1"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024

POLICY = {
    "schema": "skytv.autonomous-decision-policy.v1",
    "version": POLICY_VERSION,
    "server_1_panel_allowed": False,
    "verified_real_gate": "MATCHING_LAB_AUTO_ELIGIBLE",
    "unresolved_safe_fallback": "LOCAL_SYNTHETIC",
    "uncertain_synthetic_title": "Cleaned channel name",
    "manual_prefill_policy": "QUARANTINE_NO_OVERWRITE",
    "open_alert_policy": "QUARANTINE_UNCOVERED",
}
POLICY_SHA256 = sha256_json(POLICY)

TERMINAL_DECISIONS = frozenset(
    {
        "RETAIN_REAL",
        "RETAIN_NATIVE",
        "RETAIN_SYNTHETIC",
        "VERIFIED_EPGSHARE_REAL",
        "LOCAL_SYNTHETIC",
        "IGNORE",
        "QUARANTINED",
    }
)


@dataclass(frozen=True, slots=True)
class DecisionBaseResult:
    output_dir: Path
    run_id: str
    ledger_path: Path
    manifest_path: Path
    patch_path: Path | None
    record_count: int
    patch_count: int
    ledger_sha256: str
    manifest_sha256: str
    patch_sha256: str


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = str(value or "")
    if text.isdigit():
        return 0, int(text), text
    return 1, text.casefold(), text


def _normalized_text(value: object, *, maximum: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).casefold()
    if len(text) > maximum:
        raise ContractError("A normalized identity field exceeds its size limit.")
    return text


def _split_metadata(value: object) -> list[str]:
    return sorted(
        {
            _normalized_text(item, maximum=80).replace(" ", "_")
            for item in str(value or "").split("|")
            if _normalized_text(item, maximum=80)
        }
    )


def _target(
    *, source: str = "", feed: str = "", epg_id: str = "", candidate_key: str = ""
) -> dict[str, str]:
    if epg_id:
        require_opaque_identifier(epg_id, label="selected target epg_id", maximum=300)
    return {
        "source": source,
        "epg_feed": feed,
        "epg_id": epg_id,
        "candidate_key": candidate_key,
    }


def _prior_target(row: Mapping[str, str], *, enabled: bool) -> dict[str, object]:
    epg_id = str(row.get("epg_id", "") or "")
    if epg_id:
        require_opaque_identifier(epg_id, label="prior epg_id", maximum=300)
    return {
        "enabled": enabled,
        "action": streaming.clean_text(row.get("action", ""), 40).upper(),
        "source": streaming.clean_text(row.get("source", ""), 40).casefold(),
        "epg_feed": streaming.clean_text(row.get("epg_feed", ""), 80),
        "epg_id": epg_id,
    }


def _source_kind(row: Mapping[str, str], *, row_number: int) -> str:
    try:
        return streaming.normalize_requested_source(
            str(row.get("source", "") or ""),
            str(row.get("epg_feed", "") or ""),
            row_number=row_number,
        )
    except streaming.BuildError as exc:
        raise ContractError(f"Mapping row {row_number} has invalid EPG controls.") from exc


def _enabled(row: Mapping[str, str], *, row_number: int) -> bool:
    try:
        return streaming.parse_bool(
            row.get("enabled", ""), default=False, field_name="enabled"
        )
    except streaming.BuildError as exc:
        raise ContractError(f"Mapping row {row_number} has invalid enabled state.") from exc


def _is_manual_prefill(row: Mapping[str, str], *, source_kind: str) -> bool:
    """Return true only for a nonblank REVIEW target without machine provenance."""

    if not str(row.get("epg_id", "") or ""):
        return False
    server_id = str(row.get("server_id", "") or "")
    # This exact migration preimage is a machine quarantine whose stated
    # purpose is replacement by a reviewed EPGShare target. It is not a human
    # selection, and it is narrow enough to preserve the Server 1 no-panel
    # boundary while allowing a programme-backed AUTO_ELIGIBLE decision.
    if automatch._exact_legacy_server1_migration(row):
        return False
    # A Server 2/3 panel ID is copied from the provider inventory and is kept
    # as upgrade evidence.  Every other nonblank disabled REVIEW target is
    # protected.  Notes are not authorization: a human-editable cell must not
    # be able to spoof machine provenance and authorize its own overwrite.
    if source_kind == "panel" and server_id in {"server_2", "server_3"}:
        return False
    return True


def _synthetic_details(row: Mapping[str, str]) -> dict[str, object]:
    marker = automatch._coverage_fallback_marker(row)
    channel_name = streaming.clean_identifier(row.get("channel_name", ""), 300)
    prefix = "Synthetic."
    suffix = ".local"
    if not marker.startswith(prefix) or not marker.endswith(suffix):
        raise ContractError("The local synthetic classifier returned an invalid marker.")
    family = marker[len(prefix) : -len(suffix)]
    if not family or not channel_name:
        raise ContractError("The local synthetic classification is incomplete.")
    generic = family == "Channel"
    return {
        "marker": marker,
        "fallback_marker_family": family,
        "identity_classification": (
            "generic_unknown" if generic else family.casefold()
        ),
        "classification_confidence": "LOW" if generic else "MEDIUM",
        # Rendering is intentionally deferred to build_epg_streaming, whose
        # programme classifier uses the complete approved MappingRow.  A
        # marker family is routing metadata, not a claim that a real event or
        # movie schedule exists.
        "projected_programme_class": "DEFER_TO_PRODUCTION_BUILDER",
        "unclassified_display_policy": "CLEANED_CHANNEL_NAME",
        "claims_real_programme_facts": False,
    }


def _selected_candidate(proposal: ProposalRecord) -> Any:
    matches = tuple(
        candidate
        for candidate in proposal.candidates
        if candidate.candidate_key == proposal.selected_candidate_key
    )
    if len(matches) != 1:
        raise ContractError("An AUTO_ELIGIBLE proposal has no exact selected candidate.")
    candidate = matches[0]
    if candidate.conflicts or candidate.programme_state is not ProgrammeState.PASS:
        raise ContractError("An AUTO_ELIGIBLE target lacks passing conflict-free evidence.")
    return candidate


def _upgrade_evidence(
    row: Mapping[str, str], proposal: ProposalRecord, *, source_kind: str
) -> tuple[bool, list[dict[str, object]]]:
    evidence: list[dict[str, object]] = []
    for candidate in proposal.candidates:
        if candidate.conflicts or candidate.programme_state is not ProgrammeState.PASS:
            continue
        evidence.append(
            {
                "kind": "PASSING_EPGSHARE_CANDIDATE",
                "candidate_key": candidate.candidate_key,
                "epg_id": candidate.epg_id,
                "score_ppm": candidate.score_ppm,
            }
        )
    native_epg_id = str(row.get("epg_id", "") or "")
    if (
        source_kind == "panel"
        and str(row.get("server_id", "")) in {"server_2", "server_3"}
        and native_epg_id
    ):
        evidence.append(
            {
                "kind": "NATIVE_ID_PRESENT",
                "candidate_key": "",
                "epg_id": native_epg_id,
                "score_ppm": 0,
            }
        )
    evidence.sort(
        key=lambda item: (
            str(item["kind"]),
            -int(item["score_ppm"]),
            str(item["epg_id"]).casefold(),
            str(item["epg_id"]),
        )
    )
    return bool(evidence), evidence


def _retained_synthetic_upgrade_evidence(
    row: Mapping[str, str],
) -> tuple[bool, list[dict[str, object]]]:
    """Recover an exact pre-fallback target without treating it as verified.

    Coverage-fallback rows deliberately retain their prior source/feed/ID in a
    length-prefixed rollback record.  That evidence is enough to place the row
    in a future real-guide revalidation queue, but never enough to publish the
    prior target directly.
    """

    preimage = automatch.verified_coverage_fallback_preimage(row)
    if preimage is None:
        return False, []
    prior_source = str(preimage.get("source", "") or "")
    prior_feed = str(preimage.get("epg_feed", "") or "")
    prior_epg_id = str(preimage.get("epg_id", "") or "")
    if not prior_epg_id:
        return False, []
    try:
        source_kind = streaming.normalize_requested_source(
            prior_source, prior_feed, row_number=0
        )
    except streaming.BuildError:
        return False, []
    server_id = str(row.get("server_id", "") or "")
    if source_kind == "panel":
        if server_id not in {"server_2", "server_3"}:
            return False, []
        kind = "ROLLBACK_NATIVE_ID_PRESENT"
    elif source_kind == "epgshare01":
        kind = "ROLLBACK_EPGSHARE_ID_PRESENT"
    else:
        return False, []
    require_opaque_identifier(prior_epg_id, label="rollback epg_id", maximum=300)
    return True, [
        {
            "kind": kind,
            "candidate_key": "",
            "epg_id": prior_epg_id,
            "score_ppm": 0,
        }
    ]


def _record_id(record: Mapping[str, object]) -> str:
    return sha256_json({key: value for key, value in record.items() if key != "record_id"})


def _patch_id(record: Mapping[str, object]) -> str:
    return sha256_json({key: value for key, value in record.items() if key != "patch_id"})


def _percent(numerator: int, denominator: int) -> str:
    if not denominator:
        return "0.00"
    basis_points = (int(numerator) * 1_000_000 + denominator // 2) // denominator
    return f"{basis_points // 10_000}.{(basis_points % 10_000) // 100:02d}"


def _read_bundle_documents(
    bundle_dir: Path, validated: ValidationResult
) -> tuple[dict[str, Any], dict[str, str], dict[str, bytes]]:
    try:
        names = {entry.name for entry in Path(bundle_dir).iterdir()}
    except OSError as exc:
        raise ContractError("The shadow bundle cannot be relisted safely.") from exc
    if names != {"manifest.json", "proposals.jsonl", "summary.json"}:
        raise ContractError("The shadow bundle changed after validation.")
    manifest_content, manifest_sha256 = read_stable_regular_file(
        Path(bundle_dir) / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES
    )
    summary_content, summary_sha256 = read_stable_regular_file(
        Path(bundle_dir) / "summary.json", maximum_bytes=MAX_SUMMARY_BYTES
    )
    proposals_content, proposals_sha256 = read_stable_regular_file(
        Path(bundle_dir) / "proposals.jsonl", maximum_bytes=MAX_PROPOSAL_BYTES
    )
    raw_manifest = strict_json_loads(manifest_content)
    if type(raw_manifest) is not dict:
        raise ContractError("The validated shadow manifest is not an object.")
    if manifest_content != canonical_json_bytes(raw_manifest) + b"\n":
        raise ContractError("The shadow manifest changed after validation.")
    if str(raw_manifest.get("proposals_sha256", "")) != proposals_sha256:
        raise ContractError("The shadow proposal artifact changed after validation.")
    if str(raw_manifest.get("summary_sha256", "")) != summary_sha256:
        raise ContractError("The shadow summary artifact changed after validation.")
    validated_proposal_content = jsonl_bytes(
        proposal.public_dict() for proposal in validated.proposals
    )
    if proposals_content != validated_proposal_content:
        raise ContractError(
            "The shadow proposals no longer match the validated proposal objects."
        )
    if (
        str(raw_manifest.get("run_id", "")) != validated.run_id
        or str(raw_manifest.get("generated_at", "")) != validated.generated_at
        or str(raw_manifest.get("expires_at", "")) != validated.expires_at
        or raw_manifest.get("proposal_count") != validated.proposal_count
    ):
        raise ContractError("The shadow manifest no longer matches validation.")
    return (
        raw_manifest,
        {
            "shadow_manifest": manifest_sha256,
            "shadow_proposals": proposals_sha256,
            "shadow_summary": summary_sha256,
        },
        {
            "manifest.json": manifest_content,
            "proposals.jsonl": proposals_content,
            "summary.json": summary_content,
        },
    )


def _read_mapping(path: Path) -> tuple[sync.MappingTable, str]:
    content, digest = read_stable_regular_file(
        path, maximum_bytes=streaming.MAX_MAPPING_BYTES
    )
    try:
        table = sync.parse_mapping_csv(content, require_v1_order=True)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    if tuple(table.headers) != tuple(streaming.SHEET_COLUMNS) or len(table.headers) != 33:
        raise ContractError("The Mapping input is not the exact 33-column Version 1 schema.")
    return table, digest


def _read_alert_snapshot(
    path: Path | None,
) -> tuple[str, frozenset[tuple[str, str]]]:
    if path is None:
        return sha256_bytes(b""), frozenset()
    content, digest = read_stable_regular_file(path, maximum_bytes=sync.MAX_SYNC_ALERT_BYTES)
    try:
        text = content.decode("utf-8-sig")
        alerts = sync.parse_sync_alert_values(
            list(csv.reader(io.StringIO(text, newline="")))
        )
        open_keys = sync.open_alert_quarantine_keys(alerts)
    except (UnicodeDecodeError, sync.SyncError) as exc:
        raise ContractError("The Sync Alerts CSV is invalid.") from exc
    return digest, frozenset(open_keys)


def _prepare_output_dir(path: Path) -> Path:
    target = Path(path)
    try:
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ContractError("The decision-base output must be a real directory.")
            if any(target.iterdir()):
                raise ContractError("The decision-base output directory must be empty.")
        else:
            target.mkdir(mode=0o700, parents=True, exist_ok=False)
        target.chmod(0o700)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("The decision-base output directory cannot be prepared.") from exc
    return target


def _atomic_private_write(path: Path, content: bytes) -> None:
    if path.exists():
        raise ContractError("A decision-base artifact already exists.")
    temporary = path.with_suffix(path.suffix + ".part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ContractError("A decision-base artifact is not a regular file.")
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except ContractError:
        raise
    except (FileExistsError, OSError) as exc:
        raise ContractError("A decision-base artifact could not be written safely.") from exc


def _validate_captured_bundle(
    *,
    documents: Mapping[str, bytes],
    mappings_csv: Path,
    alerts_csv: Path,
    as_of: str,
) -> ValidationResult:
    """Run the full validator over the exact three byte strings just reread."""

    with tempfile.TemporaryDirectory(prefix="skytv-decision-snapshot-") as temporary:
        snapshot = Path(temporary) / "bundle"
        snapshot.mkdir(mode=0o700)
        for name in ("manifest.json", "proposals.jsonl", "summary.json"):
            content = documents.get(name)
            if not isinstance(content, bytes):
                raise ContractError("The captured shadow bundle is incomplete.")
            _atomic_private_write(snapshot / name, content)
        return validate_bundle(
            snapshot,
            mappings_csv=mappings_csv,
            alerts_csv=alerts_csv,
            as_of=as_of,
        )


def _patch_for_record(
    *,
    row: Mapping[str, str],
    record: Mapping[str, object],
    source_sha256: str,
) -> dict[str, object] | None:
    decision = str(record["decision"])
    if decision not in {"VERIFIED_EPGSHARE_REAL", "LOCAL_SYNTHETIC", "IGNORE"}:
        return None
    if decision == "IGNORE" and "DECORATIVE_PROVIDER_HEADING" not in set(
        record["evidence"]["reason_codes"]
    ):
        return None
    expected = {
        field: str(row.get(field, "") or "")
        for field in ("enabled", "action", "source", "epg_feed", "epg_id")
    }
    changes: dict[str, str]
    if decision == "VERIFIED_EPGSHARE_REAL":
        target = record["selected_target"]
        if not isinstance(target, dict):
            raise ContractError("A verified-real ledger target is malformed.")
        changes = {
            "enabled": "TRUE",
            "action": "AUTO_EPGSHARE",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": str(target["epg_id"]),
            "reason": "Autonomous exact target passed the validated Matching Lab gate.",
            "notes": (
                f"{POLICY_VERSION} run={record['run_id']} "
                f"proposal={record['evidence']['proposal_id']}"
            )[:500],
        }
    elif decision == "LOCAL_SYNTHETIC":
        patched = dict(row)
        key = (str(row["server_id"]), str(row["stream_id"]))
        try:
            automatch._apply_coverage_fallback(
                key=key,
                original=row,
                patched=patched,
                source_sha256=source_sha256,
            )
        except automatch.AutoMatchError as exc:
            raise ContractError(
                "A local synthetic patch could not preserve rollback state."
            ) from exc
        changes = {
            field: str(patched[field])
            for field in (
                "enabled",
                "action",
                "source",
                "epg_feed",
                "epg_id",
                "reason",
                "notes",
            )
        }
        selected_target = record.get("selected_target")
        if (
            not isinstance(selected_target, dict)
            or changes["epg_id"] != selected_target.get("epg_id")
        ):
            raise ContractError(
                "The local synthetic patch differs from its ledger target."
            )
    else:
        changes = {
            "enabled": "FALSE",
            "action": "IGNORE",
            "reason": "Decorative provider heading; no channel schedule is applicable.",
            "notes": f"{POLICY_VERSION} run={record['run_id']}"[:500],
        }
    patch: dict[str, object] = {
        "schema": PATCH_SCHEMA,
        "patch_id": "",
        "run_id": record["run_id"],
        "ledger_record_id": record["record_id"],
        "identity": record["identity"],
        "expected": expected,
        "changes": changes,
        "reason_codes": record["evidence"]["reason_codes"],
    }
    patch["patch_id"] = _patch_id(patch)
    return patch


def _decision_record(
    *,
    row: Mapping[str, str],
    row_number: int,
    proposal: ProposalRecord | None,
    run_id: str,
    source_hashes: Mapping[str, str],
    open_alert: bool | None = None,
) -> dict[str, object]:
    server_id = streaming.normalize_server_id(row.get("server_id", ""))
    stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
    channel_name = streaming.clean_identifier(row.get("channel_name", ""), 300)
    action = streaming.clean_text(row.get("action", ""), 40).upper()
    enabled = _enabled(row, row_number=row_number)
    source_kind = _source_kind(row, row_number=row_number)
    provider_sha256 = provider_identity_guard(row)
    row_sha256 = mapping_row_guard(row)
    ledger_key_sha256 = sha256_json(
        {
            "schema": "skytv.autonomous-ledger-key.v1",
            "server_id": server_id,
            "stream_id": stream_id,
            "provider_identity_sha256": provider_sha256,
        }
    )
    if not stream_id or not channel_name:
        raise ContractError(f"Mapping row {row_number} has an incomplete identity.")
    if action not in streaming.ALLOWED_ACTIONS:
        raise ContractError(f"Mapping row {row_number} has an unsupported action.")
    expected_source_by_action = {
        "AUTO_EPGSHARE": "epgshare01",
        "KEEP_PANEL": "panel",
        "AUTO_DUMMY": "dummy",
    }
    expected_source = expected_source_by_action.get(action)
    if expected_source is not None and source_kind != expected_source:
        raise ContractError(
            f"Mapping row {row_number} has inconsistent action/source controls."
        )
    if (action == "REVIEW") != (proposal is not None):
        raise ContractError("Shadow proposal ownership does not match the Mapping action.")

    decision = ""
    reason_codes: list[str] = []
    confidence_level = "HIGH"
    confidence_ppm = 1_000_000
    selected_target = _target()
    synthetic: dict[str, object] | None = None
    upgrade_candidate = False
    upgrade_evidence: list[dict[str, object]] = []

    shadow_blocked = bool(
        proposal is not None and proposal.state is DecisionState.BLOCKED_ALERT
    )
    if open_alert is not None and bool(open_alert) != shadow_blocked and proposal is not None:
        raise ContractError("The current OPEN-alert state differs from the shadow decision.")
    effective_open_alert = shadow_blocked if open_alert is None else bool(open_alert)

    if effective_open_alert:
        decision = "QUARANTINED"
        reason_codes = ["OPEN_SYNC_ALERT"]
        confidence_level = "NONE"
        confidence_ppm = 0
    elif action in {"IGNORE", "SKIP", "REJECTED"}:
        decision = "IGNORE"
        reason_codes = [f"EXISTING_{action}_TERMINAL"]
    elif action in {"UNMATCHED", "NO_EPG", "UNRESOLVED"}:
        decision = "QUARANTINED"
        reason_codes = [f"EXISTING_{action}_UNCOVERED"]
        confidence_level = "NONE"
        confidence_ppm = 0
    elif action == "REVIEW":
        if proposal is None:  # pragma: no cover - guarded above.
            raise ContractError("A REVIEW row is missing its shadow proposal.")
        if enabled:
            decision = "QUARANTINED"
            reason_codes = ["ENABLED_REVIEW_CONFLICT"]
            confidence_level = "NONE"
            confidence_ppm = 0
        else:
            manual_prefill = _is_manual_prefill(row, source_kind=source_kind)
            selected = (
                _selected_candidate(proposal)
                if proposal.state is DecisionState.AUTO_ELIGIBLE
                else None
            )
            changes_manual_target = bool(
                manual_prefill
                and selected is not None
                and selected.epg_id != str(row.get("epg_id", "") or "")
            )
            native_revalidation_required = bool(
                source_kind == "panel"
                and server_id in {"server_2", "server_3"}
                and str(row.get("epg_id", "") or "")
            )
            # Protect every nonblank human/unbound target before classifying a
            # heading.  Heading-shaped text is not authority to discard a
            # manually selected EPG candidate.
            if manual_prefill and (selected is None or changes_manual_target):
                decision = "QUARANTINED"
                reason_codes = [
                    "MANUAL_CANDIDATE_PROTECTED",
                    "NO_AUTONOMOUS_OVERWRITE",
                ]
                confidence_level = "NONE"
                confidence_ppm = 0
                upgrade_candidate, upgrade_evidence = _upgrade_evidence(
                    row, proposal, source_kind=source_kind
                )
            elif selected is not None:
                decision = "VERIFIED_EPGSHARE_REAL"
                reason_codes = [
                    "MATCHING_LAB_AUTO_ELIGIBLE",
                    "PROGRAMME_GATE_PASSED",
                    "EXACT_SHADOW_CANDIDATE_SELECTED",
                ]
                if manual_prefill:
                    reason_codes.append("MANUAL_TARGET_REVALIDATED_UNCHANGED")
                confidence_ppm = proposal.score_ppm
                selected_target = _target(
                    source="epgshare01",
                    feed="ALL_SOURCES1",
                    epg_id=selected.epg_id,
                    candidate_key=selected.candidate_key,
                )
            elif native_revalidation_required:
                # The EPGShare shadow bundle contains no native XMLTV
                # programme/name verdict.  Preserve the provider target until
                # the production native verifier returns KEEP_PANEL or a
                # definitive terminal miss; this offline ledger has no
                # authority to replace it with a synthetic guide.
                decision = "QUARANTINED"
                reason_codes = ["NATIVE_REVALIDATION_REQUIRED"]
                confidence_level = "NONE"
                confidence_ppm = 0
                upgrade_candidate, upgrade_evidence = _upgrade_evidence(
                    row, proposal, source_kind=source_kind
                )
            elif automatch._matcher_is_decorative_heading(channel_name):
                decision = "IGNORE"
                reason_codes = ["DECORATIVE_PROVIDER_HEADING"]
            else:
                decision = "LOCAL_SYNTHETIC"
                reason_codes = [
                    "SAFE_UNRESOLVED_FALLBACK",
                    "NO_REAL_PROGRAMME_CLAIM",
                ]
                synthetic = _synthetic_details(row)
                selected_target = _target(
                    source="dummy",
                    feed="DUMMY_CHANNELS",
                    epg_id=str(synthetic["marker"]),
                )
                upgrade_candidate, upgrade_evidence = _upgrade_evidence(
                    row, proposal, source_kind=source_kind
                )
                confidence_level = "MEDIUM"
                confidence_ppm = 700_000
    elif action in streaming.ACTIVE_ACTIONS:
        if not enabled:
            decision = "QUARANTINED"
            reason_codes = ["DISABLED_ACTIVE_MAPPING"]
            confidence_level = "NONE"
            confidence_ppm = 0
        else:
            epg_id = str(row.get("epg_id", "") or "")
            if not epg_id:
                raise ContractError(
                    f"Mapping row {row_number} is enabled without an EPG ID."
                )
            if source_kind == "panel":
                if server_id == "server_1":
                    raise ContractError(
                        "Server 1 cannot retain a panel/native EPG target."
                    )
                decision = "RETAIN_NATIVE"
                reason_codes = ["EXISTING_ENABLED_NATIVE"]
            elif source_kind == "dummy":
                decision = "RETAIN_SYNTHETIC"
                reason_codes = ["EXISTING_ENABLED_LOCAL_SYNTHETIC"]
                synthetic = _synthetic_details(row)
                upgrade_candidate, upgrade_evidence = (
                    _retained_synthetic_upgrade_evidence(row)
                )
            elif source_kind == "epgshare01":
                decision = "RETAIN_REAL"
                reason_codes = ["EXISTING_ENABLED_EPGSHARE"]
            else:  # pragma: no cover - guarded by normalize_requested_source.
                raise ContractError(
                    f"Mapping row {row_number} has an unsupported source."
                )
            selected_target = _target(
                source=source_kind,
                feed=str(row.get("epg_feed", "") or ""),
                epg_id=epg_id,
            )
    else:  # pragma: no cover - ALLOWED_ACTIONS is exhausted above.
        raise ContractError("A Mapping row has no supported terminal action.")

    if decision not in TERMINAL_DECISIONS:
        raise ContractError("A Mapping row did not receive exactly one terminal decision.")
    if server_id == "server_1" and selected_target["source"] == "panel":
        raise ContractError("Server 1 panel policy was violated.")

    proposal_id = proposal.proposal_id if proposal is not None else ""
    shadow_state = proposal.state.value if proposal is not None else "NOT_APPLICABLE"
    record: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "record_id": "",
        "run_id": run_id,
        "ledger_key_sha256": ledger_key_sha256,
        "identity": {
            "server_id": server_id,
            "stream_id": stream_id,
            "provider_identity_sha256": provider_sha256,
            "row_guard_sha256": row_sha256,
            "channel_name": channel_name,
            "category_name": streaming.clean_text(row.get("category_name", ""), 200),
            "normalized_channel_name": _normalized_text(channel_name, maximum=300),
            "normalized_category_name": _normalized_text(
                row.get("category_name", ""), maximum=200
            ),
        },
        "classification": {
            "market": streaming.clean_text(
                proposal.market
                if proposal is not None and proposal.market
                else row.get("region_code", ""),
                80,
            ).upper(),
            "primary_language": _normalized_text(
                row.get("primary_language", ""), maximum=80
            ),
            "language_codes": _split_metadata(row.get("language_codes", "")),
            "genre": _normalized_text(row.get("genre", ""), maximum=80),
            "role": _normalized_text(row.get("channel_role", ""), maximum=80),
            "synthetic": synthetic,
        },
        "decision": decision,
        "confidence": {
            "level": confidence_level,
            "score_ppm": confidence_ppm,
        },
        "selected_target": selected_target,
        "prior_target": _prior_target(row, enabled=enabled),
        "upgrade_candidate": upgrade_candidate,
        "upgrade_evidence": upgrade_evidence,
        "evidence": {
            "reason_codes": sorted(set(reason_codes)),
            "proposal_id": proposal_id,
            "shadow_state": shadow_state,
            "shadow_reason_codes": list(proposal.reason_codes) if proposal else [],
            "model": {
                "name": proposal.ai.model if proposal is not None and proposal.ai else "",
                "decision": (
                    proposal.ai.decision
                    if proposal is not None and proposal.ai
                    else "NOT_USED"
                ),
                "request_sha256": (
                    proposal.ai.request_sha256
                    if proposal is not None and proposal.ai
                    else ""
                ),
            },
            "source_hashes": dict(sorted(source_hashes.items())),
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
        },
    }
    record["record_id"] = _record_id(record)
    return record


def build_decision_base(
    *,
    mappings_csv: Path,
    alerts_csv: Path | None,
    shadow_bundle: Path,
    output_dir: Path,
    as_of: str,
    emit_mapping_patch: bool = False,
) -> DecisionBaseResult:
    """Create a deterministic decision ledger without mutating external state."""

    if not as_of:
        raise ContractError("A pinned --as-of timestamp is required.")
    if alerts_csv is None:
        raise ContractError(
            "A complete autonomous ledger requires the current Sync Alerts CSV."
        )
    mappings_path = Path(mappings_csv)
    alerts_path = Path(alerts_csv)
    validated: ValidationResult = validate_bundle(
        Path(shadow_bundle),
        mappings_csv=mappings_path,
        alerts_csv=alerts_path,
        as_of=as_of,
    )
    if not validated.mappings_checked:
        raise ContractError("The shadow bundle was not validated against Mappings.")
    if not validated.alerts_checked:
        raise ContractError(
            "The shadow bundle was not validated against current Sync Alerts."
        )

    table, mapping_sha256 = _read_mapping(mappings_path)
    alerts_sha256, open_alert_keys = _read_alert_snapshot(alerts_path)
    shadow_manifest, bundle_hashes, captured_documents = _read_bundle_documents(
        Path(shadow_bundle), validated
    )
    captured_validation = _validate_captured_bundle(
        documents=captured_documents,
        mappings_csv=mappings_path,
        alerts_csv=alerts_path,
        as_of=as_of,
    )
    if (
        captured_validation.run_id != validated.run_id
        or captured_validation.generated_at != validated.generated_at
        or captured_validation.expires_at != validated.expires_at
        or captured_validation.validated_as_of != validated.validated_as_of
        or captured_validation.counts != validated.counts
        or jsonl_bytes(
            proposal.public_dict() for proposal in captured_validation.proposals
        )
        != jsonl_bytes(proposal.public_dict() for proposal in validated.proposals)
    ):
        raise ContractError("The captured shadow bundle differs from validation.")
    validated = captured_validation
    input_hashes = shadow_manifest.get("input_sha256")
    if type(input_hashes) is not dict:
        raise ContractError("The shadow manifest input binding is malformed.")
    if str(input_hashes.get("MAPPING_FILE", "")) != mapping_sha256:
        raise ContractError("The Mapping input changed after shadow validation.")
    manifest_alert_sha256 = str(input_hashes.get("ALERTS_FILE", ""))
    if manifest_alert_sha256 == sha256_bytes(b""):
        raise ContractError(
            "The autonomous policy requires a shadow run with an alert snapshot."
        )
    if manifest_alert_sha256 != alerts_sha256:
        raise ContractError("The Sync Alerts input changed after shadow validation.")
    if str(shadow_manifest.get("run_id", "")) != validated.run_id:
        raise ContractError("The shadow run identity changed after validation.")

    script_content, script_sha256 = read_stable_regular_file(
        Path(__file__), maximum_bytes=4 * 1024 * 1024
    )
    del script_content
    dependency_hashes: dict[str, str] = {}
    for name, path in (
        ("dependency_auto_match", SCRIPT_DIR / "auto_match_inventory.py"),
        ("dependency_builder", SCRIPT_DIR / "build_epg_streaming.py"),
        ("dependency_grounded_search", SCRIPT_DIR / "ai_grounded_search.py"),
        ("dependency_sync", SCRIPT_DIR / "sync_channel_inventory.py"),
        (
            "dependency_contextual_engine",
            REPOSITORY_ROOT / "src" / "skytv_epg_contextual_v8.py",
        ),
    ):
        _content, digest = read_stable_regular_file(
            path, maximum_bytes=16 * 1024 * 1024
        )
        dependency_hashes[name] = digest
    dependency_hashes["matching_stack"] = package_code_sha256(
        REPOSITORY_ROOT / "matching_lab"
    )
    if str(shadow_manifest.get("lab_code_sha256", "")) != dependency_hashes[
        "matching_stack"
    ]:
        raise ContractError(
            "The matching dependency stack changed after shadow validation."
        )
    source_hashes = {
        "mapping_file": mapping_sha256,
        "alerts_file": manifest_alert_sha256,
        "shadow_manifest": bundle_hashes["shadow_manifest"],
        "shadow_proposals": bundle_hashes["shadow_proposals"],
        "shadow_summary": bundle_hashes["shadow_summary"],
        "shadow_epg_xml": str(input_hashes.get("EPG_XML", "")),
        "shadow_epg_text": str(input_hashes.get("EPG_TEXT", "")),
        "generator_code": script_sha256,
        **dependency_hashes,
    }
    for name, value in source_hashes.items():
        require_sha256(value, label=f"decision-base source hash {name}")
    run_id = sha256_json(
        {
            "schema": MANIFEST_SCHEMA,
            "as_of": validated.validated_as_of,
            "shadow_run_id": validated.run_id,
            "policy_sha256": POLICY_SHA256,
            "source_hashes": source_hashes,
        }
    )

    proposal_by_key: dict[tuple[str, str], ProposalRecord] = {}
    for proposal in validated.proposals:
        key = (proposal.server_id, proposal.stream_id)
        if key in proposal_by_key:
            raise ContractError("The validated shadow bundle contains a duplicate proposal key.")
        proposal_by_key[key] = proposal

    expected_review_keys = {
        (str(row["server_id"]), str(row["stream_id"]))
        for row in table.rows
        if streaming.clean_text(row.get("action", ""), 40).upper() == "REVIEW"
    }
    if set(proposal_by_key) != expected_review_keys:
        raise ContractError("The shadow proposal set does not exactly cover Mapping REVIEW rows.")

    records: list[dict[str, object]] = []
    patches: list[dict[str, object]] = []
    seen_ledger_keys: set[str] = set()
    for row_number, row in sorted(
        zip(table.row_numbers, table.rows),
        key=lambda item: (
            str(item[1]["server_id"]),
            _stream_sort_key(item[1]["stream_id"]),
        ),
    ):
        key = (str(row["server_id"]), str(row["stream_id"]))
        proposal = proposal_by_key.get(key)
        if proposal is not None and (
            proposal.provider_identity_sha256 != provider_identity_guard(row)
            or proposal.row_guard_sha256 != mapping_row_guard(row)
        ):
            raise ContractError("A proposal differs from its exact Mapping row.")
        record = _decision_record(
            row=row,
            row_number=row_number,
            proposal=proposal,
            run_id=run_id,
            source_hashes=source_hashes,
            open_alert=key in open_alert_keys,
        )
        ledger_key = str(record["ledger_key_sha256"])
        if ledger_key in seen_ledger_keys:
            raise ContractError("The decision ledger contains a duplicate provider identity key.")
        seen_ledger_keys.add(ledger_key)
        records.append(record)
        if emit_mapping_patch:
            source_sha256 = (
                proposal.source_sha256
                if proposal is not None
                else source_hashes["shadow_epg_xml"]
            )
            patch = _patch_for_record(
                row=row,
                record=record,
                source_sha256=source_sha256,
            )
            if patch is not None:
                patches.append(patch)

    if len(records) != len(table.rows) or len(seen_ledger_keys) != len(table.rows):
        raise ContractError("The decision ledger does not reconcile one-to-one with Mappings.")

    decision_counts = Counter(str(record["decision"]) for record in records)
    quarantine_counts: Counter[str] = Counter()
    for record in records:
        if record["decision"] != "QUARANTINED":
            continue
        codes = tuple(str(code) for code in record["evidence"]["reason_codes"])
        primary = next(
            (
                code
                for code in (
                    "OPEN_SYNC_ALERT",
                    "MANUAL_CANDIDATE_PROTECTED",
                    "NATIVE_REVALIDATION_REQUIRED",
                )
                if code in codes
            ),
            codes[0] if codes else "UNSPECIFIED_QUARANTINE",
        )
        quarantine_counts[primary] += 1
    real = decision_counts["RETAIN_REAL"] + decision_counts["VERIFIED_EPGSHARE_REAL"]
    native = decision_counts["RETAIN_NATIVE"]
    synthetic = decision_counts["RETAIN_SYNTHETIC"] + decision_counts["LOCAL_SYNTHETIC"]
    ignored = decision_counts["IGNORE"]
    quarantined = decision_counts["QUARANTINED"]
    literal_total = len(records)
    actionable = literal_total - ignored
    assigned = real + native + synthetic
    uncovered = quarantined
    if real + native + synthetic + ignored + quarantined != literal_total:
        raise ContractError("Decision assignment buckets do not reconcile to literal rows.")
    if assigned + uncovered != actionable:
        raise ContractError("Decision assignment does not reconcile to actionable rows.")
    if sum(quarantine_counts.values()) != quarantined:
        raise ContractError("Quarantine reason counts do not reconcile.")

    terminal_dependencies = {
        "generator_code": Path(__file__),
        "dependency_auto_match": SCRIPT_DIR / "auto_match_inventory.py",
        "dependency_builder": SCRIPT_DIR / "build_epg_streaming.py",
        "dependency_grounded_search": SCRIPT_DIR / "ai_grounded_search.py",
        "dependency_sync": SCRIPT_DIR / "sync_channel_inventory.py",
        "dependency_contextual_engine": (
            REPOSITORY_ROOT / "src" / "skytv_epg_contextual_v8.py"
        ),
    }
    for name, path in terminal_dependencies.items():
        _content, digest = read_stable_regular_file(
            path, maximum_bytes=16 * 1024 * 1024
        )
        if digest != source_hashes[name]:
            raise ContractError("A decision dependency changed during generation.")
    if package_code_sha256(REPOSITORY_ROOT / "matching_lab") != source_hashes[
        "matching_stack"
    ]:
        raise ContractError("The matching dependency stack changed during generation.")

    ledger_content = jsonl_bytes(records)
    patch_content = jsonl_bytes(patches) if emit_mapping_patch else b""
    ledger_sha256 = sha256_bytes(ledger_content)
    patch_sha256 = sha256_bytes(patch_content) if emit_mapping_patch else ""
    reconciliation = {
        "literal_total": literal_total,
        "channel_rows_excluding_ignored": actionable,
        "assigned_target_total": assigned,
        "real": real,
        "native": native,
        "synthetic": synthetic,
        "ignored": ignored,
        "quarantined": quarantined,
        "uncovered": uncovered,
        "literal_assignment_percent": _percent(assigned, literal_total),
        "channel_assignment_percent": _percent(assigned, actionable),
        "uncovered_percent_of_channel_rows": _percent(uncovered, actionable),
        "literal_assignment_target_percent": "90.00",
        "meets_literal_assignment_target": assigned * 100 >= literal_total * 90,
        "actual_programme_coverage": {
            "status": "NOT_MEASURED",
            "authoritative_source": "build_epg_streaming.guideCoverage",
        },
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "private_artifact": True,
        "run_id": run_id,
        "generated_at": validated.validated_as_of,
        "policy": POLICY,
        "policy_sha256": POLICY_SHA256,
        "shadow": {
            "run_id": validated.run_id,
            "generated_at": validated.generated_at,
            "expires_at": validated.expires_at,
            "proposal_count": validated.proposal_count,
            "mappings_checked": validated.mappings_checked,
            "alerts_checked": validated.alerts_checked,
        },
        "source_hashes": dict(sorted(source_hashes.items())),
        "artifacts": {
            "decision_ledger": {
                "filename": "decision_ledger.jsonl",
                "records": len(records),
                "sha256": ledger_sha256,
            },
            "mapping_patch": (
                {
                    "filename": "mapping_patch.jsonl",
                    "records": len(patches),
                    "sha256": patch_sha256,
                }
                if emit_mapping_patch
                else None
            ),
        },
        "decision_counts": dict(sorted(decision_counts.items())),
        "quarantine_counts": dict(sorted(quarantine_counts.items())),
        "upgrade_candidate_rows": sum(
            bool(record["upgrade_candidate"]) for record in records
        ),
        "reconciliation": reconciliation,
    }
    manifest_content = canonical_json_bytes(manifest) + b"\n"
    manifest_sha256 = sha256_bytes(manifest_content)

    target = _prepare_output_dir(Path(output_dir))
    ledger_path = target / "decision_ledger.jsonl"
    manifest_path = target / "manifest.json"
    patch_path = target / "mapping_patch.jsonl" if emit_mapping_patch else None
    _atomic_private_write(ledger_path, ledger_content)
    if patch_path is not None:
        _atomic_private_write(patch_path, patch_content)
    _atomic_private_write(manifest_path, manifest_content)

    return DecisionBaseResult(
        output_dir=target.resolve(),
        run_id=run_id,
        ledger_path=ledger_path.resolve(),
        manifest_path=manifest_path.resolve(),
        patch_path=patch_path.resolve() if patch_path is not None else None,
        record_count=len(records),
        patch_count=len(patches),
        ledger_sha256=ledger_sha256,
        manifest_sha256=manifest_sha256,
        patch_sha256=patch_sha256,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a private autonomous EPG decision ledger without external writes."
    )
    parser.add_argument("--mappings-csv", type=Path, required=True)
    parser.add_argument("--alerts-csv", type=Path, required=True)
    parser.add_argument("--shadow-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--emit-mapping-patch",
        action="store_true",
        help="Also emit a private JSONL patch; no patch is applied by this command.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_decision_base(
        mappings_csv=args.mappings_csv,
        alerts_csv=args.alerts_csv,
        shadow_bundle=args.shadow_bundle,
        output_dir=args.output_dir,
        as_of=args.as_of,
        emit_mapping_patch=args.emit_mapping_patch,
    )
    print(
        "Autonomous decision base complete: "
        f"records={result.record_count:,}; patches={result.patch_count:,}; "
        f"run_id={result.run_id}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"Autonomous decision base failed closed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


__all__ = (
    "DecisionBaseResult",
    "MANIFEST_SCHEMA",
    "POLICY_SHA256",
    "POLICY_VERSION",
    "build_decision_base",
    "main",
    "parse_args",
)
