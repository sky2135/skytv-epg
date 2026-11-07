from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tivimate_guide_health as health  # noqa: E402


NOW = 1_800_000_000


def row(stream="1", **overrides):
    values = {
        "server_id": "server_1", "stream_id": stream,
        "channel_name": "UK || SKY SPORTS FOOTBALL", "action": "APPROVED",
        "enabled": True, "runtime_eligible": True, "uses_local_synthetic": False,
        "requested_source": "epgshare01", "effective_source": "epgshare01",
        "epg_feed": "UK1", "epg_id": "Sky.Sports.Football.HD.uk",
        "schedule_channel_key": "Sky.Sports.Football.HD.uk",
        "source_key": "epgshare01", "schedule_key": "UK1::Sky.Sports.Football.HD.uk",
        "provider_name_aliases": (), "provider_epg_id_aliases": (),
        "source_policy_blocked": False, "reason": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def stats(**overrides):
    values = {
        "programme_rows": 12, "future_rows": 8, "current_rows": 1,
        "earliest_start": NOW - 3_600, "latest_stop": NOW + 86_400,
        "latest_future_stop": NOW + 86_400,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def entry(channel_key="Sky.Sports.Football.HD.uk", source_key="epgshare01"):
    return SimpleNamespace(source_key=source_key, channel_key=channel_key)


class GuideLinkHealthTests(unittest.TestCase):
    def build(self, rows, entries=None, schedule_stats=None, provider_links=None):
        return health.build_guide_link_health(
            rows=rows,
            entries=entries or {},
            schedule_stats=schedule_stats or {},
            generated_at=NOW,
            provider_links=provider_links,
        )

    def test_actual_exported_provider_id_proves_link_and_real_current_schedule(self):
        channel = row(provider_epg_id_aliases=("provider-football",))
        summary, records = self.build(
            [channel],
            entries={channel.channel_name: entry(), "provider-football": entry()},
            schedule_stats={(channel.source_key, channel.schedule_channel_key): stats()},
        )
        self.assertEqual(summary["mappingRows"], 1)
        self.assertEqual(summary["realCurrentRows"], 1)
        self.assertEqual(summary["realCurrentOrUpcomingRows"], 1)
        self.assertEqual(summary["placeholderRows"], 0)
        self.assertEqual(records[0]["outcome"], "real_current")
        self.assertEqual(records[0]["provider_epg_id_status"], "bound")
        self.assertEqual(records[0]["upcoming_programme_rows"], 7)
        self.assertFalse(records[0]["requires_attention"])

    def test_programme_counts_do_not_prove_alias_survived_conflict_resolution(self):
        channel = row(provider_epg_id_aliases=("provider-shared",))
        summary, records = self.build(
            [channel], entries={
                channel.epg_id: entry(),
                channel.channel_name: entry("4seven.uk"),
                "provider-shared": entry("4seven.uk"),
            },
            schedule_stats={(channel.source_key, channel.schedule_channel_key): stats()},
        )
        report = records[0]
        self.assertEqual(report["channel_name_binding"], "other_schedule")
        self.assertEqual(report["provider_epg_id_status"], "wrong_schedule")
        self.assertEqual(report["conflicting_id_count"], 2)
        self.assertEqual(summary["requiresAttentionRows"], 1)
        self.assertIn("channel_name_points_elsewhere", report["issues"])
        self.assertIn("provider_id_wrong_schedule", report["issues"])

    def test_synthetic_programmes_are_never_counted_as_real_coverage(self):
        channel = row(
            uses_local_synthetic=True, source_key="synthetic:one",
            epg_id="Synthetic.Event.local", schedule_channel_key="Synthetic.Event.local",
            requested_source="dummy", effective_source="dummy",
        )
        summary, records = self.build(
            [channel],
            entries={channel.channel_name: entry("Synthetic.Event.local", "synthetic:one")},
            schedule_stats={(channel.source_key, channel.schedule_channel_key): stats()},
        )
        self.assertEqual(summary["realScheduleRows"], 0)
        self.assertEqual(summary["realCurrentRows"], 0)
        self.assertEqual(summary["placeholderRows"], 1)
        self.assertEqual(records[0]["guide_kind"], "placeholder")
        self.assertEqual(records[0]["outcome"], "placeholder")

    def test_all_mapping_rows_are_reported_even_when_not_exported(self):
        disabled = row("1", enabled=False, runtime_eligible=False)
        review = row("2", action="REVIEW", enabled=False, runtime_eligible=False)
        policy = row("3", source_policy_blocked=True, runtime_eligible=False)
        ignored = row("4", action="IGNORE", runtime_eligible=False)
        quarantine = row(
            "5", action="REVIEW", runtime_eligible=False,
            reason="Effective snapshot quarantine: identity review",
        )
        missing = row("6", epg_id="", runtime_eligible=False)
        summary, records = self.build([disabled, review, policy, ignored, quarantine, missing])
        self.assertEqual(summary["mappingRows"], 6)
        self.assertEqual(summary["omittedRows"], 6)
        self.assertEqual(summary["exportedRows"], 0)
        self.assertEqual([r["omitted_reason"] for r in records], [
            "disabled", "awaiting_mapping_review", "source_policy_blocked",
            "explicit_ignore", "identity_quarantine", "missing_epg_id",
        ])
        self.assertFalse(any(r["requires_attention"] for r in records))

    def test_expired_programmes_are_separate_from_missing_and_current_real(self):
        expired = row("1")
        missing = row("2", schedule_channel_key="Missing.uk", epg_id="Missing.uk")
        summary, records = self.build(
            [expired, missing], entries={expired.channel_name: entry()},
            schedule_stats={
                (expired.source_key, expired.schedule_channel_key): stats(
                    future_rows=0, current_rows=0, latest_stop=NOW,
                    latest_future_stop=0,
                ),
            },
        )
        self.assertEqual(summary["realScheduleRows"], 1)
        self.assertEqual(summary["realCurrentOrUpcomingRows"], 0)
        self.assertEqual(summary["expiredScheduleRows"], 1)
        self.assertEqual(summary["missingScheduleRows"], 1)
        self.assertEqual([r["outcome"] for r in records], ["real_expired", "missing_schedule"])
        self.assertEqual(summary["requiresAttentionRows"], 2)

    def test_does_not_infer_current_programme_from_a_range_with_possible_gaps(self):
        channel = row()
        unmeasured = stats()
        del unmeasured.current_rows
        summary, records = self.build(
            [channel], entries={channel.channel_name: entry()},
            schedule_stats={(channel.source_key, channel.schedule_channel_key): unmeasured},
        )
        self.assertEqual(records[0]["schedule_status"], "current_or_upcoming")
        self.assertIsNone(records[0]["current_programme_rows"])
        self.assertIsNone(records[0]["upcoming_programme_rows"])
        self.assertEqual(summary["realCurrentRows"], 0)
        self.assertEqual(summary["currentStatusMeasuredRows"], 0)

    def test_upcoming_only_is_reported_without_claiming_current_coverage(self):
        channel = row()
        _, records = self.build(
            [channel], entries={channel.channel_name: entry()},
            schedule_stats={(channel.source_key, channel.schedule_channel_key): stats(current_rows=0)},
        )
        self.assertEqual(records[0]["schedule_status"], "upcoming")
        self.assertEqual(records[0]["outcome"], "real_upcoming")
        self.assertEqual(records[0]["upcoming_programme_rows"], 8)
        self.assertIn("no_current_programme", records[0]["issues"])
        self.assertTrue(records[0]["requires_attention"])

    def test_observed_missing_id_differs_from_unavailable_inventory(self):
        channels = [row(str(n)) for n in range(1, 5)]
        summary, records = self.build(channels, provider_links={
            ("server_1", "1"): {"status": "name_only"},
            ("server_1", "2"): {"status": "unavailable"},
            ("server_1", "3"): {"status": "name_mismatch"},
        })
        self.assertEqual([r["provider_epg_id_status"] for r in records], [
            "missing", "not_observed", "unverified", "not_observed",
        ])
        self.assertEqual(summary["providerIdStatuses"]["not_observed"], 2)

    def test_verified_provider_id_missing_from_xml_is_actionable(self):
        channel = row()
        summary, records = self.build(
            [channel], entries={channel.channel_name: entry()},
            schedule_stats={(channel.source_key, channel.schedule_channel_key): stats()},
            provider_links={("server_1", "1"): {
                "provider_epg_id": "provider-football", "status": "verified",
            }},
        )
        self.assertEqual(records[0]["provider_epg_id_status"], "not_exported")
        self.assertEqual(summary["requiresAttentionRows"], 1)

    def test_shared_id_for_different_schedules_is_ambiguous_even_if_one_wins(self):
        first = row("1", provider_epg_id_aliases=("shared",))
        second = row(
            "2", provider_epg_id_aliases=("shared",),
            channel_name="UK || SKY SPORTS PLUS", epg_id="SkySportsPlus.uk",
            schedule_channel_key="SkySportsPlus.uk",
        )
        _, records = self.build(
            [first, second], entries={"shared": entry()},
            schedule_stats={
                (first.source_key, first.schedule_channel_key): stats(),
                (second.source_key, second.schedule_channel_key): stats(),
            },
        )
        self.assertEqual([r["provider_epg_id_status"] for r in records], ["ambiguous", "ambiguous"])

    def test_provider_evidence_cannot_leak_transport_urls_or_credentials(self):
        channel = row(channel_name="News https://provider.invalid/live/u/secret/1")
        _, records = self.build([channel], provider_links={
            ("server_1", "1"): {
                "provider_epg_id": "https://provider.invalid/xml?username=user&password=secret",
                "status": "invalid_id",
            },
        })
        encoded = json.dumps(records)
        self.assertNotIn("provider.invalid", encoded)
        self.assertNotIn("secret", encoded)
        self.assertEqual(records[0]["provider_epg_id_status"], "invalid")

    def test_exact_identifier_whitespace_is_retained_in_diagnostics(self):
        channel = row(channel_name="IN:  Aaj_Tak", provider_epg_id_aliases=("Native:  Aaj_Tak",))
        _, records = self.build([channel], entries={"Native:  Aaj_Tak": entry()})
        self.assertEqual(records[0]["channel_name"], "IN:  Aaj_Tak")
        self.assertEqual(records[0]["provider_epg_id"], "Native:  Aaj_Tak")
        self.assertEqual(records[0]["provider_epg_id_status"], "bound")
        csv_records = list(health.csv_rows(records))
        self.assertEqual(len(csv_records[0]), len(health.CSV_HEADERS))
        self.assertEqual(csv_records[0][health.CSV_HEADERS.index("provider_epg_id")], "Native:  Aaj_Tak")

    def test_duplicate_stream_identity_fails_instead_of_hiding_a_row(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.build([row("1"), row("1")])

    def test_empty_report_has_clear_zero_counts(self):
        summary, records = self.build([])
        self.assertEqual(records, [])
        self.assertEqual(summary["mappingRows"], 0)
        self.assertEqual(summary["omittedRows"], 0)
        self.assertEqual(summary["eligibleRows"], 0)
        self.assertEqual(summary["realCurrentRows"], 0)
        self.assertEqual(summary["servers"], [])


if __name__ == "__main__":
    unittest.main()
