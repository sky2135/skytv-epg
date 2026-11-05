import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recover_native_epg as recovery

NOW = 1800000000
HEALTH = {"generatedAt": NOW-3600, "unavailableNativePanelServers": ["server_3"]}
RUNS = [{"status":"completed", "created_at":"2026-11-01T00:00:00Z"}]
GOOD = {"serverId":"server_3", "status":"ok", "usable":True}


class NativeRecoveryTests(unittest.TestCase):
    def test_healthy_publication_does_not_contact_any_provider(self):
        with mock.patch.object(recovery.diagnostic, "native_epg_probe") as probe:
            report = recovery.probe({**HEALTH,"unavailableNativePanelServers":[]}, RUNS, NOW)
        probe.assert_not_called()
        self.assertFalse(report["refreshNeeded"])

    def test_running_refresh_prevents_duplicate_probe_and_dispatch(self):
        active=[{"status":"in_progress"}]
        with mock.patch.object(recovery.diagnostic, "native_epg_probe") as probe:
            report = recovery.probe(HEALTH, active, NOW)
        probe.assert_not_called()
        self.assertFalse(report["refreshNeeded"])

    def test_source_recovery_requests_only_one_fresh_generation_refresh(self):
        with mock.patch.object(recovery.diagnostic, "native_epg_probe", return_value=GOOD) as probe:
            report = recovery.probe(HEALTH, RUNS, NOW)
        probe.assert_called_once_with("server_3")
        self.assertTrue(recovery.dispatch_allowed(report, HEALTH, RUNS, NOW+10))
        self.assertFalse(recovery.dispatch_allowed(report, {**HEALTH,"generatedAt":NOW}, RUNS, NOW+10))
        self.assertFalse(recovery.dispatch_allowed(report, HEALTH, [{"status":"queued"}], NOW+10))
        self.assertFalse(recovery.dispatch_allowed(report, HEALTH, RUNS, NOW+601))

    def test_outage_is_reported_without_triggering_full_build(self):
        with mock.patch.object(recovery.diagnostic, "native_epg_probe", return_value={**GOOD,"usable":False,"status":"unavailable"}):
            report = recovery.probe(HEALTH, RUNS, NOW)
        self.assertFalse(report["refreshNeeded"])
        self.assertFalse(recovery.dispatch_allowed(report, HEALTH, RUNS, NOW))

    def test_source_health_cannot_select_server_one_or_arbitrary_servers(self):
        for value in (["server_1"], ["server_3","server_3"], ["arbitrary"], "server_3", [{}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recovery.targets({**HEALTH,"unavailableNativePanelServers":value})

    def test_mismatched_probe_cannot_authorize_refresh(self):
        with mock.patch.object(recovery.diagnostic, "native_epg_probe", return_value={**GOOD,"serverId":"server_2"}):
            report = recovery.probe(HEALTH, RUNS, NOW)
        self.assertFalse(recovery.dispatch_allowed(report, HEALTH, RUNS, NOW))
