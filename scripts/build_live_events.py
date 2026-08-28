#!/usr/bin/env python3
"""Build the provider-neutral live sports feed consumed by SKY TV clients.

The API keys are used only while this script runs in GitHub Actions.  The
published JSON contains a deliberately small whitelist of event fields and is
safe for Android TV, webOS, Tizen, Vega, and future clients to download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


SCHEMA_VERSION = 1
DEFAULT_STARTING_SOON_MINUTES = 90
DEFAULT_FEED_LIFETIME_SECONDS = 2 * 60 * 60
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_EVENTS_PER_PROVIDER = 2_000

CRICKETDATA_URL = "https://api.cricapi.com/v1/cricScore"
API_FOOTBALL_URL = "https://v3.football.api-sports.io/fixtures"

CRICKETDATA_HOST = "api.cricapi.com"
API_FOOTBALL_HOST = "v3.football.api-sports.io"

FOOTBALL_LIVE_STATES = frozenset(
    {
        "1H",   # First half
        "HT",   # Half-time
        "2H",   # Second half
        "ET",   # Extra time
        "BT",   # Break in extra time
        "P",    # Penalties
        "INT",  # Interrupted, but still an active fixture
        "LIVE",
    }
)

WHITESPACE_RE = re.compile(r"\s+")


class FeedBuildError(RuntimeError):
    """A safe, already-redacted live-feed build error."""


def clean_text(value: object, *, maximum: int = 180) -> str:
    text = WHITESPACE_RE.sub(" ", str(value or "")).strip()
    return text[:maximum]


def safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def parse_cricket_utc(value: object) -> int | None:
    """Parse CricketData's timezone-less dateTimeGMT as explicit UTC."""
    text = clean_text(value, maximum=40)
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None

    if parsed is None:
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp())


def stable_event_id(provider: str, supplied_id: object, parts: Iterable[object]) -> str:
    raw_id = clean_text(supplied_id, maximum=120)
    if not raw_id:
        seed = "|".join(clean_text(part, maximum=200).casefold() for part in parts)
        raw_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"{provider}:{raw_id}"


def participant(name: object) -> dict[str, Any] | None:
    cleaned = clean_text(name, maximum=120)
    if not cleaned:
        return None
    return {
        "name": cleaned,
        "aliases": [cleaned],
    }


def cricket_competition(match_type: object) -> str:
    normalized = clean_text(match_type, maximum=40).casefold()
    return {
        "test": "Test Cricket",
        "odi": "One Day Cricket",
        "t20": "T20 Cricket",
        "t10": "T10 Cricket",
    }.get(normalized, "Cricket")


def normalize_cricket_events(
    payload: Mapping[str, Any],
    *,
    now_epoch_seconds: int,
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
            provider_state == "fixture"
            and start is not None
            and now_epoch_seconds <= start <= now_epoch_seconds + starting_soon_seconds
        ):
            state = "STARTING_SOON"
        else:
            continue

        home = participant(row.get("t1"))
        away = participant(row.get("t2"))
        if home is None or away is None:
            continue

        match_type = clean_text(row.get("matchType"), maximum=40)
        display_title = f"{home['name']} vs {away['name']}"
        event_id = stable_event_id(
            "cricketdata",
            row.get("id"),
            (home["name"], away["name"], start, match_type),
        )
        events.append(
            {
                "id": event_id,
                "provider": "CRICKETDATA",
                "providerEventId": event_id.split(":", 1)[1],
                "state": state,
                "sport": "CRICKET",
                "competition": cricket_competition(match_type),
                "displayTitle": display_title,
                "startEpochSeconds": start,
                "participants": [home, away],
            }
        )
    return events


def normalize_football_events(
    payload: Mapping[str, Any],
    *,
    now_epoch_seconds: int,
    starting_soon_seconds: int,
) -> list[dict[str, Any]]:
    rows = football_response_rows(payload)

    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        fixture = row.get("fixture")
        teams = row.get("teams")
        league = row.get("league")
        if not isinstance(fixture, Mapping) or not isinstance(teams, Mapping):
            continue

        fixture_status = fixture.get("status")
        short_status = ""
        if isinstance(fixture_status, Mapping):
            short_status = clean_text(fixture_status.get("short"), maximum=12).upper()
        start = safe_int(fixture.get("timestamp"))

        if short_status in FOOTBALL_LIVE_STATES:
            state = "LIVE"
        elif (
            short_status == "NS"
            and start is not None
            and now_epoch_seconds <= start <= now_epoch_seconds + starting_soon_seconds
        ):
            state = "STARTING_SOON"
        else:
            continue

        home_row = teams.get("home")
        away_row = teams.get("away")
        if not isinstance(home_row, Mapping) or not isinstance(away_row, Mapping):
            continue
        home = participant(home_row.get("name"))
        away = participant(away_row.get("name"))
        if home is None or away is None:
            continue

        competition = "Football"
        if isinstance(league, Mapping):
            competition = clean_text(league.get("name"), maximum=100) or competition

        supplied_id = fixture.get("id")
        event_id = stable_event_id(
            "api-football",
            supplied_id,
            (home["name"], away["name"], start, competition),
        )
        events.append(
            {
                "id": event_id,
                "provider": "API_FOOTBALL",
                "providerEventId": event_id.split(":", 1)[1],
                "state": state,
                "sport": "FOOTBALL",
                "competition": competition,
                "displayTitle": f"{home['name']} vs {away['name']}",
                "startEpochSeconds": start,
                "participants": [home, away],
            }
        )
    return events


def football_response_rows(payload: Mapping[str, Any]) -> list[Any]:
    """Validate one API-Football envelope before its rows are consumed."""
    errors = payload.get("errors")
    if errors not in (None, [], {}):
        raise FeedBuildError("API-Football returned an unsuccessful response.")

    rows = payload.get("response")
    if not isinstance(rows, list):
        raise FeedBuildError("API-Football returned an invalid fixture list.")
    if len(rows) > MAX_EVENTS_PER_PROVIDER:
        raise FeedBuildError("API-Football returned too many fixtures.")
    return rows


def merge_football_payloads(
    payloads: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Merge ordered responses and retain the first row for each fixture ID."""
    combined: list[Any] = []
    seen_fixture_ids: set[str] = set()
    for payload in payloads:
        rows = football_response_rows(payload)
        for row in rows:
            fixture_id = ""
            if isinstance(row, Mapping):
                fixture = row.get("fixture")
                if isinstance(fixture, Mapping):
                    supplied_id = fixture.get("id")
                    if isinstance(supplied_id, (int, str)) and not isinstance(
                        supplied_id, bool
                    ):
                        fixture_id = clean_text(supplied_id, maximum=120)
            if fixture_id:
                if fixture_id in seen_fixture_ids:
                    continue
                seen_fixture_ids.add(fixture_id)
            combined.append(row)
            if len(combined) > MAX_EVENTS_PER_PROVIDER:
                raise FeedBuildError("API-Football returned too many fixtures.")
    return {
        "errors": [],
        "results": len(combined),
        "response": combined,
    }


def build_feed(
    cricket_payload: Mapping[str, Any],
    football_payload: Mapping[str, Any],
    *,
    now_epoch_seconds: int,
    starting_soon_minutes: int = DEFAULT_STARTING_SOON_MINUTES,
    lifetime_seconds: int = DEFAULT_FEED_LIFETIME_SECONDS,
) -> dict[str, Any]:
    if not 1 <= starting_soon_minutes <= 360:
        raise FeedBuildError("The starting-soon window must be between 1 and 360 minutes.")
    if not 300 <= lifetime_seconds <= 6 * 60 * 60:
        raise FeedBuildError("The feed lifetime must be between 5 minutes and 6 hours.")

    starting_soon_seconds = starting_soon_minutes * 60
    events = normalize_cricket_events(
        cricket_payload,
        now_epoch_seconds=now_epoch_seconds,
        starting_soon_seconds=starting_soon_seconds,
    )
    events.extend(
        normalize_football_events(
            football_payload,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_seconds=starting_soon_seconds,
        )
    )

    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        unique.setdefault(str(event["id"]), event)
    ordered = sorted(
        unique.values(),
        key=lambda event: (
            0 if event["state"] == "LIVE" else 1,
            event.get("startEpochSeconds")
            if event.get("startEpochSeconds") is not None
            else 2**63 - 1,
            str(event["displayTitle"]).casefold(),
            str(event["id"]),
        ),
    )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtEpochSeconds": now_epoch_seconds,
        "expiresAtEpochSeconds": now_epoch_seconds + lifetime_seconds,
        "startingSoonMinutes": starting_soon_minutes,
        "providers": ["CRICKETDATA", "API_FOOTBALL"],
        "events": ordered,
    }


def utc_date_range(now_epoch_seconds: int, starting_soon_minutes: int) -> tuple[date, date]:
    start = datetime.fromtimestamp(now_epoch_seconds, tz=timezone.utc).date()
    end = datetime.fromtimestamp(
        now_epoch_seconds + starting_soon_minutes * 60,
        tz=timezone.utc,
    ).date()
    return start, end


def fetch_json(
    session: Any,
    *,
    provider_name: str,
    url: str,
    allowed_host: str,
    params: Mapping[str, str],
    headers: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    try:
        response = session.get(
            url,
            params=dict(params),
            headers=dict(headers or {}),
            timeout=(10, 25),
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
    session: Any,
    *,
    api_football_key: str,
    now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> Mapping[str, Any]:
    """Fetch all live games and the UTC dates covered by the look-ahead window."""
    start_date, end_date = utc_date_range(
        now_epoch_seconds,
        starting_soon_minutes,
    )
    request_dates = [start_date]
    if end_date != start_date:
        request_dates.append(end_date)

    request_headers = {"x-apisports-key": api_football_key}
    live_payload = fetch_json(
        session,
        provider_name="API-Football",
        url=API_FOOTBALL_URL,
        allowed_host=API_FOOTBALL_HOST,
        params={"live": "all"},
        headers=request_headers,
    )
    football_response_rows(live_payload)
    payloads = [live_payload]
    for fixture_date in request_dates:
        daily_payload = fetch_json(
            session,
            provider_name="API-Football",
            url=API_FOOTBALL_URL,
            allowed_host=API_FOOTBALL_HOST,
            params={
                "date": fixture_date.isoformat(),
                "timezone": "UTC",
            },
            headers=request_headers,
        )
        football_response_rows(daily_payload)
        payloads.append(daily_payload)
    return merge_football_payloads(payloads)


def fetch_provider_payloads(
    *,
    cricketdata_api_key: str,
    api_football_key: str,
    now_epoch_seconds: int,
    starting_soon_minutes: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    try:
        import requests
    except ImportError:
        raise FeedBuildError(
            "The requests package is required to build the live event feed."
        ) from None

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "SKYTV-Live-Events/1.0",
        }
    )
    try:
        cricket = fetch_json(
            session,
            provider_name="CricketData",
            url=CRICKETDATA_URL,
            allowed_host=CRICKETDATA_HOST,
            params={"apikey": cricketdata_api_key},
        )

        football = fetch_api_football_payload(
            session,
            api_football_key=api_football_key,
            now_epoch_seconds=now_epoch_seconds,
            starting_soon_minutes=starting_soon_minutes,
        )
        return cricket, football
    finally:
        session.close()


def write_feed(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".build/live-events.json"),
    )
    parser.add_argument(
        "--starting-soon-minutes",
        type=int,
        default=DEFAULT_STARTING_SOON_MINUTES,
    )
    parser.add_argument(
        "--now-epoch-seconds",
        type=int,
        default=None,
        help="Override current UTC time for a reproducible local build.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = [
        name
        for name in ("CRICKETDATA_API_KEY", "API_FOOTBALL_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise FeedBuildError(
            "Missing required GitHub secret(s): " + ", ".join(missing)
        )

    now_epoch_seconds = args.now_epoch_seconds
    if now_epoch_seconds is None:
        now_epoch_seconds = int(datetime.now(tz=timezone.utc).timestamp())

    cricket, football = fetch_provider_payloads(
        cricketdata_api_key=os.environ["CRICKETDATA_API_KEY"].strip(),
        api_football_key=os.environ["API_FOOTBALL_KEY"].strip(),
        now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=args.starting_soon_minutes,
    )
    feed = build_feed(
        cricket,
        football,
        now_epoch_seconds=now_epoch_seconds,
        starting_soon_minutes=args.starting_soon_minutes,
    )
    write_feed(args.output, feed)
    print(
        f"Published {len(feed['events'])} live/starting-soon event(s) "
        f"to {args.output}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeedBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
