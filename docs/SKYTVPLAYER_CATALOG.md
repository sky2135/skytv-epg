# SKYtvPlayer movie and TV database

The GitHub publisher builds a separate movie and TV database for SKYtvPlayer
from Server 1, Server 2, and Server 3. The app downloads published JSON instead
of scanning the provider's complete VOD library to build its browsing screens.
Provider credentials remain in GitHub secrets and the app's signed-in session.
Playback still uses that session.

The publisher change is staged for review and has not been deployed. This
repository supplies the database and the Android integration contract. The
Android app reader is being implemented separately and must use the contract
below before its screens can use these files.

## Published location

The app's stable entry point after deployment is:

[SKYtvPlayer index](https://sky2135.github.io/skytv-epg/skytvplayer/index.json)

The output directory is `public/skytvplayer/`, deployed with the existing
`public/EPG/` JSON guide. The app fetches the index over HTTPS; copying generated
data into the Android source folder is unnecessary.

The index has `schemaVersion: 1`, `appName`, `generatedAt`, `serverCount`, and a
`servers` array. Match the authenticated app server using its exact ID:
`server_1`, `server_2`, or `server_3`. Provider identifiers are scoped to that
server; a title on another server is not a playable substitute.

Each server contains these references:

| Field | Purpose |
| --- | --- |
| `movies` | Separate movie manifest, provider categories, and movie shard references |
| `series` | Separate TV series manifest, provider categories, and series shard references |
| `home` | Small compressed discovery database with ordered shelves and preview records |
| `epg` | References to existing programme and live metadata JSON when available |

All VOD `file` values are relative to the index directory, including references
inside the dataset manifests. A VOD artifact reference contains `file`,
`sha256`, `compressedBytes`, `uncompressedBytes`, and `recordCount`. The SHA-256 covers the exact hosted
bytes before gzip decompression. For a plain JSON manifest, compressed and
uncompressed byte counts are equal. A manifest's `recordCount` counts its
shards; a shard's count counts its `items`. EPG references contain only `file`,
`sha256`, and `compressedBytes`; the existing EPG manifests retain their own
guide and metadata validation contracts.

Movie and series manifests have `schemaVersion`, `serverId`, `dataset`,
`generatedAt`, `totalRecords`, normalized `categories` with `id` and `name`, and
`shards`. The datasets are `vod-movies` and `vod-series`. A shard carries the
same server and dataset identity and an `items` array. Each shard has one media
type, at most 500 items, and at most 512 KiB compressed.

Each movie/series shard reference also lists its `languages` as a browse hint.
Together with `providerCategoryIds`, these hints let the app download only
shards that may contain a selected language or provider category. Check each
record against the selected language after loading it.
Records are ordered by provider category and then provider ID. Each shard
reference includes `providerCategoryIds`, so a category view can download only
the shards that may contain that category and then filter their records.

Use the generated contract, including the `file` reference key. The Android
project's older example documents used `path` and example server IDs with
hyphens; those examples are not an implemented VOD reader.

## Title and playback identity

Every public record has a stable catalog key in `contentId`:

```text
server_1:movie:123
server_1:series:456
```

These keys join discovery shelves to title records. They are not Xtream stream
IDs. Movies also carry `providerStreamId` and a sanitized container extension;
series carry `providerSeriesId`. Preserve these values as strings when adapting
to the Android models.

| Android model | Published value |
| --- | --- |
| `VodMovie.streamId` | `providerStreamId` |
| `VodMovie.categoryId` | `category.id` |
| `VodMovie.containerExtension` | `containerExtension` |
| `VodMovie.directSource` | `null`; resolve privately at playback time |
| `VodSeries.seriesId` | `providerSeriesId` |
| `VodEpisode.id` | Provider episode stream ID from a verified episode record |

The public database contains no provider API origin, username, password,
playback URL, direct source, or signed access token. The app resolves the title
through its authenticated server when Play is selected. Do not construct a
playback URL with the prefixed `contentId`.

## Metadata and episode coverage

Provider facts remain available when external enrichment has no confident
match. Unknown adult status stays `null`. Unknown language stays `und`.
Original language and provider language availability remain separate facts;
country or server market alone is not language evidence.

Supplied TMDB IDs are hints. Title and year must independently corroborate both
new responses and cache entries before external metadata is published. Cache
keys include the matching policy and display identity. Compatible old cache
entries are migrated only when their original title and year corroborate the
displayed work; an unrelated stale match is never a fallback.

Reviewed record-specific corrections are guarded by server, media kind,
provider ID and exact display identity in `data/vod_reviewed_corrections.json`.
They preserve provider audio fields and carry public source URLs in
`metadataEvidence`. `metadataMatch: reviewed_correction` distinguishes these
facts when a verified external match is unavailable. Rejected identities lose
their inherited synopsis, artwork and dates. Unverified provider TMDB IDs are
withheld from the app catalog so they cannot join unrelated trending records.
The release validator rejects known wrong IDs and lost reviewed facts.

`language` and `languages` describe the known provider version. TMDB's
`originalLanguage` describes the work's original language; it does not prove
which audio tracks a provider supplies. Never offer a language option solely
because TMDB lists it.

Confident TMDB matches can add original title, overview, release information,
genres, rating, vote count, popularity, runtime, cast, directors, trailers,
artwork, and TV status and episode summary fields. Title searches require
conservative title and year agreement. Unmatched or ambiguous records remain
provider records rather than taking the closest search result.

When a movie has neither provider poster nor backdrop and matching reaches a
definite `no_match`, further attempts stop for that unchanged provider record.
The movie remains in the app catalog with its sanitized provider title, facts
and playback ID. This private retry decision does not add guessed artwork or
mark the movie as verified. New source identity, artwork, evidence, reviewed
corrections or matching-policy versions reopen matching. Request failures,
incomplete searches and exhausted budgets remain retryable. Series and movies
with any provider artwork keep the existing refresh policy.

Series details are fetched incrementally and keep sanitized episode IDs,
season numbers, episode numbers, and container extensions. `episodesStatus`
distinguishes `available`, `stale`, `pending`, and `unavailable`. An empty
episode list is not proof that a show has no episodes. When details are pending
or unavailable, the app may request that selected show's details privately
from the provider. TMDB's next-airing episode is display information, not
evidence that the provider can play it.

Full series records have `episodes`, `episodeFiles`, and
`availableEpisodeCount`. A normal inventory is inline in `episodes`, with
`episodeFiles: []`. More than 500 episodes, or a series record that exceeds
256 KiB compressed with inline episodes, triggers separate episode shards:
`episodes: []` and `episodeFiles` holds verified artifact references.
`availableEpisodeCount` counts the provider inventory across both forms; it is
separate from TMDB's `episodeCount`, which describes the show and may include
episodes the provider does not offer.

Fetch episode files only after selecting a series or resolving its local Up
Next state. Their `file` paths are relative to the index directory. Each gzip
payload has `schemaVersion: 1`, `dataset: vod-episodes`, `serverId`,
`providerSeriesId`, `generatedAt`, `recordCount`, and episode `items`. Check the
series and server identity as well as artifact counts, sizes, and checksums.
Each episode shard has at most 500 items and 512 KiB compressed.
Combine verified shards in season and episode order. Do not interpret an empty
inline array as an empty inventory when `episodeFiles` is populated, or fetch
episode files while preparing Home previews.

`firstSeenAt` records the publisher's first observation. Provider added dates
take precedence when usable. The first complete import establishes a baseline;
it must not make an entire undated library appear newly added. Keep the
incremental publisher cache so later observations can distinguish additions
and newly available episodes.

These observation and upload timestamps describe catalog changes. They do not
establish when a show premiered. A provider library addition may be an older
work, and `lastAirDate` may refer to an episode the provider does not offer.

## Movies and TV shelves

The `home` artifact contains `movieRows`, `seriesRows`, and preview records in
`items`, keyed by catalog `contentId`. Preview records omit episode arrays and
other large detail fields. Fetch full shards lazily for browsing, search, and
selected-show details. The compressed home artifact is bounded to 512 KiB;
each shelf keeps up to 20 candidates. Every known safe language has a group,
with up to 12 candidates per language group. Featured has up to 12 candidates.
These are previews: a missing title or language in Home does not mean it is
absent from the full catalog. Load additional verified catalog data when a
selected language or Browse All needs it.

Preview records contain these common keys when present: `id`, `contentId`,
`title`, `category`, `language`, `languages`, `genres`, `adult`, `tmdbId`,
`artwork`, `releaseDate`, `rating`, and `addedAt`. Movies add `providerStreamId`
and `containerExtension`; series add `providerSeriesId`,
`latestEpisodeAddedAt`, and `episodesStatus`. Previews can also retain
`firstSeenAt`, `lastAirDate`, and `status` from the full record. Eligible series
previews add `discoveryAvailability: {"evidence": "provider_episode_inventory",
"episodeCount": N}` for a positive observed provider episode count. This
compact evidence does not claim a playback test or include episode details.
Do not copy it into the full-record `availableEpisodeCount` field: existing
readers require the complete inline or sharded inventory with that field.
`overview` is included with at
most 500 characters. Cast, directors, trailers, complete episode inventories,
and episode-file references belong to full records.

Rows use `id`, `title`, `source`, and `itemIds`. A language container has nested
`rows`; language-sensitive trending contains `byLanguage`. `source: local`
marks a device-owned shelf. Empty public `itemIds` in a local shelf do not mean
the viewer has no viewing history.

| Movies order | Meaning |
| --- | --- |
| Featured | Publisher-ranked safe titles available on the selected server |
| Continue Watching | Local unfinished movie progress joined to the current catalog |
| Trending in Your Languages | Playable trending candidates filtered by the viewer's selected provider languages |
| Newly Added | Provider added dates or post-baseline first observations |
| Language rows | Known language versions, filtered by viewer preferences |
| Browse All | Complete server movie library, subject to parental and visibility controls |

| TV Shows order | Meaning |
| --- | --- |
| Up Next | The viewer's next playable episode, calculated from local progress and available provider episodes |
| New Episodes | Newly observed provider episodes, not future TMDB air dates or series upload dates |
| New Series | Known premieres within the preceding 30 calendar days, with observed provider episodes |
| Trending TV | Safe trending series available on the selected server |
| Language rows | Known provider language versions, filtered by viewer preferences |
| Browse All | Complete server TV library, subject to parental and visibility controls |

Automatic discovery shelves require `adult: false`. Unclassified titles remain
browsable under the app's parental policy. Do not convert missing classification
into a safe default or replace publisher fields with title-name guesses.
Every automatic TV shelf also requires `episodesStatus: available` or `stale`
and a positive `availableEpisodeCount` in the full catalog. This works for both
inline and sharded episode inventories. Pending, unavailable and empty observed
inventories remain in Browse All for private selected-show resolution, rather
than being promoted as available recommendations. Stale inventory is cached
provider evidence; it is not a promise of successful playback.

The `newSeries` row keeps its schema-v1 identity and adds `dateField:
releaseDate`, `windowDays: 30`, and `availabilityEvidence:
provider_episode_inventory`. Select and sort by the show's `releaseDate`,
newest first, rather than `addedAt`, `firstSeenAt`, or provider modification
time. A complete, valid `YYYY-MM-DD` date must fall from 30 days before the
publication's UTC calendar date through that date, inclusive. Unknown dates,
year/month precision, impossible dates and future premieres are excluded.
Recent premieres can qualify even on an initial baseline import. A client
which additionally filters this row must apply the same release-date rule;
an upload-date filter can incorrectly hide a valid premiere. `newEpisodes`
continues to describe newly observed provider episodes using
`latestEpisodeAddedAt`; it does not claim a recent broadcast or premiere.
Personalization further filters the published candidates using language,
category visibility, interests, and parental settings. Hide an automatic shelf
that has no eligible titles.

Continue Watching and Up Next stay on the device. Scope their storage with a
one-way digest of server ID, normalized provider username, and household
profile ID, using `default` until a profile picker exists. Do not upload viewing
history to GitHub or TMDB. The current Android progress store retains resumable
items and removes completed entries; true Up Next also needs a bounded
per-series completion cursor so finishing an episode can advance the series.

## Android loading and cache rules

1. Show the last verified local index and home data immediately. Home must have
   a usable focus target while refresh runs.
2. Refresh the index in the background, coalesce concurrent requests, and
   avoid checking more than once per 30 minutes while active.
3. Match `serverId`, schema, byte counts, record counts, and SHA-256 before
   accepting referenced data. Enforce compressed and decompressed size limits
   while streaming, before allocating a large JSON tree.
4. Reuse verified content by checksum. Write downloads atomically and keep the
   prior verified release when an optional update fails.
5. Parse and index manifests, shards, classification, and shelves on background
   dispatchers. Never scan the library on the Compose thread.
6. Load the home artifact first. Load movie and series manifests separately;
   fetch only needed shards and artwork. A metadata error must not block EPG
   display or playback.
7. Invalidate derived indexes and personalized shelf caches when their source
   checksums change. Bound cache storage and remove unreferenced artifacts with
   a least-recently-used policy.

Resolve VOD references only within the index's trusted HTTPS origin and
catalog directory. Reject absolute URLs, credentials, query strings, fragments,
encoded traversal, symlinks in local caches, and unexpected schema versions.
The index's explicit EPG sibling references may point to `../EPG/`; resolve
these through a separate constrained rule that permits only the same repository
origin, `/skytv-epg/EPG/` prefix, and the selected server's exact
`server_N_epg_v2.json.gz`, existing-format `server_N_epg.json.gz`, or
`server_N_metadata.json.gz` filename. `epg.programme` retains the compatible
schema v1 programme file. Optional `epg.programmeRich` advertises the rich
schema v2 object-row feed for clients that explicitly support it. Do not
weaken the VOD shard resolver to
accept arbitrary `..` paths.

## GitHub setup

The existing build-and-publish workflow collects inventories from all three
configured provider secret sets. It then builds `public/skytvplayer/` from the
private `.build/vod-inputs/` snapshots. No second download of the full provider
library is needed for app discovery publication.

To enable TMDB enrichment, create a TMDB account and obtain application API
credentials from its account API settings. Add either repository Actions
secret `TMDB_READ_ACCESS_TOKEN` with the API Read Access Token, or `TMDB_API_KEY`
with the v3 API key. A bearer token and an API key are alternative application
authentication methods. See [TMDB application
authentication](https://developer.themoviedb.org/docs/authentication-application).

The workflow restores and saves `.build/vod-cache/` through GitHub Actions
cache. The `server_N_inventory.json` files hold first-observation and episode
change state; `tmdb/` and `episodes/` hold sanitized enrichment caches. This
private state must never be copied into the public Pages artifact. Enrichment
is bounded per run, so a large first library fills out over later successful
runs. Default per-run budgets are 1,500 TMDB HTTP requests and 600 provider
series-detail requests, controlled by `--max-tmdb-requests` and
`--max-series-requests`. Missing TMDB credentials still permit provider-only
publication.

TMDB work shares its request budget across all six server and media-type
groups, prioritizing uncached titles before stale metadata. It uses a
30-second elapsed budget for trending and 45 seconds per group for title
enrichment. The scheduled publisher enables three metadata workers; standalone
CLI builds remain serial unless `--tmdb-workers 2` or `3` is selected. Workers
use isolated clients, deterministic cache ownership, fixed fair request quotas,
and shared request pacing/429 cooldown. All enrichment finishes before final
artifact validation and index publication. Unused quota is reported and does
not increase the total request cap. Provider episode requests share the global budget across all
three servers, with a 90-second elapsed budget per server. Individual in-flight
requests remain subject to their network timeouts. Attempted uncached TMDB
failures are cached for one day so one failing title cannot monopolize later
runs; titles deferred by a budget remain eligible on the next run. Cached
trending results older than seven days are not published as current trends.

Before the normal app build, the workflow runs a separate release-date backfill
for Server 1 movies tagged English-only (`languages == ["en"]`) whose displayed
year is the current build year and whose full release date is missing. This
phase has its own limit of 3,000 TMDB requests and 600 seconds. It uses the same
title/year identity checks, reviewed corrections, and private metadata cache as
the normal builder. Uncached candidates are processed before stale or fresh
cache entries, with the newest provider additions first within each cache tier.
The provider's added date only controls processing order; it never becomes a
movie release date. Ambiguous matches remain unknown.

The backfill writes only sanitized private cache entries and a count summary.
The normal three-server build then consumes those entries and publishes verified
`releaseDate` values in `YYYY-MM-DD` form. The normal per-server/type request and
time allowances are unchanged. This phase does not fetch provider APIs or
publish a separate Server 1 index. It can also be run against captured inputs:

```text
python scripts/build_skytvplayer_catalog.py --input-dir .build/vod-inputs --cache-dir .build/vod-cache --backfill-release-dates server_1 --max-tmdb-requests 3000 --backfill-max-seconds 600
```

Build from already captured private inventory snapshots:

```text
python scripts/build_skytvplayer_catalog.py --input-dir .build/vod-inputs --output-dir public/skytvplayer --cache-dir .build/vod-cache --epg-dir public/EPG
```

The workflow adds `--fetch-series-details` to permit the bounded provider
episode refresh, using the existing `SERVER_N_BASE_URL`, `SERVER_N_USERNAME`,
and `SERVER_N_PASSWORD` secrets. Validate a complete three-server publication
without network access:

```text
python scripts/build_skytvplayer_catalog.py --validate-only --output-dir public/skytvplayer --epg-dir public/EPG
```

TMDB allows free use for non-commercial purposes with attribution; commercial
use requires its commercial arrangement. The Android About or Credits screen
must show an approved TMDB logo and this notice:

> This product uses the TMDB API but is not endorsed or certified by TMDB.

See [TMDB API terms and attribution
requirements](https://developer.themoviedb.org/docs/faq).

## App chat handoff

The Android project chat is implementing the reader. This brief records the
final contract for that integration:

```text
Connect SKYtvPlayer's Movies and TV Shows to the GitHub publisher database at
https://sky2135.github.io/skytv-epg/skytvplayer/index.json.
Read the EPG repository's docs/SKYTVPLAYER_CATALOG.md and the actual generated
schema before implementing. The local publisher repository is:
C:\Users\rsingh\OneDrive - Golden Windows\Work-P\IPT\EPG\last-attempt-to-correct-the-github\work\skytvplayer-vod-catalog

Use server_1/server_2/server_3, checksum-verified manifests, lazy movie/series
shards, compact home previews, last verified local caching, and private session
playback. Every VOD artifact uses file relative to the index directory; use
providerCategoryIds on shard references for category loading. EPG sibling refs
allow only the selected server's epg_v2, existing epg, and metadata JSON files.
Map providerStreamId/providerSeriesId to existing playback models; contentId
is a separate catalog join key. Keep classification in the publisher.

Home is at most 512 KiB gzip. Rows have up to 20 candidates; every known safe
language has up to 12 candidates. Preview fields come from home_preview() and
exclude full episodes and rich detail metadata. Full series records contain
episodes, episodeFiles, and availableEpisodeCount. Large inventories use
lazy vod-episodes shards; fetch those only for the selected series or Up Next.

Movies order: Featured, Continue Watching, Trending in Your Languages,
Newly Added, language rows, Browse All.
TV Shows order: Up Next, New Episodes, New Series, Trending TV,
language rows, Browse All.

Continue Watching and Up Next must use local per-server/account/profile state.
Add a bounded completed-episode cursor for Up Next; never treat a future TMDB
episode as playable. Respect adult status, parental controls, hidden categories,
and selected languages. Add TMDB attribution. Keep EPG and playback usable if
optional metadata refresh fails. Follow the project's AGENTS.md, including
Kotlin compilation, unit tests, debug APK and Android-test APK builds, then
real emulator screenshots and D-pad verification for viewer-facing changes.
The publisher is staged for review; do not assume its public index is deployed
until the GitHub Pages publication succeeds.
```
