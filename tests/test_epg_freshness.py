from __future__ import annotations

import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as builder
import check_epg_freshness as checker

NOW = 1_800_000_000


def publication(*, real_stop=NOW + 12 * 3600, generated=NOW):
    health = {"status": "ok", "generatedAt": generated, "mappingSha256": "a" * 64,
              "serverCount": 1, "servers": 1, "unavailableNativePanelServers": [],
              "inventoryAvailability": {"status": "ok", "checkedServers": ["server_1"], "unavailableServers": []}}
    horizon = int(real_stop >= generated + 6 * 3600)
    index = {"generatedAt": generated, "mappingSha256": "a" * 64, "builds": [{
        "serverId": "server_1", "futureProgrammeCoverage": {
            "schemaVersion": 1, "measuredAt": generated, "minimumFutureSeconds": 6 * 3600,
            "eligibleStreams": 1001, "realStreamsWithFutureProgrammes": 1,
            "realStreamsReachingMinimumHorizon": horizon, "realLatestStop": real_stop,
            "sources": {
                "epgshare": {"mappedStreams": 1, "streamsWithFutureProgrammes": 1,
                             "streamsReachingMinimumHorizon": horizon, "latestStop": real_stop},
                "nativePanel": {"mappedStreams": 0, "streamsWithFutureProgrammes": 0,
                                "streamsReachingMinimumHorizon": 0, "latestStop": None},
                "localSynthetic": {"mappedStreams": 1000, "streamsWithFutureProgrammes": 1000,
                                   "streamsReachingMinimumHorizon": 1000, "latestStop": NOW + 14 * 86400},
            }},
    }]}
    return health, index


class FreshnessTests(unittest.TestCase):
    def check(self, health, index):
        return checker.check_payloads(health, index, now_epoch=NOW, expected_servers=["server_1"])

    def test_stale_success_and_long_placeholders_do_not_mask_expired_real_guide(self):
        health, index = publication(real_stop=NOW - 1, generated=NOW - 5 * 86400)
        result = self.check(health, index)
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("older" in issue for issue in result["errors"]))
        self.assertTrue(any("horizon" in issue for issue in result["errors"]))

    def test_horizon_is_measured_against_check_time(self):
        health, index = publication(real_stop=NOW + 5 * 3600)
        self.assertEqual(self.check(health, index)["status"], "error")
        health, index = publication(real_stop=NOW + 6 * 3600)
        self.assertEqual(self.check(health, index)["status"], "ok")

    def test_partial_provider_status_is_separate_from_freshness_errors(self):
        health, index = publication()
        health.update(status="degraded", inventoryAvailability={"status": "partial", "checkedServers": ["server_1"], "unavailableServers": ["server_2"]})
        result = self.check(health, index)
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["errors"])

    def test_missing_server_or_mixed_snapshot_fails(self):
        health, index = publication()
        index["mappingSha256"] = "different"
        self.assertEqual(self.check(health, index)["status"], "error")
        index["builds"] = []
        self.assertTrue(any("absent" in value for value in self.check(health, index)["errors"]))

    def test_missing_hashes_bool_counts_and_invalid_availability_fail_closed(self):
        for mutation in ("missing_hashes", "bad_hash", "bool_count", "negative_count",
                         "count_endpoint_conflict", "missing_availability", "unknown_server",
                         "unknown_availability", "bool_server_count", "missing_provider_check"):
            with self.subTest(mutation=mutation):
                health, index = publication()
                if mutation == "missing_hashes":
                    del health["mappingSha256"]
                    del index["mappingSha256"]
                elif mutation == "bad_hash":
                    health["mappingSha256"] = index["mappingSha256"] = "not-a-sha"
                elif mutation == "bool_count":
                    index["builds"][0]["futureProgrammeCoverage"]["sources"]["epgshare"]["streamsWithFutureProgrammes"] = True
                elif mutation == "negative_count":
                    index["builds"][0]["futureProgrammeCoverage"]["sources"]["epgshare"]["streamsWithFutureProgrammes"] = -1
                elif mutation == "count_endpoint_conflict":
                    index["builds"][0]["futureProgrammeCoverage"]["sources"]["nativePanel"]["latestStop"] = NOW + 3600
                elif mutation == "missing_availability":
                    del health["inventoryAvailability"]
                elif mutation == "unknown_server":
                    health["inventoryAvailability"]["checkedServers"] = ["server_4"]
                elif mutation == "unknown_availability":
                    health["inventoryAvailability"] = {"status": "not_provided", "checkedServers": [], "unavailableServers": []}
                elif mutation == "missing_provider_check":
                    health["inventoryAvailability"]["checkedServers"] = []
                else:
                    health["serverCount"] = True
                self.assertEqual(self.check(health, index)["status"], "error")

    def test_inventory_diagnostics_are_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            path.write_text(json.dumps({"servers": {"server_1": {"source": "secret"}},
                                        "provider_failures": {"server_2": "password=secret"}}))
            result = builder.read_inventory_availability(path)
            self.assertEqual(result, {"status": "partial", "checkedServers": ["server_1"],
                                      "unavailableServers": ["server_2"]})
            self.assertNotIn("secret", json.dumps(result))
            path.write_text(json.dumps({"provider_failures": {"https://secret": "sensitive"}}))
            with self.assertRaises(builder.BuildError):
                builder.read_inventory_availability(path)

    def test_future_counts_separate_native_external_and_placeholders(self):
        def row(stream, source, synthetic=False):
            return SimpleNamespace(runtime_eligible=True, uses_local_synthetic=synthetic,
                                   effective_source=source, source_key=source,
                                   epg_id=stream)
        rows = [row("external", "epgshare01"), row("expired", "epgshare01"),
                row("native", "panel"), row("placeholder", "synthetic", True)]
        stats = {
            ("epgshare01", "external"): builder.ScheduleStats(2, 1, NOW - 3600, NOW + 12 * 3600, NOW + 12 * 3600, 0),
            ("epgshare01", "expired"): builder.ScheduleStats(2, 0, NOW - 3600, NOW - 1, 0, 0),
            ("panel", "native"): builder.ScheduleStats(2, 1, NOW, NOW + 3 * 3600, NOW + 3 * 3600, 0),
            ("synthetic", "placeholder"): builder.ScheduleStats(2, 2, NOW, NOW + 14 * 86400, NOW + 14 * 86400, 0),
        }
        result = builder.build_future_programme_coverage(rows, stats, now_epoch=NOW)
        self.assertEqual(result["realStreamsWithFutureProgrammes"], 2)
        self.assertEqual(result["realStreamsReachingMinimumHorizon"], 1)
        self.assertEqual(result["realLatestStop"], NOW + 12 * 3600)
        self.assertEqual(result["realFutureCoveragePercent"], 50)
        self.assertEqual(result["sources"]["localSynthetic"]["streamsWithFutureProgrammes"], 1)

    def test_cli_allows_provider_degradation_but_never_staleness(self):
        health, index = publication()
        health["status"] = "degraded"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "epg").mkdir()
            (root / "health.json").write_text(json.dumps(health))
            (root / "epg" / "index.json").write_text(json.dumps(index))
            args = ["--public-dir", str(root), "--expected-server", "server_1",
                    "--now-epoch", str(NOW), "--allow-degraded"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(args), 0)
                del index["mappingSha256"]
                (root / "epg" / "index.json").write_text(json.dumps(index))
                self.assertEqual(checker.main(args), 1)
                index["mappingSha256"] = "a" * 64
                (root / "epg" / "index.json").write_text(json.dumps(index))
            health["generatedAt"] = NOW - 5 * 86400
            (root / "health.json").write_text(json.dumps(health))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(args), 1)


if __name__ == "__main__":
    unittest.main()
