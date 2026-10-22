from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import component_artifacts  # noqa: E402
import vod_verification_artifacts as artifacts  # noqa: E402
from vod_verification_checkpoint import Checkpoint  # noqa: E402

TEST_ID = "full-vod-test"
SHA = "a" * 40
REF = "audit/full-vod-test"
RUN_ID = 123
ARTIFACT_ID = 456
SOURCE_ID = 42


class VerificationArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = {"testId": TEST_ID, "sourceArtifactId": SOURCE_ID,
                       "codeIdentity": "b" * 64, "seedCacheKey": "seed-a",
                       "requestsPerSecond": 20, "workers": 8, "requestQuota": None}
        self.identity = {"verificationConfig": self.config, "recordCount": 1,
                         "generatedAt": "2026-10-22T17:21:42Z"}
        with Checkpoint(self.state / "checkpoint.sqlite") as checkpoint:
            checkpoint.initialize([{"contentId": "server_1:movie:1", "languages": ["en"],
                                    "item": {"title": "Example (2026)"}, "identityKey": "full-key",
                                    "baseline": {"tmdbId": None, "metadataMatch": "no_match"}}], self.identity)
            checkpoint.set_meta("verificationConfig", self.config)
            checkpoint.set_meta("sourceIdentity", self.identity)
            checkpoint.set_meta("batchNumber", 1)
            self.report = checkpoint.report()
        self.report.update(testId=TEST_ID, sourceIdentity=self.identity,
                           batchNumber=1, shouldContinue=True, fatalError=None,
                           productionCatalogWritten=False)
        self.write_report()
        (self.state / "cache/tmdb").mkdir(parents=True)
        (self.state / "cache/tmdb/identity.json").write_text('{"proof":"private"}', encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {"GITHUB_SHA": SHA, "GITHUB_REF_NAME": REF})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_report(self):
        (self.state / "report.json").write_text(json.dumps(self.report, sort_keys=True), encoding="utf-8")

    def archive(self, *, changes=None, extra=None, outer_extra=False):
        artifacts.seal(self.state, TEST_ID)
        values = {path.relative_to(self.state).as_posix(): path.read_bytes()
                  for path in self.state.rglob("*") if path.is_file()}
        for key, value in (changes or {}).items():
            values[key] = value(values[key]) if callable(value) else value
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in values.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            if extra:
                member, content = extra
                archive.addfile(member, io.BytesIO(content) if content is not None else None)
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("state.tar", stream.getvalue())
            if outer_extra:
                archive.writestr("unexpected.txt", b"extra")
        return zipped.getvalue()

    def github(self, archive=None, *, artifact_changes=None, run_changes=None):
        artifact = {"id": ARTIFACT_ID, "name": f"vod-verification-{TEST_ID}-{RUN_ID}-1",
                    "workflow_run": {"id": RUN_ID}, "expired": False,
                    "size_in_bytes": len(archive or b"zip")}
        artifact.update(artifact_changes or {})
        run = {"id": RUN_ID, "path": ".github/workflows/verify-vod.yml",
               "head_sha": SHA, "head_branch": REF, "conclusion": "success"}
        run.update(run_changes or {})
        github = mock.Mock()
        github.base = "https://api.github.com/repos/example/project"
        github.get.side_effect = lambda route: artifact if route == f"/actions/artifacts/{ARTIFACT_ID}" else run
        if archive is not None:
            github.download.side_effect = lambda _artifact, path: path.write_bytes(archive)
        github.session.post.return_value.status_code = 204
        return github

    def restore(self, archive=None, **github_kwargs):
        github = self.github(archive or self.archive(), **github_kwargs)
        with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
            result = artifacts.restore_state(ARTIFACT_ID, self.root / "restored", TEST_ID)
        return result, github

    def test_seal_binds_real_database_and_report_content(self):
        manifest = artifacts.seal(self.state, TEST_ID)
        self.assertEqual(manifest["testId"], TEST_ID)
        self.assertEqual(manifest["sourceArtifactId"], SOURCE_ID)
        self.assertEqual(manifest["codeIdentity"], self.config["codeIdentity"])
        self.assertEqual(manifest["checkpointSha256"], artifacts.digest(self.state / "checkpoint.sqlite"))
        self.assertEqual(manifest["reportSha256"], artifacts.digest(self.state / "report.json"))
        self.assertEqual(manifest["batchNumber"], 1)

    def test_seal_rejects_production_write_or_wrong_test_identity(self):
        for changed in ({"productionCatalogWritten": True}, {"testId": "another-test"}):
            with self.subTest(changed=changed):
                report = {**self.report, **changed}
                (self.state / "report.json").write_text(json.dumps(report), encoding="utf-8")
                with self.assertRaises(ValueError):
                    artifacts.seal(self.state, TEST_ID)

    def test_seal_rejects_corrupt_sqlite_database(self):
        (self.state / "checkpoint.sqlite").write_bytes(b"not a SQLite database")
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            artifacts.seal(self.state, TEST_ID)

    def test_seal_rejects_report_from_different_frozen_source(self):
        self.report["sourceIdentity"] = copy.deepcopy(self.identity)
        self.report["sourceIdentity"]["verificationConfig"]["sourceArtifactId"] = SOURCE_ID + 1
        self.write_report()
        with self.assertRaises(ValueError):
            artifacts.seal(self.state, TEST_ID)

    def test_seal_rejects_report_batch_different_from_database(self):
        self.report["batchNumber"] = 2
        self.write_report()
        with self.assertRaises(ValueError):
            artifacts.seal(self.state, TEST_ID)

    def test_restore_resumes_database_and_private_cache_without_touching_production(self):
        production = self.root / "production/skytvplayer/index.json"
        production.parent.mkdir(parents=True)
        production.write_bytes(b'{"generation":"keep"}')
        before = production.read_bytes()
        result, github = self.restore()
        restored = self.root / "restored"
        self.assertEqual(result, {"restoredArtifactId": ARTIFACT_ID, "batchNumber": 1})
        self.assertEqual((restored / "cache/tmdb/identity.json").read_bytes(),
                         (self.state / "cache/tmdb/identity.json").read_bytes())
        with Checkpoint(restored / "checkpoint.sqlite") as checkpoint:
            self.assertEqual(checkpoint.count(), 1)
            self.assertEqual(checkpoint.get_meta("verificationConfig"), self.config)
        self.assertEqual(production.read_bytes(), before)
        github.session.post.assert_not_called()

    def test_restore_rejects_wrong_test_name_and_artifact_run_identity_before_download(self):
        changes = ({"name": "vod-verification-other-test-123-1"},
                   {"workflow_run": {"id": RUN_ID + 1}})
        for change in changes:
            with self.subTest(change=change):
                github = self.github(self.archive(), artifact_changes=change)
                with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
                    with self.assertRaises(ValueError):
                        artifacts.restore_state(ARTIFACT_ID, self.root / "restored", TEST_ID)
                github.download.assert_not_called()

    def test_restore_rejects_wrong_revision_branch_workflow_or_failed_parent(self):
        for changed in ({"head_sha": "c" * 40}, {"head_branch": "main"},
                        {"path": ".github/workflows/vod.yml"}, {"conclusion": "failure"}):
            with self.subTest(changed=changed):
                github = self.github(self.archive(), run_changes=changed)
                with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
                    with self.assertRaises(ValueError):
                        artifacts.restore_state(ARTIFACT_ID, self.root / "restored", TEST_ID)
                github.download.assert_not_called()

    def test_restore_rejects_checkpoint_or_report_corruption(self):
        for filename in ("checkpoint.sqlite", "report.json"):
            with self.subTest(filename=filename):
                archive = self.archive(changes={filename: lambda value: value + b"corrupt"})
                github = self.github(archive)
                with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
                    with self.assertRaises(ValueError):
                        artifacts.restore_state(ARTIFACT_ID, self.root / ("corrupt-" + filename), TEST_ID)

    def test_restore_rejects_manifest_source_code_or_batch_disagreement(self):
        for name, value in (("sourceArtifactId", SOURCE_ID + 1), ("codeIdentity", "d" * 64), ("batchNumber", 2)):
            with self.subTest(name=name):
                def changed(raw, key=name, replacement=value):
                    return json.dumps({**json.loads(raw), key: replacement}).encode("utf-8")
                archive = self.archive(changes={"manifest.json": changed})
                github = self.github(archive)
                with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
                    with self.assertRaises(ValueError):
                        artifacts.restore_state(ARTIFACT_ID, self.root / ("wrong-" + name), TEST_ID)

    def test_restore_rejects_outer_zip_extra_files(self):
        archive = self.archive(outer_extra=True)
        with self.assertRaises(ValueError):
            self.restore(archive)
        self.assertFalse((self.root / "restored").exists())

    def test_restore_rejects_traversal_symlinks_and_duplicate_tar_paths(self):
        traversal = tarfile.TarInfo("../escaped.txt")
        traversal.size = 4
        symlink = tarfile.TarInfo("cache/link")
        symlink.type, symlink.linkname = tarfile.SYMTYPE, "../../outside"
        duplicate = tarfile.TarInfo("report.json")
        duplicate.size = 4
        for member, content in ((traversal, b"evil"), (symlink, None), (duplicate, b"evil")):
            with self.subTest(name=member.name):
                with self.assertRaises(ValueError):
                    self.restore(self.archive(extra=(member, content)))
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_restore_does_not_overwrite_an_existing_destination(self):
        target = self.root / "restored"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"keep")
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual((target / "keep.txt").read_bytes(), b"keep")

    def test_restore_restores_production_component_extraction_limit_on_failure_and_success(self):
        original = component_artifacts.LIMIT
        with self.assertRaises(ValueError):
            self.restore(self.archive(outer_extra=True))
        self.assertEqual(component_artifacts.LIMIT, original)
        self.restore()
        self.assertEqual(component_artifacts.LIMIT, original)

    def test_restore_component_requires_validated_vod_build(self):
        github = self.github()
        github._validated.return_value = False
        with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
            with self.assertRaises(ValueError):
                artifacts.restore_component(SOURCE_ID, self.root / "component")
        github._restore_artifact.assert_not_called()

    def test_continue_dispatches_one_resume_and_preserves_explicit_inputs(self):
        github = self.github()
        inputs = {"test-id": TEST_ID, "source-artifact-id": str(SOURCE_ID),
                  "seed-cache-key": "seed-a", "rate": "20", "workers": "8",
                  "batch-seconds": "900", "batch-records": "25000"}
        original = copy.deepcopy(inputs)
        with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
            result = artifacts.continue_test(self.state / "report.json", ARTIFACT_ID, inputs)
        self.assertTrue(result["continued"])
        self.assertEqual(inputs, original)
        call = github.session.post.call_args
        self.assertEqual(call.args[0], github.base + "/actions/workflows/verify-vod.yml/dispatches")
        self.assertEqual(call.kwargs["json"], {"ref": REF, "inputs": {**inputs, "resume-artifact-id": str(ARTIFACT_ID)}})
        self.assertIs(call.kwargs["allow_redirects"], False)
        github.session.post.assert_called_once()

    def test_completed_or_fatal_report_does_not_dispatch_more_work(self):
        for changed in ({"complete": True, "shouldContinue": False, "fatalError": None},
                        {"complete": False, "shouldContinue": False, "fatalError": "tmdb_authentication_rejected"}):
            with self.subTest(changed=changed):
                self.report.update(changed)
                self.write_report()
                github = self.github()
                with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
                    result = artifacts.continue_test(self.state / "report.json", ARTIFACT_ID, {})
                self.assertFalse(result["continued"])
                github.session.post.assert_not_called()

    def test_dispatch_failure_keeps_original_checkpoint_recoverable(self):
        before = (self.state / "checkpoint.sqlite").read_bytes()
        github = self.github()
        github.session.post.return_value.status_code = 403
        with mock.patch.object(artifacts, "GitHubArtifacts", return_value=github):
            with self.assertRaisesRegex(ValueError, "Checkpoint saved"):
                artifacts.continue_test(self.state / "report.json", ARTIFACT_ID, {"test-id": TEST_ID})
        self.assertEqual((self.state / "checkpoint.sqlite").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
