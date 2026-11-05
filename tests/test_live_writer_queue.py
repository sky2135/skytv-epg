"""Live recovery must not cancel a queued daily refresh or Sheet update."""
from pathlib import Path
import re
import unittest


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


class LiveWriterQueueTests(unittest.TestCase):
    def test_all_live_writers_preserve_pending_runs_under_the_same_lock(self):
        for name in ("main.yml", "channel_inventory_sync.yml", "native-epg-recovery.yml"):
            with self.subTest(workflow=name):
                workflow = (WORKFLOWS / name).read_text(encoding="utf-8")
                section = re.search(r"^concurrency:\s*\n((?:[ \t]+[^\n]*\n)+)", workflow, re.MULTILINE)
                self.assertIsNotNone(section, "Live writer needs a workflow-level concurrency lock")
                settings = dict(re.findall(r"^  ([a-z-]+): (.*?)\s*$", section.group(1), re.MULTILINE))
                self.assertEqual(settings.get("group"), "${{ github.repository }}-skytv-epg-v1")
                # GitHub's default single pending slot cancels earlier pending
                # work when another recovery arrives, even with cancellation
                # of the running job disabled. The max queue retains both.
                self.assertEqual(settings.get("queue"), "max")
                self.assertEqual(settings.get("cancel-in-progress"), "false")


if __name__ == "__main__":
    unittest.main()
