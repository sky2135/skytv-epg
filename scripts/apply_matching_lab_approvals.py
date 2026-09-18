#!/usr/bin/env python3
"""Prepare or apply a bounded Matching Lab human-approval batch.

``prepare`` remains a non-authoritative, offline inspection mode.  ``apply``
is a separate fail-closed lane: it binds the canonical approval to the frozen
run inputs, recomputes current XML/TXT catalogue and programme evidence,
revalidates provider identity twice, and makes adjacent authoritative Mapping
and OPEN-alert reads before one atomic Google ``batchUpdate`` of at most 25
rows.  Every uncertain result is followed by an authoritative full-table read.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
for search_path in (REPOSITORY_ROOT, SCRIPTS_DIR):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

import build_epg_streaming as streaming  # noqa: E402
import matching_lab_approvals as approvals  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from matching_lab.artifacts import (  # noqa: E402
    MAX_PROPOSAL_BYTES,
    read_stable_regular_file,
    strict_json_loads,
)
from matching_lab.compat import automatch, catalog_stream  # noqa: E402
from matching_lab.models import (  # noqa: E402
    ContractError,
    canonical_json_bytes,
    require_sha256,
    sha256_json,
)
from matching_lab.retrieval import (  # noqa: E402
    mapping_row_guard,
    provider_identity_guard,
)


BATCH_SCHEMA = "skytv.smart-match-human-approval-batch.v1"
BATCH_ID_SCHEMA = "skytv.smart-match-human-approval-batch-id.v1"
BATCH_MODE = "prepare_only"
PROVENANCE_MARKER = "human-approved-lab-v1"
APPROVAL_REASON = "Human approved the exact Matching Lab proposal after guarded review."
MAX_APPROVAL_BATCH_ROWS = 25
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_NOTES_LENGTH = 2_000
MAX_REASON_LENGTH = 500
MAX_LIVE_CATALOG_CHECK_SECONDS = 10 * 60
PATCH_COLUMNS = (
    "enabled",
    "action",
    "source",
    "epg_feed",
    "epg_id",
    "reason",
    "notes",
)
SELECTION_ORDER = "score_desc_margin_desc_server_stream_proposal"


@dataclass(frozen=True, slots=True)
class PreparedApprovalRow:
    """One exact Mapping transition selected for a bounded batch."""

    proposal_id: str
    server_id: str
    stream_id: str
    row_number: int
    row_guard_sha256: str
    provider_identity_sha256: str
    desired_row: dict[str, str]


@dataclass(frozen=True, slots=True)
class PreparedApprovalBatch:
    """Pure preparation result; this object is not remote write authority."""

    approval_id: str
    approval_content_sha256: str
    run_id: str
    prepared_at: str
    expires_at: str
    limit: int
    eligible_count: int
    already_committed_count: int
    remaining_count: int
    selected: tuple[PreparedApprovalRow, ...]

    @property
    def selected_count(self) -> int:
        return len(self.selected)


@dataclass(frozen=True, slots=True)
class LiveApplyResult:
    """Authoritatively observed result of one live invocation."""

    approval_id: str
    run_id: str
    selected_count: int
    committed_count: int
    already_committed_count: int
    remaining_count: int
    validated_at: str


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string.")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer.")
    return value


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object.")
    return dict(value)


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a JSON array.")
    return list(value)


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = str(value or "")
    if text.isdigit():
        return 0, int(text), text
    return 1, text.casefold(), text


def _proposal_priority(proposal: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        -_integer(proposal.get("score_ppm"), label="approval score_ppm"),
        -_integer(proposal.get("margin_ppm"), label="approval margin_ppm"),
        _string(proposal.get("server_id"), label="approval server_id"),
        _stream_sort_key(
            _string(proposal.get("stream_id"), label="approval stream_id")
        ),
        _string(proposal.get("proposal_id"), label="approval proposal_id"),
    )


def _parse_enabled(row: Mapping[str, str]) -> bool:
    try:
        return streaming.parse_bool(
            row.get("enabled", ""), default=False, field_name="mapping enabled"
        )
    except streaming.BuildError as exc:
        raise ContractError("A Mapping row has an invalid enabled value.") from exc


def _row_key(row: Mapping[str, str]) -> tuple[str, str]:
    try:
        server_id = streaming.normalize_server_id(row.get("server_id", ""))
    except streaming.BuildError as exc:
        raise ContractError("A Mapping row has an invalid server_id.") from exc
    stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
    if not stream_id:
        raise ContractError("A Mapping row has a blank stream_id.")
    return server_id, stream_id


def _proposal_key(proposal: Mapping[str, Any]) -> tuple[str, str]:
    try:
        server_id = streaming.normalize_server_id(
            _string(proposal.get("server_id"), label="approval server_id")
        )
    except streaming.BuildError as exc:
        raise ContractError("An approval has an invalid server_id.") from exc
    stream_id = streaming.clean_identifier(
        _string(proposal.get("stream_id"), label="approval stream_id"), 120
    )
    if not stream_id:
        raise ContractError("An approval has a blank stream_id.")
    return server_id, stream_id


def _provenance(
    *, approval_id: str, run_id: str, proposal_id: str, approved_at: str
) -> str:
    return (
        f"{PROVENANCE_MARKER} approval_id={approval_id} run_id={run_id} "
        f"proposal_id={proposal_id} approved_at={approved_at}"
    )


def _is_recognized_committed_row(
    row: Mapping[str, str],
    proposal: Mapping[str, Any],
    *,
    approval_id: str,
    run_id: str,
    approved_at: str,
) -> bool:
    """Recognize package provenance; live apply separately proves the full row."""

    action = streaming.clean_text(row.get("action", ""), 40).upper()
    if action != "APPROVED":
        return False
    proposal_id = _string(proposal.get("proposal_id"), label="approval proposal_id")
    selected_epg_id = _string(
        proposal.get("selected_epg_id"), label="approval selected_epg_id"
    )
    notes = str(row.get("notes", ""))
    expected_provenance = _provenance(
        approval_id=approval_id,
        run_id=run_id,
        proposal_id=proposal_id,
        approved_at=approved_at,
    )
    has_exact_provenance = (
        notes == expected_provenance
        or notes.startswith(expected_provenance + "\n")
    ) and notes.count(PROVENANCE_MARKER) == 1
    return (
        _parse_enabled(row)
        and streaming.clean_text(row.get("source", ""), 40).casefold()
        == "epgshare01"
        and streaming.clean_text(row.get("epg_feed", ""), 80).upper()
        == "ALL_SOURCES1"
        and str(row.get("epg_id", "")) == selected_epg_id
        and str(row.get("reason", "")) == APPROVAL_REASON
        and provider_identity_guard(row)
        == require_sha256(
            proposal.get("provider_identity_sha256"),
            label="approval provider identity guard",
        )
        and has_exact_provenance
    )


def _desired_row(
    before: Mapping[str, str],
    proposal: Mapping[str, Any],
    *,
    approval_id: str,
    run_id: str,
    approved_at: str,
) -> dict[str, str]:
    action = streaming.clean_text(before.get("action", ""), 40).upper()
    if action != "REVIEW" or _parse_enabled(before):
        raise ContractError(
            "An uncommitted approval no longer targets a disabled REVIEW row."
        )
    if PROVENANCE_MARKER in str(before.get("notes", "")).casefold():
        raise ContractError(
            "A REVIEW row already contains human-approval provenance."
        )
    selected_epg_id = _string(
        proposal.get("selected_epg_id"), label="approval selected_epg_id"
    )
    if not selected_epg_id:
        raise ContractError("An approval has a blank selected_epg_id.")
    proposal_id = require_sha256(
        proposal.get("proposal_id"), label="approval proposal_id"
    )
    prefix = _provenance(
        approval_id=approval_id,
        run_id=run_id,
        proposal_id=proposal_id,
        approved_at=approved_at,
    )
    prior_notes = str(before.get("notes", ""))
    notes = prefix + ("\n" + prior_notes if prior_notes else "")
    if len(notes) > MAX_NOTES_LENGTH:
        raise ContractError(
            "Human provenance cannot be prepended without truncating existing notes."
        )
    reason = APPROVAL_REASON
    if len(reason) > MAX_REASON_LENGTH:  # pragma: no cover - constant invariant.
        raise ContractError("The human-approval reason exceeds its safety limit.")

    desired = {
        column: str(before.get(column, "")) for column in streaming.SHEET_COLUMNS
    }
    desired.update(
        {
            "enabled": "TRUE",
            "action": "APPROVED",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            # EPG IDs are opaque and must never be Unicode-normalized.
            "epg_id": selected_epg_id,
            "reason": reason,
            "notes": notes,
        }
    )
    changed = {
        column
        for column in streaming.SHEET_COLUMNS
        if str(before.get(column, "")) != desired[column]
    }
    if not changed or not changed.issubset(PATCH_COLUMNS):
        raise ContractError("A human approval attempted to change unsupported columns.")
    return desired


def prepare_approved_rows(
    approval_document: Mapping[str, Any],
    mapping_table: sync.MappingTable,
    *,
    prepared_at: str,
    limit: int = MAX_APPROVAL_BATCH_ROWS,
) -> PreparedApprovalBatch:
    """Purely construct the highest-confidence uncommitted approval prefix.

    The caller must first validate the approval artifact against its bundle and
    current snapshots.  This function independently proves every row guard and
    provider-identity guard before selecting the deterministic score/margin
    prefix.  It performs no file, network, or Google Sheets I/O.
    """

    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_APPROVAL_BATCH_ROWS:
        raise ContractError("The human approval batch limit must be between 1 and 25.")
    approval_id = require_sha256(
        approval_document.get("approval_id"), label="approval ID"
    )
    approval_content_sha256 = require_sha256(
        approval_document.get("content_sha256"), label="approval content hash"
    )
    approved_at = _string(
        approval_document.get("approved_at"), label="approval approved_at"
    )
    run = _mapping(approval_document.get("run"), label="approval run")
    run_id = require_sha256(run.get("run_id"), label="approval run_id")
    expires_at = _string(run.get("expires_at"), label="approval expires_at")
    proposals = _list(
        approval_document.get("proposals"), label="approval proposals"
    )
    proposal_count = _integer(
        approval_document.get("proposal_count"), label="approval proposal_count"
    )
    if proposal_count != len(proposals):
        raise ContractError("The approval proposal count is inconsistent.")
    if tuple(mapping_table.headers) != tuple(streaming.SHEET_COLUMNS):
        raise ContractError("Mappings columns are not in exact Version 1 order.")

    current_by_key: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}
    for row_number, row in zip(mapping_table.row_numbers, mapping_table.rows):
        key = _row_key(row)
        if key in current_by_key:
            raise ContractError("Mappings contains a duplicate stream identity.")
        current_by_key[key] = (int(row_number), row)

    eligible: list[tuple[dict[str, Any], int, dict[str, str]]] = []
    committed = 0
    seen_targets: set[tuple[str, str]] = set()
    seen_proposal_ids: set[str] = set()
    for index, raw_proposal in enumerate(proposals, start=1):
        proposal = _mapping(raw_proposal, label=f"approval proposal {index}")
        proposal_id = require_sha256(
            proposal.get("proposal_id"), label=f"approval proposal {index} ID"
        )
        if proposal_id in seen_proposal_ids:
            raise ContractError("The approval contains a duplicate proposal_id.")
        seen_proposal_ids.add(proposal_id)
        key = _proposal_key(proposal)
        if key in seen_targets:
            raise ContractError("The approval contains a duplicate stream target.")
        seen_targets.add(key)
        current = current_by_key.get(key)
        if current is None:
            raise ContractError("An approved stream identity is missing from Mappings.")
        row_number, before = current

        if _is_recognized_committed_row(
            before,
            proposal,
            approval_id=approval_id,
            run_id=run_id,
            approved_at=approved_at,
        ):
            committed += 1
            continue
        if streaming.clean_text(before.get("action", ""), 40).upper() == "APPROVED":
            raise ContractError(
                "An approved Mapping row does not have exact package provenance."
            )
        if mapping_row_guard(before) != require_sha256(
            proposal.get("row_guard_sha256"),
            label=f"approval proposal {index} row guard",
        ):
            raise ContractError("A Mapping row changed after the approved proposal.")
        if provider_identity_guard(before) != require_sha256(
            proposal.get("provider_identity_sha256"),
            label=f"approval proposal {index} provider guard",
        ):
            raise ContractError(
                "A provider identity changed after the approved proposal."
            )
        if _string(
            proposal.get("decision_state"),
            label=f"approval proposal {index} decision_state",
        ) != "NEEDS_REVIEW":
            raise ContractError("An approval proposal is not in NEEDS_REVIEW state.")
        score = _integer(
            proposal.get("score_ppm"), label=f"approval proposal {index} score_ppm"
        )
        margin = _integer(
            proposal.get("margin_ppm"),
            label=f"approval proposal {index} margin_ppm",
        )
        if not 0 <= score <= 1_000_000 or not 0 <= margin <= 1_000_000:
            raise ContractError("An approval proposal has an invalid score or margin.")
        eligible.append((proposal, row_number, before))

    eligible.sort(key=lambda item: _proposal_priority(item[0]))
    selected: list[PreparedApprovalRow] = []
    for proposal, row_number, before in eligible[: int(limit)]:
        server_id, stream_id = _proposal_key(proposal)
        desired = _desired_row(
            before,
            proposal,
            approval_id=approval_id,
            run_id=run_id,
            approved_at=approved_at,
        )
        selected.append(
            PreparedApprovalRow(
                proposal_id=str(proposal["proposal_id"]),
                server_id=server_id,
                stream_id=stream_id,
                row_number=row_number,
                row_guard_sha256=str(proposal["row_guard_sha256"]),
                provider_identity_sha256=str(
                    proposal["provider_identity_sha256"]
                ),
                desired_row=desired,
            )
        )

    return PreparedApprovalBatch(
        approval_id=approval_id,
        approval_content_sha256=approval_content_sha256,
        run_id=run_id,
        prepared_at=prepared_at,
        expires_at=expires_at,
        limit=int(limit),
        eligible_count=len(eligible),
        already_committed_count=committed,
        remaining_count=len(eligible) - len(selected),
        selected=tuple(selected),
    )


def _read_exact_snapshot(path: Path, expected_sha256: object, *, label: str) -> bytes:
    content, digest = read_stable_regular_file(
        Path(path), maximum_bytes=sync.MAX_SHEET_BYTES
    )
    if digest != require_sha256(expected_sha256, label=f"{label} hash"):
        raise ContractError(f"The exact {label} snapshot is no longer current.")
    return content


def _verify_bundle_files(bundle_dir: Path, run: Mapping[str, Any]) -> None:
    _manifest, manifest_digest = read_stable_regular_file(
        Path(bundle_dir) / "manifest.json", maximum_bytes=1024 * 1024
    )
    if manifest_digest != require_sha256(
        run.get("manifest_sha256"), label="approval manifest hash"
    ):
        raise ContractError("The Matching Lab manifest changed after approval.")
    _proposals, proposals_digest = read_stable_regular_file(
        Path(bundle_dir) / "proposals.jsonl", maximum_bytes=MAX_PROPOSAL_BYTES
    )
    if proposals_digest != require_sha256(
        run.get("proposals_sha256"), label="approval proposals hash"
    ):
        raise ContractError("The Matching Lab proposals changed after approval.")


def _parse_canonical_utc(value: object, *, label: str) -> datetime:
    text = _string(value, label=label)
    if not text.endswith("Z"):
        raise ContractError(f"{label} must be canonical UTC ending in Z.")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is not an ISO-8601 timestamp.") from exc
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if parsed.microsecond or text != canonical:
        raise ContractError(f"{label} must use canonical whole-second UTC form.")
    return parsed


def _bundle_manifest(bundle_dir: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    content, digest = read_stable_regular_file(
        Path(bundle_dir) / "manifest.json", maximum_bytes=1024 * 1024
    )
    if digest != require_sha256(
        run.get("manifest_sha256"), label="approval manifest hash"
    ):
        raise ContractError("The Matching Lab manifest changed after approval.")
    raw = strict_json_loads(content)
    manifest = _mapping(raw, label="bundle manifest")
    if canonical_json_bytes(manifest) + b"\n" != content:
        raise ContractError("The Matching Lab manifest is not canonical JSON.")
    if require_sha256(manifest.get("run_id"), label="manifest run_id") != require_sha256(
        run.get("run_id"), label="approval run_id"
    ):
        raise ContractError("The Matching Lab manifest belongs to another run.")
    return manifest


def _selected_epg_ids(batch: PreparedApprovalBatch) -> tuple[str, ...]:
    return tuple(
        sorted(
            {str(item.desired_row["epg_id"]) for item in batch.selected},
            key=lambda value: (value.casefold(), value),
        )
    )


def validate_current_catalogue(
    batch: PreparedApprovalBatch,
    approval_document: Mapping[str, Any],
    *,
    bundle_dir: Path,
    all_source_file: Path,
    all_source_catalog_file: Path,
    as_of: str,
) -> None:
    """Recompute exact catalogue membership and programme gates for this batch."""

    if not batch.selected:
        return
    checked_at = _parse_canonical_utc(as_of, label="catalogue validation time")
    run = _mapping(approval_document.get("run"), label="approval run")
    manifest = _bundle_manifest(Path(bundle_dir), run)
    input_hashes = _mapping(
        manifest.get("input_sha256"), label="bundle input hashes"
    )
    required_hashes = (
        "EPG_XML",
        "EPG_XML_CATALOG",
        "EPG_TEXT",
        "EPG_TEXT_CATALOG",
    )
    if any(name not in input_hashes for name in required_hashes):
        raise ContractError("The Matching Lab manifest lacks catalogue input hashes.")
    expected_hashes = {
        name: require_sha256(input_hashes[name], label=f"manifest {name} hash")
        for name in required_hashes
    }

    text_content, text_sha256 = read_stable_regular_file(
        Path(all_source_catalog_file),
        maximum_bytes=catalog_stream.MAX_CATALOG_TEXT_BYTES,
    )
    if text_sha256 != expected_hashes["EPG_TEXT"]:
        raise ContractError("The current ALL_SOURCES1 text file differs from the approved run.")
    try:
        text_catalog = catalog_stream.parse_all_sources_text(text_content)
        text_catalog.validate_for_unattended_matching()
        automatch._validate_text_catalog_generation(
            text_catalog.generated_token, now_epoch=int(checked_at.timestamp())
        )
    except (catalog_stream.CatalogStreamError, automatch.AutoMatchError) as exc:
        raise ContractError(str(exc)) from exc
    if text_catalog.fingerprint_sha256 != expected_hashes["EPG_TEXT_CATALOG"]:
        raise ContractError("The current text catalogue differs from the approved run.")

    wanted_ids = _selected_epg_ids(batch)
    text_by_id = {entry.epg_id: entry for entry in text_catalog.entries}
    for epg_id in wanted_ids:
        entry = text_by_id.get(epg_id)
        if entry is None or entry.kind != "real":
            raise ContractError("An approved EPG ID is not an exact current real text-catalogue ID.")
        if epg_id.casefold() in text_catalog.casefold_collision_keys:
            raise ContractError("An approved EPG ID has a current text-catalogue case collision.")

    try:
        one_pass = catalog_stream.stream_catalog_and_programmes_once(
            path=Path(all_source_file),
            fixed_wanted_ids=wanted_ids,
            select_provisional_ids=lambda _catalog: (),
            window_start=int(checked_at.timestamp()) - 86_400,
            now_epoch=int(checked_at.timestamp()),
        )
    except catalog_stream.CatalogStreamError as exc:
        raise ContractError(str(exc)) from exc
    if one_pass.source_sha256 != expected_hashes["EPG_XML"]:
        raise ContractError("The current ALL_SOURCES1 XML differs from the approved run.")
    if one_pass.catalog.fingerprint_sha256 != expected_hashes["EPG_XML_CATALOG"]:
        raise ContractError("The current XML catalogue differs from the approved run.")
    if one_pass.unresolved_requested_ids:
        raise ContractError("An approved EPG ID is absent from the current XML catalogue.")
    for epg_id in wanted_ids:
        channel = one_pass.catalog.exact(epg_id)
        if channel is None or one_pass.resolved_source_ids.get(epg_id) != epg_id:
            raise ContractError("An approved EPG ID is not an exact current XML catalogue ID.")
        if epg_id.casefold() in one_pass.catalog.casefold_collision_keys:
            raise ContractError("An approved EPG ID has a current XML catalogue case collision.")
        gate = one_pass.programme_gates.get(epg_id)
        if gate is None or not gate.passed:
            raise ContractError(
                "An approved EPG ID no longer passes the current programme gate."
            )
    elapsed = int(datetime.now(timezone.utc).timestamp()) - int(checked_at.timestamp())
    if elapsed < 0 or elapsed > MAX_LIVE_CATALOG_CHECK_SECONDS:
        raise ContractError("The live catalogue check took too long to remain current.")


def _alert_keys_from_live_values(values: Sequence[Sequence[Any]]) -> set[tuple[str, str]]:
    try:
        return sync.open_alert_quarantine_keys(sync.parse_sync_alert_values(values))
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc


def _google_mapping_and_alert_values(
    session: Any,
    sheet_id: str,
    mapping_tab: str,
    alerts_tab: str,
) -> tuple[list[list[Any]], list[list[Any]]]:
    """Read both safety-critical tabs in one final pre-write HTTP response."""

    mapping_range = unquote(
        sync.quoted_a1(
            mapping_tab, f"A1:AG{sync.MAX_GOOGLE_MAPPING_ROWS + 2}"
        )
    )
    alerts_range = unquote(
        sync.quoted_a1(alerts_tab, f"A1:K{sync.MAX_SYNC_ALERT_ROWS + 2}")
    )
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{sync.validate_sheet_id(sheet_id)}/values:batchGet"
    )
    response = None
    try:
        response = session.get(
            url,
            params={
                "ranges": [mapping_range, alerts_range],
                "majorDimension": "ROWS",
                "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
            },
            timeout=(20, 180),
            stream=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise sync.SyncError(
                "Google Sheets could not jointly reread Mappings and Sync Alerts."
            )
        content = sync.response_body_limited(
            response, sync.MAX_SHEET_BYTES + 32 * 1024 * 1024
        )
        payload = json.loads(content.decode("utf-8"))
    except sync.SyncError:
        raise
    except Exception:
        raise sync.SyncError(
            "Google Sheets could not jointly reread Mappings and Sync Alerts."
        ) from None
    finally:
        if response is not None:
            sync.close_response(response)
    value_ranges = payload.get("valueRanges", []) if isinstance(payload, dict) else []
    if not isinstance(value_ranges, list) or len(value_ranges) != 2:
        raise sync.SyncError(
            "Google Sheets returned an invalid joint Mappings/Sync Alerts response."
        )
    results: list[list[list[Any]]] = []
    expected_tabs = (mapping_tab, alerts_tab)
    for index, value_range in enumerate(value_ranges):
        if not isinstance(value_range, Mapping):
            raise sync.SyncError(
                "Google Sheets returned an invalid joint Mappings/Sync Alerts response."
            )
        returned_range = str(value_range.get("range", ""))
        returned_tab = returned_range.split("!", 1)[0]
        if (
            len(returned_tab) >= 2
            and returned_tab.startswith("'")
            and returned_tab.endswith("'")
        ):
            returned_tab = returned_tab[1:-1].replace("''", "'")
        if returned_tab != expected_tabs[index]:
            raise sync.SyncError(
                "Google Sheets returned joint tab values in an unexpected range."
            )
        values = value_range.get("values", [])
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise sync.SyncError(
                "Google Sheets returned invalid joint tab values."
            )
        results.append(values)
    return results[0], results[1]


def _require_no_open_alerts(
    values: Sequence[Sequence[Any]], batch: PreparedApprovalBatch
) -> None:
    targets = {(item.server_id, item.stream_id) for item in batch.selected}
    conflicts = sorted(targets.intersection(_alert_keys_from_live_values(values)))
    if conflicts:
        raise ContractError("A selected approval now has an OPEN Sync Alert.")


def _fetch_provider_inventories(
    server_configs: Sequence[sync.ServerConfig], *, allow_insecure_http: bool
) -> tuple[sync.PanelInventory, ...]:
    session = sync.requests.Session()
    session.headers.update(
        {
            "User-Agent": f"SKYTV-Matching-Lab-Approval/{sync.SYNC_VERSION}",
            "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
        }
    )
    inventories: list[sync.PanelInventory] = []
    try:
        for config in server_configs:
            inventories.append(
                sync.fetch_panel_inventory(
                    session,
                    config,
                    allow_insecure_http=bool(allow_insecure_http),
                )
            )
    finally:
        session.close()
    return tuple(inventories)


def _require_provider_identity_matches(
    batch: PreparedApprovalBatch,
    inventories: Sequence[sync.PanelInventory],
) -> None:
    current: dict[tuple[str, str], Mapping[str, Any]] = {}
    for inventory in inventories:
        server_id = streaming.normalize_server_id(inventory.server_id)
        for channel in inventory.channels:
            key = (
                server_id,
                streaming.clean_identifier(channel.get("stream_id", ""), 120),
            )
            if key in current:
                raise ContractError("A provider returned a duplicate stream identity.")
            current[key] = channel
    for item in batch.selected:
        row = item.desired_row
        channel = current.get((item.server_id, item.stream_id))
        if channel is None:
            raise ContractError("A selected approval is missing from the live provider.")
        row_category_id = streaming.clean_identifier(row.get("category_id", ""), 120)
        provider_category_id = streaming.clean_identifier(
            channel.get("category_id", ""), 120
        )
        if (
            sync.exact_inventory_name_key(row.get("channel_name", ""))
            != sync.exact_inventory_name_key(channel.get("name", ""))
            or sync.comparison_name_key(row.get("category_name", ""))
            != sync.comparison_name_key(channel.get("category_name", ""))
            or row_category_id != provider_category_id
        ):
            raise ContractError("A selected approval has changed provider identity.")


def _batch_signature(batch: PreparedApprovalBatch) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.proposal_id,
            item.server_id,
            item.stream_id,
            item.row_number,
            item.row_guard_sha256,
            item.provider_identity_sha256,
            tuple((column, item.desired_row[column]) for column in streaming.SHEET_COLUMNS),
        )
        for item in batch.selected
    )


def _require_committed_rows_match_approved_transition(
    approval_document: Mapping[str, Any],
    original_table: sync.MappingTable,
    live_table: sync.MappingTable,
) -> None:
    """Allow only exact original rows or exact commits from this approval."""

    if (
        tuple(original_table.headers) != tuple(live_table.headers)
        or tuple(original_table.row_numbers) != tuple(live_table.row_numbers)
        or len(original_table.rows) != len(live_table.rows)
    ):
        raise ContractError(
            "The live Mapping table structure differs from the approved snapshot."
        )
    approval_id = require_sha256(
        approval_document.get("approval_id"), label="approval ID"
    )
    approved_at = _string(
        approval_document.get("approved_at"), label="approval approved_at"
    )
    run = _mapping(approval_document.get("run"), label="approval run")
    run_id = require_sha256(run.get("run_id"), label="approval run_id")
    proposals_by_key: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for index, raw in enumerate(
        _list(approval_document.get("proposals"), label="approval proposals"),
        start=1,
    ):
        proposal = _mapping(raw, label=f"approval proposal {index}")
        key = _proposal_key(proposal)
        if key in proposals_by_key:
            raise ContractError("The approval contains a duplicate stream target.")
        proposals_by_key[key] = (index, proposal)

    for original, live in zip(original_table.rows, live_table.rows):
        original_key = _row_key(original)
        if _row_key(live) != original_key:
            raise ContractError(
                "The live Mapping row order differs from the approved snapshot."
            )
        if all(
            str(live.get(column, "")) == str(original.get(column, ""))
            for column in streaming.SHEET_COLUMNS
        ):
            continue
        proposal_entry = proposals_by_key.get(original_key)
        if proposal_entry is None:
            raise ContractError(
                "An unrelated Mapping row changed after the approval snapshot."
            )
        index, proposal = proposal_entry
        if streaming.clean_text(live.get("action", ""), 40).upper() != "APPROVED":
            raise ContractError(
                "An approved Mapping target drifted without an exact commit."
            )
        if mapping_row_guard(original) != require_sha256(
            proposal.get("row_guard_sha256"),
            label=f"approval proposal {index} row guard",
        ):
            raise ContractError("The frozen Mapping snapshot does not match an approval guard.")
        expected = _desired_row(
            original,
            proposal,
            approval_id=approval_id,
            run_id=run_id,
            approved_at=approved_at,
        )
        if any(
            str(live.get(column, "")) != expected[column]
            for column in streaming.SHEET_COLUMNS
        ):
            raise ContractError(
                "A previously committed approval no longer matches its exact transition."
            )


def _ensure_live_time_is_valid(approval_document: Mapping[str, Any]) -> str:
    now = _canonical_now()
    current = _parse_canonical_utc(now, label="live validation time")
    approved = _parse_canonical_utc(
        approval_document.get("approved_at"), label="approval approved_at"
    )
    run = _mapping(approval_document.get("run"), label="approval run")
    generated = _parse_canonical_utc(run.get("generated_at"), label="approval generated_at")
    expires = _parse_canonical_utc(run.get("expires_at"), label="approval expires_at")
    if not generated <= approved <= current < expires:
        raise ContractError("The approval run is not live at the actual write time.")
    return now


def prepare_approval_batch(
    *,
    approval_file: Path,
    bundle_dir: Path,
    mappings_csv: Path,
    alerts_csv: Path,
    as_of: str,
    limit: int = MAX_APPROVAL_BATCH_ROWS,
) -> tuple[PreparedApprovalBatch, dict[str, Any]]:
    """Validate exact files on both sides of pure batch preparation."""

    initial_result = approvals.validate_approval(
        Path(approval_file),
        bundle_dir=Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=as_of,
    )
    approval_document = approvals.load_approval_document(Path(approval_file))
    if (
        approval_document.get("approval_id") != initial_result.approval_id
        or approval_document.get("content_sha256") != initial_result.content_sha256
        or approval_document.get("proposal_count") != initial_result.proposal_count
    ):
        raise ContractError("The approval artifact changed after validation.")
    run = _mapping(approval_document.get("run"), label="approval run")
    snapshots = _mapping(
        approval_document.get("snapshots"), label="approval snapshots"
    )
    _verify_bundle_files(Path(bundle_dir), run)
    mapping_content = _read_exact_snapshot(
        Path(mappings_csv),
        snapshots.get("mappings_file_sha256"),
        label="Mappings",
    )
    _read_exact_snapshot(
        Path(alerts_csv), snapshots.get("alerts_file_sha256"), label="Sync Alerts"
    )
    try:
        mapping_table = sync.parse_mapping_csv(mapping_content)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    batch = prepare_approved_rows(
        approval_document, mapping_table, prepared_at=as_of, limit=limit
    )

    terminal_result = approvals.validate_approval(
        Path(approval_file),
        bundle_dir=Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=as_of,
    )
    if terminal_result != initial_result:
        raise ContractError("The approval validation result changed during preparation.")
    terminal_document = approvals.load_approval_document(Path(approval_file))
    if terminal_document != approval_document:
        raise ContractError("The approval artifact changed during preparation.")
    _verify_bundle_files(Path(bundle_dir), run)
    _read_exact_snapshot(
        Path(mappings_csv),
        snapshots.get("mappings_file_sha256"),
        label="Mappings",
    )
    _read_exact_snapshot(
        Path(alerts_csv), snapshots.get("alerts_file_sha256"), label="Sync Alerts"
    )
    return batch, approval_document


def _validate_approval_exact(
    *,
    approval_file: Path,
    bundle_dir: Path,
    mappings_csv: Path,
    alerts_csv: Path,
    as_of: str,
    expected_document: Mapping[str, Any] | None = None,
) -> tuple[approvals.ApprovalResult, dict[str, Any]]:
    result = approvals.validate_approval(
        Path(approval_file),
        bundle_dir=Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=as_of,
    )
    document = approvals.load_approval_document(Path(approval_file))
    if (
        document.get("approval_id") != result.approval_id
        or document.get("content_sha256") != result.content_sha256
        or document.get("proposal_count") != result.proposal_count
    ):
        raise ContractError("The approval artifact changed after validation.")
    if expected_document is not None and document != dict(expected_document):
        raise ContractError("The approval artifact changed during live apply.")
    run = _mapping(document.get("run"), label="approval run")
    _verify_bundle_files(Path(bundle_dir), run)
    return result, document


def _post_atomic_human_batch(
    session: Any,
    *,
    sheet_id: str,
    tab_name: str,
    before_table: sync.MappingTable,
    batch: PreparedApprovalBatch,
    numeric_sheet_id: int,
) -> tuple[int, sync.MappingTable]:
    """Send one atomic update and authoritatively reconcile every outcome."""

    if not 1 <= batch.selected_count <= MAX_APPROVAL_BATCH_ROWS:
        raise ContractError("A live human-approval batch must contain 1 to 25 rows.")
    desired = {
        (item.server_id, item.stream_id): item.desired_row for item in batch.selected
    }
    if len(desired) != batch.selected_count:
        raise ContractError("The live human-approval batch contains duplicate targets.")
    requests_body: list[dict[str, Any]] = []
    for item in batch.selected:
        requests_body.extend(
            sync._review_update_requests(
                numeric_sheet_id=int(numeric_sheet_id),
                row_number=item.row_number,
                row=item.desired_row,
            )
        )
    body = {"requests": requests_body}
    encoded_body = json.dumps(
        body, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded_body) > sync.MAX_RECHECK_UPDATE_REQUEST_BYTES:
        raise ContractError("The live human-approval request exceeds its size limit.")

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    response = None
    uncertain = False
    rejection_message = ""
    try:
        response = session.post(
            url,
            data=encoded_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=(20, 180),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        response_content = sync.response_body_limited(response, 2 * 1024 * 1024)
        if status not in {200, 201}:
            uncertain = True
            rejection_message = sync.google_write_rejection_message(
                "Mappings human approval",
                status=status,
                response_content=response_content,
                operation="atomic update",
            )
        else:
            try:
                payload = json.loads((response_content or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if not (
                isinstance(payload, dict)
                and str(payload.get("spreadsheetId", "")) == sheet_id
                and isinstance(payload.get("replies"), list)
                and len(payload["replies"]) == len(requests_body)
                and all(isinstance(reply, dict) for reply in payload["replies"])
            ):
                uncertain = True
    except Exception:
        uncertain = True
    finally:
        if response is not None:
            sync.close_response(response)

    observed: sync.MappingTable | None = None
    try:
        observed = sync.parse_table_values(
            sync.google_sheet_values(session, sync.validate_sheet_id(sheet_id), tab_name),
            maximum_rows=sync.MAX_GOOGLE_MAPPING_ROWS,
        )
        sync._verify_review_update_result(before_table, observed, desired)
    except sync.SyncError as exc:
        committed = 0
        if observed is not None and sync._review_update_targets_match(
            before_table, observed, desired
        ):
            committed = batch.selected_count
        if uncertain:
            raise sync.SheetWriteError(
                rejection_message
                or (
                    "Google Sheets returned an uncertain human-approval result and "
                    "the authoritative re-read did not confirm the full transition."
                ),
                committed,
            ) from None
        raise sync.SheetWriteError(str(exc), committed) from None
    return batch.selected_count, observed


def apply_live_approval_batch(
    *,
    approval_file: Path,
    bundle_dir: Path,
    mappings_csv: Path,
    alerts_csv: Path,
    all_source_file: Path,
    all_source_catalog_file: Path,
    google_session: Any,
    sheet_id: str,
    sheet_tab: str,
    alerts_tab: str,
    confirm_approval_id: str,
    limit: int = MAX_APPROVAL_BATCH_ROWS,
    allow_insecure_http: bool = False,
) -> LiveApplyResult:
    """Apply one confirmed, fully revalidated batch to the private Sheet."""

    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_APPROVAL_BATCH_ROWS:
        raise ContractError("The human approval batch limit must be between 1 and 25.")
    confirmation = require_sha256(
        confirm_approval_id, label="confirmed approval ID"
    )
    if str(confirm_approval_id) != confirmation:
        raise ContractError("The confirmed approval ID must be canonical lowercase hex.")
    live_sheet_id = sync.validate_sheet_id(sheet_id)
    started_at = _canonical_now()
    initial_result, approval_document = _validate_approval_exact(
        approval_file=Path(approval_file),
        bundle_dir=Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=started_at,
    )
    if confirmation != initial_result.approval_id:
        raise ContractError("The explicit approval-ID confirmation does not match.")
    snapshots = _mapping(
        approval_document.get("snapshots"), label="approval snapshots"
    )
    original_mapping_content = _read_exact_snapshot(
        Path(mappings_csv),
        snapshots.get("mappings_file_sha256"),
        label="Mappings",
    )
    try:
        original_table = sync.parse_mapping_csv(original_mapping_content)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc

    try:
        initial_table = sync.parse_table_values(
            sync.google_sheet_values(google_session, live_sheet_id, sheet_tab),
            maximum_rows=sync.MAX_GOOGLE_MAPPING_ROWS,
        )
        initial_alert_values = sync.google_sync_alert_values(
            google_session, live_sheet_id, alerts_tab
        )
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    _require_committed_rows_match_approved_transition(
        approval_document, original_table, initial_table
    )
    batch = prepare_approved_rows(
        approval_document,
        initial_table,
        prepared_at=started_at,
        limit=int(limit),
    )
    _require_no_open_alerts(initial_alert_values, batch)
    if not batch.selected:
        terminal_time = _ensure_live_time_is_valid(approval_document)
        _validate_approval_exact(
            approval_file=Path(approval_file),
            bundle_dir=Path(bundle_dir),
            mappings_csv=Path(mappings_csv),
            alerts_csv=Path(alerts_csv),
            as_of=terminal_time,
            expected_document=approval_document,
        )
        return LiveApplyResult(
            approval_id=batch.approval_id,
            run_id=batch.run_id,
            selected_count=0,
            committed_count=0,
            already_committed_count=batch.already_committed_count,
            remaining_count=batch.remaining_count,
            validated_at=terminal_time,
        )

    catalog_time = _canonical_now()
    validate_current_catalogue(
        batch,
        approval_document,
        bundle_dir=Path(bundle_dir),
        all_source_file=Path(all_source_file),
        all_source_catalog_file=Path(all_source_catalog_file),
        as_of=catalog_time,
    )

    selected_servers = tuple(sorted({item.server_id for item in batch.selected}))
    try:
        server_configs = tuple(sync.load_server_configs(selected_servers))
        initial_inventories = _fetch_provider_inventories(
            server_configs, allow_insecure_http=bool(allow_insecure_http)
        )
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    _require_provider_identity_matches(batch, initial_inventories)

    terminal_validation_time = _canonical_now()
    terminal_result, _terminal_document = _validate_approval_exact(
        approval_file=Path(approval_file),
        bundle_dir=Path(bundle_dir),
        mappings_csv=Path(mappings_csv),
        alerts_csv=Path(alerts_csv),
        as_of=terminal_validation_time,
        expected_document=approval_document,
    )
    if terminal_result != initial_result:
        raise ContractError("The approval validation result changed during live apply.")

    desired_rows = tuple(item.desired_row for item in batch.selected)
    try:
        verified_provider_rows, provider_stats = sync.revalidate_provider_identity_updates(
            desired_rows,
            initial_inventories,
            server_configs,
            allow_insecure_http=bool(allow_insecure_http),
        )
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    expected_provider_rows = {
        (item.server_id, item.stream_id): item.desired_row for item in batch.selected
    }
    actual_provider_rows = {
        (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        ): {column: str(row.get(column, "")) for column in streaming.SHEET_COLUMNS}
        for row in verified_provider_rows
    }
    if (
        int(provider_stats.get("provider_batch_revalidation_rejected", 0))
        or int(provider_stats.get("provider_batch_revalidation_unavailable", 0))
        or actual_provider_rows != expected_provider_rows
    ):
        raise ContractError("Fresh provider identity revalidation rejected the batch.")

    current_document = approvals.load_approval_document(Path(approval_file))
    if current_document != approval_document:
        raise ContractError("The approval artifact changed before the adjacent reads.")
    run = _mapping(approval_document.get("run"), label="approval run")
    _verify_bundle_files(Path(bundle_dir), run)
    _ensure_live_time_is_valid(approval_document)

    try:
        layout = sync.google_sheet_layout(
            google_session,
            live_sheet_id,
            sheet_tab,
            column_count=len(streaming.SHEET_COLUMNS),
            expected_used_rows=len(initial_table.rows) + 1,
        )
        sync.google_sheet_layout(
            google_session,
            live_sheet_id,
            alerts_tab,
            column_count=len(sync.ALERT_COLUMNS),
        )
        fresh_mapping_values, adjacent_alert_values = _google_mapping_and_alert_values(
            google_session, live_sheet_id, sheet_tab, alerts_tab
        )
        fresh_table = sync.parse_table_values(
            fresh_mapping_values, maximum_rows=sync.MAX_GOOGLE_MAPPING_ROWS
        )
        if sync.mapping_table_fingerprint(fresh_table) != sync.mapping_table_fingerprint(
            initial_table
        ):
            raise ContractError(
                "The live Mappings tab changed before the approval write."
            )
        _require_committed_rows_match_approved_transition(
            approval_document, original_table, fresh_table
        )
        fresh_batch = prepare_approved_rows(
            approval_document,
            fresh_table,
            prepared_at=started_at,
            limit=int(limit),
        )
        if (
            _batch_signature(fresh_batch) != _batch_signature(batch)
            or fresh_batch.already_committed_count != batch.already_committed_count
            or fresh_batch.remaining_count != batch.remaining_count
        ):
            raise ContractError("The selected approval batch changed before write.")
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    _require_no_open_alerts(adjacent_alert_values, fresh_batch)

    final_time = _ensure_live_time_is_valid(approval_document)
    catalogue_age = int(
        (
            _parse_canonical_utc(final_time, label="final write time")
            - _parse_canonical_utc(catalog_time, label="catalogue validation time")
        ).total_seconds()
    )
    if catalogue_age < 0 or catalogue_age > MAX_LIVE_CATALOG_CHECK_SECONDS:
        raise ContractError(
            "The current catalogue/programme validation is too old to authorize a write."
        )
    committed_count, _observed = _post_atomic_human_batch(
        google_session,
        sheet_id=live_sheet_id,
        tab_name=sheet_tab,
        before_table=fresh_table,
        batch=fresh_batch,
        numeric_sheet_id=layout.numeric_sheet_id,
    )
    return LiveApplyResult(
        approval_id=fresh_batch.approval_id,
        run_id=fresh_batch.run_id,
        selected_count=fresh_batch.selected_count,
        committed_count=committed_count,
        already_committed_count=fresh_batch.already_committed_count + committed_count,
        remaining_count=fresh_batch.remaining_count,
        validated_at=final_time,
    )


def approval_batch_document(
    batch: PreparedApprovalBatch,
    approval_document: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a content-addressed, explicitly non-authoritative batch file."""

    snapshots = _mapping(
        approval_document.get("snapshots"), label="approval snapshots"
    )
    payload: dict[str, Any] = {
        "schema": BATCH_SCHEMA,
        "mode": BATCH_MODE,
        "private_artifact": True,
        "write_authority": False,
        "provider_revalidation_required": True,
        "alert_revalidation_required": True,
        "catalog_revalidation_required": True,
        "programme_revalidation_required": True,
        "approval_id": batch.approval_id,
        "approval_content_sha256": batch.approval_content_sha256,
        "run_id": batch.run_id,
        "prepared_at": batch.prepared_at,
        "expires_at": batch.expires_at,
        "snapshots": snapshots,
        "selection": {
            "order": SELECTION_ORDER,
            "limit": batch.limit,
            "eligible_count": batch.eligible_count,
            "already_committed_count": batch.already_committed_count,
            "selected_count": batch.selected_count,
            "remaining_count": batch.remaining_count,
        },
        "rows": [
            {
                "proposal_id": item.proposal_id,
                "server_id": item.server_id,
                "stream_id": item.stream_id,
                "row_number": item.row_number,
                "row_guard_sha256": item.row_guard_sha256,
                "provider_identity_sha256": item.provider_identity_sha256,
                "desired_row": {
                    column: item.desired_row[column]
                    for column in streaming.SHEET_COLUMNS
                },
            }
            for item in batch.selected
        ],
    }
    content_sha256 = sha256_json(payload)
    batch_id = sha256_json(
        {
            "schema": BATCH_ID_SCHEMA,
            "approval_id": batch.approval_id,
            "content_sha256": content_sha256,
        }
    )
    return {
        "batch_id": batch_id,
        "content_sha256": content_sha256,
        **payload,
    }


def _write_new_private_file(path: Path, document: Mapping[str, Any]) -> None:
    content = canonical_json_bytes(dict(document)) + b"\n"
    if len(content) > MAX_BATCH_BYTES:
        raise ContractError("The prepared approval batch exceeds its size limit.")
    target = Path(path)
    try:
        parent_stat = target.parent.lstat()
    except OSError as exc:
        raise ContractError("The batch output directory is unavailable.") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ContractError("The batch output directory must be a real directory.")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ContractError("The batch output target cannot be inspected.") from exc
    else:
        raise ContractError("The batch output file already exists.")
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
        raise ContractError("The batch temporary output already exists.") from exc
    except OSError as exc:
        raise ContractError("The prepared batch could not be written safely.") from exc


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or safely apply at most 25 exact human-approved Mapping "
            "updates from one canonical Matching Lab approval."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="Validate and write a private, non-authoritative batch."
    )
    prepare.add_argument("--approval-file", type=Path, required=True)
    prepare.add_argument("--bundle-dir", type=Path, required=True)
    prepare.add_argument("--mappings-csv", type=Path, required=True)
    prepare.add_argument("--alerts-csv", type=Path, required=True)
    prepare.add_argument("--output-file", type=Path, required=True)
    prepare.add_argument(
        "--as-of",
        default=None,
        help="Canonical UTC validation time; defaults to the current whole second.",
    )
    prepare.add_argument(
        "--limit", type=int, default=MAX_APPROVAL_BATCH_ROWS, choices=range(1, 26)
    )
    apply = commands.add_parser(
        "apply",
        help=(
            "Revalidate current catalogue, programmes, providers, Mappings and "
            "alerts, then atomically apply one confirmed batch."
        ),
    )
    apply.add_argument("--approval-file", type=Path, required=True)
    apply.add_argument("--bundle-dir", type=Path, required=True)
    apply.add_argument(
        "--mappings-csv",
        type=Path,
        required=True,
        help="Immutable Mappings snapshot bound into the approved run.",
    )
    apply.add_argument(
        "--alerts-csv",
        type=Path,
        required=True,
        help="Immutable Sync Alerts snapshot bound into the approved run.",
    )
    apply.add_argument("--all-source-file", type=Path, required=True)
    apply.add_argument("--all-source-catalog-file", type=Path, required=True)
    apply.add_argument(
        "--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", ""), required=False
    )
    apply.add_argument("--sheet-tab", default="Mappings")
    apply.add_argument(
        "--alerts-tab",
        default=os.environ.get("GOOGLE_SYNC_ALERTS_TAB", "Sync Alerts"),
    )
    apply.add_argument(
        "--confirm-approval-id",
        required=True,
        help="Exact 64-character approval ID; the command never prompts or abbreviates it.",
    )
    apply.add_argument(
        "--limit", type=int, default=MAX_APPROVAL_BATCH_ROWS, choices=range(1, 26)
    )
    apply.add_argument(
        "--allow-insecure-http",
        action="store_true",
        default=str(os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "")).strip().casefold()
        in streaming.TRUE_VALUES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "prepare":
        as_of = args.as_of or _canonical_now()
        try:
            batch, approval_document = prepare_approval_batch(
                approval_file=args.approval_file,
                bundle_dir=args.bundle_dir,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                as_of=as_of,
                limit=args.limit,
            )
            document = approval_batch_document(batch, approval_document)
            _write_new_private_file(args.output_file, document)
        except (ContractError, sync.SyncError) as exc:
            print(f"Matching Lab approval preparation stopped safely: {exc}", file=sys.stderr)
            return 2
        print(
            f"Prepared {batch.selected_count:,} of {batch.eligible_count:,} "
            f"uncommitted approvals; {batch.remaining_count:,} remain."
        )
        print(f"Private prepare-only batch: {Path(args.output_file).resolve()}")
        print("No Google Sheets write was performed.")
        return 0

    google_session = None
    try:
        google_session = sync.authorized_google_session(
            os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        )
        result = apply_live_approval_batch(
            approval_file=args.approval_file,
            bundle_dir=args.bundle_dir,
            mappings_csv=args.mappings_csv,
            alerts_csv=args.alerts_csv,
            all_source_file=args.all_source_file,
            all_source_catalog_file=args.all_source_catalog_file,
            google_session=google_session,
            sheet_id=args.sheet_id,
            sheet_tab=args.sheet_tab,
            alerts_tab=args.alerts_tab,
            confirm_approval_id=args.confirm_approval_id,
            limit=args.limit,
            allow_insecure_http=args.allow_insecure_http,
        )
    except sync.SheetWriteError as exc:
        print(
            "Matching Lab approval apply stopped after authoritative reconciliation: "
            f"{exc} Confirmed committed rows: {exc.appended_count:,}.",
            file=sys.stderr,
        )
        return 3
    except (ContractError, sync.SyncError) as exc:
        print(f"Matching Lab approval apply stopped safely: {exc}", file=sys.stderr)
        return 2
    finally:
        if google_session is not None:
            close = getattr(google_session, "close", None)
            if callable(close):
                close()
    print(
        f"Live approval batch confirmed: {result.committed_count:,} row(s) "
        f"committed; {result.remaining_count:,} remain."
    )
    print(f"Approval ID: {result.approval_id}")
    return 0


__all__ = (
    "BATCH_SCHEMA",
    "MAX_APPROVAL_BATCH_ROWS",
    "PATCH_COLUMNS",
    "PROVENANCE_MARKER",
    "LiveApplyResult",
    "PreparedApprovalBatch",
    "PreparedApprovalRow",
    "apply_live_approval_batch",
    "approval_batch_document",
    "prepare_approval_batch",
    "prepare_approved_rows",
    "validate_current_catalogue",
)


if __name__ == "__main__":
    raise SystemExit(main())
