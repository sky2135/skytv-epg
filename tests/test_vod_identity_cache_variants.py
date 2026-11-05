from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_vod_catalog as verification
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


NOW = 1_800_000_000


def source(title="Example Film (2025)", identifier="101"):
    return {"title": title, "tmdbId": identifier}


def adiyae_details():
    value = movie_details(101)
    value.update(title="Adiyae!", original_title="Adiyae!", release_date="2023-08-25",
                 translations={"translations": []})
    return value


class BarrierSession(FakeSession):
    def __init__(self, barrier, response):
        super().__init__(FakeResponse(response))
        self.barrier = barrier

    def get(self, *args, **kwargs):
        self.barrier.wait(timeout=5)
        return super().get(*args, **kwargs)


class IdentityCacheVariantTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, *responses, factory=tmdb.TmdbClient):
        return factory(self.root, token="offline-fixture", session=FakeSession(*responses), now=NOW)

    def seed_legacy_positive(self, client, item, *, identifier=101):
        details = movie_details(identifier)
        metadata = tmdb._metadata(details, "movie", "matched_id")
        key = tmdb._enrichment_identity(item, "movie")[0]
        # Deliberately seed the old physical namespace, as production caches do.
        client._write_identity_cache(key, metadata, 7200, tmdb._detail_identity(details, "movie"))
        return key, metadata

    def files(self, client):
        return {path.name: path.read_bytes() for path in client.cache_dir.glob("*.json")}

    def test_success_then_incompatible_miss_cannot_poison_the_success(self):
        good, bad = source("Adiyae! (2023)."), source("Adiyae! (2023)'")
        self.assertEqual(tmdb._enrichment_identity(good, "movie")[0], tmdb._enrichment_identity(bad, "movie")[0])
        client = self.client(FakeResponse(adiyae_details()), FakeResponse(adiyae_details()))
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(client.enrich(bad, "movie")["matchStatus"], "no_match")
        self.assertNotEqual(client.identity_cache_path(good, "movie"), client.identity_cache_path(bad, "movie"))
        client.session = FakeSession()
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(client.enrich(bad, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.session.calls, [])

    def test_incompatible_miss_first_does_not_suppress_the_valid_query(self):
        good, bad = source("Adiyae! (2023)."), source("Adiyae! (2023)'")
        client = self.client(FakeResponse(adiyae_details()), FakeResponse(adiyae_details()))
        self.assertEqual(client.enrich(bad, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        client.session = FakeSession()
        self.assertEqual(client.enrich(bad, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(client.session.calls, [])

    def test_concurrent_clients_keep_incompatible_outcomes_and_proofs_separate(self):
        good, bad = source("Adiyae! (2023)."), source("Adiyae! (2023)'")
        barrier = threading.Barrier(2)
        clients = [tmdb.TmdbClient(self.root, token="offline-fixture", now=NOW,
                   session=BarrierSession(barrier, adiyae_details())) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(client.enrich, item, "movie")
                       for client, item in zip(clients, (good, bad))]
            self.assertEqual([future.result()["matchStatus"] for future in futures], ["matched_id", "no_match"])
        replay = self.client()
        self.assertEqual(replay.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(replay.enrich(bad, "movie")["matchStatus"], "no_match")
        self.assertEqual(replay.session.calls, [])
        self.assertEqual(len(self.files(replay)), 2)

    def test_distinct_indic_display_alias_cannot_borrow_or_erase_a_verified_alias(self):
        good, bad = source("काला (2025)"), source("काली (2025)")
        self.assertEqual(tmdb._enrichment_identity(good, "movie")[0], tmdb._enrichment_identity(bad, "movie")[0])
        details = movie_details(101)
        details["translations"] = {"translations": [{"data": {"title": "काला"}}]}
        client = self.client(FakeResponse(details), FakeResponse(details))
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(client.enrich(bad, "movie")["matchStatus"], "no_match")
        client.session = FakeSession()
        self.assertEqual(client.enrich(good, "movie")["matchStatus"], "matched_id")
        self.assertEqual(client.session.calls, [])

    def test_legacy_positive_is_reused_without_a_request_copy_or_rewrite(self):
        client, item = self.client(), source()
        key, metadata = self.seed_legacy_positive(client, item)
        before = self.files(client)
        self.assertEqual(client.enrich(item, "movie"), metadata)
        self.assertEqual(client.identity_cache_path(item, "movie"), client._cache_path(key))
        self.assertEqual(client.session.calls, [])
        self.assertEqual(self.files(client), before)

    def test_legacy_negative_is_ignored_and_is_not_overwritten(self):
        client, item = self.client(FakeResponse(movie_details(101))), source()
        key = tmdb._enrichment_identity(item, "movie")[0]
        client._write_identity_cache(key, tmdb._empty(None, "no_match"), 7200, {})
        legacy = client._cache_path(key).read_bytes()
        self.assertEqual(client.enrich(item, "movie")["matchStatus"], "matched_id")
        self.assertEqual(len(client.session.calls), 1)
        self.assertEqual(client._cache_path(key).read_bytes(), legacy)
        self.assertNotEqual(client.identity_cache_path(item, "movie"), client._cache_path(key))

    def test_exact_rejected_variant_miss_wins_over_valid_legacy_positive(self):
        client, item = self.client(), source()
        key, _metadata = self.seed_legacy_positive(client, item)
        _, identifier, title, year = tmdb._enrichment_identity(item, "movie")
        negative = tmdb._empty(None, "no_match")
        negative["rejectedIdentity"] = True
        variant = tmdb._identity_cache_key(key, item, "movie", title, year)
        client._write_identity_cache(variant, negative, 7200, {})
        before = self.files(client)
        self.assertEqual(client.enrich(item, "movie"), negative)
        self.assertEqual(client.session.calls, [])
        self.assertEqual(self.files(client), before)
        verifier = self.client(factory=verification.VerificationClient)
        cached, _fresh, rejected = verifier._identity_cache(key, item, "movie", identifier, title, year)
        self.assertIsNone(cached)
        self.assertTrue(rejected)

    def test_expired_rejected_variant_does_not_resurrect_a_legacy_positive(self):
        client, item = self.client(), source()
        key, _metadata = self.seed_legacy_positive(client, item)
        _, identifier, title, year = tmdb._enrichment_identity(item, "movie")
        negative = tmdb._empty(None, "no_match")
        negative["rejectedIdentity"] = True
        client._write_identity_cache(tmdb._identity_cache_key(key, item, "movie", title, year), negative, -1, {})
        cached, fresh, rejected = client._main_identity_cache(key, item, "movie", identifier, title, year)
        self.assertEqual(cached, negative)
        self.assertFalse(fresh)
        self.assertTrue(rejected)
        self.assertEqual(client.session.calls, [])

    def test_invalid_variant_can_fall_back_only_to_independently_validated_legacy_proof(self):
        client, item = self.client(), source()
        key, metadata = self.seed_legacy_positive(client, item)
        _, identifier, title, year = tmdb._enrichment_identity(item, "movie")
        wrong_proof = tmdb._detail_identity(movie_details(101), "movie")
        wrong_proof["names"] = ["unrelated title"]
        variant = tmdb._identity_cache_key(key, item, "movie", title, year)
        client._write_identity_cache(variant, metadata, 7200, wrong_proof)
        self.assertEqual(client.enrich(item, "movie"), metadata)
        self.assertEqual(client.identity_cache_path(item, "movie"), client._cache_path(key))
        self.assertEqual(client.session.calls, [])
        self.seed_legacy_positive(client, item, identifier=202)
        cached, _fresh, _rejected = client._main_identity_cache(key, item, "movie", identifier, title, year)
        self.assertIsNone(cached)

    def test_explicitly_rejected_invalid_variant_cannot_revive_a_legacy_positive(self):
        client, item = self.client(), source()
        key, metadata = self.seed_legacy_positive(client, item)
        _, identifier, title, year = tmdb._enrichment_identity(item, "movie")
        metadata["rejectedIdentity"] = True
        proof = tmdb._detail_identity(movie_details(101), "movie")
        proof["names"] = ["unrelated title"]
        client._write_identity_cache(tmdb._identity_cache_key(key, item, "movie", title, year),
                                     metadata, 7200, proof)
        cached, fresh, rejected = client._main_identity_cache(key, item, "movie", identifier, title, year)
        self.assertIsNone(cached)
        self.assertFalse(fresh)
        self.assertTrue(rejected)
        self.assertEqual(client.session.calls, [])

    def test_id_free_variant_migration_preserves_original_supplied_id_and_selected_path(self):
        item = source(identifier=None)
        client = self.client()
        key, _identifier, title, year = tmdb._enrichment_identity(item, "movie")
        details = movie_details(101)
        variant = tmdb._identity_cache_key(key, item, "movie", title, year)
        client._write_identity_cache(variant, tmdb._metadata(details, "movie", "matched_search"),
                                     7200, tmdb._detail_identity(details, "movie"))
        original = client._cache_path(variant)
        before = self.files(client)
        verifier = self.client(factory=verification.VerificationClient)
        matching = dict(item, tmdbId="101")
        result = verifier.enrich(matching, "movie")
        self.assertEqual(result["tmdbId"], "101")
        self.assertEqual(verifier.identity_cache_path(matching, "movie"), original)
        self.assertEqual(verifier.session.calls, [])
        self.assertEqual(self.files(verifier), before)
        wrong = dict(item, tmdbId="202")
        wrong_key, wrong_id, wrong_title, wrong_year = tmdb._enrichment_identity(wrong, "movie")
        self.assertIsNone(verifier._identity_cache(wrong_key, wrong, "movie", wrong_id, wrong_title, wrong_year)[0])


if __name__ == "__main__":
    unittest.main()
