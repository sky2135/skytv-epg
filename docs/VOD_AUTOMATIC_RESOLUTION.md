# Automatic VOD resolution and publishing efficiency

The daily publication now handles unresolved Server 1 English-tagged current-year
movies as a persistent, bounded queue. Playback IDs, catalogue schemas, full
publication validation and the frozen live-channel matching engine are preserved.

## Identity matching

- Preserve identity-bearing colon prefixes such as `BTS:`, `NFT:` and `IT:`.
  Only established locale/technical decorations are removed.
- Inspect at most eight complete search pages. Deduplicate TMDB IDs, reject
  inconsistent/partial responses, and stop early when ambiguity is established.
  Reaching a cap or transport failure is deferred rather than proof of no match.
- Inspect official alternative-title proof for at most three eligible candidates.
  An alias or evidence-assisted acceptance requires checking every eligible
  candidate in that complete bounded search set.
- A record-bound provider IMDb ID first uses TMDB `/find` in the correct movie
  or TV lane. A unique ID must agree with any supplied/reviewed ID, and details
  must corroborate the same IMDb ID, full title or official alias, and year.
  Failed, malformed or contradictory discovery defers; only an empty correct-kind
  result falls back to ordinary title search. Verified old positives survive
  transient discovery failures.
- Optional provider facts can distinguish a small ambiguous candidate set using
  the same IMDb ID, or a matching director plus two distinct cast members. The
  display title/year must still pass; runtime can veto but cannot select a film.
  Popularity, artwork and English provider labels never choose an identity.
- Evidence-assisted caches additionally bind the provider content ID, full
  display title, source fingerprint and a derived digest of the actual facts.
  Old negative results retry under strategy 3; verified positive caches remain
  reusable under the existing identity proof policy.

## Daily queue and targeted details

The existing prewarm budget remains 3,000 TMDB HTTP attempts / 600 seconds,
with at most 300 due entries selected in fair rotating order. Fresh successful
metadata is reused without placing the whole library into the queue.

The workflow explicitly enables at most 40 optional provider HTTP attempts
(including redirects), 60 seconds and 16 MiB of detail downloads. It asks only
for unresolved `get_vod_info` IDs, not every movie/series. Existing credential,
host, redirect and transport rules apply. Raw responses, provider URLs, playback
addresses and credentials are not retained. Optional lookup failures leave the
normal inventory publication usable.

Private state lives in `.build/vod-cache/resolution-queue.json` and
`.build/vod-cache/provider-evidence/`, covered by the existing incremental cache.
Stable unresolved reasons retry after 7, 14, then 28 days; unavailable lookups
after 1, 2, 4, then 7 days; missing identity after 30 days. Changed identity,
provider facts or strategy resets scheduling. The normal app publisher also
honors the same backoff when a validated negative cache exists. It never blocks
an uncached item or suppresses a verified positive result.

Sanitized CSV/JSON reports are uploaded as `vod-resolution-RUN-ATTEMPT` artifacts
with seven-day retention. They identify the title, reason, candidate TMDB IDs
and next attempt. Candidate links are references, not accepted identities.
Uncertain primary release years and possible TV specials stay unresolved with
an explicit reason. Yearless diagnostic candidates and regional release dates
are not silently converted into a different global release date or media type.

## Efficiency changes and evidence

The measured pre-change successful build was 49m42s plus 1m50s deployment.
Its largest steps were channel synchronization 19m33s, app generation 9m39s,
live output 7m10s and current-year prewarm 3m33s. Cache restore/save took seconds.

- Fetch and validate VOD inventories immediately after offline tests, before
  expensive EPG processing. Prepared public catalogues and private raw snapshots
  stay separate; after the EPG builder atomically replaces its output tree, the
  prepared catalogue's exact referenced bytes are installed with its index last.
  This detects provider inventory failures before the long guide build and
  avoids downloading or compressing the same VOD inventory again.
  An explicitly empty movie/series list is confirmed once after one second,
  using the same session and cumulative download budget. Persistent empty,
  malformed and authentication failures still stop publication safely.
- Refresh the three independent episode providers concurrently with separate
  sessions/cache paths and fair quotas. Reallocate unused quota only within the
  original aggregate request cap and each provider's remaining active-I/O time.
  Carry first-pass inventories in memory so an oversized optional cache cannot
  cause duplicate requests or discard a successful inventory.
- Reuse the exact already-serialized/compressed accepted shard bytes rather
  than encoding and compressing them again for installation.
- Reuse prepared immutable live-channel candidate semantics in verification and
  advisory passes. A saved 27,475-candidate comparison was identical for every
  candidate: 9.584s reparsing versus 0.256s prepared lookup. This is a local
  microbenchmark, not a promised end-to-end workflow speedup.

The publisher enables `--tmdb-workers 3` (CLI default: 1). The six movie/series
groups use isolated clients and sessions, fixed fair shares of the remaining
1,500-request cap after trending, and 45 seconds of active I/O per group.
One deterministic owner handles each physical cache key; different reviewed
constraints and bound evidence remain distinct. Fixed shares are not rolled
over, so unused requests are reported as `parallelUnusedRequests`. All sessions
share a 20-request/second pace and bounded HTTP 429 cooldown. Normalization is
prepared once, all enrichment joins before sequential serialization/validation,
and only a fully validated publication switches the public index. All ordinary
identity, checksum, secret, size, full-server and final Pages gates remain.
Measure deployed runs before claiming an end-to-end speedup.

## Deployed verification on October 21, 2026

Run `39628360336` completed successfully: build 44m30s (previous 49m42s),
app stage 7m50s (previous 9m39s), channel synchronization 19m09s and live output
7m03s. This is one observed comparison. The 37-second resolution stage reused
1,174 dated entries and processed 154 due rows, while the earlier 213-second
stage processed 1,328 nearly cold rows. Cache warmth and workload differ.

Public generation `2026-10-21T16:17:29Z` passed checks for all three servers'
movie/series/Home manifests and all 132 Server 1 movie shards. Of the exact
154 reviewed movies, 31 gained verified dates and 123 remained unresolved.
All 1,559 previous verified English-tagged identities and dates were preserved;
61 existing English-tagged movies gained dates overall, reaching 1,620.

Optional provider-detail lookups failed in that run. Private collector summaries
now distinguish fixed error categories and request/normalize/validate/cache
stages without exposing exception text, raw responses or credentials. The manual
provider transport workflow has a `vod_evidence` mode for one approved public
movie identity, capped at one HTTP attempt and 15 seconds. It reports only
sanitized counts and availability flags; the existing `live` mode remains the
default. Optional lookup failure is an observation, not publication permission.

The single-request follow-up probe (`39643747184`) found a same-host redirect
with a changed path and non-equivalent query. It was refused before credentials
could be replayed to a different API route. This observation does not establish
that every optional detail failure has the same cause; recovering provider facts
requires an authenticated endpoint that satisfies the configured transport contract.
The diagnostic now reports only fixed redirect decisions/reasons and boolean
constraints, with no raw redirect URL, path, host or query.
