#!/usr/bin/env python3
"""CI-only filtered provider list/detail samples; never a full raw response."""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

import build_vod_catalogs as vod
from diagnose_vod_identity_coverage import SERVERS, TARGETS, _category_evidence, _identity, _rows, _failure_code
from diagnose_vod_provider_evidence import _redirect_constraints
from vod_provider_evidence import DeadlineSession, _error_category


ALLOW = frozenset("""info movie_data metadata details streams video audio format
id stream_id vod_id stream_type num name title original_title originalTitle category_id category_ids category_name
added added_at created_at last_modified year releaseDate release_date releasedate
plot description overview genre genres country countries language languages lang original_language spoken_languages
rating rating_5based vote_average vote_count adult is_adult container_extension extension
tmdb tmdb_id tmdbId tmdb_url tmdbUrl imdb imdb_id imdbId imdb_url imdbUrl
director directors cast actors duration duration_secs duration_seconds duration_minutes runtime runtimeMinutes
age age_rating mpaa youtube_trailer trailer
stream_icon movie_image cover cover_big cover_url poster poster_url poster_path backdrop backdrop_url backdrop_path image image_url
codec_name codec_long_name codec_type codec_tag_string codec_tag profile width height
display_aspect_ratio sample_aspect_ratio r_frame_rate avg_frame_rate sample_rate channels channel_layout
bit_rate bitrate bits_per_raw_sample resolution fps format_name format_long_name size nb_streams probe_score
role character code iso_639_1""".split())
DENIED = frozenset("""direct_source directSource stream_url playback_url source source_url url movie_url
username password user_info server_info auth token access_token portal base_url m3u8 custom_sid""".split())
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_VALUE_BYTES = 8 * 1024
MAX_NODES = 512
IMAGE_FIELDS = frozenset("stream_icon movie_image cover cover_big cover_url poster poster_url poster_path backdrop backdrop_url backdrop_path image image_url".split())
URL_FIELDS = IMAGE_FIELDS | {"tmdb_url", "tmdbUrl", "imdb_url", "imdbUrl"}
PER_SERVER_SECONDS = 300
GLOBAL_SECONDS = 420


def _type(value):
    return "null" if value is None else "boolean" if type(value) is bool else "integer" if type(value) is int else "number" if type(value) is float else "string" if isinstance(value, str) else "object" if isinstance(value, Mapping) else "array" if isinstance(value, list) else "unsupported"


def _secret_values(configs):
    result = set()
    for config in configs:
        for value in (config.base_url, config.username, config.password):
            if value:
                result.update(token.decode("utf-8") for token in vod._secret_variants(value))
        host = urlsplit(config.base_url).hostname
        if host:
            result.add(host)
    return result


def _reflects(value, secrets):
    text = str(value)
    return any(secret.casefold() in text.casefold() for secret in secrets if secret)


def _decoded(value):
    variants = [value]
    for _ in range(3):
        decoded = html.unescape(unquote(variants[-1]))
        if decoded == variants[-1]:
            break
        variants.append(decoded)
    return variants


def _public_url(value):
    if _identity(value, "tmdb", url_only=True) or _identity(value, "imdb", url_only=True):
        return True
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            return False
        if parsed.hostname == "image.tmdb.org":
            return re.fullmatch(r"/t/p/(?:original|[wh][0-9]{2,4})/[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)", parsed.path, re.I) is not None
        if parsed.hostname == "m.media-amazon.com":
            return re.fullmatch(r"/images/M/[A-Za-z0-9_.,@%-]+\.(?:jpg|jpeg|png|webp)", parsed.path, re.I) is not None
    except (ValueError, TypeError):
        pass
    return False


def sanitize_record(payload, secrets=()):
    """Retain only original allowlisted values; withheld values become null."""
    audit = {"fieldsPresent": [], "unknownFieldCount": 0, "withheldFieldCount": 0, "limitReached": False}
    remaining = [MAX_VALUE_BYTES]
    nodes = [MAX_NODES]

    def visit(value, path, depth):
        reason = None
        if nodes[0] <= 0:
            audit["limitReached"] = True
            return None, "size_limit"
        nodes[0] -= 1
        if depth > 6 or len(audit["fieldsPresent"]) >= 64:
            audit["limitReached"] = True
            return None, "size_limit"
        if isinstance(value, Mapping):
            result = {}
            for key, child in value.items():
                if key not in ALLOW and key not in DENIED:
                    audit["unknownFieldCount"] += 1
                    continue
                if len(audit["fieldsPresent"]) >= 64 or nodes[0] <= 0:
                    audit["limitReached"] = True
                    break
                child_path = path + "." + key if path else key
                field = {"path": child_path, "type": _type(child)}
                audit["fieldsPresent"].append(field)
                if key in DENIED:
                    retained, why = (child, None) if child is None or child == "" or child == [] or child == {} else (None, "playback_or_private")
                else:
                    retained, why = visit(child, child_path, depth + 1)
                if why:
                    field["withheldReason"] = why
                    audit["withheldFieldCount"] += 1
                result[key] = retained
            return result, None
        if isinstance(value, list):
            if len(value) > 20:
                audit["limitReached"] = True
                return None, "size_limit"
            result = []
            for index, child in enumerate(value):
                if nodes[0] <= 0 or len(audit["fieldsPresent"]) >= 64:
                    audit["limitReached"] = True
                    break
                child_path = path + "." + str(index)
                retained, why = visit(child, child_path, depth + 1)
                result.append(retained)
                if why:
                    audit["fieldsPresent"].append({"path": child_path, "type": _type(child), "withheldReason": why})
                    audit["withheldFieldCount"] += 1
            return result, None
        if _type(value) == "unsupported" or (type(value) is float and not math.isfinite(value)):
            return None, "unsupported_type"
        if value is not None and _reflects(value, secrets):
            return None, "secret_reflection"
        if isinstance(value, str):
            if len(value) > 2000:
                audit["limitReached"] = True
                return None, "size_limit"
            if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
                return None, "unsafe_text"
            public = _public_url(value)
            key = next((part for part in reversed(path.split(".")) if not part.isdigit()), "")
            safe_image_path = key in IMAGE_FIELDS and re.fullmatch(r"/?[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)", value, re.I)
            if value and key in URL_FIELDS and not public and not safe_image_path:
                return None, "private_or_unsupported_url"
            for checked in _decoded(value):
                if _reflects(checked, secrets):
                    return None, "secret_reflection"
                if not public and re.search(r"(?i)(?:[a-z][a-z0-9+.-]*://|\bwww\.)", checked):
                    return None, "private_or_unsupported_url"
                if not public and ("//" in checked or re.search(r"(?i)/(?:movie|live|series)/|/(?:player_api|get)\.php", checked)):
                    return None, "playback_or_private"
                if re.search(r'''(?i)(?<![a-z0-9])(?:username|password|access[_-]?token|token|api[_-]?key|auth|authorization)["']?\s*[:=]''', checked):
                    return None, "playback_or_private"
        size = len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())
        if size > 2000 or size > remaining[0]:
            audit["limitReached"] = True
            return None, "size_limit"
        remaining[0] -= size
        return value, reason

    safe, reason = visit(payload, "", 0)
    if reason:
        audit["rootWithheldReason"] = reason
    audit["visitedNodes"] = MAX_NODES - nodes[0]
    return {"record": safe, **audit}


def _added(value, now):
    if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}", value)):
        return None
    number = int(value)
    if number <= 0:
        return None
    seconds = number / 1000 if number >= 10**12 else number
    return seconds if 0 < seconds <= now + 300 else None


def select_recent(categories_payload, movies_payload, *, now=None, deadline=None):
    now = time.time() if now is None else now
    category_rows = _rows(categories_payload)
    categories = {}
    for row in category_rows:
        if isinstance(row, Mapping):
            key = vod.normalize_item_id(row.get("category_id", row.get("id")))
            if key in categories and categories[key] != row:
                raise ValueError("Conflicting category")
            categories[key] = row
    selected, seen = {code: [] for code in TARGETS}, {}
    counts = {"inventoryRows": 0, "duplicateRows": 0, "invalidIdOrTitle": 0, "invalidAdded": 0,
              "subtitleLabelExcluded": 0, "ambiguousLabelExcluded": 0, "unclassifiedLabelExcluded": 0}
    for position, row in enumerate(_rows(movies_payload)):
        if position % 128 == 0 and deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        counts["inventoryRows"] += 1
        if not isinstance(row, Mapping):
            counts["invalidIdOrTitle"] += 1
            continue
        identifier = next((str(row[key]) for key in ("stream_id", "vod_id", "id")
                           if type(row.get(key)) in (str, int) and re.fullmatch(r"[1-9][0-9]{0,9}", str(row[key]))), None)
        if not identifier or not isinstance(row.get("name", row.get("title")), str) or not row.get("name", row.get("title", "")).strip():
            counts["invalidIdOrTitle"] += 1
            continue
        if identifier in seen:
            counts["duplicateRows"] += 1
            if seen[identifier] != row:
                raise ValueError("Conflicting provider ID")
            continue
        seen[identifier] = row
        added = _added(row.get("added"), now)
        if added is None:
            counts["invalidAdded"] += 1
            continue
        category = categories.get(str(row.get("category_id", "")), {})
        name = vod.clean_text(row.get("category_name") or category.get("category_name", category.get("name", "")), 160)
        languages, subtitles, ambiguous, _kind = _category_evidence(name)
        if subtitles:
            counts["subtitleLabelExcluded"] += 1
        elif ambiguous and not languages:
            counts["ambiguousLabelExcluded"] += 1
        elif not languages:
            counts["unclassifiedLabelExcluded"] += 1
        for code in languages:
            selected[code].append((added, identifier, row, category))
    for code in TARGETS:
        selected[code] = sorted(selected[code], key=lambda entry: (-entry[0], int(entry[1]), entry[1]))[:5]
    return selected, counts


class ObservedSession(DeadlineSession):
    def __init__(self, *args):
        super().__init__(*args)
        self.redirect_flags = None

    def get(self, url, **kwargs):
        response = super().get(url, **kwargs)
        if self.redirect_flags is None and (response.is_redirect or response.is_permanent_redirect):
            self.redirect_flags = _redirect_constraints(url, response.headers.get("Location", ""), kwargs.get("params", {}))
        return response


def _identity_binding(payload, selected_id):
    values = []
    if isinstance(payload, Mapping):
        for container, aliases in ((payload, ("stream_id", "vod_id", "id")),
                (payload.get("movie_data", {}), ("stream_id", "vod_id", "id"))):
            if isinstance(container, Mapping):
                values.extend(container[key] for key in aliases if key in container)
    if not values:
        return "request_only_unverified"
    return "provider_id_matches" if all(type(value) in (str, int) and str(value) == selected_id for value in values) else "identity_mismatch"


def fetch_bulk(server_id, config, global_deadline):
    started = time.monotonic()
    server_deadline = min(global_deadline, started + PER_SERVER_SECONDS)
    stats = {"requests": 0}
    session = ObservedSession(min(started + 150, server_deadline - 10), stats, 10)
    budget = vod.DownloadBudget(limit=104 * 1024 * 1024)
    try:
        categories = vod._request_panel_json_once(session, config, "get_vod_categories", maximum_bytes=8 * 1024 * 1024, budget=budget)
        movies = vod._request_panel_json_once(session, config, "get_vod_streams", maximum_bytes=96 * 1024 * 1024, budget=budget)
        selected, counts = select_recent(categories, movies, deadline=min(started + 175, server_deadline))
        return {"status": "available", "selected": selected, "counts": counts, "deadline": server_deadline,
                "requests": stats["requests"], "downloadBytes": budget.consumed}
    except Exception as error:
        status = "deadline" if isinstance(error, TimeoutError) or time.monotonic() >= session.deadline else "schema_failure" if isinstance(error, ValueError) else _failure_code(error, server_id)
        return {"status": status, "selected": {code: [] for code in TARGETS}, "counts": {},
                "deadline": server_deadline, "requests": stats["requests"], "downloadBytes": budget.consumed,
                "redirectConstraints": session.redirect_flags}
    finally:
        session.close()


def fetch_detail(server_id, identifier, config, deadline, secrets):
    if time.monotonic() >= deadline - 10:
        return {"status": "deadline", "identityBinding": "not_received", "detailRecord": None}
    stats = {"requests": 0}
    session = ObservedSession(min(deadline - 10, time.monotonic() + 20), stats, 5)
    budget = vod.DownloadBudget(limit=512 * 1024)
    result = {"status": "unavailable", "identityBinding": "not_received", "detailRecord": None}
    try:
        payload = vod._request_panel_json_once(session, config, "get_vod_info", item_id=identifier,
                                              maximum_bytes=512 * 1024, budget=budget)
        if not isinstance(payload, Mapping):
            result["status"] = "schema_failure"
        else:
            binding = _identity_binding(payload, identifier)
            result["identityBinding"] = binding
            if binding == "identity_mismatch":
                result["status"] = binding
            else:
                safe = sanitize_record(payload, secrets)
                result.update(status="available" if binding == "provider_id_matches" else "received_unverified_identity",
                              detailRecord=safe.pop("record"), detailRecordAudit=safe)
    except Exception as error:
        result["status"] = "deadline" if time.monotonic() >= session.deadline else _error_category(error, server_id)
    finally:
        result.update(requests=stats["requests"], downloadBytes=budget.consumed, redirectConstraints=session.redirect_flags)
        session.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "false").strip().casefold()
        if allow_http not in {"true", "false"}:
            raise ValueError()
        configs, failures = {}, {}
        for server_id in SERVERS:
            try:
                configs[server_id] = vod.load_server_config(server_id, allow_insecure_http=allow_http == "true")
            except Exception:
                failures[server_id] = "configuration"
        secrets = _secret_values(configs.values())
        deadline = time.monotonic() + GLOBAL_SECONDS
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {server_id: executor.submit(fetch_bulk, server_id, config, deadline) for server_id, config in configs.items()}
            bulk = {server_id: future.result() for server_id, future in futures.items()}
        jobs = {(server_id, entry[1]): entry for server_id, data in bulk.items()
                for selected in data["selected"].values() for entry in selected}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {key: executor.submit(fetch_detail, key[0], key[1], configs[key[0]],
                       min(deadline, bulk[key[0]]["deadline"]), secrets) for key in jobs}
            details = {key: future.result() for key, future in futures.items()}
        servers = {}
        for server_id in SERVERS:
            data = bulk.get(server_id, {"status": failures.get(server_id, "configuration"), "counts": {},
                                      "selected": {code: [] for code in TARGETS}})
            languages = {}
            for code in TARGETS:
                samples = []
                for _added_seconds, identifier, row, category in data["selected"][code]:
                    safe = sanitize_record(row, secrets)
                    sample = {"listRecord": safe.pop("record"), "listRecordAudit": safe,
                              "categoryRecord": sanitize_record(category, secrets),
                              **details[(server_id, identifier)]}
                    samples.append(sample)
                languages[code] = {"requested": 5, "selected": len(samples), "samples": samples}
            servers[server_id] = {"status": data["status"], "selectionCounts": data["counts"],
                                  "languages": languages, "bulkRequests": data.get("requests", 0),
                                  "bulkDownloadBytes": data.get("downloadBytes", 0),
                                  "bulkRedirectConstraints": data.get("redirectConstraints")}
        report = {"schemaVersion": 1, "capturedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "filteredProviderMetadata": True, "fullRawPayloadExported": False, "audioAvailabilityVerified": False,
            "selectionUsesProviderAddedOnly": True, "selectionLanguageEvidence": "explicit_category_labels",
            "serverCount": 3, "uniqueDetailRequests": len(jobs), "servers": servers}
        raw = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = vod._temporary_path(args.output.parent, "recent-provider-samples")
        try:
            temporary.write_bytes(raw + b"\n")
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"serverCount": 3, "selectedSamples": sum(value["selected"] for server in servers.values() for value in server["languages"].values()),
                          "uniqueDetailRequests": len(jobs)}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"success": False, "status": "diagnostic_failure"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
