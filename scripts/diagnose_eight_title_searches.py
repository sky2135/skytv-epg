"""One-off, read-only comparison of eight provider identities and TMDB searches.

Production metadata and retry state are read only. Matcher replays write only
to a temporary directory. This script never contacts an IPTV provider.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import build_skytvplayer_catalog as app
from diagnose_vod_matches import TraceClient, public_candidate
from vod_provider_evidence import MovieEvidenceCollector
from vod_resolution_queue import ResolutionQueue
from vod_tmdb import (MATCH_STRATEGY, MAX_CACHE_BYTES, POSTER_POLICY, TmdbClient,
                      _date, _detail_identity, _enrichment_identity, _identifier,
                      _integer, _match_diagnostic, _normalized_title, _text,
                      official_paths, poster_identity)

MAX_INPUT_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
CONTENT_ID = re.compile(r"server_[1-9][0-9]?:movie:[0-9]{1,10}\Z")


def _timestamp(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 4_102_444_800):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _candidate(value):
    candidate = public_candidate(value)
    return {key: candidate.get(key) for key in
            ("tmdbId", "title", "originalTitle", "releaseDate", "originalLanguage", "sourceURL")}


def _trace(rows):
    result = []
    for row in rows[:30]:
        event = {key: row.get(key) for key in ("phase", "kind", "state")}
        if row.get("kind") == "search":
            event.update({key: row.get(key) for key in
                          ("requestedPage", "responsePage", "totalPages", "totalResults")})
            event["candidates"] = row.get("candidates", [])[:20]
        else:
            event["candidate"] = row.get("candidate")
            identity = row.get("identity", {})
            event["identity"] = {key: identity.get(key) for key in
                                 ("tmdbId", "year", "originalTitle")}
            event["identity"]["names"] = identity.get("names", [])[:25]
        result.append(event)
    return {"events": result, "truncatedEvents": max(0, len(rows) - len(result))}


def _expiry(client, key):
    """Read only envelope version/expiry after the normal cache reader accepted it."""
    path = client._cache_path(key)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        raw = path.read_bytes()
        if len(raw) > MAX_CACHE_BYTES:
            return None
        envelope = json.loads(raw)
        if isinstance(envelope, dict) and envelope.get("version") == 1:
            return _timestamp(envelope.get("expiresAt"))
    except (OSError, ValueError, TypeError):
        pass
    return None


def _cached_observation(client, key):
    value, fresh = client._read_cache(key)
    if not isinstance(value, dict):
        return {"present": False}
    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else value
    diagnostic = _match_diagnostic(value.get("matchDiagnostic"))
    status = metadata.get("matchStatus")
    return {"present": True, "fresh": bool(fresh), "expiresAt": _expiry(client, key),
            "matchStatus": status if status in {"matched_id", "matched_search", "matched_poster", "no_match", "unavailable"} else None,
            "tmdbId": _identifier(metadata.get("tmdbId")),
            "reason": diagnostic["reason"], "candidateIds": diagnostic["candidateIds"],
            "diagnosticAvailable": isinstance(value.get("matchDiagnostic"), dict),
            "matchStrategy": _integer(value.get("matchStrategy"), 1, 1_000_000)}


def _production_observation(client, queue, item):
    key, identifier, title, year = _enrichment_identity(item, "movie")
    metadata, fresh, rejected = client._identity_cache(key, item, "movie", identifier, title, year)
    result = {"validatedCacheAvailable": metadata is not None, "fresh": bool(fresh),
              "rejectedIdentity": bool(rejected),
              "validatedMatchStatus": metadata.get("matchStatus") if metadata else None,
              "validatedTmdbId": metadata.get("tmdbId") if metadata else None,
              "currentKey": _cached_observation(client, key)}
    marker = f":poster-v{POSTER_POLICY}:"
    if marker in key:
        result["prePosterKeyObservation"] = _cached_observation(client, key.rsplit(marker, 1)[0])
        result["prePosterKeyInterpretation"] = "Historical cache evidence only; not an approved identity or a replay result."
    queue_key = queue._by_content.get(item["contentId"])
    queued = queue.entries.get(queue_key)
    if queued:
        result["queueRecord"] = {key: queued[key] for key in
                                 ("reason", "attempts", "failureStreak", "candidateIds")}
        result["queueRecord"].update({key: _timestamp(queued[key]) for key in
                                     ("firstSeenAt", "lastSeenAt", "lastAttemptAt", "nextAttemptAt")})
        result["queueRecord"]["backoffActive"] = queue.defer_cached_miss(
            item["contentId"].split(":", 1)[0], "movie", item)
    else:
        result["queueRecord"] = None
    return result


def _public_details(body):
    result = _candidate(body)
    aliases = []
    alternatives = body.get("alternative_titles", {})
    rows = alternatives.get("titles", alternatives.get("results", [])) if isinstance(alternatives, dict) else []
    for row in rows[:100] if isinstance(rows, list) else []:
        name = _text(row.get("title")) if isinstance(row, dict) else None
        if name and name not in aliases:
            aliases.append(name)
    paths, complete = official_paths(body)
    result.update(officialAliasNames=aliases, posterPath=body.get("poster_path") if body.get("poster_path") in paths else None,
                  officialPosterPaths=sorted(paths), alternatePostersComplete=complete)
    external = body.get("external_ids", {})
    imdb = external.get("imdb_id") if isinstance(external, dict) else None
    result["imdbId"] = imdb if isinstance(imdb, str) and re.fullmatch(r"tt[0-9]{7,10}", imdb) else None
    releases = body.get("release_dates", {})
    countries = releases.get("results", []) if isinstance(releases, dict) else []
    dates = []
    for country in countries[:100] if isinstance(countries, list) else []:
        if not isinstance(country, dict):
            continue
        code = country.get("iso_3166_1")
        code = code if isinstance(code, str) and re.fullmatch(r"[A-Z]{2}", code) else None
        entries = country.get("release_dates", [])
        for release in entries[:20] if isinstance(entries, list) else []:
            if not isinstance(release, dict):
                continue
            date_value = release.get("release_date")
            date_value = _date(date_value[:10]) if isinstance(date_value, str) else None
            record = {"countryCode": code, "date": date_value,
                      "type": _integer(release.get("type"), 1, 6)}
            if date_value and record not in dates and len(dates) < 100:
                dates.append(record)
    result["alternativeReleaseDates"] = dates
    return result


def _comparison(item, public):
    _key, _identifier_value, title, year = _enrichment_identity(item, "movie")
    names = [public.get("title"), public.get("originalTitle"), *public.get("officialAliasNames", [])]
    path = poster_identity(item)
    return {"matcherTitleIsOfficialTitleOrAlias": _normalized_title(title) in {
                _normalized_title(name) for name in names if name},
            "matcherYear": year, "primaryReleaseYear": (public.get("releaseDate") or "")[:4] or None,
            "matcherYearMatchesPrimaryReleaseYear": bool(year and (public.get("releaseDate") or "")[:4] == year),
            "providerPosterPath": path,
            "providerPosterEqualsOfficialPoster": bool(path and path in public.get("officialPosterPaths", []))}


def _direct_search(client, query, *, adult=False):
    client.phase = "direct_name_search_adult_inclusive" if adult else "direct_name_search_website_like"
    before = len(client.trace)
    params = {"query": query, "language": "en-US", "include_adult": "true" if adult else "false", "page": 1}
    state, body = client._request("/search/movie", params)
    result = {"query": query, "yearFilter": None, "language": "en-US", "includeAdult": adult,
              "state": state, "trace": _trace(client.trace[before:])}
    if state == "ok" and isinstance(body, dict):
        rows = body.get("results")
        result.update(totalResults=_integer(body.get("total_results"), 0, 10_000_000),
                      totalPages=_integer(body.get("total_pages"), 0, 100_000),
                      first20=[_candidate(row) for row in rows[:20]] if isinstance(rows, list) else [])
    return result


def _validate(document):
    if not isinstance(document, dict) or not isinstance(document.get("records"), list) or not 1 <= len(document["records"]) <= 8:
        raise ValueError("Input requires one to eight records")
    seen, probes = set(), set()
    for record in document["records"]:
        if not isinstance(record, dict):
            raise ValueError("Invalid record")
        content_id = record.get("contentId")
        item = record.get("matcherInput")
        if (not isinstance(content_id, str) or not CONTENT_ID.fullmatch(content_id) or content_id in seen
                or not isinstance(item, dict) or item.get("contentId") != content_id
                or str(item.get("id")) != content_id.rsplit(":", 1)[1]):
            raise ValueError("Inconsistent movie identity")
        for value in (record.get("providerTitle"), record.get("directQuery"), item.get("title")):
            if not isinstance(value, str) or not value.strip() or len(value) > 300 or "://" in value or any(ord(character) < 32 for character in value):
                raise ValueError("Invalid public title")
        detail_ids = record.get("detailProbeIds", [])
        if not isinstance(detail_ids, list) or len(detail_ids) > 4 or any(_identifier(value) != str(value) for value in detail_ids):
            raise ValueError("Invalid discovery detail IDs")
        probes.update(str(value) for value in detail_ids)
        seen.add(content_id)
    if len(probes) > 4:
        raise ValueError("Too many discovery detail IDs")


def diagnose(document, client, production, production_cache, *, compare_adult=False):
    _validate(document)
    collector = MovieEvidenceCollector(production_cache / "provider-evidence", configs={}, max_requests=0, max_seconds=0)
    queue = ResolutionQueue(production_cache / "resolution-queue.json", policy_version=MATCH_STRATEGY)
    details_cache, output = {}, []
    total_limit = client.max_requests

    def details(identifier, phase):
        if identifier in details_cache:
            return dict(details_cache[identifier], reusedDiagnosticDetails=True)
        client.phase = phase
        before = len(client.trace)
        state, body = client._request(f"/movie/{identifier}", {
            "append_to_response": "alternative_titles,images,release_dates,external_ids", "language": "en-US"})
        result = {"state": state, "reusedDiagnosticDetails": False, "trace": _trace(client.trace[before:])}
        if state == "ok" and isinstance(body, dict) and _identifier(body.get("id")) == identifier:
            result["identity"] = _public_details(body)
        details_cache[identifier] = result
        return result

    for position, record in enumerate(document["records"]):
        item = app._matching_item(record["matcherInput"], record.get("rawProviderRecord", {}), "movie", collector)
        _key, identifier, title, year = _enrichment_identity(item, "movie")
        observed = _production_observation(production, queue, item)
        client.trace, client.phase = [], "fresh_actual_matcher_replay"
        # Reserve bounded room for the explicit searches and public detail
        # probes. Replay is isolated from the restored production cache.
        remaining = len(document["records"]) - position
        reserve = remaining * (3 if compare_adult else 2) + 4
        client.max_requests = max(client.stats["requests"], total_limit - reserve)
        metadata = client.enrich(item, "movie")
        diagnostic = _match_diagnostic(client.last_match_diagnostic)
        replay = {"matchStatus": metadata.get("matchStatus"), "tmdbId": metadata.get("tmdbId"),
                  "releaseDate": metadata.get("releaseDate"), "diagnostic": diagnostic,
                  "trace": _trace(client.trace)}
        client.max_requests = total_limit
        direct = _direct_search(client, record["directQuery"])
        result = {"contentId": record["contentId"], "providerTitle": record["providerTitle"],
                  "publishedStatus": _text(record.get("publishedStatus"), 40),
                  "matcherInputIdentity": {"searchTitle": title, "searchYear": year, "providerTmdbId": identifier,
                                           "posterPath": poster_identity(item), "recordBoundEvidenceAvailable": bool(item.get("_identityEvidence"))},
                  "productionCache": observed, "freshMatcherReplay": replay, "directSearch": direct}
        unique = direct.get("first20", [])
        if direct.get("totalResults") == 1 and len(unique) == 1 and unique[0].get("tmdbId"):
            detail = details(unique[0]["tmdbId"], "unique_direct_result_details")
            result["uniqueDirectResultDetails"] = detail
            if "identity" in detail:
                result["uniqueDirectResultComparison"] = _comparison(item, detail["identity"])
        if compare_adult:
            result["adultInclusiveDirectSearch"] = _direct_search(client, record["directQuery"], adult=True)
        probes = []
        for probe in record.get("detailProbeIds", []):
            detail = details(str(probe), "explicit_discovery_candidate_details")
            probe_result = {"tmdbId": str(probe), "role": "discovery_candidate_not_verified_provider_match", "details": detail}
            if "identity" in detail:
                probe_result["comparison"] = _comparison(item, detail["identity"])
            probes.append(probe_result)
        result["discoveryDetailProbes"] = probes
        observations = []
        if _normalized_title(title) != _normalized_title(record["directQuery"]):
            observations.append("direct_query_differs_from_actual_matcher_title")
        if not year:
            observations.append("actual_matcher_release_year_missing")
        queued = observed.get("queueRecord")
        if queued and queued.get("backoffActive"):
            observations.append("production_retry_backoff_active")
        comparison = result.get("uniqueDirectResultComparison")
        if comparison:
            if not comparison["matcherTitleIsOfficialTitleOrAlias"]:
                observations.append("unique_result_does_not_corroborate_actual_matcher_title")
            if not comparison["matcherYearMatchesPrimaryReleaseYear"]:
                observations.append("unique_result_primary_release_year_does_not_match")
            if comparison["providerPosterPath"] and not comparison["providerPosterEqualsOfficialPoster"]:
                observations.append("provider_poster_not_found_in_official_posters")
        if str(replay["matchStatus"]).startswith("matched_") and record.get("publishedStatus") == "no_match":
            observations.append("fresh_replay_now_matches_despite_published_no_match")
        result["observations"] = observations
        output.append(result)
    return {"schemaVersion": 1, "mode": "read_only_eight_title_search_diagnostic",
            "queriedAt": datetime.now(timezone.utc).isoformat(), "matcherStrategy": MATCH_STRATEGY,
            "productionCacheKey": _text(os.environ.get("PRODUCTION_CACHE_KEY"), 200),
            "interpretation": "Direct search counts are authoritative TMDB API totals for the explicit query and filters, not identity approval. Fresh isolated matcher replay, restored cache history, and explicit discovery probes are separate evidence. A unique search result still requires exact current title/alias, release-year and applicable poster/evidence checks. Cached history does not prove an earlier API response. No production cache, queue, provider or publication was modified.",
            "recordCount": len(output), "reasonCounts": dict(collections.Counter(
                row["freshMatcherReplay"]["diagnostic"]["reason"] for row in output)),
            "stats": {key: client.stats.get(key, 0) for key in
                      ("requests", "cacheHits", "cacheMisses", "errors", "retries", "budgetExhausted", "elapsedBudgetExhausted")},
            "records": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--production-cache", type=Path, default=Path(".build/vod-cache"))
    parser.add_argument("--compare-adult", action="store_true")
    args = parser.parse_args(argv)
    if args.input.is_symlink() or args.input.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Diagnostic input exceeds its bound")
    raw = args.input.read_bytes()
    document = json.loads(raw)
    _validate(document)
    token, api_key = os.environ.get("TMDB_READ_ACCESS_TOKEN", ""), os.environ.get("TMDB_API_KEY", "")
    if not (token or api_key):
        raise ValueError("TMDB credential is not configured")
    with tempfile.TemporaryDirectory(prefix="skytv-eight-search-") as temporary:
        with TmdbClient(args.production_cache / "tmdb", max_requests=0) as production, \
                TraceClient(Path(temporary), token=token, api_key=api_key,
                            max_requests=200, max_elapsed_seconds=180) as replay:
            report = diagnose(document, replay, production, args.production_cache, compare_adult=args.compare_adult)
    report["inputSha256"] = hashlib.sha256(raw).hexdigest()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(rendered.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("Diagnostic report exceeds its bound")
    protected = [token, api_key, *[value for name, value in os.environ.items()
                  if name.startswith("SERVER_") and name.endswith(("_PASSWORD", "_USERNAME", "_BASE_URL"))]]
    if any(secret and (secret in rendered or json.dumps(secret)[1:-1] in rendered) for secret in protected):
        raise ValueError("Diagnostic report failed credential safety validation")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("recordCount", "reasonCounts", "stats")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("Eight-title diagnostic failed; production data was not modified.", file=sys.stderr)
        raise SystemExit(2) from None
