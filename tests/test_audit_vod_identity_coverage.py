from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import audit_vod_identity_coverage as coverage
import build_skytvplayer_catalog as app
import build_vod_catalogs as vod

NOW = "2026-10-22T13:13:55Z"


def item(identifier, languages=("en",), *, kind="movie", server="server_1", status="unavailable", tmdb=None, **extra):
    return dict(id=str(identifier), contentId=f"{server}:{kind}:{identifier}", title=f"Private title {identifier}",
                category={"id": "1", "name": "Provider category"}, language="mul" if len(languages) > 1 else languages[0],
                languages=list(languages), metadataMatch=status, tmdbId=tmdb, **extra)


class VodIdentityCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()

    def build(self, movies=(), series=(), *, servers=("server_1",), now=NOW):
        rows = []
        for server in servers:
            entry = {"serverId": server, "epg": {}}
            for kind, plural, source in (("movie", "movies", movies), ("series", "series", series)):
                records = [copy.deepcopy(row) for row in source if row["contentId"].startswith(server + ":")]
                entry[plural] = app.write_dataset(self.catalog, server, kind, records, now)
                entry["movieCount" if kind == "movie" else "seriesCount"] = len(records)
            rows.append(entry)
        index = dict(schemaVersion=1, appName="SkyTVPlayer", generatedAt=now, serverCount=len(rows), servers=rows)
        app.atomic_json(self.catalog / "index.json", index)
        return index

    def audit(self, servers=("server_1",)):
        return coverage.audit(self.catalog, expected_servers=servers)

    def test_language_overlap_union_unknown_and_unverified_hints(self):
        self.build([
            item(1, ("en", "hi"), status="matched_poster", tmdb="101"),
            item(2, ("en",), status="unavailable", tmdb="102", imdbId="tt1234567", imdbVerified=True),
            item(3, ("pa",), status="no_match"),
            item(4, ("und",), status="matched_id", tmdb="104"),
            item(5, ("fr",), status="matched_search", tmdb="105"),
            item(6, ("hi",), status="matched_id", tmdb=None),
        ], [item(7, ("pa",), kind="series", status="matched_search", tmdb="107")])
        report = self.audit()
        self.assertEqual(report["totalRecords"], 7)
        selected = report["selectedLanguageUnionTotal"]
        self.assertEqual((selected["total"], selected["verifiedTmdb"], selected["missingBoth"]), (5, 2, 3))
        self.assertEqual(report["perLanguage"]["server_1/movie/en"]["total"], 2)
        self.assertEqual(report["perLanguage"]["server_1/movie/hi"]["total"], 2)
        self.assertEqual(report["perLanguage"]["server_1/movie/pa"]["total"], 1)
        self.assertEqual(report["selectedLanguageUnion"]["server_1/movie"]["total"], 4)
        self.assertEqual(report["unknownLanguage"]["server_1/movie"]["total"], 1)
        self.assertEqual(report["multilingual"]["server_1/movie"]["total"], 1)
        self.assertEqual(selected["unverifiedTmdbFieldPresent"], 1)
        self.assertEqual(selected["imdbFieldPresent"], 1)
        self.assertEqual(selected["verifiedImdb"], 0)
        self.assertEqual(report["metadataMatchCountsTotal"]["matched_poster"], 1)
        self.assertNotIn("Private title", json.dumps(report))

    def test_reviewed_identity_requires_exact_allowlisted_id_and_evidence(self):
        correction = {"metadata": {"tmdbId": "321"}, "evidenceURLs": ["https://www.themoviedb.org/movie/321"]}
        evidence = {"method": "reviewed_correction", "sourceURLs": correction["evidenceURLs"]}
        self.build([
            item(1, status="reviewed_correction", tmdb="321", metadataEvidence=evidence),
            item(2, status="reviewed_correction", tmdb="999", metadataEvidence=evidence),
            item(3, status="reviewed_correction", tmdb="321", metadataEvidence={"method": "provider_hint"}),
        ])
        with mock.patch.object(coverage, "correction_for", return_value=correction):
            report = self.audit()
        self.assertEqual(report["allRecordsTotal"]["verifiedTmdb"], 1)
        with mock.patch.object(coverage, "correction_for", return_value=None):
            report = self.audit()
        self.assertEqual(report["allRecordsTotal"]["verifiedTmdb"], 0)

    def test_missing_imdb_fields_and_zero_language_groups_are_explicit(self):
        self.build([item(1, ("und",))])
        report = self.audit()
        self.assertEqual(report["allRecordsTotal"]["imdbFieldPresent"], 0)
        self.assertEqual(report["allRecordsTotal"]["verifiedImdb"], 0)
        self.assertEqual(report["selectedLanguageUnionTotal"]["total"], 0)
        self.assertEqual(len(report["perLanguage"]), 6)
        self.assertEqual(report["metadataMatchCountsTotal"]["matched_poster"], 0)
        self.assertEqual(report["matcherStrategyVersion"], coverage.MATCH_STRATEGY)
        self.assertEqual(report["posterPolicyVersion"], coverage.POSTER_POLICY)

    def test_unknown_matched_status_cannot_turn_provider_hint_into_verified_id(self):
        self.build([item(1, status="matched_provider_hint", tmdb="101"),
                    item(2, status="matched_poster", tmdb="102")])
        report = self.audit()
        self.assertEqual(report["allRecordsTotal"]["verifiedTmdb"], 1)
        self.assertEqual(report["allRecordsTotal"]["unverifiedTmdbFieldPresent"], 1)
        self.assertEqual(report["metadataMatchCountsTotal"]["matched_provider_hint"], 1)

    def test_same_provider_id_on_different_servers_or_kinds_stays_separate(self):
        self.build([item(1), item(1, server="server_2")], [item(1, kind="series")], servers=("server_1", "server_2"))
        report = self.audit(("server_1", "server_2"))
        self.assertEqual(report["totalRecords"], 3)
        self.assertEqual(report["selectedLanguageUnionTotal"]["total"], 3)

    def test_checksum_tampering_preserves_existing_report(self):
        index = self.build([item(1)])
        destination = self.root / "report.json"
        destination.write_bytes(b"previous report")
        manifest = app.read_artifact(self.catalog, index["servers"][0]["movies"])
        path = self.catalog / manifest["shards"][0]["file"]
        path.write_bytes(path.read_bytes() + b"corrupt")
        with self.assertRaises(vod.CatalogBuildError):
            coverage.write_report(self.catalog, destination, expected_servers=("server_1",))
        self.assertEqual(destination.read_bytes(), b"previous report")

    def test_hash_valid_shard_server_kind_generation_and_count_mismatch_are_rejected(self):
        for change in ({"serverId": "server_2"}, {"dataset": "vod-series"},
                       {"generatedAt": "2026-10-21T13:13:55Z"}, {"recordCount": 2}, {"schemaVersion": True}):
            with self.subTest(change=change):
                index = self.build([item(1)])
                manifest = app.read_artifact(self.catalog, index["servers"][0]["movies"])
                shard = app.read_artifact(self.catalog, manifest["shards"][0])
                shard.update(change)
                reference = app.write_artifact(self.catalog, "server_1_movies_changed", shard, count=1)
                manifest["shards"] = [reference]
                index["servers"][0]["movies"] = app.write_artifact(
                    self.catalog, "server_1_movies_manifest_changed", manifest, count=1, compressed=False)
                app.atomic_json(self.catalog / "index.json", index)
                with self.assertRaises(vod.CatalogBuildError):
                    self.audit()

    def test_snapshot_index_change_during_read_is_rejected(self):
        index = self.build([item(1)])
        original = app.read_artifact

        def change_index(*args):
            payload = original(*args)
            app.atomic_json(self.catalog / "index.json", dict(index, generatedAt="2026-10-21T13:13:55Z"))
            return payload

        with mock.patch.object(app, "read_artifact", side_effect=change_index):
            with self.assertRaises(vod.CatalogBuildError):
                self.audit()

    def test_manifest_generation_or_wrong_expected_server_set_is_rejected(self):
        index = self.build([item(1)])
        with self.assertRaises(vod.CatalogBuildError):
            self.audit(("server_1", "server_2"))
        manifest = app.read_artifact(self.catalog, index["servers"][0]["movies"])
        manifest["generatedAt"] = "2026-10-21T13:13:55Z"
        index["servers"][0]["movies"] = app.write_artifact(
            self.catalog, "server_1_movies_bad_manifest", manifest, count=1, compressed=False)
        app.atomic_json(self.catalog / "index.json", index)
        with self.assertRaises(vod.CatalogBuildError):
            self.audit()

    def test_changed_live_references_preserve_catalog_identity_and_counts(self):
        index = self.build([item(1)])
        before = self.audit()
        index["servers"][0]["epg"] = {"programme": {"file": "../EPG/new.json.gz", "sha256": "a" * 64}}
        app.atomic_json(self.catalog / "index.json", index)
        self.assertEqual(before, self.audit())

    def test_duplicate_records_and_inconsistent_language_metadata_fail(self):
        for record in (dict(item(1), contentId="server_1:invalid:1"), dict(item(1), language="en", languages=["en", "hi"]),
                       dict(item(1), languages=["en", "en"])):
            with self.subTest(record=record):
                self.build([record])
                with self.assertRaises(vod.CatalogBuildError):
                    self.audit()
        index = self.build([item(1)])
        manifest = app.read_artifact(self.catalog, index["servers"][0]["movies"])
        shard = app.read_artifact(self.catalog, manifest["shards"][0])
        shard["items"].append(shard["items"][0])
        shard["recordCount"] = 2
        manifest["shards"] = [app.write_artifact(self.catalog, "server_1_movies_duplicated", shard, count=2)]
        manifest["totalRecords"] = index["servers"][0]["movieCount"] = 2
        index["servers"][0]["movies"] = app.write_artifact(
            self.catalog, "server_1_movies_manifest_duplicated", manifest, count=1, compressed=False)
        app.atomic_json(self.catalog / "index.json", index)
        with self.assertRaises(vod.CatalogBuildError):
            self.audit()

    def test_output_cannot_replace_index_or_content_addressed_artifact(self):
        index = self.build([item(1)])
        for name in ("index.json", index["servers"][0]["movies"]["file"]):
            with self.subTest(name=name), self.assertRaises(vod.CatalogBuildError):
                coverage.write_report(self.catalog, self.catalog / name, expected_servers=("server_1",))

    def test_cli_writes_aggregate_report_and_optional_github_summary(self):
        self.build([item(1, status="matched_poster", tmdb="101")])
        summary = self.root / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), mock.patch("builtins.print"):
            self.assertEqual(coverage.main(["--catalog-dir", str(self.catalog), "--expected-server", "server_1", "--github-summary"]), 0)
        report = json.loads((self.catalog / "identity_coverage.json").read_bytes())
        self.assertEqual(report["allRecordsTotal"]["verifiedTmdb"], 1)
        self.assertIn("server_1/movie", summary.read_text())
        self.assertNotIn("Private title", summary.read_text())


if __name__ == "__main__":
    unittest.main()
