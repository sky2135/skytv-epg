import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_poster_index as posters
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


class ImportedCollisionTests(unittest.TestCase):
    def test_missing_file_is_an_empty_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(posters.seed_collision_file(
                posters.VerifiedPosterIndex(), Path(directory) / "missing.json"), 0)

    def test_invalid_evidence_is_rejected_before_changing_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collisions.json"
            for value in ([], {}, {"schemaVersion": 2, "collidedPosterPaths": []},
                          {"schemaVersion": 1, "collidedPosterPaths": ["/safe.jpg", "https://private/x"]}):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    index = posters.VerifiedPosterIndex()
                    with self.assertRaisesRegex(ValueError, "Invalid persisted"):
                        posters.seed_collision_file(index, path)
                    self.assertFalse(index.collided("/safe.jpg"))

    def test_imported_subset_cannot_reuse_a_known_collided_poster_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            details = {**movie_details(123), "title": "Movie", "original_title": "Movie",
                       "release_date": "2024-01-02", "poster_path": "/shared.jpg"}
            client = tmdb.TmdbClient(root / "tmdb", token="offline", session=FakeSession(FakeResponse(details)))
            metadata = client.enrich({"title": "Movie (2024)", "tmdbId": "123"}, "movie")
            self.assertEqual(metadata["matchStatus"], "matched_id")
            metadata = dict(metadata, matchStatus="matched_poster")
            item = {"title": "Movie (2024)",
                    "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/shared.jpg"}}
            key, identifier, title, year = tmdb._enrichment_identity(item, "movie")
            proof = {"names": ["movie"], "originalTitle": "movie", "year": "2024", "tmdbId": "123",
                     "posterPolicy": posters.POSTER_POLICY, "posterPath": "/shared.jpg",
                     "posterPaths": ["/shared.jpg"], "mediaKind": "movie"}
            client._write_identity_cache(key, metadata, tmdb.DETAIL_TTL, proof)
            self.assertIsNotNone(client._identity_cache(key, item, "movie", identifier, title, year)[0])
            (root / "enrichment-collisions.json").write_text(json.dumps({
                "schemaVersion": 1, "collidedPosterPaths": ["/shared.jpg"]}))
            imported = tmdb.TmdbClient(root / "tmdb", max_requests=0)
            value, _fresh, rejected = imported._identity_cache(key, item, "movie", identifier, title, year)
            self.assertIsNone(value)
            self.assertTrue(rejected)
            self.assertTrue(imported.poster_index.collided("/shared.jpg"))
            self.assertEqual(imported.stats["requests"], 0)
            imported.close()
            client.close()


if __name__ == "__main__":
    unittest.main()
