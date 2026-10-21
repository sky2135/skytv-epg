"""Replay public VOD identities against TMDB without publishing or provider calls.

Reports describe the current replay. Historic no_match cache entries do not
retain enough evidence to prove which branch rejected an earlier response.
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from vod_tmdb import (TmdbClient, _date, _detail_identity, _enrichment_identity,
                      _identifier, _identity_matches, _integer, _normalized_title, _text)


def public_candidate(value):
    """Only bounded movie identity fields; never request URLs or response extras."""
    if not isinstance(value, dict):
        return {}
    result = {"tmdbId": _identifier(value.get("id")),
              "title": _text(value.get("title")),
              "originalTitle": _text(value.get("original_title")),
              "releaseDate": _date(value.get("release_date")),
              "originalLanguage": _text(value.get("original_language"), 3)}
    if result["tmdbId"]:
        result["sourceURL"] = "https://www.themoviedb.org/movie/" + result["tmdbId"]
    return result


class TraceClient(TmdbClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace = []
        self.phase = "matcher"

    def _request(self, route, params=None):
        state, body = super()._request(route, params)
        row = {"phase": self.phase, "kind": "search" if route == "/search/movie" else "details", "state": state}
        if state == "ok" and isinstance(body, dict):
            if row["kind"] == "search":
                results = body.get("results")
                row.update(totalPages=_integer(body.get("total_pages", 1), 0, 100_000),
                           totalResults=_integer(body.get("total_results"), 0, 10_000_000),
                           candidates=[public_candidate(x) for x in results[:100]] if isinstance(results, list) else [])
            else:
                row["candidate"] = public_candidate(body)
                row["identity"] = _detail_identity(body, "movie")
        self.trace.append(row)
        return state, body


def exact_candidates(candidates, title, year):
    normalized = _normalized_title(title)
    same_name = [x for x in candidates if normalized in {
        _normalized_title(x.get("title")), _normalized_title(x.get("originalTitle"))}]
    exact = [x for x in same_name if x.get("tmdbId") and x.get("releaseDate")
             and x["releaseDate"][:4] == year]
    return same_name, {x["tmdbId"] for x in exact}


def rejection_reason(metadata, trace, title, year):
    status = metadata.get("matchStatus")
    if str(status).startswith("matched_"):
        return "matched_on_replay"
    if status == "unavailable":
        return "lookup_unavailable"
    searches = [x for x in trace if x["phase"] == "matcher" and x["kind"] == "search"]
    details = [x for x in trace if x["phase"] == "matcher" and x["kind"] == "details"]
    if searches:
        search = searches[0]
        if search["state"] != "ok":
            return "lookup_unavailable"
        pages = search.get("totalPages")
        if pages not in {0, 1}:
            return "multiple_search_pages"
        candidates = search["candidates"]
        same_name, exact = exact_candidates(candidates, title, year)
        if len(exact) > 1:
            return "multiple_exact_title_year_matches"
        if not exact:
            if not candidates:
                return "no_search_results"
            if any(x.get("releaseDate") and x["releaseDate"][:4] != year for x in same_name):
                return "same_title_different_primary_year"
            if same_name:
                return "same_title_missing_full_date"
            return "returned_titles_do_not_match"
    if details:
        detail = details[-1]
        if detail["state"] == "not_found":
            return "details_not_found"
        if detail["state"] == "ok" and not _identity_matches(detail.get("identity", {}), title, year):
            return "details_identity_conflict"
    return "identity_not_usable_or_unexplained"


def diagnose(document, client, *, max_seconds=600, yearless=True):
    if not math.isfinite(max_seconds) or not 1 <= max_seconds <= 900:
        raise ValueError("Diagnostic time budget is invalid")
    if not isinstance(document, dict):
        raise ValueError("Diagnostic input must be an object")
    records = document.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 500:
        raise ValueError("Diagnostic input requires 1 to 500 public identities")
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("Diagnostic records must be objects")
    ids = [row.get("contentId") for row in records]
    if any(not isinstance(x, str) or not re.fullmatch(r"server_1:movie:[0-9]{1,10}", x) for x in ids):
        raise ValueError("Diagnostic content IDs are invalid")
    if len(ids) != len(set(ids)):
        raise ValueError("Diagnostic content IDs are invalid or duplicated")
    for row in records:
        item = row.get("matcherInput")
        if (not isinstance(item, dict) or item.get("contentId") != row["contentId"]
                or str(item.get("id")) != row["contentId"].rsplit(":", 1)[1]
                or not isinstance(item.get("title"), str) or not item["title"].strip()
                or "://" in item["title"] or not isinstance(row.get("providerTitle"), str)
                or "://" in row["providerTitle"]):
            raise ValueError("Diagnostic input identity is inconsistent")
    started = time.monotonic()
    client.reset_elapsed_budget(max_seconds)
    output, prior = [], {}
    stopped = "complete"
    for row in records:
        if time.monotonic() - started >= max_seconds or client.stats["requests"] >= client.max_requests:
            stopped = "budget"
            break
        item = row.get("matcherInput")
        key, _identifier_value, title, year = _enrichment_identity(item, "movie")
        if key in prior:
            result = copy.deepcopy(prior[key])
            result["reusedDiagnosticQuery"] = True
        else:
            client.trace = []
            client.phase = "matcher"
            metadata = client.enrich(item, "movie")
            reason = rejection_reason(metadata, client.trace, title, year)
            result = {"searchTitle": title, "searchYear": year, "reason": reason,
                      "replayStatus": metadata.get("matchStatus"), "replayTmdbId": metadata.get("tmdbId"),
                      "replayReleaseDate": metadata.get("releaseDate"), "reusedDiagnosticQuery": False}
            if yearless and reason not in {"matched_on_replay", "lookup_unavailable", "multiple_exact_title_year_matches"}:
                client.phase = "yearless_review"
                client._request("/search/movie", {"query": title, "include_adult": "true", "page": 1})
            result["trace"] = copy.deepcopy(client.trace)
            search = next((t for t in client.trace if t["phase"] == "matcher" and t["kind"] == "search"), None)
            if search:
                _same, exact = exact_candidates(search.get("candidates", []), title, year)
                result["exactTitleYearIdsOnFirstPage"] = sorted(exact)
                result["searchTotalPages"] = search.get("totalPages")
                result["searchTotalResults"] = search.get("totalResults")
            prior[key] = copy.deepcopy(result)
        output.append({"row": row.get("row"), "contentId": row["contentId"],
                       "providerTitle": row.get("providerTitle"), "publishedStatus": row.get("publishedStatus"), **result})
    return {"schemaVersion": 1, "mode": "read_only_vod_match_diagnostic",
            "queriedAt": datetime.now(timezone.utc).isoformat(), "snapshotGeneratedAt": document.get("snapshotGeneratedAt"),
            "interpretation": "Current TMDB replay, not proof of the historical API response. No metadata was published or corrected.",
            "sourceProof": document.get("sourceProof"), "inputCount": len(records), "recordCount": len(output),
            "uniqueQueries": len(prior), "stoppedReason": stopped,
            "reasonCounts": dict(collections.Counter(row["reason"] for row in output)),
            "stats": {k: client.stats.get(k, 0) for k in ("requests", "cacheHits", "cacheMisses", "errors", "retries", "cacheWriteErrors")},
            "records": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=900)
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    if not 0 <= args.max_requests <= 2000 or not math.isfinite(args.max_seconds) or not 1 <= args.max_seconds <= 900:
        raise ValueError("Diagnostic budgets are invalid")
    if args.input.stat().st_size > 1024 * 1024:
        raise ValueError("Diagnostic input exceeds size limit")
    token, api_key = os.environ.get("TMDB_READ_ACCESS_TOKEN", ""), os.environ.get("TMDB_API_KEY", "")
    if not (token or api_key):
        raise ValueError("TMDB application credential is not configured")
    raw = args.input.read_bytes()
    document = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix="skytv-match-diagnostic-") as cache:
        with TraceClient(Path(cache), token=token, api_key=api_key, max_requests=args.max_requests,
                         max_elapsed_seconds=args.max_seconds) as client:
            report = diagnose(document, client, max_seconds=args.max_seconds)
    report["inputSha256"] = hashlib.sha256(raw).hexdigest()
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if any(secret and (secret in serialized or json.dumps(secret)[1:-1] in serialized) for secret in (token, api_key)):
        raise ValueError("Diagnostic output failed credential safety validation")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("recordCount", "uniqueQueries", "stoppedReason", "reasonCounts", "stats")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError):
        print("VOD match diagnostic failed; no publication was attempted.", file=sys.stderr)
        raise SystemExit(2) from None
