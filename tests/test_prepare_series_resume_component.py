from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as catalog
import build_vod_catalogs as vod
import component_artifacts
import prepare_series_resume_component as prepared
import publication_snapshots as snapshots
import series_resume_publication as resume
from test_component_artifacts import artifact, build_responses
from test_skytvplayer_catalog import FakeTmdbClient, NOW, app_source


SERVERS = ("server_1",)
LATER = "2026-10-17T16:30:00Z"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bytes_map(path):
    return {item.relative_to(path).as_posix(): item.read_bytes()
            for item in path.rglob("*") if item.is_file()}


class PreparedComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.build_public("candidate", LATER, "Candidate title")
        self.last_good = self.build_public("last-good", NOW, "Earlier title") / "skytvplayer"
        self.output = self.root / "private-review-bundle"

    def build_public(self, name, when, title):
        public = self.root / name
        source = app_source()
        source["get_series"][1]["name"] = title
        vod.publish_catalogs(public / "VOD", [("server_1", source, None)], generated_at=when)
        catalog.build(public / "skytvplayer", self.root / f"cache-{name}",
                      [("server_1", source, None)], FakeTmdbClient(), when)
        return public

    def enable_source(self):
        app = self.source / "skytvplayer"
        resume.attach(app, enabled=True, expected_index_sha256=digest(app / "index.json"),
                      expected_servers=SERVERS)

    def prepare(self, **kwargs):
        return prepared.prepare(self.source, self.last_good, self.output,
                                expected_index_sha256=digest(self.source / "skytvplayer/index.json"),
                                expected_last_good_index_sha256=digest(self.last_good / "index.json"),
                                expected_servers=SERVERS, **kwargs)

    def test_default_is_non_activating_and_retains_complete_old_closure(self):
        original, old = bytes_map(self.source), bytes_map(self.last_good)
        result = self.prepare()
        self.assertEqual(result["mode"], "preserve")
        self.assertIs(result["readyForPublication"], False)
        self.assertIs(result["productionSecretScanPerformed"], False)
        self.assertEqual(bytes_map(self.source), original)
        self.assertEqual(bytes_map(self.last_good), old)
        app = self.output / "snapshot/files/skytvplayer"
        self.assertEqual((app / "index.json").read_bytes(), original["skytvplayer/index.json"])
        for name, data in old.items():
            if name != "index.json":
                self.assertEqual((app / name).read_bytes(), data)
        self.assertEqual(set(item.name for item in self.output.iterdir()), {"snapshot", "report.json"})
        manifest = json.loads((self.output / "snapshot/snapshot.json").read_bytes())
        self.assertEqual(result["componentFiles"], manifest["fileCount"])
        self.assertEqual(result["componentBytes"], manifest["totalBytes"])
        self.assertEqual(result["snapshotManifestSha256"], digest(self.output / "snapshot/snapshot.json"))
        self.assertEqual(resume.validate_optional_graphs(app, expected_servers=SERVERS)["lookupServerCount"], 0)

    def test_explicit_attachment_seals_complete_graph_without_changing_source(self):
        original = bytes_map(self.source)
        result = self.prepare(enabled=True)
        app = self.output / "snapshot/files/skytvplayer"
        self.assertEqual(bytes_map(self.source), original)
        self.assertEqual(result["mode"], "attach")
        self.assertEqual(resume.validate_optional_graphs(app, expected_servers=SERVERS,
                                                       require_lookup=True)["lookupServerCount"], 1)
        for name, data in original.items():
            if name != "skytvplayer/index.json":
                self.assertEqual((self.output / "snapshot/files" / name).read_bytes(), data)
        self.assertNotEqual((app / "index.json").read_bytes(), original["skytvplayer/index.json"])

    def test_compensation_removes_only_optional_references_and_retains_objects(self):
        self.enable_source()
        original = bytes_map(self.source)
        source_index = json.loads(original["skytvplayer/index.json"])
        expected = copy.deepcopy(source_index)
        for server in expected["servers"]:
            server.pop("seriesResumeLookup")
        result = self.prepare(compensate=True)
        app = self.output / "snapshot/files/skytvplayer"
        self.assertEqual(json.loads((app / "index.json").read_bytes()), expected)
        self.assertEqual(bytes_map(self.source), original)
        self.assertEqual(result["mode"], "compensate")
        for name, data in original.items():
            if name != "skytvplayer/index.json":
                self.assertEqual((self.output / "snapshot/files" / name).read_bytes(), data)
        resume.validate_optional_graphs(app, expected_servers=SERVERS)

    def test_compensation_withdraws_missing_optional_descendant_and_retains_known_good(self):
        self.enable_source()
        resume.attach(self.last_good, enabled=True, expected_index_sha256=digest(self.last_good / "index.json"),
                      expected_servers=SERVERS)
        app = self.source / "skytvplayer"
        index = json.loads((app / "index.json").read_bytes())
        root = catalog.read_artifact(app, index["servers"][0]["seriesResumeLookup"])
        leaf = next(ref for ref in root["seriesBuckets"].values() if "file" in ref)
        (app / leaf["file"]).unlink()
        original = bytes_map(self.source)
        result = self.prepare(compensate=True)
        final = self.output / "snapshot/files/skytvplayer"
        self.assertEqual(bytes_map(self.source), original)
        self.assertEqual(result["mode"], "compensate")
        self.assertEqual(resume.validate_optional_graphs(final, expected_servers=SERVERS)["lookupServerCount"], 0)
        old_index = json.loads((self.last_good / "index.json").read_bytes())
        self.assertEqual(prepared.closure.collect_app_closure(final, expected_servers=SERVERS,
                                                            index=old_index)["metrics"]["lookupServerCount"], 1)

    def test_compensation_withdraws_semantically_invalid_but_clean_optional_root(self):
        self.enable_source()
        app = self.source / "skytvplayer"
        index = json.loads((app / "index.json").read_bytes())
        payload = catalog.read_artifact(app, index["servers"][0]["seriesResumeLookup"])
        payload["serverId"] = "server_2"
        index["servers"][0]["seriesResumeLookup"] = catalog.write_artifact(
            app, "server_1_resume_index", payload, count=512)
        catalog.atomic_json(app / "index.json", index)
        with self.assertRaises(resume.SeriesResumePublicationError):
            resume.validate_optional_graphs(app, expected_servers=SERVERS)
        expected = copy.deepcopy(index)
        expected["servers"][0].pop("seriesResumeLookup")
        original = bytes_map(self.source)
        self.prepare(compensate=True)
        final = self.output / "snapshot/files/skytvplayer"
        self.assertEqual(json.loads((final / "index.json").read_bytes()), expected)
        self.assertEqual(bytes_map(self.source), original)
        resume.validate_optional_graphs(final, expected_servers=SERVERS)

    def test_compensation_rejects_corrupt_storage_requiring_a_clean_source(self):
        self.enable_source()
        app = self.source / "skytvplayer"
        index = json.loads((app / "index.json").read_bytes())
        (app / index["servers"][0]["seriesResumeLookup"]["file"]).write_bytes(b"broken gzip bytes")
        original = bytes_map(self.source)
        with self.assertRaises(prepared.PreparedComponentError):
            self.prepare(compensate=True)
        self.assertFalse(self.output.exists())
        self.assertEqual(bytes_map(self.source), original)

    def test_prior_prepared_enabled_bundle_can_be_compensated_against_distinct_last_good(self):
        self.prepare(enabled=True)
        source = self.output / "snapshot/files"
        source_app = source / "skytvplayer"
        before = bytes_map(source)
        old_index = json.loads((source_app / "index.json").read_bytes())
        destination = self.root / "sequential-compensation"
        report = prepared.prepare(source, self.last_good, destination,
            expected_index_sha256=digest(source_app / "index.json"),
            expected_last_good_index_sha256=digest(self.last_good / "index.json"),
            expected_servers=SERVERS, compensate=True)
        final = destination / "snapshot/files/skytvplayer"
        expected = copy.deepcopy(old_index)
        expected["servers"][0].pop("seriesResumeLookup")
        self.assertEqual(json.loads((final / "index.json").read_bytes()), expected)
        self.assertEqual(bytes_map(source), before)
        self.assertTrue(report["retention"]["compensationAnchorRetained"])
        self.assertEqual(report["retention"]["remainingFollowupGenerations"], 1)
        prepared.closure.validate_retention_owner(final)
        self.assertEqual(resume.validate_optional_graphs(final, expected_servers=SERVERS)["lookupServerCount"], 0)

    def test_prior_preserved_bundle_can_be_enabled_without_discarding_ownership(self):
        self.prepare()
        source = self.output / "snapshot/files"
        source_app = source / "skytvplayer"
        before = bytes_map(source)
        destination = self.root / "sequential-enablement"
        prepared.prepare(source, self.last_good, destination,
            expected_index_sha256=digest(source_app / "index.json"),
            expected_last_good_index_sha256=digest(self.last_good / "index.json"),
            expected_servers=SERVERS, enabled=True)
        final = destination / "snapshot/files/skytvplayer"
        prepared.closure.validate_retention_owner(final)
        self.assertEqual(resume.validate_optional_graphs(final, expected_servers=SERVERS,
                                                       require_lookup=True)["lookupServerCount"], 1)
        self.assertEqual(bytes_map(source), before)

    def test_unowned_source_record_is_rejected_before_lookup_transform(self):
        self.prepare()
        source = self.output / "snapshot/files"
        source_app = source / "skytvplayer"
        record_path = source_app / prepared.closure.RETENTION_RECORD
        record = json.loads(record_path.read_bytes())
        record["currentProjectionSha256"] = "0" * 64
        record_path.write_bytes(catalog.json_bytes(record))
        before = bytes_map(source)
        destination = self.root / "rejected-owner"
        with self.assertRaises(prepared.PreparedComponentError):
            prepared.prepare(source, self.last_good, destination,
                expected_index_sha256=digest(source_app / "index.json"),
                expected_last_good_index_sha256=digest(self.last_good / "index.json"),
                expected_servers=SERVERS, enabled=True)
        self.assertFalse(destination.exists())
        self.assertEqual(bytes_map(source), before)

    def test_bad_pin_conflicting_mode_and_existing_destination_preserve_inputs(self):
        original = bytes_map(self.source)
        with self.assertRaises(prepared.PreparedComponentError):
            prepared.prepare(self.source, self.last_good, self.output,
                             expected_index_sha256="0" * 64,
                             expected_last_good_index_sha256=digest(self.last_good / "index.json"),
                             expected_servers=SERVERS)
        self.assertFalse(self.output.exists())
        with self.assertRaises(prepared.PreparedComponentError):
            self.prepare(enabled=True, compensate=True)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        (self.output / "sentinel").write_bytes(b"retain")
        with self.assertRaises(prepared.PreparedComponentError):
            self.prepare()
        self.assertEqual(bytes_map(self.output), {"sentinel": b"retain"})
        self.assertEqual(bytes_map(self.source), original)

    def test_missing_lookup_descendant_or_vod_catalogue_rejects_preparation(self):
        self.enable_source()
        app = self.source / "skytvplayer"
        index = json.loads((app / "index.json").read_bytes())
        root = catalog.read_artifact(app, index["servers"][0]["seriesResumeLookup"])
        leaf = next(value for value in root["seriesBuckets"].values() if "file" in value)
        removed = (app / leaf["file"]).read_bytes()
        (app / leaf["file"]).unlink()
        bad = bytes_map(self.source)
        with self.assertRaises(prepared.PreparedComponentError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(bytes_map(self.source), bad)
        (app / leaf["file"]).write_bytes(removed)
        raw_index = json.loads((self.source / "VOD/index.json").read_bytes())
        (self.source / "VOD" / raw_index["servers"][0]["manifestFile"]).unlink()
        with self.assertRaises(prepared.PreparedComponentError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_input_pin_change_during_prepare_is_not_committed(self):
        original_retain = prepared.closure.retain_app_closures
        original = (self.source / "skytvplayer/index.json").read_bytes()
        def changed(*args, **kwargs):
            result = original_retain(*args, **kwargs)
            (self.source / "skytvplayer/index.json").write_bytes(original + b"\n")
            return result
        with mock.patch.object(prepared.closure, "retain_app_closures", side_effect=changed):
            with self.assertRaises(prepared.PreparedComponentError):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual((self.source / "skytvplayer/index.json").read_bytes(), original + b"\n")

    def test_materialize_ignores_report_trust_and_copies_exact_verified_component(self):
        result = self.prepare(enabled=True)
        manifest_path = self.output / "snapshot/snapshot.json"
        expected_pin = digest(manifest_path)
        (self.output / "report.json").write_bytes(b"untrusted or absent report metadata")
        public = self.root / "materialized"
        copied = prepared.materialize(self.output, public, expected_snapshot_sha256=expected_pin,
                                      expected_servers=SERVERS)
        self.assertEqual(bytes_map(public), bytes_map(self.output / "snapshot/files"))
        self.assertEqual(copied["componentFiles"], result["componentFiles"])
        self.assertFalse(copied["readyForPublication"])
        self.assertFalse(copied["productionSecretScanPerformed"])

    def test_materialize_rejects_bad_pin_corrupt_payload_or_existing_output(self):
        self.prepare(enabled=True)
        public = self.root / "materialized"
        pin = digest(self.output / "snapshot/snapshot.json")
        with self.assertRaises(prepared.PreparedComponentError):
            prepared.materialize(self.output, public, expected_snapshot_sha256="0" * 64,
                                 expected_servers=SERVERS)
        self.assertFalse(public.exists())
        manifest = json.loads((self.output / "snapshot/snapshot.json").read_bytes())
        payload = self.output / "snapshot/files" / manifest["files"][0]["path"]
        payload.write_bytes(payload.read_bytes() + b"corrupt")
        with self.assertRaises(prepared.PreparedComponentError):
            prepared.materialize(self.output, public, expected_snapshot_sha256=pin,
                                 expected_servers=SERVERS)
        self.assertFalse(public.exists())
        public.mkdir()
        (public / "sentinel").write_bytes(b"retain")
        with self.assertRaises(prepared.PreparedComponentError):
            prepared.materialize(self.output, public, expected_snapshot_sha256=pin,
                                 expected_servers=SERVERS)
        self.assertEqual(bytes_map(public), {"sentinel": b"retain"})

    def test_fresh_compensation_wins_both_current_and_previous_restoration_paths(self):
        self.enable_source()
        self.prepare(compensate=True)
        old = artifact(900, 20, created_at="2026-01-01T00:10:00Z")
        compensation = artifact(100, 21, created_at="2026-01-01T00:11:00Z")
        review = artifact(9999, 22, name="series-resume-preparation-local",
                          created_at="2026-01-01T00:12:00Z")
        responses = {"/actions/artifacts/900": old,
                     "/actions/artifacts?per_page=100&page=1": {"artifacts": [old, review, compensation]}}
        responses.update(build_responses(20))
        responses.update(build_responses(21))
        selected = []
        client = object.__new__(component_artifacts.GitHubArtifacts)
        def download(value, path):
            selected.append(value["id"])
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in bytes_map(self.output / "snapshot").items():
                    archive.writestr(name, data)
        with mock.patch.object(client, "get", side_effect=responses.__getitem__), \
                mock.patch.object(client, "download", side_effect=download), \
                mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "20"}):
            current = self.root / "restored-current"
            client.current("vod", current, 900)
            self.assertEqual(selected, [100])
            self.assertEqual(bytes_map(current), bytes_map(self.output / "snapshot"))
            previous = self.root / "restored-previous"
            with mock.patch.object(component_artifacts, "GitHubArtifacts", return_value=client), \
                    mock.patch.object(sys, "argv", ["component_artifacts.py", "previous", "--component", "vod",
                                                    "--destination", str(previous)]):
                component_artifacts.main()
            self.assertEqual(selected, [100, 100])
            self.assertEqual(bytes_map(previous), bytes_map(self.output / "snapshot"))
            for restored in (current, previous):
                _root, manifest = snapshots._snapshot(restored, "vod")
                self.assertEqual(manifest["component"], "vod")
                index = json.loads((restored / "files/skytvplayer/index.json").read_bytes())
                self.assertTrue(all("seriesResumeLookup" not in server for server in index["servers"]))


if __name__ == "__main__":
    unittest.main()
