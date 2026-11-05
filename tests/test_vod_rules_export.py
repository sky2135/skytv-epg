from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_enrichment_artifacts as artifacts
import vod_tmdb as tmdb
import test_vod_enrichment_artifacts as artifact_fixtures
from test_vod_matching_rules import provider_item, translated_details
from test_vod_tmdb import FakeResponse, FakeSession


class MatchingRuleExportTests(unittest.TestCase):
    def setUp(self):
        # Reuse the sealed-artifact test fixture, including its offline GitHub
        # archive/download mock. No live service or production path is used.
        self.fixture = artifact_fixtures.EnrichmentArtifactTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.root = self.fixture.root

    def client(self, directory=None, *responses):
        return tmdb.TmdbClient(directory or self.state / "cache/tmdb", token="fixture",
                               session=FakeSession(*responses), now=1_800_000_000)

    def envelope(self, client, source):
        return artifacts.read_json(client.identity_cache_path(source, "movie"))

    def roundtrip(self, name="candidate"):
        candidate = self.root / name
        counts = artifacts.export_cache(self.state, candidate)
        github, _ = self.fixture.github(self.fixture.zip_tree(candidate), kind="cache")
        destination = self.root / (name + "-import")
        imported = artifacts.import_cache(destination, github=github)
        self.assertEqual(imported["imported"], counts["positive"])
        return self.client(destination / "tmdb"), counts

    def test_prefix_translation_proof_survives_export_import_and_offline_reuse(self):
        source = provider_item("EX - Film exemple (2025)")
        client = self.client(None, FakeResponse(translated_details()))
        result = client.enrich(source, "movie")
        envelope = self.envelope(client, source)
        self.assertTrue(artifacts._positive(envelope))
        reader, counts = self.roundtrip()
        self.assertEqual(counts["positive"], 1)
        self.assertEqual(self.envelope(reader, source), envelope)
        self.assertEqual(reader.enrich(source, "movie"), result)
        self.assertEqual(reader.session.calls, [])

    def test_missing_source_year_poster_proof_survives_roundtrip(self):
        source = provider_item("Example Film", tmdbId=None)
        client = self.client(None, FakeResponse(translated_details()))
        client.poster_index.add("movie", "101", ["/abc123.jpg"], {"names": ["example film"], "year": "2025"})
        result = client.enrich(source, "movie")
        self.assertEqual(result["matchStatus"], "matched_poster")
        self.assertTrue(artifacts._positive(self.envelope(client, source)))
        reader, _ = self.roundtrip()
        self.assertEqual(reader.enrich(source, "movie"), result)
        self.assertEqual(reader.session.calls, [])

    def test_two_verified_variants_coexist_and_replay_after_export_import(self):
        first = provider_item("EX - काला (2025)")
        second = provider_item("EX - काली (2025)")
        details = translated_details(title="काला", translated="काली")
        client = self.client(None, FakeResponse(details), FakeResponse(details))
        self.assertEqual(tmdb._enrichment_identity(first, "movie")[0],
                         tmdb._enrichment_identity(second, "movie")[0])
        expected = [client.enrich(source, "movie") for source in (first, second)]
        self.assertTrue(all(result["matchStatus"] == "matched_poster" for result in expected))
        self.assertNotEqual(client.identity_cache_path(first, "movie"), client.identity_cache_path(second, "movie"))
        reader, counts = self.roundtrip()
        self.assertEqual(counts["positive"], 2)
        self.assertEqual([reader.enrich(source, "movie") for source in (first, second)], expected)
        self.assertEqual(reader.session.calls, [])

    def test_export_rejects_malformed_typed_bindings_without_partial_copy(self):
        source = provider_item("EX - Film exemple (2025)")
        client = self.client(None, FakeResponse(translated_details()))
        client.enrich(source, "movie")
        envelope = self.envelope(client, source)
        mutations = (
            lambda proof: proof["providerTitle"].update(policyVersion=True),
            lambda proof: proof["providerTitle"].update(matchTitleKey="absent from official names"),
            lambda proof: proof["providerTitle"].update(sourceYear="2025-01-01"),
            lambda proof: proof["providerTitle"].update(sourceTitleKey="https://private.invalid"),
            lambda proof: proof["providerTitle"].update(rawResponse={"secret": "unsafe"}),
            lambda proof: proof.update(translationDisplayKey="other"),
            lambda proof: proof.update(translationDisplayKey="Film exemple"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(envelope)
                mutate(changed["data"]["identity"])
                self.assertFalse(artifacts._positive(changed))
                path = self.state / "cache/tmdb/tmdb-v1" / (str(index + 1) * 64 + ".json")
                artifacts.atomic_json(path, changed)
        counts = artifacts.export_cache(self.state, self.root / "candidate")
        self.assertEqual(counts["positive"], 1)
        self.assertEqual(counts["skipped"], len(mutations))

    def test_imported_strict_source_binding_blocks_indic_cache_key_collision(self):
        source = provider_item("EX - काला (2025)")
        changed = provider_item("EX - काली (2025)")
        details = translated_details(title="काला", translated="Unrelated")
        client = self.client(None, FakeResponse(details))
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_poster")
        reader, _ = self.roundtrip()
        source_key = tmdb._enrichment_identity(source, "movie")[0]
        key, identifier, title, year = tmdb._enrichment_identity(changed, "movie")
        self.assertEqual(key, source_key)
        self.assertIsNone(reader._main_identity_cache(key, changed, "movie", identifier, title, year)[0])
        self.assertEqual(reader.session.calls, [])

    def test_legacy_verified_cache_remains_exportable_after_strategy_upgrade(self):
        source = {"title": "Example Film (2025)", "tmdbId": "101"}
        client = self.client(None, FakeResponse(translated_details()))
        result = client.enrich(source, "movie")
        key = tmdb._enrichment_identity(source, "movie")[0]
        envelope = self.envelope(client, source)
        envelope["data"]["matchStrategy"] = 5
        self.assertNotIn("providerTitle", envelope["data"]["identity"])
        self.assertNotIn("translationDisplayKey", envelope["data"]["identity"])
        client.identity_cache_path(source, "movie").unlink()
        artifacts.atomic_json(client._cache_path(key), envelope)
        self.assertTrue(artifacts._positive(envelope))
        reader, _ = self.roundtrip()
        self.assertEqual(reader.enrich(source, "movie"), result)
        self.assertEqual(reader.session.calls, [])


if __name__ == "__main__":
    unittest.main()
