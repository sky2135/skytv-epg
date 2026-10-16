# Live programme JSON v2

Every successful build publishes `EPG/server_N_epg_v2.json.gz` and
`EPG/server_N_epg_v2_manifest.json` alongside the existing v1 guide. The v1
filename, schema version, and `[start, stop, title]` programme rows remain
compatible with existing app readers.

The app index `EPG/index.json` keeps its existing `dataFile` and `manifestFile`
references. Each server build also provides `richSchemaVersion: 2`,
`richDataFile`, `richManifestFile`, `richDataSha256`, and `richCompressedBytes`.
The v1 manifest provides the same references relative to its own directory.
The SKYtvPlayer catalog keeps `epg.programme` on v1 for current app readers
and advertises v2 separately as optional `epg.programmeRich`.

The v2 envelope has `schemaVersion: 2`, `programmeRowFormat: "object"`,
`serverId`, Unix UTC second timestamps in `generatedAt`, `windowStart` and
`windowEnd`, plus `streamToEpg`, `programmes`, `streamLogos`, and
`logoAttribution`. Stream IDs and schedule keys have exactly the same meanings
as v1. A programme is an object:

```json
{
  "start": 1792152000,
  "stop": 1792155600,
  "title": "Example programme",
  "subtitle": "Example episode title",
  "description": "The description supplied by the guide source.",
  "categories": ["Drama"],
  "sourceType": "epgshare",
  "episode": [{"system": "xmltv_ns", "value": "1.2."}],
  "icon": "https://art.example/programme.jpg",
  "ratings": [{"system": "US", "value": "TV-PG"}],
  "starRatings": [{"system": "IMDb", "value": "8/10"}],
  "broadcastType": ["new"],
  "credits": [{"role": "director", "name": "Example Name"}]
}
```

`start`, `stop`, `title`, `sourceType`, and `categories` are always present.
`sourceType` is `epgshare`, `panel`, or `synthetic`. A synthetic record remains
a generated channel/event label; its existence does not establish that a real
programme is airing. Join channel metadata by stream ID for `guideMode` and
`guideFallbackReason`.

Other fields appear only when supplied by XMLTV. The publisher does not infer
episode numbers, artwork, cast, ratings, dates, or broadcast flags from names.
`episode` preserves each source notation and its `system`; `xmltv_ns` numbers
are zero-based and must be interpreted according to XMLTV rather than displayed
as ordinary season/episode numbers. `ratings` preserves the source rating
system; a channel's general/adult classification is separate from programme
ratings. `starRatings` is a review score, not an age rating.

Additional optional fields are:

| Field | Source meaning |
| --- | --- |
| `date` | XMLTV date, preserving its supplied precision |
| `language`, `originalLanguage` | XMLTV programme language and original language |
| `countries` | Source country names, without inferring production country |
| `broadcastType` | Explicit `live`, `new`, `premiere`, `last-chance`, or `previously-shown` markers |
| `previouslyShown` | Supplied `start` and/or `channel` attributes; an empty object is possible |
| `video` | Supplied `present`, `colour`, `aspect`, and `quality` values |
| `audio` | Supplied `present` and `stereo` values |
| `subtitles` | Source subtitle records with `type` and optional `language` |
| `length` | Source duration `value` and `units`; scheduled duration still comes from `start`/`stop` |
| `stopInferred` | `true` when an invalid/missing source stop uses the existing one-hour fallback |

Programme artwork accepts only credential-free HTTP(S) URLs without query
strings or fragments. Native provider programme artwork is excluded under the
same privacy rule used for provider channel icons. Each optional record list
has at most 32 entries. Text and the complete retained details object are
bounded; names and short facts are limited to 300 characters, artwork URLs to
2,000, and details JSON to 32 KiB. Existing subtitle/description/category text
limits still apply. The publisher selects English title/description text when
available and otherwise the first usable source text.

The v2 manifest includes `programmeFieldCounts`: counts of rows with each
nonempty optional field. Coverage and programme counts must agree with v1.
The SHA-256 covers the hosted gzip bytes. Check server ID, supported schema,
checksum, and download/decompression size limits before replacing a cached
guide. Parse the object rows on a background dispatcher and retain the last
verified guide if refresh fails. An app must explicitly implement the v2
object schema before switching to this endpoint.

The selected SQLite handoff now uses spool schema/writer version 2. Old spools
are rejected rather than accepted with missing rich details. Direct XMLTV
ingestion and the inventory synchronizer's single-pass spool retain the same
programme facts, include them in the integrity seal, and preserve them through
duplicate selection. Unknown or absent source facts stay absent.
