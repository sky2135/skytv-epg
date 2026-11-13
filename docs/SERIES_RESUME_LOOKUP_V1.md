# Series resume lookup version 1

Status: local additive contract and synthetic interoperability fixtures. This
document does not certify hosted coverage or authorize publication. The Android
source remains subject to its separate freeze and Tester closure.

The reviewed input is `REMEDIATION_PROPOSAL.json`, SHA-256
`3C3DAC2FFDCBBE2DB63DBD40903941BC18C0C704BC1362219BB67A808D54B13A`
(hexadecimal is case-insensitive). Fixture bytes and hash vectors live in
`tests/fixtures/series_resume_lookup_v1/`; `schema.json` is the machine-readable
JSON Schema 2020-12 structural contract. Cross-record and reference rules below
are additionally mandatory.

## Accepted reference graph

An accepted `skytvplayer/index.json` server entry MAY add `seriesResumeLookup`.
Its existing `series` reference remains unchanged. Both references belong to the
same accepted server/index descriptor. An absent or unsupported lookup is
`UnsupportedContract`; it preserves progress and does not authorize new
cross-revision recovery.

Every nonempty artifact reference has exactly `file`, `sha256`,
`compressedBytes`, `uncompressedBytes`, `recordCount`. `file` is a constrained
relative content-addressed filename within the existing catalog directory;
`sha256` is the full lowercase SHA-256 of the hosted gzip bytes. The filename
includes its first 16 hex digits. The root reference has `recordCount: 512`.
Lookup root and bucket files are gzip; counts and byte sizes are exact.

Build the final ordinary series manifest first, then its lookup children and
root, and finally the accepted index. The lookup root binds the ordinary
manifest's exact checksum. The ordinary manifest does not reference the lookup,
so there is no checksum cycle. Root SHA additions, replacements and removals
must invalidate dependent evidence even when Home and series SHA do not change.

The root has exactly these keys:

| Key | Required value |
| --- | --- |
| `schemaVersion` | integer `1` |
| `dataset` | `"vod-resume-index"` |
| `serverId` | accepted exact server ID |
| `seriesManifestSha256` | accepted `servers[].series.sha256` |
| `hashAlgorithm` | `"sha256-domain-nul-first-byte-v1"` |
| `seriesRecords` | exact number of all final published parent records |
| `episodeRecords` | exact number of all reachable published `(series, episode)` pairs |
| `seriesBuckets` | object with exactly the 256 keys `00` through `ff` |
| `episodeBuckets` | object with exactly the 256 keys `00` through `ff` |

There is no root payload `recordCount`, timestamp or extra field. Each bucket
slot is either a full nonempty artifact reference or exactly
`{"empty":true,"recordCount":0}`. A missing slot, null, empty object or missing
artifact is invalid; it is never authoritative emptiness. The accepted root
owns every child by checksum. Children omit generation, root SHA and manifest
SHA, allowing byte-identical facts to be reused across accepted generations.

## Identity, routing and ordering

Server IDs match `server_([1-9][0-9]?)`. Provider IDs match
`[A-Za-z0-9][A-Za-z0-9._:-]{0,119}`. IDs are exact ASCII strings: preserve
case, leading zeroes, punctuation and length. No trim, numeric conversion,
case-folding, title/TMDB matching, cross-server or edition substitution occurs.

Compute the SHA-256 of these exact UTF-8 byte strings, where `NUL` is one zero
byte, not backslash plus `0`:

```text
series:  "skytv-resume-series-v1"  NUL serverId NUL seriesId
episode: "skytv-resume-episode-v1" NUL serverId NUL seriesId NUL episodeId
```

The bucket key is the first digest byte formatted as two lowercase hex digits.
Hash the explicit tuple; concatenating series and episode IDs is forbidden.
`hash_vectors.json` includes input UTF-8 hex, full SHA-256, bucket and
same-text-concatenation pairs such as `(1,23)` versus `(12,3)`.

Each child has exactly `schemaVersion`, `dataset`, `serverId`, `bucket`,
`recordCount`, `items`. Schema version is `1`; datasets are respectively
`vod-resume-series` and `vod-resume-episodes`. `serverId` matches the root,
`bucket` matches its slot and every contained identity hashes to that slot.
`recordCount` equals the nonempty items count. Series items sort by exact ASCII
`providerSeriesId`; episode items sort by the tuple
`(providerSeriesId, providerEpisodeId)`. No duplicates are permitted.

Canonical JSON is UTF-8 without BOM or trailing newline, object keys sorted,
no insignificant whitespace, Unicode preserved, and no NaN/Infinity. Fixtures
use deterministic gzip, compression level 9, mtime 0, empty filename, OS byte
255. Consumers verify bytes and do not require a particular valid compressor.

## Exact parent projection

Every field below is required; do not default missing facts into safe values.
The writer copies each value exactly from the final full published parent,
except `section: "series"`, which is added. Root/bucket envelopes supply server
identity. The projection has exactly these 20 fields:

```text
section, id, providerSeriesId, contentId, title, category, adult,
genre, genres, language, languages, originalLanguage, isMultilingual,
languageListComplete, languageSource, episodesStatus, availableEpisodeCount,
serverEpisodeCount, episodeInventoryComplete, episodeInventoryCheckedAt
```

`id == providerSeriesId`; `contentId` is exactly
`serverId + ":series:" + providerSeriesId`.
`category` is exactly `{id, name}`, each nullable as allowed by the existing
catalogue contract. `adult` is true, false or null. `genre` is a string, using
`unknown` exactly when `genres` is empty; otherwise it equals `genres[0]`.
Genre and language lists preserve the source sequence; they are not re-sorted.

`language`/`languages` retain existing normalized catalogue semantics and
values. Unknown is `und`/`["und"]`; known singular language has the matching
one-item list; `mul` carries at least two actual language codes. Preserve
`originalLanguage` separately, including null. `isMultilingual` and
`languageListComplete` are explicit booleans; `languageSource` is one of
`provider_audio`, `title_suffix`, `provider_multilingual`, `verified_original`,
`unknown`. Keep existing full-parent evidence consistency checks.

`episodesStatus` is `available`, `stale`, `pending` or `unavailable`.
`availableEpisodeCount` is an integer 0..50000 describing retained published
inventory. `serverEpisodeCount` is null or an integer 0..50000.
`episodeInventoryComplete` is boolean. `episodeInventoryCheckedAt` is null or a
valid normalized UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`, no later than the source
manifest generation. A nonnull server count requires a checked time; complete
inventory requires both a server count and checked time. No new TTL is added.

No artwork, episode arrays, full detail object or shard ordinal is included.
This is a typed proof DTO, not a partially populated ordinary catalogue record.

## Exact episode projection

Each item has exactly:

```text
providerSeriesId, providerEpisodeId, seasonNumber, episodeNumber,
containerExtension, title, durationSeconds
```

The provider episode ID is copied from the source episode's `id`; its parent
ID comes from the containing series. The other values copy the final source
episode exactly. Season and episode numbers are integers 0..100000, matching
the existing publisher normalizer (zero is valid). `containerExtension` is
null or lowercase `[a-z0-9]{1,8}`. `title` is a nonempty string of at most 300
characters. `durationSeconds` is null or an integer 0..604800. Integers must
not accept JSON booleans or lossy numeric/string coercion.

Coverage includes all inline and every referenced external published episode,
including stale retained inventory. A missing referenced input, duplicate pair,
contradictory identity, malformed record or cap overflow rejects the entire
candidate. Lookup completeness is independent of provider inventory
completeness: a complete map can truthfully describe an incomplete inventory.

## Resolution matrix

All decisions are pinned to the accepted server, ordinary series SHA, lookup
root SHA and exact parent/episode IDs. Local account/profile/session ownership,
latest progress, language, adult, hidden, category/series/episode parental
checks remain mandatory. Current same-snapshot Home safety takes precedence;
conflicting proofs reject new recovery. No result deletes durable progress.

| Verified condition | Structural result before local policy |
| --- | --- |
| Exact parent missing from a verified complete parent bucket | `ParentAbsent` from this snapshot |
| Parent `available`, valid nonnull checked time within accepted snapshot cutoff, exact episode present, usable current playback DTO | Positive proof; local guards may authorize `Available`, even when inventory completeness is false |
| Same current available parent, complete inventory, exact episode missing | `EpisodeAbsent` from this snapshot |
| Available but incomplete inventory, episode missing | `Unknown` |
| Null/invalid/future checked time, or stale/pending/unavailable status | `Unknown`, even for positive pairs or complete empty inventory |
| Missing facts, malformed/checksum/identity failure, expired ownership, deadline or budget exhaustion | `Unknown` |
| Optional lookup absent or unsupported | `UnsupportedContract` |

Unknown adult/language/category must pass existing conservative app policy;
presence alone never certifies safety. Exact episode membership does not
discover ordering or the next episode, and snapshot presence does not prove
provider playback success. No `loadRecord`, `loadSeriesDetails`, provider query
or whole-catalogue fallback is allowed in this recovery lane.

## Bounds and compatibility

Every root and nonempty bucket is at most 65536 compressed bytes and 262144
expanded bytes. Series buckets have at most 2048 records; episode buckets at
most 8192. Root tables each have exactly 256 slots; its reference count is 512.
Validate declared bounds before allocating, exact received lengths/hash after
reading and decoded bounds/counts while processing. Empty slots contain no file.

Per slice: at most four candidates, nine artifact attempts including retries,
1048576 received compressed bytes, 4194304 expanded bytes, ten seconds shared
deadline, concurrency one. Start from the already accepted Home/index
descriptor; the lane does not fetch another index or series manifest. Charge
partial failed bodies and cached decoding to the same budget.

Per accepted generation: at most 100 candidate identities, target ten eligible
cards, 201 artifact attempts including retries, 13631488 compressed bytes,
54525952 expanded bytes. Deduplicate buckets; preserve remaining budgets and
known outcomes across position ticks/reentry/recomposition. Retry spends the
remaining allowance. These are ceilings, not promised completion or targets.
Existing Home acquisition keeps its separate preexisting bounds. Root-only
changes create a new lookup generation and retire prior owned openings.

Old clients ignore the optional index field. A writer/validator must not change
existing series sharding, Home fields, playback identifiers, provider fetching,
or deployed data while producing this addition. Full retained-data coverage and
all-artifact maximum sizing remain rollout gates; preliminary sampling with
missing episode files is not certification.

## Shared fixtures

`generate.py` uses only the Python standard library and writes only its own
fixture directory. It emits source publication artifacts, a complete positive
lookup graph, root-only-change/unsupported descriptors, negative graphs,
hash/UTF-8 vectors, fixture checksums and a decision matrix. Fixtures are
synthetic and contain no accounts, credentials, playback URLs or real history.

Structural schema validation is necessary but insufficient. Implementations
must also verify checksum/length chains, exact bucket identity and placement,
sorting, uniqueness, manifest/root binding, full source projection parity,
coverage/count reconciliation and accepted-cutoff semantics. Negative cases
list the intended rejection layer; consumers must not turn transport or
structural failures into catalogue absence. App and publisher tests should read
the same checked-in bytes rather than regenerating them independently.
