# Optimization audit — Smart Rules v7.0 / Builder v7.1

## Scope

Source notebook:

`SKYTV_EPG_3_Servers_TiviMate_Smart_Rules_v7_1_BUILD_FIXED(1).ipynb`

The goal was to improve runtime and deployment efficiency without changing any
channel-to-EPG matching decision.

## Frozen code boundary

The original notebook's two engine cells are preserved verbatim in the
optimized notebook. `src/skytv_epg_engine.py` is those two cells joined by one
newline, with no edits.

The SHA-256 values are recorded in `MATCHER_INTEGRITY.json`. The regression test
recalculates the engine hash before it runs.

The following remain unchanged:

- Smart Rules v7 aliases, regular expressions, direct mappings, and dummy rules;
- region detection and candidate eligibility;
- normalization and Unicode/mojibake repair;
- scores, thresholds, margins, and tie-breaking;
- panel EPG quality acceptance;
- East/West, number, call-sign, event, adult, sports, and provider safety rules;
- canonical ID conflict resolution;
- XMLTV programme filtering, output IDs, validation, and refresh calculation.

## Inefficiencies found

### 1. The same v7 candidate indexes were prepared twice in Stage 1

`run_smart_rules_v7_self_test(...)` prepares the catalog-backed indexes. The
real `make_matching_report_v7(...)` call immediately prepared the same indexes
again from the same candidate and dummy catalogs.

### 2. Independent EPGShare text catalogs were downloaded sequentially

The final downloader visited each configured `.txt` catalog one at a time even
though the requests are independent.

### 3. A three-server scheduled build would redownload shared XML sources

Calling the original builder separately for three servers causes each server to
download any shared EPGShare `.xml.gz` feed again.

### 4. The notebook mixes interactive matching with unattended production work

A scheduled job does not need panel inventory APIs, fuzzy matching, review
queues, or Colab upload prompts. Running all of that unattended also creates a
quality risk.

## Changes made outside the frozen algorithm

### Exact-catalog matcher index reuse

A BLAKE2 fingerprint covers the complete ordered real-candidate catalog and the
complete dummy-ID map. When the fingerprint is unchanged, the second prepare
call is skipped and the already-built v7 indexes are reused. Any content or
order change causes a normal rebuild.

### Deterministic parallel text-catalog downloads

Each configured text catalog is downloaded in its own session through a bounded
thread pool. Results are then processed in the original source-registry order.
Candidate order, duplicate removal, and tie-breaking therefore remain the same.

### Shared XML download cache for the three-server builder

`scripts/build_all_servers.py` first determines the union of source feeds used
by all reviewed mappings. It downloads every distinct EPGShare XML file once,
downloads each required panel XML once, validates them, and lets the original
builder copy those files into each server work directory.

### Production path uses reviewed mappings only

The GitHub workflow calls only Builder v7.1 with
`mappings/server_X_final_mapping.csv`. It never calls the matching engine. New
or renamed channels must be reviewed in Colab before the committed mapping is
changed.

### Reproducible environment and early failure

Dependencies are pinned, engine integrity is checked, all 54 built-in v7 cases
run before each scheduled build, missing mappings and secrets fail clearly, and
Pages is deployed only after all three files pass the original XML/gzip
validation.

## Regression evidence

`tests/test_matcher_regression.py` verifies:

1. the frozen engine SHA-256 matches `MATCHER_INTEGRITY.json`;
2. the original engine and optimized wrapper both pass all 54 v7 self-tests;
3. the complete returned DataFrames are equal, column by column and row by row;
4. a second run with the exact same catalog records one index build and at
   least one reuse;
5. parallel catalog downloads return candidates and dummy IDs in exactly the
   same order/content as the original sequential downloader, even when later
   feeds finish first.

## Deliberate non-changes

Historical v4-v6 definitions remain inside the frozen engine because the final
v7 layer captures and reuses selected earlier functions. Removing or rewriting
those layers would make the code smaller, but would create an unnecessary risk
of changing behavior. The safe production speedup comes from avoiding repeated
work and separating matching from scheduled building, not from rewriting the
proven matcher.

## Expected effect

- Stage 1 no longer rebuilds the same catalog indexes for the self-test and real
  scan.
- Text-catalog network time is bounded more by the slowest group of requests
  than by the sum of every request, subject to provider and runner conditions.
- During a three-server refresh, each distinct EPGShare XML source is downloaded
  once instead of up to three times.
- Scheduled runs skip all inventory and fuzzy-matching work and operate only on
  approved mappings.

No fixed percentage speed claim is made because the largest component depends
on network latency, catalog size, mapping composition, and the number of panel
sources used in a particular run.
