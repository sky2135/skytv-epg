from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import requests

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_epg_streaming as runner
import channel_metadata_enrichment as enrichment


def channel(channel_id, name, country, categories=(), aliases=(), **extra):
    return {"id": channel_id, "name": name, "country": country, "categories": list(categories), "alt_names": list(aliases), **extra}


def feed(channel_id, languages, *, is_main=True, feed_id="SD"):
    return {"channel": channel_id, "id": feed_id, "languages": list(languages), "broadcast_area": [], "is_main": is_main}


CHANNELS = [
    channel("GolfPlus.fr", "Golf +", "FR", ["sports"]),
    channel("SkyWitness.uk", "Sky Witness", "UK", ["entertainment"]),
    channel("SonySAB.in", "Sony SAB", "IN", ["entertainment"], ["SAB TV"]),
    channel("News24.bd", "News 24", "BD", ["news"]),
    channel("News24.pk", "News 24", "PK", ["news"]),
    channel("Bilingual.ca", "Bilingual", "CA", ["general"]),
    channel("Regional.us", "Regional", "US", ["general"]),
    channel("Retired.fr", "Retired", "FR", ["general"], closed="2021-01-01"),
    channel("Adult.us", "Explicit Channel", "US", ["movies"], is_nsfw=True),
]
FEEDS = [
    feed("GolfPlus.fr", ["fra"]), feed("SkyWitness.uk", ["eng"]),
    feed("SonySAB.in", ["hin"]), feed("News24.bd", ["ben"]),
    feed("News24.pk", ["urd"]), feed("Bilingual.ca", ["eng", "fra"]),
    feed("Regional.us", ["eng"], is_main=False),
    feed("Regional.us", ["spa"], is_main=False, feed_id="Spanish"),
    feed("Adult.us", ["eng"]),
]
COUNTRIES = [{"code": code, "name": name, "languages": languages} for code, name, languages in [
    ("FR", "France", ["fra"]), ("UK", "United Kingdom", ["eng"]),
    ("US", "United States", ["eng"]), ("CA", "Canada", ["eng", "fra"]),
    ("IN", "India", ["hin"]), ("BD", "Bangladesh", ["ben"]),
    ("PK", "Pakistan", ["urd"]), ("AR", "Argentina", ["spa"]),
    ("AF", "Afghanistan", ["pus"]), ("MY", "Malaysia", ["msa"]),
]]


class MetadataEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.registry = enrichment.ChannelRegistry(CHANNELS, FEEDS, COUNTRIES)

    def enrich(self, **raw):
        before = runner.build_metadata(raw, 2)
        return enrichment.enrich_metadata(raw, before, self.registry)

    def test_exact_quality_prefix_and_plus_channel_identity(self):
        result, evidence = self.enrich(channel_name="FR - GOLF+ FHD")
        self.assertEqual(result.countries, ["FR"])
        self.assertEqual(result.region, "europe")
        self.assertEqual(result.languages, ["fr"])
        self.assertEqual(evidence["channelId"], "GolfPlus.fr")
        self.assertIsNone(self.registry.resolve_channel("Golf"))
        self.assertNotEqual(enrichment.normalize_name("BBC UHD 2"), enrichment.normalize_name("BBC 2"))

    def test_exact_alias_and_indian_language_bank(self):
        result, evidence = self.enrich(channel_name="IN-TM | Completely Unlisted Channel")
        self.assertEqual(result.countries, ["IN"])
        self.assertEqual(result.primary_language, "ta")
        self.assertEqual(evidence["facets"]["languages"], "provider_language_prefix")
        self.assertEqual(self.registry.resolve_channel("IN-NEWS | SAB TV FHD").channel_id, "SonySAB.in")

    def test_language_is_from_broadcast_evidence_never_country(self):
        result, _ = self.enrich(channel_name="US | Unlisted Channel")
        self.assertEqual(result.countries, ["US"])
        self.assertEqual(result.languages, ["und"])
        result, _ = self.enrich(channel_name="MY | Unknown Television")
        self.assertEqual(result.countries, [])
        self.assertEqual(result.languages, ["und"])

    def test_ambiguous_names_and_provider_region_codes_are_not_guessed(self):
        self.assertIsNone(self.registry.resolve_channel("News 24"))
        self.assertIsNone(self.registry.resolve_channel("FR | News 24"))
        self.assertEqual(self.registry.resolve_channel("BD | News 24 HD").channel_id, "News24.bd")
        for prefix in ("AR", "AF"):
            result, _ = self.enrich(channel_name=f"{prefix} - Unknown Channel")
            self.assertEqual(result.countries, [])
            self.assertEqual(result.languages, ["und"])

    def test_explicit_epg_id_is_authoritative_and_closed_channels_are_ignored(self):
        self.assertEqual(self.registry.resolve_channel("Unknown display", epg_id="SkyWitness.uk").channel_id, "SkyWitness.uk")
        self.assertIsNone(self.registry.resolve_channel("Retired"))

    def test_multilingual_main_feed_and_unknown_regional_feed(self):
        result, _ = self.enrich(channel_name="Bilingual")
        self.assertEqual(result.languages, ["en", "fr"])
        self.assertEqual(result.primary_language, "mul")
        result, _ = self.enrich(channel_name="Regional")
        self.assertEqual(result.languages, ["und"])

    def test_provider_market_does_not_change_broadcast_language(self):
        result, _ = self.enrich(channel_name="CA | Sony SAB", epg_id="SonySAB.in")
        self.assertEqual(result.countries, ["CA"])
        self.assertEqual(result.languages, ["hi"])

    def test_conflicting_unique_country_variant_is_rejected(self):
        self.assertIsNone(self.registry.resolve_channel("CA | Sony SAB"))

    def test_manual_locked_and_explicit_values_survive(self):
        for raw in [
            {"channel_name": "Sky Witness", "metadata_locked": "TRUE"},
            {"channel_name": "Sky Witness", "metadata_source": "manual"},
            {"channel_name": "Sky Witness", "country_codes": "CA", "primary_language": "fr", "language_codes": "fr", "genre": "movies"},
        ]:
            before = runner.build_metadata(raw, 2)
            result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
            self.assertEqual(result, before)
            self.assertEqual(evidence, {})

    def test_explicit_partial_values_preserve_authority_but_allow_missing_facets(self):
        result, _ = self.enrich(channel_name="Sky Witness", country_codes="GB", genre="movies", metadata_source="provider_category")
        self.assertEqual(result.countries, ["GB"])
        self.assertEqual(result.genre, "movies")
        self.assertEqual(result.languages, ["en"])

    def test_stronger_provider_market_corrects_unlocked_weak_country_inference(self):
        raw = {"channel_name": "BD | News 24 HD", "category_name": "INR | Bangla"}
        before = replace(runner.build_metadata(raw, 2), countries=["IN"], region="south_asia")
        result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
        self.assertEqual(result.countries, ["BD"])
        self.assertEqual(evidence["facets"]["countries"], "provider_prefix")

    def test_automatic_sheet_unknown_sentinels_do_not_block_enrichment(self):
        for source in ("provider_category", ""):
            result, evidence = self.enrich(channel_name="Sky Witness", region_code="unknown", genre="unknown", primary_language="und", language_codes="und", metadata_status="auto", metadata_source=source)
            self.assertEqual(result.countries, ["GB"])
            self.assertEqual(result.region, "europe")
            self.assertEqual(result.languages, ["en"])
            self.assertEqual(result.genre, "entertainment")
            self.assertEqual(evidence["channelId"], "SkyWitness.uk")

    def test_ambiguous_indian_language_country_tokens_are_not_countries(self):
        for token in ("TM", "MY", "TG", "KN", "AS", "ML", "PA", "GU", "MT", "TL"):
            countries, method = self.registry.country_evidence({"channel_name": f"{token}: Unlisted Channel", "category_name": "Punjabi"})
            self.assertEqual(countries, [])
            self.assertEqual(method, "")

    def test_synthetic_event_and_group_header_identity_is_not_invented(self):
        for raw in [
            {"channel_name": "##### FR - GOLF+ #####"},
            {"channel_name": "Golf +", "channel_role": "event", "metadata_source": "provider_category"},
            {"channel_name": "Golf +", "channel_role": "virtual", "metadata_source": "provider_category"},
        ]:
            before = runner.build_metadata(raw, 2)
            result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
            self.assertEqual(result, before)
            self.assertEqual(evidence, {})

    def test_registry_adult_category_preserves_parental_boundary(self):
        result, _ = self.enrich(channel_name="Explicit Channel")
        self.assertEqual(result.genre, "adult")
        self.assertEqual(result.audiences, ["adults"])
        self.assertEqual(result.content_rating, "adult")


class Response:
    status_code = 200

    def __init__(self, payload):
        self.content = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise requests.ConnectionError("Network failure")
        payload = {"channels.json": CHANNELS, "feeds.json": FEEDS, "countries.json": COUNTRIES, "logos.json": [{"channel": "GolfPlus.fr", "url": "https://example.com/logo.png", "in_use": True, "feed": None, "tags": []}]}[url.rsplit("/", 1)[-1]]
        return Response(payload)


class RegistryCacheTests(unittest.TestCase):
    def test_validated_bounded_fetch_and_integrity_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "registry"
            session = Session()
            report = enrichment.fetch_registry(target, session=session)
            self.assertEqual(report["cacheState"], "downloaded")
            self.assertEqual(len(session.calls), 4)
            self.assertTrue(all(call[1]["allow_redirects"] is False for call in session.calls))
            self.assertEqual(enrichment.fetch_registry(target, session=Session(fail=True))["cacheState"], "fresh")
            (target / "channels.json").write_text("[]")
            with self.assertRaises(enrichment.RegistryError):
                enrichment.ChannelRegistry.from_directory(target)

    def test_failed_refresh_uses_verified_recent_cache_and_leaves_it_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "registry"
            enrichment.fetch_registry(target, session=Session())
            original = (target / "channels.json").read_bytes()
            report = enrichment.fetch_registry(target, max_age_hours=0, session=Session(fail=True))
            self.assertEqual(report["cacheState"], "stale")
            self.assertEqual((target / "channels.json").read_bytes(), original)
            manifest = json.loads((target / "manifest.json").read_text())
            manifest["fetchedAtEpoch"] = int(time.time()) - 31 * 86400
            (target / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(enrichment.RegistryError):
                enrichment.fetch_registry(target, max_age_hours=0, session=Session(fail=True))

    def test_invalid_public_schema_is_rejected(self):
        with self.assertRaises(enrichment.RegistryError):
            enrichment._validate_catalog("feeds.json", [{"channel": "A.us", "id": "SD", "languages": ["en;private"], "is_main": True}])


if __name__ == "__main__":
    unittest.main()
