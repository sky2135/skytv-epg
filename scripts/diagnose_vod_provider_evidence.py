#!/usr/bin/env python3
"""Observe one approved movie-detail lookup without exposing private facts."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Mapping

import build_vod_catalogs as vod
from vod_provider_evidence import MovieEvidenceCollector


APPROVED_INPUT = Path(__file__).resolve().parents[1] / "data/diagnostics/server1-unmatched-2026-10-21.json"
STAT_KEYS = ("requests", "lookups", "cacheHits", "errors", "budgetDeferred")
ERROR_KEYS = {"transport", "auth_rejected", "invalid_json", "response_limit",
              "empty_refresh", "cache_io", "invalid_facts_or_internal", "redirect_refused"}


def _counter(value):
    return value if type(value) is int and value >= 0 else 0


def _report(stats=None, facts=None):
    stats = stats if isinstance(stats, Mapping) else {}
    facts = facts if isinstance(facts, Mapping) else {}
    errors = stats.get("errorCounts", {})
    errors = errors if isinstance(errors, Mapping) else {}
    stages = stats.get("errorStages", {})
    stages = stages if isinstance(stages, Mapping) else {}
    return {
        "success": bool(facts),
        "hasIMDb": isinstance(facts.get("imdbId"), str)
        and re.fullmatch(r"tt[0-9]{7,10}", facts["imdbId"]) is not None,
        "castCount": len(facts["cast"]) if isinstance(facts.get("cast"), list) else 0,
        "directorCount": len(facts["directors"]) if isinstance(facts.get("directors"), list) else 0,
        "stats": {key: _counter(stats.get(key)) for key in STAT_KEYS},
        "errorCounts": {
            key: _counter(value) for key, value in errors.items()
            if isinstance(key, str) and (key in ERROR_KEYS or re.fullmatch(r"http_[1-5][0-9]{2}", key))
        },
        "errorStages": {key: _counter(stages[key]) for key in ("request", "normalize", "validate", "cache")
                        if key in stages},
    }


def _approved_item():
    if APPROVED_INPUT.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Invalid diagnostic input")
    payload = json.loads(APPROVED_INPUT.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("schemaVersion") != 1
            or payload.get("serverId") != "server_1" or not isinstance(payload.get("records"), list)):
        raise ValueError("Invalid diagnostic input")
    for record in payload["records"]:
        if not isinstance(record, dict):
            continue
        item = record.get("matcherInput")
        if not isinstance(item, dict):
            continue
        content_id = item.get("contentId")
        if (isinstance(content_id, str) and re.fullmatch(r"server_1:movie:[0-9]{1,10}", content_id)
                and content_id == record.get("contentId")
                and str(item.get("id")) == content_id.rsplit(":", 1)[-1]
                and isinstance(item.get("title"), str) and item["title"].strip()
                and item["title"] == record.get("providerTitle")):
            return dict(item)
    raise ValueError("No approved diagnostic movie")


def main(argv=None):
    report = _report()
    if argv is None:
        argv = sys.argv[1:]
    try:
        if argv:
            raise ValueError("No diagnostic options are supported")
        allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "false").strip().casefold()
        if allow_http not in {"true", "false"}:
            raise ValueError("Invalid HTTP setting")
        item = _approved_item()
        config = vod.load_server_config("server_1", allow_insecure_http=allow_http == "true")
    except Exception:
        # Exception strings can contain configuration values or input paths.
        print(json.dumps(report, sort_keys=True))
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="vod-evidence-diagnostic-") as cache:
            collector = MovieEvidenceCollector(cache, configs={"server_1": config},
                max_requests=1, max_seconds=15,
                secret_values=(config.base_url, config.username, config.password))
            try:
                facts = collector.collect("server_1", item)
                report = _report(collector.stats, facts)
            finally:
                collector.close()
    except Exception:
        # This optional observation must never fail publication or emit details.
        report = _report({"errors": 1})
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
