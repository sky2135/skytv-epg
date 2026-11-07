from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def inventory_step() -> tuple[str, str]:
    """Read the actual inventory run block without adding a YAML dependency."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    steps = re.split(r"(?m)^      - name:", workflow)
    matches = [step for step in steps if re.search(r"(?m)^          INVENTORY_MODE:", step)]
    if len(matches) != 1:
        raise AssertionError("Expected one inventory step with an explicit inventory mode.")
    step = matches[0]
    default = re.search(
        r"(?m)^          INVENTORY_MODE: \$\{\{ inputs\.inventory_mode \|\| ['\"]([^'\"]+)['\"] \}\}$",
        step,
    )
    if default is None:
        raise AssertionError("Inventory mode must have an explicit scheduled-run default.")
    lines = step.splitlines()
    starts = [index for index, line in enumerate(lines) if line == "        run: |"]
    if len(starts) != 1:
        raise AssertionError("Expected one literal inventory shell block.")
    body = []
    for line in lines[starts[0] + 1:]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(body) + "\n", default.group(1)


def find_bash() -> str | None:
    explicit = os.environ.get("SKYTV_TEST_BASH", "")
    candidates = [explicit, shutil.which("bash") or ""]
    if os.name == "nt":
        candidates.extend([
            str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/bin/bash.exe"),
            "C:/msys64/usr/bin/bash.exe",
            "C:/cygwin64/bin/bash.exe",
        ])
        candidates.extend(str(Path(entry) / "bash.exe") for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
    for candidate in candidates:
        if not candidate:
            continue
        # Windows' bash.exe launches WSL, which does not inherit the Windows
        # fixture environment/cwd contract used by this test. Use native Git
        # Bash, an explicit compatible shell, or the Linux CI runner instead.
        if os.name == "nt" and re.search(r"[\\/](?:Windows[\\/](?:System32|SysWOW64)|WindowsApps)[\\/]", candidate, re.I):
            continue
        try:
            if not Path(candidate).is_file():
                continue
        except OSError:
            # Windows App Execution Aliases can fail even a read-only stat.
            continue
        return candidate
    return None


FAKE_COMMANDS = textwrap.dedent(r"""
    timeout() {
        printf '%s\0' "$@" > "$ARGV_CAPTURE"
        local -a captured=("$@")
        local index output
        for (( index=0; index<${#captured[@]}; index+=1 )); do
            case "${captured[index]}" in
                --snapshot-out|--authoritative-snapshot-out|--snapshot-manifest-out|--epgshare-icon-catalog-out)
                    output="${captured[index+1]}"
                    mkdir -p -- "$(dirname -- "$output")"
                    printf 'fixture output\n' > "$output"
                    ;;
            esac
        done
    }
    unexpected_command() {
        printf '%s\n' "$1" >> "$UNEXPECTED_CAPTURE"
        return 97
    }
    python() { unexpected_command python; }
    python3() { unexpected_command python3; }
    curl() { unexpected_command curl; }
    wget() { unexpected_command wget; }
""")


class LiveWorkflowInventoryModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body, cls.default_mode = inventory_step()
        cls.bash = find_bash()
        if cls.bash is None:
            raise unittest.SkipTest("A native Bash shell is required; Linux CI executes these tests.")

    def run_inventory(self, mode=None, *, gemini="true"):
        with tempfile.TemporaryDirectory(prefix="inventory mode ") as directory:
            root = Path(directory)
            (root / "workflow.sh").write_text(FAKE_COMMANDS + self.body, encoding="utf-8", newline="\n")
            env = os.environ.copy()
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            env.update({
                "INVENTORY_MODE": self.default_mode if mode is None else mode,
                "GOOGLE_SHEET_ID": "fixture-sheet-id",
                "GOOGLE_SHEET_TAB": "Fixture mapping rows",
                "RUNNER_TEMP": "runner temp",
                "GITHUB_STEP_SUMMARY": "step summary.txt",
                "REVIEW_APPLY_LIMIT": "17",
                "AI_REVIEW_LIMIT": "3",
                "EPG_COVERAGE_FALLBACK_LIMIT": "29",
                "USE_GEMINI_AI": gemini,
                "ALLOW_INSECURE_PANEL_HTTP": "false",
                "ARGV_CAPTURE": "captured args.bin",
                "UNEXPECTED_CAPTURE": "unexpected command.txt",
            })
            result = subprocess.run(
                [self.bash, "--noprofile", "--norc", "workflow.sh"],
                cwd=directory, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=20,
            )
            captured = root / env["ARGV_CAPTURE"]
            argv = captured.read_bytes().decode("utf-8").split("\0")[:-1] if captured.exists() else None
            outputs = {
                name: (root / name).read_bytes() if (root / name).is_file() else None
                for name in (
                    ".build/channel-sync/effective_mapping.csv",
                    ".build/channel-sync/authoritative_mapping.csv",
                    ".build/channel-sync/mapping_snapshot_manifest.json",
                    "runner temp/skytv-epg-v1/source_icons.json",
                )
            }
            summary = root / env["GITHUB_STEP_SUMMARY"]
            return {
                "result": result, "argv": argv, "outputs": outputs,
                "summary": summary.read_text(encoding="utf-8") if summary.exists() else "",
                "unexpected": (root / env["UNEXPECTED_CAPTURE"]).exists(),
                "inventory_directory_created": (root / ".build/channel-sync").exists(),
            }

    def option(self, argv, flag):
        positions = [index for index, value in enumerate(argv) if value == flag]
        self.assertEqual(len(positions), 1, f"Expected exactly one {flag}: {argv!r}")
        return argv[positions[0] + 1]

    def assert_common_safety_inputs(self, invocation):
        result, argv = invocation["result"], invocation["argv"]
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(invocation["unexpected"], "Workflow attempted to run Python or a network client.")
        self.assertEqual(argv[:6], ["--signal=TERM", "--kill-after=60s", "85m", "python", "-u", "scripts/sync_channel_inventory.py"])
        expected = {
            "--mode": "refresh",
            "--sheet-id": "fixture-sheet-id",
            "--sheet-tab": "Fixture mapping rows",
            "--snapshot-out": ".build/channel-sync/effective_mapping.csv",
            "--authoritative-snapshot-out": ".build/channel-sync/authoritative_mapping.csv",
            "--snapshot-manifest-out": ".build/channel-sync/mapping_snapshot_manifest.json",
            "--all-source-file": "runner temp/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz",
            "--all-source-catalog-file": "runner temp/skytv-epg-v1/epg_ripper_ALL_SOURCES1.txt",
            "--epgshare-spool-out": "runner temp/skytv-epg-v1/selected_epg.sqlite3",
            "--epgshare-icon-catalog-out": "runner temp/skytv-epg-v1/source_icons.json",
        }
        for flag, value in expected.items():
            self.assertEqual(self.option(argv, flag), value)
        self.assertIn("--provider-playlist-links", argv)
        floor_index = argv.index("--minimum-server-channels")
        self.assertEqual(argv[floor_index + 1:floor_index + 4], ["server_1=4000", "server_2=10500", "server_3=9400"])
        self.assertTrue(all(invocation["outputs"].values()), "Snapshot postconditions did not execute against fixture files.")

    def test_default_normal_preserves_writes_review_and_enabled_gemini(self):
        self.assertEqual(self.default_mode, "normal")
        invocation = self.run_inventory(gemini="TrUe")
        self.assert_common_safety_inputs(invocation)
        argv = invocation["argv"]
        self.assertEqual(self.option(argv, "--review-recheck-mode"), "apply")
        self.assertEqual(self.option(argv, "--review-apply-limit"), "17")
        self.assertEqual(self.option(argv, "--coverage-fallback-limit"), "29")
        for flag in ("--write-to-sheet", "--validate-native-review", "--use-gemini-ai"):
            self.assertIn(flag, argv)

    def test_normal_can_disable_gemini_without_disabling_write_checks(self):
        invocation = self.run_inventory("normal", gemini="FALSE")
        self.assert_common_safety_inputs(invocation)
        argv = invocation["argv"]
        self.assertEqual(self.option(argv, "--review-recheck-mode"), "apply")
        self.assertIn("--write-to-sheet", argv)
        self.assertIn("--validate-native-review", argv)
        self.assertNotIn("--use-gemini-ai", argv)

    def test_existing_mappings_never_requests_writes_apply_native_or_gemini(self):
        invocation = self.run_inventory("existing-mappings", gemini="true")
        self.assert_common_safety_inputs(invocation)
        argv = invocation["argv"]
        self.assertEqual(self.option(argv, "--review-recheck-mode"), "off")
        self.assertEqual(self.option(argv, "--review-apply-limit"), "0")
        self.assertEqual(self.option(argv, "--coverage-fallback-limit"), "0")
        for forbidden in ("apply", "--write-to-sheet", "--validate-native-review", "--use-gemini-ai"):
            self.assertNotIn(forbidden, argv)
        self.assertIn("existing mappings only", invocation["summary"])

    def test_invalid_mode_stops_before_any_command_or_snapshot_creation(self):
        for mode in ("", "apply", "normal; python unsafe.py"):
            with self.subTest(mode=mode):
                invocation = self.run_inventory(mode, gemini="true")
                self.assertNotEqual(invocation["result"].returncode, 0)
                self.assertIsNone(invocation["argv"])
                self.assertFalse(invocation["unexpected"])
                self.assertFalse(invocation["inventory_directory_created"])
                self.assertFalse(any(invocation["outputs"].values()))
