# Resumable full-catalog verification

`verify-vod.yml` runs an isolated verification pass over every movie and TV
series record in one immutable, validated production VOD component. It never
builds or deploys a production publication, saves a production cache, or calls
provider playback or account endpoints.

TMDB's current guidance describes an approximate upper range of 40 requests
per second and requires respecting HTTP 429 responses. This test defaults to
one shared 20-request/second gate across eight workers and all six datasets.
The request count is measured, not used as a daily or per-pass quota. Verified
cache proofs are validated against current source identities before reuse.

Each batch freezes its source records and baseline app identities, commits
completed outcomes to SQLite, saves its own TMDB cache and complete checkpoint
as a checksum-verified Actions artifact, then dispatches the next batch. No
completed record is skipped merely because an integer cursor advanced.
The initial batch checkpoints after at most two minutes of active matching
following input/cache preparation, proving the handoff before the normal
15-minute batches continue.
Cross-server work is shared only when the actual matcher identity and its
reviewed constraints agree. Negative caches are revisited for this pass.

HTTP 429 cooldowns accept both numeric and HTTP-date `Retry-After` values,
are shared across workers, and persist with the checkpoint. A deadline or
cooldown interruption leaves work pending without consuming an error attempt.
Actual transient failures receive three attempts, then remain explicitly
unresolved for this pass. Definitive title/year ambiguity and missing source
identity are reported separately. This is completion of an attempted pass,
not a claim that every provider title can be identified safely.

Reports contain baseline counts, newly verified entries, unresolved reasons,
server/language/movie/series breakdowns, actual wire requests including
retries, HTTP statuses, shared cooldowns, batch execution time and total wall
time. They do not contain API credentials or raw HTTP response bodies.

To resume an interrupted chain, dispatch the same workflow revision/ref with
the same test ID, source artifact, seed cache key, rate and workers, supplying
the most recent successful checkpoint artifact ID. A changed code/configuration
or frozen input is rejected rather than silently mixing passes. Completed
batch artifacts remain available for seven days. An in-progress batch can be
repeated after abrupt runner loss; the last uploaded batch is the durable
cross-run recovery boundary.

Guidance: https://developer.themoviedb.org/docs/rate-limiting
