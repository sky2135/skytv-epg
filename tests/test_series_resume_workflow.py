"""Execute the new prepared-route boundary offline; no GitHub/provider access."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/vod.yml").read_text(encoding="utf-8")


def retention_code():
    block = WORKFLOW.split("      - name: Retain catalogue closures and optionally attach series resume lookup", 1)[1]
    block = block.split("      - name: Trace selected", 1)[0]
    body = block.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0]
    return compile(textwrap.dedent(body), "retention-workflow", "exec")


def prepared_code():
    block = WORKFLOW.split("      - name: Materialize checksum-pinned private bundle without provider work", 1)[1]
    block = block.split("      - name: Verify files contain", 1)[0]
    body = block.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0]
    return compile(textwrap.dedent(body), "prepared-workflow", "exec")


@contextmanager
def owned_directory():
    before = Path.cwd()
    with tempfile.TemporaryDirectory() as value:
        os.chdir(value)
        try:
            yield
        finally:
            os.chdir(before)


class PreparedWorkflowBoundaryTests(unittest.TestCase):
    def execute(self, *, identifier="42", pin="a" * 64, **changes):
        self.events = []
        record = {"id": 42, "expired": False, "name": "series-resume-preparation-reviewed"}
        record.update(changes)
        events = self.events
        class FakeGitHub:
            def get(self, path):
                events.append(("get", path))
                return record
            def download(self, artifact, path):
                events.append(("download", path.as_posix()))
        fake = types.ModuleType("component_artifacts")
        fake.GitHubArtifacts = FakeGitHub
        fake.extract_zip = lambda archive, destination: events.append(("extract", destination.as_posix()))
        with owned_directory(), patch.dict(sys.modules, {"component_artifacts": fake}), \
                patch.dict(os.environ, {"PREPARED_ARTIFACT_ID": identifier, "PREPARED_SNAPSHOT_SHA256": pin}), \
                patch.object(subprocess, "run", side_effect=lambda args, **kwargs: events.append(("run", args, kwargs))), \
                patch.object(sys, "path", list(sys.path)):
            exec(prepared_code(), {"__name__": "__main__"})

    def test_review_artifact_is_only_materialized_with_exact_operator_pin(self):
        self.execute()
        self.assertEqual([x[0] for x in self.events], ["get", "download", "extract", "run"])
        args = self.events[-1][1]
        self.assertEqual(args[1:3], ["scripts/prepare_series_resume_component.py", "materialize"])
        self.assertEqual(args[-2:], ["--snapshot-sha256", "a" * 64])
        self.assertEqual(self.events[-1][2], {"check": True})
        self.assertNotIn("--enabled", args)
        self.assertNotIn("--compensate", args)

    def test_bad_or_missing_identity_pin_never_reaches_github(self):
        for change in ({"identifier": "0"}, {"identifier": "42;bad"}, {"identifier": "1" * 30},
                       {"pin": ""}, {"pin": "A" * 64}, {"pin": "a" * 63}):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                self.execute(**change)
            self.assertEqual(self.events, [])

    def test_production_or_wrong_artifact_never_downloaded(self):
        for change in ({"name": "vod-snapshot-10-1"}, {"name": "live-snapshot-10-1"},
                       {"name": "series-resume-preparation-../bad"}, {"expired": True},
                       {"expired": None}, {"id": 43}):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                self.execute(**change)
            self.assertEqual([x[0] for x in self.events], ["get"])


class DailyRetentionBoundaryTests(unittest.TestCase):
    def execute(self, enabled):
        self.calls = []
        with owned_directory(), patch.dict(os.environ, {"SERIES_RESUME_LOOKUP_ENABLED": enabled}):
            Path("public/skytvplayer").mkdir(parents=True)
            Path("public/skytvplayer/index.json").write_bytes(b"ordinary-index")
            Path(".build/resume-last-good/files/skytvplayer").mkdir(parents=True)
            Path(".build/resume-last-good/files/skytvplayer/index.json").write_bytes(b"last-good")
            def command(args, **kwargs):
                self.calls.append(args)
                if args[2] == "prepare":
                    Path(".build/resume-prepared/snapshot").mkdir(parents=True)
                    Path(".build/resume-prepared/snapshot/snapshot.json").write_bytes(b"sealed-manifest")
                elif args[2] == "materialize":
                    Path(".build/resume-public").mkdir()
                    Path(".build/resume-public/materialized").write_bytes(b"validated")
                else:
                    raise AssertionError(args)
            with patch.object(subprocess, "run", side_effect=command):
                exec(retention_code(), {"__name__": "__main__"})
            self.assertEqual(Path("public/materialized").read_bytes(), b"validated")
            self.assertEqual(Path(".build/resume-public-before/skytvplayer/index.json").read_bytes(), b"ordinary-index")

    def test_disabled_daily_build_still_prepares_and_retains_without_enabling(self):
        self.execute("false")
        self.assertEqual([x[2] for x in self.calls], ["prepare", "materialize"])
        self.assertNotIn("--enabled", self.calls[0])
        self.assertNotIn("--compensate", self.calls[0])

    def test_enabled_daily_build_adds_only_the_explicit_attachment_flag(self):
        self.execute("true")
        self.assertEqual(self.calls[0][-1], "--enabled")
        self.assertEqual(self.calls[0].count("--enabled"), 1)

    def test_invalid_setting_stops_before_preparation(self):
        with self.assertRaises(SystemExit):
            self.execute("unexpected")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
