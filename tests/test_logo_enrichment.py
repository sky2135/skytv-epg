from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import logo_enrichment as logos


class LogoEnrichmentTests(unittest.TestCase):
    def test_known_public_resize_urls_are_recovered_without_allowing_tokens(self):
        expected = "https://dtil.tmsimg.com/assets/channel.png"
        self.assertEqual(logos.normalize_logo_url(
            "http://dtil.tmsimg.com/assets/channel.png?w=360&amp;h=270"), expected)
        self.assertEqual(logos.normalize_logo_url(
            "https://http://dtil.tmsimg.com/assets/channel.png?w=360&h=270"), expected)
        self.assertEqual(logos.normalize_logo_url("//assets.tv24.co/channels/news.webp"),
                         "https://assets.tv24.co/channels/news.webp")
        self.assertEqual(logos.normalize_logo_url(
            "https://www.a1.hr/rasporedhttps://ssc.a1.hr/ITV/pictures/logos/Mini TV.png"),
            "https://ssc.a1.hr/ITV/pictures/logos/Mini%20TV.png")
        for value in (
            "https://dtil.tmsimg.com/assets/channel.png?token=secret",
            "https://dtil.tmsimg.com/assets/channel.png?w=360&h=270&token=secret",
            "https://dtil.tmsimg.com/assets/channel.png?w=360&w=270",
            "https://unreviewed.test/channel.png?w=360&h=270",
            "https://user:pass@logos.test/channel.png",
            "https://logos.test/channel.png#secret", "http://127.0.0.1/logo.png",
            "https://logos.test:8080/logo.png", "https://logos.test/a b.png",
            "https://television.telerama.frhttps://focus.telerama.fr/0000/00/01/clear-192.png",
        ):
            with self.subTest(value=value):
                self.assertEqual(logos.normalize_logo_url(value), "")

    def test_known_generic_source_placeholders_are_not_channel_logos(self):
        for url in ("http://dtil.tmsimg.com/sources/generic/generic_sources_h3.png",
                    "https://images.test/placeholder.png", "https://images.test/no-logo.png",
                    "https://focus.telerama.fr/0000/00/01/clear-192.png"):
            self.assertEqual(logos.normalize_logo_url(url), "")
        self.assertEqual(logos.normalize_logo_url("https://images.test/real-logo.png"),
                         "https://images.test/real-logo.png")

    def test_brand_evidence_is_independent_from_image_verification(self):
        url = "https://logos.test/cnn.png"
        verified = {url: {"status": "valid_image", "checkedAt": 100}}
        source = logos.logo_provenance(url, source_icon=url, verification=verified)
        self.assertEqual(source["imageStatus"], "valid_image")
        self.assertEqual(source["kind"], "source_channel_logo")
        self.assertFalse(source["brandVerified"])
        brand = logos.logo_provenance(url, override_notes="BRAND_LOGO: cnn")
        self.assertTrue(brand["brandVerified"])
        self.assertEqual(brand["imageStatus"], "unchecked")
        generated = logos.logo_provenance("https://site.test/logos/generated/category-news-v3.png")
        self.assertEqual(generated["kind"], "generated_artwork")
        self.assertFalse(generated["brandVerified"])
        self.assertEqual(logos.logo_provenance("")["kind"], "missing")

    def test_cache_reuses_positive_checks_and_retries_expired_failures(self):
        good, stale, never = ("https://images.test/" + suffix for suffix in
                             ("good.png", "stale.png", "never.png"))
        now = 2_000_000
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache.json"
            cache.write_text(json.dumps({"schemaVersion": 1, "images": {
                good: {"status": "valid_image", "checkedAt": now - 86400},
                stale: {"status": "http_error", "checkedAt": now - 86401},
            }}))
            def check(url, **_kwargs):
                return {"status": "valid_image", "checkedAt": now}
            with mock.patch.object(logos, "check_logo_image", side_effect=check) as checker:
                results = logos.verify_logo_urls([good, stale, never, never], cache_path=cache,
                                                 now_epoch=now)
            self.assertEqual(checker.call_count, 2)
            self.assertEqual(results[good]["checkedAt"], now - 86400)
            self.assertEqual(results[stale]["checkedAt"], now)
            self.assertEqual(logos.load_logo_verification(cache), results)

    def test_check_budget_is_visible_as_unchecked(self):
        with mock.patch.object(logos, "check_logo_image") as check:
            results = logos.verify_logo_urls(["https://images.test/one.png"], max_checks=0)
        check.assert_not_called()
        self.assertEqual(results["https://images.test/one.png"]["status"], "unchecked")

    def test_verification_budget_respects_input_priority_after_deduplication(self):
        first, second = "https://images.test/z-selected.png", "https://images.test/a-source.png"
        with mock.patch.object(logos, "check_logo_image", return_value={"status": "valid_image", "checkedAt": 100}) as check:
            records = logos.verify_logo_urls([first, second, first], max_checks=1)
        self.assertEqual(check.call_args.args[0], first)
        self.assertEqual(records[first]["status"], "valid_image")
        self.assertEqual(records[second]["status"], "unchecked")

    def test_cli_inputs_prioritize_selected_overrides_and_filter_unused_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping, config, catalog = (root / name for name in ("mapping.csv", "config.csv", "source.json"))
            with mapping.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("server_id", "stream_id", "enabled", "action", "source", "epg_id", "channel_name", "logo_url"))
                writer.writeheader()
                writer.writerows([
                    {"server_id": "server_1", "stream_id": "1", "enabled": "true", "action": "APPROVED", "source": "epgshare01", "epg_id": "Selected.us", "channel_name": "Selected", "logo_url": "https://images.test/y-mapping.png"},
                    {"server_id": "server_2", "stream_id": "2", "enabled": "true", "action": "KEEP_PANEL", "source": "panel", "epg_id": "Panel.us", "channel_name": "Panel"},
                    {"server_id": "server_1", "stream_id": "3", "enabled": "false", "action": "APPROVED", "source": "epgshare01", "epg_id": "Disabled.us"},
                ])
            with config.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("enabled", "server_id", "stream_id", "icon_url", "local_file"))
                writer.writeheader()
                writer.writerows([
                    {"enabled": "true", "server_id": "server_1", "stream_id": "1", "icon_url": "https://images.test/z-override.png"},
                    {"enabled": "true", "server_id": "server_2", "stream_id": "2", "local_file": "generated/new-art.png"},
                    {"enabled": "true", "server_id": "server_1", "stream_id": "3", "icon_url": "https://images.test/disabled.png"},
                ])
            catalog.write_text(json.dumps({"channel_records": [
                {"epg_id": "Unused.us", "icon_url": "https://images.test/unused.png"},
                {"epg_id": "Selected.us", "icon_url": "https://images.test/a-source.png"},
                {"epg_id": "Panel.us", "icon_url": "https://images.test/wrong-panel.png"},
            ]}))
            urls = logos.collect_logo_verification_urls(catalog, icon_config=config, mapping_csv=mapping)
        self.assertEqual(urls, ["https://images.test/z-override.png",
                                "https://images.test/y-mapping.png", "https://images.test/a-source.png"])

    def test_temporary_dns_and_http_failures_do_not_discard_public_images(self):
        url = "https://images.test/channel.png"
        with mock.patch.object(logos, "_public_network_target", return_value=None):
            self.assertEqual(logos.check_logo_image(url)["status"], "network_error")
        with mock.patch.object(logos, "_public_network_target", return_value=True):
            for code in (404, 429, 503):
                opener = mock.Mock()
                opener.open.side_effect = urllib.error.HTTPError(url, code, "test", {}, None)
                with mock.patch.object(logos.urllib.request, "build_opener", return_value=opener):
                    result = logos.check_logo_image(url)
                self.assertEqual(result["status"], "http_error" if code == 404 else "network_error")

    def test_response_html_and_unsafe_svg_are_rejected_as_images(self):
        self.assertEqual(logos._image_kind(b"<html>Access denied</html>", "image/png"), "")
        self.assertEqual(logos._image_kind(b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                                          "image/svg+xml"), "svg")
        self.assertEqual(logos._image_kind(b'<!DOCTYPE svg SYSTEM "https://evil.test/x"><svg/>',
                                          "image/svg+xml"), "")

    def test_rejected_selected_identity_cannot_return_through_canonical_logo(self):
        from channel_metadata_enrichment import ChannelRegistry
        channels = [
            {"id": "StarMovies.pt", "name": "Star Movies", "country": "PT"},
            {"id": "CanalHollywood.pt", "name": "Canal Hollywood", "country": "PT"},
            {"id": "KTV.al", "name": "KTV", "country": "AL"},
            {"id": "KTV.in", "name": "KTV", "country": "IN"},
            {"id": "StarPlus.in", "name": "Star Plus", "country": "IN"},
        ]
        registry = ChannelRegistry(channels, [], [
            {"code": "PT", "name": "Portugal"}, {"code": "AL", "name": "Albania"},
            {"code": "IN", "name": "India"},
        ])
        row = {"channel_name": "PT - Star Movies", "canonical_name": "Canal Hollywood",
               "epg_id": "CanalHollywood.pt"}
        wrong = {"channel": "CanalHollywood.pt", "feed": None, "in_use": True,
                 "url": "https://logos.test/canal-hollywood.png"}
        index = logos.RegistryLogoIndex(registry, [wrong])
        self.assertTrue(index.selected_id_identity_conflict(row))
        self.assertIsNone(index.lookup(row))
        index = logos.RegistryLogoIndex(registry, [wrong,
            {"channel": "StarMovies.pt", "feed": None, "in_use": True,
             "url": "https://logos.test/star-movies.png"},
            {"channel": "KTV.al", "feed": None, "in_use": True,
             "url": "https://logos.test/ktv-albania.png"},
            {"channel": "KTV.in", "feed": None, "in_use": True,
             "url": "https://logos.test/ktv-india.png"},
            {"channel": "StarPlus.in", "feed": None, "in_use": True,
             "url": "https://logos.test/star-plus-india.png"},
        ])
        self.assertEqual(index.lookup(row), ("StarMovies.pt", "https://logos.test/star-movies.png"))
        self.assertEqual(index.lookup({"channel_name": "AL - KTV", "canonical_name": "KTV",
                                       "epg_id": "KTV.in"}),
                         ("KTV.al", "https://logos.test/ktv-albania.png"))
        star_plus = {"channel_name": "AL: STAR PLUS HD", "canonical_name": "Star Plus",
                     "epg_id": "STAR.PLUS.HD.in"}
        self.assertTrue(index.selected_id_identity_conflict(star_plus))
        self.assertIsNone(index.lookup(star_plus))
        self.assertFalse(index.selected_id_identity_conflict({
            "channel_name": "Uncatalogued label", "canonical_name": "Canal Hollywood",
            "epg_id": "CanalHollywood.pt",
        }))

    def test_registry_logos_require_one_exact_current_channel_and_safe_feed_scope(self):
        from channel_metadata_enrichment import ChannelRegistry
        channels = [
            {"id": "CityTV.ca", "name": "City TV", "country": "CA"},
            {"id": "CityTV.us", "name": "City TV", "country": "US"},
            {"id": "Regional.us", "name": "Regional", "country": "US"},
            {"id": "Closed.us", "name": "Closed", "country": "US", "closed": "2020-01-01"},
        ]
        registry = ChannelRegistry(channels, [], [{"code": "CA", "name": "Canada"},
                                                 {"code": "US", "name": "United States"}])
        records = [
            {"channel": "CityTV.ca", "feed": None, "in_use": True, "url": "https://logos.test/ca.png"},
            {"channel": "CityTV.us", "feed": None, "in_use": True, "url": "https://logos.test/us.png"},
            {"channel": "Regional.us", "feed": "East", "in_use": True, "url": "https://logos.test/east.png"},
            {"channel": "Regional.us", "feed": "West", "in_use": True, "url": "https://logos.test/west.png"},
            {"channel": "Closed.us", "feed": None, "in_use": True, "url": "https://logos.test/closed.png"},
            {"channel": "CityTV.ca", "feed": None, "in_use": False, "url": "https://logos.test/retired.png"},
        ]
        index = logos.RegistryLogoIndex(registry, records)
        self.assertIsNone(index.lookup({"channel_name": "City TV"}))
        self.assertEqual(index.lookup({"channel_name": "CA: City TV HD"}),
                         ("CityTV.ca", "https://logos.test/ca.png"))
        self.assertIsNone(index.lookup({"channel_name": "Regional"}))
        self.assertIsNone(index.lookup({"channel_name": "Closed"}))
        self.assertIsNone(index.lookup({"channel_name": "### City TV ###"}))
        self.assertIsNone(index.lookup({"channel_name": "US: City TV", "canonical_name": "Regional"}))
        evidence = logos.logo_provenance("https://logos.test/ca.png", override_notes="REGISTRY_LOGO: CityTV.ca")
        self.assertEqual(evidence["source"], "iptv-org")
        self.assertFalse(evidence["brandVerified"])


if __name__ == "__main__":
    unittest.main()
