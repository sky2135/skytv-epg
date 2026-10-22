from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import diagnose_vod_recent_samples as samples

NOW = 1_800_000_000
CONFIG = samples.vod.ServerConfig("server_1", "https://provider.invalid", "private-user", "private-password")


def row(identifier, added, category="1", **extra):
    return {"stream_id": identifier, "name": f"Real provider title {identifier}",
            "added": added, "category_id": category, **extra}


class RecentProviderSamplesTests(unittest.TestCase):
    def test_latest_five_use_only_actual_added_with_stable_numeric_id_ties(self):
        categories = [{"category_id": "1", "category_name": "English"}]
        rows = [row(i, str(NOW - i)) for i in range(1, 8)]
        rows.extend([row(20, NOW + 1_000_000), row(21, "2026-10-21"), row(22, True),
                     row(23, 0), row(24, None), row(25, float(NOW - 1))])
        rows.append(copy.deepcopy(rows[0]))
        selected, counts = samples.select_recent(categories, rows, now=NOW)
        self.assertEqual([entry[1] for entry in selected["en"]], ["1", "2", "3", "4", "5"])
        self.assertIsInstance(selected["en"][0][2]["added"], str)
        self.assertEqual(counts["duplicateRows"], 1)
        self.assertEqual(counts["invalidAdded"], 6)
        tied, _ = samples.select_recent(categories, [row(10, NOW - 1), row(2, NOW - 1)], now=NOW)
        self.assertEqual([entry[1] for entry in tied["en"]], ["2", "10"])
        milliseconds, _ = samples.select_recent(categories, [row(30, str((NOW - 1) * 1000)), row(31, str(NOW - 2))], now=NOW)
        self.assertEqual([entry[1] for entry in milliseconds["en"]], ["30", "31"])

    def test_subtitle_ambiguous_and_india_labels_never_supply_selected_languages(self):
        names = ["[EN] SUB", "English MULTISUB", "INDIA", "[PA]", "[PAN]", "PUNJABI", "[HI] Movies", "[EN] Movies"]
        categories = [{"category_id": str(i), "category_name": name} for i, name in enumerate(names, 1)]
        selected, counts = samples.select_recent(categories, [row(i, NOW - i, str(i)) for i in range(1, 9)], now=NOW)
        self.assertEqual([entry[1] for entry in selected["en"]], ["8"])
        self.assertEqual([entry[1] for entry in selected["hi"]], ["7"])
        self.assertEqual([entry[1] for entry in selected["pa"]], ["6"])
        self.assertEqual((counts["subtitleLabelExcluded"], counts["ambiguousLabelExcluded"]), (2, 2))
        with self.assertRaises(ValueError):
            samples.select_recent(categories, [row(1, NOW - 1), row(1, NOW - 2)], now=NOW)

    def test_live_title_language_conflicts_override_categories_without_changing_raw_records(self):
        categories = [{"category_id": "1", "category_name": "English + Hindi"},
                      {"category_id": "2", "category_name": "English"}]
        actual = [row(1, NOW - 1, "1", name="Cerita Lila (2026) (Hindi)"),
                  row(2, NOW - 2, "2", name="The Last Mistress (2007) [FRN]"),
                  row(3, NOW - 3, "2", name="Vanishing Point (2026) [CHI]")]
        original = copy.deepcopy(actual)
        selected, counts = samples.select_recent(categories, actual, now=NOW)
        self.assertEqual(selected["en"], [])
        self.assertEqual([entry[1] for entry in selected["hi"]], ["1"])
        self.assertEqual(selected["pa"], [])
        self.assertEqual(actual, original)
        self.assertEqual(counts["titleCategoryConflicts"], 3)
        self.assertEqual(counts["titleLanguageExcluded"], 2)
        evidence = samples._selection_evidence(actual[0], categories[0])
        self.assertEqual(evidence["categoryLanguageCodes"], ["en", "hi"])
        self.assertEqual(evidence["titleLanguageCodes"], ["hi"])
        self.assertTrue(evidence["selectionConflict"])
        self.assertEqual(evidence["selectionLanguageEvidence"], "explicit_title_markers")

    def test_title_words_years_quality_and_unrelated_parentheses_are_not_language_markers(self):
        for title in ("English Vinglish (2012)", "Tutt Paini English Ne (2025)",
                      "The English Patient (1996)", "A Movie (2026) [HDTS]", "A Movie (The English Patient)"):
            with self.subTest(title=title):
                self.assertEqual(samples._title_language_hints(title), (set(), set(), False))
        for title, expected in (("A Movie (Punjabi) (CAM)", "pa"), ("A Movie [HIN] [HDTS]", "hi"),
                                ("PB - A Movie (2026)", "pa"), ("[ENG] A Movie (2026)", "en"),
                                ("HIN: A Movie (2026)", "hi"), ("A Movie [TAM]", "ta"),
                                ("A Movie [TEL]", "te"), ("A Movie [MAL]", "ml"), ("A Movie [KAN]", "kn")):
            with self.subTest(title=title):
                self.assertEqual(samples._title_language_hints(title)[0], {expected})
        self.assertEqual(samples._title_language_hints("x" * 1001 + " (Hindi)")[0], set())

    def test_subtitle_markers_never_supply_audio_and_dubbed_markers_do(self):
        categories = [{"category_id": "1", "category_name": "English"}]
        rows = [row(1, NOW - 1, name="A Movie (English Sub)"),
                row(2, NOW - 2, name="A Movie (Hindi Dubbed)"),
                row(3, NOW - 3, name="A Movie (Hindi) [English Subtitles]"),
                row(4, NOW - 4, name="A Movie (Hindi/English Sub)")]
        selected, counts = samples.select_recent(categories, rows, now=NOW)
        self.assertEqual(selected["en"], [])
        self.assertEqual([entry[1] for entry in selected["hi"]], ["2", "3", "4"])
        self.assertEqual(counts["titleSubtitleExcluded"], 1)
        self.assertTrue(samples._selection_evidence(rows[1], categories[0])["titleDubbedLabelHint"])

    def test_live_hdtc_and_tc_quality_tags_do_not_hide_language_conflicts(self):
        categories = [{"category_id": "1", "category_name": "English"}]
        for quality in ("HDTC", "TC"):
            with self.subTest(quality=quality):
                selected, _counts = samples.select_recent(categories,
                    [row(1, NOW - 1, name=f"Movie (2026) [FRN] [{quality}]"),
                     row(2, NOW - 2, name=f"Movie (2026) [HIN] [{quality}]")], now=NOW)
                self.assertEqual(selected["en"], [])
                self.assertEqual([entry[1] for entry in selected["hi"]], ["2"])

    def test_additional_known_xtream_metadata_stays_raw_and_legacy_url_key_stays_guarded(self):
        raw = {"o_name": "Actual alternate title", "mpaa_rating": "R", "episode_run_time": [90],
               "rating_count_kinopoisk": 3, "kinopoisk_id": "123", "kinopoisk_url": "https://private.invalid/movie/1"}
        safe = samples.sanitize_record(raw)
        self.assertEqual(safe["unknownFieldCount"], 0)
        self.assertEqual(safe["record"]["episode_run_time"], [90])
        self.assertEqual(safe["record"]["o_name"], raw["o_name"])
        self.assertIsNone(safe["record"]["kinopoisk_url"])
        public = "https://www.themoviedb.org/movie/123"
        self.assertEqual(samples.sanitize_record({"kinopoisk_url": public})["record"]["kinopoisk_url"], public)

    def test_metadata_sanitizer_retains_raw_types_empty_values_and_original_nesting(self):
        original = {"stream_id": 101, "name": "Actual provider title", "stream_type": "movie",
            "added": "1799999999", "rating": 0, "genre": [], "director": None,
            "direct_source": "", "custom_sid": "", "info": {"plot": "", "audio": {"bit_rate": "128000", "channels": 2},
            "video": {"width": 1920}, "movie_image": None}, "mystery-secret-key": "PRIVATE"}
        result = samples.sanitize_record(original)
        self.assertEqual(result["record"]["added"], "1799999999")
        self.assertEqual(result["record"]["stream_id"], 101)
        self.assertEqual(result["record"]["direct_source"], "")
        self.assertEqual(result["record"]["info"], original["info"])
        self.assertEqual(result["unknownFieldCount"], 1)
        self.assertNotIn("mystery-secret-key", json.dumps(result))
        self.assertEqual(result["record"]["genre"], [])
        self.assertEqual(result["record"]["rating"], 0)

    def test_private_urls_paths_unknown_fields_and_secret_variants_are_withheld(self):
        secrets = samples._secret_values([CONFIG])
        payload = {"plot": "PRIVATE-PASSWORD in reflected text", "name": "PROVIDER.INVALID title",
            "cast": ["private-user"], "info": {"direct_source": "https://provider.invalid/movie/u/p/1.mkv",
            "movie_image": "//other-host/movie/u/p/1.mkv", "cover": "/movie/u/p/1.mkv",
            "custom_sid": "secret-session", "description": "https://unknown.invalid/a", "password": "private-password"},
            "unknown-private": "https://provider.invalid/anything"}
        result = samples.sanitize_record(payload, secrets)
        raw = json.dumps(result)
        for value in ("PRIVATE-PASSWORD", "PROVIDER.INVALID", "private-user", "private-password", "https://", "secret-session", "unknown-private", "/movie/u/p"):
            self.assertNotIn(value, raw)
        self.assertIsNone(result["record"]["plot"])
        self.assertGreaterEqual(result["withheldFieldCount"], 6)
        self.assertTrue(any(field.get("withheldReason") == "secret_reflection" for field in result["fieldsPresent"]))

    def test_only_strict_public_metadata_urls_are_retained(self):
        good = "https://image.tmdb.org/t/p/w500/abc.jpg"
        self.assertTrue(samples._public_url(good))
        self.assertTrue(samples._public_url("https://www.imdb.com/title/tt1234567/"))
        for value in ("http://image.tmdb.org/t/p/w500/abc.jpg", good + "?key=secret", good + "#secret",
                      "https://user:pass@image.tmdb.org/t/p/w500/abc.jpg", "https://image.tmdb.org.evil/t/p/w500/abc.jpg"):
            self.assertFalse(samples._public_url(value))
        self.assertEqual(samples.sanitize_record({"movie_image": good})["record"]["movie_image"], good)

    def test_short_secrets_encoded_urls_auth_markers_and_relative_playback_paths_are_redacted(self):
        cases = [({"plot": "password:pin"}, ("pin",)),
                 ({"name": "PROVIDER.INVALID title"}, ("provider.invalid",)),
                 ({"stream_icon": "https%3A%2F%2Fother-private%2Flive%2Faccount%2Ftoken%2F123.ts"}, ()),
                 ({"plot": "//other-private/live/account/token/123.ts"}, ()),
                 ({"plot": "/live/account/token/123.ts"}, ()),
                 ({"plot": "token=unrecognized-sensitive-value"}, ()),
                 ({"description": "auth%253Dprivate-value"}, ()),
                 ({"plot": '{"api_key":"private-value"}'}, ())]
        for payload, secrets in cases:
            with self.subTest(payload_keys=list(payload)):
                result = samples.sanitize_record(payload, secrets)
                self.assertIsNone(next(iter(result["record"].values())))
                self.assertEqual(result["withheldFieldCount"], 1)

    def test_nested_arrays_have_shared_node_and_output_limits(self):
        tree = [0] * 20
        for _ in range(3):
            tree = [tree] * 20
        result = samples.sanitize_record({"streams": tree})
        self.assertTrue(result["limitReached"])
        self.assertLessEqual(result["visitedNodes"], samples.MAX_NODES)
        self.assertLess(len(json.dumps(result)), 20_000)

    def test_detail_is_separate_and_bound_or_explicitly_unverified(self):
        cases = [({"movie_data": {"stream_id": 101}, "info": {"plot": "Raw provider plot"}}, "provider_id_matches", "available"),
                 ({"info": {"plot": "Unbound raw provider plot"}}, "request_only_unverified", "received_unverified_identity"),
                 ({"movie_data": {"stream_id": 999}}, "identity_mismatch", "identity_mismatch")]
        for payload, binding, status in cases:
            with self.subTest(binding=binding):
                session = mock.Mock(deadline=samples.time.monotonic() + 20, redirect_flags=None)
                with mock.patch.object(samples, "ObservedSession", return_value=session), \
                        mock.patch.object(samples.vod, "_request_panel_json_once", return_value=payload) as fetch:
                    result = samples.fetch_detail("server_1", "101", CONFIG, samples.time.monotonic() + 100, ())
                self.assertEqual((result["identityBinding"], result["status"]), (binding, status))
                self.assertEqual(fetch.call_args.args[2], "get_vod_info")
                self.assertEqual(fetch.call_args.kwargs["item_id"], "101")
                self.assertIsNone(result["detailRecord"]) if binding == "identity_mismatch" else self.assertEqual(result["detailRecord"], payload)
                session.close.assert_called_once()
        self.assertEqual(samples._identity_binding({"stream_id": 999, "movie_data": {"stream_id": 101}}, "101"), "identity_mismatch")

    def test_detail_redirect_refusal_has_only_fixed_code_and_constraint_flags(self):
        session = mock.Mock(deadline=samples.time.monotonic() + 20,
                            redirect_flags={"sameHost": True, "samePath": False, "queryEquivalent": True})
        with mock.patch.object(samples, "ObservedSession", return_value=session), \
                mock.patch.object(samples.vod, "_request_panel_json_once", side_effect=samples.vod.CatalogBuildError("server_1: unsafe provider redirect refused.")):
            result = samples.fetch_detail("server_1", "101", CONFIG, samples.time.monotonic() + 100, ())
        self.assertEqual(result["status"], "redirect_refused")
        self.assertIsNone(result["detailRecord"])
        self.assertNotIn("https://", json.dumps(result))

    def test_cli_deduplicates_detail_queries_per_server_without_merging_list_payloads(self):
        entry = (NOW - 1, "101", row(101, str(NOW - 1)), {"category_id": "1", "category_name": "English / Hindi"})
        bulk = {"status": "available", "selected": {"en": [entry], "hi": [entry], "pa": []}, "counts": {},
                "deadline": samples.time.monotonic() + 200, "requests": 2, "downloadBytes": 1}
        detail = {"status": "available", "identityBinding": "provider_id_matches", "detailRecord": {"info": {"plot": "Provider detail"}}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            stream = io.StringIO()
            with mock.patch.dict(os.environ, {"ALLOW_INSECURE_PANEL_HTTP": "false"}), \
                    mock.patch.object(samples.vod, "load_server_config", side_effect=lambda server, **kw: samples.vod.ServerConfig(server, CONFIG.base_url, CONFIG.username, CONFIG.password)), \
                    mock.patch.object(samples, "fetch_bulk", return_value=bulk), \
                    mock.patch.object(samples, "fetch_detail", return_value=detail) as fetch, contextlib.redirect_stdout(stream):
                self.assertEqual(samples.main(["--output", str(output)]), 0)
            report = json.loads(output.read_text())
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(report["uniqueDetailRequests"], 3)
            self.assertEqual(report["selectionLanguageEvidence"], "explicit_title_markers_then_category_labels")
            first = report["servers"]["server_1"]["languages"]["en"]["samples"][0]
            self.assertNotIn("plot", first["listRecord"])
            self.assertEqual(first["detailRecord"]["info"]["plot"], "Provider detail")
            self.assertEqual(first["listRecord"]["added"], str(NOW - 1))
            self.assertEqual(first["selectionEvidence"]["categoryLanguageCodes"], ["en", "hi"])
            self.assertNotIn("Real provider title", stream.getvalue())
            self.assertLess(output.stat().st_size, samples.MAX_REPORT_BYTES)

    def test_workflow_dispatch_has_exact_mode_timeout_and_artifact(self):
        text = (ROOT / ".github/workflows/provider-transport-diagnostics.yml").read_text(encoding="utf-8")
        self.assertIn("- vod_recent_samples", text)
        self.assertIn('"$DIAGNOSTIC_MODE" == "vod_recent_samples"', text)
        self.assertIn("8m python -u scripts/diagnose_vod_recent_samples.py", text)
        self.assertIn("--output .build/vod-recent-samples/report.json", text)
        self.assertIn("path: .build/vod-recent-samples/report.json", text)


if __name__ == "__main__":
    unittest.main()
