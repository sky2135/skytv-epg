from __future__ import annotations

import csv
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import matching_lab.dummy_shadow as dummy_shadow_module
from matching_lab.compat import automatch, streaming, sync
from matching_lab.dummy_shadow import _saved_inventory, run_dummy_shadow
from matching_lab.models import sha256_json


AS_OF = "2026-09-15T12:00:00Z"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _mapping_row(
    stream_id: str,
    *,
    action: str = "REVIEW",
    enabled: str = "FALSE",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "stream_id": stream_id,
            "enabled": enabled,
            "channel_name": f"Event Slot {stream_id}",
            "canonical_name": f"Event Slot {stream_id}",
            "category_id": "events",
            "category_name": "US | Events",
            "action": action,
            "source": "epgshare01" if action == "REVIEW" else "dummy",
            "epg_feed": "ALL_SOURCES1" if action == "REVIEW" else "DUMMY_CHANNELS",
            "epg_id": "" if action == "REVIEW" else "PPV.EVENTS.Dummy.us",
            "metadata_status": "review",
            "metadata_locked": "FALSE",
        }
    )
    return row


def _alert(stream_id: str) -> dict[str, str]:
    row = {column: "" for column in sync.ALERT_COLUMNS}
    row.update(
        {
            "detected_at": AS_OF,
            "server_id": "server_1",
            "stream_id": stream_id,
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "status": "OPEN",
        }
    )
    return row


def _write_inputs(root: Path) -> tuple[Path, Path, Path, Path]:
    mappings = root / "mappings.csv"
    alerts = root / "alerts.csv"
    source = root / "all.xml.gz"
    catalog = root / "all.txt"
    mappings.write_bytes(
        _csv_bytes(
            tuple(streaming.SHEET_COLUMNS),
            [
                _mapping_row("101"),
                _mapping_row("102"),
                _mapping_row("999", action="AUTO_DUMMY", enabled="TRUE"),
            ],
        )
    )
    alerts.write_bytes(_csv_bytes(tuple(sync.ALERT_COLUMNS), [_alert("101")]))
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?><tv>"
        '<channel id="Good.Channel.us2"><display-name>Good</display-name></channel>'
        '<channel id="ESPN+.Dummy.us"><display-name>ESPN Plus Events</display-name></channel>'
        '<channel id="PPV.EVENTS.Dummy.us"><display-name>Events</display-name></channel>'
        "</tv>"
    ).encode("utf-8")
    with source.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
            handle.write(xml)
    catalog.write_text(
        "20260915120000\n"
        "-- epg_ripper_US2 --\n"
        "Good.Channel.us2\n"
        "-- epg_ripper_DUMMY_CHANNELS --\n"
        "ESPN+.Dummy.us\n"
        "PPV.EVENTS.Dummy.us\n",
        encoding="utf-8",
    )
    return mappings, alerts, source, catalog


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        resolver=object(),
        identity=SimpleNamespace(
            version="8.4",
            build_id="TEST-BUILD",
            source_sha256=HASH_A,
            engine_source_sha256=HASH_B,
        ),
        preflight=SimpleNamespace(ready=True),
        approved_aliases_sha256=HASH_C,
        schedule_equivalences_sha256=HASH_D,
    )


def _proposals(*args, **kwargs):
    del args
    server_id = kwargs["server_id"]
    return {
        (server_id, stream_id): SimpleNamespace(
            server_id=server_id,
            stream_id=stream_id,
            channel_name=f"Event Slot {stream_id}",
            category_name="US | Events",
            matcher_action="AUTO_DUMMY",
            target_source="dummy",
            target_feed="DUMMY_CHANNELS",
            target_epg_id="PPV.EVENTS.Dummy.us",
            match_method="safety_rule",
            matcher_reason="Verified event slot with no stable schedule",
            second_epg_id="",
            eligible_for_finalization=True,
        )
        for _target_server, stream_id in kwargs["target_keys"]
    }


class DummyShadowTests(unittest.TestCase):
    def test_saved_category_id_label_collisions_are_split_not_merged(self) -> None:
        first = _mapping_row("201")
        second = _mapping_row("202")
        second["category_name"] = "US | Different Events"
        channels, categories = _saved_inventory([first, second], "server_1")
        self.assertEqual(len(categories), 2)
        self.assertNotEqual(channels[0]["category_id"], channels[1]["category_id"])
        self.assertEqual(
            {categories[channel["category_id"]] for channel in channels},
            {"US | Events", "US | Different Events"},
        )

    def test_saved_inventory_canonicalizes_display_whitespace(self) -> None:
        row = _mapping_row("203")
        row["channel_name"] = "Next | Cyclisme\N{NO-BREAK SPACE}: Grand Prix"
        row["category_name"] = "US\N{NO-BREAK SPACE}| Events"
        channels, categories = _saved_inventory([row], "server_1")
        self.assertEqual(channels[0]["name"], "Next | Cyclisme : Grand Prix")
        self.assertEqual(categories[channels[0]["category_id"]], "US | Events")

    def test_review_only_exact_bank_can_be_recorded_without_live_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings, alerts, source, catalog = _write_inputs(root)
            exact_rows = [_mapping_row("101"), _mapping_row("102")]
            for row in exact_rows:
                row["channel_name"] = "(US) ESPN PLAY 1"
                row["canonical_name"] = "(US) ESPN PLAY 1"
                row["category_name"] = "SPORTS | ESPN+"
            mappings.write_bytes(
                _csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [*exact_rows, _mapping_row("999", action="AUTO_DUMMY", enabled="TRUE")],
                )
            )

            def review_only_proposals(*args, **kwargs):
                del args
                server_id = kwargs["server_id"]
                return {
                    (server_id, stream_id): SimpleNamespace(
                        server_id=server_id,
                        stream_id=stream_id,
                        channel_name="(US) ESPN PLAY 1",
                        category_name="SPORTS | ESPN+",
                        matcher_action="AUTO_DUMMY",
                        target_source="dummy",
                        target_feed="DUMMY_CHANNELS",
                        target_epg_id="ESPN+.Dummy.us",
                        match_method="exact_numbered_event_bank",
                        matcher_reason="Exact audited ESPN PLAY numbered event bank",
                        second_epg_id="",
                        eligible_for_finalization=False,
                    )
                    for _target_server, stream_id in kwargs["target_keys"]
                }

            with (
                mock.patch.object(
                    automatch, "prepare_matcher_runtime", return_value=_runtime()
                ),
                mock.patch.object(
                    automatch,
                    "propose_new_channel_matches",
                    side_effect=review_only_proposals,
                ),
                mock.patch.object(
                    automatch,
                    "finalize_proposal",
                    return_value=SimpleNamespace(approved=False),
                ),
                mock.patch.object(
                    dummy_shadow_module,
                    "_has_exact_numbered_event_bank_evidence",
                    return_value=True,
                ) as exact_check,
            ):
                result = run_dummy_shadow(
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    all_source_file=source,
                    all_source_catalog_file=catalog,
                    output_dir=root / "review-only",
                    as_of=AS_OF,
                    minimum_unique_channels=2,
                )
            self.assertEqual(result.classification_count, 2)
            self.assertEqual(exact_check.call_count, 2)

    def test_private_sidecar_is_deterministic_and_alerts_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings, alerts, source, catalog = _write_inputs(root)
            outputs = (root / "first", root / "second")
            results = []
            with (
                mock.patch.object(
                    automatch, "prepare_matcher_runtime", return_value=_runtime()
                ),
                mock.patch.object(
                    automatch,
                    "propose_new_channel_matches",
                    side_effect=_proposals,
                ),
                mock.patch.object(
                    automatch,
                    "finalize_proposal",
                    return_value=SimpleNamespace(approved=True),
                ),
                mock.patch.object(
                    sync,
                    "update_google_sheet_review_rows",
                    side_effect=AssertionError("dummy-shadow attempted a Sheet write"),
                ) as writer,
            ):
                for output in outputs:
                    results.append(
                        run_dummy_shadow(
                            mappings_csv=mappings,
                            alerts_csv=alerts,
                            all_source_file=source,
                            all_source_catalog_file=catalog,
                            output_dir=output,
                            as_of=AS_OF,
                            minimum_unique_channels=2,
                        )
                    )
            writer.assert_not_called()
            self.assertEqual(results[0].run_id, results[1].run_id)
            self.assertEqual(results[0].classification_count, 2)
            self.assertEqual(results[0].counts["DUMMY_BLOCKED_ALERT"], 1)
            self.assertEqual(results[0].counts["DUMMY_UNBLOCKED"], 1)
            for name in ("classifications.jsonl", "summary.json", "manifest.json"):
                self.assertEqual(
                    (outputs[0] / name).read_bytes(),
                    (outputs[1] / name).read_bytes(),
                )

            records = [
                json.loads(line)
                for line in (outputs[0] / "classifications.jsonl")
                .read_text("utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["decision"]["state"] for record in records],
                ["BLOCKED_ALERT", "CLASSIFIED"],
            )
            self.assertEqual(
                [record["identity"]["stream_id"] for record in records],
                ["101", "102"],
            )
            for record in records:
                unsigned = dict(record)
                classification_id = unsigned.pop("classification_id")
                self.assertEqual(classification_id, sha256_json(unsigned))
                self.assertFalse(record["decision"]["apply_eligible"])
                self.assertFalse(record["decision"]["write_authority"])
                self.assertEqual(record["decision"]["source"], "dummy")
                self.assertEqual(
                    record["decision"]["epg_id"], "PPV.EVENTS.Dummy.us"
                )
            self.assertIn(
                "OPEN_SYNC_ALERT", records[0]["decision"]["reason_codes"]
            )
            self.assertNotIn(
                "OPEN_SYNC_ALERT", records[1]["decision"]["reason_codes"]
            )
            manifest = json.loads((outputs[0] / "manifest.json").read_text("utf-8"))
            self.assertTrue(manifest["private_artifact"])
            self.assertFalse(manifest["write_authority"])
            self.assertNotIn("proposals.jsonl", {path.name for path in outputs[0].iterdir()})

    def test_non_dummy_or_unverified_target_is_not_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings, alerts, source, catalog = _write_inputs(root)

            def unsafe_proposals(*args, **kwargs):
                produced = _proposals(*args, **kwargs)
                return {
                    key: SimpleNamespace(
                        **{
                            **vars(proposal),
                            "target_epg_id": "Good.Channel.us2",
                        }
                    )
                    for key, proposal in produced.items()
                }

            with (
                mock.patch.object(
                    automatch, "prepare_matcher_runtime", return_value=_runtime()
                ),
                mock.patch.object(
                    automatch,
                    "propose_new_channel_matches",
                    side_effect=unsafe_proposals,
                ),
                mock.patch.object(
                    automatch,
                    "finalize_proposal",
                    return_value=SimpleNamespace(approved=True),
                ),
            ):
                result = run_dummy_shadow(
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    all_source_file=source,
                    all_source_catalog_file=catalog,
                    output_dir=root / "unsafe",
                    as_of=AS_OF,
                    minimum_unique_channels=2,
                )
            self.assertEqual(result.classification_count, 0)
            self.assertEqual(result.counts["DUMMY_REJECTED"], 2)
            self.assertEqual((root / "unsafe" / "classifications.jsonl").read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
