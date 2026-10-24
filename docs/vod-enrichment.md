# Daily VOD enrichment

`vod.yml` still fetches all three provider inventories daily. It builds the app
catalog from the latest valid production cache plus verified candidates from
`enrich-vod.yml`. The publisher makes no TMDB requests. Provider episode refresh,
catalog validation, secret scanning and serialized publication remain in place.
If candidate retrieval fails, an explicit warning is emitted and the existing
production cache remains available. Missing metadata remains visibly unresolved.

`enrich-vod.yml` runs after a successful VOD refresh. It also wakes hourly to
recover a missed continuation or a persisted service cooldown. It never deploys
Pages or writes a production catalog. New verified metadata becomes available to
the next normal VOD publication.

## Durable work

The worker loads a validated VOD component containing normalized provider inputs
and the corresponding app baseline. A published ID is an observation, not proof.
Only the matching client's validated cache envelopes or fresh API verification
can establish a match.

The private SQLite queue tracks each server, media kind and provider record.
Unchanged source identities keep their unfinished work and scheduling age when a
new daily snapshot arrives. Changed identities and matching policies invalidate
only the affected work; removed records cease to be active. Due work is shared
fairly across all six server/media groups. Transient failures remain retryable;
they never become permanent misses merely because an attempt count was reached.

Each batch uses eight workers and one shared 20-request/second limiter, without
a total request quota. HTTP 429 pauses honor the complete `Retry-After` value,
including across processes. Batches stop after 15 active minutes and seal their
queue, cache and report before requesting continuation. Future cooldowns stop
immediate continuation; the hourly wakeup retries when allowed. A fatal batch
retains any sealable progress and reports failure, rather than hiding the error.

The former bounded Server 1 detail probe now runs in this worker. It remains
limited to unresolved current-year English movie candidates and at most 40
provider wire requests per source generation; it is not a detail-library sweep.

## Cache boundaries

State, verified candidate and report artifacts are retained for 14 days. A new
state can seed from the ordinary production cache, but never from an old isolated
verification checkpoint. Candidate exports contain full positive identity-cache
envelopes and sanitized, record-bound provider evidence. Negative outcomes and
raw HTTP/provider responses are not candidate metadata. The two bounded Trending
rank caches are refreshed by the independent worker and transported separately,
so the cache-only publisher retains the existing Home feeds. Known poster collisions
travel with candidates so an imported subset cannot make a shared image appear
unique. The publisher rechecks each candidate against its current source,
reviewed corrections and evidence before using it.

Manual isolated runs use `vod-enrichment-test-*` artifacts. Production imports
reject those artifacts. No automatic deployment is part of an isolated run.

## Source-field provenance

The daily job traces Server 3 movie IDs 1194029 and 1194031, with 954024 as the
unchanged control, using its already-fetched inventory. The retained report has
only field presence/types, safe numeric IDs, validation decisions and hashes.
It includes no titles, URLs, credentials, playback data or raw provider rows.

The original October 24 raw bundle was not retained. A new trace can reproduce a
current provider omission or normalizer defect; it cannot reconstruct that
historical response. Unverified provider IDs remain separate from verified app
identities.

## Discovery scope

These changes repair metadata acquisition and scheduling. They do not add a new
release/genre index or change the app's download contract. For the reviewed
Server 3 snapshot, all published-English records fit its four language-hinted
shards. A future complete compact index or ordered query can avoid a large TV
scan, but cannot establish the true newest movies while release dates are
unknown.
