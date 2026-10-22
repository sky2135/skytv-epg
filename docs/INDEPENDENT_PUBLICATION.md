# Independent live EPG and VOD refreshes

`main.yml` retains the existing daily live schedule and manual trigger. It
refreshes provider channel inventories, Google Sheets mappings, real programme
schedules, logos and live app metadata. It never fetches VOD inventories or
waits for TMDB enrichment. `vod.yml` runs daily at 4:47 a.m. America/New_York
time and can also be manually dispatched. It refreshes all three servers'
movie/series inventories, categories, languages,
release dates, episode metadata and Home shelves. It never runs channel matching
or rewrites Google Sheets. No new credentials are required.

Both validate and upload a component snapshot with an explicit file inventory,
byte counts and SHA-256 hashes. The reusable `publish-component.yml` holds one
publication lock through restore, composition, validation and deployment.
Independent builds can run together. Publication uses the latest successful
validated component build, including one whose publisher is queued or failed;
an old publisher retry cannot roll back a newer component. A producer's exact
artifact ID is retained across retries of only its publisher job.

If Actions snapshots have expired, the deployed component manifests and files
provide a durable fallback. On the first split publication, a successful legacy
main-branch Pages artifact bootstraps the missing counterpart. Missing or corrupt
counterparts stop publication and preserve the deployed site. Component
artifacts are retained seven days; deployed manifests remain until the next
successful composition.

The only shared app field is `skytvplayer/index.json`'s per-server `epg` object.
Composition updates its paths, SHA-256 hashes and byte counts from the selected
live guide. Movie, series and Home payloads remain unchanged. Component generation
timestamps are never refreshed merely because the other component changed.
All complete-site publication checks and configured-secret scans still run.

## Poster-assisted VOD identity matching

The VOD refresh builds a shared candidate index from identity-verified TMDB
metadata already in its private cache. A provider's exact public TMDB poster
filename can suggest an ID across servers. The matcher fetches current official
details and requires the title or official alias, year, media kind and poster
path to agree. Provider evidence, supplied IDs and reviewed rejections remain
hard constraints. Known poster collisions do not establish an identity.

A complete title/year search with at most three ambiguous candidates can also
use their official default or alternate posters to resolve one unique match.
Poster hashes do not encode TMDB IDs; a title without a known candidate still
needs normal bounded search. The existing request limits, three-worker cap and
per-group time limits remain in place. Daily runs gradually process the backlog.

New poster proofs use a separate cache-key policy so old misses cannot hide the
new evidence. Validated existing matches are reused. Each successful VOD build
publishes `skytvplayer/identity_coverage.json`, with verified ID coverage by
server, movie/series and published English/Hindi/Punjabi language labels.
Provider ID hints are not verified IDs. Unknown languages are reported
separately, and multilingual rows can appear in more than one language total.

## Incremental channel matching

The approved-alias index narrows lookups to the original matching alias rows in
their original order, then invokes the unchanged frozen policy body. Market,
language, timeshift, cross-storage and ambiguity rules remain authoritative.
The frozen matcher files and their protected function hashes are unchanged.

`SKYTV_MATCH_CACHE_DIR` enables authenticated private caching of provisional
deterministic results, including unresolved results. Dependencies include the
complete current provider lineup and category/profile inputs, native IDs,
catalog identities/semantics, executable rules and knowledge, learned aliases,
and quarantine state. Changes invalidate cached searches. Schedule bytes are
excluded from the candidate-search key so daily schedule changes can reuse a
search, but every reused proposal is rebound to this run's catalog and must pass
current ambiguity and programme gates. Cached results are never final approvals.

Plaintext decisions stay in the runner's private workspace. Only an AES-GCM
encrypted envelope enters the Actions cache. Authentication and encryption use
separate keys derived from the existing Google service account private key;
credential rotation invalidates the cache safely. Missing, corrupt, wrong-key,
or oversized cache files fall back to ordinary matching. Cache availability does
not determine whether publication is valid. No cache content is published in
diagnostics or Pages.

Logs include elapsed milliseconds for runtime preparation, learned aliases,
catalog ambiguity, deterministic proposals, AI shortlist preparation and the
whole catalog callback, plus aggregate cache hits/misses and alias-index work.
Compare a first cold run with a subsequent unchanged run to measure end-to-end
improvement; the isolated alias lookup benchmark is not a workflow speed claim.
