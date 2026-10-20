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

    def test_exact_alias_and_uncorroborated_indian_language_bank(self):
        result, evidence = self.enrich(channel_name="IN-TM | Completely Unlisted Channel")
        self.assertEqual(result.countries, ["IN"])
        self.assertEqual(result.primary_language, "und")
        self.assertEqual(result.status, "review")
        self.assertEqual(evidence["languageReview"], "provider_language_bank_without_feed_corroboration")
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

    def test_unverified_provider_market_does_not_inherit_domestic_language(self):
        result, _ = self.enrich(channel_name="CA | Sony SAB", epg_id="SonySAB.in")
        self.assertEqual(result.countries, ["CA"])
        self.assertEqual(result.languages, ["und"])

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

    def test_automatic_prior_genre_can_be_corrected_by_reviewed_source(self):
        result, _ = self.enrich(channel_name="Sky Witness", country_codes="GB", genre="movies", metadata_source="provider_category")
        self.assertEqual(result.countries, ["GB"])
        self.assertEqual(result.genre, "entertainment")
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

    def test_registry_nsfw_strengthens_existing_automatic_nonadult_classification(self):
        for genre in ("entertainment", "sports", "religion"):
            with self.subTest(genre=genre):
                raw = {"channel_name": "Explicit Channel", "genre": genre,
                       "content_rating": "general", "audience_codes": "general",
                       "sport_codes": "multi_sport" if genre == "sports" else "",
                       "religion_codes": "general_spirituality" if genre == "religion" else "",
                       "metadata_source": "provider_category", "metadata_status": "auto"}
                before = runner.build_metadata(raw, 2)
                self.assertEqual(before.genre, genre)
                result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
                self.assertEqual(result.genre, "adult")
                self.assertEqual(result.content_rating, "adult")
                self.assertEqual(result.audiences, ["adults"])
                self.assertEqual(result.sports, [])
                self.assertEqual(result.religions, [])
                self.assertEqual(evidence["facets"]["genre"], "registry_nsfw_identity")

    def test_registry_nsfw_preserves_manual_and_locked_classification(self):
        for authority in ({"metadata_source": "manual"}, {"metadata_locked": "TRUE"}):
            with self.subTest(authority=authority):
                raw = {"channel_name": "Explicit Channel", "genre": "entertainment",
                       "content_rating": "general", "audience_codes": "general", **authority}
                before = runner.build_metadata(raw, 2)
                result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
                self.assertEqual(result, before)
                self.assertEqual(evidence, {})


class AuditedLiveRegressionTests(unittest.TestCase):
    """Actual Oct-20 audit failures, including unrelated same-name channels."""

    def setUp(self):
        additions = [
            channel("Zing.in", "Zing", "IN", ["music"]),
            channel("ZeeBiskope.in", "Zee Biskope", "IN", ["movies"]),
            channel("News18Gujarati.in", "News18 Gujarati", "IN", ["news"]),
            channel("StarBharat.in", "Star Bharat", "IN", ["entertainment"]),
            channel("AndTV.in", "&TV", "IN", ["entertainment"], ["And TV"]),
            channel("AlJazeeraDocumentary.qa", "Al Jazeera Documentary", "QA", ["documentary"]),
            channel("AlJazeera.qa", "Al Jazeera", "QA", ["news"]),
            channel("KTV.in", "KTV", "IN", ["movies"]),
            channel("Kohavision.xk", "Kohavision", "XK", ["general"], ["KTV"]),
            channel("SABTV.pk", "SAB TV", "PK", ["entertainment"]),
            channel("StarPlus.in", "StarPlus", "IN", ["entertainment"]),
            channel("StarPlus.al", "Star Plus", "AL", ["general"]),
            channel("StarMovies.in", "Star Movies", "IN", ["movies"]),
            channel("StarMovies.pt", "Star Movies", "PT", ["movies"]),
            channel("CanalHollywood.pt", "Canal Hollywood", "PT", ["movies"]),
            channel("SonyMax.in", "Sony Max", "IN", ["movies"]),
            channel("SonySportsTen5.in", "Sony Sports Ten 5", "IN", ["sports"], ["Sony MAX"]),
            channel("SonyPIX.in", "Sony PIX", "IN", ["movies"]),
            channel("Colors.in", "Colors", "IN", ["entertainment"]),
            channel("GeminiLife.in", "Gemini Life", "IN", ["entertainment"]),
            channel("PTCPunjabi.in", "PTC Punjabi", "IN", ["entertainment"]),
            channel("CNBCBajar.in", "CNBC Bajar", "IN", ["business"]),
            channel("News18MadhyaPradeshChhattisgarh.in", "News18 Madhya Pradesh/Chhattisgarh", "IN", ["news"]),
            channel("SunGemini.in", "Sun Gemini", "IN", ["general"]),
            channel("Alternative.us", "Alternative", "US", ["general"]),
        ]
        languages = {"Zing.in": "hin", "ZeeBiskope.in": "bho", "News18Gujarati.in": "guj", "StarBharat.in": "hin", "AndTV.in": "hin", "AlJazeeraDocumentary.qa": "ara", "KTV.in": "tam", "Kohavision.xk": "sqi", "SABTV.pk": "urd", "StarPlus.in": "hin", "StarPlus.al": "sqi", "StarMovies.in": "eng", "StarMovies.pt": "por", "CanalHollywood.pt": "spa", "SonyMax.in": "hin", "SonySportsTen5.in": "eng", "SonyPIX.in": "eng", "Colors.in": "hin", "GeminiLife.in": "tel", "PTCPunjabi.in": "pan"}
        countries = COUNTRIES + [{"code": code, "name": name, "languages": []} for code, name in [("AL", "Albania"), ("XK", "Kosovo"), ("PT", "Portugal"), ("HU", "Hungary"), ("QA", "Qatar")]]
        feeds = FEEDS + [feed(key, [language]) for key, language in languages.items()]
        feeds += [feed("Alternative.us", ["eng"]), feed("Alternative.us", ["spa"], feed_id="Spanish")]
        self.registry = enrichment.ChannelRegistry(CHANNELS + additions, feeds, countries)

    def enrich(self, **raw):
        raw.setdefault("metadata_source", "provider_category")
        raw.setdefault("metadata_status", "auto")
        before = runner.build_metadata(raw, 2)
        result, evidence = enrichment.enrich_metadata(raw, before, self.registry)
        return before, result, evidence

    def test_all_seven_confirmed_genre_correction_identities(self):
        for name, category, expected in [
            ("UKFHD : Sky Witness", "UK| MOVIES", "entertainment"),
            ("IN | Zing", "INDIA MOVIES", "music"),
            ("IN | Zee Biskope", "INDIA MUSIC", "movies"),
            ("IN-GUJ | News18 Gujarati", "INDIA ENTERTAINMENT", "news"),
            ("IN - STAR BHARAT UHD", "INDIA NEWS", "entertainment"),
            ("IN - AND TV UHD", "INDIA MOVIES", "entertainment"),
            ("AR - AL JAZEERA DOCUMENTARY", "ARABIC NEWS & FACTUAL", "documentary"),
        ]:
            with self.subTest(name=name):
                before, result, evidence = self.enrich(channel_name=name, category_name=category)
                self.assertNotEqual(before.genre, expected)
                self.assertEqual(result.genre, expected)
                self.assertEqual(evidence["rulesVersion"], enrichment.RULES_VERSION)
                self.assertEqual(evidence["facets"]["genre"], "reviewed_broadcaster_channel_fact")
                self.assertTrue(evidence["reviewedFact"]["sourceUrl"].startswith("https://"))

    def test_durable_correction_of_stored_automatic_genre_cells(self):
        _, result, _ = self.enrich(channel_name="UK HD : Sky Witness", genre="movies", country_codes="GB")
        self.assertEqual(result.genre, "entertainment")
        for authority in ({"metadata_source": "manual"}, {"metadata_locked": "TRUE"}, {"metadata_status": "approved"}):
            before, result, evidence = self.enrich(channel_name="Sky Witness", genre="movies", **authority)
            self.assertEqual(before, result)
            self.assertEqual(evidence, {})

    def test_curated_gap_is_filled_without_promoting_uncertain_genre_candidate(self):
        _, result, evidence = self.enrich(channel_name="PJB - PTC PUNJABI", category_name="PUNJABI")
        self.assertEqual(result.genre, "entertainment")
        self.assertEqual(evidence["reviewedFact"]["id"], "ptcpunjabi")
        _, result, _ = self.enrich(channel_name="IN-TG | Gemini Life", genre="lifestyle", category_name="TELUGU")
        self.assertEqual(result.genre, "lifestyle")

    def test_reviewed_aliases_join_only_their_exact_available_registry_identity(self):
        for name, channel_id, language in [
            ("IN-NEWS | CNBC Bajaar", "CNBCBajar.in", "gu"),
            ("IN: News18 MP Chhattisgarh SD", "News18MadhyaPradeshChhattisgarh.in", "hi"),
            ("IN-TG | Gemini TV FHD", "SunGemini.in", "te"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.registry.resolve_channel(name).channel_id, channel_id)
                _, result, evidence = self.enrich(channel_name=name, primary_language="und", language_codes="und", genre="unknown")
                self.assertEqual(result.languages, [language])
                self.assertIn("reviewedFact", evidence)

    def test_cross_market_collisions_cannot_inherit_indian_facts(self):
        for name, bad_id in [
            ("ALB: KTV", "KTV.in"), ("AL: STAR PLUS HD", "StarPlus.in"),
            ("PK: SAB TV-HD", "SonySAB.in"), ("HU - SONY MAX", "SonyMax.in"),
            ("PT - STAR MOVIES UHD", "CanalHollywood.pt"),
        ]:
            with self.subTest(name=name):
                self.assertIsNone(self.registry.resolve_channel(name, epg_id=bad_id))
                _, result, evidence = self.enrich(channel_name=name, epg_id=bad_id, language_codes="hi", primary_language="hi")
                self.assertEqual(result.languages, ["und"])
                self.assertEqual(result.status, "review")
                self.assertNotIn("reviewedFact", evidence)
                self.assertEqual(evidence["identityReview"], "selected_id_name_or_market_disagreement")

    def test_explicit_provider_market_corrects_stale_automatic_country(self):
        _, result, evidence = self.enrich(channel_name="AL: STAR PLUS HD", epg_id="StarPlus.in", country_codes="IN", region_code="south_asia")
        self.assertEqual(result.countries, ["AL"])
        self.assertEqual(result.region, "europe")
        self.assertEqual(evidence["facets"]["countries"], "provider_prefix")

    def test_pakistan_sab_is_distinct_and_primary_name_beats_historical_alias(self):
        self.assertEqual(self.registry.resolve_channel("PK: SAB TV-HD").channel_id, "SABTV.pk")
        self.assertEqual(self.registry.resolve_channel("IN | Sony MAX FHD").channel_id, "SonyMax.in")
        _, result, evidence = self.enrich(channel_name="PK: SAB TV-HD", category_name="PAKISTAN | TV")
        self.assertEqual(result.languages, ["ur"])
        self.assertNotIn("reviewedFact", evidence)

    def test_dubbed_or_regional_bank_disagreements_remain_unknown(self):
        for name, category, stored_language in [
            ("IN-TM | Sony Pix FHD", "TAMIL", "ta"),
            ("IN-MAR | Colors FHD", "MARATHI", "mr"),
            ("IN-GUJ | COLORS FHD", "GUJARATI", "gu"),
        ]:
            with self.subTest(name=name):
                _, result, evidence = self.enrich(channel_name=name, category_name=category, primary_language=stored_language, language_codes=stored_language)
                self.assertEqual(result.languages, ["und"])
                self.assertEqual(result.status, "review")
                self.assertEqual(evidence["languageReview"], "provider_audio_or_category_disagrees_with_named_feed")

    def test_category_language_cannot_supply_or_expand_channel_audio(self):
        _, result, evidence = self.enrich(channel_name="Unlisted Channel", category_name="TAMIL", primary_language="ta", language_codes="ta")
        self.assertEqual(result.languages, ["und"])
        self.assertIn("languageReview", evidence)
        _, result, _ = self.enrich(channel_name="AR - AL JAZEERA ENGLISH", category_name="ARABIC NEWS", primary_language="mul", language_codes="en|ar")
        self.assertEqual(result.languages, ["en"])
        self.assertEqual(result.primary_language, "en")

    def test_different_main_feeds_do_not_mean_a_multilingual_provider_stream(self):
        _, result, _ = self.enrich(channel_name="Alternative")
        self.assertEqual(result.languages, ["und"])

    def test_domestic_fact_does_not_fill_foreign_unverified_feed_language(self):
        _, result, _ = self.enrich(channel_name="FR - STAR MOVIES", category_name="MOVIES")
        self.assertEqual(result.languages, ["und"])
        self.assertEqual(result.countries, ["FR"])

    def test_positive_conflict_api_distinguishes_unknown_name_from_wrong_identity(self):
        self.assertFalse(self.registry.selected_id_identity_conflict("Unlisted Channel", epg_id="StarMovies.in"))
        self.assertTrue(self.registry.selected_id_identity_conflict("PT - STAR MOVIES UHD", epg_id="CanalHollywood.pt"))
        self.assertTrue(self.registry.selected_id_identity_conflict("Al Jazeera English", epg_id="AlJazeera.qa"))

    def test_source_id_presentation_alias_detects_actual_star_plus_market_conflict(self):
        for guide_id in ("StarPlus.in", "STAR.PLUS.HD.in"):
            with self.subTest(guide_id=guide_id):
                self.assertTrue(self.registry.selected_id_identity_conflict("AL: STAR PLUS HD", epg_id=guide_id))
                _, result, evidence = self.enrich(channel_name="AL: STAR PLUS HD", epg_id=guide_id)
                self.assertEqual(result.languages, ["und"])
                self.assertIn("identityReview", evidence)
        # The safety lookup must not broaden the general schedule resolver.
        self.assertIsNone(self.registry.resolve_channel("Unlisted Channel", epg_id="STAR.PLUS.HD.in"))

    def test_unrecognized_source_id_does_not_prove_identity_from_country_alone(self):
        for guide_id in ("UNLISTED.HD.in", "STAR.PLUS.HD.notacountry", "STAR.PLUS.HD", "SONY.MAX.HD.in"):
            with self.subTest(guide_id=guide_id):
                # Sony MAX is intentionally ambiguous with a same-country
                # historical sports alias unless its exact registry ID is used.
                self.assertFalse(self.registry.selected_id_identity_conflict("AL: STAR PLUS HD", epg_id=guide_id))

    def test_legitimate_foreign_broadcast_keeps_exact_and_source_alias_ids(self):
        uk_feed = feed("SonyMax.in", ["hin"])
        uk_feed["broadcast_area"] = ["c/GB"]
        registry = enrichment.ChannelRegistry([channel("SonyMax.in", "Sony Max", "IN", ["movies"])], [uk_feed], COUNTRIES)
        for guide_id in ("SonyMax.in", "SONY.MAX.HD.in"):
            with self.subTest(guide_id=guide_id):
                self.assertFalse(registry.selected_id_identity_conflict("UK: Sony Max FHD", epg_id=guide_id))

    def test_repeated_review_preserves_identity_rejection_evidence(self):
        _, result, evidence = self.enrich(
            channel_name="PT - STAR MOVIES UHD", epg_id="CanalHollywood.pt",
            country_codes="PT", region_code="europe", genre="movies",
            primary_language="und", language_codes="und", metadata_status="review",
            metadata_confidence="0.5", metadata_source="registry",
        )
        self.assertEqual(result.status, "review")
        self.assertEqual(result.languages, ["und"])
        self.assertEqual(evidence["identityReview"], "selected_id_name_or_market_disagreement")

    def test_known_genre_replacements_are_limited_to_confirmed_audit_facts(self):
        facts = enrichment.load_curated_facts()
        self.assertEqual({fact["id"] for fact in facts if fact.get("replaceAutomaticGenre")},
                         {"skywitness", "zing", "news18gujarati", "zeebiskope", "aljazeeradocumentary", "starbharat", "andtv"})
        self.assertTrue(all(fact["sourceUrl"].startswith("https://") and fact["checkedAt"] == "2026-10-20" for fact in facts))
        self.assertTrue(all("logo" not in key.casefold() and "brandverified" not in key.casefold() for fact in facts for key in fact))


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
