# SKY TV EPG Version 1 — technical reference

This document defines the production contracts for the private Google Sheet,
provider inventory synchronization, streaming EPG build, and GitHub Pages
deployment. For the beginning-to-end setup instructions, use
[`START_HERE_VERSION_1.md`](START_HERE_VERSION_1.md).

## Production architecture

Version 1 uses the existing `sky2135/skytv-epg` repository and its `main`
branch. It does not need a second repository or a `gh-pages` branch.

1. A private Google Sheet is the authoritative mapping database.
2. `scripts/sync_channel_inventory.py` reads each provider's authenticated live
   channel inventory.
3. Exact `(server_id, stream_id)` identities are compared with `Mappings`.
4. The complete EPGShare channel catalog is frozen during one bounded XMLTV
   stream and checked against EPGShare's sectioned companion ID catalog. The
   pinned matcher may propose an EPG for previously unseen
   identities only; a proposal is activated only after exact-ID, route, and
   current/future programme-gate verification from that same open source file.
   The compressed SHA-256, safety scan, catalog, and programmes are bound to
   one verified file descriptor rather than separate pathname reads.
   Uncertain rows remain disabled in `REVIEW`. Existing mapping rows are not
   edited or deleted.
5. Possible stream-ID reuse is recorded in the private `Sync Alerts` tab and
   quarantined in the temporary build snapshot.
6. The synchronizer seals selected EPGShare channels/programmes in a temporary
   SQLite spool. `scripts/build_epg_streaming.py` verifies its source binding,
   schema, request history, content limits, freshness, and logical integrity,
   then reuses it without a second XML parse. It downloads only explicitly
   required Server 2/3 panel guides.
7. Validated XMLTV, app JSON, personalization metadata, indexes, and reports
   are deployed as a GitHub Pages artifact.

The Google Sheet remains **Restricted**. GitHub Actions uses a dedicated Google
service-account key and the Google Sheets API. Version 1 does not publish the
Sheet as CSV and does not commit a generated mapping CSV to Git.

The effective CSV exists only in `.build/channel-sync/` on the temporary
Actions runner. Before the builder starts, every file in that private directory
is checked for provider passwords, private provider addresses,
credential-shaped usernames, and Google service-account values. All provider
passwords, base URLs, and high-risk Google
values use raw and common serialized-form comparisons. Provider usernames use
credential-field and URL-context checks; distinctive usernames also receive a
global raw/serialized search. A bare short or common username in ordinary EPG
prose is intentionally not treated as a leak because it cannot be distinguished
reliably. After the build, the same policy scans every regular file in `public/`
and `.build/reports/`; gzip files are checked through streaming decompression. A
match stops both artifact uploads without printing the secret. The scanner
receives credentials only for comparison, and Server 1 credentials remain
deliberately absent from the EPG-building step itself.

## What “all server channels” means

The first successful write-enabled bootstrap makes `Mappings` contain every
valid, uniquely identified **live channel row returned by the three configured
providers** at that time, combined with the migration starter rows.

This definition has deliberate limits:

- It covers live channels returned by `get_live_streams`, or live `#EXTINF`
  entries from the authenticated M3U fallback.
- It excludes movies, series, and other video-on-demand entries.
- It cannot include a channel that the provider account does not expose or
  return during the check.
- Every provider row must have both a channel name and a stream ID. A partial
  row stops the sync instead of being silently discarded.
- Duplicate stream IDs in one provider inventory stop the sync. Version 1 does
  not guess which duplicate is correct.
- The provider API is preferred. The M3U fallback extracts a numeric stream ID
  from the stream URL when possible; otherwise it creates a deterministic
  `m3u_...` identity by hashing the URL path, channel name, and EPG ID. The raw
  stream URL is never stored or reported. A provider URL path can itself contain
  account data, so the derived ID is opaque provider data rather than proof that
  every hash input was non-secret; changing the provider path or credentials may
  change that identity.
- One server inventory is limited to 250,000 valid channels.

Bootstrap mode requires valid inventories from Server 1, Server 2, and Server
3 and applies the configured minimum channel floors. Refresh mode may continue
with the servers that are available. If one provider is unavailable during a
refresh, its Sheet rows are preserved and Version 1 makes no missing-channel
claim for that server.

## Private Google Sheet contract

The supplied workbook contains these required tabs:

- `START HERE` — short operator instructions;
- `Mappings` — the authoritative 33-column channel table;
- `Sync Alerts` — persistent stream-identity safety alerts; and
- `Taxonomy` — controlled metadata values and examples.

The workbook's `Mappings` tab and the supplied
`SKYTV_EPG_VERSION_1_MAPPING_SEED.csv` initially contain the same 25,170
historical rows. The one-time `scripts/export_google_sheet_seed.py` exporter
merges `mappings/server_1_final_mapping.csv`,
`mappings/server_2_final_mapping.csv`, and
`mappings/server_3_final_mapping.csv` into the canonical 33-column seed. The
seed remains frozen; after import and the first synchronization, the private
Google Sheet is the changing production authority.

Do not rename `Mappings` or `Sync Alerts`, rearrange their columns, add title
rows above their headers, merge cells in their data ranges, or delete their
header rows. Sorting and filters are safe. The synchronizer copies formatting
and data validation from `Mappings` row 2 to newly appended rows.

The unique mapping key is:

```text
(server_id, stream_id)
```

`stream_id` is provider-local, so the same number may safely occur on different
servers. The same pair may appear only once.

### Mappings schema

`Mappings` must contain these 33 columns in exactly this order:

```csv
server_id,server_label,region_code,genre,primary_language,stream_id,enabled,channel_name,canonical_name,category_id,category_name,channel_number,country_codes,language_codes,subgenres,sport_codes,religion_codes,audience_codes,content_rating,channel_role,tags,sort_priority,action,source,epg_feed,epg_id,logo_url,metadata_status,metadata_source,metadata_confidence,metadata_locked,reason,notes
```

| Column | Contract |
|---|---|
| `server_id` | Stable ID such as `server_1`, `server_2`, or `server_3`. |
| `server_label` | Human-readable server label. |
| `region_code` | Controlled geographic region, such as `south_asia` or `north_america`. |
| `genre` | Controlled primary content genre, such as `sports`, `religion`, or `news`. |
| `primary_language` | Primary BCP-47 language code; `und` means undetermined and `mul` means multilingual. |
| `stream_id` | Exact provider live-stream identity; unique within one server. |
| `enabled` | Boolean inclusion control: `TRUE` or `FALSE`. |
| `channel_name` | Provider-facing channel name retained for exact identity and drift checks. |
| `canonical_name` | Clean display name used by generated outputs. |
| `category_id` | Provider category identity. |
| `category_name` | Provider category label and classification evidence. |
| `channel_number` | Optional provider/display channel number. |
| `country_codes` | Pipe-separated ISO alpha-2 country codes, for example `IN|CA`. |
| `language_codes` | Pipe-separated BCP-47 language codes. |
| `subgenres` | Pipe-separated controlled tokens for finer content grouping. |
| `sport_codes` | Pipe-separated controlled sport values; valid only when `genre=sports`. |
| `religion_codes` | Pipe-separated controlled faith values; valid only when `genre=religion`. |
| `audience_codes` | Pipe-separated audience values such as `general`, `family`, `kids`, or `adults`. |
| `content_rating` | `general`, `parental_guidance`, `mature`, `adult`, or `unknown`. |
| `channel_role` | `linear`, `event`, `ppv`, `timeshift`, `radio`, `virtual`, or `unknown`. |
| `tags` | Optional pipe-separated controlled tokens for app grouping. |
| `sort_priority` | Integer ordering hint; default is `1000`. |
| `action` | Mapping state. Active values are `APPROVED`, `MANUAL`, `AUTO_EPGSHARE`, `KEEP_PANEL`, and `AUTO_DUMMY`; review/exclusion values are listed below. |
| `source` | Requested schedule namespace: `epgshare01`, `panel`, or `dummy`; legacy `epgshare` is accepted as an alias. |
| `epg_feed` | `ALL_SOURCES1` for EPGShare, `panel` for native Server 2/3 EPG, or `DUMMY_CHANNELS` for an approved placeholder. |
| `epg_id` | Exact XMLTV channel ID in the selected schedule source. |
| `logo_url` | Optional reviewed public logo URL. Provider playlist icon URLs are never imported automatically. |
| `metadata_status` | `approved`, `auto`, `review`, or `unknown`. |
| `metadata_source` | `manual`, `registry`, `epgshare`, `provider_category`, `channel_name`, or `default`. |
| `metadata_confidence` | Decimal from `0` through `1`. |
| `metadata_locked` | `TRUE` after editorial metadata has been checked and should be treated as authoritative. |
| `reason` | Short explanation of the current mapping or review state. |
| `notes` | Private operator notes and discovery timestamps; omitted from public reports. |

Core controlled personalization values are maintained in the workbook's
`Taxonomy` tab and emitted to `EPG/taxonomy.json`. The workbook additionally
documents operational actions, sources, metadata states, and common language
examples. Country codes, valid BCP-47 language codes, free controlled
subgenres, and tags are validated by format or policy rather than represented
as one exhaustive list in `taxonomy.json`.

Rows with `REVIEW`, `UNMATCHED`, `NO_EPG`, `UNRESOLVED`, `SKIP`, `IGNORE`, or
`REJECTED` are not runnable schedule mappings. An enabled active row must have
an exact `epg_id`. `KEEP_PANEL` requires `source=panel`, `AUTO_EPGSHARE`
requires EPGShare, and `AUTO_DUMMY` requires the dummy source.

The same runtime-eligibility gate controls schedule selection and per-server
public metadata. A disabled or review-state row is absent from both and cannot
enter personalization. An `OPEN` identity alert makes the temporary snapshot
disabled and review-state even when the stored mapping had been active.

### Newly discovered rows

Only previously unseen exact `(server_id, stream_id)` keys enter automatic
matching. Existing rows—including prior `REVIEW` rows and manual edits—are not
rematched or rewritten.

A new row is activated as `AUTO_EPGSHARE` only when all of these independent
checks succeed:

- the frozen matcher and legacy engine hashes/build IDs match their manifest;
- the complete combined-source catalog passes its minimum-size and XML safety
  checks and exactly agrees with the official sectioned ID catalog;
- the method is an allowlisted deterministic identity method, never fuzzy or
  broad containment;
- exactly one case-sensitive, real, non-dummy EPG ID is selected;
- the channel has an explicit unambiguous market and the catalog route agrees;
- the identity is not adult or a generic numbered placeholder; and
- the same source snapshot has at least two distinct informative programme
  time intervals, its first useful interval starts within six hours, and its
  final useful interval reaches at least six hours beyond the check time.

Every other result is appended with `enabled=FALSE` and `action=REVIEW`.
Server 1 panel identifiers are stripped before matching and can never be
activated. Provider icon URLs are discarded because their paths may contain
account credentials.

Automatic EPG approval changes only schedule-control fields. New rows retain
`metadata_status=review`; personalization language, region, genre, sport, and
religion are not promoted to human-approved status.

After confirming the channel identity, exact EPG source/ID, and personalization
metadata, the owner sets `action=APPROVED` and `enabled=TRUE`. Approval without
enabling deliberately leaves the row excluded.

If a channel should remain excluded, set `enabled=FALSE` and `action=IGNORE`
instead of deleting it. Deleting it makes the next inventory refresh see it as
new and append it again.

## Append-only synchronization

The Google Sheets writer uses row-inserting value appends in retry-safe chunks
and rereads every appended cell afterward. This path also supports Google
native tables created when the supplied XLSX is imported; their table range is
expanded after the values exist. Before appending, it compares exact mapping
keys again so a rerun does not intentionally duplicate already accepted rows.
It never sends an update or delete request for an existing `Mappings` row.

The following conditions block a mass append:

- invalid or out-of-order Version 1 columns;
- duplicate mapping or provider identities;
- invalid provider records;
- projected Sheet size or row-limit overflow;
- an unexpectedly small bootstrap inventory;
- all-server bootstrap without all three valid inventories; or
- suspiciously low stream-ID overlap indicating a changed/rotated provider ID
  namespace.

Changed names/categories and missing provider rows are reported without
modifying the existing `Mappings` row. The synchronizer may append a durable
entry to `Sync Alerts`. A disappeared channel remains in `Mappings` until the
owner explicitly disables it.

## Persistent Sync Alerts quarantine

`Sync Alerts` is a durable safety input, not a disposable report. It has exactly
11 columns in this order:

```csv
detected_at,server_id,stream_id,alert_type,sheet_channel_name,provider_channel_name,sheet_category_name,provider_category_name,action_taken,status,review_notes
```

Version 1 currently persists `POSSIBLE_STREAM_ID_REUSE` alerts. This occurs
when a provider returns an existing `(server_id, stream_id)` with a materially
different core channel identity. Ordinary spelling, spacing, quality suffix,
or category drift may be reported without becoming a persistent quarantine.

An alert lifecycle is:

1. The mismatch is detected.
2. A deduplicated row is appended with `status=OPEN` and
   `action_taken=QUARANTINED_IN_EFFECTIVE_SNAPSHOT`.
3. The original `Mappings` row remains untouched for auditability.
4. The temporary effective mapping changes that identity to `enabled=FALSE`
   and `action=REVIEW`, preventing the old schedule and metadata from being
   published for a newly reused stream ID.
5. Every later run keeps that `(server_id, stream_id)` quarantined while any
   matching alert remains `OPEN`, including when the provider is temporarily
   unavailable.
6. The owner checks the provider identity, fixes or replaces the mapping, adds
   a note, and changes the alert status to `RESOLVED`.
7. The resolved row remains unchanged as audit history.
8. If the mismatch still exists after resolution, a later refresh appends a
   new `OPEN` alert for that identity. Resolution is therefore not a bypass for
   an unfixed mapping.

Only `OPEN` and `RESOLVED` are valid status values. Do not delete an alert
instead of resolving it; set `RESOLVED` with `review_notes`. Old resolved rows
may later be moved through the documented Sheet-limit archival procedure.

## Inventory synchronization commands

The GitHub workflows are the supported operating interface. These commands are
provided for testing and maintenance.

### Strict first bootstrap

```bash
python -u scripts/sync_channel_inventory.py \
  --mode bootstrap \
  --sheet-id "$GOOGLE_SHEET_ID" \
  --sheet-tab Mappings \
  --output-dir .build/channel-sync \
  --snapshot-out .build/channel-sync/effective_mapping.csv \
  --all-source-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz" \
  --all-source-catalog-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.txt" \
  --epgshare-spool-out "$RUNNER_TEMP/skytv-epg-v1/selected_epg.sqlite3" \
  --minimum-server-channels \
    server_1=4000 server_2=10500 server_3=9400 \
  --write-to-sheet
```

Bootstrap mode requires all three servers. Omit `--write-to-sheet` for a
report-only safety run.

### Normal refresh

```bash
python -u scripts/sync_channel_inventory.py \
  --mode refresh \
  --sheet-id "$GOOGLE_SHEET_ID" \
  --sheet-tab Mappings \
  --output-dir .build/channel-sync \
  --snapshot-out .build/channel-sync/effective_mapping.csv \
  --all-source-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz" \
  --all-source-catalog-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.txt" \
  --epgshare-spool-out "$RUNNER_TEMP/skytv-epg-v1/selected_epg.sqlite3" \
  --write-to-sheet
```

Refresh mode preserves unavailable servers and produces an effective snapshot
from the authoritative Sheet plus the active quarantine state.

### Offline diagnostic

```bash
python -u scripts/sync_channel_inventory.py \
  --mode refresh \
  --mapping-file path/to/mapping.csv \
  --output-dir .build/channel-sync \
  --snapshot-out .build/channel-sync/effective_mapping.csv
```

`--mapping-file` is for tests/offline diagnostics and cannot be combined with
`--write-to-sheet`. Provider credentials are still read from the
`SERVER_1_*`, `SERVER_2_*`, and `SERVER_3_*` environment variables.

Use `--servers` to select a subset in refresh mode, `--alerts-tab` only when a
deliberately renamed alerts tab is also configured everywhere, and
`--allow-insecure-http` only when a provider offers no HTTPS service.

The local diagnostic directory contains:

```text
summary.json
inventory.csv
new_channels.csv
changed_channels.csv
possible_id_reuse.csv
missing_channels.csv
mapping_before_append.csv
effective_mapping.csv
```

Full inventory and mapping snapshots are not uploaded as public-repository
artifacts. The manual sync workflow retains only aggregate `summary.json` for
seven days.

## Streaming build command

The production workflow runs the equivalent of:

```bash
python -u scripts/build_epg_streaming.py \
  --mapping-file .build/channel-sync/effective_mapping.csv \
  --all-source-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz" \
  --epgshare-spool-file "$RUNNER_TEMP/skytv-epg-v1/selected_epg.sqlite3" \
  --public-dir public \
  --work-dir .build/work \
  --servers server_1 server_2 server_3 \
  --server-1-policy epgshare-only \
  --minimum-server-rows server_1=4000 \
  --minimum-server-rows server_2=10500 \
  --minimum-server-rows server_3=9400 \
  --icon-config config/channel_icons.csv \
  --public-base-url "$EPG_PUBLIC_BASE_URL" \
  --minimum-coverage "${EPG_MINIMUM_COVERAGE:-80}"
```

The workflow downloads the combined guide and its small official sectioned ID
catalog once. The synchronizer requires their exact channel-ID sets to agree,
parses the guide once, and creates the temporary spool used by the builder.
The companion catalog prevents an incomplete source or an unknown dummy section
from becoming an automatic real-channel match. Local fixtures may use repeated
`--panel-file server_2=/path/guide.xml.gz` arguments. A Server 1 panel file is
always rejected.

The build writes to a staging directory, validates all expected outputs, and
then replaces `public/` atomically. Repository-relative work paths are limited
to `.build/work`, and the only repository-relative publish target is `public/`.

## Server 1 source boundary

Server 1 credentials are used only for live-channel inventory discovery.
Server 1's native XMLTV is never downloaded or passed to the streaming builder.

Every runnable Server 1 row must use an exact EPGShare channel ID. A legacy
Server 1 row that still requests `panel` is quarantined even if its native ID
happens to look like an EPGShare ID. Provider and EPGShare identifiers are
different namespaces and are never treated as interchangeable.

Servers 2 and 3 may use EPGShare or their native panel XMLTV row by row. Panel
guides are downloaded only when an eligible mapping actually requests them.

## GitHub configuration

### Repository variables

| Name | Required | Purpose |
|---|---:|---|
| `GOOGLE_SHEET_ID` | Yes | ID between `/d/` and `/edit` in the private Sheet URL. |
| `GOOGLE_SHEET_TAB` | Yes | Must be `Mappings` for the supplied workbook. |
| `EPG_PUBLIC_BASE_URL` | Recommended | `https://sky2135.github.io/skytv-epg` or an approved custom domain. |
| `EPG_MINIMUM_COVERAGE` | No | Percentage gate; workflow default is `80`. |
| `ALLOW_INSECURE_PANEL_HTTP` | No | Set to `true` only if a provider cannot serve HTTPS. |

The obsolete `EPG_MAPPING_CSV_URL` variable is not used by Version 1.

### Repository secrets

```text
GOOGLE_SERVICE_ACCOUNT_JSON
SERVER_1_BASE_URL
SERVER_1_USERNAME
SERVER_1_PASSWORD
SERVER_2_BASE_URL
SERVER_2_USERNAME
SERVER_2_PASSWORD
SERVER_3_BASE_URL
SERVER_3_USERNAME
SERVER_3_PASSWORD
```

`GOOGLE_SERVICE_ACCOUNT_JSON` is the complete JSON key for the service account
shared as an Editor on this one Sheet. No Google Cloud project-wide role is
required. Base URLs must not contain credentials, query strings, or fragments.
Redirects are constrained so credentials cannot silently move to another host.

Using an `http://` provider requires the explicit insecure option and sends
that provider's account credentials without transport encryption.

GitHub Pages must use **Source: GitHub Actions**. The deployment job receives
only `pages: write` and `id-token: write`; the repository is otherwise checked
out read-only and generated files are not committed.

## Workflows and scheduling

- `.github/workflows/channel_inventory_sync.yml` is a manual bootstrap or
  on-demand inventory check. Its checkbox defaults to report-only and controls
  whether new rows and alerts are appended.
- `.github/workflows/main.yml` runs daily at 04:37 in `America/Toronto` and may
  also be started manually. It performs a refresh with Sheet writes, builds,
  validates, and deploys.

Both workflows use the same non-cancelling concurrency group. This serializes
Sheet writes and Pages deployments without cancelling an in-progress publish.

## Published endpoints

With the current repository name, the base URL is:

```text
https://sky2135.github.io/skytv-epg
```

Primary endpoints are:

```text
/health.json
/epg/index.json
/epg/server_1_tivimate.xml.gz
/epg/server_2_tivimate.xml.gz
/epg/server_3_tivimate.xml.gz
/EPG/index.json
/EPG/taxonomy.json
/EPG/server_1_epg.json.gz
/EPG/server_2_epg.json.gz
/EPG/server_3_epg.json.gz
/EPG/server_1_metadata.json.gz
/EPG/server_2_metadata.json.gz
/EPG/server_3_metadata.json.gz
/EPG/server_1_epg_manifest.json
/EPG/server_2_epg_manifest.json
/EPG/server_3_epg_manifest.json
/EPG/server_1_metadata_manifest.json
/EPG/server_2_metadata_manifest.json
/EPG/server_3_metadata_manifest.json
```

Per-server validation and TiviMate manifests are under
`/reports/server_X/`. Output paths are case-sensitive: `epg` is the XMLTV
directory and `EPG` is the app-data directory.

The app guide preserves schema Version 1 three-item programme tuples:

```json
[start_epoch, stop_epoch, "title"]
```

Personalization facets are in the separate metadata files. Clients should use
AND between requested dimensions, OR within one dimension, and apply parental
exclusions before preference matching.

This repository produces the data contract only. It does not contain or modify
the custom app's preference screens or client-side filtering implementation.

## Safety and resource limits

- Generic builder mapping input: at most 50 MiB and 250,000 data rows.
- Private Google `Mappings` tab: at most 150,000 data rows.
- Google Mappings API response: at most 80 MiB.
- `Sync Alerts`: at most 10,000 data rows and 8 MiB serialized.
- Provider JSON response: at most 64 MiB.
- Authenticated M3U response: at most 128 MiB after decompression.
- Provider inventory: at most 250,000 live rows per server.
- Leak checks reject provider passwords and base URLs wherever found, plus
  usernames in credential-shaped fields and URLs. Every provider password
  (minimum four characters) is searched
  globally in raw and common serialized forms; a collision with ordinary EPG
  text must be resolved by rotating that password to a longer, random value,
  never by bypassing the check. Provider usernames may be any nonempty length:
  short or common usernames are checked in credential fields and URLs, while
  distinctive usernames are additionally searched as raw serialized values.
  A bare short or common username in ordinary EPG prose is not treated as a
  leak because doing so would make legitimate guides fail unpredictably.
  Provider base URLs and high-risk Google service-account values are searched
  globally.
- EPG source: at most 1 GiB compressed and 4 GiB expanded.
- Automatic matching requires at least 25,000 unique IDs and exact agreement
  between the XML channel set and the sectioned companion catalog.
- XMLTV parse: at most 20 million elements and 10,000 child elements in one
  record; DTD/entity declarations are rejected.
- SQLite ingestion batch: 2,000 programme rows.
- Build process virtual-memory ceiling: 6 GiB in GitHub Actions.
- Inner build timeout: 95 minutes; build job timeout: 120 minutes.
- GitHub Pages artifact: at most 900 MiB in this workflow, leaving margin below
  the 1 GiB site limit.

Downloads, gzip structure, XML structure, timestamps, channel references, JSON
syntax, coverage, output existence, symbolic links, hard links, and final file
hashes are checked before deployment.

## Known operational limits

- The migration workbook starts with 25,170 historical mapping rows; it is not
  proof of a current complete provider inventory until bootstrap succeeds.
- Those starter rows are schedule-mapping history, not fully curated
  personalization metadata. All 25,170 metadata records are initially inferred
  and unlocked; 22,720 have `primary_language=und`, 11,447 have
  `region_code=unknown`, and 8,030 have `genre=unknown`. Unknown dimensions do
  not match specific preferences.
- The starter includes 11,939 approved dummy-guide placeholders and 171
  disabled rows in `REVIEW`. Those 171 rows are excluded from schedules, public
  metadata, and personalization until reviewed, approved, and enabled; an
  inventory row and a real programme schedule are not the same guarantee.
- New rows are discovered automatically. Exact, single-candidate,
  region-consistent EPGShare identities with a strong same-snapshot programme
  guide are activated automatically. All fuzzy, ambiguous, adult, dummy,
  generic-numbered, or weak-guide matches require human review.
- Missing and ordinary drift rows are reported but not deleted or rewritten.
- Only possible stream-ID reuse is persisted in `Sync Alerts`; ordinary drift
  and missing counts are not a historical inventory database.
- An `OPEN` reuse alert remains a hard quarantine until explicitly resolved.
- A provider API or M3U can only describe what that account exposes at that
  moment. Provider-side omissions are not recoverable by the workflow.
- Google Sheets API quotas, a revoked service-account key, removed Sheet access,
  provider outages, EPGShare outages, and delayed GitHub schedules can postpone
  a refresh. The last valid Pages deployment remains available when a new build
  fails before deployment.
- A large provider stream-ID namespace rotation is blocked for manual review
  instead of appending thousands of likely duplicates.
- Synthetic `m3u_...` identities can change if the provider changes the entry's
  path, name, and EPG ID together.
- `ALLOW_INSECURE_PANEL_HTTP=true` is compatibility support, not equivalent to
  HTTPS security.
- Public GitHub repositories expose workflow logs and uploaded artifacts. Full
  inventories and mapping snapshots therefore remain on the temporary runner;
  only aggregate sync summaries are retained.
- The current repository is public. GitHub Pages from a private repository may
  require a paid plan, and `actions/configure-pages` requires `pages: read` in
  its build job for private resources.
- GitHub Pages also has a 100 GB-per-month soft bandwidth limit, a 1 GiB
  published-site limit, and a 10-minute deployment timeout. Version 1's custom
  Actions workflow is not subject to the separate soft ten-builds-per-hour
  limit, but Pages may still rate-limit excessive use.
- This Pages deployment target is for personal, noncommercial use. GitHub Pages
  is not permitted as free hosting for an online business or commercial SaaS;
  a customer-facing or high-bandwidth deployment requires an object-store/CDN
  publishing design instead.

### Sheet-limit recovery

The active `Mappings` tab is capped at 150,000 data rows. Rows cannot simply be
archived out of that tab because the exact-key inventory would discover and
append them again. Reaching this limit requires an intentional capacity or
tombstone-design update before another Sheet write.

The active `Sync Alerts` tab is capped at 10,000 rows and 8 MiB. To preserve the
audit trail while freeing active capacity, copy old `RESOLVED` rows into a
private `Sync Alerts Archive` tab using values-only paste, verify the copy, and
then delete only those copied `RESOLVED` rows from the active tab. Never remove
an `OPEN` alert from the active tab. Run a report-only bootstrap after archival
before enabling Sheet writes again.

## Frozen compatibility components

Files and symbols containing `v7` or `v8` are internal, frozen compatibility
components inherited from the reviewed notebook matcher. They are not the
current product version.

- Builder v7.1 preserves established TiviMate/app compatibility behavior.
- Smart Rules v8.4 preserves the reviewed contextual matching and regression
  corpus used during migration and manual analysis.
- Scheduled Version 1 production does not run fuzzy rematching.
- Integrity hashes and regression tests protect these boundaries.

Do not rename or casually edit the frozen modules merely to make their internal
version numbers say “1”. Version 1 is the complete production workflow around
those compatibility components.
