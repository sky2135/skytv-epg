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
from urllib.parse import parse_qsl, urljoin, urlsplit

import build_vod_catalogs as vod
from vod_provider_evidence import MovieEvidenceCollector


APPROVED_INPUT = Path(__file__).resolve().parents[1] / "data/diagnostics/server1-unmatched-2026-10-21.json"
STAT_KEYS = ("requests", "lookups", "cacheHits", "errors", "budgetDeferred")
ERROR_KEYS = {"transport", "auth_rejected", "invalid_json", "response_limit",
              "empty_refresh", "cache_io", "invalid_facts_or_internal", "redirect_refused"}
REDIRECT_FLAGS = ("locationSyntaxValid", "parseable", "sameHost", "samePort", "portAllowed",
                  "upgradeOrSameScheme", "samePath", "queryEquivalent", "hasQuery", "hasFragment", "hasUserInfo")
REDIRECT_REASONS = {"accepted", "invalid_location", "unparseable_location", "scheme_changed",
                    "host_changed", "port_changed", "user_info", "fragment", "path_changed",
                    "query_changed", "unclassified_refusal"}


def _counter(value):
    return value if type(value) is int and value >= 0 else 0


def _redirect_constraints(endpoint, location, params):
    result = {key: False for key in REDIRECT_FLAGS}
    result["locationSyntaxValid"] = bool(isinstance(location, str) and location and len(location) <= 8192
                                          and not any(ord(character) <= 32 for character in location))
    if not result["locationSyntaxValid"]:
        return result
    try:
        original = urlsplit(endpoint)
        target = urlsplit(urljoin(endpoint, location))
        original_port = original.port if original.port is not None else (443 if original.scheme == "https" else 80)
        target_port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
        upgrade = original.scheme == "http" and target.scheme == "https"
        result.update(parseable=True,
                      sameHost=bool(target.hostname and target.hostname.casefold() == (original.hostname or "").casefold()),
                      samePort=original_port == target_port,
                      portAllowed=original_port == target_port or (upgrade and original_port == 80 and target_port == 443),
                      upgradeOrSameScheme=target.scheme in {"http", "https"} and (target.scheme == original.scheme or upgrade),
                      samePath=target.path == original.path,
                      hasQuery=bool(target.query), hasFragment=bool(target.fragment),
                      hasUserInfo=target.username is not None or target.password is not None)
        if target.query:
            try:
                query = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
                result["queryEquivalent"] = len(query) == len(params) and dict(query) == dict(params)
            except (ValueError, TypeError):
                pass
        else:
            # The transport supplies the unchanged structured parameters again.
            result["queryEquivalent"] = True
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def _redirect_reason(flags):
    for flag, required, reason in (("locationSyntaxValid", True, "invalid_location"),
                                   ("parseable", True, "unparseable_location"),
                                   ("upgradeOrSameScheme", True, "scheme_changed"),
                                   ("sameHost", True, "host_changed"), ("portAllowed", True, "port_changed"),
                                   ("hasUserInfo", False, "user_info"), ("hasFragment", False, "fragment"),
                                   ("samePath", True, "path_changed"), ("queryEquivalent", True, "query_changed")):
        if flags[flag] is not required:
            return reason
    return "unclassified_refusal"


def _observe_redirect(original, observations):
    def observe(endpoint, location, params):
        flags = _redirect_constraints(endpoint, location, params)
        try:
            target = original(endpoint, location, params)
        except Exception:
            if not observations:
                observations.append({**flags, "decision": "refused", "reason": _redirect_reason(flags)})
            raise
        if not observations:
            observations.append({**flags, "decision": "accepted", "reason": "accepted"})
        return target
    return observe


def _report(stats=None, facts=None, observations=None):
    stats = stats if isinstance(stats, Mapping) else {}
    facts = facts if isinstance(facts, Mapping) else {}
    errors = stats.get("errorCounts", {})
    errors = errors if isinstance(errors, Mapping) else {}
    stages = stats.get("errorStages", {})
    stages = stages if isinstance(stages, Mapping) else {}
    observation = observations[0] if isinstance(observations, list) and observations else None
    redirect = None
    if isinstance(observation, Mapping):
        decision, reason = observation.get("decision"), observation.get("reason")
        redirect = {key: observation.get(key) is True for key in REDIRECT_FLAGS}
        redirect.update(decision=decision if isinstance(decision, str) and decision in {"accepted", "refused"} else "refused",
                        reason=reason if isinstance(reason, str) and reason in REDIRECT_REASONS else "unclassified_refusal")
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
        "redirect": redirect,
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
    observations = []
    try:
        with tempfile.TemporaryDirectory(prefix="vod-evidence-diagnostic-") as cache:
            collector = MovieEvidenceCollector(cache, configs={"server_1": config},
                max_requests=1, max_seconds=15,
                secret_values=(config.base_url, config.username, config.password))
            original_redirect = vod._safe_panel_redirect
            try:
                vod._safe_panel_redirect = _observe_redirect(original_redirect, observations)
                facts = collector.collect("server_1", item)
                report = _report(collector.stats, facts, observations)
            finally:
                vod._safe_panel_redirect = original_redirect
                collector.close()
    except Exception:
        # This optional observation must never fail publication or emit details.
        report = _report({"errors": 1}, observations=observations)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
