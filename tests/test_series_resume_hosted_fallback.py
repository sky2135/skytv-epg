"""Exercise normal-build restore fallback with mocked HTTP, no real accounts."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import component_artifacts
import publication_snapshots as snapshots
from test_publication_snapshots import Response, SessionFactory

BASE = "https://sky2135.github.io/skytv-epg/"


def normal_restore_environment(variables):
    workflow = (ROOT / ".github/workflows/vod.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Retain catalogue closures and optionally attach series resume lookup", 1)[1]
    step = step.split("        shell: bash", 1)[0]
    match = re.search(r"EPG_PUBLIC_BASE_URL: \$\{\{ vars\.(EPG_PUBLIC_BASE_URL) \}\}", step)
    return {"EPG_PUBLIC_BASE_URL": variables[match[1]]} if match else {}


class HostedLastGoodFallbackTests(unittest.TestCase):
    def exercise(self, *, configured):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "public"
        for name in ("VOD/index.json", "skytvplayer/index.json"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")
        manifest = snapshots.package("vod", source, root / "sealed")
        responses = {BASE + "component-manifests/vod.json": Response(BASE + "component-manifests/vod.json",
                     json.dumps(manifest).encode())}
        for row in manifest["files"]:
            responses[BASE + row["path"]] = Response(BASE + row["path"], (source / row["path"]).read_bytes())
        transport = SessionFactory(responses)
        client = mock.Mock()
        client.latest_component.return_value = False
        destination = root / "restored"
        variables = {"EPG_PUBLIC_BASE_URL": BASE if configured else ""}
        environment = normal_restore_environment(variables)
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(sys, "argv", ["component_artifacts.py", "previous", "--component", "vod", "--destination", str(destination)]), \
             mock.patch.object(component_artifacts, "GitHubArtifacts", return_value=client), \
             mock.patch.object(snapshots.requests, "Session", side_effect=transport):
            if not configured:
                with self.assertRaises(snapshots.SnapshotError):
                    component_artifacts.main()
                self.assertFalse(destination.exists())
                self.assertEqual(transport.calls, [])
            else:
                component_artifacts.main()
                self.assertEqual(json.loads((destination / "snapshot.json").read_bytes()), manifest)
                for row in manifest["files"]:
                    self.assertEqual((destination / "files" / row["path"]).read_bytes(), (source / row["path"]).read_bytes())
                self.assertEqual({call[0] for call in transport.calls}, set(responses))
                self.assertTrue(all(not call[2] for call in transport.calls))
        client.latest_component.assert_called_once_with("vod", destination)
        client.legacy.assert_not_called()

    def test_no_eligible_artifact_fetches_full_hosted_last_good_using_workflow_configuration(self):
        self.exercise(configured=True)

    def test_missing_configuration_fails_before_transport_and_preserves_destination(self):
        self.exercise(configured=False)


if __name__ == "__main__":
    unittest.main()
