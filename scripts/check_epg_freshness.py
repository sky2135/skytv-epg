#!/usr/bin/env python3
"""Check publication age and real future schedule endpoints without credentials."""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


MAX_JSON_BYTES = 16 * 1024 * 1024
SERVER_IDS = ("server_1", "server_2", "server_3")


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid nonnegative integer")
    return value


def _server_list(value: Any) -> list[str]:
    if (not isinstance(value, list) or len(value) > len(SERVER_IDS)
            or any(not isinstance(item, str) or item not in SERVER_IDS for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("Invalid server list")
    return value


def _real_future_metrics(coverage: Any, generated_at: int) -> tuple[int, int]:
    if not isinstance(coverage, dict) or type(coverage.get("schemaVersion")) is not int or coverage["schemaVersion"] != 1:
        raise ValueError("Invalid future coverage schema")
    if _count(coverage.get("measuredAt")) != generated_at:
        raise ValueError("Future metrics belong to another generation")
    minimum_future = _count(coverage.get("minimumFutureSeconds"))
    sources = coverage.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"epgshare", "nativePanel", "localSynthetic"}:
        raise ValueError("Invalid future source list")
    totals = {"mappedStreams": 0, "streamsWithFutureProgrammes": 0, "streamsReachingMinimumHorizon": 0}
    real_future = real_horizon = real_latest = 0
    for key, source in sources.items():
        if not isinstance(source, dict):
            raise ValueError("Invalid future source metrics")
        mapped = _count(source.get("mappedStreams"))
        future = _count(source.get("streamsWithFutureProgrammes"))
        horizon = _count(source.get("streamsReachingMinimumHorizon"))
        latest = source.get("latestStop")
        if not horizon <= future <= mapped or (future == 0 and latest is not None):
            raise ValueError("Inconsistent future source counts")
        if future:
            latest = _count(latest)
            if latest <= generated_at or bool(horizon) != (latest >= generated_at + minimum_future):
                raise ValueError("Inconsistent future source endpoint")
        else:
            latest = 0
        totals["mappedStreams"] += mapped
        if key != "localSynthetic":
            real_future += future
            real_horizon += horizon
            real_latest = max(real_latest, latest)
    if (_count(coverage.get("eligibleStreams")) != totals["mappedStreams"]
            or _count(coverage.get("realStreamsWithFutureProgrammes")) != real_future
            or _count(coverage.get("realStreamsReachingMinimumHorizon")) != real_horizon
            or (real_latest and _count(coverage.get("realLatestStop")) != real_latest)
            or (not real_latest and coverage.get("realLatestStop") is not None)):
        raise ValueError("Inconsistent real future totals")
    return real_future, real_latest


def check_payloads(
    health: Mapping[str, Any], index: Mapping[str, Any], *, now_epoch: int,
    max_age_hours: float = 36, minimum_future_hours: float = 6,
    expected_servers: Sequence[str] = SERVER_IDS,
) -> dict[str, Any]:
    errors: list[str] = []
    degraded: list[str] = []
    ages: dict[str, float] = {}
    if not isinstance(health, dict) or not isinstance(index, dict):
        raise ValueError("Health and index must be objects")
    if (not expected_servers or len(set(expected_servers)) != len(expected_servers)
            or any(server not in SERVER_IDS for server in expected_servers)):
        raise ValueError("Invalid expected server list")
    for label, payload in (("health", health), ("index", index)):
        try:
            generated = _count(payload.get("generatedAt"))
            age = now_epoch - generated
            ages[label] = round(age / 3600, 3)
            if age < -300:
                errors.append(f"{label}: generation time is in the future")
            elif age > max_age_hours * 3600:
                errors.append(f"{label}: publication is older than the maximum age")
        except (ValueError, TypeError, OverflowError, AttributeError):
            errors.append(f"{label}: generation timestamp is missing or invalid")
    if health.get("generatedAt") != index.get("generatedAt"):
        errors.append("health and index generation times differ")
    hashes = [payload.get("mappingSha256") for payload in (health, index)]
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        errors.append("health or index mapping snapshot SHA-256 is missing or invalid")
    elif hashes[0] != hashes[1]:
        errors.append("health and index mapping snapshots differ")
    if health.get("status") == "degraded":
        degraded.append("publication reported degraded provider availability")
    elif health.get("status") != "ok":
        errors.append("publication health status is missing or invalid")
    try:
        inventory = health["inventoryAvailability"]
        if not isinstance(inventory, dict) or inventory.get("status") not in {"ok", "partial", "not_provided"}:
            raise ValueError("Invalid inventory availability")
        checked = _server_list(inventory.get("checkedServers"))
        unavailable = _server_list(inventory.get("unavailableServers"))
        panel_unavailable = _server_list(health.get("unavailableNativePanelServers"))
        if (set(checked) & set(unavailable)
                or (inventory["status"] == "partial") != bool(unavailable)
                or inventory["status"] == "not_provided"
                or not set(expected_servers).issubset(set(checked) | set(unavailable))
                or (health["status"] == "ok" and (unavailable or panel_unavailable))):
            raise ValueError("Inconsistent provider availability")
        if unavailable:
            degraded.append("one or more provider inventories were unavailable")
        if panel_unavailable:
            degraded.append("one or more native panel guides were unavailable")
    except (KeyError, TypeError, ValueError):
        errors.append("provider availability schema is missing or invalid")

    builds = index.get("builds", [])
    if not isinstance(builds, list):
        builds = []
        errors.append("index build list is invalid")
    by_server: dict[str, Mapping[str, Any]] = {}
    for item in builds:
        if not isinstance(item, dict) or item.get("serverId") not in SERVER_IDS:
            errors.append("index contains an invalid server build")
            continue
        server_id = item["serverId"]
        if server_id in by_server:
            errors.append(f"{server_id}: duplicate server build")
        by_server[server_id] = item
    try:
        if (_count(health.get("serverCount")) != len(by_server)
                or _count(health.get("servers")) != len(by_server)):
            raise ValueError("Inconsistent server count")
    except ValueError:
        errors.append("health server counts are missing or inconsistent with the index")
    horizons: dict[str, float | None] = {}
    for server_id in expected_servers:
        if server_id not in by_server:
            errors.append(f"{server_id}: expected server build is absent")
            continue
        try:
            # Use only external sources. Synthetic placeholders cannot keep an
            # expired real schedule green, regardless of their end date.
            real_streams, latest = _real_future_metrics(
                by_server[server_id]["futureProgrammeCoverage"], _count(index.get("generatedAt"))
            )
            horizons[server_id] = round((latest - now_epoch) / 3600, 3) if latest else None
            if real_streams <= 0 or latest < now_epoch + minimum_future_hours * 3600:
                errors.append(f"{server_id}: real future programme endpoint is below the minimum horizon")
        except (KeyError, TypeError, ValueError, OverflowError):
            horizons[server_id] = None
            errors.append(f"{server_id}: real future programme metrics are missing or invalid")
    return {
        "schemaVersion": 1,
        "checkedAt": now_epoch,
        "checkedAtUtc": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "error" if errors else "degraded" if degraded else "ok",
        "maximumAgeHours": max_age_hours,
        "minimumFutureHours": minimum_future_hours,
        "publicationAgeHours": ages,
        "realFutureEndpointHours": horizons,
        "errors": errors,
        "degradedReasons": degraded,
    }


def read_payload(*, public_dir: Path | None, base_url: str, relative: str) -> dict[str, Any]:
    if public_dir is not None:
        with (public_dir / relative).open("rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
    else:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Public base URL must be credential-free HTTPS")
        request = Request(urljoin(base_url.rstrip("/") + "/", relative),
                          headers={"User-Agent": "SKYTV-EPG-Freshness/1.0", "Cache-Control": "no-cache"})
        with urlopen(request, timeout=30) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Publication JSON exceeds its size limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Publication JSON must be an object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--public-dir", type=Path)
    location.add_argument("--base-url", default="")
    parser.add_argument("--max-age-hours", type=float, default=36)
    parser.add_argument("--minimum-future-hours", type=float, default=6)
    parser.add_argument("--expected-server", action="append", choices=SERVER_IDS)
    parser.add_argument("--allow-degraded", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--now-epoch", type=int)
    args = parser.parse_args(argv)
    if not 0 < args.max_age_hours <= 168 or not 0 <= args.minimum_future_hours <= 168:
        parser.error("age must be positive and both bounds must be at most 168 hours")
    try:
        health = read_payload(public_dir=args.public_dir, base_url=args.base_url, relative="health.json")
        index = read_payload(public_dir=args.public_dir, base_url=args.base_url, relative="epg/index.json")
        result = check_payloads(health, index, now_epoch=args.now_epoch or int(time.time()),
                                max_age_hours=args.max_age_hours, minimum_future_hours=args.minimum_future_hours,
                                expected_servers=args.expected_server or SERVER_IDS)
    except Exception:
        # Remote URLs and exception text may contain infrastructure diagnostics.
        result = {"schemaVersion": 1, "status": "error", "errors": ["Could not read valid public health/index JSON"]}
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if result["status"] == "ok" or (args.allow_degraded and result["status"] == "degraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
