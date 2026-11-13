from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import app_catalog_closure as closure
import build_skytvplayer_catalog as catalog
import publication_snapshots as snapshots
import series_resume_lookup as lookup
from test_skytvplayer_catalog import FakeTmdbClient, NOW, LATER, app_source


class AppCatalogClosureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / "old" / "skytvplayer"
        self.candidate = self.root / "candidate" / "skytvplayer"
        self.old_index = self.build(self.old, NOW, external=True, attach=True)
        self.current_index = self.build(self.candidate, LATER)

    def build(self, output, now, *, external=False, attach=False):
        source = app_source()
        if external:
            source["series_details"]["202"] = {"episodes": {"1": [
                {"id": str(900000 + n), "episode_num": n + 1, "title": f"Episode {n + 1}",
                 "container_extension": "mp4"} for n in range(501)]}}
        index = catalog.build(output, output.parent / "private", [("server_1", source, None)], FakeTmdbClient(), now)
        if attach:
            staging = output.parent / "lookup"
            reference = lookup.write_series_resume_lookup(output, staging, "server_1", index["servers"][0]["series"])
            for path in staging.iterdir():
                shutil.copyfile(path, output / path.name)
            index["servers"][0]["seriesResumeLookup"] = reference
            (output / "index.json").write_bytes(catalog.json_bytes(index))
        return index

    def collect(self, directory, *, index=None):
        return closure.collect_app_closure(directory, expected_servers=("server_1",), index=index)

    def retain(self):
        return closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",))

    def hashes(self, directory):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}

    def parent(self, directory, index):
        manifest = catalog.read_artifact(directory, index["servers"][0]["series"])
        return next(parent for ref in manifest["shards"] for parent in catalog.read_artifact(directory, ref)["items"]
                    if parent["id"] == "202")

    def test_collect_traverses_movies_home_external_episodes_and_all_lookup_children(self):
        result = self.collect(self.old)
        names = result["files"]
        server = self.old_index["servers"][0]
        for key in ("movies", "series", "home", "seriesResumeLookup"):
            self.assertIn(server[key]["file"], names)
        for reference in self.parent(self.old, self.old_index)["episodeFiles"]:
            self.assertIn(reference["file"], names)
        root = catalog.read_artifact(self.old, server["seriesResumeLookup"])
        for table in ("seriesBuckets", "episodeBuckets"):
            for reference in root[table].values():
                if not reference.get("empty"):
                    self.assertIn(reference["file"], names)
        self.assertEqual(result["metrics"]["lookupServerCount"], 1)
        self.assertEqual(result["metrics"]["closureFileCount"], len(names))
        self.assertEqual(result["validation"]["app"]["serverCount"], 1)
        self.assertEqual(result["validation"]["lookup"]["lookupServerCount"], 1)

    def test_retention_keeps_both_complete_graphs_without_replacing_candidate_index(self):
        old_before, candidate_index = self.hashes(self.old), (self.candidate / "index.json").read_bytes()
        old_closure, current_closure = self.collect(self.old), self.collect(self.candidate)
        result = self.retain()
        self.assertGreater(result["copiedFiles"], 0)
        self.assertTrue(result["candidateIndexUnchanged"])
        self.assertFalse(result["publicationActivated"])
        self.assertEqual((self.candidate / "index.json").read_bytes(), candidate_index)
        self.assertEqual(self.hashes(self.old), old_before)
        current_after = self.collect(self.candidate)
        old_after = self.collect(self.candidate, index=self.old_index)
        self.assertEqual(current_after["files"], current_closure["files"])
        self.assertEqual(old_after["files"], {k:v for k,v in old_closure["files"].items() if k != "index.json"})
        self.assertEqual(self.retain()["copiedFiles"], 0)

    def test_missing_old_external_episode_rejects_before_any_copy(self):
        missing = self.parent(self.old, self.old_index)["episodeFiles"][0]
        (self.old / missing["file"]).unlink()
        before = self.hashes(self.candidate)
        with self.assertRaises(closure.ClosureError):
            self.retain()
        self.assertEqual(self.hashes(self.candidate), before)

    def test_missing_old_lookup_child_rejects_before_any_copy(self):
        root = catalog.read_artifact(self.old, self.old_index["servers"][0]["seriesResumeLookup"])
        child = next(ref for ref in root["episodeBuckets"].values() if not ref.get("empty"))
        (self.old / child["file"]).unlink()
        before = self.hashes(self.candidate)
        with self.assertRaises(closure.ClosureError):
            self.retain()
        self.assertEqual(self.hashes(self.candidate), before)

    def test_different_bytes_at_retained_filename_are_never_overwritten(self):
        reference = self.old_index["servers"][0]["movies"]
        target = self.candidate / reference["file"]
        target.write_bytes(b"unreferenced conflicting bytes")
        before = self.hashes(self.candidate)
        with self.assertRaisesRegex(closure.ClosureError, "filename checksum|conflicting bytes"):
            self.retain()
        self.assertEqual(self.hashes(self.candidate), before)

    def test_actual_retained_union_enforces_all_publication_caps_before_copy(self):
        before = self.hashes(self.candidate)
        caps = {"MAX_FILES": len(before), "MAX_TREE_ENTRIES": len(before) + 4,
                "MAX_TOTAL_BYTES": 1, "MAX_MANIFEST_BYTES": 1, "MAX_INDEX_BYTES": 1}
        for constant, value in caps.items():
            with self.subTest(constant=constant), mock.patch.object(snapshots, constant, value):
                with self.assertRaises(closure.ClosureError):
                    self.retain()
            self.assertEqual(self.hashes(self.candidate), before)

    def test_unindexed_candidate_artifacts_and_sibling_vod_bytes_are_measured(self):
        extra_bytes = b"already retained data"
        extra = self.candidate / ("retired." + hashlib.sha256(extra_bytes).hexdigest()[:16] + ".json.gz")
        extra.write_bytes(extra_bytes)
        sibling = self.candidate.parent / "VOD"
        sibling.mkdir()
        (sibling / "index.json").write_bytes(b"{}")
        result = self.retain()
        self.assertTrue(result["includesSiblingVod"])
        self.assertEqual(result["retainedAppFiles"], len(self.hashes(self.candidate)))
        self.assertEqual(result["componentFiles"], len(self.hashes(self.candidate)) + 1)
        self.assertFalse(extra.exists())
        self.assertEqual(result["prunedPrivateCandidateFiles"], 1)

    def test_expected_server_set_and_overlapping_trees_are_rejected(self):
        with self.assertRaises(closure.ClosureError):
            closure.collect_app_closure(self.old, expected_servers=("server_2",))
        with self.assertRaises(closure.ClosureError):
            closure.retain_app_closures(self.candidate, self.candidate, expected_servers=("server_1",))

    def test_optional_root_removal_does_not_drop_last_good_graph(self):
        self.assertNotIn("seriesResumeLookup", self.current_index["servers"][0])
        self.retain()
        old_root = self.old_index["servers"][0]["seriesResumeLookup"]
        self.assertTrue((self.candidate / old_root["file"]).is_file())
        self.assertEqual(self.collect(self.candidate)["metrics"]["lookupServerCount"], 0)
        self.assertEqual(self.collect(self.candidate, index=self.old_index)["metrics"]["lookupServerCount"], 1)

    def test_enabled_then_compensated_then_fresh_disabled_build_retains_complete_old_graph(self):
        compensated = self.candidate
        result = closure.retain_app_closures(compensated, self.old, expected_servers=("server_1",),
                                             compensation_index=self.old_index)
        self.assertTrue(result["compensationAnchorRetained"])
        self.assertEqual(result["remainingFollowupGenerations"], 1)
        self.assertLessEqual(result["protectedClosureCount"], 3)
        # Re-preparing this exact compensated candidate against an older
        # last-good record with no hold must preserve its current owned hold.
        null_hold = {"schemaVersion": 1, "kind": "skytv-app-closure-retention-v1", "anchor": None,
            "remainingFollowupGenerations": 0,
            "currentProjectionSha256": closure._projection_sha(self.old_index)}
        (self.old / closure.RETENTION_RECORD).write_bytes(lookup._json_bytes(null_hold))
        replay = closure.retain_app_closures(compensated, self.old, expected_servers=("server_1",))
        self.assertTrue(replay["compensationAnchorRetained"])
        self.assertEqual(replay["remainingFollowupGenerations"], 1)
        import json
        epg_repack = json.loads((compensated / "index.json").read_bytes())
        epg_repack["metadata"] = {"stats": {"requests": 42}}
        epg_repack["servers"][0]["epg"] = {"programme": {"file": "../EPG/server_1_epg.json.gz", "sha256": "e" * 64, "compressedBytes": 100}}
        (compensated / "index.json").write_bytes(catalog.json_bytes(epg_repack))
        epg_replay = closure.retain_app_closures(compensated, self.old, expected_servers=("server_1",))
        self.assertEqual(epg_replay["remainingFollowupGenerations"], 1)
        self.assertEqual(epg_replay["currentProjectionSha256"], replay["currentProjectionSha256"])
        root_ref = self.old_index["servers"][0]["seriesResumeLookup"]
        following = self.root / "following" / "skytvplayer"
        self.build(following, "2026-10-18T16:30:00Z")
        before = self.hashes(following)
        record_path = compensated / closure.RETENTION_RECORD
        record_bytes = record_path.read_bytes()
        record_path.unlink()
        with self.assertRaisesRegex(closure.ClosureError, "bounded retention record"):
            closure.retain_app_closures(following, compensated, expected_servers=("server_1",))
        self.assertEqual(self.hashes(following), before)
        record_path.write_bytes(record_bytes)
        old_root = compensated / root_ref["file"]
        root_bytes = old_root.read_bytes()
        old_root.unlink()
        with self.assertRaisesRegex(closure.ClosureError, "anchor closure is incomplete"):
            closure.retain_app_closures(following, compensated, expected_servers=("server_1",))
        self.assertEqual(self.hashes(following), before)
        old_root.write_bytes(root_bytes)
        result = closure.retain_app_closures(following, compensated, expected_servers=("server_1",))
        self.assertEqual(result["remainingFollowupGenerations"], 0)
        self.assertEqual(self.collect(following, index=self.old_index)["metrics"]["lookupServerCount"], 1)
        import json
        epg_only = json.loads((following / "index.json").read_bytes())
        epg_only["metadata"] = {"stats": {"requests": 123}}
        epg_only["servers"][0]["epg"] = {"programme": {"file": "../EPG/server_1_epg.json.gz", "sha256": "f" * 64, "compressedBytes": 100}}
        (following / "index.json").write_bytes(catalog.json_bytes(epg_only))
        replay = closure.retain_app_closures(following, compensated, expected_servers=("server_1",))
        self.assertEqual(replay["remainingFollowupGenerations"], 0)
        self.assertEqual(replay["currentProjectionSha256"], result["currentProjectionSha256"])
        final = self.root / "after-following" / "skytvplayer"
        self.build(final, "2026-10-19T16:30:00Z")
        for path in following.iterdir():
            if path.name != "index.json" and not (final / path.name).exists():
                shutil.copyfile(path, final / path.name)
        result = closure.retain_app_closures(final, following, expected_servers=("server_1",))
        self.assertFalse(result["compensationAnchorRetained"])
        self.assertLessEqual(result["protectedClosureCount"], 2)
        self.assertFalse((final / root_ref["file"]).exists())
        self.assertTrue((following / root_ref["file"]).is_file())

    def test_four_distinct_ordinary_generations_keep_only_current_and_last_good(self):
        previous = self.old
        first_root = self.old_index["servers"][0]["seriesResumeLookup"]["file"]
        for number in range(4):
            current = self.root / f"ordinary-{number}" / "skytvplayer"
            self.build(current, f"2026-10-{20 + number}T16:30:00Z")
            for path in previous.iterdir():
                if path.name != "index.json" and not (current / path.name).exists():
                    shutil.copyfile(path, current / path.name)
            previous_before = self.hashes(previous)
            result = closure.retain_app_closures(current, previous, expected_servers=("server_1",))
            self.assertLessEqual(result["protectedClosureCount"], 2)
            self.assertFalse(result["compensationAnchorRetained"])
            self.assertEqual(self.hashes(previous), previous_before)
            expected = set(self.collect(current)["files"]) | (set(self.collect(previous)["files"]) - {"index.json"})
            hashed = {p.name for p in current.iterdir() if catalog.ARTIFACT_NAME.fullmatch(p.name)}
            self.assertEqual(hashed, expected - {"index.json"})
            if number:
                self.assertFalse((current / first_root).exists())
            previous = current


class LookupOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / "old" / "skytvplayer"
        self.candidate = self.root / "candidate" / "skytvplayer"
        # Small real catalogue graphs exercise this ownership delta without
        # repeating the larger external-inventory and transfer-boundary suites.
        self.old_index = AppCatalogClosureTests.build(self, self.old, NOW, attach=True)
        self.origin = AppCatalogClosureTests.build(self, self.candidate, LATER)
        closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                    compensation_index=self.old_index)

    def hashes(self, directory):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir() if path.is_file()}

    def enable_candidate(self):
        staging = self.root / "candidate-lookup"
        reference = lookup.write_series_resume_lookup(self.candidate, staging, "server_1",
                                                       self.origin["servers"][0]["series"])
        for path in staging.iterdir():
            shutil.copyfile(path, self.candidate / path.name)
        current = copy.deepcopy(self.origin)
        current["servers"][0]["seriesResumeLookup"] = reference
        (self.candidate / "index.json").write_bytes(catalog.json_bytes(current))
        return current

    def test_ownership_lookup_only_delta_preserves_hold_and_failures_do_not_mutate(self):
        original = closure.validate_retention_owner(self.candidate)
        self.assertEqual(original["remainingFollowupGenerations"], 1)
        self.assertIsNone(closure.validate_retention_owner(self.old))
        self.enable_candidate()
        before, old_before = self.hashes(self.candidate), self.hashes(self.old)
        with self.assertRaises(closure.ClosureError):
            closure.validate_retention_owner(self.candidate)
        self.assertEqual(closure.validate_retention_owner(self.candidate, index=self.origin), original)
        forged = copy.deepcopy(self.origin)
        forged["metadata"]["stats"]["requests"] = 999
        # Stats do not bind the retained generation, but this transformation
        # authorization must still reject changing anything except lookup refs.
        self.assertEqual(closure._projection_sha(forged), closure._projection_sha(self.origin))
        with self.assertRaisesRegex(closure.ClosureError, "ordinary index facts"):
            closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                        lookup_change_source_index=forged)
        self.assertEqual(self.hashes(self.candidate), before)
        path = self.candidate / closure.RETENTION_RECORD
        raw_record = path.read_bytes()
        forged_record = copy.deepcopy(original)
        forged_record["currentProjectionSha256"] = "0" * 64
        path.write_bytes(lookup._json_bytes(forged_record))
        with self.assertRaisesRegex(closure.ClosureError, "original retention owner"):
            closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                        lookup_change_source_index=self.origin)
        self.assertEqual(path.read_bytes(), lookup._json_bytes(forged_record))
        path.write_bytes(raw_record)
        with mock.patch.object(snapshots, "MAX_TOTAL_BYTES", 1):
            with self.assertRaises(closure.ClosureError):
                closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                            lookup_change_source_index=self.origin)
        self.assertEqual(self.hashes(self.candidate), before)
        result = closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                            lookup_change_source_index=self.origin)
        self.assertEqual(result["remainingFollowupGenerations"], 1)
        self.assertTrue(result["compensationAnchorRetained"])
        rebound = closure.validate_retention_owner(self.candidate)
        self.assertEqual(rebound["anchor"], original["anchor"])
        self.assertEqual(rebound["remainingFollowupGenerations"], original["remainingFollowupGenerations"])
        replay = closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                            lookup_change_source_index=self.origin)
        self.assertEqual(replay["remainingFollowupGenerations"], 1)
        self.assertEqual(self.hashes(self.old), old_before)

    def test_ownership_precheck_does_not_open_an_optional_graph_before_withdrawal(self):
        current = self.enable_candidate()
        closure.retain_app_closures(self.candidate, self.old, expected_servers=("server_1",),
                                    lookup_change_source_index=self.origin)
        reference = current["servers"][0]["seriesResumeLookup"]
        (self.candidate / reference["file"]).write_bytes(b"intentionally corrupt optional graph")
        before = self.hashes(self.candidate)
        record = closure.validate_retention_owner(self.candidate)
        self.assertEqual(record["currentProjectionSha256"], closure._projection_sha(current))
        self.assertEqual(self.hashes(self.candidate), before)
        with self.assertRaises(closure.ClosureError):
            closure.collect_app_closure(self.candidate, expected_servers=("server_1",))


if __name__ == "__main__":
    unittest.main()
