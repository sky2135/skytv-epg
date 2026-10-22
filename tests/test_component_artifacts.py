import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.component_artifacts import COMPONENT_BUILDS, GitHubArtifacts, extract_pages, extract_zip


class ArtifactExtractionTests(unittest.TestCase):
    def test_zip_rejects_traversal_and_symlink_before_writing(self):
        for name, mode in (("../outside", None), ("link", 0o120777 << 16)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "archive.zip"
                with zipfile.ZipFile(archive, "w") as out:
                    info = zipfile.ZipInfo(name)
                    if mode:
                        info.external_attr = mode
                    out.writestr(info, b"unsafe")
                with self.assertRaises(ValueError):
                    extract_zip(archive, root / "output")
                self.assertFalse((root / "output").exists())

    def test_pages_rejects_links_and_preserves_normal_files(self):
        for linked in (False, True):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with tarfile.open(root / "artifact.tar", "w") as out:
                    info = tarfile.TarInfo("./health.json")
                    if linked:
                        info.type = tarfile.SYMTYPE
                        info.linkname = "../outside"
                        out.addfile(info)
                    else:
                        info.size = 2
                        out.addfile(info, io.BytesIO(b"{}"))
                if linked:
                    with self.assertRaises(ValueError):
                        extract_pages(root / "artifact.tar", root / "output")
                else:
                    extract_pages(root / "artifact.tar", root / "output")
                    self.assertEqual((root / "output/health.json").read_bytes(), b"{}")


def artifact(identifier, run_id, attempt=1, component="vod", **changes):
    result = {"id": identifier, "name": f"{component}-snapshot-{run_id}-{attempt}",
              "expired": False, "size_in_bytes": 100, "workflow_run": {"id": run_id}}
    result.update(changes)
    return result


def build_responses(run_id, attempt=1, component="vod", *, conclusion="success", branch="main", current_attempt=None):
    workflow, job_name = COMPONENT_BUILDS[component]
    return {f"/actions/runs/{run_id}": {"id": run_id, "head_branch": branch,
                "path": f".github/workflows/{workflow}", "run_attempt": current_attempt or attempt},
            f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100": {"jobs": [
                {"name": job_name, "conclusion": conclusion, "run_attempt": attempt}]}}


class ArtifactSelectionTests(unittest.TestCase):
    def selected(self, responses, *, current=None, run_id=2, component="vod"):
        client = object.__new__(GitHubArtifacts)
        seen = []
        def download(value, path):
            seen.append(value["id"])
            with zipfile.ZipFile(path, "w") as out:
                out.writestr("snapshot.json", str(value["id"]).encode())
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "get", side_effect=responses.__getitem__) as get, \
                patch.object(client, "download", side_effect=download), patch.dict(os.environ, {"GITHUB_RUN_ID": str(run_id)}):
            destination = Path(tmp) / "restored"
            if current is None:
                result = client.latest_component(component, destination)
            else:
                result = client.current(component, destination, current)
            if seen:
                self.assertEqual((destination / "snapshot.json").read_bytes(), str(seen[-1]).encode())
            else:
                self.assertFalse(destination.exists())
            return result, seen, [call.args[0] for call in get.call_args_list]

    def test_latest_component_requires_successful_build_and_exact_artifact(self):
        responses = {"/actions/artifacts?per_page=100&page=1": {"artifacts": [
            artifact(30, 3), artifact(10, 2), artifact(11, 2, 2)]}}
        responses.update(build_responses(3, conclusion="failure"))
        responses.update(build_responses(2, current_attempt=2))
        responses.update(build_responses(2, 2))
        result, selected, _calls = self.selected(responses)
        self.assertTrue(result)
        self.assertEqual(selected, [11])

    def test_new_upload_from_older_run_is_newer_than_recent_run_creation(self):
        responses = {"/actions/artifacts?per_page=100&page=1": {"artifacts": [
            artifact(80, 9), artifact(99, 2, 3)]}}
        responses.update(build_responses(9))
        responses.update(build_responses(2, 3))
        self.assertEqual(self.selected(responses)[1], [99])

    def test_old_publisher_retry_uses_newer_validated_same_component(self):
        old, new = artifact(10, 2, component="live"), artifact(20, 3, component="live")
        responses = {"/actions/artifacts/10": old,
                     "/actions/artifacts?per_page=100&page=1": {"artifacts": [old, new]}}
        responses.update(build_responses(2, component="live", current_attempt=2))
        responses.update(build_responses(3, component="live"))
        self.assertEqual(self.selected(responses, current=10, component="live")[1], [20])

    def test_publisher_only_rerun_reuses_successful_build_attempt_one(self):
        own = artifact(10, 2)
        responses = {"/actions/artifacts/10": own,
                     "/actions/artifacts?per_page=100&page=1": {"artifacts": [own]}}
        responses.update(build_responses(2, current_attempt=2))
        _result, selected, calls = self.selected(responses, current=10)
        self.assertEqual(selected, [10])
        self.assertIn("/actions/runs/2/attempts/1/jobs?per_page=100", calls)
        self.assertFalse(any("attempts/2" in call for call in calls))

    def test_forged_name_wrong_workflow_branch_or_build_attempt_cannot_win(self):
        forged = artifact(100, 8, workflow_run={"id": 99})
        wrong_workflow = artifact(99, 7)
        wrong_branch = artifact(98, 6)
        wrong_attempt = artifact(97, 5, 2)
        good = artifact(90, 4)
        responses = {"/actions/artifacts?per_page=100&page=1": {"artifacts": [
            forged, wrong_workflow, wrong_branch, wrong_attempt, good]}}
        for run in (7, 6, 4):
            responses.update(build_responses(run, branch="feature" if run == 6 else "main"))
        responses["/actions/runs/7"]["path"] = ".github/workflows/other.yml"
        responses.update(build_responses(5, 2))
        responses["/actions/runs/5/attempts/2/jobs?per_page=100"]["jobs"][0]["run_attempt"] = 1
        _result, selected, calls = self.selected(responses)
        self.assertEqual(selected, [90])
        self.assertNotIn("/actions/runs/8", calls)

    def test_unvalidated_current_artifact_is_rejected_even_if_newer_exists(self):
        for candidate in (artifact(10, 3), artifact(11, 2), artifact(10, 2, expired=True)):
            with self.subTest(candidate=candidate):
                responses = {"/actions/artifacts/10": candidate}
                with self.assertRaises(ValueError):
                    self.selected(responses, current=10)

    def test_expired_or_unrelated_artifacts_need_no_run_queries_and_do_not_restore(self):
        responses = {"/actions/artifacts?per_page=100&page=1": {"artifacts": [
            artifact(30, 3, expired=True), artifact(20, 2, component="live"),
            artifact(10, 1, name="vod-snapshot-1-1-suffix")]}}
        result, selected, calls = self.selected(responses)
        self.assertFalse(result)
        self.assertEqual(selected, [])
        self.assertEqual(calls, ["/actions/artifacts?per_page=100&page=1"])

    def test_repository_scan_is_bounded_to_five_pages_and_can_find_fifth_page(self):
        noise = [{"id": 10000 + number, "name": "other", "expired": False} for number in range(100)]
        responses = {f"/actions/artifacts?per_page=100&page={page}": {"artifacts": noise.copy()}
                     for page in range(1, 6)}
        responses["/actions/artifacts?per_page=100&page=5"]["artifacts"][-1] = artifact(50, 2)
        responses.update(build_responses(2))
        _result, selected, calls = self.selected(responses)
        self.assertEqual(selected, [50])
        self.assertEqual(sum(call.startswith("/actions/artifacts?") for call in calls), 5)

    def test_github_metadata_error_fails_instead_of_selecting_older_state(self):
        client = object.__new__(GitHubArtifacts)
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, "get", side_effect=ValueError("API unavailable")):
            with self.assertRaises(ValueError):
                client.latest_component("vod", Path(tmp) / "restored")
            self.assertFalse((Path(tmp) / "restored").exists())


class IndependentWorkflowTests(unittest.TestCase):
    def test_builds_are_independent_and_deployment_is_serialized(self):
        root = Path(__file__).resolve().parents[1]
        live = (root / ".github/workflows/main.yml").read_text()
        vod = (root / ".github/workflows/vod.yml").read_text()
        publish = (root / ".github/workflows/publish-component.yml").read_text()
        self.assertNotIn("build_vod_catalogs.py", live)
        self.assertNotIn("build_skytvplayer_catalog.py", live)
        self.assertNotIn("sync_channel_inventory.py", vod)
        self.assertNotIn("build_epg_streaming.py", vod)
        for workflow in (live, vod):
            self.assertNotIn("actions/deploy-pages@", workflow)
            self.assertIn("uses: ./.github/workflows/publish-component.yml", workflow)
        self.assertIn("skytv-pages-publication", publish)
        self.assertLess(publish.index("concurrency:"), publish.index("component_artifacts.py previous"))
        self.assertLess(publish.index("check_publication.sh all"), publish.index("actions/deploy-pages@"))
        self.assertNotEqual(live.split("group:", 1)[1].splitlines()[0], vod.split("group:", 1)[1].splitlines()[0])
        self.assertIn("path: .build/matcher-cache.enc", live)
        self.assertNotIn("path: .build/matcher-cache\n", live)
