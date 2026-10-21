# Investigating unmatched movie metadata

The manual **Inspect unmatched VOD metadata** workflow replays a reviewed public
input snapshot against the existing TMDB identity matcher. It uses the existing
TMDB application credential, at most 900 HTTP requests and 600 seconds, and an
isolated temporary cache. Its only retained output is a diagnostic JSON artifact.
It does not collect provider inventories, restore or change the production cache,
edit movie metadata, write to Sheets, or deploy Pages.

The October 21 snapshot covers all 154 Server 1 English-tagged current-year
records left as `no_match` after the release-date backfill. The snapshot joins
each app record to the same publication's basic VOD row, preserving its original
TMDB identifier and title/year inputs. All 154 basic identifiers and release
dates are null. Two records share the same normalized query, giving 153 queries.
Source hashes and generation timestamps are retained with the input.

The report records the existing matcher's current decision, the bounded public
identity fields returned by its search/detail calls, and an optional yearless
search for manual review. Credentials, request URLs, full API response bodies,
synopses and playback URLs are excluded. Duplicate queries retain separate
provider records while sharing the diagnostic result.

`no_match` is intentionally generic in the production cache. The earlier
response and rejection branch were not retained, so a replay reason must not be
presented as proof of the historical response. A replay distinguishes multiple
search pages, multiple exact title/year candidates, different primary release
years, missing full dates, different returned titles, empty results and detail
identity conflicts. None of these alone establishes that a film does not exist.
Supplementary yearless results are candidates, never automatic corrections.

Run against a reviewed local public snapshot:

```text
python scripts/diagnose_vod_matches.py --input data/diagnostics/server1-unmatched-2026-10-21.json --output .build/vod-match-diagnostics/report.json
```
