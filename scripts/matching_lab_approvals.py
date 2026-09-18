#!/usr/bin/env python3
"""Create and validate private human approvals for Matching Lab proposals.

This module is intentionally outside :mod:`matching_lab`: shadow-run manifests
bind the Matching Lab package code hash, while this downstream approval format
must be able to approve an already validated run without changing that hash.

The command has no Google Sheets client and exposes no write/apply operation.
It only validates an exact private bundle against current Mapping and Sync
Alerts snapshots, then writes a content-addressed JSON approval artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from matching_lab.artifacts import (
    MAX_PROPOSAL_BYTES,
    read_stable_regular_file,
    strict_json_loads,
)
from matching_lab.models import (
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    canonical_json_bytes,
    require_sha256,
    safe_text,
    sha256_bytes,
    sha256_json,
)
from matching_lab.validation import ValidationResult, validate_bundle


APPROVAL_SCHEMA = "skytv.smart-match-human-approval.v1"
APPROVAL_ID_SCHEMA = "skytv.smart-match-human-approval-id.v1"
APPROVAL_MODE = "human_approval"
STRONG_REASON_CODE = "MULTI_SIGNAL_STRONG_PROPOSAL"
APPROVAL_DECISION = "APPROVED"
APPROVAL_SCOPE = "ALL_MATCHING_PROPOSALS"

MAX_APPROVAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_APPROVED_PROPOSALS = 30_000
MAX_REVIEWER_NOTES = 2_000

_DOCUMENT_FIELDS = frozenset(
    {
        "schema",
        "approval_id",
        "content_sha256",
        "mode",
        "private_artifact",
        "approved_at",
        "reviewer_notes",
        "approval_rule",
        "run",
        "snapshots",
        "proposal_count",
        "proposals",
    }
)
_RULE_FIELDS = frozenset({"decision", "reason_code", "scope"})
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "generated_at",
        "expires_at",
        "manifest_sha256",
        "proposals_sha256",
        "policy_sha256",
        "lab_code_sha256",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "mappings_file_sha256",
        "mappings_table_sha256",
        "alerts_file_sha256",
        "open_alert_keys_sha256",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {
        "proposal_id",
        "server_id",
        "stream_id",
        "row_guard_sha256",
        "provider_identity_sha256",
        "decision_state",
        "selected_candidate_key",
        "selected_epg_id",
        "score_ppm",
        "margin_ppm",
    }
)
_REQUIRED_INPUT_HASHES = frozenset(
    {"MAPPING_FILE", "MAPPING_TABLE", "ALERTS_FILE", "OPEN_ALERT_KEYS"}
)


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """Summary of one verified private approval artifact."""

    approval_file: Path
    approval_id: str
    content_sha256: str
    run_id: str
    approved_at: str
    proposal_count: int


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    manifest_sha256: str
    proposals_sha256: str
    policy_sha256: str
    lab_code_sha256: str
    mappings_file_sha256: str
    mappings_table_sha256: str
    alerts_file_sha256: str
    open_alert_keys_sha256: str


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object.")
    return dict(value)


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a JSON array.")
    return list(value)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string.")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{label} must be an integer of at least {minimum}.")
    return value


def _sha256(value: object, *, label: str) -> str:
    raw = _string(value, label=label)
    canonical = require_sha256(raw, label=label)
    if raw != canonical:
        raise ContractError(f"{label} must use canonical lowercase hexadecimal.")
    return canonical


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        unknown = sorted(actual.difference(expected))
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ContractError(f"{label} has an invalid field set ({'; '.join(detail)}).")


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


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = str(value or "")
    if text.isdigit():
        return 0, int(text), text
    return 1, text.casefold(), text


def _approval_sort_key(proposal: ProposalRecord) -> tuple[object, ...]:
    return proposal.server_id, _stream_sort_key(proposal.stream_id)


def _selected_candidate(proposal: ProposalRecord) -> Any:
    return next(
        (
            candidate
            for candidate in proposal.candidates
            if candidate.candidate_key == proposal.selected_candidate_key
        ),
        None,
    )


def _select_strong_proposals(
    validation: ValidationResult,
) -> tuple[ProposalRecord, ...]:
    selected = tuple(
        sorted(
            (
                proposal
                for proposal in validation.proposals
                if STRONG_REASON_CODE in proposal.reason_codes
            ),
            key=_approval_sort_key,
        )
    )
    if not selected:
        raise ContractError(
            "The validated run contains no MULTI_SIGNAL_STRONG_PROPOSAL rows."
        )
    if len(selected) > MAX_APPROVED_PROPOSALS:
        raise ContractError("The approval proposal count exceeds its safety limit.")

    proposal_ids: set[str] = set()
    target_keys: set[tuple[str, str]] = set()
    for proposal in selected:
        proposal.verify_id()
        if proposal.run_id != validation.run_id:
            raise ContractError("An approval proposal belongs to another run_id.")
        if proposal.created_at != validation.generated_at:
            raise ContractError("An approval proposal has a mismatched creation time.")
        if proposal.expires_at != validation.expires_at:
            raise ContractError("An approval proposal has a mismatched expiry time.")
        if proposal.proposal_id in proposal_ids:
            raise ContractError("The approval selection contains a duplicate proposal_id.")
        proposal_ids.add(proposal.proposal_id)
        target = (proposal.server_id, proposal.stream_id)
        if target in target_keys:
            raise ContractError("The approval selection contains a duplicate stream target.")
        target_keys.add(target)
        if proposal.state is not DecisionState.NEEDS_REVIEW:
            raise ContractError(
                "A strong human-approval proposal is not in NEEDS_REVIEW state."
            )
        if proposal.auto_apply_eligible:
            raise ContractError("A human-approval proposal unexpectedly requests auto-apply.")
        candidate = _selected_candidate(proposal)
        if candidate is None:
            raise ContractError("A strong approval proposal has no selected candidate.")
        if candidate.conflicts:
            raise ContractError("A strong approval proposal selected a conflicting candidate.")
        if candidate.programme_state is not ProgrammeState.PASS:
            raise ContractError("A strong approval proposal lacks passing programme evidence.")
    return selected


def _canonical_manifest(bundle_dir: Path) -> tuple[dict[str, Any], str]:
    content, digest = read_stable_regular_file(
        Path(bundle_dir) / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES
    )
    raw = strict_json_loads(content)
    manifest = _mapping(raw, label="bundle manifest")
    if content != canonical_json_bytes(manifest) + b"\n":
        raise ContractError("The bundle manifest is not canonical JSON.")
    return manifest, digest


def _bind_current_snapshots(
    validation: ValidationResult,
    *,
    mappings_csv: Path,
    alerts_csv: Path,
    expected_manifest_sha256: str,
) -> _RunSnapshot:
    manifest, manifest_digest = _canonical_manifest(validation.bundle_dir)
    if manifest_digest != expected_manifest_sha256:
        raise ContractError("The bundle manifest changed during approval validation.")
    if _string(manifest.get("run_id"), label="manifest run_id") != validation.run_id:
        raise ContractError("The bundle manifest changed after validation.")
    if manifest.get("generated_at") != validation.generated_at:
        raise ContractError("The bundle generation time changed after validation.")
    if manifest.get("expires_at") != validation.expires_at:
        raise ContractError("The bundle expiry changed after validation.")

    inputs = _mapping(manifest.get("input_sha256"), label="manifest input hashes")
    if not _REQUIRED_INPUT_HASHES.issubset(inputs):
        raise ContractError("The bundle manifest lacks approval snapshot hashes.")
    input_hashes = {
        name: require_sha256(inputs[name], label=f"manifest input {name}")
        for name in _REQUIRED_INPUT_HASHES
    }
    proposals_sha256 = require_sha256(
        manifest.get("proposals_sha256"), label="manifest proposals hash"
    )
    policy_sha256 = require_sha256(
        manifest.get("policy_sha256"), label="manifest policy hash"
    )
    lab_code_sha256 = require_sha256(
        manifest.get("lab_code_sha256"), label="manifest lab-code hash"
    )

    _proposal_content, current_proposals_sha256 = read_stable_regular_file(
        validation.bundle_dir / "proposals.jsonl",
        maximum_bytes=MAX_PROPOSAL_BYTES,
    )
    if current_proposals_sha256 != proposals_sha256:
        raise ContractError("The bundle proposals changed after validation.")
    validated_proposals_hash = hashlib.sha256()
    for proposal in validation.proposals:
        validated_proposals_hash.update(canonical_json_bytes(proposal.public_dict()))
        validated_proposals_hash.update(b"\n")
    if validated_proposals_hash.hexdigest() != proposals_sha256:
        raise ContractError(
            "The validated proposal records differ from the current bundle."
        )

    _mapping_content, mapping_digest = read_stable_regular_file(
        Path(mappings_csv), maximum_bytes=MAX_SNAPSHOT_BYTES
    )
    if mapping_digest != input_hashes["MAPPING_FILE"]:
        raise ContractError("The current Mappings snapshot changed after validation.")
    _alert_content, alert_digest = read_stable_regular_file(
        Path(alerts_csv), maximum_bytes=MAX_SNAPSHOT_BYTES
    )
    if alert_digest != input_hashes["ALERTS_FILE"]:
        raise ContractError("The current Sync Alerts snapshot changed after validation.")

    return _RunSnapshot(
        manifest_sha256=manifest_digest,
        proposals_sha256=proposals_sha256,
        policy_sha256=policy_sha256,
        lab_code_sha256=lab_code_sha256,
        mappings_file_sha256=input_hashes["MAPPING_FILE"],
        mappings_table_sha256=input_hashes["MAPPING_TABLE"],
        alerts_file_sha256=input_hashes["ALERTS_FILE"],
        open_alert_keys_sha256=input_hashes["OPEN_ALERT_KEYS"],
    )


def _proposal_public_dict(proposal: ProposalRecord) -> dict[str, object]:
    candidate = _selected_candidate(proposal)
    if candidate is None:  # Defensive: _select_strong_proposals checked this.
        raise ContractError("A strong approval proposal has no selected candidate.")
    return {
        "proposal_id": proposal.proposal_id,
        "server_id": proposal.server_id,
        "stream_id": proposal.stream_id,
        "row_guard_sha256": proposal.row_guard_sha256,
        "provider_identity_sha256": proposal.provider_identity_sha256,
        "decision_state": proposal.state.value,
        "selected_candidate_key": proposal.selected_candidate_key,
        "selected_epg_id": candidate.epg_id,
        "score_ppm": proposal.score_ppm,
        "margin_ppm": proposal.margin_ppm,
    }


def _approval_payload(
    validation: ValidationResult,
    snapshot: _RunSnapshot,
    proposals: Sequence[ProposalRecord],
    *,
    approved_at: str,
    reviewer_notes: str,
) -> dict[str, object]:
    return {
        "schema": APPROVAL_SCHEMA,
        "mode": APPROVAL_MODE,
        "private_artifact": True,
        "approved_at": approved_at,
        "reviewer_notes": reviewer_notes,
        "approval_rule": {
            "decision": APPROVAL_DECISION,
            "reason_code": STRONG_REASON_CODE,
            "scope": APPROVAL_SCOPE,
        },
        "run": {
            "run_id": validation.run_id,
            "generated_at": validation.generated_at,
            "expires_at": validation.expires_at,
            "manifest_sha256": snapshot.manifest_sha256,
            "proposals_sha256": snapshot.proposals_sha256,
            "policy_sha256": snapshot.policy_sha256,
            "lab_code_sha256": snapshot.lab_code_sha256,
        },
        "snapshots": {
            "mappings_file_sha256": snapshot.mappings_file_sha256,
            "mappings_table_sha256": snapshot.mappings_table_sha256,
            "alerts_file_sha256": snapshot.alerts_file_sha256,
            "open_alert_keys_sha256": snapshot.open_alert_keys_sha256,
        },
        "proposal_count": len(proposals),
        "proposals": [_proposal_public_dict(proposal) for proposal in proposals],
    }


def _approval_document(payload: Mapping[str, object]) -> dict[str, object]:
    content_sha256 = sha256_json(dict(payload))
    run = _mapping(payload.get("run"), label="approval run")
    run_id = _sha256(run.get("run_id"), label="approval run_id")
    approval_id = sha256_json(
        {
            "schema": APPROVAL_ID_SCHEMA,
            "run_id": run_id,
            "content_sha256": content_sha256,
        }
    )
    return {
        "approval_id": approval_id,
        "content_sha256": content_sha256,
        **dict(payload),
    }


def _write_new_private_file(path: Path, content: bytes) -> None:
    target = Path(path)
    parent = target.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ContractError("The approval output directory is unavailable.") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ContractError("The approval output directory must be a real directory.")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ContractError("The approval output target cannot be inspected.") from exc
    else:
        raise ContractError("The approval output file already exists.")

    temporary = target.with_name(target.name + ".part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except FileExistsError as exc:
        raise ContractError("The approval temporary output already exists.") from exc
    except OSError as exc:
        raise ContractError("The approval artifact could not be written safely.") from exc


def approve_all_strong_proposals(
    *,
    bundle_dir: Path,
    mappings_csv: Path,
    alerts_csv: Path,
    output_file: Path,
    approved_at: str,
    reviewer_notes: str = "",
) -> ApprovalResult:
    """Approve every strong proposal in an exact, still-current shadow run.

    ``approved_at`` is mandatory and is also the strict bundle validation time.
    Therefore an approval cannot be created before generation or at/after bundle
    expiry.  Blank ``reviewer_notes`` are explicitly valid.
    """

    approved_time = _parse_utc(approved_at, label="approved_at")
    canonical_approved_at = approved_time.isoformat().replace("+00:00", "Z")
    notes = safe_text(reviewer_notes, maximum=MAX_REVIEWER_NOTES)
    _initial_manifest, initial_manifest_sha256 = _canonical_manifest(
        Path(bundle_dir)
    )
    validation = validate_bundle(
        Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=canonical_approved_at,
    )
    if not validation.mappings_checked or not validation.alerts_checked:
        raise ContractError(
            "Approval requires both current Mappings and Sync Alerts validation."
        )
    proposals = _select_strong_proposals(validation)
    snapshot = _bind_current_snapshots(
        validation,
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        expected_manifest_sha256=initial_manifest_sha256,
    )
    payload = _approval_payload(
        validation,
        snapshot,
        proposals,
        approved_at=canonical_approved_at,
        reviewer_notes=notes,
    )
    document = _approval_document(payload)
    content = canonical_json_bytes(document) + b"\n"
    if len(content) > MAX_APPROVAL_BYTES:
        raise ContractError("The approval artifact exceeds its size limit.")
    _write_new_private_file(Path(output_file), content)
    return ApprovalResult(
        approval_file=Path(output_file).resolve(),
        approval_id=str(document["approval_id"]),
        content_sha256=str(document["content_sha256"]),
        run_id=validation.run_id,
        approved_at=canonical_approved_at,
        proposal_count=len(proposals),
    )


def _parse_approval(path: Path) -> tuple[dict[str, Any], bytes]:
    content, _digest = read_stable_regular_file(
        Path(path), maximum_bytes=MAX_APPROVAL_BYTES
    )
    raw = strict_json_loads(content)
    document = _mapping(raw, label="approval artifact")
    if content != canonical_json_bytes(document) + b"\n":
        raise ContractError(
            "The approval artifact is not canonical JSON with one final newline."
        )
    _exact_fields(document, _DOCUMENT_FIELDS, label="approval artifact")
    if _string(document["schema"], label="approval schema") != APPROVAL_SCHEMA:
        raise ContractError("The approval schema is unsupported.")
    if _string(document["mode"], label="approval mode") != APPROVAL_MODE:
        raise ContractError("The approval mode is unsupported.")
    if document["private_artifact"] is not True:
        raise ContractError("The approval artifact must remain private.")
    _parse_utc(document["approved_at"], label="approval approved_at")
    safe_notes = safe_text(
        _string(document["reviewer_notes"], label="approval reviewer_notes"),
        maximum=MAX_REVIEWER_NOTES,
    )
    if safe_notes != document["reviewer_notes"]:
        raise ContractError("The approval reviewer notes are not normalized.")

    rule = _mapping(document["approval_rule"], label="approval rule")
    _exact_fields(rule, _RULE_FIELDS, label="approval rule")
    if rule != {
        "decision": APPROVAL_DECISION,
        "reason_code": STRONG_REASON_CODE,
        "scope": APPROVAL_SCOPE,
    }:
        raise ContractError("The approval rule is unsupported.")

    run = _mapping(document["run"], label="approval run")
    _exact_fields(run, _RUN_FIELDS, label="approval run")
    for name in (
        "run_id",
        "manifest_sha256",
        "proposals_sha256",
        "policy_sha256",
        "lab_code_sha256",
    ):
        _sha256(run[name], label=f"approval run {name}")
    generated = _parse_utc(run["generated_at"], label="approval generated_at")
    expires = _parse_utc(run["expires_at"], label="approval expires_at")
    approved = _parse_utc(document["approved_at"], label="approval approved_at")
    if not generated <= approved < expires:
        raise ContractError("The approval time falls outside the proposal lifetime.")

    snapshots = _mapping(document["snapshots"], label="approval snapshots")
    _exact_fields(snapshots, _SNAPSHOT_FIELDS, label="approval snapshots")
    for name in _SNAPSHOT_FIELDS:
        _sha256(snapshots[name], label=f"approval snapshot {name}")

    proposals = _list(document["proposals"], label="approval proposals")
    count = _integer(
        document["proposal_count"], label="approval proposal_count", minimum=1
    )
    if count != len(proposals) or count > MAX_APPROVED_PROPOSALS:
        raise ContractError("The approval proposal count is inconsistent.")
    proposal_ids: set[str] = set()
    targets: set[tuple[str, str]] = set()
    order: list[tuple[object, ...]] = []
    for index, raw_proposal in enumerate(proposals, start=1):
        proposal = _mapping(raw_proposal, label=f"approval proposal {index}")
        _exact_fields(
            proposal, _PROPOSAL_FIELDS, label=f"approval proposal {index}"
        )
        proposal_id = _sha256(
            proposal["proposal_id"], label=f"approval proposal {index} ID"
        )
        if proposal_id in proposal_ids:
            raise ContractError("The approval artifact contains a duplicate proposal_id.")
        proposal_ids.add(proposal_id)
        server_id = _string(
            proposal["server_id"], label=f"approval proposal {index} server_id"
        )
        stream_id = _string(
            proposal["stream_id"], label=f"approval proposal {index} stream_id"
        )
        target = (server_id, stream_id)
        if target in targets:
            raise ContractError("The approval artifact contains a duplicate stream target.")
        targets.add(target)
        order.append((server_id, _stream_sort_key(stream_id)))
        for name in ("row_guard_sha256", "provider_identity_sha256"):
            _sha256(
                proposal[name], label=f"approval proposal {index} {name}"
            )
        if (
            _string(
                proposal["decision_state"],
                label=f"approval proposal {index} decision_state",
            )
            != DecisionState.NEEDS_REVIEW.value
        ):
            raise ContractError("An approval entry is not in NEEDS_REVIEW state.")
        for name in ("selected_candidate_key", "selected_epg_id"):
            if not _string(
                proposal[name], label=f"approval proposal {index} {name}"
            ):
                raise ContractError(f"An approval proposal has a blank {name}.")
        for name in ("score_ppm", "margin_ppm"):
            score = _integer(
                proposal[name], label=f"approval proposal {index} {name}"
            )
            if score > 1_000_000:
                raise ContractError(f"An approval proposal has an invalid {name}.")
    if order != sorted(order):
        raise ContractError("Approval proposals are not in deterministic stream order.")

    content_sha256 = _sha256(
        document["content_sha256"], label="approval content hash"
    )
    payload = {
        key: value
        for key, value in document.items()
        if key not in {"approval_id", "content_sha256"}
    }
    if sha256_json(payload) != content_sha256:
        raise ContractError("The approval content hash is invalid.")
    expected_approval_id = sha256_json(
        {
            "schema": APPROVAL_ID_SCHEMA,
            "run_id": run["run_id"],
            "content_sha256": content_sha256,
        }
    )
    if _sha256(
        document["approval_id"], label="approval ID"
    ) != expected_approval_id:
        raise ContractError("The approval ID is invalid.")
    return document, content


def load_approval_document(path: Path) -> dict[str, Any]:
    """Return one strictly parsed canonical approval document.

    This performs local schema, ordering, duplicate, content-hash, and approval-
    ID checks.  Consumers that could prepare or apply a change must additionally
    call :func:`validate_approval` to bind it to the current unexpired bundle,
    Mapping snapshot, and Sync Alerts snapshot.
    """

    document, _content = _parse_approval(Path(path))
    return document


def validate_approval(
    approval_file: Path,
    *,
    bundle_dir: Path,
    mappings_csv: Path,
    alerts_csv: Path,
    as_of: str | None = None,
) -> ApprovalResult:
    """Validate an approval against the exact, current, unexpired run inputs."""

    document, initial_content = _parse_approval(Path(approval_file))
    approved_at = _string(document["approved_at"], label="approval approved_at")
    _initial_manifest, initial_manifest_sha256 = _canonical_manifest(
        Path(bundle_dir)
    )
    validation = validate_bundle(
        Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=as_of,
    )
    if not validation.mappings_checked or not validation.alerts_checked:
        raise ContractError(
            "Approval validation requires current Mappings and Sync Alerts."
        )
    validation_time = _parse_utc(
        validation.validated_as_of, label="bundle validation time"
    )
    if _parse_utc(approved_at, label="approval approved_at") > validation_time:
        raise ContractError("The approval was recorded after the validation time.")
    proposals = _select_strong_proposals(validation)
    snapshot = _bind_current_snapshots(
        validation,
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        expected_manifest_sha256=initial_manifest_sha256,
    )
    expected = _approval_document(
        _approval_payload(
            validation,
            snapshot,
            proposals,
            approved_at=approved_at,
            reviewer_notes=_string(
                document["reviewer_notes"], label="approval reviewer_notes"
            ),
        )
    )
    if document != expected:
        raise ContractError(
            "The approval artifact does not exactly match the current validated proposals."
        )
    terminal_content, _digest = read_stable_regular_file(
        Path(approval_file), maximum_bytes=MAX_APPROVAL_BYTES
    )
    if terminal_content != initial_content:
        raise ContractError("The approval artifact changed during validation.")
    return ApprovalResult(
        approval_file=Path(approval_file).resolve(),
        approval_id=str(document["approval_id"]),
        content_sha256=str(document["content_sha256"]),
        run_id=validation.run_id,
        approved_at=approved_at,
        proposal_count=len(proposals),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or validate a private Matching Lab human-approval package. "
            "This command cannot write to Google Sheets."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    approve = commands.add_parser(
        "approve-strong",
        help="Approve every MULTI_SIGNAL_STRONG_PROPOSAL in one exact run.",
    )
    approve.add_argument("--bundle-dir", type=Path, required=True)
    approve.add_argument("--mappings-csv", type=Path, required=True)
    approve.add_argument("--alerts-csv", type=Path, required=True)
    approve.add_argument("--output-file", type=Path, required=True)
    approve.add_argument(
        "--approved-at",
        required=True,
        help="Canonical whole-second UTC human approval time ending in Z.",
    )
    approve.add_argument(
        "--reviewer-notes",
        default="",
        help="Optional reviewer notes; blank is valid.",
    )

    check = commands.add_parser(
        "validate", help="Validate a private approval against current snapshots."
    )
    check.add_argument("--approval-file", type=Path, required=True)
    check.add_argument("--bundle-dir", type=Path, required=True)
    check.add_argument("--mappings-csv", type=Path, required=True)
    check.add_argument("--alerts-csv", type=Path, required=True)
    check.add_argument(
        "--as-of",
        help="Optional canonical UTC validation time; defaults to current UTC.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "approve-strong":
            result = approve_all_strong_proposals(
                bundle_dir=args.bundle_dir,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                output_file=args.output_file,
                approved_at=args.approved_at,
                reviewer_notes=args.reviewer_notes,
            )
            verb = "created"
        elif args.command == "validate":
            result = validate_approval(
                args.approval_file,
                bundle_dir=args.bundle_dir,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                as_of=args.as_of,
            )
            verb = "valid"
        else:  # pragma: no cover - argparse enforces the command set.
            raise ContractError("Unknown approval command.")
    except ContractError as exc:
        print(f"Matching Lab approval stopped safely: {exc}", file=sys.stderr)
        return 2
    print(
        f"Matching Lab approval {verb}: {result.proposal_count:,} proposals; "
        f"approval {result.approval_id[:12]}..."
    )
    print(f"Private approval file: {result.approval_file}")
    print("No Google Sheets write was performed.")
    return 0


__all__ = (
    "APPROVAL_SCHEMA",
    "ApprovalResult",
    "approve_all_strong_proposals",
    "load_approval_document",
    "validate_approval",
)


if __name__ == "__main__":
    raise SystemExit(main())
