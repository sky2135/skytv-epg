from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as catalog
import series_resume_lookup as lookup
import series_resume_publication as publication
from test_skytvplayer_catalog import FakeTmdbClient, NOW, app_source


class SeriesResumePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "skytvplayer"
        self.build()

    def build(self, *, servers=("server_1",), now=NOW):
        self.servers = servers
        self.index = catalog.build(self.app, self.root / "private",
                                   [(server, app_source(), None) for server in servers], FakeTmdbClient(), now)
        return self.index

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.app.iterdir() if path.is_file()}

    def pin(self):
        return hashlib.sha256((self.app / "index.json").read_bytes()).hexdigest()

    def attach(self, **kwargs):
        return publication.attach(self.app, expected_servers=self.servers,
                                   expected_index_sha256=self.pin(), **kwargs)

    def validate(self, **kwargs):
        return publication.validate_optional_graphs(self.app, expected_servers=self.servers, **kwargs)

    def save_index(self):
        catalog.atomic_json(self.app / "index.json", self.index)

    def accepted(self):
        return json.loads((self.app / "index.json").read_bytes())

    def assert_clean_stage(self):
        self.assertFalse(list(self.root.glob(".series-resume-publication-*")))
        self.assertFalse((self.root / ".skytvplayer.series-resume-attachment.lock").exists())

    def test_disabled_is_byte_preserving_and_never_generates(self):
        before = self.snapshot()
        with mock.patch.object(lookup, "write_series_resume_lookup", side_effect=AssertionError("Must stay disabled")):
            result = self.attach()
        self.assertFalse(result["enabled"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.snapshot(), before)
        self.assert_clean_stage()

    def test_stale_pin_is_rejected_even_when_disabled(self):
        before = self.snapshot()
        with self.assertRaises(publication.SeriesResumePublicationError):
            publication.attach(self.app, expected_index_sha256="0" * 64, expected_servers=self.servers)
        self.assertEqual(self.snapshot(), before)

    def test_success_changes_only_optional_index_field_and_is_byte_idempotent(self):
        before = self.snapshot()
        with mock.patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
            result = self.attach(enabled=True)
        self.assertTrue(result["changed"])
        current = self.accepted()
        reference = current["servers"][0].pop("seriesResumeLookup")
        self.assertEqual(current, self.index)
        self.assertEqual(reference["recordCount"], 512)
        for name, data in before.items():
            if name != "index.json":
                self.assertEqual((self.app / name).read_bytes(), data)
        self.assertEqual(self.validate(require_lookup=True)["lookupServerCount"], 1)
        installed = self.snapshot()
        result = self.attach(enabled=True)
        self.assertFalse(result["changed"])
        self.assertEqual(self.snapshot(), installed)
        self.assert_clean_stage()

    def test_absence_is_supported_but_present_invalid_roots_are_not_ignored(self):
        self.assertEqual(self.validate()["lookupServerCount"], 0)
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.validate(require_lookup=True)
        for value in (None, {}, False, {"schemaVersion": "unsupported", "file": "invalid"}):
            with self.subTest(value=value):
                self.index["servers"][0]["seriesResumeLookup"] = value
                self.save_index()
                with self.assertRaises(publication.SeriesResumePublicationError):
                    self.validate()

    def test_partial_server_opt_in_is_supported_unless_all_are_required(self):
        self.build(servers=("server_1", "server_2", "server_3"))
        self.attach(enabled=True)
        self.index = self.accepted()
        del self.index["servers"][1]["seriesResumeLookup"]
        self.save_index()
        self.assertEqual(self.validate()["lookupServerCount"], 2)
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.validate(require_lookup=True)

    def test_last_server_generation_failure_installs_no_artifacts(self):
        self.build(servers=("server_1", "server_2", "server_3"))
        before = self.snapshot()
        original = lookup.write_series_resume_lookup

        def fail_last(source, target, server, reference):
            if server == "server_3":
                raise lookup.ResumeLookupError("Injected third-server failure")
            return original(source, target, server, reference)

        with mock.patch.object(lookup, "write_series_resume_lookup", side_effect=fail_last):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual(self.snapshot(), before)
        self.assert_clean_stage()

    def test_all_collisions_are_preflighted_before_any_install(self):
        stage = self.root / "collision-reference"
        lookup.write_series_resume_lookup(self.app, stage, "server_1", self.index["servers"][0]["series"])
        name = sorted(path.name for path in stage.iterdir())[-1]
        (self.app / name).write_bytes(b"unrelated bytes")
        before = self.snapshot()
        with mock.patch.object(publication, "_install_artifact", side_effect=AssertionError("Preflight must fail first")):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual(self.snapshot(), before)
        self.assert_clean_stage()

    def test_install_failure_keeps_index_and_retry_accepts_identical_orphans(self):
        before = (self.app / "index.json").read_bytes()
        install = publication._install_artifact
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Injected install failure")
            install(*args)

        with mock.patch.object(publication, "_install_artifact", side_effect=fail_second):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual((self.app / "index.json").read_bytes(), before)
        self.assertTrue(list(self.app.glob("*_resume_*.json.gz")))
        self.assert_clean_stage()
        self.assertTrue(self.attach(enabled=True)["changed"])
        self.validate(require_lookup=True)

    def test_changed_index_is_never_overwritten_by_attachment(self):
        original = lookup.write_series_resume_lookup
        replacement = copy.deepcopy(self.index)
        replacement["operatorNote"] = "externally changed"
        replacement_bytes = catalog.json_bytes(replacement)

        def change_index(*args):
            result = original(*args)
            (self.app / "index.json").write_bytes(replacement_bytes)
            return result

        with mock.patch.object(lookup, "write_series_resume_lookup", side_effect=change_index):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual((self.app / "index.json").read_bytes(), replacement_bytes)
        self.assertFalse(list(self.app.glob("*_resume_*.json.gz")))
        self.assert_clean_stage()

    def test_source_change_after_validation_is_detected_before_install(self):
        before = (self.app / "index.json").read_bytes()
        manifest = catalog.read_artifact(self.app, self.index["servers"][0]["series"])
        source_file = self.app / manifest["shards"][0]["file"]
        preflight = publication._preflight_artifacts

        def change_source(*args):
            preflight(*args)
            source_file.write_bytes(b"changed externally after validation")

        with mock.patch.object(publication, "_preflight_artifacts", side_effect=change_source):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual((self.app / "index.json").read_bytes(), before)
        self.assertFalse(list(self.app.glob("*_resume_*.json.gz")))

    def test_failed_atomic_index_replace_preserves_index_and_cleans_owned_temp(self):
        before = (self.app / "index.json").read_bytes()
        replace = publication.os.replace

        def fail_index(source, target):
            if Path(target).name == "index.json":
                raise PermissionError("Injected index replacement failure")
            return replace(source, target)

        with mock.patch.object(publication.os, "replace", side_effect=fail_index):
            with self.assertRaises(publication.SeriesResumePublicationError):
                self.attach(enabled=True)
        self.assertEqual((self.app / "index.json").read_bytes(), before)
        self.assertFalse(list(self.app.glob(".series-resume-index-*")))
        self.assert_clean_stage()

    def test_home_cutoff_must_match_series_but_global_index_can_be_later(self):
        self.attach(enabled=True)
        self.index = self.accepted()
        self.index["generatedAt"] = "2026-10-17T16:30:00Z"
        self.save_index()
        self.validate(require_lookup=True)
        entry = self.index["servers"][0]
        home = catalog.read_artifact(self.app, entry["home"])
        home["generatedAt"] = self.index["generatedAt"]
        entry["home"] = catalog.write_artifact(self.app, "server_1_home", home, count=len(home["items"]))
        self.save_index()
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.validate()

    def test_rebuild_requires_new_reference_and_attachment_rebinds_final_manifest(self):
        self.attach(enabled=True)
        old = self.accepted()["servers"][0]
        self.build(now="2026-10-17T16:30:00Z")
        fresh_series = self.index["servers"][0]["series"]
        self.assertNotEqual(old["series"]["sha256"], fresh_series["sha256"])
        self.index["servers"][0]["seriesResumeLookup"] = old["seriesResumeLookup"]
        self.save_index()
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.validate()
        ordinary_before = self.snapshot()
        self.attach(enabled=True)
        self.validate(require_lookup=True)
        entry = self.accepted()["servers"][0]
        root = catalog.read_artifact(self.app, entry["seriesResumeLookup"])
        self.assertEqual(root["seriesManifestSha256"], fresh_series["sha256"])
        for name, data in ordinary_before.items():
            if name != "index.json":
                self.assertEqual((self.app / name).read_bytes(), data)

    def test_missing_source_artifact_cannot_become_a_published_empty_graph(self):
        reference = self.index["servers"][0]["series"]
        manifest = catalog.read_artifact(self.app, reference)
        (self.app / manifest["shards"][0]["file"]).unlink()
        before = self.snapshot()
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.attach(enabled=True)
        self.assertEqual(self.snapshot(), before)

    def test_cooperating_lock_blocks_enabled_attachment_and_is_not_removed(self):
        lock = self.root / ".skytvplayer.series-resume-attachment.lock"
        lock.mkdir()
        before = self.snapshot()
        with self.assertRaises(publication.SeriesResumePublicationError):
            self.attach(enabled=True)
        self.assertTrue(lock.is_dir())
        self.assertEqual(self.snapshot(), before)
        self.attach()  # Disabled calls do not acquire or clear another owner's lock.
        lock.rmdir()

    def test_post_commit_lock_cleanup_error_is_reported_as_cleanup_not_failed_attachment(self):
        remove = Path.rmdir

        def fail_lock(path):
            if path.name == ".skytvplayer.series-resume-attachment.lock":
                raise PermissionError("Injected lock cleanup failure")
            return remove(path)

        with mock.patch.object(Path, "rmdir", fail_lock):
            result = self.attach(enabled=True)
        self.assertTrue(result["changed"])
        self.assertTrue(result["lockCleanupPending"])
        self.assertEqual(result["indexSha256"], self.pin())
        self.validate(require_lookup=True)
        (self.root / ".skytvplayer.series-resume-attachment.lock").rmdir()

    def test_cli_validation_has_explicit_required_mode_and_disabled_attach_default(self):
        before = self.snapshot()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(publication.main(["validate", "--app-dir", str(self.app), "--server", "server_1"]), 0)
            self.assertEqual(publication.main(["attach", "--app-dir", str(self.app), "--server", "server_1",
                                                "--index-sha256", self.pin()]), 0)
        self.assertEqual(self.snapshot(), before)
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as exited:
            publication.main(["validate", "--app-dir", str(self.app), "--server", "server_1", "--require-lookup"])
        self.assertEqual(exited.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
