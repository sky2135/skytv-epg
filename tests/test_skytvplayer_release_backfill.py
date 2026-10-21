from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as app  # noqa: E402
import build_vod_catalogs as vod  # noqa: E402
import vod_tmdb as tmdb  # noqa: E402
import vod_reviewed_corrections as reviewed  # noqa: E402
from test_vod_tmdb import FakeResponse, FakeSession, movie_details  # noqa: E402


NOW = "2026-10-21T16:30:00Z"


def movie(identifier, title="Example Film (2026)", **fields):
    return {"stream_id": identifier, "name": title, "language": "en", **fields}


def source(*rows):
    return {
        "get_vod_streams": list(rows), "get_vod_categories": [], "vod_details": {},
        "get_series": [{"series_id": 990, "name": "Series (2026)", "language": "en"}],
        "get_series_categories": [], "series_details": {},
    }


class RecordingClient:
    def __init__(self, *, max_requests=3000, priorities=None, results=None):
        self.max_requests = max_requests
        self.stats = {"requests": 0, "matches": 0, "cacheHits": 0}
        self.priorities = priorities or {}
        self.results = results or {}
        self.items = []
        self.elapsed_limits = []
        self.closed = False

    def enrichment_priority(self, item, kind):
        return self.priorities.get(item["id"], 0)

    def reset_elapsed_budget(self, seconds):
        self.elapsed_limits.append(seconds)

    def enrich(self, item, kind):
        self.items.append((copy.deepcopy(item), kind))
        self.stats["requests"] += 1
        result = self.results.get(item["id"], {
            "matchStatus": "matched_search", "releaseDate": "2026-10-10", "tmdbId": "101",
        })
        if result["matchStatus"].startswith("matched_"):
            self.stats["matches"] += 1
        return dict(result)

    def close(self):
        self.closed = True


class ReleaseDateBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def backfill(self, data, client=None, *, now=NOW, seconds=600):
        return app.backfill_release_dates(
            [("server_1", data)], client or RecordingClient(), now,
            max_elapsed_seconds=seconds,
        )

    def test_only_english_current_year_movies_missing_a_full_date_are_processed(self):
        data = source(
            movie(1),
            movie(2, "Year Only (2026)", releaseDate="2026"),
            movie(3, "Month Only (2026)", releaseDate="2026-10"),
            movie(4, "Older Film (2025)"),
            movie(5, "Future Film (2027)"),
            movie(6, "Known Film (2026)", releaseDate="2026-10-01"),
            movie(7, "Dubbed Film (2026)", language=["en", "hi"]),
            movie(8, "Unknown Language (2026)", language="und"),
            movie(9, "Hindi Film (2026)", language="hi"),
            movie(10, "No Dated Identity"),
        )
        original = copy.deepcopy(data)
        client = RecordingClient()
        self.backfill(data, client)
        self.assertEqual({item["id"] for item, _kind in client.items}, {"1", "2", "3"})
        self.assertEqual({kind for _item, kind in client.items}, {"movie"})
        self.assertEqual(data, original)

    def test_title_year_overrides_polluted_provider_year_without_reinterpreting_titles(self):
        client = RecordingClient()
        self.backfill(source(
            movie(1, "Current Film (2026)", releaseDate="2025"),
            movie(2, "Old Film (2025)", releaseDate="2026"),
            movie(3, "EN - Decorated Film (2026) [FHD]"),
            movie(4, "Film 2026 Remaster"),
        ), client)
        self.assertEqual({item["id"] for item, _kind in client.items}, {"1", "3"})

    def test_reviewed_corrections_and_content_identity_apply_before_enrichment(self):
        client = RecordingClient(results={"102406": {"matchStatus": "no_match", "releaseDate": None}})
        self.backfill(source(movie(102406, "Doctor (2021)")), client, now="2021-10-21T16:30:00Z")
        self.assertEqual(len(client.items), 1)
        item, kind = client.items[0]
        self.assertEqual(item["contentId"], "server_1:movie:102406")
        self.assertEqual(kind, "movie")
        self.assertEqual(item["genres"], ["action", "drama"])
        self.assertEqual(tmdb._enrichment_identity(item, kind)[3], "2021")

    def test_reviewed_known_date_needs_no_external_request(self):
        client = RecordingClient()
        self.backfill(source(movie(178503, "Marvel's Behind the Mask (2021)")), client,
                      now="2021-10-21T16:30:00Z")
        self.assertEqual(client.items, [])

    def test_reviewed_year_can_select_a_title_without_an_embedded_year(self):
        correction = {"year": "2026", "rejectIdentity": False, "metadata": {"originalLanguage": "en"}}
        client = RecordingClient()
        with mock.patch.object(reviewed, "correction_for", return_value=correction), \
                mock.patch.object(tmdb, "correction_for", return_value=correction):
            self.backfill(source(movie(1, "Reviewed Film", releaseDate="2025")), client)
            self.assertEqual(len(client.items), 1)
            item, kind = client.items[0]
            self.assertIsNone(item["releaseDate"])
            self.assertEqual(tmdb._enrichment_identity(item, kind)[3], "2026")

    def test_cache_priority_precedes_recent_uploads_and_newest_wins_within_a_tier(self):
        client = RecordingClient(priorities={"1": 3, "2": 0, "3": 0, "4": 1})
        self.backfill(source(
            movie(1, added="1792599999"), movie(2, added="1792500000"),
            movie(3, added="1792600000"), movie(4, added="1792700000"),
        ), client)
        self.assertEqual([item["id"] for item, _kind in client.items], ["3", "2", "4", "1"])

    def test_request_budget_stops_before_another_title(self):
        client = RecordingClient(max_requests=1)
        result = self.backfill(source(movie(1), movie(2)), client)
        self.assertEqual(len(client.items), 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual(result["stoppedReason"], "request_budget")

    def test_elapsed_budget_stops_before_another_title(self):
        client = RecordingClient()
        with mock.patch.object(app.time, "monotonic", side_effect=[0, 0, 601, 601, 601, 601]):
            result = self.backfill(source(movie(1), movie(2)), client)
        self.assertLessEqual(len(client.items), 1)
        self.assertEqual(result["stoppedReason"], "time_budget")
        self.assertEqual(client.elapsed_limits, [600])

    def test_summary_counts_only_verified_full_current_year_dates_and_whitelists_stats(self):
        client = RecordingClient(results={
            "1": {"matchStatus": "matched_id", "releaseDate": "2026-10-10"},
            "2": {"matchStatus": "unavailable", "releaseDate": "2026-10-10"},
            "3": {"matchStatus": "matched_search", "releaseDate": "2025-10-10"},
            "4": {"matchStatus": "matched_search", "releaseDate": "2026-10"},
        })
        client.stats["privateSource"] = "https://provider.invalid/user/password"
        report = self.backfill(source(*(movie(identifier) for identifier in range(1, 5))), client)
        self.assertEqual(report["targetServers"], ["server_1"])
        self.assertEqual(report["year"], 2026)
        self.assertEqual(report["candidateCount"], 4)
        self.assertEqual(report["processedCount"], 4)
        self.assertEqual(report["matchedDates"], 1)
        self.assertEqual(report["servers"][0]["matchedDates"], 1)
        self.assertNotIn("privateSource", report["stats"])
        self.assertNotIn("provider.invalid", json.dumps(report))

    def test_real_client_request_cap_includes_search_and_detail_calls(self):
        session = FakeSession(FakeResponse({"total_pages": 1, "results": [
            {"id": 101, "title": "Example Film", "release_date": "2026-10-10"},
        ]}))
        client = tmdb.TmdbClient(self.root / "tmdb", token="private-read-token", session=session,
                                 max_requests=1)
        report = self.backfill(source(movie(1), movie(2)), client)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual(report["processedCount"], 1)
        self.assertEqual(report["matchedDates"], 0)
        self.assertEqual(report["stoppedReason"], "request_budget")

    def test_real_matcher_rejects_an_unrelated_supplied_identity(self):
        session = FakeSession(FakeResponse({**movie_details(), "release_date": "2026-10-10"}))
        client = tmdb.TmdbClient(self.root / "tmdb", token="private-read-token", session=session)
        self.backfill(source(movie(1, "A Different Film (2026)", tmdb_id=101)), client)
        self.assertEqual(len(session.calls), 1)
        cached = [json.loads(path.read_text()) for path in self.root.rglob("*.json")]
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["data"]["metadata"]["matchStatus"], "no_match")
        self.assertIsNone(cached[0]["data"]["metadata"]["releaseDate"])

    def test_verified_backfill_cache_is_reused_by_the_normal_publisher_without_a_token(self):
        session = FakeSession(FakeResponse({**movie_details(), "release_date": "2026-10-10"}))
        cache = self.root / "private-cache"
        data = source(movie(1, tmdb_id=101))
        client = tmdb.TmdbClient(cache / "tmdb", token="private-read-token", session=session)
        report = self.backfill(data, client)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(report["newlyMatchedDates"], 1)
        replay = tmdb.TmdbClient(cache / "tmdb", session=FakeSession())
        repeated = self.backfill(data, replay)
        self.assertEqual(repeated["knownCachedDatesBefore"], 1)
        self.assertEqual(repeated["matchedDates"], 1)
        self.assertEqual(repeated["newlyMatchedDates"], 0)
        replay.trending = lambda _kind: {}
        output = self.root / "public" / "skytvplayer"
        index = app.build(output, cache, [("server_1", data, None)], replay, NOW)
        manifest = app.read_artifact(output, index["servers"][0]["movies"])
        rows = [item for ref in manifest["shards"] for item in app.read_artifact(output, ref)["items"]]
        self.assertEqual(rows[0]["releaseDate"], "2026-10-10")
        self.assertEqual(rows[0]["metadataMatch"], "matched_id")
        self.assertEqual(replay.stats["requests"], 0)
        self.assertGreaterEqual(replay.stats["cacheHits"], 1)
        private = "".join(path.read_text() for path in cache.rglob("*.json"))
        self.assertNotIn("private-read-token", private)
        self.assertNotIn("provider.invalid", private)
        self.assertNotIn("secret", json.dumps(report))

    def write_snapshot(self, data):
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        (snapshots / "server_1_vod_bundle.json").write_text(json.dumps(data), encoding="utf-8")
        return snapshots

    def test_cli_backfill_reads_only_snapshot_and_writes_no_public_artifact(self):
        snapshots = self.write_snapshot(source(movie(1, direct_source="https://provider.invalid/user/password/1")))
        output = self.root / "public"
        cache = self.root / "cache"
        client = RecordingClient()
        stdout = io.StringIO()
        with mock.patch.object(app, "TmdbClient", return_value=client), \
                mock.patch.object(vod, "load_server_config", side_effect=AssertionError("Provider config requested")), \
                mock.patch.object(vod, "fetch_live_inputs", side_effect=AssertionError("Provider requested")), \
                mock.patch.object(app, "build", side_effect=AssertionError("Publication requested")), \
                contextlib.redirect_stdout(stdout):
            status = app.main([
                "--backfill-release-dates", "server_1", "--input-dir", str(snapshots),
                "--output-dir", str(output), "--cache-dir", str(cache), "--now-utc", NOW,
                "--max-tmdb-requests", "3000", "--backfill-max-seconds", "600",
            ])
        self.assertEqual(status, 0)
        self.assertTrue(client.closed)
        self.assertEqual([item["contentId"] for item, _kind in client.items], ["server_1:movie:1"])
        self.assertFalse(output.exists())
        rendered = stdout.getvalue()
        for private in ("Example Film", "provider.invalid", "password", str(snapshots), str(cache)):
            self.assertNotIn(private, rendered)
        self.assertIn("2026", rendered)

    def test_invalid_cli_is_rejected_before_loading_credentials_snapshots_or_clients(self):
        invalid = [
            ["--backfill-release-dates", "server_1"],
            ["--backfill-release-dates", "server_1", "--input-dir", "unused", "--server", "server_1"],
            ["--backfill-release-dates", "server_1", "--input-dir", "unused", "--fetch-series-details"],
            ["--backfill-release-dates", "server_1", "--input-dir", "unused", "--validate-only"],
            ["--backfill-release-dates", "server_1", "--backfill-release-dates", "server_1", "--input-dir", "unused"],
        ]
        invalid.extend([
            ["--backfill-release-dates", "server_1", "--input-dir", "unused", option, value]
            for option, value in [
                ("--backfill-max-seconds", "-1"), ("--backfill-max-seconds", "0"),
                ("--backfill-max-seconds", "901"),
                ("--backfill-max-seconds", "nan"), ("--backfill-max-seconds", "inf"),
                ("--max-tmdb-requests", "-1"), ("--max-tmdb-requests", "20001"),
            ]
        ])
        public = self.root / "public" / "skytvplayer"
        invalid.extend([
            ["--backfill-release-dates", "server_1", "--input-dir", "unused",
             "--output-dir", str(public), "--cache-dir", str(cache)]
            for cache in (public, public / "private-cache")
        ])
        with mock.patch.object(vod, "load_offline_inputs", side_effect=AssertionError("Snapshot loaded")), \
                mock.patch.object(vod, "load_server_config", side_effect=AssertionError("Credentials loaded")), \
                mock.patch.object(app, "TmdbClient", side_effect=AssertionError("Client created")), \
                mock.patch.object(app.requests, "Session", side_effect=AssertionError("Session created")):
            for arguments in invalid:
                with self.subTest(arguments=arguments), self.assertRaises((vod.CatalogBuildError, SystemExit)):
                    app.main(arguments)


if __name__ == "__main__":
    unittest.main()
