#!/usr/bin/env python3
"""Strict Version 1 auto-matching and one-pass EPGShare spool orchestration.

This module deliberately has no Google Sheets or provider-network code.  It
receives an already validated mapping table and provider inventories, proposes
matches only for newly discovered stream identities, validates those proposals
against programmes from the same ALL_SOURCES1 byte snapshot, and seals the
selected rows for the production builder.  The caller may perform external
writes only after this function returns successfully.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


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
    ScheduleEvidence,
    finalize_proposal,
    prepare_resolver_strict,
    propose_new_channel_matches,
)
from skytv_epg_contextual_v8 import install_contextual_v8  # noqa: E402


AUTO_MATCH_INTEGRATION_VERSION = "1.0"
SUPPORTED_SERVERS = frozenset({"server_1", "server_2", "server_3"})
DEFAULT_EPG_HISTORY_DAYS = 3
MINIMUM_CORROBORATED_CATALOG_IDS = 25_000
SOURCE_BASE_URL = "https://epgshare01.online/epgshare01/"
ENGINE_SOURCE_PATH = SRC_DIR / "skytv_epg_engine.py"
CONTEXTUAL_SOURCE_PATH = SRC_DIR / "skytv_epg_contextual_v8.py"
APPROVED_ALIASES_PATH = REPOSITORY_ROOT / "knowledge" / "approved_channel_aliases.csv"
SCHEDULE_EQUIVALENCES_PATH = (
    REPOSITORY_ROOT / "knowledge" / "schedule_equivalence_groups.json"
)
APPROVED_ALIASES_SHA256 = (
    "f99a1d53ecfb2487d441aabe720f62f458299751de49f2d63b779bdf4c5b83e1"
)
SCHEDULE_EQUIVALENCES_SHA256 = (
    "a0de16a12d00d6aca96c7f7a54ed444a47467115266f467521880cc8a56b6edc"
)
LARGE_BATCH_THRESHOLD = 100
MAX_LARGE_BATCH_APPROVAL_FRACTION = 0.35
MAX_LARGE_BATCH_APPROVALS = 5_000
MAX_LARGE_BATCH_APPROVALS_PER_SERVER = 2_500
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
class AutoMatchOutcome:
    rows: tuple[dict[str, str], ...]
    considered_rows: int
    provisional_rows: int
    approved_rows: int
    review_rows: int
    rejected_programme_gates: int
    fixed_requested_ids: int
    catalog_channels: int
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

    def summary_fields(self) -> dict[str, Any]:
        return {
            "auto_match_considered_rows": self.considered_rows,
            "auto_match_provisional_rows": self.provisional_rows,
            "auto_matched_rows": self.approved_rows,
            "auto_match_review_rows": self.review_rows,
            "auto_match_rejected_programme_gates": self.rejected_programme_gates,
            "epgshare_spool_fixed_ids": self.fixed_requested_ids,
            "epgshare_catalog_channels": self.catalog_channels,
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
        if requested_source != "panel":
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


def _validate_new_rows(
    rows: Sequence[Mapping[str, Any]], existing_keys: frozenset[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, str]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    for raw in rows:
        key = _canonical_key(raw.get("server_id", ""), raw.get("stream_id", ""))
        if key in existing_keys or key in result:
            raise AutoMatchError("The new-channel set contains an existing or duplicate identity.")
        row = {str(column): str(value or "") for column, value in raw.items()}
        result[key] = row
    return result


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


def auto_match_and_spool(
    *,
    mapping_rows: Sequence[Mapping[str, Any]],
    inventories: Sequence[Any],
    new_rows: Sequence[Mapping[str, Any]],
    all_source_file: Path,
    all_source_catalog_file: Path,
    spool_out: Path,
    generated_at: str,
    minimum_unique_channels: int = MINIMUM_CORROBORATED_CATALOG_IDS,
    runtime_factory: MatcherRuntimeFactory = prepare_matcher_runtime,
) -> AutoMatchOutcome:
    """Return patched new rows only after a successful one-pass sealed parse."""
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
    if text_catalog_path.is_symlink() or not text_catalog_path.is_file():
        raise AutoMatchError("The official ALL_SOURCES1 text catalog is unavailable.")
    try:
        text_catalog_size = text_catalog_path.stat().st_size
        if text_catalog_size < 1 or text_catalog_size > catalog_stream.MAX_CATALOG_TEXT_BYTES:
            raise AutoMatchError("The official ALL_SOURCES1 text catalog has an invalid size.")
        text_catalog_bytes = text_catalog_path.read_bytes()
        if len(text_catalog_bytes) != text_catalog_size:
            raise AutoMatchError("The official ALL_SOURCES1 text catalog changed while reading.")
        text_catalog = catalog_stream.parse_all_sources_text(
            text_catalog_bytes,
            minimum_ids=int(minimum_unique_channels),
        )
    except AutoMatchError:
        raise
    except (OSError, catalog_stream.CatalogStreamError) as exc:
        raise AutoMatchError(str(exc)) from exc

    existing_keys = frozenset(
        _canonical_key(row.get("server_id", ""), row.get("stream_id", ""))
        for row in mapping_rows
    )
    rows_by_key = _validate_new_rows(new_rows, existing_keys)
    fixed_ids = active_combined_source_ids(mapping_rows)
    now_epoch = _timestamp_epoch(generated_at)
    window_start = now_epoch - DEFAULT_EPG_HISTORY_DAYS * 86400

    safe_inventories: list[tuple[str, list[dict[str, str]], dict[str, str]]] = []
    for inventory in inventories:
        server_id = _canonical_key(getattr(inventory, "server_id", ""), "probe")[0]
        channels, categories = _safe_matcher_inventory(inventory)
        safe_inventories.append((server_id, channels, categories))

    proposals: dict[tuple[str, str], Any] = {}
    runtime_box: list[MatcherRuntime] = []
    matcher_catalog_box: list[MatcherCatalogSnapshot] = []

    def select_provisional_ids(
        source_catalog: catalog_stream.CatalogSnapshot,
    ) -> Iterable[str]:
        xml_ids = frozenset(channel.epg_id for channel in source_catalog.channels)
        text_ids = frozenset(entry.epg_id for entry in text_catalog.entries)
        if xml_ids != text_ids:
            raise catalog_stream.CatalogStreamError(
                "The XML and official text catalogs do not declare the same exact IDs."
            )
        real_candidates, dummy_ids = text_catalog.matcher_inputs()
        runtime = runtime_factory(real_candidates, dummy_ids)
        if not runtime.identity.is_expected or not runtime.preflight.ready:
            raise AutoMatchError("The Version 1 matcher verification failed safely.")
        matcher_catalog = MatcherCatalogSnapshot.from_matcher_catalog(
            (channel.epg_id for channel in source_catalog.channels),
            real_candidates=real_candidates,
            dummy_ids=dummy_ids,
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
                catalog=matcher_catalog,
                matcher_identity=runtime.identity,
                preflight=runtime.preflight,
            )
            overlap = set(proposals).intersection(server_proposals)
            if overlap:
                raise AutoMatchError("The matcher returned duplicate stream identities.")
            proposals.update(server_proposals)
        if set(proposals) != set(rows_by_key):
            raise AutoMatchError("The matcher result did not cover the exact new-channel set.")
        runtime_box.append(runtime)
        matcher_catalog_box.append(matcher_catalog)
        return sorted(
            {
                proposal.target_epg_id
                for proposal in proposals.values()
                if proposal.eligible_for_finalization
                and proposal.target_source == "epgshare01"
                and proposal.target_epg_id
            },
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
            if len(runtime_box) != 1 or len(matcher_catalog_box) != 1:
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
                    if (
                        row.get("action") != "AUTO_EPGSHARE"
                        or row.get("source") != "epgshare01"
                        or row.get("epg_feed") != "ALL_SOURCES1"
                        or row.get("enabled") != "TRUE"
                        or not row.get("epg_id")
                    ):
                        raise AutoMatchError("An approved match violated the EPGShare-only policy.")
                    approved_count += 1
                    approved_ids.add(row["epg_id"])
                    approved_by_server[key[0]] += 1
                else:
                    row["action"] = "REVIEW"
                    row["enabled"] = "FALSE"
                if any(
                    row.get(column, "") != metadata_before[column]
                    for column in _METADATA_COLUMNS
                ):
                    raise AutoMatchError("Automatic EPG matching attempted to approve metadata.")
                patched_rows.append(row)

            _enforce_approval_blast_radius(
                approved_by_server, total_new_rows=len(rows_by_key)
            )

            resolved_targets = frozenset(result.resolved_source_ids.values())
            if not approved_ids.issubset(resolved_targets):
                raise AutoMatchError("A newly approved EPG ID was not resolved in ALL_SOURCES1.")
            for epg_id in approved_ids:
                gate = gates.get(epg_id)
                if gate is None or not gate.passed:
                    raise AutoMatchError("A newly approved EPG ID lacks the strong programme gate.")
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
    provisional_ids = {
        proposal.target_epg_id
        for proposal in proposals.values()
        if proposal.eligible_for_finalization and proposal.target_epg_id
    }
    rejected_gates = sum(
        1
        for epg_id in provisional_ids
        if epg_id not in result.programme_gates
        or not result.programme_gates[epg_id].passed
    )
    return AutoMatchOutcome(
        rows=tuple(patched_rows),
        considered_rows=len(proposals),
        provisional_rows=len(provisional_ids),
        approved_rows=approved_count,
        review_rows=len(patched_rows) - approved_count,
        rejected_programme_gates=rejected_gates,
        fixed_requested_ids=len(result.requested_fixed_ids),
        catalog_channels=len(result.catalog.channels),
        source_sha256=result.source_sha256,
        catalog_sha256=result.catalog.fingerprint_sha256,
        text_catalog_generated_token=text_catalog.generated_token,
        text_catalog_fingerprint_sha256=text_catalog.fingerprint_sha256,
        text_catalog_file_sha256=hashlib.sha256(text_catalog_bytes).hexdigest(),
        matcher_version=runtime.identity.version,
        matcher_build_id=runtime.identity.build_id,
        matcher_sha256=runtime.identity.source_sha256,
        matcher_engine_sha256=runtime.identity.engine_source_sha256,
        approved_aliases_sha256=runtime.approved_aliases_sha256,
        schedule_equivalences_sha256=runtime.schedule_equivalences_sha256,
    )


__all__ = [
    "AUTO_MATCH_INTEGRATION_VERSION",
    "AutoMatchError",
    "AutoMatchOutcome",
    "MatcherRuntime",
    "active_combined_source_ids",
    "auto_match_and_spool",
    "prepare_matcher_runtime",
]
