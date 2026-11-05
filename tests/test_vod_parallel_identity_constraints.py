from __future__ import annotations

import copy
from contextlib import closing
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_skytvplayer_catalog as app
import verify_vod_catalog as verification
import enrich_vod_catalog as rolling
import vod_tmdb as tmdb
from vod_enrichment_queue import EnrichmentQueue
from test_vod_tmdb import FakeResponse, FakeSession, movie_details
import test_verify_vod_catalog as verification_fixtures


def item(title, *, server="server_1", identifier="1", kind="movie", **changes):
    result = {"id": identifier, "contentId": f"{server}:{kind}:{identifier}", "title": title,
              "tmdbId": None, "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/abc123.jpg"}}
    result.update(changes)
    return result


def mismatched_pairs():
    yield "movie", item("Adiyae! (2023)."), item("Adiyae! (2023)'", identifier="2")
    for title, year in (("Cubicles", "2019"), ("Family Aaj Kal", "2024"),
                        ("Kota Factory", "2019"), ("Maharani", "2021")):
        yield ("series", item(f"{title} (Bangla)", kind="series", releaseDate=f"{year}-01-01"),
               item(f"{title} (Bangla) ({year})", kind="series", server="server_2", identifier="2"))
    yield ("series", item("Love Island (US)", kind="series", releaseDate="2019-07-09"),
           item("Love Island US", kind="series", identifier="2", releaseDate="2019-07-09"))


class AdiyaeSession:
    """Route only the expected offline search/detail requests."""
    def __init__(self, calls):
        self.calls = calls

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        if url.endswith("/search/movie"):
            return FakeResponse({"page": 1, "total_pages": 1, "total_results": 1,
                                 "results": [{"id": 101, "title": "Adiyae!", "release_date": "2023-08-25"}]})
        if url.endswith("/movie/101"):
            return FakeResponse({"id": 101, "title": "Adiyae!", "original_title": "Adiyae!",
                                 "release_date": "2023-08-25", "poster_path": "/abc123.jpg",
                                 "translations": {"translations": []}})
        raise AssertionError("Unexpected offline URL")

    def close(self):
        pass


class ParallelIdentityConstraintTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_all_six_observed_non_equivalent_peers_get_separate_result_keys(self):
        for kind, first, second in mismatched_pairs():
            with self.subTest(kind=kind, first=first["title"], second=second["title"]):
                old_first = tmdb._enrichment_identity(first, kind)[0]
                old_second = tmdb._enrichment_identity(second, kind)[0]
                self.assertEqual(old_first, old_second)
                first_key = app._metadata_result_key(first, kind, None)
                second_key = app._metadata_result_key(second, kind, None)
                self.assertEqual(first_key[0], old_first)
                self.assertEqual(second_key[0], old_second)
                self.assertNotEqual(first_key, second_key)

    def test_equivalent_punctuation_and_cross_server_duplicates_still_share_results(self):
        first = item("Adiyae! (2023).")
        second = item("Adiyae! (2023),", server="server_2", identifier="2")
        self.assertEqual(app._metadata_result_key(first, "movie", None),
                         app._metadata_result_key(second, "movie", None))
        first = item("EN - Example Film (2025)")
        second = item("IN - Example Film (2025)", server="server_2", identifier="2")
        self.assertEqual(app._metadata_result_key(first, "movie", None),
                         app._metadata_result_key(second, "movie", None))

    def test_strict_source_and_effective_keys_keep_indic_vowel_variants_separate(self):
        first, second = item("काला (2025)"), item("काली (2025)", identifier="2")
        self.assertEqual(tmdb._enrichment_identity(first, "movie")[0], tmdb._enrichment_identity(second, "movie")[0])
        self.assertNotEqual(app._metadata_result_key(first, "movie", None),
                            app._metadata_result_key(second, "movie", None))

    def test_independent_corroboration_guard_is_part_of_result_identity(self):
        source = item("Example Film (2025)")
        context = ("Example Film", "2025", None)
        ordinary_constraints = tmdb._provider_query_constraints(source, "movie", *context[:2])
        with mock.patch.object(app, "_provider_query_constraints", return_value=ordinary_constraints):
            ordinary = app._metadata_result_key(source, "movie", None)
        guarded_constraints = dict(ordinary_constraints, requiresCorroboration=True)
        with mock.patch.object(app, "_provider_query_constraints", return_value=guarded_constraints):
            guarded = app._metadata_result_key(source, "movie", None)
        self.assertEqual(ordinary[0], guarded[0])
        self.assertNotEqual(ordinary, guarded)

    def test_enrich_uses_the_same_shared_query_guard_as_grouping(self):
        source = item("Example Film (2025)", tmdbId="101", artwork={})
        client = tmdb.TmdbClient(self.root / "cache", token="fixture", session=AdiyaeSession([]))
        with mock.patch.object(tmdb, "_provider_query_context", return_value=("Example Film", "2025", None, True)) as context:
            self.assertEqual(client.enrich(source, "movie")["matchStatus"], "no_match")
            self.assertEqual(client.stats["requests"], 0)
            self.assertGreaterEqual(context.call_count, 1)

    def test_prefetch_does_not_copy_success_to_peer_with_different_cleanup(self):
        calls = []
        first, second = item("Adiyae! (2023)."), item("Adiyae! (2023)'", identifier="2")
        groups = [{"serverId": "server_1", "kind": "movie", "prepared": {
            "normalized": [first, second], "raw_rows": {}, "details": {}, "corrections": {}}}]
        client = tmdb.TmdbClient(self.root / "cache", token="fixture", max_requests=10,
                                 session=AdiyaeSession(calls))
        prefetched = app._prefetch_metadata(groups, client, 1, None, None,
                                            session_factory=lambda *_args: AdiyaeSession(calls))
        self.assertEqual(prefetched.enrich(first, "movie")["matchStatus"], "matched_search")
        self.assertEqual(prefetched.enrich(second, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.stats["parallelUniqueIdentities"], 2)
        self.assertEqual(client.stats["parallelResultReuse"], 0)

    def test_verifier_records_independent_outcomes_for_non_equivalent_source_peers(self):
        calls = []
        rows = []
        for index, source in enumerate((item("Adiyae! (2023)."), item("Adiyae! (2023)'", identifier="2")), 1):
            row = verification_fixtures.record(provider_id=str(index), title=source["title"], tmdb_id=None)
            row["item"] = copy.deepcopy(source)
            rows.append(row)
        seed = self.root / "seed"
        (seed / "tmdb").mkdir(parents=True)
        report = verification.run_batch(state_dir=self.root / "state", test_id="identity-peer-fixture",
            component_dir=self.root / "component", seed_cache=seed, token="offline-secret-token",
            source_artifact_id=123, seed_cache_key="offline-seed", workers=2, rate=10, batch_seconds=30,
            input_loader=lambda _path: verification_fixtures.document(rows),
            session_factory=lambda _limiter: AdiyaeSession(calls))
        self.assertTrue(report["complete"])
        self.assertEqual(report["totals"]["matched"], 1)
        self.assertEqual(report["totals"]["unresolved"], 1)
        self.assertEqual(report["unresolvedReasons"], {"missing_identity": 1})
        self.assertEqual(report.get("batchCompletedGroups"), 2, report.get("fatalError"))
        self.assertFalse(report["productionCatalogWritten"])

    def test_rolling_warm_expiry_uses_actual_variant_or_legacy_selected_file(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                seed = root / "seed"
                source = verification_fixtures.record()
                writer = tmdb.TmdbClient(seed / "tmdb", token="fixture", session=FakeSession(FakeResponse(movie_details())))
                writer.enrich(source["item"], "movie")
                path = writer.identity_cache_path(source["item"], "movie")
                envelope = json.loads(path.read_text(encoding="utf-8"))
                expires = 1_900_000_123
                envelope["expiresAt"] = expires
                if legacy:
                    path.unlink()
                    path = writer._cache_path(tmdb._enrichment_identity(source["item"], "movie")[0])
                    envelope["data"]["matchStrategy"] = 5
                path.write_text(json.dumps(envelope), encoding="utf-8")
                before = {entry.name: entry.read_bytes() for entry in (seed / "tmdb/tmdb-v1").glob("*.json")}
                state = root / "state"
                with mock.patch.object(rolling, "load_component_input", return_value=verification_fixtures.document([source])):
                    report = rolling.run_batch(state_dir=state, component_dir=root / "component", seed_cache=seed,
                        token="offline-secret-token", workers=1, rate=10, batch_seconds=5,
                        session_factory=lambda _limiter: FakeSession())
                self.assertIsNone(report["fatalError"])
                self.assertEqual(report["processedThisBatch"], 0)
                self.assertEqual(report["api"]["wireAttempts"], 0)
                with closing(EnrichmentQueue(state / "queue.sqlite")) as queue:
                    record = queue.records()[0]
                self.assertEqual(record["queue"]["nextAttemptAt"], expires)
                after = {entry.name: entry.read_bytes() for entry in (state / "cache/tmdb/tmdb-v1").glob("*.json")}
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
