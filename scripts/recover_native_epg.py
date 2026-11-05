#!/usr/bin/env python3
"""Probe only unavailable native guides; request one refresh after recovery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests

import diagnose_provider_transport as diagnostic

NATIVE_SERVERS = frozenset({"server_2", "server_3"})


def targets(health: dict) -> list[str]:
    if not isinstance(health, dict):
        raise ValueError("Invalid public health report")
    value = health.get("unavailableNativePanelServers", [])
    if (not isinstance(value, list) or any(not isinstance(item, str) for item in value)
            or set(value) - NATIVE_SERVERS or len(value) != len(set(value))):
        raise ValueError("Invalid native guide availability report")
    if type(health.get("generatedAt")) is not int or health["generatedAt"] <= 0:
        raise ValueError("Missing public guide generation")
    return sorted(value)


def active_or_recent(runs: list[dict], now: int) -> bool:
    for run in runs:
        if run.get("status") != "completed":
            return True
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp()
        if now - created < 600:
            return True
    return False


def probe(health: dict, runs: list[dict], now: int) -> dict:
    selected = targets(health)
    report = {"schemaVersion": 1, "checkedAt": now,
              "generation": health["generatedAt"], "targets": selected,
              "providerChecks": [], "refreshNeeded": False}
    if not selected:
        report["outcome"] = "native_guides_available"
    elif active_or_recent(runs, now):
        report["outcome"] = "refresh_already_active_or_recent"
    else:
        report["providerChecks"] = [diagnostic.native_epg_probe(server) for server in selected]
        recovered = [item["serverId"] for item in report["providerChecks"] if item.get("usable") is True]
        report["refreshNeeded"] = bool(recovered)
        report["recoveredServers"] = recovered
        report["outcome"] = "source_recovered" if recovered else "provider_still_unavailable"
    return report


def dispatch_allowed(report: dict, health: dict, runs: list[dict], now: int) -> bool:
    """Recheck generation, actual probe result, age and pending refreshes."""
    if (report.get("schemaVersion") != 1 or report.get("refreshNeeded") is not True
            or type(report.get("checkedAt")) is not int
            or not 0 <= now - report["checkedAt"] <= 600
            or report.get("generation") != health.get("generatedAt")
            or report.get("targets") != targets(health) or active_or_recent(runs, now)):
        return False
    checks = report.get("providerChecks")
    if not isinstance(checks, list) or len(checks) != len(report["targets"]):
        return False
    if [item.get("serverId") for item in checks] != report["targets"]:
        return False
    return any(item.get("usable") is True and item.get("status") == "ok" for item in checks)


class Controller:
    def __init__(self):
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.base = "https://api.github.com/repos/" + repository
        self.public_base = os.environ.get("EPG_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not self.public_base:
            owner, name = repository.split("/")
            self.public_base = f"https://{owner}.github.io/{name}"
        url = urlsplit(self.public_base)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError("Invalid public guide URL")
        self.github = requests.Session()
        self.github.trust_env = False
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise ValueError("Missing workflow authorization")
        self.github.headers.update({"Authorization": "Bearer " + token,
                                    "Accept": "application/vnd.github+json",
                                    "X-GitHub-Api-Version": "2022-11-28"})

    def state(self):
        # Never send a GitHub token to the public site or a redirect target.
        with requests.Session() as public:
            public.trust_env = False
            response = public.get(self.public_base + "/health.json", params={"_": time.time_ns()},
                                  timeout=(10, 30), allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 64 * 1024:
                raise ValueError("Cannot read bounded public guide health")
            health = response.json()
        response = self.github.get(self.base + "/actions/workflows/main.yml/runs",
                                   params={"branch": "main", "per_page": 5, "_": time.time_ns()},
                                   timeout=(10, 30), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("Cannot check active live refreshes")
        runs = response.json()["workflow_runs"]
        if not isinstance(runs, list):
            raise ValueError("Invalid live workflow state")
        return health, runs

    def dispatch(self):
        response = self.github.post(self.base + "/actions/workflows/main.yml/dispatches",
                                    json={"ref": "main"}, timeout=(10, 30), allow_redirects=False)
        if response.status_code != 204:
            raise ValueError("Live recovery refresh was not accepted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "dispatch"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    controller = Controller()
    try:
        health, runs = controller.state()
        now = int(time.time())
        if args.mode == "probe":
            report = probe(health, runs, now)
            report["probeStartedAt"] = now
            report["checkedAt"] = int(time.time())
        else:
            if args.report.stat().st_size > 64 * 1024:
                raise ValueError("Oversized recovery report")
            report = json.loads(args.report.read_text(encoding="utf-8"))
            allowed = dispatch_allowed(report, health, runs, now)
            if allowed:
                controller.dispatch()
            report["refreshDispatched"] = allowed
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write("### Native guide recovery\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")
    finally:
        controller.github.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("Native guide recovery could not complete safely; no private response was logged.") from None
