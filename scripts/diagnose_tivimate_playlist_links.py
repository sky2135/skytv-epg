#!/usr/bin/env python3
"""Observe three Server 1 playlist IDs without changing mappings or guides."""
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

import sync_channel_inventory as inventory


TARGET_STREAM_IDS = ("122410", "122412", "122414")
MEMBER_LIMIT = 50
DEFAULT_OUTPUT = Path(".build/tivimate-playlist-links/report.json")


def _safe_name(channel: Mapping[str, Any]) -> str:
    name = channel.get("name", "")
    return name if (
        inventory._safe_playlist_text(name)
        and channel.get("_provider_channel_name_exact") is True
    ) else ""


def _safe_stream_id(channel: Mapping[str, Any]) -> str:
    stream_id = channel.get("stream_id", "")
    return stream_id if isinstance(stream_id, str) and re.fullmatch(
        r"[0-9]{1,120}|m3u_[0-9a-f]{20}", stream_id
    ) else ""


def _exact_epg_id(channel: Mapping[str, Any]) -> str:
    epg_id = channel.get("epg_channel_id", "")
    return epg_id if (
        inventory._safe_playlist_text(epg_id)
        and channel.get("_native_epg_id_exact") is True
    ) else ""


def _empty_target(stream_id: str, status: str) -> dict[str, Any]:
    return {
        "streamId": stream_id, "found": False, "name": "",
        "providerEpgId": "", "idStatus": status,
        "memberCount": 0, "members": [], "membersOmitted": 0,
        "memberCountCompleteness": "unknown",
    }


def build_report(
    source: inventory.PanelInventory | None, *, status: str = "unavailable",
    expected_source: str = "authenticated_m3u",
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schemaVersion": 1, "mode": "tivimate_playlist_links",
        "serverId": "server_1", "status": status,
        "source": "", "candidateLookups": 0, "playlistRows": 0,
        "quarantinedRowsExcluded": 0,
        "membershipCountCompleteness": "unknown",
        "targets": [_empty_target(stream_id, "unavailable") for stream_id in TARGET_STREAM_IDS],
    }
    if source is None:
        return report
    if source.server_id != "server_1" or source.source != expected_source:
        report["status"] = "invalid_source"
        return report
    report.update(status="available", source=expected_source, playlistRows=len(source.channels))
    quarantined = source.quarantined_stream_ids
    completeness = "lower_bound" if quarantined else "complete"
    report.update(
        quarantinedRowsExcluded=len(quarantined),
        membershipCountCompleteness=completeness,
    )
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    targets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for channel in source.channels:
        stream_id = _safe_stream_id(channel)
        if stream_id in TARGET_STREAM_IDS:
            targets[stream_id].append(channel)
        epg_id = _exact_epg_id(channel)
        if epg_id:
            # All raw, exact playlist members count, including rows that do
            # not have an exportable name or a numeric stream identity.
            groups[epg_id].append(channel)
    for index, stream_id in enumerate(TARGET_STREAM_IDS):
        rows = targets.get(stream_id, [])
        target = _empty_target(stream_id, "not_found")
        target["memberCountCompleteness"] = completeness
        report["targets"][index] = target
        if not rows:
            if stream_id in quarantined:
                target.update(found=True, idStatus="quarantined")
            continue
        target["found"] = True
        if len(rows) != 1:
            target["idStatus"] = "ambiguous_stream"
            continue
        channel = rows[0]
        target["name"] = _safe_name(channel)
        epg_id = _exact_epg_id(channel)
        if not epg_id:
            target["idStatus"] = "unsafe" if channel.get("_native_epg_id_raw_present") else "absent"
            continue
        members = groups[epg_id]
        # Select only the small output set without storing any stream URL.
        selected_members = heapq.nsmallest(
            MEMBER_LIMIT, members,
            key=lambda row: inventory.streaming.stream_sort_key(_safe_stream_id(row)),
        )
        target.update(
            providerEpgId=epg_id, idStatus="exact",
            memberCount=len(members), membersOmitted=max(0, len(members) - MEMBER_LIMIT),
            members=[
                {"streamId": _safe_stream_id(row), "name": _safe_name(row)}
                for row in selected_members
            ],
        )
    return report


def _api_inventory_probe(session, config, bases, *, allow_insecure_http, observations):
    """Only get_live_streams, without the ordinary API retry/category lanes."""
    def observe_response(response, **_kwargs):
        if response.status_code == 429:
            observations["rateLimited"] = True
        delay = inventory.streaming.panel_retry_after_seconds(
            response.headers.get("Retry-After"), now=time.time(),
        )
        if delay is not None:
            observations["retryAfterSeconds"] = delay
            observations["nextAttemptAtEpoch"] = int(time.time()) + delay
        return response

    session.hooks.setdefault("response", []).append(observe_response)
    for base in bases[:2]:
        observations["candidateLookups"] += 1
        try:
            status, content = inventory.safe_credentialed_get(
                session, f"{base}/player_api.php",
                params={"username": config.username, "password": config.password, "action": "get_live_streams"},
                maximum_bytes=inventory.MAX_PANEL_JSON_BYTES, timeout=(10, 20),
                allow_insecure_http=allow_insecure_http,
            )
        except inventory.SyncError:
            if observations.get("rateLimited") or observations.get("retryAfterSeconds") is not None:
                break
            continue
        observations["httpStatuses"].append(status)
        # A second safe-base candidate is discovery, never an HTTP retry.
        # Respect any advertised service cooldown without issuing it early.
        if status == 429 or observations.get("retryAfterSeconds") is not None:
            observations["rateLimited"] = status == 429
            break
        if status != 200:
            continue
        try:
            payload = json.loads(content.decode("utf-8-sig", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        quarantined: tuple[str, ...] = ()
        try:
            channels = inventory.normalize_panel_action_rows(payload, "get_live_streams")
        except inventory.SyncError:
            try:
                channels, quarantined = inventory.normalize_blank_name_quarantine(
                    payload,
                    reflection_policy=inventory.provider_reflection_needles(config, bases[:2]),
                )
            except inventory.SyncError:
                continue
        if channels:
            return inventory.PanelInventory(
                "server_1", "Server 1", [], channels, "player_api",
                quarantined_stream_ids=quarantined,
            )
    return None


def diagnose(*, allow_insecure_http: bool, source_mode: str = "m3u") -> dict[str, Any]:
    started = time.monotonic()
    report = build_report(None)
    config = None
    observations = {"candidateLookups": 0, "httpStatuses": [], "rateLimited": False}
    try:
        configs = inventory.load_server_configs(["server_1"])
        if len(configs) != 1 or configs[0].server_id != "server_1":
            raise inventory.SyncError("Server 1 configuration is unavailable.")
        config = configs[0]
        bases = inventory.panel_base_candidates(
            config.base_url, allow_insecure_http=allow_insecure_http,
        )
        if not bases:
            raise inventory.SyncError("Server 1 configuration is unavailable.")
    except inventory.SyncError:
        report["status"] = "configuration_unavailable"
    else:
        with requests.Session() as session:
            session.trust_env = False
            session.auth = inventory.streaming.PanelCredentiallessAuth()
            session.headers.update({
                "User-Agent": f"SKYTV-Channel-Inventory/{inventory.SYNC_VERSION}",
                "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
            })
            try:
                if source_mode == "api":
                    source = _api_inventory_probe(
                        session, config, bases,
                        allow_insecure_http=allow_insecure_http, observations=observations,
                    )
                else:
                    observations["candidateLookups"] = 1
                    source = inventory.fetch_m3u_inventory(
                        session, config, bases[:1], [],
                        allow_insecure_http=allow_insecure_http,
                        maximum_attempts=1, request_timeout=(10, 60),
                    )
                if source is not None:
                    inventory.validate_provider_inventory_secret_safe(
                        source.categories, source.channels,
                        inventory.provider_reflection_needles(config, bases[:2]),
                    )
                report = build_report(source, expected_source="player_api" if source_mode == "api" else "authenticated_m3u")
            except inventory.SyncError:
                # Existing transport/parser/reflection guards intentionally do
                # not expose provider bodies, endpoint URLs or error text.
                report = build_report(None, status="source_rejected")
    if config is not None:
        try:
            inventory.validate_provider_inventory_secret_safe(
                [], [
                    value
                    for target in report["targets"]
                    for value in [
                        {"stream_id": target["streamId"], "name": target["name"], "epg_id": target["providerEpgId"]},
                        *target["members"],
                    ]
                ],
                inventory.provider_reflection_needles(config, [config.base_url]),
            )
        except inventory.SyncError:
            report = build_report(None, status="reflection_blocked")
    report.update(observations)
    report["requestedSource"] = source_mode
    report["elapsedSeconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source", choices=("m3u", "api"), default="m3u")
    args = parser.parse_args(argv)
    report = diagnose(
        allow_insecure_http=os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "").strip().casefold() == "true",
        source_mode=args.source,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    inventory.atomic_write_bytes(
        args.output,
        (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    # Stream IDs, names and EPG identifiers are confined to the checked report.
    print(json.dumps({
        "candidateLookups": report["candidateLookups"],
        "playlistRows": report["playlistRows"],
        "targetsFound": sum(row["found"] for row in report["targets"]),
        "targetIdsObserved": sum(row["idStatus"] == "exact" for row in report["targets"]),
        "sharedTargets": sum(row["memberCount"] > 1 for row in report["targets"]),
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
