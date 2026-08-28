from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_live_events as live  # noqa: E402


NOW = 1_800_000_000


def cricket_time(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def football_row(
    fixture_id: int,
    status: str,
    timestamp: int,
    home: str,
    away: str,
    league: str = "Premier League",
) -> dict[str, object]:
    return {
        "fixture": {
            "id": fixture_id,
            "timestamp": timestamp,
            "status": {"short": status, "long": "Provider text"},
        },
        "league": {"id": 39, "name": league, "country": "England"},
        "teams": {
            "home": {"id": 1, "name": home, "logo": "https://secret.test/a.png"},
            "away": {"id": 2, "name": away, "logo": "https://secret.test/b.png"},
        },
        "goals": {"home": 1, "away": 0},
    }


class LiveEventNormalizationTests(unittest.TestCase):
    def test_cricket_live_and_starting_soon_only(self) -> None:
        payload = {
            "status": "success",
            "info": {"hitsToday": 12, "hitsLimit": 100},
            "data": [
                {
                    "id": "test-live",
                    "dateTimeGMT": cricket_time(NOW - 3_600),
                    "matchType": "test",
                    "status": "Day 1: Stumps",
                    "ms": "live",
                    "t1": "England",
                    "t2": "India",
                    "t1s": "248/9",
                    "t2s": "",
                },
                {
                    "id": "t20-soon",
                    "dateTimeGMT": cricket_time(NOW + 30 * 60),
                    "matchType": "t20",
                    "status": "Match starts soon",
                    "ms": "fixture",
                    "t1": "Punjab Kings",
                    "t2": "Mumbai Indians",
                },
                {
                    "id": "too-late",
                    "dateTimeGMT": cricket_time(NOW + 4 * 60 * 60),
                    "matchType": "odi",
                    "ms": "fixture",
                    "t1": "Australia",
                    "t2": "New Zealand",
                },
                {
                    "id": "finished",
                    "dateTimeGMT": cricket_time(NOW - 7_200),
                    "matchType": "odi",
                    "ms": "result",
                    "t1": "South Africa",
                    "t2": "Pakistan",
                },
            ],
        }

        events = live.normalize_cricket_events(
            payload,
            now_epoch_seconds=NOW,
            starting_soon_seconds=90 * 60,
        )

        self.assertEqual([event["id"] for event in events], [
            "cricketdata:test-live",
            "cricketdata:t20-soon",
        ])
        self.assertEqual(events[0]["state"], "LIVE")
        self.assertEqual(events[0]["competition"], "Test Cricket")
        self.assertEqual(events[0]["displayTitle"], "England vs India")
        self.assertEqual(events[1]["state"], "STARTING_SOON")
        self.assertNotIn("status", events[0])
        self.assertNotIn("t1s", json.dumps(events))

    def test_football_live_and_starting_soon_only(self) -> None:
        payload = {
            "errors": [],
            "results": 5,
            "response": [
                football_row(10, "1H", NOW - 1_200, "Arsenal", "Chelsea"),
                football_row(11, "NS", NOW + 2_700, "Liverpool", "Everton"),
                football_row(12, "FT", NOW - 7_200, "Fulham", "Leeds"),
                football_row(13, "PST", NOW + 1_200, "Roma", "Lazio"),
                football_row(14, "NS", NOW + 7_200, "Milan", "Inter"),
            ],
        }

        events = live.normalize_football_events(
            payload,
            now_epoch_seconds=NOW,
            starting_soon_seconds=90 * 60,
        )

        self.assertEqual([event["id"] for event in events], [
            "api-football:10",
            "api-football:11",
        ])
        self.assertEqual(events[0]["state"], "LIVE")
        self.assertEqual(events[1]["state"], "STARTING_SOON")
        self.assertEqual(events[0]["sport"], "FOOTBALL")
        self.assertNotIn("logo", json.dumps(events))
        self.assertNotIn("goals", json.dumps(events))

    def test_build_feed_is_deterministic_deduplicated_and_live_first(self) -> None:
        cricket = {
            "status": "success",
            "data": [
                {
                    "id": "same",
                    "dateTimeGMT": cricket_time(NOW + 1_800),
                    "matchType": "t20",
                    "ms": "fixture",
                    "t1": "Team C",
                    "t2": "Team D",
                },
                {
                    "id": "same",
                    "dateTimeGMT": cricket_time(NOW + 1_800),
                    "matchType": "t20",
                    "ms": "fixture",
                    "t1": "Team C",
                    "t2": "Team D",
                },
            ],
        }
        football = {
            "errors": {},
            "response": [
                football_row(50, "HT", NOW - 2_000, "Team A", "Team B"),
            ],
        }

        feed = live.build_feed(
            cricket,
            football,
            now_epoch_seconds=NOW,
            starting_soon_minutes=90,
            lifetime_seconds=3_600,
        )

        self.assertEqual(feed["schemaVersion"], 1)
        self.assertEqual(feed["generatedAtEpochSeconds"], NOW)
        self.assertEqual(feed["expiresAtEpochSeconds"], NOW + 3_600)
        self.assertEqual(len(feed["events"]), 2)
        self.assertEqual(feed["events"][0]["state"], "LIVE")
        self.assertEqual(feed["events"][1]["state"], "STARTING_SOON")

    def test_provider_supplied_secrets_and_extra_fields_are_not_published(self) -> None:
        sentinel = "sentinel-private-api-key"
        cricket = {
            "status": "success",
            "apikey": sentinel,
            "data": [
                {
                    "id": "safe",
                    "dateTimeGMT": cricket_time(NOW),
                    "matchType": "test",
                    "ms": "live",
                    "t1": "West Indies",
                    "t2": "Sri Lanka",
                    "private": sentinel,
                }
            ],
        }
        football = {"errors": [], "response": [], "private": sentinel}

        encoded = json.dumps(
            live.build_feed(
                cricket,
                football,
                now_epoch_seconds=NOW,
            )
        )
        self.assertNotIn(sentinel, encoded)
        self.assertNotIn("apikey", encoded)

    def test_timezone_less_cricket_timestamp_is_utc(self) -> None:
        parsed = live.parse_cricket_utc("2027-01-15T12:30:00")
        expected = int(
            datetime(2027, 1, 15, 12, 30, tzinfo=timezone.utc).timestamp()
        )
        self.assertEqual(parsed, expected)

    def test_date_range_identifies_utc_midnight_crossing(self) -> None:
        near_midnight = int(
            datetime(2027, 2, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        )
        start, end = live.utc_date_range(near_midnight, 90)
        self.assertEqual(start.isoformat(), "2027-02-01")
        self.assertEqual(end.isoformat(), "2027-02-02")

    def test_invalid_provider_envelopes_fail_closed(self) -> None:
        with self.assertRaises(live.FeedBuildError):
            live.normalize_cricket_events(
                {"status": "failure", "data": []},
                now_epoch_seconds=NOW,
                starting_soon_seconds=5_400,
            )
        with self.assertRaises(live.FeedBuildError):
            live.normalize_football_events(
                {"errors": {"token": "invalid"}, "response": []},
                now_epoch_seconds=NOW,
                starting_soon_seconds=5_400,
            )


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        status_code: int = 200,
        payload: object | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.content = json.dumps(payload if payload is not None else {}).encode("utf-8")


class FakeSession:
    def __init__(
        self,
        response: FakeResponse | None = None,
        error: Exception | None = None,
        responses: list[FakeResponse] | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.responses = list(responses or [])
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        assert self.response is not None
        return self.response


class LiveEventTransportTests(unittest.TestCase):
    def test_request_exception_never_echoes_query_parameter_key(self) -> None:
        sentinel = "sentinel-secret-in-exception"
        session = FakeSession(error=RuntimeError(f"failed?apikey={sentinel}"))

        with self.assertRaises(live.FeedBuildError) as raised:
            live.fetch_json(
                session,
                provider_name="CricketData",
                url=live.CRICKETDATA_URL,
                allowed_host=live.CRICKETDATA_HOST,
                params={"apikey": sentinel},
            )

        self.assertNotIn(sentinel, str(raised.exception))
        self.assertEqual(str(raised.exception), "CricketData request failed.")

    def test_http_error_never_echoes_final_url(self) -> None:
        sentinel = "sentinel-secret-in-url"
        response = FakeResponse(
            url=f"{live.CRICKETDATA_URL}?apikey={sentinel}",
            status_code=429,
        )
        with self.assertRaises(live.FeedBuildError) as raised:
            live.fetch_json(
                FakeSession(response=response),
                provider_name="CricketData",
                url=live.CRICKETDATA_URL,
                allowed_host=live.CRICKETDATA_HOST,
                params={"apikey": sentinel},
            )
        self.assertNotIn(sentinel, str(raised.exception))
        self.assertEqual(str(raised.exception), "CricketData returned HTTP 429.")

    def test_redirect_to_unexpected_host_is_rejected(self) -> None:
        response = FakeResponse(
            url="https://untrusted.example/events",
            payload={"status": "success", "data": []},
        )
        with self.assertRaises(live.FeedBuildError):
            live.fetch_json(
                FakeSession(response=response),
                provider_name="CricketData",
                url=live.CRICKETDATA_URL,
                allowed_host=live.CRICKETDATA_HOST,
                params={"apikey": "not-published"},
            )

    def test_api_football_same_day_uses_live_and_one_utc_date_query(self) -> None:
        midday = int(
            datetime(2027, 2, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        )
        live_row = football_row(100, "1H", midday - 900, "Arsenal", "Chelsea")
        duplicate_daily_row = football_row(
            100,
            "NS",
            midday - 900,
            "Arsenal",
            "Chelsea",
        )
        upcoming_row = football_row(
            101,
            "NS",
            midday + 1_800,
            "Liverpool",
            "Everton",
        )
        session = FakeSession(
            responses=[
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={"errors": [], "response": [live_row]},
                ),
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={
                        "errors": [],
                        "response": [duplicate_daily_row, upcoming_row],
                    },
                ),
            ]
        )

        payload = live.fetch_api_football_payload(
            session,
            api_football_key="not-published",
            now_epoch_seconds=midday,
            starting_soon_minutes=90,
        )

        self.assertEqual(
            [call[1]["params"] for call in session.calls],
            [
                {"live": "all"},
                {"date": "2027-02-01", "timezone": "UTC"},
            ],
        )
        self.assertEqual(payload["results"], 2)
        self.assertEqual(
            [row["fixture"]["id"] for row in payload["response"]],
            [100, 101],
        )
        self.assertEqual(payload["response"][0]["fixture"]["status"]["short"], "1H")
        for _, request in session.calls:
            self.assertEqual(
                request["headers"],
                {"x-apisports-key": "not-published"},
            )
            self.assertNotIn("x-apisports-key", request["params"])

    def test_api_football_cross_midnight_adds_tomorrow_utc_date_query(self) -> None:
        near_midnight = int(
            datetime(2027, 2, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        )
        session = FakeSession(
            responses=[
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={"errors": [], "response": []},
                )
                for _ in range(3)
            ]
        )

        payload = live.fetch_api_football_payload(
            session,
            api_football_key="not-published",
            now_epoch_seconds=near_midnight,
            starting_soon_minutes=90,
        )

        self.assertEqual(
            [call[1]["params"] for call in session.calls],
            [
                {"live": "all"},
                {"date": "2027-02-01", "timezone": "UTC"},
                {"date": "2027-02-02", "timezone": "UTC"},
            ],
        )
        self.assertEqual(payload["response"], [])

    def test_api_football_keeps_live_fixture_from_previous_utc_date(self) -> None:
        after_midnight = int(
            datetime(2027, 2, 2, 0, 30, tzinfo=timezone.utc).timestamp()
        )
        previous_date_kickoff = int(
            datetime(2027, 2, 1, 23, 20, tzinfo=timezone.utc).timestamp()
        )
        session = FakeSession(
            responses=[
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={
                        "errors": [],
                        "response": [
                            football_row(
                                102,
                                "2H",
                                previous_date_kickoff,
                                "Team A",
                                "Team B",
                            )
                        ],
                    },
                ),
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={"errors": [], "response": []},
                ),
            ]
        )

        payload = live.fetch_api_football_payload(
            session,
            api_football_key="not-published",
            now_epoch_seconds=after_midnight,
            starting_soon_minutes=90,
        )
        events = live.normalize_football_events(
            payload,
            now_epoch_seconds=after_midnight,
            starting_soon_seconds=90 * 60,
        )

        self.assertEqual([event["id"] for event in events], ["api-football:102"])
        self.assertEqual(events[0]["state"], "LIVE")

    def test_api_football_exact_midnight_boundary_queries_tomorrow(self) -> None:
        exact_boundary = int(
            datetime(2027, 2, 1, 22, 30, tzinfo=timezone.utc).timestamp()
        )
        session = FakeSession(
            responses=[
                FakeResponse(
                    url=live.API_FOOTBALL_URL,
                    payload={"errors": [], "response": []},
                )
                for _ in range(3)
            ]
        )

        live.fetch_api_football_payload(
            session,
            api_football_key="not-published",
            now_epoch_seconds=exact_boundary,
            starting_soon_minutes=90,
        )

        self.assertEqual(
            [call[1]["params"] for call in session.calls],
            [
                {"live": "all"},
                {"date": "2027-02-01", "timezone": "UTC"},
                {"date": "2027-02-02", "timezone": "UTC"},
            ],
        )

    def test_api_football_fails_closed_on_each_response_envelope(self) -> None:
        near_midnight = int(
            datetime(2027, 2, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        )
        successful = {"errors": [], "response": []}
        failed = {"errors": {"request": "rejected"}, "response": []}

        for error_index in range(3):
            with self.subTest(error_index=error_index):
                envelopes = [successful, successful, successful]
                envelopes[error_index] = failed
                session = FakeSession(
                    responses=[
                        FakeResponse(url=live.API_FOOTBALL_URL, payload=envelope)
                        for envelope in envelopes
                    ]
                )

                with self.assertRaises(live.FeedBuildError):
                    live.fetch_api_football_payload(
                        session,
                        api_football_key="not-published",
                        now_epoch_seconds=near_midnight,
                        starting_soon_minutes=90,
                    )
                self.assertEqual(len(session.calls), error_index + 1)

    def test_feed_write_is_valid_compact_json(self) -> None:
        payload = {
            "schemaVersion": 1,
            "generatedAtEpochSeconds": NOW,
            "expiresAtEpochSeconds": NOW + 3_600,
            "events": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "live-events.json"
            live.write_feed(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_workflow_uses_github_secrets_and_no_pull_request_secret_trigger(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "live-events.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("${{ secrets.CRICKETDATA_API_KEY }}", workflow)
        self.assertIn("${{ secrets.API_FOOTBALL_KEY }}", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("pull_request:", workflow)


if __name__ == "__main__":
    unittest.main()
