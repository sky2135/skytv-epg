from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import apply_matching_lab_approvals as apply_approvals  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from matching_lab.models import ContractError, sha256_json  # noqa: E402
from matching_lab.retrieval import (  # noqa: E402
    mapping_row_guard,
    provider_identity_guard,
)


APPROVAL_ID = "a" * 64
CONTENT_SHA = "b" * 64
RUN_ID = "c" * 64
GENERATED_AT = "2026-09-18T00:41:00Z"
APPROVED_AT = "2026-09-18T01:00:00Z"
PREPARED_AT = "2026-09-18T01:30:00Z"
EXPIRES_AT = "2026-09-18T06:41:00Z"


def mapping_row(stream_id: str, *, notes: str | None = None) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": f"Channel {stream_id}",
            "canonical_name": f"Channel {stream_id}",
            "category_id": "cat-1",
            "category_name": "US | General",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": "",
            "reason": "Automated Matching Lab review candidate.",
            "notes": notes if notes is not None else f"auto-map-v1 existing={stream_id}",
        }
    )
    return row


def proposal(
    row: dict[str, str],
    *,
    score_ppm: int = 950_000,
    margin_ppm: int = 200_000,
    epg_id: str | None = None,
) -> dict[str, object]:
    stream_id = row["stream_id"]
    return {
        "proposal_id": sha256_json(["proposal", stream_id]),
        "server_id": row["server_id"],
        "stream_id": stream_id,
        "row_guard_sha256": mapping_row_guard(row),
        "provider_identity_sha256": provider_identity_guard(row),
        "decision_state": "NEEDS_REVIEW",
        "selected_candidate_key": f"candidate-{stream_id}",
        "selected_epg_id": epg_id or f"Guide.{stream_id}.us",
        "score_ppm": score_ppm,
        "margin_ppm": margin_ppm,
    }


def approval_document(proposals: list[dict[str, object]]) -> dict[str, object]:
    return {
        "approval_id": APPROVAL_ID,
        "content_sha256": CONTENT_SHA,
        "approved_at": APPROVED_AT,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "run": {
            "run_id": RUN_ID,
            "generated_at": GENERATED_AT,
            "expires_at": EXPIRES_AT,
        },
        "snapshots": {
            "mappings_file_sha256": "d" * 64,
            "mappings_table_sha256": "e" * 64,
            "alerts_file_sha256": "f" * 64,
            "open_alert_keys_sha256": "0" * 64,
        },
    }


def mapping_table(rows: list[dict[str, str]]) -> sync.MappingTable:
    columns = list(streaming.SHEET_COLUMNS)
    return sync.MappingTable(columns, columns, rows)


class ApplyMatchingLabApprovalTests(unittest.TestCase):
    def test_selects_highest_confidence_25_and_changes_only_patch_columns(self) -> None:
        rows = [mapping_row(str(index)) for index in range(1, 31)]
        proposals = [
            proposal(
                row,
                score_ppm=900_000 + int(row["stream_id"]),
                margin_ppm=100_000 + int(row["stream_id"]),
                epg_id=(
                    "Česká.Televize.HD.cz"
                    if row["stream_id"] == "30"
                    else f"Guide.{row['stream_id']}.us"
                ),
            )
            for row in reversed(rows)
        ]
        result = apply_approvals.prepare_approved_rows(
            approval_document(proposals),
            mapping_table(rows),
            prepared_at=PREPARED_AT,
        )

        self.assertEqual(result.selected_count, 25)
        self.assertEqual(result.eligible_count, 30)
        self.assertEqual(result.remaining_count, 5)
        self.assertEqual(result.limit, 25)
        self.assertEqual(result.selected[0].stream_id, "30")
        self.assertEqual(result.selected[-1].stream_id, "6")
        self.assertEqual(
            result.selected[0].desired_row["epg_id"], "Česká.Televize.HD.cz"
        )
        original_by_stream = {row["stream_id"]: row for row in rows}
        for selected in result.selected:
            before = original_by_stream[selected.stream_id]
            desired = selected.desired_row
            changed = {
                column
                for column in streaming.SHEET_COLUMNS
                if before[column] != desired[column]
            }
            self.assertTrue(changed)
            self.assertTrue(changed.issubset(apply_approvals.PATCH_COLUMNS))
            self.assertEqual(desired["enabled"], "TRUE")
            self.assertEqual(desired["action"], "APPROVED")
            self.assertEqual(desired["source"], "epgshare01")
            self.assertEqual(desired["epg_feed"], "ALL_SOURCES1")
            self.assertTrue(
                desired["notes"].startswith(
                    f"{apply_approvals.PROVENANCE_MARKER} "
                )
            )
            self.assertTrue(desired["notes"].endswith(before["notes"]))
            for column in set(streaming.SHEET_COLUMNS) - set(
                apply_approvals.PATCH_COLUMNS
            ):
                self.assertEqual(desired[column], before[column])
        self.assertEqual(rows[29]["action"], "REVIEW")

    def test_ties_use_natural_stream_order(self) -> None:
        row_10 = mapping_row("10")
        row_2 = mapping_row("2")
        result = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row_10), proposal(row_2)]),
            mapping_table([row_10, row_2]),
            prepared_at=PREPARED_AT,
            limit=1,
        )
        self.assertEqual(result.selected[0].stream_id, "2")

    def test_mapping_guard_drift_stops_entire_batch(self) -> None:
        original = mapping_row("1")
        approved = proposal(original)
        drifted = dict(original)
        drifted["category_name"] = "Changed"
        with self.assertRaisesRegex(ContractError, "Mapping row changed"):
            apply_approvals.prepare_approved_rows(
                approval_document([approved]),
                mapping_table([drifted]),
                prepared_at=PREPARED_AT,
            )

    def test_exact_committed_row_is_skipped(self) -> None:
        committed_original = mapping_row("1")
        committed_proposal = proposal(committed_original)
        committed = apply_approvals._desired_row(
            committed_original,
            committed_proposal,
            approval_id=APPROVAL_ID,
            run_id=RUN_ID,
            approved_at=APPROVED_AT,
        )
        pending = mapping_row("2")
        result = apply_approvals.prepare_approved_rows(
            approval_document([committed_proposal, proposal(pending)]),
            mapping_table([committed, pending]),
            prepared_at=PREPARED_AT,
        )
        self.assertEqual(result.already_committed_count, 1)
        self.assertEqual(result.selected_count, 1)
        self.assertEqual(result.selected[0].stream_id, "2")

    def test_approved_row_without_exact_provenance_is_rejected(self) -> None:
        original = mapping_row("1")
        approved = proposal(original)
        bad = dict(original)
        bad.update(
            {
                "enabled": "TRUE",
                "action": "APPROVED",
                "epg_id": str(approved["selected_epg_id"]),
                "notes": "manual approval without package binding",
            }
        )
        with self.assertRaisesRegex(ContractError, "exact package provenance"):
            apply_approvals.prepare_approved_rows(
                approval_document([approved]),
                mapping_table([bad]),
                prepared_at=PREPARED_AT,
            )

    def test_spoofed_or_repeated_provenance_is_rejected(self) -> None:
        original = mapping_row("1")
        approved = proposal(original)
        exact = (
            f"{apply_approvals.PROVENANCE_MARKER} "
            f"approval_id={APPROVAL_ID} run_id={RUN_ID} "
            f"proposal_id={approved['proposal_id']} approved_at={APPROVED_AT}"
        )
        for notes in (
            "not-" + exact,
            exact.replace(f"approved_at={APPROVED_AT}", "approved_at=2026-09-18T01:00:01Z"),
            exact + "\n" + exact,
        ):
            with self.subTest(notes=notes[:80]):
                bad = dict(original)
                bad.update(
                    {
                        "enabled": "TRUE",
                        "action": "APPROVED",
                        "epg_id": str(approved["selected_epg_id"]),
                        "notes": notes,
                    }
                )
                with self.assertRaisesRegex(
                    ContractError, "exact package provenance"
                ):
                    apply_approvals.prepare_approved_rows(
                        approval_document([approved]),
                        mapping_table([bad]),
                        prepared_at=PREPARED_AT,
                    )

    def test_batch_limit_above_25_is_rejected(self) -> None:
        row = mapping_row("1")
        with self.assertRaisesRegex(ContractError, "between 1 and 25"):
            apply_approvals.prepare_approved_rows(
                approval_document([proposal(row)]),
                mapping_table([row]),
                prepared_at=PREPARED_AT,
                limit=26,
            )

    def test_batch_document_is_explicitly_non_authoritative(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        document["snapshots"] = {
            "mappings_file_sha256": "d" * 64,
            "mappings_table_sha256": "e" * 64,
            "alerts_file_sha256": "f" * 64,
            "open_alert_keys_sha256": "0" * 64,
        }
        result = apply_approvals.prepare_approved_rows(
            document,
            mapping_table([row]),
            prepared_at=PREPARED_AT,
        )
        batch = apply_approvals.approval_batch_document(result, document)
        self.assertFalse(batch["write_authority"])
        self.assertTrue(batch["provider_revalidation_required"])
        self.assertTrue(batch["alert_revalidation_required"])
        self.assertTrue(batch["catalog_revalidation_required"])
        self.assertTrue(batch["programme_revalidation_required"])
        self.assertEqual(batch["selection"]["limit"], 25)
        self.assertEqual(batch["selection"]["selected_count"], 1)
        self.assertEqual(len(batch["batch_id"]), 64)
        self.assertEqual(len(batch["content_sha256"]), 64)

    def test_live_atomic_writer_commits_exactly_25_in_one_request(self) -> None:
        rows = [mapping_row(str(index)) for index in range(1, 26)]
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row) for row in rows]),
            mapping_table(rows),
            prepared_at=PREPARED_AT,
        )
        after_rows = [item.desired_row for item in prepared.selected]
        after_values = [list(streaming.SHEET_COLUMNS)] + [
            [row[column] for column in streaming.SHEET_COLUMNS]
            for row in after_rows
        ]

        class Response:
            status_code = 200
            headers: dict[str, str] = {}

            def __init__(self, content: bytes):
                self.content = content

            def close(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.posts: list[dict[str, object]] = []

            def post(self, url: str, **kwargs: object) -> Response:
                body = json.loads(bytes(kwargs["data"]).decode("utf-8"))
                self.posts.append({"url": url, "body": body})
                return Response(
                    json.dumps(
                        {
                            "spreadsheetId": "s" * 20,
                            "replies": [{} for _request in body["requests"]],
                        }
                    ).encode("utf-8")
                )

        session = Session()
        with mock.patch.object(sync, "google_sheet_values", return_value=after_values):
            committed, observed = apply_approvals._post_atomic_human_batch(
                session,
                sheet_id="s" * 20,
                tab_name="Mappings",
                before_table=mapping_table(rows),
                batch=prepared,
                numeric_sheet_id=123,
            )

        self.assertEqual(committed, 25)
        self.assertEqual(len(observed.rows), 25)
        self.assertEqual(len(session.posts), 1)
        requests = session.posts[0]["body"]["requests"]
        self.assertEqual(len(requests), 75)
        touched_columns = {
            (
                request["updateCells"]["range"]["startColumnIndex"],
                request["updateCells"]["range"]["endColumnIndex"],
            )
            for request in requests
        }
        indexes = {
            column: list(streaming.SHEET_COLUMNS).index(column)
            for column in apply_approvals.PATCH_COLUMNS
        }
        self.assertEqual(
            touched_columns,
            {
                (indexes["enabled"], indexes["enabled"] + 1),
                (indexes["action"], indexes["epg_id"] + 1),
                (indexes["reason"], indexes["notes"] + 1),
            },
        )

    def test_live_atomic_writer_fails_closed_when_post_is_uncertain(self) -> None:
        row = mapping_row("1")
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row)]),
            mapping_table([row]),
            prepared_at=PREPARED_AT,
        )
        before_values = [list(streaming.SHEET_COLUMNS)] + [
            [row[column] for column in streaming.SHEET_COLUMNS]
        ]

        class Session:
            def post(self, _url: str, **_kwargs: object) -> object:
                raise TimeoutError("unknown outcome")

        with mock.patch.object(sync, "google_sheet_values", return_value=before_values):
            with self.assertRaises(sync.SheetWriteError) as raised:
                apply_approvals._post_atomic_human_batch(
                    Session(),
                    sheet_id="s" * 20,
                    tab_name="Mappings",
                    before_table=mapping_table([row]),
                    batch=prepared,
                    numeric_sheet_id=123,
                )
        self.assertEqual(raised.exception.appended_count, 0)

    def test_live_confirmation_rejects_noncanonical_id_before_remote_access(self) -> None:
        with self.assertRaisesRegex(ContractError, "confirmed approval ID"):
            apply_approvals.apply_live_approval_batch(
                approval_file=Path("approval.json"),
                bundle_dir=Path("bundle"),
                mappings_csv=Path("Mappings.csv"),
                alerts_csv=Path("Sync Alerts.csv"),
                all_source_file=Path("ALL_SOURCES1.xml.gz"),
                all_source_catalog_file=Path("ALL_SOURCES1.txt"),
                google_session=object(),
                sheet_id="s" * 20,
                sheet_tab="Mappings",
                alerts_tab="Sync Alerts",
                confirm_approval_id="A" * 64,
            )

    def test_provider_identity_requires_current_category_and_name(self) -> None:
        row = mapping_row("1")
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row)]),
            mapping_table([row]),
            prepared_at=PREPARED_AT,
        )
        inventory = sync.PanelInventory(
            server_id="server_1",
            server_label="Server 1",
            categories=[],
            channels=[
                {
                    "stream_id": "1",
                    "name": row["channel_name"],
                    "category_id": row["category_id"],
                    "category_name": "Changed Category",
                }
            ],
            source="test",
        )
        with self.assertRaisesRegex(ContractError, "changed provider identity"):
            apply_approvals._require_provider_identity_matches(prepared, [inventory])

    def test_resumed_apply_rejects_drift_in_previously_committed_row(self) -> None:
        original = mapping_row("1")
        approved = proposal(original)
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([approved]),
            mapping_table([original]),
            prepared_at=PREPARED_AT,
        )
        committed = dict(prepared.selected[0].desired_row)
        committed["category_name"] = "Changed after commit"
        with self.assertRaisesRegex(
            ContractError, "previously committed approval"
        ):
            apply_approvals._require_committed_rows_match_approved_transition(
                approval_document([approved]),
                mapping_table([original]),
                mapping_table([committed]),
            )

    def test_live_apply_rejects_unrelated_mapping_drift(self) -> None:
        target = mapping_row("1")
        unrelated = mapping_row("2")
        changed_unrelated = dict(unrelated)
        changed_unrelated["notes"] = "operator changed unrelated row"
        with self.assertRaisesRegex(ContractError, "unrelated Mapping row"):
            apply_approvals._require_committed_rows_match_approved_transition(
                approval_document([proposal(target)]),
                mapping_table([target, unrelated]),
                mapping_table([target, changed_unrelated]),
            )

    def test_live_apply_orchestrates_every_gate_before_one_atomic_post(self) -> None:
        row = mapping_row("1")
        approved = proposal(row)
        document = approval_document([approved])
        before_table = mapping_table([row])
        prepared = apply_approvals.prepare_approved_rows(
            document, before_table, prepared_at=PREPARED_AT
        )
        desired = prepared.selected[0].desired_row
        before_values = [list(streaming.SHEET_COLUMNS)] + [
            [row[column] for column in streaming.SHEET_COLUMNS]
        ]
        after_values = [list(streaming.SHEET_COLUMNS)] + [
            [desired[column] for column in streaming.SHEET_COLUMNS]
        ]
        alert_values = [list(sync.ALERT_COLUMNS)]
        inventory = sync.PanelInventory(
            server_id="server_1",
            server_label="Server 1",
            categories=[],
            channels=[
                {
                    "stream_id": "1",
                    "name": row["channel_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                }
            ],
            source="test",
        )
        config = sync.ServerConfig(
            "server_1", "Server 1", "https://provider.invalid", "user", "pass"
        )
        validation = apply_approvals.approvals.ApprovalResult(
            approval_file=Path("approval.json"),
            approval_id=APPROVAL_ID,
            content_sha256=CONTENT_SHA,
            run_id=RUN_ID,
            approved_at=APPROVED_AT,
            proposal_count=1,
        )

        class Response:
            status_code = 200
            headers: dict[str, str] = {}

            def __init__(self, content: bytes):
                self.content = content

            def close(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.posts = 0

            def post(self, _url: str, **kwargs: object) -> Response:
                self.posts += 1
                body = json.loads(bytes(kwargs["data"]).decode("utf-8"))
                return Response(
                    json.dumps(
                        {
                            "spreadsheetId": "s" * 20,
                            "replies": [{} for _request in body["requests"]],
                        }
                    ).encode("utf-8")
                )

        session = Session()
        provider_stats = {
            "provider_batch_revalidation_checked": 1,
            "provider_batch_revalidation_rejected": 0,
            "provider_batch_revalidation_unavailable": 0,
        }
        layout = sync.GoogleSheetLayout(
            123, "Mappings", None, None, 100, 33, False, None
        )
        with (
            mock.patch.object(apply_approvals, "_canonical_now", return_value=PREPARED_AT),
            mock.patch.object(
                apply_approvals,
                "_validate_approval_exact",
                return_value=(validation, document),
            ) as approval_check,
            mock.patch.object(
                apply_approvals, "_read_exact_snapshot", return_value=b"snapshot"
            ),
            mock.patch.object(sync, "parse_mapping_csv", return_value=before_table),
            mock.patch.object(
                sync, "google_sheet_values", side_effect=[before_values, after_values]
            ),
            mock.patch.object(
                sync, "google_sync_alert_values", return_value=alert_values
            ),
            mock.patch.object(
                apply_approvals, "validate_current_catalogue", return_value=None
            ) as catalogue_check,
            mock.patch.object(sync, "load_server_configs", return_value=[config]),
            mock.patch.object(
                apply_approvals,
                "_fetch_provider_inventories",
                return_value=(inventory,),
            ) as provider_fetch,
            mock.patch.object(
                sync,
                "revalidate_provider_identity_updates",
                side_effect=lambda updates, *_args, **_kwargs: (
                    list(updates),
                    provider_stats,
                ),
            ) as provider_refetch,
            mock.patch.object(
                apply_approvals.approvals,
                "load_approval_document",
                return_value=document,
            ),
            mock.patch.object(apply_approvals, "_verify_bundle_files", return_value=None),
            mock.patch.object(sync, "google_sheet_layout", return_value=layout),
            mock.patch.object(
                apply_approvals,
                "_google_mapping_and_alert_values",
                return_value=(before_values, alert_values),
            ) as adjacent_read,
        ):
            result = apply_approvals.apply_live_approval_batch(
                approval_file=Path("approval.json"),
                bundle_dir=Path("bundle"),
                mappings_csv=Path("Mappings.csv"),
                alerts_csv=Path("Sync Alerts.csv"),
                all_source_file=Path("ALL_SOURCES1.xml.gz"),
                all_source_catalog_file=Path("ALL_SOURCES1.txt"),
                google_session=session,
                sheet_id="s" * 20,
                sheet_tab="Mappings",
                alerts_tab="Sync Alerts",
                confirm_approval_id=APPROVAL_ID,
            )

        self.assertEqual(result.committed_count, 1)
        self.assertEqual(result.remaining_count, 0)
        self.assertEqual(session.posts, 1)
        self.assertEqual(approval_check.call_count, 2)
        catalogue_check.assert_called_once()
        provider_fetch.assert_called_once()
        provider_refetch.assert_called_once()
        adjacent_read.assert_called_once()

    def test_live_apply_blocks_failed_second_provider_revalidation(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        before_table = mapping_table([row])
        before_values = [list(streaming.SHEET_COLUMNS)] + [
            [row[column] for column in streaming.SHEET_COLUMNS]
        ]
        inventory = sync.PanelInventory(
            server_id="server_1",
            server_label="Server 1",
            categories=[],
            channels=[
                {
                    "stream_id": "1",
                    "name": row["channel_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                }
            ],
            source="test",
        )
        config = sync.ServerConfig(
            "server_1", "Server 1", "https://provider.invalid", "user", "pass"
        )
        validation = apply_approvals.approvals.ApprovalResult(
            approval_file=Path("approval.json"),
            approval_id=APPROVAL_ID,
            content_sha256=CONTENT_SHA,
            run_id=RUN_ID,
            approved_at=APPROVED_AT,
            proposal_count=1,
        )

        class Session:
            def __init__(self) -> None:
                self.posts = 0

            def post(self, _url: str, **_kwargs: object) -> object:
                self.posts += 1
                raise AssertionError("provider rejection must precede POST")

        for failure_field in (
            "provider_batch_revalidation_rejected",
            "provider_batch_revalidation_unavailable",
        ):
            with self.subTest(failure_field=failure_field):
                session = Session()
                provider_stats = {
                    "provider_batch_revalidation_checked": 1,
                    "provider_batch_revalidation_rejected": 0,
                    "provider_batch_revalidation_unavailable": 0,
                }
                provider_stats[failure_field] = 1
                with (
                    mock.patch.object(
                        apply_approvals, "_canonical_now", return_value=PREPARED_AT
                    ),
                    mock.patch.object(
                        apply_approvals,
                        "_validate_approval_exact",
                        return_value=(validation, document),
                    ),
                    mock.patch.object(
                        apply_approvals,
                        "_read_exact_snapshot",
                        return_value=b"snapshot",
                    ),
                    mock.patch.object(
                        sync, "parse_mapping_csv", return_value=before_table
                    ),
                    mock.patch.object(
                        sync, "google_sheet_values", return_value=before_values
                    ),
                    mock.patch.object(
                        sync,
                        "google_sync_alert_values",
                        return_value=[list(sync.ALERT_COLUMNS)],
                    ),
                    mock.patch.object(
                        apply_approvals,
                        "validate_current_catalogue",
                        return_value=None,
                    ),
                    mock.patch.object(
                        sync, "load_server_configs", return_value=[config]
                    ),
                    mock.patch.object(
                        apply_approvals,
                        "_fetch_provider_inventories",
                        return_value=(inventory,),
                    ),
                    mock.patch.object(
                        sync,
                        "revalidate_provider_identity_updates",
                        return_value=([], provider_stats),
                    ),
                ):
                    with self.assertRaisesRegex(
                        ContractError, "provider identity revalidation rejected"
                    ):
                        apply_approvals.apply_live_approval_batch(
                            approval_file=Path("approval.json"),
                            bundle_dir=Path("bundle"),
                            mappings_csv=Path("Mappings.csv"),
                            alerts_csv=Path("Sync Alerts.csv"),
                            all_source_file=Path("ALL_SOURCES1.xml.gz"),
                            all_source_catalog_file=Path("ALL_SOURCES1.txt"),
                            google_session=session,
                            sheet_id="s" * 20,
                            sheet_tab="Mappings",
                            alerts_tab="Sync Alerts",
                            confirm_approval_id=APPROVAL_ID,
                        )
                self.assertEqual(session.posts, 0)

    def test_open_alert_blocks_selected_approval(self) -> None:
        row = mapping_row("1")
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row)]),
            mapping_table([row]),
            prepared_at=PREPARED_AT,
        )
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "server_id": "server_1",
                "stream_id": "1",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )
        values = [list(sync.ALERT_COLUMNS)] + [
            [alert[column] for column in sync.ALERT_COLUMNS]
        ]
        with self.assertRaisesRegex(ContractError, "OPEN Sync Alert"):
            apply_approvals._require_no_open_alerts(values, prepared)

    def test_uncertain_post_is_success_only_when_authoritative_read_confirms(self) -> None:
        row = mapping_row("1")
        prepared = apply_approvals.prepare_approved_rows(
            approval_document([proposal(row)]),
            mapping_table([row]),
            prepared_at=PREPARED_AT,
        )
        desired = prepared.selected[0].desired_row
        after_values = [list(streaming.SHEET_COLUMNS)] + [
            [desired[column] for column in streaming.SHEET_COLUMNS]
        ]

        class Session:
            def post(self, _url: str, **_kwargs: object) -> object:
                raise TimeoutError("response lost after commit")

        with mock.patch.object(sync, "google_sheet_values", return_value=after_values):
            committed, _observed = apply_approvals._post_atomic_human_batch(
                Session(),
                sheet_id="s" * 20,
                tab_name="Mappings",
                before_table=mapping_table([row]),
                batch=prepared,
                numeric_sheet_id=123,
            )
        self.assertEqual(committed, 1)

    def test_catalogue_gate_failure_prevents_any_post(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        before_table = mapping_table([row])
        before_values = [list(streaming.SHEET_COLUMNS)] + [
            [row[column] for column in streaming.SHEET_COLUMNS]
        ]
        validation = apply_approvals.approvals.ApprovalResult(
            approval_file=Path("approval.json"),
            approval_id=APPROVAL_ID,
            content_sha256=CONTENT_SHA,
            run_id=RUN_ID,
            approved_at=APPROVED_AT,
            proposal_count=1,
        )

        class Session:
            def __init__(self) -> None:
                self.posts = 0

            def post(self, _url: str, **_kwargs: object) -> object:
                self.posts += 1
                raise AssertionError("catalogue failure must precede POST")

        session = Session()
        with (
            mock.patch.object(apply_approvals, "_canonical_now", return_value=PREPARED_AT),
            mock.patch.object(
                apply_approvals,
                "_validate_approval_exact",
                return_value=(validation, document),
            ),
            mock.patch.object(
                apply_approvals, "_read_exact_snapshot", return_value=b"snapshot"
            ),
            mock.patch.object(sync, "parse_mapping_csv", return_value=before_table),
            mock.patch.object(sync, "google_sheet_values", return_value=before_values),
            mock.patch.object(
                sync,
                "google_sync_alert_values",
                return_value=[list(sync.ALERT_COLUMNS)],
            ),
            mock.patch.object(
                apply_approvals,
                "validate_current_catalogue",
                side_effect=ContractError("current programme gate failed"),
            ),
        ):
            with self.assertRaisesRegex(ContractError, "programme gate failed"):
                apply_approvals.apply_live_approval_batch(
                    approval_file=Path("approval.json"),
                    bundle_dir=Path("bundle"),
                    mappings_csv=Path("Mappings.csv"),
                    alerts_csv=Path("Sync Alerts.csv"),
                    all_source_file=Path("ALL_SOURCES1.xml.gz"),
                    all_source_catalog_file=Path("ALL_SOURCES1.txt"),
                    google_session=session,
                    sheet_id="s" * 20,
                    sheet_tab="Mappings",
                    alerts_tab="Sync Alerts",
                    confirm_approval_id=APPROVAL_ID,
                )
        self.assertEqual(session.posts, 0)

    def test_current_catalogue_rejects_text_hash_drift(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        batch = apply_approvals.prepare_approved_rows(
            document, mapping_table([row]), prepared_at=PREPARED_AT
        )
        manifest = {
            "input_sha256": {
                "EPG_XML": "1" * 64,
                "EPG_XML_CATALOG": "2" * 64,
                "EPG_TEXT": "3" * 64,
                "EPG_TEXT_CATALOG": "4" * 64,
            }
        }
        with (
            mock.patch.object(
                apply_approvals, "_bundle_manifest", return_value=manifest
            ),
            mock.patch.object(
                apply_approvals,
                "read_stable_regular_file",
                return_value=(b"text", "5" * 64),
            ),
        ):
            with self.assertRaisesRegex(ContractError, "text file differs"):
                apply_approvals.validate_current_catalogue(
                    batch,
                    document,
                    bundle_dir=Path("bundle"),
                    all_source_file=Path("guide.xml.gz"),
                    all_source_catalog_file=Path("guide.txt"),
                    as_of=PREPARED_AT,
                )

    def test_current_catalogue_rejects_xml_hash_drift(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        batch = apply_approvals.prepare_approved_rows(
            document, mapping_table([row]), prepared_at=PREPARED_AT
        )
        epg_id = batch.selected[0].desired_row["epg_id"]
        manifest = {
            "input_sha256": {
                "EPG_XML": "1" * 64,
                "EPG_XML_CATALOG": "2" * 64,
                "EPG_TEXT": "3" * 64,
                "EPG_TEXT_CATALOG": "4" * 64,
            }
        }
        text_catalog = SimpleNamespace(
            generated_token="20260918010000",
            entries=(SimpleNamespace(epg_id=epg_id, kind="real"),),
            fingerprint_sha256="4" * 64,
            casefold_collision_keys=frozenset(),
            validate_for_unattended_matching=lambda: None,
        )
        one_pass = SimpleNamespace(source_sha256="5" * 64)
        with (
            mock.patch.object(
                apply_approvals, "_bundle_manifest", return_value=manifest
            ),
            mock.patch.object(
                apply_approvals,
                "read_stable_regular_file",
                return_value=(b"text", "3" * 64),
            ),
            mock.patch.object(
                apply_approvals.catalog_stream,
                "parse_all_sources_text",
                return_value=text_catalog,
            ),
            mock.patch.object(
                apply_approvals.automatch,
                "_validate_text_catalog_generation",
                return_value=0,
            ),
            mock.patch.object(
                apply_approvals.catalog_stream,
                "stream_catalog_and_programmes_once",
                return_value=one_pass,
            ),
        ):
            with self.assertRaisesRegex(ContractError, "XML differs"):
                apply_approvals.validate_current_catalogue(
                    batch,
                    document,
                    bundle_dir=Path("bundle"),
                    all_source_file=Path("guide.xml.gz"),
                    all_source_catalog_file=Path("guide.txt"),
                    as_of=PREPARED_AT,
                )

    def test_current_catalogue_rejects_decayed_programme_gate(self) -> None:
        row = mapping_row("1")
        document = approval_document([proposal(row)])
        batch = apply_approvals.prepare_approved_rows(
            document, mapping_table([row]), prepared_at=PREPARED_AT
        )
        epg_id = batch.selected[0].desired_row["epg_id"]
        manifest = {
            "input_sha256": {
                "EPG_XML": "1" * 64,
                "EPG_XML_CATALOG": "2" * 64,
                "EPG_TEXT": "3" * 64,
                "EPG_TEXT_CATALOG": "4" * 64,
            }
        }
        text_catalog = SimpleNamespace(
            generated_token="20260918010000",
            entries=(SimpleNamespace(epg_id=epg_id, kind="real"),),
            fingerprint_sha256="4" * 64,
            casefold_collision_keys=frozenset(),
            validate_for_unattended_matching=lambda: None,
        )
        xml_catalog = SimpleNamespace(
            fingerprint_sha256="2" * 64,
            casefold_collision_keys=frozenset(),
            exact=lambda value: SimpleNamespace(epg_id=value),
        )
        one_pass = SimpleNamespace(
            source_sha256="1" * 64,
            catalog=xml_catalog,
            unresolved_requested_ids=(),
            resolved_source_ids={epg_id: epg_id},
            programme_gates={epg_id: SimpleNamespace(passed=False)},
        )
        with (
            mock.patch.object(
                apply_approvals, "_bundle_manifest", return_value=manifest
            ),
            mock.patch.object(
                apply_approvals,
                "read_stable_regular_file",
                return_value=(b"text", "3" * 64),
            ),
            mock.patch.object(
                apply_approvals.catalog_stream,
                "parse_all_sources_text",
                return_value=text_catalog,
            ),
            mock.patch.object(
                apply_approvals.automatch,
                "_validate_text_catalog_generation",
                return_value=0,
            ),
            mock.patch.object(
                apply_approvals.catalog_stream,
                "stream_catalog_and_programmes_once",
                return_value=one_pass,
            ),
        ):
            with self.assertRaisesRegex(ContractError, "programme gate"):
                apply_approvals.validate_current_catalogue(
                    batch,
                    document,
                    bundle_dir=Path("bundle"),
                    all_source_file=Path("guide.xml.gz"),
                    all_source_catalog_file=Path("guide.txt"),
                    as_of=PREPARED_AT,
                )

    def test_adjacent_mapping_and_alert_read_uses_one_batch_get(self) -> None:
        mapping_values = [["server_id"], ["server_1"]]
        alert_values = [["detected_at"]]

        class Response:
            status_code = 200
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.content = json.dumps(
                    {
                        "valueRanges": [
                            {"range": "Mappings!A1:AG2", "values": mapping_values},
                            {
                                "range": "'Sync Alerts'!A1:K1",
                                "values": alert_values,
                            },
                        ]
                    }
                ).encode("utf-8")

            def close(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.gets: list[dict[str, object]] = []

            def get(self, url: str, **kwargs: object) -> Response:
                self.gets.append({"url": url, **kwargs})
                return Response()

        session = Session()
        actual_mapping, actual_alerts = (
            apply_approvals._google_mapping_and_alert_values(
                session, "s" * 20, "Mappings", "Sync Alerts"
            )
        )
        self.assertEqual(actual_mapping, mapping_values)
        self.assertEqual(actual_alerts, alert_values)
        self.assertEqual(len(session.gets), 1)
        self.assertEqual(len(session.gets[0]["params"]["ranges"]), 2)


if __name__ == "__main__":
    unittest.main()
