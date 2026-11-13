from __future__ import annotations

import copy
import gzip
import hashlib
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
import series_resume_lookup as resume
import stage_series_resume_lookup as staging
from test_skytvplayer_catalog import FakeTmdbClient, NOW, app_source


def install(directory, prefix, payload, record_count):
    """Independent fixture encoder for deliberately re-sealed bad documents."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded = bytearray(gzip.compress(raw, mtime=0))
    encoded[9] = 255
    data = bytes(encoded)
    digest = hashlib.sha256(data).hexdigest()
    name = f"{prefix}.{digest[:16]}.json.gz"
    (directory / name).write_bytes(data)
    return {"file": name, "sha256": digest, "compressedBytes": len(data),
            "uncompressedBytes": len(raw), "recordCount": record_count}


class ResumeLookupFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.temporary = temporary
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "lookup"
        self.index = catalog.build(self.source, self.root / "private",
                                   [("server_1", app_source(), None)], FakeTmdbClient(), NOW)
        self.server = self.index["servers"][0]

    def build(self):
        return resume.write_series_resume_lookup(self.source, self.output, "server_1", self.server["series"])

    def validate(self, reference):
        return resume.validate_series_resume_lookup(
            self.output, reference, source_dir=self.source,
            server_id="server_1", series_ref=self.server["series"])

    def payload(self, reference, directory=None):
        return catalog.read_artifact(directory or self.output, reference)

    def parents(self):
        manifest = self.payload(self.server["series"], self.source)
        return [item for reference in manifest["shards"]
                for item in self.payload(reference, self.source)["items"]]

    def replace_parents(self, parents):
        self.server["series"] = catalog.write_dataset(self.source, "server_1", "series", parents, NOW)

    def rewrite_root(self, root):
        return install(self.output, "server_1_resume_index", root, 512)

    def rewrite_child(self, root, table, bucket, child):
        root[table][bucket] = install(
            self.output, f"server_1_resume_test_{bucket}", child, len(child["items"]))
        return self.rewrite_root(root)

    def large_inventory(self, count):
        source = app_source()
        source["series_details"]["202"] = {"episodes": {"1": [
            {"id": str(900000 + number), "episode_num": number + 1,
             "title": f"Episode {number + 1}", "container_extension": "mp4"}
            for number in range(count)
        ]}}
        output = self.root / f"source-{count}"
        self.index = catalog.build(output, self.root / f"private-{count}",
                                   [("server_1", source, None)], FakeTmdbClient(), NOW)
        self.source, self.server = output, self.index["servers"][0]


class SeriesResumeLookupTests(ResumeLookupFixture):
    def test_round_trip_preserves_canonical_facts_and_source_bytes(self):
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.source.iterdir() if path.is_file()}
        reference = self.build()
        root = self.payload(reference)
        metrics = self.validate(reference)
        self.assertEqual(root["seriesManifestSha256"], self.server["series"]["sha256"])
        self.assertEqual(set(root["seriesBuckets"]), {f"{value:02x}" for value in range(256)})
        self.assertEqual(set(root["episodeBuckets"]), set(root["seriesBuckets"]))
        self.assertEqual(reference["recordCount"], 512)
        self.assertEqual(metrics["seriesRecords"], len(self.parents()))
        self.assertGreater(metrics["episodeRecords"], 0)
        parent_bucket = self.payload(root["seriesBuckets"][resume.series_bucket("server_1", "202")])
        projected = next(row for row in parent_bucket["items"] if row["providerSeriesId"] == "202")
        canonical = next(row for row in self.parents() if row["id"] == "202")
        self.assertEqual(projected["section"], "series")
        for key in resume.PARENT_FIELDS:
            if key != "section":
                self.assertEqual(projected[key], canonical[key], key)
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in self.source.iterdir() if path.is_file()})
        self.assertNotIn("seriesResumeLookup", self.server)

    def test_write_is_deterministic_and_validated(self):
        first = self.build()
        second_output = self.root / "second-lookup"
        second = resume.write_series_resume_lookup(self.source, second_output, "server_1", self.server["series"])
        self.assertEqual(first, second)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()},
                         {path.name: path.read_bytes() for path in second_output.iterdir()})

    def test_fifty_thousand_episodes_resolve_by_two_child_artifacts(self):
        self.large_inventory(50_000)
        reference = self.build()
        metrics = self.validate(reference)
        self.assertGreaterEqual(metrics["episodeRecords"], 50_000)
        root = self.payload(reference)
        loaded = []
        for table, bucket in (
            ("seriesBuckets", resume.series_bucket("server_1", "202")),
            ("episodeBuckets", resume.episode_bucket("server_1", "202", "949999")),
        ):
            loaded.append(self.payload(root[table][bucket]))
        self.assertTrue(any(row["providerSeriesId"] == "202" for row in loaded[0]["items"]))
        episode = next(row for row in loaded[1]["items"]
                       if row["providerSeriesId"] == "202" and row["providerEpisodeId"] == "949999")
        self.assertEqual(episode["episodeNumber"], 50_000)
        self.assertEqual(episode["containerExtension"], "mp4")
        self.assertLessEqual(len(loaded[1]["items"]), 8192)

    def test_missing_referenced_episode_does_not_become_an_empty_bucket(self):
        self.large_inventory(501)
        parent = next(row for row in self.parents() if row["id"] == "202")
        (self.source / parent["episodeFiles"][0]["file"]).unlink()
        with self.assertRaises(resume.ResumeLookupError):
            self.build()
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))

    def test_partial_pending_stale_and_unknown_facts_are_not_normalized_away(self):
        baseline = self.parents()
        for status, complete, checked in (("available", False, NOW), ("available", False, None),
                                          ("pending", False, None), ("stale", False, NOW)):
            with self.subTest(status=status, complete=complete, checked=checked):
                parents = copy.deepcopy(baseline)
                parent = next(row for row in parents if row["id"] == "202")
                parent.update(episodesStatus=status, episodeInventoryComplete=complete,
                              episodeInventoryCheckedAt=checked,
                              serverEpisodeCount=1 if checked else None, adult=None,
                              language="und", languages=["und"],
                              languageSource="unknown", languageListComplete=False)
                self.replace_parents(parents)
                target = self.root / f"variant-{status}-{checked is None}"
                reference = resume.write_series_resume_lookup(self.source, target, "server_1", self.server["series"])
                root = self.payload(reference, target)
                rows = self.payload(root["seriesBuckets"][resume.series_bucket("server_1", "202")], target)["items"]
                projected = next(row for row in rows if row["providerSeriesId"] == "202")
                self.assertEqual(projected["episodesStatus"], status)
                self.assertEqual(projected["episodeInventoryComplete"], complete)
                self.assertEqual(projected["episodeInventoryCheckedAt"], checked)
                self.assertIsNone(projected["adult"])
                self.assertEqual(projected["languages"], ["und"])

    def test_resealed_wrong_revision_server_and_incomplete_tables_fail(self):
        reference = self.build()
        original = self.payload(reference)
        bad_documents = []
        wrong_revision = copy.deepcopy(original)
        wrong_revision["seriesManifestSha256"] = "0" * 64
        bad_documents.append(wrong_revision)
        wrong_server = copy.deepcopy(original)
        wrong_server["serverId"] = "server_2"
        bad_documents.append(wrong_server)
        missing_slot = copy.deepcopy(original)
        del missing_slot["seriesBuckets"]["ff"]
        bad_documents.append(missing_slot)
        wrong_empty = copy.deepcopy(original)
        key = next(key for key, value in wrong_empty["seriesBuckets"].items() if value.get("empty"))
        wrong_empty["seriesBuckets"][key] = {"empty": True, "recordCount": 1}
        bad_documents.append(wrong_empty)
        for number, document in enumerate(bad_documents):
            with self.subTest(number=number), self.assertRaises(resume.ResumeLookupError):
                self.validate(self.rewrite_root(document))

    def test_resealed_wrong_bucket_id_and_projection_parity_fail(self):
        reference = self.build()
        original = self.payload(reference)
        bucket = resume.series_bucket("server_1", "202")
        original_child = self.payload(original["seriesBuckets"][bucket])
        for mutation in ("bucket", "id", "adult", "typed_bool", "duplicate"):
            with self.subTest(mutation=mutation):
                root, child = copy.deepcopy(original), copy.deepcopy(original_child)
                if mutation == "bucket":
                    child["bucket"] = f"{(int(bucket, 16) + 1) % 256:02x}"
                elif mutation == "id":
                    child["items"][0]["providerSeriesId"] += "0"
                elif mutation == "adult":
                    child["items"][0]["adult"] = not child["items"][0]["adult"]
                elif mutation == "typed_bool":
                    child["items"][0]["adult"] = int(child["items"][0]["adult"])
                else:
                    child["items"].append(copy.deepcopy(child["items"][0]))
                    child["recordCount"] = len(child["items"])
                bad_reference = self.rewrite_child(root, "seriesBuckets", bucket, child)
                with self.assertRaises(resume.ResumeLookupError):
                    self.validate(bad_reference)

    def test_episode_identity_playback_metadata_and_complete_coverage_are_validated(self):
        reference = self.build()
        original = self.payload(reference)
        bucket = resume.episode_bucket("server_1", "202", "9001")
        original_child = self.payload(original["episodeBuckets"][bucket])
        for mutation in ("parent", "extension", "missing", "duplicate"):
            with self.subTest(mutation=mutation):
                root, child = copy.deepcopy(original), copy.deepcopy(original_child)
                if mutation == "parent":
                    child["items"][0]["providerSeriesId"] = "elsewhere"
                elif mutation == "extension":
                    child["items"][0]["containerExtension"] = "mkv"
                elif mutation == "missing":
                    child["items"].pop(0)
                else:
                    child["items"].append(copy.deepcopy(child["items"][0]))
                child["recordCount"] = len(child["items"])
                with self.assertRaises(resume.ResumeLookupError):
                    self.validate(self.rewrite_child(root, "episodeBuckets", bucket, child))

    def test_corrupt_bytes_and_declared_sizes_fail(self):
        reference = self.build()
        tampered = dict(reference, uncompressedBytes=reference["uncompressedBytes"] + 1)
        with self.assertRaises(resume.ResumeLookupError):
            self.validate(tampered)
        path = self.output / reference["file"]
        data = bytearray(path.read_bytes())
        data[-5] ^= 1
        path.write_bytes(data)
        with self.assertRaises(resume.ResumeLookupError):
            self.validate(reference)

    def test_hard_byte_caps_reject_before_output_is_installed(self):
        for overrides in ({"root_compressed_bytes": 1}, {"root_expanded_bytes": 1},
                          {"bucket_compressed_bytes": 1}, {"bucket_expanded_bytes": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(resume.ResumeLookupError):
                resume.write_series_resume_lookup(
                    self.source, self.output, "server_1", self.server["series"],
                    limits=resume.LookupLimits(**overrides))
            self.assertFalse(self.output.exists() and any(self.output.iterdir()))

    def test_episode_row_ceiling_and_source_reference_counts_are_enforced(self):
        parents = self.parents()
        parent = next(row for row in parents if row["id"] == "202")
        original = parent["episodes"][0]
        original_bucket = resume.episode_bucket("server_1", "202", original["id"])
        identifier = next(str(value) for value in range(10000, 20000)
                          if resume.episode_bucket("server_1", "202", str(value)) == original_bucket)
        parent["episodes"].append(dict(original, id=identifier, episodeNumber=2))
        parent["availableEpisodeCount"] = 2
        parent["serverEpisodeCount"] = 2
        self.replace_parents(parents)
        with self.assertRaises(resume.ResumeLookupError):
            resume.write_series_resume_lookup(
                self.source, self.output, "server_1", self.server["series"],
                limits=resume.LookupLimits(max_episode_rows=1))
        self.assertFalse(self.output.exists())
        bad_reference = dict(self.server["series"], recordCount=self.server["series"]["recordCount"] + 1)
        with self.assertRaises(resume.ResumeLookupError):
            resume.write_series_resume_lookup(self.source, self.output, "server_1", bad_reference)

    def test_record_overflow_stops_before_reading_later_episode_shards(self):
        self.large_inventory(501)
        parent = next(row for row in self.parents() if row["id"] == "202")
        later_name = parent["episodeFiles"][-1]["file"]
        with mock.patch.object(resume, "_read_artifact", wraps=resume._read_artifact) as reads:
            with self.assertRaisesRegex(resume.ResumeLookupError, "limit"):
                resume.write_series_resume_lookup(
                    self.source, self.output, "server_1", self.server["series"],
                    limits=resume.LookupLimits(max_episode_rows=1))
        self.assertNotIn(later_name, [call.args[1].get("file") for call in reads.call_args_list])
        self.assertFalse(self.output.exists())

    def test_moves_removal_and_root_only_change_are_observable_without_home_mutation(self):
        first = self.build()
        original_index = copy.deepcopy(self.index)
        parents = self.parents()
        moved = next(row for row in parents if row["id"] == "202")
        moved["category"] = {"id": "Z", "name": "Moved folder"}
        self.replace_parents([row for row in parents if row["id"] != "203"])
        target = self.root / "moved"
        second = resume.write_series_resume_lookup(self.source, target, "server_1", self.server["series"])
        current = self.payload(second, target)
        child = self.payload(current["seriesBuckets"][resume.series_bucket("server_1", "202")], target)
        self.assertEqual(next(row for row in child["items"] if row["id"] == "202")["category"]["id"], "Z")
        removed_slot = current["seriesBuckets"][resume.series_bucket("server_1", "203")]
        if not removed_slot.get("empty"):
            self.assertFalse(any(row["id"] == "203" for row in self.payload(removed_slot, target)["items"]))
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(original_index["servers"][0]["home"], self.server["home"])
        self.assertNotIn("seriesResumeLookup", self.server)


class ResumeStagingTests(ResumeLookupFixture):
    def stage(self, output=None):
        index_path = self.source / "index.json"
        data = index_path.read_bytes()
        return staging.stage(index_path, [self.source], output or self.root / "local-stage",
                             expected_index_sha256=hashlib.sha256(data).hexdigest())

    def test_stage_only_has_an_investigation_descriptor_and_preserves_index(self):
        before = (self.source / "index.json").read_bytes()
        with mock.patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
            report = self.stage()
        self.assertEqual(report["status"], "validated_local_stage_only")
        self.assertFalse(report["readyForPublication"])
        self.assertFalse(report["publicReferenceActivated"])
        self.assertEqual(before, (self.source / "index.json").read_bytes())
        self.assertFalse((self.root / "local-stage" / "index.json").exists())

    def test_missing_inputs_report_a_dependency_without_lookup_outputs(self):
        self.large_inventory(501)
        parent = next(row for row in self.parents() if row["id"] == "202")
        missing_name = parent["episodeFiles"][0]["file"]
        (self.source / missing_name).unlink()
        report = self.stage()
        self.assertEqual(report["status"], "blocked_missing_checksum_linked_inputs")
        self.assertEqual(report["missingArtifacts"][0]["file"], missing_name)
        self.assertFalse((self.root / "local-stage" / "artifacts").exists())

    def test_existing_source_public_and_wrong_index_targets_are_rejected(self):
        for target in (self.source, self.source / "child", self.root / "public" / "new"):
            with self.subTest(target=target), self.assertRaises(staging.StageError):
                self.stage(target)
        with self.assertRaises(staging.StageError):
            staging.stage(self.source / "index.json", [self.source], self.root / "new",
                          expected_index_sha256="0" * 64)

    def test_index_schema_count_and_snapshot_cutoff_are_strictly_bound(self):
        baseline = copy.deepcopy(self.index)
        for number, mutation in enumerate(("schema_bool", "server_count_bool", "series_count", "snapshot")):
            document = copy.deepcopy(baseline)
            if mutation == "schema_bool":
                document["schemaVersion"] = True
            elif mutation == "server_count_bool":
                document["serverCount"] = True
            elif mutation == "series_count":
                document["servers"][0]["seriesCount"] += 1
            else:
                document["generatedAt"] = "2020-01-01T00:00:00Z"
            path = self.root / f"wrong-index-{number}.json"
            data = catalog.json_bytes(document)
            path.write_bytes(data)
            target = self.root / f"rejected-stage-{number}"
            with self.subTest(mutation=mutation), self.assertRaises(staging.StageError):
                staging.stage(path, [self.source], target, expected_index_sha256=hashlib.sha256(data).hexdigest())
            self.assertFalse(target.exists())

    def test_later_component_index_cutoff_preserves_matching_per_server_home_time(self):
        document = copy.deepcopy(self.index)
        document["generatedAt"] = "2026-10-16T16:31:00Z"
        path = self.root / "component-index.json"
        data = catalog.json_bytes(document)
        path.write_bytes(data)
        report = staging.stage(path, [self.source], self.root / "later-index-stage",
                               expected_index_sha256=hashlib.sha256(data).hexdigest())
        self.assertEqual(report["status"], "validated_local_stage_only")

    def test_malformed_optional_lookup_does_not_poison_existing_catalogue(self):
        document = copy.deepcopy(self.index)
        document["servers"][0]["seriesResumeLookup"] = {"schemaVersion": "unsupported", "file": "invalid"}
        (self.source / "index.json").write_bytes(catalog.json_bytes(document))
        self.assertEqual(catalog.validate(self.source, expected_servers=["server_1"]), {"serverCount": 1})
        report = self.stage()
        self.assertEqual(report["status"], "validated_local_stage_only")

    def test_deep_staging_paths_remain_usable(self):
        self.temporary.name = str(staging._filesystem_path(Path(self.temporary.name)))
        target = self.root
        number = 0
        while len(str(target)) <= 300:
            target /= f"resume-long-path-{number}-" + "x" * 24
            number += 1
        report = self.stage(target)
        self.assertEqual(report["status"], "validated_local_stage_only")
        self.assertGreater(len(str(target)), 300)


class SharedResumeGoldenTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "tests/fixtures/series_resume_lookup_v1"
        self.descriptor = json.loads((self.folder / "accepted_descriptor.json").read_text(encoding="utf-8"))
        self.artifacts = self.folder / self.descriptor["artifactDirectory"]

    def validate(self, reference):
        return resume.validate_series_resume_lookup(
            self.artifacts, reference, source_dir=self.artifacts,
            server_id=self.descriptor["serverId"], series_ref=self.descriptor["series"])

    def test_independent_vectors_preserve_exact_bytes_case_zeroes_and_pair_boundaries(self):
        vectors = json.loads((self.folder / "hash_vectors.json").read_text(encoding="utf-8"))["vectors"]
        for vector in vectors:
            with self.subTest(vector=vector):
                domain = "skytv-resume-series-v1" if vector["kind"] == "series" else "skytv-resume-episode-v1"
                parts = [domain, vector["serverId"], vector["providerSeriesId"]]
                if vector["kind"] == "episode":
                    parts.append(vector["providerEpisodeId"])
                encoded = "\0".join(parts).encode("utf-8")
                self.assertEqual(encoded.hex(), vector["utf8Hex"])
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), vector["sha256"])
                actual = (resume.series_bucket(vector["serverId"], vector["providerSeriesId"])
                          if vector["kind"] == "series" else resume.episode_bucket(
                              vector["serverId"], vector["providerSeriesId"], vector["providerEpisodeId"]))
                self.assertEqual(actual, vector["bucket"])
        for invalid in (None, 123, True, "", " 123", "123 ", "12/3", "é", "1\0" , "a" * 121):
            with self.subTest(invalid=invalid), self.assertRaises(resume.ResumeLookupError):
                resume.series_bucket("server_1", invalid)

    def test_golden_graph_is_fully_valid_and_writer_bytes_match_independent_generator(self):
        metrics = self.validate(self.descriptor["seriesResumeLookup"])
        self.assertEqual(metrics["seriesRecords"], 16)
        self.assertEqual(metrics["episodeRecords"], 13)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "candidate"
            reference = resume.write_series_resume_lookup(
                self.artifacts, output, self.descriptor["serverId"], self.descriptor["series"])
            self.assertEqual(reference, self.descriptor["seriesResumeLookup"])
            root = catalog.read_artifact(output, reference)
            reachable = [reference]
            for table in ("seriesBuckets", "episodeBuckets"):
                reachable.extend(value for value in root[table].values() if not value.get("empty"))
            for artifact in reachable:
                self.assertEqual((output / artifact["file"]).read_bytes(),
                                 (self.artifacts / artifact["file"]).read_bytes())

    def test_all_resealed_negative_graphs_are_rejected(self):
        negatives = json.loads((self.folder / "negative_cases.json").read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(negatives), 22)
        for case in negatives:
            with self.subTest(case=case["name"]), self.assertRaises(resume.ResumeLookupError):
                self.validate(case["seriesResumeLookup"])

    def test_valid_root_only_byte_change_preserves_facts_but_changes_identity(self):
        change = json.loads((self.folder / "root_only_change.json").read_text(encoding="utf-8"))
        self.assertNotEqual(change["before"]["sha256"], change["after"]["sha256"])
        before, after = self.validate(change["before"]), self.validate(change["after"])
        measured_bytes = {"maxCompressedBytes", "maxExpandedBytes"}
        self.assertEqual({key: value for key, value in before.items() if key not in measured_bytes},
                         {key: value for key, value in after.items() if key not in measured_bytes})
        self.assertNotEqual(before["maxCompressedBytes"], after["maxCompressedBytes"])

    def test_parent_row_ceiling_rejects_golden_collision_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "too-small"
            with self.assertRaises(resume.ResumeLookupError):
                resume.write_series_resume_lookup(
                    self.artifacts, output, self.descriptor["serverId"], self.descriptor["series"],
                    limits=resume.LookupLimits(max_parent_rows=1))
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
