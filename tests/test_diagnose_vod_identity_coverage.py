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

import diagnose_vod_identity_coverage as audit


def movie(identifier, **fields):
    return {"stream_id": identifier, "name": f"PRIVATE TITLE {identifier}", **fields}


class IdentityCoverageTests(unittest.TestCase):
    def test_syntax_valid_ids_overlap_and_alias_presence_use_unique_provider_records(self):
        categories = [{"category_id": "1", "category_name": "English"},
                      {"category_id": "2", "category_name": "Hindi / Punjabi"}]
        rows = [movie(1, category_id="1", tmdb_id="123", imdb="tt1234567",
                      runtime=100, director="PRIVATE DIRECTOR", cast="PRIVATE ACTOR"),
                movie(2, category_id="2", tmdb=0, imdb_url="https://www.imdb.com/title/tt7654321/"),
                movie(3, category_id="1", languages=["en", "hi"],
                      tmdbUrl="https://www.themoviedb.org/movie/456-private-slug", imdb_id=1234),
                movie(4, category_id="1")]
        rows.append(copy.deepcopy(rows[0]))
        report = audit.summarize_inventory(categories, rows)
        self.assertTrue(report["success"])
        self.assertEqual((report["inventoryRows"], report["uniqueProviderIds"], report["duplicateRows"]), (5, 4, 1))
        total = report["total"]
        self.assertEqual((total["movieCount"], total["withTmdb"], total["withImdb"], total["withBoth"]), (4, 2, 2, 1))
        self.assertEqual((total["withAnySupportedId"], total["withNeitherSupportedId"]), (3, 1))
        self.assertEqual((total["withNumericTmdb"], total["withTtImdb"], total["withPublicTmdbUrl"], total["withPublicImdbUrl"]), (1, 1, 1, 1))
        self.assertEqual((total["withRuntime"], total["withDirector"], total["withCast"]), (1, 1, 1))
        groups = report["languageGroups"]["currentNormalizedLanguages"]
        self.assertEqual([groups[key]["movieCount"] for key in ("en", "hi", "pa", "targetUnion")], [3, 2, 1, 4])
        self.assertEqual(report["rawFields"]["tmdb"], {"present": 1, "nonempty": 1, "valid": 0})
        self.assertEqual(report["rawFields"]["imdb_id"], {"present": 1, "nonempty": 1, "valid": 0})

    def test_subtitle_and_ambiguous_country_markers_are_separate_from_language_labels(self):
        labels = {"1": "[EN] SUB", "2": "English subtitles", "3": "INDIA Movies",
                  "4": "[PAN] Movies", "5": "[PA] Movies", "6": "[HI] Latest",
                  "7": "[EN] Movies", "8": "PUNJABI Movies", "9": "English MULTISUB"}
        report = audit.summarize_inventory(
            [{"category_id": key, "category_name": value} for key, value in labels.items()],
            [movie(int(key), category_id=key) for key in labels])
        self.assertTrue(report["success"])
        groups = report["languageGroups"]
        self.assertEqual(groups["subtitleCategoryHints"]["en"]["movieCount"], 3)
        self.assertEqual(groups["explicitCategoryLanguageLabels"]["en"]["movieCount"], 1)
        self.assertEqual(groups["explicitCategoryLanguageLabels"]["hi"]["movieCount"], 1)
        self.assertEqual(groups["explicitCategoryLanguageLabels"]["pa"]["movieCount"], 1)
        self.assertEqual(groups["ambiguousCategoryCodeHints"]["pa"]["movieCount"], 2)
        self.assertEqual(groups["rawLanguageMetadata"]["targetUnion"]["movieCount"], 0)
        india = next(row for row in report["categories"] if row["evidenceType"] == "unclassified")
        self.assertEqual(india["languageCodes"], [])
        self.assertTrue(all("categoryId" not in row for row in report["categories"]))

    def test_original_spoken_and_structured_metadata_have_separate_evidence_buckets(self):
        report = audit.summarize_inventory([], [movie(1, original_language="hi"),
            movie(2, spoken_languages=[{"iso_639_1": "pa"}]), movie(3, language="en"),
            movie(4, languages=["en", "hi"], original_language="pa")])
        groups = report["languageGroups"]
        self.assertEqual(groups["structuredLanguageFields"]["targetUnion"]["movieCount"], 2)
        self.assertEqual(groups["originalSpokenLanguageHints"]["targetUnion"]["movieCount"], 3)
        self.assertEqual(groups["rawLanguageMetadata"]["targetUnion"]["movieCount"], 4)
        self.assertEqual(groups["rawLanguageMetadata"]["pa"]["movieCount"], 1)

    def test_strict_identifier_forms_reject_credentials_queries_other_hosts_and_provider_ids(self):
        for value in (True, False, 0, -1, 1.2, "0", "tt1234567", "123junk", 10**12, float("inf"), {"id": True}):
            with self.subTest(value=value):
                self.assertIsNone(audit._identity(value, "tmdb"))
        for value in (1234567, "1234567", "TT1234567", "tt123", "https://imdb.com.evil/title/tt1234567/",
                      "https://user:password@www.imdb.com/title/tt1234567/",
                      "https://www.imdb.com/title/tt1234567/?secret=value",
                      "https://www.imdb.com/title/tt1234567/#private", "http://www.imdb.com/title/tt1234567/",
                      "https://www.imdb.com:8443/title/tt1234567/"):
            with self.subTest(value=value):
                self.assertIsNone(audit._identity(value, "imdb"))
        self.assertIsNone(audit._identity("https://www.themoviedb.org/tv/123", "tmdb"))
        self.assertIsNone(audit._identity("https://provider.invalid/movie/123", "tmdb"))
        report = audit.summarize_inventory([], [movie(123)])
        self.assertEqual(report["total"]["withNeitherSupportedId"], 1)
        self.assertEqual(report["total"]["withTmdb"], 0)

    def test_tmdb_safe_normalizer_forms_and_integral_float_hints_are_canonical(self):
        for value in (101, 101.0, "00101", "tmdb:101", {"id": 101}, {"id": "tmdb:101"}):
            with self.subTest(value=value):
                self.assertEqual(audit._identity(value, "tmdb"), ("101", "numeric"))
        self.assertEqual(audit._identity("999999999999", "tmdb"), ("999999999999", "numeric"))
        self.assertIsNone(audit._identity("١٢٣", "tmdb"))

    def test_nested_raw_language_evidence_is_visible_but_production_view_stays_top_level(self):
        report = audit.summarize_inventory([], [movie(1, metadata={"language": "hi", "tmdbId": 101}),
            movie(2, info={"spoken_languages": [{"iso_639_1": "pa"}]})])
        groups = report["languageGroups"]
        self.assertEqual(groups["currentNormalizedLanguages"]["targetUnion"]["movieCount"], 0)
        self.assertEqual(groups["rawLanguageMetadata"]["targetUnion"]["movieCount"], 2)
        self.assertEqual(groups["structuredLanguageFields"]["hi"]["movieCount"], 1)
        self.assertEqual(groups["originalSpokenLanguageHints"]["pa"]["movieCount"], 1)

    def test_contradictory_raw_aliases_are_counted_as_unverified_conflicts(self):
        report = audit.summarize_inventory([], [movie(1, tmdb="101", tmdb_id="102",
            imdb="tt1234567", imdbId="tt7654321", info={"tmdbId": "103"})])
        self.assertTrue(report["success"])
        self.assertEqual(report["total"]["withBoth"], 1)
        self.assertEqual(report["total"]["withConflictingTmdbHints"], 1)
        self.assertEqual(report["total"]["withConflictingImdbHints"], 1)
        self.assertEqual(report["rawFields"]["tmdbId"]["valid"], 1)

    def test_duplicate_title_identity_and_language_conflicts_fail_without_partial_counts(self):
        first = movie(1, language="en", tmdb="101")
        for second in (dict(first, name="RECYCLED PRIVATE TITLE"), dict(first, tmdb="102"),
                       dict(first, language="hi")):
            with self.subTest(second_keys=list(second)):
                report = audit.summarize_inventory([], [first, second])
                self.assertFalse(report["countsAvailable"])
                self.assertEqual(report["conflictingDuplicateIds"], 1)
                self.assertEqual(report["errorCounts"], {"conflicting_duplicates": 1})
                self.assertEqual(report["total"]["movieCount"], 0)

    def test_unusable_rows_bad_schema_and_deadline_do_not_leak_or_inflate(self):
        report = audit.summarize_inventory([], [None, {}, {"stream_id": False, "name": "PRIVATE"},
                                               {"stream_id": 1}, movie(2)])
        self.assertEqual(report["skippedUnusableRows"], 4)
        self.assertEqual(report["total"]["movieCount"], 1)
        self.assertEqual(audit.summarize_inventory([], {"PRIVATE": "SECRET"})["errorCounts"], {"schema_failure": 1})
        self.assertEqual(audit.summarize_inventory([], [movie(2)], deadline=0)["errorCounts"], {"deadline": 1})

    def test_collection_uses_only_two_bulk_actions_isolated_session_and_one_byte_budget(self):
        config = audit.vod.ServerConfig("server_1", "https://PRIVATE.invalid", "PRIVATE USER", "PRIVATE PASSWORD")
        session = mock.Mock()
        with mock.patch.object(audit.vod, "load_server_config", return_value=config) as loader, \
                mock.patch.object(audit, "DeadlineSession", return_value=session) as factory, \
                mock.patch.object(audit.vod, "_request_panel_json_once", side_effect=[[], [movie(1)]]) as fetch:
            report = audit.collect_server("server_1", True)
        self.assertTrue(report["success"])
        loader.assert_called_once_with("server_1", allow_insecure_http=True)
        self.assertEqual([call.args[2] for call in fetch.call_args_list], ["get_vod_categories", "get_vod_streams"])
        self.assertEqual(factory.call_args.args[2], 10)
        self.assertIs(fetch.call_args_list[0].kwargs["budget"], fetch.call_args_list[1].kwargs["budget"])
        self.assertEqual(fetch.call_args_list[1].kwargs["budget"].limit, 104 * 1024 * 1024)
        self.assertLessEqual(factory.call_args.args[0], audit.time.monotonic() + 150)
        session.close.assert_called_once()

    def test_fixed_failures_and_report_artifact_never_emit_raw_values(self):
        with mock.patch.object(audit.vod, "load_server_config", side_effect=ValueError("PRIVATE SECRET URL")):
            failed = audit.collect_server("server_2", False)
        self.assertEqual(failed["errorCounts"], {"configuration": 1})
        categories = [{"category_id": "123", "category_name": "PRIVATE SECRET English"}]
        report = audit.summarize_inventory(categories, [movie(1, category_id="123", tmdb="99", imdb="tt1234567",
            director="PRIVATE SECRET", cast=["PRIVATE SECRET"], private="https://PRIVATE.invalid/password")])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            stream = io.StringIO()
            with mock.patch.dict(os.environ, {"ALLOW_INSECURE_PANEL_HTTP": "false"}), \
                    mock.patch.object(audit, "collect_server", return_value=report) as collect, \
                    contextlib.redirect_stdout(stream):
                self.assertEqual(audit.main(["--output", str(path)]), 0)
            raw = path.read_text()
            for value in ("PRIVATE", "SECRET", "tt1234567", "https://", "\"99\"", "movie:1"):
                self.assertNotIn(value, raw + stream.getvalue())
            saved = json.loads(raw)
            self.assertEqual(set(saved["servers"]), set(audit.SERVERS))
            self.assertTrue(saved["providerHintsUnverified"])
            self.assertFalse(saved["audioAvailabilityVerified"])
            self.assertRegex(saved["capturedAtUtc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(collect.call_count, 3)
            self.assertEqual([call.args[0] for call in collect.call_args_list], list(audit.SERVERS))

    def test_configured_secret_variants_are_rejected_before_artifact_installation(self):
        with mock.patch.dict(os.environ, {"SERVER_1_PASSWORD": "private-password"}, clear=True):
            for value in audit.vod._secret_variants("private-password"):
                with self.assertRaises(audit.AuditFailure):
                    audit._reject_secret_reflection(b'{"value":"' + value + b'"}')
        with mock.patch.dict(os.environ, {"SERVER_1_PASSWORD": "123"}, clear=True):
            audit._reject_secret_reflection(b'{"movieCount":123}')

    def test_workflow_dispatch_has_exact_read_only_wiring_and_aggregate_artifact(self):
        text = (ROOT / ".github/workflows/provider-transport-diagnostics.yml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\s+- vod_identity_coverage$")
        self.assertIn('elif [[ "$DIAGNOSTIC_MODE" == "vod_identity_coverage" ]]; then', text)
        self.assertIn("timeout --signal=TERM --kill-after=10s 7m python -u scripts/diagnose_vod_identity_coverage.py", text)
        self.assertIn("--output .build/vod-identity-coverage/report.json", text)
        self.assertIn("path: .build/vod-identity-coverage/report.json", text)
        self.assertIn("if-no-files-found: error", text)
        self.assertIn("timeout-minutes: 10", text)
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read$")
        self.assertNotIn("contents: write", text)
        self.assertNotIn("id-token: write", text)
        for number in (1, 2, 3):
            for suffix in ("BASE_URL", "USERNAME", "PASSWORD"):
                key = f"SERVER_{number}_{suffix}"
                self.assertIn(key + ": ${{ secrets." + key + " }}", text)


if __name__ == "__main__":
    unittest.main()
