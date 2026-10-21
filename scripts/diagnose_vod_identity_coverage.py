#!/usr/bin/env python3
"""CI-only aggregate audit of supplied movie identity and language metadata.

Only the category and movie inventory actions are fetched. Supplied identifiers
are syntax-checked hints, never verified film matches or evidence of audio.
No raw record, title, identifier value, URL or exception is written to output.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import build_vod_catalogs as vod
from vod_provider_evidence import DeadlineSession, _names


SERVERS = ("server_1", "server_2", "server_3")
TARGETS = ("en", "hi", "pa")
TMDB_FIELDS = ("tmdb", "tmdb_id", "tmdbId", "tmdb_url", "tmdbUrl")
IMDB_FIELDS = ("imdb", "imdb_id", "imdbId", "imdb_url", "imdbUrl")
RUNTIME_FIELDS = ("runtimeMinutes", "runtime", "duration_minutes", "duration_secs", "duration_seconds", "duration")
DIRECTOR_FIELDS = ("director", "directors")
CAST_FIELDS = ("cast", "actors")
LANGUAGE_FIELDS = ("languages", "language", "lang", "original_language", "spoken_languages")
RAW_FIELDS = TMDB_FIELDS + IMDB_FIELDS + RUNTIME_FIELDS + DIRECTOR_FIELDS + CAST_FIELDS + LANGUAGE_FIELDS
GROUPS = ("currentNormalizedLanguages", "rawLanguageMetadata", "structuredLanguageFields",
          "originalSpokenLanguageHints", "explicitCategoryLanguageLabels", "subtitleCategoryHints",
          "ambiguousCategoryCodeHints")
ERRORS = {"configuration", "transport", "http_error", "auth_rejected", "redirect_refused",
          "response_limit", "invalid_json", "schema_failure", "conflicting_duplicates", "deadline", "internal",
          "reflection_refused"}
PER_SERVER_SECONDS = 175
GLOBAL_SECONDS = 330
MAX_CATEGORIES = 10_000
MAX_REPORT_BYTES = 8 * 1024 * 1024


class AuditFailure(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else "internal"


def _rows(payload):
    rows = payload
    if isinstance(payload, Mapping):
        rows = next((payload[key] for key in ("data", "results", "response", "items")
                     if isinstance(payload.get(key), list)), None)
    if not isinstance(rows, list) or len(rows) > vod.MAX_ITEMS_PER_CATALOG:
        raise AuditFailure("schema_failure")
    return rows


def _nonempty(value):
    return value is not None and value != "" and value != [] and value != {}


def _sources(row):
    return [row] + [row[key] for key in ("movie_data", "info", "metadata") if isinstance(row.get(key), Mapping)]


def _identity(value, kind, *, url_only=False):
    if not url_only and kind == "tmdb" and not isinstance(value, bool):
        if isinstance(value, Mapping):
            value = value.get("id")
        if type(value) in (int, float) and 0 < value < 10**12 and math.isfinite(value) and value == int(value):
            return str(int(value)), "numeric"
        if isinstance(value, str):
            text = value.strip()
            if text.casefold().startswith("tmdb:"):
                text = text.split(":", 1)[1].strip()
            if re.fullmatch(r"[0-9]{1,12}", text) and 0 < int(text) < 10**12:
                return str(int(text)), "numeric"
    if not url_only and kind == "imdb" and isinstance(value, str) and re.fullmatch(r"tt[0-9]{7,10}", value):
        return value, "tt"
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            return None
        if kind == "tmdb" and parsed.hostname in {"themoviedb.org", "www.themoviedb.org"}:
            match = re.fullmatch(r"/movie/([1-9][0-9]{0,11})(?:-[A-Za-z0-9][A-Za-z0-9-]{0,200})?/?", parsed.path)
        elif kind == "imdb" and parsed.hostname in {"imdb.com", "www.imdb.com", "m.imdb.com"}:
            match = re.fullmatch(r"/title/(tt[0-9]{7,10})/?", parsed.path)
        else:
            return None
        return (match.group(1), "public_url") if match else None
    except (ValueError, TypeError):
        return None


def _runtime(value, alias):
    if isinstance(value, bool):
        return False
    try:
        if alias == "duration":
            match = re.fullmatch(r"([0-9]{1,2}):([0-5][0-9]):([0-5][0-9])", str(value))
            minutes = int(match[1]) * 60 + int(match[2]) + int(match[3]) / 60 if match else 0
        else:
            minutes = float(value)
            if alias in {"duration_secs", "duration_seconds"}:
                minutes /= 60
        return math.isfinite(minutes) and 1 <= minutes <= 1440
    except (ValueError, TypeError, OverflowError):
        return False


def _valid(value, alias):
    if alias in TMDB_FIELDS + IMDB_FIELDS:
        return _identity(value, "tmdb" if alias in TMDB_FIELDS else "imdb",
                         url_only=alias.endswith(("_url", "Url"))) is not None
    if alias in RUNTIME_FIELDS:
        return _runtime(value, alias)
    if alias in DIRECTOR_FIELDS + CAST_FIELDS:
        return bool(_names(value))
    return vod.normalize_languages([{alias: value}], "")[0] != "und"


def _category_evidence(name):
    folded = name.casefold()
    full = {code for word, code in (("english", "en"), ("hindi", "hi"), ("punjabi", "pa"))
            if re.search(rf"(?<![a-z]){word}(?![a-z])", folded)}
    bracket = set(re.findall(r"\[\s*(en|hi|pa|pan)\s*\]", folded))
    unambiguous = bracket & {"en", "hi"}
    ambiguous = {"pa"} if bracket & {"pa", "pan"} and "pa" not in full else set()
    codes = full | unambiguous
    subtitle = bool(re.search(r"(?<![a-z])(?:sub|subs|subtitle|subtitles|subbed|multisubs?)(?![a-z])", folded))
    if subtitle:
        return set(), codes | ambiguous, set(), "subtitle_hint"
    evidence = "full_name" if full else "bracket_code" if unambiguous else "ambiguous_code" if ambiguous else "unclassified"
    return codes, set(), ambiguous, evidence


def _coverage():
    return {key: 0 for key in ("movieCount", "withTmdb", "withImdb", "withBoth",
        "withAnySupportedId", "withNeitherSupportedId", "withNumericTmdb", "withTtImdb",
        "withPublicTmdbUrl", "withPublicImdbUrl", "withRuntime", "withDirector", "withCast",
        "withConflictingTmdbHints", "withConflictingImdbHints")}


def _empty_server():
    return {"success": False, "countsAvailable": False, "inventoryRows": 0, "uniqueProviderIds": 0,
        "skippedUnusableRows": 0, "duplicateRows": 0, "conflictingDuplicateIds": 0,
        "total": _coverage(),
        "languageGroups": {group: {code: _coverage() for code in (*TARGETS, "targetUnion")} for group in GROUPS},
        "rawFields": {alias: {"present": 0, "nonempty": 0, "valid": 0} for alias in RAW_FIELDS},
        "categories": [], "errorCounts": {}, "transport": {"requests": 0, "downloadBytes": 0, "elapsedMilliseconds": 0}}


def _add(bucket, facts):
    bucket["movieCount"] += 1
    tmdb, imdb = bool(facts["tmdb"]), bool(facts["imdb"])
    flags = {"withTmdb": tmdb, "withImdb": imdb, "withBoth": tmdb and imdb,
        "withAnySupportedId": tmdb or imdb, "withNeitherSupportedId": not (tmdb or imdb),
        "withNumericTmdb": "numeric" in facts["tmdbTypes"], "withTtImdb": "tt" in facts["imdbTypes"],
        "withPublicTmdbUrl": "public_url" in facts["tmdbTypes"],
        "withPublicImdbUrl": "public_url" in facts["imdbTypes"],
        "withRuntime": facts["runtime"], "withDirector": facts["director"], "withCast": facts["cast"],
        "withConflictingTmdbHints": len(facts["tmdb"]) > 1, "withConflictingImdbHints": len(facts["imdb"]) > 1}
    for key, present in flags.items():
        bucket[key] += int(present)


def summarize_inventory(categories_payload, movies_payload, *, deadline=None):
    """Pure offline aggregate; duplicate conflicts fail closed without raw data."""
    report = _empty_server()
    try:
        categories = {}
        category_rows = _rows(categories_payload)
        if len(category_rows) > MAX_CATEGORIES:
            raise AuditFailure("schema_failure")
        for row in category_rows:
            if not isinstance(row, Mapping):
                continue
            identifier = vod.normalize_item_id(row.get("category_id", row.get("id")))
            name = vod.clean_text(row.get("category_name", row.get("name")), 160)
            if identifier and identifier in categories and categories[identifier] != name:
                raise AuditFailure("conflicting_duplicates")
            if identifier:
                categories[identifier] = name
        rows = _rows(movies_payload)
        report["inventoryRows"] = len(rows)
        seen, conflicts = {}, set()
        for position, row in enumerate(rows):
            if position % 128 == 0 and deadline is not None and time.monotonic() >= deadline:
                raise AuditFailure("deadline")
            if not isinstance(row, Mapping):
                report["skippedUnusableRows"] += 1
                continue
            provider_id = next((value for alias in ("stream_id", "vod_id", "id")
                                if (value := vod.normalize_item_id(row.get(alias)))), "")
            title = vod.clean_text(row.get("name", row.get("title")), 300)
            if not provider_id or not title:
                report["skippedUnusableRows"] += 1
                continue
            category_id = vod.normalize_item_id(row.get("category_id"))
            category_name = vod.clean_text(row.get("category_name") or categories.get(category_id, ""), 160)
            sources = _sources(row)
            values = {alias: [source[alias] for source in sources if alias in source] for alias in RAW_FIELDS}
            # Titles are compared only in private memory to detect recycled IDs.
            signature = {"title": title, "categoryId": category_id, "categoryName": category_name, "fields": values}
            if provider_id in seen:
                report["duplicateRows"] += 1
                if seen[provider_id][0] != signature:
                    conflicts.add(provider_id)
                continue
            seen[provider_id] = (signature, row, category_id, category_name, values)
        report["uniqueProviderIds"], report["conflictingDuplicateIds"] = len(seen), len(conflicts)
        if conflicts:
            raise AuditFailure("conflicting_duplicates")
        category_counts = Counter()
        for position, (_signature, row, category_id, category_name, values) in enumerate(seen.values()):
            if position % 128 == 0 and deadline is not None and time.monotonic() >= deadline:
                raise AuditFailure("deadline")
            facts = {"tmdb": set(), "imdb": set(), "tmdbTypes": set(), "imdbTypes": set(),
                     "runtime": False, "director": False, "cast": False}
            for alias, supplied in values.items():
                counts = report["rawFields"][alias]
                counts["present"] += int(bool(supplied))
                counts["nonempty"] += int(any(_nonempty(value) for value in supplied))
                counts["valid"] += int(any(_valid(value, alias) for value in supplied))
                for value in supplied:
                    if alias in TMDB_FIELDS + IMDB_FIELDS:
                        kind = "tmdb" if alias in TMDB_FIELDS else "imdb"
                        identity = _identity(value, kind, url_only=alias.endswith(("_url", "Url")))
                        if identity:
                            facts[kind].add(identity[0])
                            facts[kind + "Types"].add(identity[1])
                    elif alias in RUNTIME_FIELDS:
                        facts["runtime"] |= _runtime(value, alias)
                    elif alias in DIRECTOR_FIELDS:
                        facts["director"] |= bool(_names(value))
                    elif alias in CAST_FIELDS:
                        facts["cast"] |= bool(_names(value))
            _add(report["total"], facts)
            explicit, subtitles, ambiguous, evidence = _category_evidence(category_name)
            sources = _sources(row)
            languages = {
                "currentNormalizedLanguages": set(vod.normalize_languages([row], category_name)[1]),
                "rawLanguageMetadata": set(vod.normalize_languages(sources, "")[1]),
                "structuredLanguageFields": set(vod.normalize_languages([{k: source[k] for k in ("languages", "language", "lang") if k in source} for source in sources], "")[1]),
                "originalSpokenLanguageHints": set(vod.normalize_languages([{k: source[k] for k in ("original_language", "spoken_languages") if k in source} for source in sources], "")[1]),
                "explicitCategoryLanguageLabels": explicit, "subtitleCategoryHints": subtitles,
                "ambiguousCategoryCodeHints": ambiguous}
            for group, codes in languages.items():
                selected = codes & set(TARGETS)
                for code in selected:
                    _add(report["languageGroups"][group][code], facts)
                if selected:
                    _add(report["languageGroups"][group]["targetUnion"], facts)
            category_counts[(evidence, tuple(sorted(explicit | subtitles | ambiguous)))] += 1
        report["categories"] = [{"evidenceType": evidence,
                                  "languageCodes": list(codes), "movieCount": count}
                                 for (evidence, codes), count in sorted(category_counts.items())]
        report.update(success=True, countsAvailable=True)
    except AuditFailure as error:
        report["errorCounts"] = {error.code: 1}
        # Partial language/identity counts must never look like full coverage.
        report["total"] = _coverage()
        report["languageGroups"] = _empty_server()["languageGroups"]
        report["rawFields"] = _empty_server()["rawFields"]
        report["categories"] = []
    except Exception:
        return {**_empty_server(), "errorCounts": {"schema_failure": 1}}
    return report


def _failure_code(error, server_id):
    if isinstance(error, AuditFailure):
        return error.code
    if isinstance(error, vod.TransientPanelRequestError):
        return "http_error" if type(error.http_status) is int else "transport"
    if isinstance(error, vod.CatalogBuildError):
        message = str(error)
        if message == f"{server_id}: provider credentials were rejected.":
            return "auth_rejected"
        if message in {f"{server_id}: too many provider redirects.", f"{server_id}: unsafe provider redirect refused."}:
            return "redirect_refused"
        if message == f"{server_id} provider response is not valid UTF-8 JSON.":
            return "invalid_json"
        if message in {f"{server_id}: provider response is too large.", f"{server_id}: provider downloads exceed the total size limit."}:
            return "response_limit"
        if re.fullmatch(rf"{server_id}: get_vod_(?:categories|streams) provider returned HTTP [1-5][0-9]{{2}}\.", message):
            return "http_error"
        return "transport"
    return "internal"


def collect_server(server_id, allow_insecure_http, *, global_deadline=None):
    started = time.monotonic()
    deadline = min(started + PER_SERVER_SECONDS, global_deadline or float("inf"))
    report, stats = _empty_server(), {"requests": 0}
    budget = vod.DownloadBudget(limit=104 * 1024 * 1024)
    session = None
    try:
        try:
            config = vod.load_server_config(server_id, allow_insecure_http=allow_insecure_http)
        except Exception:
            raise AuditFailure("configuration") from None
        # Leave time for decoding and aggregation inside the total deadline.
        session = DeadlineSession(min(deadline - 15, started + 150), stats, 10)
        categories = vod._request_panel_json_once(session, config, "get_vod_categories",
                                                  maximum_bytes=8 * 1024 * 1024, budget=budget)
        movies = vod._request_panel_json_once(session, config, "get_vod_streams",
                                              maximum_bytes=96 * 1024 * 1024, budget=budget)
        report = summarize_inventory(categories, movies, deadline=deadline)
    except Exception as error:
        code = "deadline" if time.monotonic() >= deadline else _failure_code(error, server_id)
        report["errorCounts"] = {code: 1}
    finally:
        if session is not None:
            session.close()
    report["transport"] = {"requests": stats["requests"], "downloadBytes": budget.consumed,
                           "elapsedMilliseconds": round(max(0, time.monotonic() - started) * 1000)}
    return report


def _reject_secret_reflection(raw):
    # All emitted strings are fixed codes/timestamps. Short credentials can
    # coincide with aggregate counts; protect configured values of >=8 chars.
    for server_id in SERVERS:
        prefix = server_id.upper() + "_"
        for suffix in ("BASE_URL", "USERNAME", "PASSWORD"):
            value = os.environ.get(prefix + suffix, "")
            if len(value) >= 8 and any(token in raw for token in vod._secret_variants(value)):
                raise AuditFailure("reflection_refused")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "false").strip().casefold()
        if allow_http not in {"true", "false"}:
            raise AuditFailure("configuration")
        global_deadline = time.monotonic() + GLOBAL_SECONDS
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(collect_server, server_id, allow_http == "true",
                                       global_deadline=global_deadline) for server_id in SERVERS]
            reports = {server_id: future.result() for server_id, future in zip(SERVERS, futures)}
        report = {"schemaVersion": 1, "success": all(value["success"] for value in reports.values()),
            "capturedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "serverCount": 3, "providerHintsUnverified": True, "audioAvailabilityVerified": False,
            "languageGroupsMayOverlap": True, "currentNormalizerMayIncludeSubtitleHints": True,
            "rawAliasCountsMayOverlap": True, "serverDeadlineSeconds": PER_SERVER_SECONDS,
            "globalDeadlineSeconds": GLOBAL_SECONDS, "servers": reports}
        raw = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_REPORT_BYTES:
            raise AuditFailure("response_limit")
        _reject_secret_reflection(raw)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = vod._temporary_path(args.output.parent, "identity-coverage")
        try:
            temporary.write_bytes(raw + b"\n")
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"success": report["success"], "serverCount": 3,
                          "successfulServers": sum(value["success"] for value in reports.values())}, sort_keys=True))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, AuditFailure) else "configuration"
        print(json.dumps({"success": False, "errorCounts": {code: 1}}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
