#!/usr/bin/env python3
"""Build the small, provider-neutral live sports feeds used by SKY TV clients.

``live-events.json`` remains schema v1 for already-deployed clients and contains
only CricketData and API-Football head-to-head events. ``live-events-v2.json``
adds the other API-Sports products and event kinds without exposing provider
payloads or API keys.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse


SCHEMA_VERSION = 1
SCHEMA_VERSION_V2 = 2
DEFAULT_STARTING_SOON_MINUTES = 90
DEFAULT_FEED_LIFETIME_SECONDS = 2 * 60 * 60
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_EVENTS_PER_PROVIDER = 2_000
MAX_PUBLISHED_EVENTS = 2_000
MAX_PUBLISHED_BYTES = 512 * 1024
MAX_ALIASES = 16

CRICKETDATA_URL = "https://api.cricapi.com/v1/cricScore"
API_FOOTBALL_URL = "https://v3.football.api-sports.io/fixtures"
CRICKETDATA_HOST = "api.cricapi.com"
API_FOOTBALL_HOST = "v3.football.api-sports.io"


@dataclass(frozen=True)
class TeamProviderSpec:
    provider: str
    id_prefix: str
    sport: str
    url: str
    host: str
    live_states: frozenset[str]
    nba_shape: bool = False


COMMON_BREAK_STATES = frozenset({"HT", "BT", "OT", "LIVE"})
TEAM_PROVIDER_SPECS: tuple[TeamProviderSpec, ...] = (
    TeamProviderSpec(
        "API_AFL", "api-afl", "AUSTRALIAN_FOOTBALL",
        "https://v1.afl.api-sports.io/games", "v1.afl.api-sports.io",
        frozenset({"Q1", "Q2", "Q3", "Q4"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_BASEBALL", "api-baseball", "BASEBALL",
        "https://v1.baseball.api-sports.io/games", "v1.baseball.api-sports.io",
        frozenset({"IN", "LIVE"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_BASKETBALL", "api-basketball", "BASKETBALL",
        "https://v1.basketball.api-sports.io/games", "v1.basketball.api-sports.io",
        frozenset({"Q1", "Q2", "Q3", "Q4"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_HANDBALL", "api-handball", "HANDBALL",
        "https://v1.handball.api-sports.io/games", "v1.handball.api-sports.io",
        frozenset({"1H", "2H", "ET", "P"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_HOCKEY", "api-hockey", "HOCKEY",
        "https://v1.hockey.api-sports.io/games", "v1.hockey.api-sports.io",
        frozenset({"P1", "P2", "P3", "PT"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_NBA", "api-nba", "BASKETBALL",
        "https://v2.nba.api-sports.io/games", "v2.nba.api-sports.io",
        frozenset({"2", "Q1", "Q2", "Q3", "Q4"}) | COMMON_BREAK_STATES,
        nba_shape=True,
    ),
    TeamProviderSpec(
        "API_RUGBY", "api-rugby", "RUGBY",
        "https://v1.rugby.api-sports.io/games", "v1.rugby.api-sports.io",
        frozenset({"1H", "2H", "ET", "P"}) | COMMON_BREAK_STATES,
    ),
    TeamProviderSpec(
        "API_VOLLEYBALL", "api-volleyball", "VOLLEYBALL",
        "https://v1.volleyball.api-sports.io/games", "v1.volleyball.api-sports.io",
        frozenset({"S1", "S2", "S3", "S4", "S5"}) | COMMON_BREAK_STATES,
    ),
)

API_NFL_URL = "https://v1.american-football.api-sports.io/games"
API_NFL_HOST = "v1.american-football.api-sports.io"
API_F1_URL = "https://v1.formula-1.api-sports.io/races"
API_F1_HOST = "v1.formula-1.api-sports.io"
API_MMA_URL = "https://v1.mma.api-sports.io/fights"
API_MMA_HOST = "v1.mma.api-sports.io"

FOOTBALL_LIVE_STATES = frozenset(
    {"1H", "HT", "2H", "ET", "BT", "P", "INT", "LIVE"}
)
NFL_LIVE_STATES = frozenset({"Q1", "Q2", "Q3", "Q4", "HT", "OT", "LIVE"})
MMA_LIVE_STATES = frozenset({"IN", "PF", "LIVE", "EOR", "WO"})
SCHEDULED_STATES = frozenset({"NS", "TBD", "SCHEDULED", "1"})

WHITESPACE_RE = re.compile(r"\s+")
BRACKET_CODE_RE = re.compile(r"\s*\[([A-Za-z0-9][A-Za-z0-9._ -]{0,10})\]\s*$")
SAFE_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,119}")
BASEBALL_INNING_RE = re.compile(r"IN(?:[1-9]|[1-4][0-9])")
IDENTITY_TOKEN_RE = re.compile(r"[^A-Z0-9]+")


class FeedBuildError(RuntimeError):
    """A safe, already-redacted live-feed build error."""


def clean_text(value: object, *, maximum: int = 180) -> str:
    text = WHITESPACE_RE.sub(" ", str(value or "")).strip()
    return text[:maximum]


def safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def parse_utc_timestamp(value: object) -> int | None:
    numeric = safe_int(value)
    if numeric is not None and numeric > 100_000_000:
        return numeric
    text = clean_text(value, maximum=60)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp())


def parse_cricket_utc(value: object) -> int | None:
    """Parse CricketData's timezone-less dateTimeGMT as explicit UTC."""
    return parse_utc_timestamp(value)


def stable_event_id(provider: str, supplied_id: object, parts: Iterable[object]) -> str:
    raw_id = clean_text(supplied_id, maximum=120)
    if not raw_id:
        seed = "|".join(clean_text(part, maximum=200).casefold() for part in parts)
        raw_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"{provider}:{raw_id}"


def unique_aliases(name: str, aliases: Iterable[object]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in (name, *aliases):
        alias = clean_text(value, maximum=120)
        key = alias.casefold()
        if not alias or key in seen:
            continue
        seen.add(key)
        output.append(alias)
        if len(output) == MAX_ALIASES:
            break
    return output


def identity(name: object, aliases: Iterable[object] = ()) -> dict[str, Any] | None:
    cleaned = clean_text(name, maximum=120)
    if not cleaned:
        return None
    return {"name": cleaned, "aliases": unique_aliases(cleaned, aliases)}


def participant(name: object, aliases: Iterable[object] = ()) -> dict[str, Any] | None:
    original = clean_text(name, maximum=120)
    if not original:
        return None
    match = BRACKET_CODE_RE.search(original)
    if match is None:
        return identity(original, aliases)
    canonical = clean_text(original[: match.start()], maximum=120)
    code = clean_text(match.group(1), maximum=12).upper()
    if not canonical:
        return identity(original, aliases)
    return identity(canonical, (code, original, *aliases))


def cricket_competition(match_type: object) -> str:
    normalized = clean_text(match_type, maximum=40).casefold()
    return {
        "test": "Test Cricket", "odi": "One Day Cricket",
        "t20": "T20 Cricket", "t10": "T10 Cricket",
    }.get(normalized, "Cricket")


def normalize_cricket_events(
    payload: Mapping[str, Any], *, now_epoch_seconds: int,
    starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    status = clean_text(payload.get("status"), maximum=30).casefold()
    if status and status != "success":
        raise FeedBuildError("CricketData returned an unsuccessful response.")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FeedBuildError("CricketData returned an invalid event list.")
    if len(rows) > MAX_EVENTS_PER_PROVIDER:
        raise FeedBuildError("CricketData returned too many events.")

    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider_state = clean_text(row.get("ms"), maximum=20).casefold()
        start = parse_cricket_utc(row.get("dateTimeGMT"))
        if provider_state == "live":
            state = "LIVE"
        elif (
            provider_state == "fixture" and start is not None
            and now_epoch_seconds <= start <= now_epoch_seconds + starting_soon_seconds
        ):
            state = "STARTING_SOON"
        else:
            continue

        first = participant(row.get("t1"))
        second = participant(row.get("t2"))
        if first is None or second is None:
            continue
        match_type = clean_text(row.get("matchType"), maximum=40)
        event_id = stable_event_id(
            "cricketdata", row.get("id"),
            (first["name"], second["name"], start, match_type),
        )
        events.append({
            "id": event_id, "provider": "CRICKETDATA",
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": "CRICKET", "competition": cricket_competition(match_type),
            "displayTitle": f"{first['name']} vs {second['name']}",
            "startEpochSeconds": start, "participants": [first, second],
        })
    return events


def api_sports_response_rows(
    payload: Mapping[str, Any], provider_name: str,
) -> list[Any]:
    if payload.get("errors") not in (None, [], {}):
        raise FeedBuildError(f"{provider_name} returned an unsuccessful response.")
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise FeedBuildError(f"{provider_name} returned an invalid event list.")
    if len(rows) > MAX_EVENTS_PER_PROVIDER:
        raise FeedBuildError(f"{provider_name} returned too many events.")
    return rows


def football_response_rows(payload: Mapping[str, Any]) -> list[Any]:
    return api_sports_response_rows(payload, "API-Football")


def normalize_football_events(
    payload: Mapping[str, Any], *, now_epoch_seconds: int,
    starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in football_response_rows(payload):
        if not isinstance(row, Mapping):
            continue
        fixture = row.get("fixture")
        teams = row.get("teams")
        league = row.get("league")
        if not isinstance(fixture, Mapping) or not isinstance(teams, Mapping):
            continue
        status = fixture.get("status")
        short_status = (
            clean_text(status.get("short"), maximum=12).upper()
            if isinstance(status, Mapping) else ""
        )
        start = safe_int(fixture.get("timestamp"))
        if short_status in FOOTBALL_LIVE_STATES:
            state = "LIVE"
        elif (
            short_status == "NS" and start is not None
            and now_epoch_seconds <= start <= now_epoch_seconds + starting_soon_seconds
        ):
            state = "STARTING_SOON"
        else:
            continue
        home_row = teams.get("home")
        away_row = teams.get("away")
        if not isinstance(home_row, Mapping) or not isinstance(away_row, Mapping):
            continue
        home = participant(home_row.get("name"), (home_row.get("code"),))
        away = participant(away_row.get("name"), (away_row.get("code"),))
        if home is None or away is None:
            continue
        competition = "Football"
        if isinstance(league, Mapping):
            competition = clean_text(league.get("name"), maximum=100) or competition
        event_id = stable_event_id(
            "api-football", fixture.get("id"),
            (home["name"], away["name"], start, competition),
        )
        events.append({
            "id": event_id, "provider": "API_FOOTBALL",
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": "FOOTBALL", "competition": competition,
            "displayTitle": f"{home['name']} vs {away['name']}",
            "startEpochSeconds": start, "participants": [home, away],
        })
    return events


def row_event_id(row: Mapping[str, Any]) -> object:
    if row.get("id") not in (None, ""):
        return row.get("id")
    for container_name in ("game", "fixture"):
        container = row.get(container_name)
        if isinstance(container, Mapping) and container.get("id") not in (None, ""):
            return container.get("id")
    return ""


def merge_api_sports_payloads(
    payloads: Iterable[Mapping[str, Any]], provider_name: str,
) -> Mapping[str, Any]:
    combined: list[Any] = []
    seen_ids: set[str] = set()
    for payload in payloads:
        for row in api_sports_response_rows(payload, provider_name):
            supplied_id = row_event_id(row) if isinstance(row, Mapping) else ""
            normalized_id = clean_text(supplied_id, maximum=120)
            if normalized_id and normalized_id in seen_ids:
                continue
            if normalized_id:
                seen_ids.add(normalized_id)
            combined.append(row)
            if len(combined) > MAX_EVENTS_PER_PROVIDER:
                raise FeedBuildError(f"{provider_name} returned too many events.")
    return {"errors": [], "results": len(combined), "response": combined}


def merge_football_payloads(
    payloads: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    return merge_api_sports_payloads(payloads, "API-Football")


def nested_mapping(row: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = row.get(key)
    return value if isinstance(value, Mapping) else None


def row_start_epoch(row: Mapping[str, Any]) -> int | None:
    for container in (row, nested_mapping(row, "game"), nested_mapping(row, "fixture")):
        if container is None:
            continue
        timestamp = safe_int(container.get("timestamp"))
        if timestamp is not None and timestamp > 100_000_000:
            return timestamp
        date_value = container.get("date")
        if isinstance(date_value, Mapping):
            for key in ("timestamp", "start"):
                parsed = parse_utc_timestamp(date_value.get(key))
                if parsed is not None:
                    return parsed
        else:
            parsed = parse_utc_timestamp(date_value)
            if parsed is not None:
                return parsed
    return None


def row_status(row: Mapping[str, Any]) -> tuple[str, str]:
    for container in (row, nested_mapping(row, "game"), nested_mapping(row, "fixture")):
        if container is None:
            continue
        status = container.get("status")
        if isinstance(status, Mapping):
            short = clean_text(status.get("short"), maximum=20).upper()
            long = clean_text(status.get("long"), maximum=40).upper()
            if short or long:
                return short, long
        elif status not in (None, ""):
            scalar = clean_text(status, maximum=40).upper()
            return scalar, scalar
    return "", ""


def event_state(
    short_status: str, long_status: str, *, live_states: frozenset[str],
    start_epoch_seconds: int | None, now_epoch_seconds: int,
    starting_soon_seconds: int, allow_baseball_inning: bool = False,
) -> str | None:
    if (
        short_status in live_states
        or long_status in {"LIVE", "IN PLAY", "IN PROGRESS"}
        or (allow_baseball_inning and BASEBALL_INNING_RE.fullmatch(short_status))
    ):
        return "LIVE"
    is_scheduled = short_status in SCHEDULED_STATES or long_status in {
        "SCHEDULED", "NOT STARTED", "TIME TO BE DEFINED",
    }
    if (
        is_scheduled and start_epoch_seconds is not None
        and now_epoch_seconds <= start_epoch_seconds <= now_epoch_seconds + starting_soon_seconds
    ):
        return "STARTING_SOON"
    return None


def team_row(row: Mapping[str, Any], *, nba_shape: bool) -> tuple[Any, Any]:
    teams = nested_mapping(row, "teams")
    if teams is None:
        game = nested_mapping(row, "game")
        teams = nested_mapping(game, "teams") if game is not None else None
    if teams is None:
        return None, None
    return teams.get("home"), teams.get("visitors" if nba_shape else "away")


def participant_from_team(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return participant(
            value.get("name"),
            (value.get("code"), value.get("short"), value.get("abbreviation")),
        )
    return participant(value)


def row_competition(row: Mapping[str, Any], default: str) -> str:
    league = row.get("league")
    if isinstance(league, Mapping):
        return clean_text(league.get("name"), maximum=180) or default
    scalar = clean_text(league, maximum=180)
    if scalar and scalar.casefold() != "standard":
        return scalar
    return default


def v2_head_to_head(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": event["id"], "provider": event["provider"],
        "providerEventId": event["providerEventId"], "state": event["state"],
        "sport": event["sport"], "kind": "HEAD_TO_HEAD",
        "competition": event["competition"], "displayTitle": event["displayTitle"],
        "startEpochSeconds": event.get("startEpochSeconds"), "endEpochSeconds": None,
        "participants": event["participants"], "eventIdentity": None,
        "sessionIdentity": None,
    }


def normalize_team_events(
    payloads: Iterable[Mapping[str, Any]], spec: TeamProviderSpec, *,
    now_epoch_seconds: int, starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    provider_label = spec.provider.replace("_", "-")
    merged = merge_api_sports_payloads(payloads, provider_label)
    events: list[dict[str, Any]] = []
    for row in api_sports_response_rows(merged, provider_label):
        if not isinstance(row, Mapping):
            continue
        start = row_start_epoch(row)
        short_status, long_status = row_status(row)
        state = event_state(
            short_status, long_status, live_states=spec.live_states,
            start_epoch_seconds=start, now_epoch_seconds=now_epoch_seconds,
            starting_soon_seconds=starting_soon_seconds,
            allow_baseball_inning=spec.sport == "BASEBALL",
        )
        if state is None:
            continue
        home_raw, away_raw = team_row(row, nba_shape=spec.nba_shape)
        home = participant_from_team(home_raw)
        away = participant_from_team(away_raw)
        if home is None or away is None:
            continue
        default_competition = (
            "NBA" if spec.provider == "API_NBA"
            else spec.sport.replace("_", " ").title()
        )
        competition = row_competition(row, default_competition)
        event_id = stable_event_id(
            spec.id_prefix, row_event_id(row),
            (home["name"], away["name"], start, competition),
        )
        events.append(v2_head_to_head({
            "id": event_id, "provider": spec.provider,
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": spec.sport, "competition": competition,
            "displayTitle": f"{home['name']} vs {away['name']}",
            "startEpochSeconds": start, "participants": [home, away],
        }))
    return events


def nfl_spec() -> TeamProviderSpec:
    return TeamProviderSpec(
        "API_NFL", "api-nfl", "AMERICAN_FOOTBALL", API_NFL_URL,
        API_NFL_HOST, NFL_LIVE_STATES,
    )


def normalize_f1_events(
    payloads: Iterable[Mapping[str, Any]], *, now_epoch_seconds: int,
    starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    merged = merge_api_sports_payloads(payloads, "API-Formula-1")
    events: list[dict[str, Any]] = []
    for row in api_sports_response_rows(merged, "API-Formula-1"):
        if not isinstance(row, Mapping):
            continue
        start = row_start_epoch(row)
        short_status, long_status = row_status(row)
        state = event_state(
            short_status, long_status, live_states=frozenset({"LIVE"}),
            start_epoch_seconds=start, now_epoch_seconds=now_epoch_seconds,
            starting_soon_seconds=starting_soon_seconds,
        )
        if state is None:
            continue
        competition_row = nested_mapping(row, "competition")
        circuit_row = nested_mapping(row, "circuit")
        event_name = ""
        event_aliases: list[object] = []
        location_values: list[object] = []
        if competition_row is not None:
            competition_name = clean_text(competition_row.get("name"), maximum=120)
            if normalized_identity_key(competition_name) not in {"F1", "FORMULA 1"}:
                event_name = competition_name
            location = nested_mapping(competition_row, "location")
            if location is not None:
                location_values.extend((location.get("country"), location.get("city")))
        if circuit_row is not None:
            circuit_name = clean_text(circuit_row.get("name"), maximum=120)
            if not event_name:
                event_name = circuit_name
            elif circuit_name:
                event_aliases.append(circuit_name)
        if not event_name:
            event_name = next(
                (clean_text(value, maximum=120) for value in location_values if clean_text(value, maximum=120)),
                "",
            )
        event_aliases.extend(location_values)
        if not event_name:
            continue
        session_name = clean_text(row.get("type"), maximum=120) or "Race"
        session_aliases: list[object] = []
        practice_number = re.search(r"([123])", session_name)
        if "practice" in session_name.casefold() and practice_number:
            number = practice_number.group(1)
            session_aliases.extend((f"Practice {number}", f"FP{number}"))
        event_id = stable_event_id(
            "api-formula-1", row_event_id(row), (event_name, session_name, start),
        )
        event_identity = identity(event_name, event_aliases)
        session_identity = identity(session_name, session_aliases)
        if event_identity is None or session_identity is None:
            continue
        events.append({
            "id": event_id, "provider": "API_FORMULA_1",
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": "MOTORSPORT", "kind": "SESSION", "competition": "Formula 1",
            "displayTitle": f"{event_name} — {session_name}",
            "startEpochSeconds": start, "endEpochSeconds": None, "participants": [],
            "eventIdentity": event_identity, "sessionIdentity": session_identity,
        })
    return events


def truthy_provider_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return clean_text(value, maximum=10).casefold() in {"1", "true", "yes"}


def safe_card_slug(value: object) -> str:
    slug = clean_text(value, maximum=120)
    if SAFE_SLUG_RE.fullmatch(slug) is None:
        return ""
    if len([piece for piece in re.split(r"[-_]", slug) if piece]) < 2:
        return ""
    return slug


def slug_display_name(slug: str) -> str:
    tokens = [token for token in re.split(r"[-_]", slug) if token]
    output = " ".join(
        token.upper() if token.casefold() in {"ufc", "mma", "pfl"} else token.title()
        for token in tokens
    )
    return clean_text(output, maximum=120)


def mma_fighters(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    fighters = nested_mapping(row, "fighters")
    if fighters is None:
        return None, None
    return participant_from_team(fighters.get("first")), participant_from_team(fighters.get("second"))


def normalize_mma_events(
    payloads: Iterable[Mapping[str, Any]], *, now_epoch_seconds: int,
    starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    merged = merge_api_sports_payloads(payloads, "API-MMA")
    active_rows: list[tuple[Mapping[str, Any], str, int | None]] = []
    for row in api_sports_response_rows(merged, "API-MMA"):
        if not isinstance(row, Mapping):
            continue
        start = row_start_epoch(row)
        short_status, long_status = row_status(row)
        state = event_state(
            short_status, long_status, live_states=MMA_LIVE_STATES,
            start_epoch_seconds=start, now_epoch_seconds=now_epoch_seconds,
            starting_soon_seconds=starting_soon_seconds,
        )
        if state is not None:
            active_rows.append((row, state, start))

    groups: dict[str, list[tuple[Mapping[str, Any], str, int | None]]] = {}
    ungrouped: list[tuple[Mapping[str, Any], str, int | None]] = []
    for item in active_rows:
        slug = safe_card_slug(item[0].get("slug"))
        (groups.setdefault(slug, []).append(item) if slug else ungrouped.append(item))

    events: list[dict[str, Any]] = []
    for slug, items in groups.items():
        main = next((item for item in items if truthy_provider_value(item[0].get("is_main"))), None)
        participants: list[dict[str, Any]] = []
        if main is not None:
            first, second = mma_fighters(main[0])
            if first is not None and second is not None:
                participants = [first, second]
        state = "LIVE" if any(item[1] == "LIVE" for item in items) else "STARTING_SOON"
        starts = [item[2] for item in items if item[2] is not None]
        # A live card can have a future main event while an undercard is already
        # underway.  Publishing the main-bout time would make the whole live
        # event look future-dated to strict clients.
        start = min(starts) if starts else None
        category = clean_text(items[0][0].get("category"), maximum=180) or "MMA"
        card_name = slug_display_name(slug)
        event_id = stable_event_id("api-mma-card", slug, (card_name, start))
        card_identity = identity(card_name, (slug,))
        if card_identity is None:
            continue
        events.append({
            "id": event_id, "provider": "API_MMA",
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": "COMBAT", "kind": "CARD", "competition": category,
            "displayTitle": card_name, "startEpochSeconds": start,
            "endEpochSeconds": None, "participants": participants,
            "eventIdentity": card_identity, "sessionIdentity": None,
        })

    for row, state, start in ungrouped:
        first, second = mma_fighters(row)
        if first is None or second is None:
            continue
        competition = clean_text(row.get("category"), maximum=180) or "MMA"
        event_id = stable_event_id(
            "api-mma", row_event_id(row),
            (first["name"], second["name"], start, competition),
        )
        events.append(v2_head_to_head({
            "id": event_id, "provider": "API_MMA",
            "providerEventId": event_id.split(":", 1)[1], "state": state,
            "sport": "COMBAT", "competition": competition,
            "displayTitle": f"{first['name']} vs {second['name']}",
            "startEpochSeconds": start, "participants": [first, second],
        }))
    return events


def v1_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": event["id"], "provider": event["provider"],
        "providerEventId": event["providerEventId"], "state": event["state"],
        "sport": event["sport"], "competition": event["competition"],
        "displayTitle": event["displayTitle"],
        "startEpochSeconds": event.get("startEpochSeconds"),
        "participants": event["participants"],
    }


def normalized_identity_key(value: object) -> str:
    return IDENTITY_TOKEN_RE.sub(" ", clean_text(value, maximum=180).upper()).strip()


def semantic_event_key(event: Mapping[str, Any]) -> tuple[object, ...]:
    kind = clean_text(event.get("kind"), maximum=30)
    if not kind and len(event.get("participants", [])) == 2:
        kind = "HEAD_TO_HEAD"
    sport = clean_text(event.get("sport"), maximum=40)
    start = safe_int(event.get("startEpochSeconds"))
    if kind == "HEAD_TO_HEAD":
        names = sorted(
            normalized_identity_key(item.get("name"))
            for item in event.get("participants", []) if isinstance(item, Mapping)
        )
        return kind, sport, tuple(names), start
    event_value = event.get("eventIdentity")
    event_name = event_value.get("name") if isinstance(event_value, Mapping) else ""
    session_value = event.get("sessionIdentity")
    session_name = session_value.get("name") if isinstance(session_value, Mapping) else ""
    return (
        kind, sport, normalized_identity_key(event_name),
        normalized_identity_key(session_name), start,
    )


def event_sort_key(event: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        0 if event.get("state") == "LIVE" else 1,
        event.get("startEpochSeconds")
        if event.get("startEpochSeconds") is not None else 2**63 - 1,
        str(event.get("displayTitle", "")).casefold(), str(event.get("id", "")),
    )


def deduplicate_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        by_id.setdefault(str(event["id"]), event)
    preferred = sorted(
        by_id.values(),
        key=lambda event: (
            0 if event.get("provider") == "API_NBA" else 1,
            event_sort_key(event),
        ),
    )
    by_semantic_key: dict[tuple[object, ...], dict[str, Any]] = {}
    for event in preferred:
        by_semantic_key.setdefault(semantic_event_key(event), event)
    return sorted(by_semantic_key.values(), key=event_sort_key)


def encoded_feed_bytes(feed: Mapping[str, Any]) -> bytes:
    return (json.dumps(feed, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def fit_feed_limits(feed: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(feed)
    events = list(feed.get("events", []))[:MAX_PUBLISHED_EVENTS]
    output["events"] = events
    if len(encoded_feed_bytes(output)) <= MAX_PUBLISHED_BYTES:
        return output
    low, high, best = 0, len(events), 0
    while low <= high:
        middle = (low + high) // 2
        output["events"] = events[:middle]
        if len(encoded_feed_bytes(output)) <= MAX_PUBLISHED_BYTES:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    output["events"] = events[:best]
    if len(encoded_feed_bytes(output)) > MAX_PUBLISHED_BYTES:
        raise FeedBuildError("The live event feed metadata was too large.")
    return output


def make_feed(
    *, schema_version: int, providers: Sequence[str],
    events: Iterable[dict[str, Any]], now_epoch_seconds: int,
    starting_soon_minutes: int, lifetime_seconds: int,
) -> dict[str, Any]:
    if not 1 <= starting_soon_minutes <= 360:
        raise FeedBuildError("The starting-soon window must be between 1 and 360 minutes.")
    if not 300 <= lifetime_seconds <= 6 * 60 * 60:
        raise FeedBuildError("The feed lifetime must be between 5 minutes and 6 hours.")
    if not providers:
        raise FeedBuildError("No live-event provider succeeded.")
    return fit_feed_limits({
        "schemaVersion": schema_version, "generatedAtEpochSeconds": now_epoch_seconds,
        "expiresAtEpochSeconds": now_epoch_seconds + lifetime_seconds,
        "startingSoonMinutes": starting_soon_minutes, "providers": list(providers),
        "events": deduplicate_events(events),
    })


def build_feed(
    cricket_payload: Mapping[str, Any], football_payload: Mapping[str, Any], *,
    now_epoch_seconds: int,
    starting_soon_minutes: int = DEFAULT_STARTING_SOON_MINUTES,
    lifetime_seconds: int = DEFAULT_FEED_LIFETIME_SECONDS,
) -> dict[str, Any]:
    soon_seconds = starting_soon_minutes * 60
    events = normalize_cricket_events(
        cricket_payload, now_epoch_seconds=now_epoch_seconds,
        starting_soon_seconds=soon_seconds,
    )
    events.extend(normalize_football_events(
        football_payload, now_epoch_seconds=now_epoch_seconds,
        starting_soon_seconds=soon_seconds,
    ))
    return make_feed(
        schema_version=SCHEMA_VERSION, providers=("CRICKETDATA", "API_FOOTBALL"),
        events=events, now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=starting_soon_minutes, lifetime_seconds=lifetime_seconds,
    )


def utc_date_range(now_epoch_seconds: int, starting_soon_minutes: int) -> tuple[date, date]:
    start = datetime.fromtimestamp(now_epoch_seconds, tz=timezone.utc).date()
    end = datetime.fromtimestamp(
        now_epoch_seconds + starting_soon_minutes * 60, tz=timezone.utc,
    ).date()
    return start, end


def date_query_days(
    now_epoch_seconds: int, starting_soon_minutes: int, *, include_yesterday: bool,
) -> list[date]:
    today, window_end = utc_date_range(now_epoch_seconds, starting_soon_minutes)
    days = [today - timedelta(days=1), today] if include_yesterday else [today]
    if window_end != today:
        days.append(window_end)
    return days


def fetch_json(
    session: Any, *, provider_name: str, url: str, allowed_host: str,
    params: Mapping[str, str], headers: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    try:
        response = session.get(
            url, params=dict(params), headers=dict(headers or {}), timeout=(10, 25),
            allow_redirects=True,
        )
    except Exception:
        raise FeedBuildError(f"{provider_name} request failed.") from None
    final_host = (urlparse(str(getattr(response, "url", ""))).hostname or "").casefold()
    if final_host != allowed_host.casefold():
        raise FeedBuildError(f"{provider_name} redirected to an unexpected host.")
    status_code = safe_int(getattr(response, "status_code", None))
    if status_code is None or not 200 <= status_code < 300:
        safe_status = status_code if status_code is not None else "unknown"
        raise FeedBuildError(f"{provider_name} returned HTTP {safe_status}.")
    content = bytes(getattr(response, "content", b""))
    if not content:
        raise FeedBuildError(f"{provider_name} returned an empty response.")
    if len(content) > MAX_RESPONSE_BYTES:
        raise FeedBuildError(f"{provider_name} response was too large.")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FeedBuildError(f"{provider_name} returned invalid JSON.") from None
    if not isinstance(payload, Mapping):
        raise FeedBuildError(f"{provider_name} returned an invalid JSON object.")
    return payload


def fetch_api_football_payload(
    session: Any, *, api_football_key: str, now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> Mapping[str, Any]:
    days = date_query_days(
        now_epoch_seconds, starting_soon_minutes, include_yesterday=False,
    )
    headers = {"x-apisports-key": api_football_key}
    payloads = [fetch_json(
        session, provider_name="API-Football", url=API_FOOTBALL_URL,
        allowed_host=API_FOOTBALL_HOST, params={"live": "all"}, headers=headers,
    )]
    football_response_rows(payloads[0])
    for fixture_date in days:
        payload = fetch_json(
            session, provider_name="API-Football", url=API_FOOTBALL_URL,
            allowed_host=API_FOOTBALL_HOST,
            params={"date": fixture_date.isoformat(), "timezone": "UTC"},
            headers=headers,
        )
        football_response_rows(payload)
        payloads.append(payload)
    return merge_football_payloads(payloads)


def fetch_team_payloads(
    session: Any, *, spec: TeamProviderSpec, api_sports_key: str,
    now_epoch_seconds: int, starting_soon_minutes: int,
) -> list[Mapping[str, Any]]:
    headers = {"x-apisports-key": api_sports_key}
    payloads: list[Mapping[str, Any]] = []
    for fixture_date in date_query_days(
        now_epoch_seconds, starting_soon_minutes, include_yesterday=True,
    ):
        params = {"date": fixture_date.isoformat()}
        if not spec.nba_shape:
            params["timezone"] = "UTC"
        payload = fetch_json(
            session, provider_name=spec.provider.replace("_", "-"), url=spec.url,
            allowed_host=spec.host, params=params, headers=headers,
        )
        api_sports_response_rows(payload, spec.provider.replace("_", "-"))
        payloads.append(payload)
    return payloads


def fetch_nfl_payloads(
    session: Any, *, api_sports_key: str, now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> list[Mapping[str, Any]]:
    spec = nfl_spec()
    headers = {"x-apisports-key": api_sports_key}
    queries: list[dict[str, str]] = [{"live": "all", "timezone": "UTC"}]
    queries.extend(
        {"date": day.isoformat(), "timezone": "UTC"}
        for day in date_query_days(
            now_epoch_seconds, starting_soon_minutes, include_yesterday=False,
        )
    )
    payloads = []
    for params in queries:
        payload = fetch_json(
            session, provider_name="API-NFL", url=spec.url,
            allowed_host=spec.host, params=params, headers=headers,
        )
        api_sports_response_rows(payload, "API-NFL")
        payloads.append(payload)
    return payloads


def fetch_f1_payloads(
    session: Any, *, api_sports_key: str, now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> list[Mapping[str, Any]]:
    start = datetime.fromtimestamp(now_epoch_seconds, tz=timezone.utc)
    end = datetime.fromtimestamp(
        now_epoch_seconds + starting_soon_minutes * 60, tz=timezone.utc,
    )
    years = [start.year] + ([end.year] if end.year != start.year else [])
    payloads = []
    for year in years:
        payload = fetch_json(
            session, provider_name="API-Formula-1", url=API_F1_URL,
            allowed_host=API_F1_HOST,
            params={"season": str(year), "timezone": "UTC"},
            headers={"x-apisports-key": api_sports_key},
        )
        api_sports_response_rows(payload, "API-Formula-1")
        payloads.append(payload)
    return payloads


def fetch_mma_payloads(
    session: Any, *, api_sports_key: str, now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> list[Mapping[str, Any]]:
    payloads = []
    for fixture_date in date_query_days(
        now_epoch_seconds, starting_soon_minutes, include_yesterday=True,
    ):
        payload = fetch_json(
            session, provider_name="API-MMA", url=API_MMA_URL,
            allowed_host=API_MMA_HOST,
            params={"date": fixture_date.isoformat(), "timezone": "UTC"},
            headers={"x-apisports-key": api_sports_key},
        )
        api_sports_response_rows(payload, "API-MMA")
        payloads.append(payload)
    return payloads


ProviderLoader = Callable[[], list[dict[str, Any]]]


def collect_provider_events(
    loaders: Mapping[str, ProviderLoader],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    successes: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    for provider, loader in loaders.items():
        try:
            successes[provider] = loader()
        except FeedBuildError as exc:
            failures[provider] = str(exc)
    if not ({"CRICKETDATA", "API_FOOTBALL"} & successes.keys()):
        raise FeedBuildError("Neither core live-event provider succeeded.")
    if not successes:
        raise FeedBuildError("No live-event provider succeeded.")
    return successes, failures


def load_provider_events(
    session: Any, *, cricketdata_api_key: str, api_sports_key: str,
    now_epoch_seconds: int, starting_soon_minutes: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    soon_seconds = starting_soon_minutes * 60

    def cricket_loader() -> list[dict[str, Any]]:
        payload = fetch_json(
            session, provider_name="CricketData", url=CRICKETDATA_URL,
            allowed_host=CRICKETDATA_HOST, params={"apikey": cricketdata_api_key},
        )
        return [
            v2_head_to_head(event)
            for event in normalize_cricket_events(
                payload, now_epoch_seconds=now_epoch_seconds,
                starting_soon_seconds=soon_seconds,
            )
        ]

    def football_loader() -> list[dict[str, Any]]:
        payload = fetch_api_football_payload(
            session, api_football_key=api_sports_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        )
        return [
            v2_head_to_head(event)
            for event in normalize_football_events(
                payload, now_epoch_seconds=now_epoch_seconds,
                starting_soon_seconds=soon_seconds,
            )
        ]

    loaders: dict[str, ProviderLoader] = {
        "CRICKETDATA": cricket_loader, "API_FOOTBALL": football_loader,
    }
    for spec in TEAM_PROVIDER_SPECS:
        loaders[spec.provider] = lambda spec=spec: normalize_team_events(
            fetch_team_payloads(
                session, spec=spec, api_sports_key=api_sports_key,
                now_epoch_seconds=now_epoch_seconds,
                starting_soon_minutes=starting_soon_minutes,
            ),
            spec, now_epoch_seconds=now_epoch_seconds,
            starting_soon_seconds=soon_seconds,
        )
    nfl = nfl_spec()
    loaders["API_NFL"] = lambda: normalize_team_events(
        fetch_nfl_payloads(
            session, api_sports_key=api_sports_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        ),
        nfl, now_epoch_seconds=now_epoch_seconds,
        starting_soon_seconds=soon_seconds,
    )
    loaders["API_FORMULA_1"] = lambda: normalize_f1_events(
        fetch_f1_payloads(
            session, api_sports_key=api_sports_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        ),
        now_epoch_seconds=now_epoch_seconds, starting_soon_seconds=soon_seconds,
    )
    loaders["API_MMA"] = lambda: normalize_mma_events(
        fetch_mma_payloads(
            session, api_sports_key=api_sports_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        ),
        now_epoch_seconds=now_epoch_seconds, starting_soon_seconds=soon_seconds,
    )
    return collect_provider_events(loaders)


def is_nba_competition(event: Mapping[str, Any]) -> bool:
    competition = normalized_identity_key(event.get("competition"))
    return (
        "NBA" in competition.split()
        or "NATIONAL BASKETBALL ASSOCIATION" in competition
    )


def build_published_feeds(
    provider_events: Mapping[str, list[dict[str, Any]]], *,
    now_epoch_seconds: int,
    starting_soon_minutes: int = DEFAULT_STARTING_SOON_MINUTES,
    lifetime_seconds: int = DEFAULT_FEED_LIFETIME_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    core_order = [
        provider for provider in ("CRICKETDATA", "API_FOOTBALL")
        if provider in provider_events
    ]
    if not core_order:
        raise FeedBuildError("Neither core live-event provider succeeded.")
    all_events: list[dict[str, Any]] = []
    for provider, events in provider_events.items():
        if provider == "API_BASKETBALL" and "API_NBA" in provider_events:
            events = [event for event in events if not is_nba_competition(event)]
        all_events.extend(events)
    v1_events = [
        v1_event(event) for provider in core_order for event in provider_events[provider]
    ]
    v1 = make_feed(
        schema_version=SCHEMA_VERSION, providers=core_order, events=v1_events,
        now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=starting_soon_minutes, lifetime_seconds=lifetime_seconds,
    )
    v2 = make_feed(
        schema_version=SCHEMA_VERSION_V2, providers=list(provider_events),
        events=all_events, now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=starting_soon_minutes, lifetime_seconds=lifetime_seconds,
    )
    return v1, v2


def fetch_provider_payloads(
    *, cricketdata_api_key: str, api_football_key: str,
    now_epoch_seconds: int, starting_soon_minutes: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Legacy two-provider helper retained for local callers and tests."""
    try:
        import requests
    except ImportError:
        raise FeedBuildError(
            "The requests package is required to build the live event feed."
        ) from None
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json", "User-Agent": "SKYTV-Live-Events/2.0",
    })
    try:
        cricket = fetch_json(
            session, provider_name="CricketData", url=CRICKETDATA_URL,
            allowed_host=CRICKETDATA_HOST, params={"apikey": cricketdata_api_key},
        )
        football = fetch_api_football_payload(
            session, api_football_key=api_football_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        )
        return cricket, football
    finally:
        session.close()


def write_feed(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded_feed_bytes(payload))
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".build/live-events.json"))
    parser.add_argument("--output-v2", type=Path, default=Path(".build/live-events-v2.json"))
    parser.add_argument(
        "--starting-soon-minutes", type=int,
        default=DEFAULT_STARTING_SOON_MINUTES,
    )
    parser.add_argument(
        "--now-epoch-seconds", type=int, default=None,
        help="Override current UTC time for a reproducible local build.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cricket_key = os.environ.get("CRICKETDATA_API_KEY", "").strip()
    api_sports_key = (
        os.environ.get("API_SPORTS_KEY", "").strip()
        or os.environ.get("API_FOOTBALL_KEY", "").strip()
    )
    missing = []
    if not cricket_key:
        missing.append("CRICKETDATA_API_KEY")
    if not api_sports_key:
        missing.append("API_SPORTS_KEY")
    if missing:
        raise FeedBuildError("Missing required GitHub secret(s): " + ", ".join(missing))
    now_epoch_seconds = args.now_epoch_seconds
    if now_epoch_seconds is None:
        now_epoch_seconds = int(datetime.now(tz=timezone.utc).timestamp())
    if not 1 <= args.starting_soon_minutes <= 360:
        raise FeedBuildError("The starting-soon window must be between 1 and 360 minutes.")

    try:
        import requests
    except ImportError:
        raise FeedBuildError(
            "The requests package is required to build the live event feed."
        ) from None
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json", "User-Agent": "SKYTV-Live-Events/2.0",
    })
    try:
        provider_events, failures = load_provider_events(
            session, cricketdata_api_key=cricket_key,
            api_sports_key=api_sports_key, now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=args.starting_soon_minutes,
        )
    finally:
        session.close()
    v1, v2 = build_published_feeds(
        provider_events, now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=args.starting_soon_minutes,
    )
    write_feed(args.output, v1)
    write_feed(args.output_v2, v2)
    for provider, error in failures.items():
        print(f"WARNING: {provider} omitted: {error}", file=sys.stderr, flush=True)
    print(
        f"Published {len(v1['events'])} v1 and {len(v2['events'])} v2 "
        "live/starting-soon event(s).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeedBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
