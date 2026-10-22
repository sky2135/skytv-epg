from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_vod_catalog as verification
import vod_tmdb as tmdb
from vod_poster_index import VerifiedPosterIndex
from vod_verification_checkpoint import Checkpoint
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


NOW = 1_800_000_000
POSTER = "https://image.tmdb.org/t/p/w500/abc123.jpg"


def record(server="server_1", provider_id="1", *, kind="movie", title="Example Film (2025)", tmdb_id="101"):
    cid = f"{server}:{kind}:{provider_id}"
    return {"contentId": cid, "serverId": server, "kind": kind, "languages": ["en"],
            "item": {"id": provider_id, "contentId": cid, "title": title, "tmdbId": tmdb_id,
                     "artwork": {"posterUrl": POSTER, "backdropUrl": None}},
            "baseline": {"metadataMatch": "unavailable", "tmdbId": None}}


def document(rows):
    return {"schemaVersion": 1, "generatedAt": "2026-10-22T17:41:42Z",
            "sourceGeneratedAt": "2026-10-22T17:40:00Z", "recordCount": len(rows),
            "inputFingerprint": "f" * 64, "records": rows}


class CountingClient:
    calls = []
    limits = []

    def __init__(self, cache_dir, *, token="", api_key="", max_requests=1500, session=None, **kwargs):
        self.cache_dir = Path(cache_dir)
        self.max_requests = max_requests
        type(self).limits.append(max_requests)
        self.stats = {"requests": 0, "cacheHits": 0}
        self.last_match_diagnostic = {}
        self.poster_index = VerifiedPosterIndex()

    def _identity_cache(self, *_args):
        return None, False, False

    def enrich(self, item, kind):
        type(self).calls.append((item["contentId"], kind))
        self.stats["requests"] += 1
        self.last_match_diagnostic = {"reason": "matched_id"}
        return {"tmdbId": item.get("tmdbId") or "101", "matchStatus": "matched_id",
                "releaseDate": "2025-04-03", "originalTitle": item["title"],
                "originalLanguage": "en", "rejectedIdentity": False}

    def reset_elapsed_budget(self, _seconds):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class VerificationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        (self.seed / "tmdb").mkdir(parents=True)
        self.state = self.root / "state"
        CountingClient.calls, CountingClient.limits = [], []

    def batch(self, **kwargs):
        return verification.run_batch(state_dir=self.state, test_id="fixture-test",
            component_dir=self.root / "component", seed_cache=self.seed,
            token="secret-fixture-token", source_artifact_id=123, seed_cache_key="frozen-seed",
            rate=20, workers=1, batch_seconds=30, client_factory=CountingClient,
            session_factory=lambda _limiter: FakeSession(), **kwargs)

    def seed_positive_without_provider_id_or_poster(self):
        old = {"title": "Example Film (2025)"}
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Example Film",
            "release_date": "2025-04-03"}], "total_pages": 1, "total_results": 1}),
            FakeResponse(movie_details()))
        with tmdb.TmdbClient(self.seed / "tmdb", token="fixture", session=session, now=NOW) as writer:
            result = writer.enrich(old, "movie")
        self.assertEqual(result["matchStatus"], "matched_search")
        return old

    def test_none_request_quota_allows_request_after_1500(self):
        session = FakeSession(FakeResponse(movie_details()))
        client = tmdb.TmdbClient(self.root / "cache", token="fixture", session=session,
                                 max_requests=None, now=NOW)
        client.stats["requests"] = 1500
        self.assertEqual(client._request("/movie/101")[0], "ok")
        self.assertEqual(client.stats["requests"], 1501)
        self.assertIsNone(client.max_requests)
        bounded = tmdb.TmdbClient(self.root / "normal", token="fixture", session=FakeSession(), now=NOW)
        bounded.stats["requests"] = 1500
        self.assertEqual(bounded._request("/movie/101"), ("unavailable", None))
        self.assertEqual(bounded.stats["budgetExhausted"], 1)

    def test_japan_sinks_year_in_title_is_not_removed(self):
        self.assertEqual(tmdb._title_and_year({"title": "Japan Sinks: 2020"}), ("Japan Sinks: 2020", None))
        self.assertEqual(tmdb._title_and_year({"title": "Japan Sinks: 2020", "releaseDate": "2020-07-09"}),
                         ("Japan Sinks: 2020", "2020"))
        self.assertEqual(tmdb._title_and_year({"title": "IN - Dangal 2016"}), ("Dangal", "2016"))

    def test_added_matching_provider_id_and_poster_reuse_current_positive_proof(self):
        self.seed_positive_without_provider_id_or_poster()
        original_cache = {path.name: path.read_bytes() for path in (self.seed / "tmdb").rglob("*.json")}
        current = record()["item"]
        session = FakeSession()
        client = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=session, now=NOW + 1)
        result = client.enrich(current, "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("101", "matched_search"))
        self.assertEqual(client.stats.get("sourceCacheMigrations"), 1)
        self.assertEqual(session.calls, [])
        self.assertEqual({path.name: path.read_bytes() for path in (self.seed / "tmdb").rglob("*.json")},
                         original_cache)
        resumed = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                  now=NOW + 2, max_requests=0)
        self.assertEqual(resumed.enrich(current, "movie")["matchStatus"], "matched_search")
        self.assertEqual(resumed.stats["requests"], 0)

    def test_current_rejected_negative_cannot_be_replaced_by_old_positive(self):
        self.seed_positive_without_provider_id_or_poster()
        current = record()["item"]
        client = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                 now=NOW + 1, max_requests=0)
        key = tmdb._enrichment_identity(current, "movie")[0]
        negative = tmdb._empty(None, "no_match")
        negative["rejectedIdentity"] = True
        client._write_identity_cache(key, negative, tmdb.MISS_TTL, {})
        result = client.enrich(current, "movie")
        self.assertEqual(result["matchStatus"], "unavailable")
        self.assertTrue(result["rejectedIdentity"])
        self.assertIsNone(result["tmdbId"])
        self.assertEqual(client.stats.get("sourceCacheMigrations", 0), 0)

    def test_added_wrong_provider_id_does_not_adopt_old_verified_identity(self):
        self.seed_positive_without_provider_id_or_poster()
        current = record(tmdb_id="999")["item"]
        client = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                 now=NOW + 1, max_requests=0)
        result = client.enrich(current, "movie")
        self.assertFalse(result["matchStatus"].startswith("matched_"))
        self.assertNotEqual(result["tmdbId"], "101")
        self.assertEqual(client.stats.get("sourceCacheMigrations", 0), 0)

    def test_one_record_batches_resume_without_reprocessing_or_publication(self):
        rows = [record(provider_id="1", tmdb_id="101"),
                record("server_2", "2", kind="series", title="Another Series (2025)", tmdb_id="202"),
                record("server_3", "3", title="Another Film (2025)", tmdb_id="303")]
        sentinel = self.root / "public" / "skytvplayer" / "index.json"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b'{"production":"keep this catalog"}')
        seed_file = self.seed / "tmdb" / "seed-sentinel.json"
        seed_file.write_bytes(b'{"privateCache":"read only"}')
        before = (sentinel.read_bytes(), seed_file.read_bytes())
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)) as load:
            reports = [self.batch(batch_records=1) for _ in range(3)]
        self.assertEqual(load.call_count, 1)
        self.assertEqual([row["totals"]["completed"] for row in reports], [1, 2, 3])
        self.assertEqual([row["shouldContinue"] for row in reports], [True, True, False])
        self.assertEqual(len(CountingClient.calls), 3)
        self.assertEqual(len(set(CountingClient.calls)), 3)
        self.assertEqual(reports[-1]["totals"]["newMatches"], 3)
        self.assertEqual(reports[-1]["apiUsage"]["requests"], 3)
        self.assertEqual(reports[-1]["attemptEvents"], 3)
        self.assertTrue(all(value is None for value in CountingClient.limits if value != 0))
        self.assertEqual((sentinel.read_bytes(), seed_file.read_bytes()), before)
        self.assertFalse(reports[-1]["productionCatalogWritten"])
        with Checkpoint(self.state / "checkpoint.sqlite") as checkpoint:
            self.assertTrue(all(row["checkpoint"]["attempts"] == 1 for row in checkpoint.records()))
        self.assertNotIn("secret-fixture-token", (self.state / "report.json").read_text(encoding="utf-8"))

    def test_matching_duplicates_across_servers_share_one_api_outcome(self):
        rows = [record("server_1", "1"), record("server_2", "2"), record("server_3", "3")]
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)):
            report = self.batch(batch_records=10)
        self.assertEqual(len(CountingClient.calls), 1)
        self.assertEqual(report["batchCompletedGroups"], 1)
        self.assertEqual(report["totals"]["matched"], 3)
        self.assertEqual(report["apiUsage"]["requests"], 1)
        self.assertEqual(report["attemptEvents"], 1)

    def test_interrupted_initialization_can_resume_after_seed_cache_copy(self):
        rows = [record()]
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)):
            with mock.patch.object(verification, "prepare_records", side_effect=RuntimeError("simulated interruption")):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    self.batch(batch_records=1)
            resumed = self.batch(batch_records=1)
        self.assertEqual(resumed["totals"]["completed"], 1)
        self.assertEqual(resumed["totals"]["newMatches"], 1)
        self.assertEqual(len(CountingClient.calls), 1)

    def test_interrupted_initial_metadata_commit_resumes_consistently(self):
        rows = [record()]
        original_set = Checkpoint.set_meta
        def interrupted(checkpoint, key, value):
            if key == "sourceIdentity":
                raise RuntimeError("interrupted initial metadata")
            return original_set(checkpoint, key, value)
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)):
            with mock.patch.object(Checkpoint, "set_meta", interrupted):
                with self.assertRaisesRegex(RuntimeError, "interrupted initial metadata"):
                    self.batch(batch_records=1)
            resumed = self.batch(batch_records=1)
        self.assertEqual(resumed["totals"]["completed"], 1)
        self.assertIsNotNone(resumed["sourceIdentity"])
        self.assertGreaterEqual(resumed["totalWallElapsedSeconds"], 0)
        self.assertEqual(len(CountingClient.calls), 1)

    def test_private_evidence_is_loaded_before_grouping_and_is_not_fetched(self):
        rows = [record("server_1", "1"), record("server_2", "2")]
        class Collector:
            def __init__(self, path, **kwargs):
                self.assertions = kwargs
                if kwargs != {"max_requests": 0, "max_seconds": 0}:
                    raise AssertionError("Verification must only load frozen evidence")
            def load(self, item):
                return {"contentId": item["contentId"], "fingerprint": "a" * 64,
                        "directors": ["Director"], "cast": ["Actor One", "Actor Two"]}
            def close(self):
                pass
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)), \
                mock.patch.object(verification, "MovieEvidenceCollector", Collector):
            report = self.batch(batch_records=10)
        self.assertEqual(len(CountingClient.calls), 2)
        self.assertEqual(report["batchCompletedGroups"], 2)
        with Checkpoint(self.state / "checkpoint.sqlite") as checkpoint:
            saved = checkpoint.records()
            self.assertTrue(all(row["item"]["_identityEvidence"]["contentId"] == row["contentId"] for row in saved))
            self.assertNotEqual(saved[0]["identityKey"], saved[1]["identityKey"])

    def test_verified_cache_is_reused_and_poster_index_rebuilt_after_resume(self):
        self.seed_positive_without_provider_id_or_poster()
        row = record()
        row["baseline"] = {"metadataMatch": "matched_search", "tmdbId": "101"}
        def client_factory(path, **kwargs):
            return verification.VerificationClient(path, now=NOW + 1, **kwargs)
        probes = []
        original = verification.warm_completed
        def probe(checkpoint, client):
            original(checkpoint, client)
            probes.append(client.poster_index.lookup("/abc123.jpg", "movie", "example film", "2025"))
        kwargs = dict(state_dir=self.state, test_id="fixture-test", component_dir=self.root / "component",
            seed_cache=self.seed, token="secret-fixture-token", source_artifact_id=123,
            seed_cache_key="frozen-seed", rate=20, workers=1, batch_seconds=30,
            batch_records=1, client_factory=client_factory, session_factory=lambda _limiter: FakeSession())
        with mock.patch.object(verification, "load_component_input", return_value=document([row])) as load, \
                mock.patch.object(verification, "warm_completed", side_effect=probe):
            first = verification.run_batch(**kwargs)
            gc.collect()
            second = verification.run_batch(**kwargs)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(first["initialVerifiedCacheReuse"], 1)
        self.assertEqual(second["initialVerifiedCacheReuse"], 1)
        self.assertEqual(probes, ["101", "101"])
        self.assertEqual(first["api"]["wireAttempts"], 0)
        self.assertEqual(second["totals"]["retainedVerified"], 1)
        self.assertEqual(second["totals"]["newMatches"], 0)
        self.assertEqual(second["totals"]["lostVerification"], 0)

    def test_unexpected_worker_error_preserves_committed_checkpoint_and_seals_report(self):
        rows = [record(), record("server_2", "2", title="Another Film (2025)", tmdb_id="202")]
        with mock.patch.object(verification, "load_component_input", return_value=document(rows)):
            first = self.batch(batch_records=1)
            self.assertEqual(first["totals"]["completed"], 1)
            with mock.patch.object(CountingClient, "enrich", side_effect=RuntimeError("secret-fixture-token response")):
                failed = self.batch(batch_records=1)
        self.assertEqual(failed["fatalError"], "verification_exception_RuntimeError")
        self.assertFalse(failed["shouldContinue"])
        self.assertTrue(failed["hasUnfinished"])
        self.assertFalse(failed["productionCatalogWritten"])
        self.assertEqual((failed["totals"]["completed"], failed["totals"]["remaining"]), (1, 1))
        saved = json.loads((self.state / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, failed)
        self.assertNotIn("secret-fixture-token", (self.state / "report.json").read_text(encoding="utf-8"))
        with Checkpoint(self.state / "checkpoint.sqlite") as checkpoint:
            self.assertEqual(checkpoint.get(rows[0]["contentId"])["checkpoint"]["state"], "done")
            self.assertEqual(checkpoint.get(rows[0]["contentId"])["checkpoint"]["attempts"], 1)
            self.assertEqual(checkpoint.get(rows[1]["contentId"])["checkpoint"]["state"], "pending")

    def test_later_official_poster_collision_blocks_warm_completion_and_reopens_old_proof(self):
        rows = [record(tmdb_id=None), record("server_2", "2", title="Other Film (2025)", tmdb_id="202")]
        client = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                 now=NOW, max_requests=0)
        metadata = [tmdb._metadata(movie_details(), "movie", "matched_poster"),
                    tmdb._metadata({**movie_details(202), "title": "Other Film", "original_title": "Other Film"},
                                   "movie", "matched_id")]
        for index, row in enumerate(rows):
            key = tmdb._enrichment_identity(row["item"], "movie")[0]
            proof = {"names": ["example film" if index == 0 else "other film"], "year": "2025",
                     "originalTitle": "original film" if index == 0 else "other film",
                     "tmdbId": "101" if index == 0 else "202", "mediaKind": "movie",
                     "posterPolicy": tmdb.POSTER_POLICY, "posterPath": "/abc123.jpg",
                     "posterPaths": ["/abc123.jpg"]}
            client._write_identity_cache(key, metadata[index], tmdb.DETAIL_TTL, proof)
        prepared, warmed = verification.prepare_records(document(rows), self.seed / "provider-evidence", client)
        self.assertTrue(client.poster_index.collided("/abc123.jpg"))
        self.assertEqual([cid for cid, _metadata in warmed], [rows[1]["contentId"]])
        with Checkpoint(self.state / "checkpoint.sqlite") as checkpoint:
            checkpoint.initialize(prepared, {"frozen": "poster-collision-fixture"})
            for row, value in zip(rows, metadata):
                checkpoint.record_result(row["contentId"], value, {"reason": value["matchStatus"]})
            self.assertEqual(verification.reopen_collided_posters(checkpoint, client), 1)
            self.assertEqual(checkpoint.get(rows[0]["contentId"])["checkpoint"]["state"], "pending")
            self.assertEqual(checkpoint.get(rows[0]["contentId"])["checkpoint"]["reason"], "poster_collision")
            self.assertEqual(checkpoint.get(rows[1]["contentId"])["checkpoint"]["state"], "done")
            self.assertEqual(checkpoint.report()["totals"]["matched"], 1)

    def test_both_reopened_poster_matches_stay_rejected_after_empty_index_resume(self):
        rows = [record(tmdb_id=None), record("server_2", "2", title="Other Film (2025)", tmdb_id=None)]
        client = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                 now=NOW, max_requests=0)
        metadata = [tmdb._metadata(movie_details(), "movie", "matched_poster"),
                    tmdb._metadata({**movie_details(202), "title": "Other Film", "original_title": "Other Film"},
                                   "movie", "matched_poster")]
        for index, row in enumerate(rows):
            key = tmdb._enrichment_identity(row["item"], "movie")[0]
            proof = {"names": ["example film" if index == 0 else "other film"], "year": "2025",
                     "originalTitle": "original film" if index == 0 else "other film",
                     "tmdbId": "101" if index == 0 else "202", "mediaKind": "movie",
                     "posterPolicy": tmdb.POSTER_POLICY, "posterPath": "/abc123.jpg",
                     "posterPaths": ["/abc123.jpg"]}
            client._write_identity_cache(key, metadata[index], tmdb.DETAIL_TTL, proof)
            client.poster_index.add("movie", proof["tmdbId"], ["/abc123.jpg"], proof)
        checkpoint_path = self.state / "checkpoint.sqlite"
        with Checkpoint(checkpoint_path) as checkpoint:
            checkpoint.initialize(rows, {"frozen": "two-revoked-poster-proofs"})
            for row, value in zip(rows, metadata):
                checkpoint.record_result(row["contentId"], value, {"reason": "matched_poster"})
            self.assertEqual(verification.reopen_collided_posters(checkpoint, client), 2)
            self.assertEqual(checkpoint.completed_records(), [])
        resumed = verification.VerificationClient(self.seed / "tmdb", token="fixture", session=FakeSession(),
                                                  now=NOW + 1, max_requests=0)
        # A new process starts with an empty shared index, not the first batch's
        # still-referenced object. Simulate that reset explicitly.
        resumed.poster_index = VerifiedPosterIndex()
        self.assertFalse(resumed.poster_index.collided("/abc123.jpg"))
        with Checkpoint(checkpoint_path) as checkpoint:
            verification.seed_collision_tombstones(resumed, checkpoint.get_meta("collidedPosterPaths"))
            verification.warm_completed(checkpoint, resumed)
            self.assertTrue(resumed.poster_index.collided("/abc123.jpg"))
            self.assertIsNone(resumed.poster_index.lookup("/abc123.jpg", "movie", "example film", "2025"))
            for row in rows:
                key, identifier, title, year = tmdb._enrichment_identity(row["item"], "movie")
                value, _fresh, _rejected = resumed._identity_cache(key, row["item"], "movie", identifier, title, year)
                self.assertIsNone(value)
            self.assertEqual(checkpoint.report()["totals"]["matched"], 0)


if __name__ == "__main__":
    unittest.main()
