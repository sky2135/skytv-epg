"""Private, read-only no-schedule classifications for saved REVIEW rows.

This companion lane deliberately reuses the production Smart Rules adapter
instead of teaching the Matching Lab candidate ranker about dummy IDs.  The
normal Lab proposal contract is built around real EPGShare schedules and a
programme gate; a verified dummy classification means the opposite.  Keeping
the artifacts separate preserves both contracts.

The runner has no Google Sheets or provider-network capability.  It reads one
complete saved Mapping snapshot so the production matcher can build its
lineup-level bank signals, corroborates every dummy ID against the paired XML
and TXT catalogs, and records OPEN alerts as blocking evidence.  Every emitted
record has ``write_authority=false`` and ``apply_eligible=false``.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import (
    _atomic_write,
    jsonl_bytes,
    package_code_sha256,
    read_stable_regular_file,
)
from .compat import automatch, catalog_stream, streaming, sync
from .models import (
    ContractError,
    canonical_json_bytes,
    safe_display_text,
    sha256_bytes,
    sha256_json,
)
from .retrieval import mapping_row_guard, provider_identity_guard
from skytv_epg_auto_match_v1 import _has_exact_numbered_event_bank_evidence


DUMMY_CLASSIFICATION_SCHEMA = "skytv.matching-lab-dummy-classification.v1"
DUMMY_MANIFEST_SCHEMA = "skytv.matching-lab-dummy-manifest.v1"
DUMMY_SUMMARY_SCHEMA = "skytv.matching-lab-dummy-summary.v1"
MAX_CLASSIFICATIONS = 30_000
SUPPORTED_SERVERS = ("server_1", "server_2", "server_3")


@dataclass(frozen=True, slots=True)
class DummyShadowResult:
    output_dir: Path
    run_id: str
    classification_count: int
    counts: Mapping[str, int]


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


def _read_alerts(path: Path) -> tuple[list[dict[str, str]], str]:
    content, digest = read_stable_regular_file(
        path, maximum_bytes=sync.MAX_SYNC_ALERT_BYTES
    )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("The Sync Alerts CSV is not UTF-8.") from exc
    try:
        values = list(csv.reader(io.StringIO(text, newline="")))
        return sync.parse_sync_alert_values(values), digest
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc


def _selected_servers(values: Sequence[str]) -> tuple[str, ...]:
    try:
        servers = tuple(
            sorted({streaming.normalize_server_id(value) for value in values})
        )
    except streaming.BuildError as exc:
        raise ContractError("The dummy-shadow server scope is invalid.") from exc
    if not servers or set(servers).difference(SUPPORTED_SERVERS):
        raise ContractError("The dummy-shadow server scope is unsupported.")
    return servers


def _is_disabled_review(row: Mapping[str, Any]) -> bool:
    action = streaming.clean_text(row.get("action", ""), 40).upper()
    try:
        enabled = streaming.parse_bool(
            row.get("enabled", ""), default=False, field_name="enabled"
        )
    except streaming.BuildError as exc:
        raise ContractError("A Mapping row has an invalid enabled value.") from exc
    return action == "REVIEW" and not enabled


def _canonical_display(value: object, maximum: int) -> str:
    return safe_display_text(
        streaming.clean_text(value, int(maximum)), maximum=int(maximum)
    )


def _saved_inventory(
    rows: Sequence[Mapping[str, Any]], server_id: str
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Adapt the complete saved Mapping lineup to the production matcher API."""

    server_rows = [row for row in rows if row.get("server_id") == server_id]
    category_variants: dict[str, set[str]] = {}
    for row in server_rows:
        category_id = streaming.clean_identifier(row.get("category_id", ""), 120)
        category_name = _canonical_display(row.get("category_name", ""), 200)
        category_variants.setdefault(category_id, set()).add(category_name)

    channels: list[dict[str, str]] = []
    categories: dict[str, str] = {}
    for row in server_rows:
        category_id = streaming.clean_identifier(row.get("category_id", ""), 120)
        category_name = _canonical_display(row.get("category_name", ""), 200)
        # A saved Mapping can retain one provider category ID under more than
        # one historical label.  Merging those labels would let one category's
        # bank evidence affect another.  Split only the colliding internal key;
        # rows with the same exact saved label still form one complete bank.
        category_key = category_id
        if len(category_variants.get(category_id, ())) > 1:
            category_key = (
                f"{category_id or 'blank'}#saved-{sha256_json(category_name)}"
            )
        categories[category_key] = category_name
        channels.append(
            {
                "stream_id": streaming.clean_identifier(
                    row.get("stream_id", ""), 120
                ),
                "category_id": category_key,
                "name": _canonical_display(row.get("channel_name", ""), 300),
                # Native panel IDs are intentionally never supplied to this lane.
                "epg_channel_id": "",
            }
        )
    return channels, categories


def _prepare_output_directory(output_dir: Path) -> Path:
    target = Path(output_dir)
    try:
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ContractError(
                    "The dummy-shadow output directory must be a real directory."
                )
            if any(target.iterdir()):
                raise ContractError(
                    "The dummy-shadow output directory must be new and empty."
                )
        else:
            target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            "The dummy-shadow output directory could not be prepared safely."
        ) from exc
    return target


def _classification_record(
    *,
    run_id: str,
    created_at: str,
    row: Mapping[str, Any],
    proposal: Any,
    blocked_alert: bool,
    source_sha256: str,
    xml_catalog_sha256: str,
    text_file_sha256: str,
    text_catalog_sha256: str,
    text_generated: str,
    mapping_table_sha256: str,
    alerts_file_sha256: str,
    open_alerts_sha256: str,
    lab_code_sha256: str,
    runtime: Any,
) -> dict[str, object]:
    reason_codes = {
        "EXACT_XML_TEXT_DUMMY_ID",
        "SHADOW_ONLY_NO_WRITE_AUTHORITY",
        "VERIFIED_DUMMY_CLASSIFICATION",
    }
    state = "CLASSIFIED"
    if blocked_alert:
        state = "BLOCKED_ALERT"
        reason_codes.add("OPEN_SYNC_ALERT")
    unsigned: dict[str, object] = {
        "schema": DUMMY_CLASSIFICATION_SCHEMA,
        "run_id": run_id,
        "created_at": created_at,
        "identity": {
            "server_id": proposal.server_id,
            "stream_id": proposal.stream_id,
            "channel_name": _canonical_display(proposal.channel_name, 300),
            "category_name": _canonical_display(proposal.category_name, 200),
            "row_guard_sha256": mapping_row_guard(row),
            "provider_identity_sha256": provider_identity_guard(row),
        },
        "decision": {
            "state": state,
            "action": "AUTO_DUMMY",
            "source": "dummy",
            "epg_feed": "DUMMY_CHANNELS",
            "epg_id": proposal.target_epg_id,
            "method": proposal.match_method,
            "matcher_reason": proposal.matcher_reason,
            "reason_codes": sorted(reason_codes),
            "apply_eligible": False,
            "write_authority": False,
        },
        "evidence": {
            "source_sha256": source_sha256,
            "xml_catalog_sha256": xml_catalog_sha256,
            "text_file_sha256": text_file_sha256,
            "text_catalog_sha256": text_catalog_sha256,
            "text_generated": text_generated,
            "mapping_table_sha256": mapping_table_sha256,
            "alerts_file_sha256": alerts_file_sha256,
            "open_alerts_sha256": open_alerts_sha256,
            "lab_code_sha256": lab_code_sha256,
            "matcher_version": runtime.identity.version,
            "matcher_build_id": runtime.identity.build_id,
            "matcher_sha256": runtime.identity.source_sha256,
            "matcher_engine_sha256": runtime.identity.engine_source_sha256,
            "approved_aliases_sha256": runtime.approved_aliases_sha256,
            "schedule_equivalences_sha256": (
                runtime.schedule_equivalences_sha256
            ),
        },
    }
    return {
        "classification_id": sha256_json(unsigned),
        **unsigned,
    }


def _write_dummy_bundle(
    output_dir: Path,
    *,
    records: Iterable[Mapping[str, object]],
    summary: Mapping[str, object],
    manifest_base: Mapping[str, object],
) -> None:
    target = _prepare_output_directory(output_dir)
    classification_bytes = jsonl_bytes(records)
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    manifest = {
        **dict(manifest_base),
        "classifications_sha256": sha256_bytes(classification_bytes),
        "summary_sha256": sha256_bytes(summary_bytes),
    }
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    _atomic_write(target / "classifications.jsonl", classification_bytes)
    _atomic_write(target / "summary.json", summary_bytes)
    _atomic_write(target / "manifest.json", manifest_bytes)


def run_dummy_shadow(
    *,
    mappings_csv: Path,
    alerts_csv: Path,
    all_source_file: Path,
    all_source_catalog_file: Path,
    output_dir: Path,
    as_of: str,
    servers: Sequence[str] = SUPPORTED_SERVERS,
    minimum_unique_channels: int = automatch.MINIMUM_CORROBORATED_CATALOG_IDS,
) -> DummyShadowResult:
    """Classify safe no-schedule REVIEW rows without any mutation authority."""

    selected_servers = _selected_servers(servers)
    as_of_dt = _parse_utc(as_of)
    as_of_epoch = int(as_of_dt.timestamp())
    created_at = _utc_text(as_of_dt)
    if isinstance(minimum_unique_channels, bool) or int(minimum_unique_channels) < 1:
        raise ContractError("The dummy-shadow catalog floor is invalid.")

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
    text_bytes, text_file_sha256 = read_stable_regular_file(
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
        if row.get("server_id") in selected_servers and _is_disabled_review(row)
    ]
    if len(target_rows) > MAX_CLASSIFICATIONS:
        raise ContractError("The selected REVIEW backlog exceeds the classification limit.")
    target_by_key = {
        (str(row.get("server_id", "")), str(row.get("stream_id", ""))): row
        for row in target_rows
    }
    if len(target_by_key) != len(target_rows):
        raise ContractError("The selected REVIEW backlog contains duplicate identities.")

    proposal_box: list[dict[tuple[str, str], Any]] = []
    runtime_box: list[Any] = []
    corroboration_box: list[Any] = []
    exact_dummy_ids_box: list[frozenset[str]] = []

    def select_no_programme_ids(source_catalog: Any) -> tuple[str, ...]:
        xml_ids = frozenset(channel.epg_id for channel in source_catalog.channels)
        text_ids = frozenset(entry.epg_id for entry in text_catalog.entries)
        corroboration = automatch._corroborate_catalog_ids(
            xml_ids,
            text_ids,
            minimum_unique_channels=int(minimum_unique_channels),
        )
        real_candidates, dummy_ids = text_catalog.matcher_inputs()
        shared_real = [
            candidate
            for candidate in real_candidates
            if candidate.get("epg_id") in corroboration.exact_ids
            and str(candidate.get("epg_id") or "").casefold()
            not in corroboration.union_casefold_collision_keys
        ]
        shared_dummy = {
            folded: epg_id
            for folded, epg_id in dummy_ids.items()
            if epg_id in corroboration.exact_ids
            and epg_id.casefold()
            not in corroboration.union_casefold_collision_keys
        }
        runtime = automatch.prepare_matcher_runtime(shared_real, shared_dummy)
        matcher_catalog = automatch.MatcherCatalogSnapshot.from_matcher_catalog(
            corroboration.exact_ids,
            real_candidates=shared_real,
            dummy_ids=shared_dummy,
            source_sha256=source_catalog.source_sha256,
        )
        proposals: dict[tuple[str, str], Any] = {}
        for server_id in selected_servers:
            channels, categories = _saved_inventory(table.rows, server_id)
            server_keys = {
                (server_id, str(row.get("stream_id", "")))
                for row in table.rows
                if row.get("server_id") == server_id
            }
            server_targets = tuple(
                key for key in target_by_key if key[0] == server_id
            )
            produced = automatch.propose_new_channel_matches(
                runtime.resolver,
                server_id=server_id,
                channels=channels,
                category_names=categories,
                existing_keys=server_keys,
                target_keys=server_targets,
                catalog=matcher_catalog,
                matcher_identity=runtime.identity,
                preflight=runtime.preflight,
            )
            overlap = set(proposals).intersection(produced)
            if overlap:
                raise ContractError(
                    "The dummy-shadow matcher returned duplicate identities."
                )
            proposals.update(produced)
        if set(proposals) != set(target_by_key):
            raise ContractError(
                "The dummy-shadow matcher did not cover the exact REVIEW set."
            )
        proposal_box.append(proposals)
        runtime_box.append(runtime)
        corroboration_box.append(corroboration)
        exact_dummy_ids_box.append(frozenset(shared_dummy.values()))
        # Dummy classification deliberately does not request programme records.
        return ()

    try:
        one_pass = catalog_stream.stream_catalog_and_programmes_once(
            path=Path(all_source_file),
            fixed_wanted_ids=(),
            select_provisional_ids=select_no_programme_ids,
            window_start=as_of_epoch,
            now_epoch=as_of_epoch,
            minimum_unique_channels=int(minimum_unique_channels),
        )
    except (catalog_stream.CatalogStreamError, automatch.AutoMatchError) as exc:
        raise ContractError(str(exc)) from exc
    if not (
        len(proposal_box)
        == len(runtime_box)
        == len(corroboration_box)
        == len(exact_dummy_ids_box)
        == 1
    ):
        raise ContractError("The dummy-shadow catalog selector did not run exactly once.")
    if one_pass.catalog.source_sha256 != one_pass.source_sha256:
        raise ContractError("The dummy-shadow XML catalog was not source-bound.")

    mapping_table_sha256 = sync.mapping_table_fingerprint(table)
    open_alerts_sha256 = sha256_json(
        [[server_id, stream_id] for server_id, stream_id in sorted(open_alerts)]
    )
    lab_code_sha256 = package_code_sha256(Path(__file__).resolve().parent)
    input_sha256 = {
        "MAPPING_FILE": mapping_file_sha256,
        "MAPPING_TABLE": mapping_table_sha256,
        "ALERTS_FILE": alerts_file_sha256,
        "OPEN_ALERT_KEYS": open_alerts_sha256,
        "EPG_XML": one_pass.source_sha256,
        "EPG_XML_CATALOG": one_pass.catalog.fingerprint_sha256,
        "EPG_TEXT": text_file_sha256,
        "EPG_TEXT_CATALOG": text_catalog.fingerprint_sha256,
    }
    run_id = sha256_json(
        {
            "schema": DUMMY_MANIFEST_SCHEMA,
            "mode": "dummy-shadow",
            "generated_at": created_at,
            "servers": list(selected_servers),
            "input_sha256": input_sha256,
            "lab_code_sha256": lab_code_sha256,
        }
    )

    runtime = runtime_box[0]
    exact_dummy_ids = exact_dummy_ids_box[0]
    empty_schedule_evidence = automatch.ScheduleEvidence(
        declared_ids=frozenset(),
        informative_future_programmes={},
        latest_informative_future_stop={},
        gate_passed_by_id={},
        checked_at_epoch=as_of_epoch,
        source_sha256=one_pass.source_sha256,
    )
    records: list[dict[str, object]] = []
    counts: Counter[str] = Counter(
        {
            "REVIEW_ROWS": len(target_rows),
            "DUMMY_CLASSIFIED": 0,
            "DUMMY_UNBLOCKED": 0,
            "DUMMY_BLOCKED_ALERT": 0,
            "DUMMY_REJECTED": 0,
        }
    )
    proposals = proposal_box[0]
    for key in sorted(
        proposals,
        key=lambda value: (value[0], streaming.stream_sort_key(value[1])),
    ):
        proposal = proposals[key]
        source_row = target_by_key[key]
        if (
            proposal.server_id != key[0]
            or proposal.stream_id != key[1]
            or proposal.channel_name
            != _canonical_display(source_row.get("channel_name", ""), 300)
            or proposal.category_name
            != _canonical_display(source_row.get("category_name", ""), 200)
        ):
            raise ContractError(
                "A dummy-shadow proposal escaped its saved provider identity."
            )
        if proposal.matcher_action != "AUTO_DUMMY":
            continue
        safe_shape = (
            proposal.target_source == "dummy"
            and proposal.target_feed == "DUMMY_CHANNELS"
            and proposal.target_epg_id in exact_dummy_ids
            and not proposal.second_epg_id
        )
        finalized = automatch.finalize_proposal(
            proposal, empty_schedule_evidence
        )
        # The exact numbered-event-bank method is deliberately review-only in
        # the live adapter. This private sidecar may still record it after an
        # independent exact category/name/target check. Every other method
        # must pass the normal production finalization boundary.
        shadow_exact_verified = (
            proposal.match_method == "exact_numbered_event_bank"
            and _has_exact_numbered_event_bank_evidence(
                epg_id=proposal.target_epg_id,
                channel_name=proposal.channel_name,
                category_name=proposal.category_name,
            )
        )
        live_safe = bool(proposal.eligible_for_finalization) and finalized.approved
        if not safe_shape or not (live_safe or shadow_exact_verified):
            counts["DUMMY_REJECTED"] += 1
            continue
        blocked_alert = key in open_alerts
        counts["DUMMY_CLASSIFIED"] += 1
        counts[
            "DUMMY_BLOCKED_ALERT" if blocked_alert else "DUMMY_UNBLOCKED"
        ] += 1
        records.append(
            _classification_record(
                run_id=run_id,
                created_at=created_at,
                row=source_row,
                proposal=proposal,
                blocked_alert=blocked_alert,
                source_sha256=one_pass.source_sha256,
                xml_catalog_sha256=one_pass.catalog.fingerprint_sha256,
                text_file_sha256=text_file_sha256,
                text_catalog_sha256=text_catalog.fingerprint_sha256,
                text_generated=text_catalog.generated_token,
                mapping_table_sha256=mapping_table_sha256,
                alerts_file_sha256=alerts_file_sha256,
                open_alerts_sha256=open_alerts_sha256,
                lab_code_sha256=lab_code_sha256,
                runtime=runtime,
            )
        )

    summary = {
        "schema": DUMMY_SUMMARY_SCHEMA,
        "mode": "dummy-shadow",
        "run_id": run_id,
        "generated_at": created_at,
        "private_details_emitted": False,
        "write_authority": False,
        "counts": dict(sorted(counts.items())),
    }
    manifest_base = {
        "schema": DUMMY_MANIFEST_SCHEMA,
        "mode": "dummy-shadow",
        "run_id": run_id,
        "generated_at": created_at,
        "servers": list(selected_servers),
        "input_sha256": input_sha256,
        "lab_code_sha256": lab_code_sha256,
        "classification_count": len(records),
        "counts": dict(sorted(counts.items())),
        "private_artifact": True,
        "write_authority": False,
    }
    _write_dummy_bundle(
        output_dir,
        records=records,
        summary=summary,
        manifest_base=manifest_base,
    )
    return DummyShadowResult(
        output_dir=Path(output_dir),
        run_id=run_id,
        classification_count=len(records),
        counts=dict(sorted(counts.items())),
    )


__all__ = (
    "DUMMY_CLASSIFICATION_SCHEMA",
    "DUMMY_MANIFEST_SCHEMA",
    "DUMMY_SUMMARY_SCHEMA",
    "DummyShadowResult",
    "run_dummy_shadow",
)
