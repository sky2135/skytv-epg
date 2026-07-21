from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "should_run.py"


class SchedulerTests(unittest.TestCase):
    def run_check(self, state_payload: dict | None, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "last_success.json"
            output = root / "github_output.txt"
            if state_payload is not None:
                state.write_text(json.dumps(state_payload), encoding="utf-8")
            env = os.environ.copy()
            env["GITHUB_OUTPUT"] = str(output)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state",
                    str(state),
                    "--interval-hours",
                    "72",
                    *extra,
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            values: dict[str, str] = {}
            if output.exists():
                for line in output.read_text(encoding="utf-8").splitlines():
                    key, value = line.split("=", 1)
                    values[key] = value
            return result, values

    def test_recent_success_intentionally_skips_build(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(hours=4)
        result, values = self.run_check(
            {"schemaVersion": 1, "lastSuccessUtc": recent.isoformat().replace("+00:00", "Z")}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["due"], "false")
        self.assertEqual(values["reason"], "not yet due")
        self.assertTrue(values["next_due_utc"].endswith("Z"))

    def test_old_success_runs_build(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=73)
        result, values = self.run_check(
            {"schemaVersion": 1, "lastSuccessUtc": old.isoformat().replace("+00:00", "Z")}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["due"], "true")
        self.assertEqual(values["reason"], "72-hour interval reached")

    def test_missing_state_fails_open_and_runs_build(self) -> None:
        result, values = self.run_check(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["due"], "true")
        self.assertEqual(values["reason"], "state file is missing")

    def test_manual_force_runs_build(self) -> None:
        recent = datetime.now(timezone.utc)
        result, values = self.run_check(
            {"schemaVersion": 1, "lastSuccessUtc": recent.isoformat().replace("+00:00", "Z")},
            "--force",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["due"], "true")
        self.assertEqual(values["reason"], "manual workflow dispatch")


if __name__ == "__main__":
    unittest.main()
