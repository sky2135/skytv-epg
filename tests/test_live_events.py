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


def api_payload(*rows: object) -> dict[str, object]:
    return {"errors": [], "results": len(rows), "response": list(rows)}


def common_game_row(
    event_id: int,
    status: object,
    timestamp: int,
    home: str,
    away: str,
    *,
    league: object = "Test League",
    nba: bool = False,
    nested_game_id: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "date": {"start": timestamp} if nba else timestamp,
        "status": status if isinstance(status, dict) else {
            "short": status,
            "long": "Provider text",
        },
        "league": league,
        "teams": {
            "home": {"name": home, "code": home[:3].upper(), "logo": "secret"},
            ("visitors" if nba else "away"): {
                "name": away,
                "code": away[:3].upper(),
                "logo": "secret",
            },
        },
        "scores": {"private": "not-published"},
    }
    if nested_game_id:
        row["game"] = {"id": event_id}
    else:
        row["id"] = event_id
    return row


def v2_match_event(
    event_id: str,
    provider: str,
    sport: str,
    start: int,
    first: str,
    second: str,
    competition: str = "League",
) -> dict[str, object]:
    return live.v2_head_to_head({
        "id": event_id,
        "provider": provider,
        "providerEventId": event_id.split(":", 1)[-1],
        "state": "LIVE",
        "sport": sport,
        "competition": competition,
        "displayTitle": f"{first} vs {second}",
        "startEpochSeconds": start,
        "participants": [live.participant(first), live.participant(second)],
    })


class MultiSportNormalizationTests(unittest.TestCase):
    def test_cricket_bracket_codes_become_safe_team_aliases(self) -> None:
        events = live.normalize_cricket_events(
            {
                "status": "success",
                "data": [{
                    "id": "coded",
                    "ms": "live",
                    "dateTimeGMT": cricket_time(NOW),
                    "matchType": "odi",
                    "t1": "India [IND]",
                    "t2": "Sri Lanka [SL]",
                }],
            },
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )

        self.assertEqual(events[0]["displayTitle"], "India vs Sri Lanka")
        self.assertIn("IND", events[0]["participants"][0]["aliases"])
        self.assertIn("India [IND]", events[0]["participants"][0]["aliases"])
        self.assertIn("SL", events[0]["participants"][1]["aliases"])

    def test_every_team_product_maps_live_and_starting_soon_and_filters_terminal(self) -> None:
        live_status = {
            "API_AFL": "Q2",
            "API_BASEBALL": "IN",
            "API_BASKETBALL": "Q3",
            "API_HANDBALL": "2H",
            "API_HOCKEY": "P2",
            "API_NBA": "2",
            "API_RUGBY": "1H",
            "API_VOLLEYBALL": "S3",
        }
        for spec in live.TEAM_PROVIDER_SPECS:
            with self.subTest(provider=spec.provider):
                nba = spec.nba_shape
                league: object = "standard" if nba else {"name": "Test League"}
                payload = api_payload(
                    common_game_row(
                        1,
                        live_status[spec.provider],
                        NOW - 300,
                        "Home Team",
                        "Away Team",
                        league=league,
                        nba=nba,
                        nested_game_id=spec.provider == "API_AFL",
                    ),
                    common_game_row(
                        2,
                        "1" if nba else "NS",
                        NOW + 1_800,
                        "Soon Home",
                        "Soon Away",
                        league=league,
                        nba=nba,
                    ),
                    common_game_row(
                        3,
                        "3" if nba else "FT",
                        NOW - 3_600,
                        "Done Home",
                        "Done Away",
                        league=league,
                        nba=nba,
                    ),
                    common_game_row(
                        4,
                        "1" if nba else "NS",
                        NOW + 10_800,
                        "Late Home",
                        "Late Away",
                        league=league,
                        nba=nba,
                    ),
                )
                events = live.normalize_team_events(
                    [payload],
                    spec,
                    now_epoch_seconds=NOW,
                    starting_soon_seconds=5_400,
                )
                self.assertEqual([event["state"] for event in events], [
                    "LIVE",
                    "STARTING_SOON",
                ])
                self.assertTrue(all(event["sport"] == spec.sport for event in events))
                self.assertTrue(all(event["kind"] == "HEAD_TO_HEAD" for event in events))
                self.assertNotIn("logo", json.dumps(events))
                self.assertNotIn("scores", json.dumps(events))

        self.assertEqual(
            live.event_state(
                "IN7",
                "",
                live_states=frozenset({"IN"}),
                start_epoch_seconds=NOW,
                now_epoch_seconds=NOW,
                starting_soon_seconds=5_400,
                allow_baseball_inning=True,
            ),
            "LIVE",
        )

    def test_nfl_uses_american_football_head_to_head_shape(self) -> None:
        events = live.normalize_team_events(
            [api_payload(common_game_row(
                77,
                "Q4",
                NOW - 900,
                "Buffalo Bills",
                "Miami Dolphins",
                league={"name": "NFL"},
            ))],
            live.nfl_spec(),
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )
        self.assertEqual(events[0]["provider"], "API_NFL")
        self.assertEqual(events[0]["sport"], "AMERICAN_FOOTBALL")
        self.assertEqual(events[0]["kind"], "HEAD_TO_HEAD")

    def test_formula_one_uses_session_identities_and_zero_participants(self) -> None:
        rows = [
            {
                "id": 101,
                "date": datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat(),
                "status": "live",
                "type": "Qualifying",
                "competition": {
                    "name": "Dutch Grand Prix",
                    "location": {"country": "Netherlands", "city": "Zandvoort"},
                },
                "circuit": {"name": "Circuit Zandvoort"},
            },
            {
                "id": 102,
                "date": datetime.fromtimestamp(NOW + 1_800, tz=timezone.utc).isoformat(),
                "status": "scheduled",
                "type": "Race",
                "competition": {"name": "Dutch Grand Prix"},
            },
            {
                "id": 103,
                "date": datetime.fromtimestamp(NOW - 7_200, tz=timezone.utc).isoformat(),
                "status": "completed",
                "type": "Practice 1",
                "competition": {"name": "Dutch Grand Prix"},
            },
        ]
        events = live.normalize_f1_events(
            [api_payload(*rows)],
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )
        self.assertEqual([event["state"] for event in events], ["LIVE", "STARTING_SOON"])
        self.assertTrue(all(event["kind"] == "SESSION" for event in events))
        self.assertTrue(all(event["participants"] == [] for event in events))
        self.assertEqual(events[0]["eventIdentity"]["name"], "Dutch Grand Prix")
        self.assertEqual(events[0]["sessionIdentity"]["name"], "Qualifying")
        self.assertNotIn("F1", events[0]["eventIdentity"]["aliases"])

        generic_only = live.normalize_f1_events(
            [api_payload({
                "id": 999,
                "date": datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat(),
                "status": "live",
                "type": "Race",
                "competition": {"name": "Formula 1"},
            })],
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )
        self.assertEqual(generic_only, [])

    def test_mma_groups_safe_slug_as_one_card_and_uses_main_fight(self) -> None:
        def fight(
            event_id: int,
            first: str,
            second: str,
            *,
            slug: object,
            main: object,
            status: str = "LIVE",
            timestamp: int = NOW,
        ) -> dict[str, object]:
            return {
                "id": event_id,
                "timestamp": timestamp,
                "status": {"short": status},
                "fighters": {
                    "first": {"name": first},
                    "second": {"name": second},
                },
                "slug": slug,
                "is_main": main,
                "category": "UFC",
            }

        events = live.normalize_mma_events(
            [api_payload(
                fight(
                    1,
                    "Main One",
                    "Main Two",
                    slug="ufc-300",
                    main=True,
                    timestamp=NOW + 1_800,
                ),
                fight(2, "Under One", "Under Two", slug="ufc-300", main=False),
                fight(3, "Solo One", "Solo Two", slug="", main=False),
                fight(4, "Done One", "Done Two", slug="ufc-299", main=True, status="FT"),
            )],
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )
        self.assertEqual([event["kind"] for event in events], ["CARD", "HEAD_TO_HEAD"])
        self.assertEqual(events[0]["eventIdentity"]["name"], "UFC 300")
        self.assertEqual(events[0]["startEpochSeconds"], NOW)
        self.assertEqual(
            [participant["name"] for participant in events[0]["participants"]],
            ["Main One", "Main Two"],
        )
        self.assertEqual(events[1]["participants"][0]["name"], "Solo One")

    def test_v1_stays_core_only_while_v2_has_new_kinds(self) -> None:
        cricket = v2_match_event(
            "cricketdata:1", "CRICKETDATA", "CRICKET", NOW, "India", "Sri Lanka"
        )
        football = v2_match_event(
            "api-football:2", "API_FOOTBALL", "FOOTBALL", NOW, "Arsenal", "Chelsea"
        )
        f1 = {
            "id": "api-formula-1:3",
            "provider": "API_FORMULA_1",
            "providerEventId": "3",
            "state": "LIVE",
            "sport": "MOTORSPORT",
            "kind": "SESSION",
            "competition": "Formula 1",
            "displayTitle": "Dutch Grand Prix — Race",
            "startEpochSeconds": NOW,
            "endEpochSeconds": None,
            "participants": [],
            "eventIdentity": live.identity("Dutch Grand Prix", ("Zandvoort",)),
            "sessionIdentity": live.identity("Race"),
        }
        v1, v2 = live.build_published_feeds(
            {
                "CRICKETDATA": [cricket],
                "API_FOOTBALL": [football],
                "API_FORMULA_1": [f1],
            },
            now_epoch_seconds=NOW,
        )
        self.assertEqual(v1["schemaVersion"], 1)
        self.assertEqual(v1["providers"], ["CRICKETDATA", "API_FOOTBALL"])
        self.assertEqual({event["sport"] for event in v1["events"]}, {"CRICKET", "FOOTBALL"})
        self.assertTrue(all("kind" not in event for event in v1["events"]))
        self.assertEqual(v2["schemaVersion"], 2)
        self.assertIn("API_FORMULA_1", v2["providers"])
        self.assertIn("SESSION", {event["kind"] for event in v2["events"]})

    def test_nba_success_excludes_api_basketball_nba_and_semantic_dedupe_prefers_nba(self) -> None:
        basketball_nba = v2_match_event(
            "api-basketball:10",
            "API_BASKETBALL",
            "BASKETBALL",
            NOW,
            "Lakers",
            "Celtics",
            "NBA Regular Season",
        )
        basketball_other = v2_match_event(
            "api-basketball:11",
            "API_BASKETBALL",
            "BASKETBALL",
            NOW + 60,
            "Madrid",
            "Barcelona",
            "EuroLeague",
        )
        nba = v2_match_event(
            "api-nba:12",
            "API_NBA",
            "BASKETBALL",
            NOW,
            "Lakers",
            "Celtics",
            "NBA",
        )
        _, v2 = live.build_published_feeds(
            {
                "CRICKETDATA": [],
                "API_BASKETBALL": [basketball_nba, basketball_other],
                "API_NBA": [nba],
            },
            now_epoch_seconds=NOW,
        )
        self.assertEqual(
            [event["provider"] for event in v2["events"]],
            ["API_NBA", "API_BASKETBALL"],
        )
        self.assertNotIn("api-basketball:10", [event["id"] for event in v2["events"]])

    def test_partial_provider_failure_is_soft_but_both_core_fail_is_fatal(self) -> None:
        def fail() -> list[dict[str, object]]:
            raise live.FeedBuildError("safe failure")

        successes, failures = live.collect_provider_events({
            "CRICKETDATA": lambda: [],
            "API_FOOTBALL": fail,
            "API_HOCKEY": lambda: [],
        })
        self.assertEqual(list(successes), ["CRICKETDATA", "API_HOCKEY"])
        self.assertEqual(failures, {"API_FOOTBALL": "safe failure"})
        with self.assertRaises(live.FeedBuildError):
            live.collect_provider_events({
                "CRICKETDATA": fail,
                "API_FOOTBALL": fail,
                "API_HOCKEY": lambda: [],
            })

    def test_global_count_and_encoded_size_are_capped(self) -> None:
        events = [
            v2_match_event(
                f"api-hockey:{index}",
                "API_HOCKEY",
                "HOCKEY",
                NOW + index,
                f"Home {index} " + "H" * 80,
                f"Away {index} " + "A" * 80,
            )
            for index in range(2_200)
        ]
        feed = live.make_feed(
            schema_version=2,
            providers=["API_HOCKEY"],
            events=events,
            now_epoch_seconds=NOW,
            starting_soon_minutes=90,
            lifetime_seconds=3_600,
        )
        self.assertLessEqual(len(feed["events"]), 2_000)
        self.assertLessEqual(len(live.encoded_feed_bytes(feed)), 512 * 1024)

    def test_v2_output_is_whitelisted_and_secret_safe(self) -> None:
        sentinel = "secret-provider-field"
        spec = next(item for item in live.TEAM_PROVIDER_SPECS if item.provider == "API_HOCKEY")
        row = common_game_row(
            1,
            "P1",
            NOW,
            "Toronto",
            "Montreal",
            league={"name": "NHL", "private": sentinel},
        )
        row["private"] = sentinel
        events = live.normalize_team_events(
            [api_payload(row)],
            spec,
            now_epoch_seconds=NOW,
            starting_soon_seconds=5_400,
        )
        encoded = json.dumps(events)
        self.assertNotIn(sentinel, encoded)
        self.assertEqual(set(events[0]), {
            "id",
            "provider",
            "providerEventId",
            "state",
            "sport",
            "kind",
            "competition",
            "displayTitle",
            "startEpochSeconds",
            "endEpochSeconds",
            "participants",
            "eventIdentity",
            "sessionIdentity",
        })


class MultiSportTransportTests(unittest.TestCase):
    def test_common_team_products_query_yesterday_and_today_with_nba_timezone_exception(self) -> None:
        midday = int(datetime(2027, 2, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        for spec in live.TEAM_PROVIDER_SPECS:
            with self.subTest(provider=spec.provider):
                session = FakeSession(responses=[
                    FakeResponse(url=spec.url, payload=api_payload()),
                    FakeResponse(url=spec.url, payload=api_payload()),
                ])
                live.fetch_team_payloads(
                    session,
                    spec=spec,
                    api_sports_key="not-published",
                    now_epoch_seconds=midday,
                    starting_soon_minutes=90,
                )
                params = [call[1]["params"] for call in session.calls]
                self.assertEqual(
                    [item["date"] for item in params],
                    ["2027-01-31", "2027-02-01"],
                )
                self.assertEqual(
                    ["timezone" in item for item in params],
                    [not spec.nba_shape, not spec.nba_shape],
                )
                self.assertTrue(all(
                    call[1]["headers"] == {"x-apisports-key": "not-published"}
                    for call in session.calls
                ))

    def test_common_team_product_adds_tomorrow_across_midnight(self) -> None:
        near_midnight = int(datetime(2027, 2, 1, 23, 30, tzinfo=timezone.utc).timestamp())
        spec = next(item for item in live.TEAM_PROVIDER_SPECS if item.provider == "API_RUGBY")
        session = FakeSession(responses=[
            FakeResponse(url=spec.url, payload=api_payload()) for _ in range(3)
        ])
        live.fetch_team_payloads(
            session,
            spec=spec,
            api_sports_key="not-published",
            now_epoch_seconds=near_midnight,
            starting_soon_minutes=90,
        )
        self.assertEqual(
            [call[1]["params"]["date"] for call in session.calls],
            ["2027-01-31", "2027-02-01", "2027-02-02"],
        )

    def test_nfl_uses_live_and_today_queries(self) -> None:
        midday = int(datetime(2027, 2, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        session = FakeSession(responses=[
            FakeResponse(url=live.API_NFL_URL, payload=api_payload()),
            FakeResponse(url=live.API_NFL_URL, payload=api_payload()),
        ])
        live.fetch_nfl_payloads(
            session,
            api_sports_key="not-published",
            now_epoch_seconds=midday,
            starting_soon_minutes=90,
        )
        self.assertEqual([call[1]["params"] for call in session.calls], [
            {"live": "all", "timezone": "UTC"},
            {"date": "2027-02-01", "timezone": "UTC"},
        ])

    def test_f1_queries_current_season_and_mma_queries_yesterday_today(self) -> None:
        midday = int(datetime(2027, 2, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        f1_session = FakeSession(responses=[
            FakeResponse(url=live.API_F1_URL, payload=api_payload()),
        ])
        live.fetch_f1_payloads(
            f1_session,
            api_sports_key="not-published",
            now_epoch_seconds=midday,
            starting_soon_minutes=90,
        )
        self.assertEqual(f1_session.calls[0][1]["params"], {
            "season": "2027",
            "timezone": "UTC",
        })

        mma_session = FakeSession(responses=[
            FakeResponse(url=live.API_MMA_URL, payload=api_payload()),
            FakeResponse(url=live.API_MMA_URL, payload=api_payload()),
        ])
        live.fetch_mma_payloads(
            mma_session,
            api_sports_key="not-published",
            now_epoch_seconds=midday,
            starting_soon_minutes=90,
        )
        self.assertEqual(
            [call[1]["params"]["date"] for call in mma_session.calls],
            ["2027-01-31", "2027-02-01"],
        )

    def test_workflow_builds_and_publishes_both_feeds_with_one_api_sports_secret(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "live-events.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("API_SPORTS_KEY: ${{ secrets.API_FOOTBALL_KEY }}", workflow)
        self.assertIn("--output-v2 .build/live-events-v2.json", workflow)
        self.assertIn("cp .build/live-events-v2.json", workflow)
        self.assertIn("timeout-minutes: 15", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("pull_request:", workflow)


if __name__ == "__main__":
    unittest.main()
