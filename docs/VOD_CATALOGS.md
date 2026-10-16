# Separate movie and series catalogs

## Purpose and boundary

`scripts/build_vod_catalogs.py` publishes the provider's on-demand movies and
series as metadata-only catalogs under `public/VOD/`. It does **not** add VOD
titles to the linear EPG, alter a mapping row, or publish a playback URL.
Linear movie and entertainment channels remain ordinary channels in the EPG;
only provider VOD titles belong in these catalogs.

The public schema never guesses missing facts. Unknown language is `und`,
unknown genre is `unknown`, and unknown release date, adult status, TMDB ID, or
artwork is `null`. In particular, a country or server market is not evidence of
a title's language. Applications using parental controls should treat
`adult: null` as unclassified, not as child-safe.

## Production input

The publisher uses the same environment convention as channel inventory:

- `SERVER_1_BASE_URL`, `SERVER_1_USERNAME`, `SERVER_1_PASSWORD`
- `SERVER_2_BASE_URL`, `SERVER_2_USERNAME`, `SERVER_2_PASSWORD`
- `SERVER_3_BASE_URL`, `SERVER_3_USERNAME`, `SERVER_3_PASSWORD`

It calls the standard Xtream actions `get_vod_categories`, `get_vod_streams`,
`get_series_categories`, and `get_series`. HTTPS is required unless the
operator explicitly passes `--allow-insecure-http`. Responses have hard size
and item-count limits. Redirects must preserve the same host, API path and
original query parameters; other redirects are rejected. Error messages omit provider
data, and a provider-reflected configured password stops publication.

Basic publication, using all three configured servers:

```text
python scripts/build_vod_catalogs.py --output-dir public/VOD
```

The list actions frequently omit release, genre, language, and TMDB details.
`--fetch-details` additionally calls `get_vod_info` or `get_series_info` once
per item. That option can issue many requests and should be enabled only when
the provider permits the load. Without it, the schema remains complete but
unavailable facts stay explicitly unknown.

## Offline and replayable input

No credentials or network access are needed to normalize saved provider
responses. Select an input directory and one or more servers:

```text
python scripts/build_vod_catalogs.py \
  --server server_1 \
  --input-dir .build/vod-input \
  --output-dir public/VOD
```

The simplest input is one object named `server_1_vod_bundle.json`:

```json
{
  "get_vod_categories": [],
  "get_vod_streams": [],
  "get_series_categories": [],
  "get_series": [],
  "vod_details": {
    "123": {"info": {"tmdb_id": 550, "language": "en"}}
  },
  "series_details": {}
}
```

The two list keys are required. Category and detail keys are optional. Detail
objects are keyed by provider stream or series ID.

Instead of a bundle, the input directory can contain these separate files:

```text
server_1_vod_categories.json
server_1_vod_streams.json
server_1_series_categories.json
server_1_series.json
server_1_vod_details.json       (optional)
server_1_series_details.json    (optional)
```

## Item schema

Each gzip catalog has a small envelope followed by `items`:

```json
{
  "schemaVersion": 1,
  "serverId": "server_1",
  "contentType": "movie",
  "generatedAt": "2026-10-02T16:30:00Z",
  "itemCount": 1,
  "items": [
    {
      "id": "123",
      "title": "Example",
      "category": {"id": "10", "name": "English Action"},
      "language": "en",
      "languages": ["en"],
      "genre": "action",
      "genres": ["action", "drama"],
      "releaseDate": "2025-04-03",
      "adult": false,
      "tmdbId": "550",
      "artwork": {
        "posterUrl": "https://image.tmdb.org/t/p/w500/example.jpg",
        "backdropUrl": null
      }
    }
  ]
}
```

`language` is an ISO 639-1 code, `mul`, or `und`. `languages` retains the known
component languages when a title is multilingual. A release date may be
`YYYY`, `YYYY-MM`, or `YYYY-MM-DD` so the publisher never invents missing month
or day precision. Artwork must be a credential-free HTTP(S) URL; query strings
and fragments are removed. Artwork hosted on the configured provider hostname
is left `null` so the public catalog never exposes that private origin; TMDB and
independent public-CDN artwork remains eligible.

## Safe release order

Files are published in this order:

1. content-addressed `server_N_movies.<hash>.json.gz`;
2. content-addressed `server_N_series.<hash>.json.gz`;
3. content-addressed `server_N_vod_manifest.<hash>.json` with SHA-256 hashes,
   sizes, counts, skipped rows, and metadata-completeness counts;
4. stable `index.json`, atomically replaced only after every server succeeds.

`index.json` is the release switch. A client should fetch it first, verify the
named per-server manifest against `manifestSha256`, then verify each compressed
catalog before using it. If any file is absent or a hash differs, the client
should retain its last verified release. Because data and manifests are
content-addressed, the old index remains valid while a new release is being
uploaded.

The deployment workflow can run the same checks without credentials or network
access:

```text
python scripts/build_vod_catalogs.py --validate-only \
  --expected-server server_1 \
  --expected-server server_2 \
  --expected-server server_3 \
  --output-dir public/VOD
```

Validation rejects path traversal and symlinks, verifies the index-to-manifest
and manifest-to-catalog SHA-256 chain, bounds gzip expansion, checks every
schema and item count, recalculates metadata-completeness totals, and confirms
that the complete production server set is present. Omit `--expected-server`
only when validating an intentionally partial offline build. Extra
unreferenced files are ignored; only files reachable from the current
`index.json` belong to the current release.

Generated content-addressed files can be removed only when no retained index
or cached client release points to them. A GitHub Pages build from a fresh
checkout naturally contains only the files for that build.
