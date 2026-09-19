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
   stream and corroborated against EPGShare's sectioned companion ID catalog.
   EPGShare can publish those two files hours apart during a non-atomic
   rollover, so Version 1 uses
   only their exact, case-sensitive intersection and accepts only the small,
   bounded rollover described below. The pinned matcher proposes an EPG for
   previously unseen identities. Workflow 1 may also retry an
   explicit set of eligible existing disabled `REVIEW` identities. A proposal
   is activated only after exact-ID, route, and current/future programme-gate
   verification from that same open source file.
   The compressed SHA-256, safety scan, catalog, and programmes are bound to
   one verified file descriptor rather than separate pathname reads.
   Uncertain rows remain disabled in `REVIEW`. Ordinary refreshes do not edit
   existing rows. The opt-in recheck can update only the bounded fields and
   rows defined below; rows are never deleted.
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
header rows. Sorting and filters are safe while no workflow is writing. Do not
edit, sort, insert, or delete rows while a write-enabled sync or recheck is
queued or running. The synchronizer copies formatting and data validation from
`Mappings` row 2 to newly appended rows.

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

### Newly discovered rows and explicit REVIEW rechecks

New-channel discovery sends only previously unseen exact
`(server_id, stream_id)` keys to automatic matching. Existing rows and manual
edits are not rematched by that path. Workflow 1 has a separate `off` /
`dry-run` / `apply` control for an explicit allowlist of existing disabled
`REVIEW` rows. Scheduled runs use `apply` for all three servers; manual runs
may still select another mode or narrower scope. `apply` alone is not write
authority: the independent total apply limit defaults to zero.

A new row is activated as `AUTO_EPGSHARE` only when all of these independent
checks succeed:

- the frozen matcher and legacy engine hashes/build IDs match their manifest;
- the complete combined-source catalog and official sectioned ID catalog pass
  the minimum-size, exact-intersection, bounded-rollover, and XML safety checks;
- the official text catalog has no ID declared as both real and dummy, and no
  real ID with contradictory or multiple-country market evidence;
- the method is an allowlisted deterministic identity method, never fuzzy or
  broad containment;
- exactly one case-sensitive, real, non-dummy EPG ID is selected;
- the channel has an explicit unambiguous market and the catalog route agrees;
- the identity is not adult or a generic numbered placeholder; and
- the same source snapshot has at least two distinct informative programme
  time intervals, its first useful interval starts within six hours, and its
  final useful interval reaches at least six hours beyond the check time.

Two explicitly non-real deterministic outcomes are separate from that approval
path. A verified allowlisted placeholder family may be enabled as
`AUTO_DUMMY`, and a decorative heading may be disabled as `IGNORE`. Neither is
reported as a real EPG match. After the catalog-wide preflight succeeds, every
other row-level result is appended with `enabled=FALSE` and `action=REVIEW`. A
catalog-wide contradiction stops the run before any new row is written; it is
not converted into one particular channel's review result.
Server 1 panel identifiers are stripped before matching and can never be
activated. Provider icon URLs are discarded because their paths may contain
account credentials.

Automatic EPG approval changes only schedule-control fields. New rows retain
`metadata_status=review`; personalization language, region, genre, sport, and
religion are not promoted to human-approved status.

For the existing-row recheck, a candidate must be in the selected server
scope, be present in the current provider inventory, have exactly
`enabled=FALSE` and `action=REVIEW`, have no open or newly detected stream-ID
reuse alert, and have no current provider/mapping identity mismatch. A row with
an untracked manual EPG candidate is also excluded. `APPROVED`, `MANUAL`,
`IGNORE`, active, missing, drifted, and quarantined rows are never candidates.

Smart Rules run first against the same corroborated EPGShare catalog and
same-snapshot programme gate used for new rows. `dry-run` does not write any
existing `REVIEW` row. The separate new-channel append control remains
independent, so operators should leave it off for a completely read-only
preview. `apply` persists at most the explicit `review_apply_limit`: one of
`0`, `25`, `100`, `500`, `2500`, or `5000`, with `0` as the default. The limit
is shared by deterministic real, native, synthetic, heading/`IGNORE`, and AI
updates. It limits persistence, not the number of eligible rows Smart Rules may
analyze. Google batches remain no more than 500 rows and 2 MiB; additional
verified rows are reported as deferred. An
accepted row becomes `enabled=TRUE`, `action=AUTO_EPGSHARE`,
`source=epgshare01`, and `epg_feed=ALL_SOURCES1` with its exact verified ID.
An allowlisted verified placeholder may instead become enabled `AUTO_DUMMY`,
and a decorative heading may become disabled `IGNORE`; both remain outside the
real-EPG count. Server 1 has no panel exception.

For Server 2 and Server 3 rechecks, Workflow 1 also runs a separate native-EPG
lane automatically. It first uses the current API `epg_channel_id`. When that
field is blank, it may recover only the M3U `tvg-id` joined by the same numeric
stream ID and the same Unicode-normalized provider name. The API inventory
continues to own channel identity, name, and category. An API/M3U ID conflict,
trimmed native ID, duplicate M3U attribute, conflicting `tvg-id`/`epg-id`
alias, non-numeric join, name mismatch, manual edit, incomplete discovery note,
provider drift, or `OPEN` alert leaves the row disabled.

A native candidate is not enabled merely because its ID exists. The complete
current panel XMLTV must contain that exact case-unique ID, an unambiguous
compatible `<display-name>`, and at least two informative programme intervals
starting within six hours and extending at least six hours beyond the check
time. Display-name uniqueness is checked against the complete native catalog,
not only the requested IDs. Immediately before a Sheet write, the provider API
and M3U are fetched again; a changed, conflicting, or unavailable identity is
removed from the write set. Only then may the guarded writer create an enabled
`KEEP_PANEL` row. The
writer accepts `KEEP_PANEL` only from the exact in-memory verified allowlist;
the same explicit total apply cap, Sheet fingerprint, alert rereads, atomic
per-batch update, post-write verification, and terminal snapshot checks still apply.
Server 1 never enters this lane and its native XMLTV is never downloaded.

Gemini verification occurs only after Smart Rules and is capped at the smaller
of 200 affected rows or capacity remaining under the total REVIEW apply limit,
with at most 50 represented rows per policy batch. A cluster that does not fit
is deferred whole. Gemini is opt-in for both scheduled and manual Workflow 1
runs; the API-key secret alone never enables it. The
request contains sanitized channel/category text and
two to eight exact real, same-snapshot programme-verified EPGShare choices; it
never contains credentials, provider URLs, playlists, or the full guide. Two
fixed local rankers must independently select the same top candidate, each
with score at least 96 and margin at least 8. Gemini must return schema-valid
`HIGH` for the opaque key bound to that exact candidate. The integration then
rechecks the complete descriptor-bound XML/TXT ID sets, exact real candidate,
programme horizon, terminal Sheet row, OPEN alerts, and current provider
identity. Only this full agreement may write enabled `AUTO_EPGSHARE` with an
`ai-verified-v2` binding. `ABSTAIN`, lower confidence, an invented key, stale
evidence, quota exhaustion, malformed output, or API outage makes no row
change and does not block deterministic work. The free Gemini API tier may use
submitted data to improve Google products.

Before proposals are evaluated, the integration rebuilds durable alias memory
from the authoritative private mapping snapshot. One enabled current human
`MANUAL` or `APPROVED` EPGShare row may teach an exact alias/market/target.
Automatic `AUTO_EPGSHARE` evidence is weaker: its immutable `auto-map-v1` or
tamper-evident `ai-verified-v2` provenance must agree on the identical exact
current real, non-`ALL` target across at least two distinct unchanged servers.
Any open identity alert, competing ID, static-rule conflict, dummy identity,
provider drift, or case/Unicode ambiguity excludes or rejects the group. A
registered alias remains only a deterministic hint; the proposed row must
still pass the normal market, catalog, and same-snapshot programme gates. No
learned file or public mapping data is written.

### EPGShare XML/text catalog rollover contract

EPGShare publishes the large XML guide and its official text catalog as two
separate files. Their replacement is not atomic, so a new copy of one file can
appear hours before the matching copy of the other. Version 1 handles only a
small, measurable rollover difference; it does not broadly reconcile IDs:

- the XML catalog must declare at least 25,000 unique IDs;
- the text catalog must declare at least 25,000 unique IDs;
- their exact, case-sensitive intersection must contain at least 25,000 IDs;
- the combined count of XML-only and text-only IDs must be no more than 64;
- that same combined difference must also be no more than 0.25% of the union;
  both limits apply, so the smaller allowance wins;
- every differing ID must be printable ASCII without whitespace or control
  characters; and
- every XML-only and text-only ID must resolve to one deterministic market;
  an `ALL`/unknown or otherwise unresolved route is not accepted.

The text catalog must also pass catalog-wide semantic preflight. If one exact
ID appears in both a real section and `DUMMY_CHANNELS`, the entire unattended
matching pass fails. For real candidates, a recognized country section such as
`US2` or `IN1` must not contradict the market inferred from the exact ID. A
dotless ID may inherit one unambiguous country market; contradictory evidence
or sections from multiple distinct country markets also fail the entire
preflight. These entries are not simply omitted, because removing a competitor
could make another similar station appear falsely unique.

Only an ID in the exact intersection is eligible for unattended approval.
An XML-only or text-only ID is recorded in the drift counts and quarantined
from automatic approval; a new channel involving it remains disabled in
`REVIEW`. Case-only variants are two different IDs and are never silently
merged. If either catalog or their shared set falls below 25,000 IDs, the
difference exceeds either limit, or a differing ID contains non-ASCII,
whitespace, or control characters, the run fails before a Sheet write. It also
fails if any one-file-only ID lacks one deterministic market, because an
unroutable shadow cannot reliably remain in the matcher competition to block
false uniqueness.

Some shared IDs are unsafe for automatic approval because the frozen matcher's
compatibility normalization could collapse distinct opaque IDs—for example a
Unicode non-breaking-space form and an ordinary-space form. Version 1 omits
such identities from the approvable real/dummy catalog but preserves a
conservative representative in the runtime competition. It can therefore
block a false claim that another candidate is unique, while no
normalization-confusable member can be approved automatically. When this
ambiguity affects a new-channel decision, that row remains in `REVIEW`; it is
never permission to normalize or rewrite the ID. One representative may stand
for IDs that collapse to the same engine-safe identity after casefold and
Unicode-whitespace cleanup only when every member has the same catalog kind
(all real or all dummy) and the identical exact `(feed, region)` route. If such
a confusable family mixes real/dummy status or differs by feed or market,
catalog-wide preflight fails instead of choosing a blocker.

A separate row-level veto covers a shared real candidate whose route is
`ALL`. That candidate remains valid for an exact existing mapping, but it is
not visible inside one explicit-market matcher index. If its strong exact or
station identity collides with a market-routed proposed target, the new
proposal remains disabled in `REVIEW`; this ambiguity does not stop unrelated
rows or rewrite the existing mapping.

After confirming the channel identity, exact EPG source/ID, and personalization
metadata, the owner sets `action=APPROVED` and `enabled=TRUE`. Approval without
enabling deliberately leaves the row excluded.

If a channel should remain excluded, set `enabled=FALSE` and `action=IGNORE`
instead of deleting it. Deleting it makes the next inventory refresh see it as
new and append it again.

## Bounded Google Sheet synchronization

The Google Sheets writer uses row-inserting value appends in retry-safe chunks
and rereads every appended cell afterward. This path also supports Google
native tables created when the supplied XLSX is imported; their table range is
expanded after the values exist. Before appending, it compares exact mapping
keys again so a rerun does not intentionally duplicate already accepted rows.
This new-channel path never sends an update or delete request for an existing
`Mappings` row.

The existing-`REVIEW` apply path is deliberately separate. It records the
exact set of `OPEN` alert identities used by Smart Rules, then rereads
`Sync Alerts` immediately before every mapping mutation. Any added or removed
`OPEN` identity invalidates the complete decision, including learned-alias
evidence, and blocks the write. Immediately before updating existing rows, it
also rereads the authoritative mapping table and requires it to match the
decision snapshot. It then uses independently verified Google Sheets batch
updates of at most 500 rows to touch only these columns on the explicitly
selected rows:

```text
enabled, action, source, epg_feed, epg_id, reason, notes
```

It rereads the Sheet after every request and verifies both the intended cells
and all non-target rows. Every apply run performs one final authoritative mapping
reread—even when it had zero updates—and only that terminal table can become
the build snapshot. Immediately before sealing that snapshot, it rereads
`Sync Alerts` once more and blocks publication if the exact matcher-time
quarantine set changed. A missing, duplicate, moved, newly ineligible, or otherwise
changed target stops the write. No code path deletes a mapping row or rewrites
identity, provider, or personalization fields. Operators must not edit or sort
the Sheet during an apply run because Google Sheets does not provide a general
compare-and-swap transaction across human edits and API updates.

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

The production hand-off contains an authoritative snapshot from the final
post-append Sheet re-read, an effective snapshot after quarantine, and a small
SHA-256-bound manifest. Both CSVs must have the same ordered exact
`(server_id, stream_id)` identities. A quarantined row may differ only in
`enabled`, `action`, `metadata_status`, and the reserved effective-snapshot
reason. The builder recomputes and verifies every count and hash before reading
the EPG source.

Three counts are intentionally distinct:

- **authoritative runnable rows** are eligible immediately before alert
  quarantine and are used by the fixed snapshot-integrity floors;
- **quarantined authoritative runnable rows** are verified rows deliberately
  removed solely by the current-risk/`OPEN` alert set; and
- **effective runnable rows** are the only rows eligible for published output.

Thus `authoritative runnable = effective runnable + quarantined authoritative
runnable` for each server. Already-disabled or `REVIEW` rows receive no floor
credit. An `OPEN` row remains absent from XML, JSON, metadata, and
personalization even though its existence is recognized by the integrity check.

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
  --authoritative-snapshot-out .build/channel-sync/authoritative_mapping.csv \
  --snapshot-manifest-out .build/channel-sync/mapping_snapshot_manifest.json \
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
  --authoritative-snapshot-out .build/channel-sync/authoritative_mapping.csv \
  --snapshot-manifest-out .build/channel-sync/mapping_snapshot_manifest.json \
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
  --snapshot-out .build/channel-sync/effective_mapping.csv \
  --authoritative-snapshot-out .build/channel-sync/authoritative_mapping.csv \
  --snapshot-manifest-out .build/channel-sync/mapping_snapshot_manifest.json
```

`--mapping-file` is for tests/offline diagnostics and cannot be combined with
`--write-to-sheet`. Provider credentials are still read from the
`SERVER_1_*`, `SERVER_2_*`, and `SERVER_3_*` environment variables.

Use `--servers` to select a subset in refresh mode, `--alerts-tab` only when a
deliberately renamed alerts tab is also configured everywhere, and
`--allow-insecure-http` only when a provider offers no HTTPS service.

Workflow 1 additionally maps its controls to these optional arguments:

```text
--review-recheck-mode off|dry-run|apply
--review-recheck-servers server_1 [server_2 server_3]
--review-apply-limit 0|25|100|500|2500|5000
--coverage-fallback-limit 0|500|2500|5000
--use-gemini-ai
--ai-review-limit 10|25|50|100|200
```

`--use-gemini-ai` requires a recheck mode other than `off`. With a nonzero
`--review-apply-limit`, it also requires `GEMINI_API_KEY` in the environment;
an external call occurs only if deterministic decisions leave capacity. A zero
total cap makes no external AI call and does not require a key. The flag does
not grant the model approval authority.

The local diagnostic directory contains:

```text
summary.json
inventory.csv
new_channels.csv
changed_channels.csv
possible_id_reuse.csv
missing_channels.csv
mapping_before_append.csv
authoritative_mapping.csv
effective_mapping.csv
mapping_snapshot_manifest.json
```

Full inventory and mapping snapshots are not uploaded as public-repository
artifacts. Workflow 1 retains only aggregate `summary.json` for seven days.

The displayed GitHub summary is intentionally compact: a per-server table for
total, EPGShare-enabled, native-enabled, review, and placeholder/excluded
channels, followed by the most important results from that run. Detailed
diagnostic counters live in the downloadable `summary.json` artifact.

Catalog rollover evidence is available without publishing the differing IDs.
The detailed artifact records:

| Detail | `summary.json` field | Meaning |
|---|---|---|
| EPG catalog alignment mode | `epgshare_catalog_corroboration_mode` | `exact` when the ID sets are identical; `bounded-drift` when the small-rollover rules were used. |
| EPG IDs confirmed in both catalogs | `epgshare_shared_catalog_channels` | Size of the exact, case-sensitive intersection. |
| XML-only EPG IDs quarantined | `epgshare_xml_only_catalog_channels` | IDs found only in the XML file and excluded from automatic approval. |
| Text-only EPG IDs quarantined | `epgshare_text_only_catalog_channels` | IDs found only in the text file and excluded from automatic approval. |

The recheck portion of `summary.json` uses exact counters rather than a
provider-total subtraction:

| Detail | `summary.json` field | Meaning |
|---|---|---|
| Existing REVIEW recheck mode | `review_recheck_mode` | `off`, `dry-run`, or `apply`. |
| Total existing REVIEW apply cap | `review_apply_limit` | Explicit total persistence cap across every decision lane; allowed values are `0`, `25`, `100`, `500`, `2500`, and `5000`. |
| Existing REVIEW channels eligible | `review_recheck_eligible_rows` | Rows that passed the current provider, state, drift, and alert filters. |
| Existing REVIEW channels checked | `review_recheck_considered_rows` | Eligible rows submitted to Smart Rules. |
| Existing REVIEW channels safely processed | `review_recheck_safe_matches` | Locally safe deterministic EPG, native, placeholder, or heading decisions plus strict AI updates that passed their applicable gates. |
| Existing channels still requiring review | `review_recheck_still_review_rows` | Checked rows that Smart Rules did not verify. |
| Existing REVIEW channels skipped by safety checks | `review_recheck_skipped_rows` | Disabled REVIEW rows in the selected scope excluded because they were missing, drifted, alerted, or held an untracked manual candidate. |
| Verified decisions deferred by the write limit | `review_recheck_deferred_rows` | Verified EPGShare, native, placeholder, or heading decisions not selected under the explicit total cap. |
| Existing REVIEW rows updated in Google Sheet | `review_recheck_rows_updated` | Exact existing-row updates confirmed by the post-write reread. |
| Rows selected by lane | `review_apply_selected_*_rows` | Total plus deterministic, native, synthetic, ignore, and AI selections under the one cap. |
| Rows persisted by lane | `review_apply_persisted_*_rows` | The corresponding exact rows confirmed after the authoritative post-write read. |
| Native EPG candidates found | `native_review_candidates` | Eligible Server 2/3 rows with a fresh non-conflicting provider/M3U native ID. |
| Native EPG matches verified | `native_review_verified` | Candidates that also passed exact XMLTV ID, display-name uniqueness, and current-programme gates. |
| Native EPG matches persisted | `native_review_persisted` | Verified `KEEP_PANEL` updates confirmed after an apply write. This remains zero in dry-run. |
| Native EPG matches deferred | `native_review_deferred` | Verified native rows not selected under the shared total apply cap. |
| Native EPG sources unavailable | `native_review_source_unavailable` | Server 2/3 native XMLTV sources that could not be safely validated; their rows remain REVIEW. |
| Channels considered by Gemini | `ai_review_considered_rows` | Unresolved rows included in bounded Gemini review. |
| Rows deferred from Gemini | `ai_review_deferred_rows` | Otherwise eligible unresolved rows held because of the AI ceiling or remaining total-cap capacity; clusters are never split. |
| Gemini HIGH responses found | `ai_review_high_suggestions_found` | Schema-valid `HIGH` responses choosing a supplied opaque candidate; this is not yet an approval count. |
| AI-verified channels enabled | `ai_review_high_suggestions_persisted` | Rows where both local rankers, Gemini HIGH, catalog/programme checks, and terminal Sheet/provider/alert rereads all agreed and the enabled update was confirmed. |
| Gemini reviews left unresolved | `ai_review_abstained_rows` | Rows that failed or abstained at any strict agreement gate and therefore remained unchanged in `REVIEW`. |
| Gemini reviews unavailable or rejected | `ai_review_error_rows` | API failures or invalid responses that made no row change and remain retryable. |

`summary.json` also keeps the individual `review_recheck_excluded_*` counts,
which distinguish non-review, active, provider-missing, open-alert,
identity-drift, and pre-existing manual-candidate exclusions.

Run-local cross-server learning records these audit counters:

| `summary.json` field | Meaning |
|---|---|
| `cross_server_alias_evidence_rows` | Eligible current human or provenance-bound automatic mapping rows examined as evidence. |
| `cross_server_alias_groups_considered` | Normalized alias/market groups evaluated. |
| `cross_server_aliases_registered` | Non-conflicting aliases admitted from one human approval or matching automatic evidence on at least two distinct servers. |
| `cross_server_alias_groups_rejected` | Evidence groups rejected for insufficient provenance/support, competing IDs, drift, alerts, or a conflicting static rule. |

`summary.json` also records `epgshare_catalog_channels`,
`epgshare_text_catalog_channels`, `epgshare_catalog_drift_channels`, and
`epgshare_catalog_drift_sha256`. The last value is a reproducible fingerprint
of the directional difference, not a list of the IDs.

## Streaming build command

The production workflow first creates private exact icon bindings:

```bash
python scripts/generate_missing_icon_overrides.py \
  --mapping-csv .build/channel-sync/effective_mapping.csv \
  --source-xmltv "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz" \
  --base-config config/channel_icons.csv \
  --output-config .build/channel-sync/channel_icons.csv \
  --asset-catalog assets/logos/icon_catalog.csv
```

The generated file stays under ignored `.build/` storage and is never uploaded.
The workflow then runs the equivalent of:

```bash
python -u scripts/build_epg_streaming.py \
  --mapping-file .build/channel-sync/effective_mapping.csv \
  --mapping-authoritative-file .build/channel-sync/authoritative_mapping.csv \
  --mapping-snapshot-manifest .build/channel-sync/mapping_snapshot_manifest.json \
  --all-source-file "$RUNNER_TEMP/skytv-epg-v1/epg_ripper_ALL_SOURCES1.xml.gz" \
  --epgshare-spool-file "$RUNNER_TEMP/skytv-epg-v1/selected_epg.sqlite3" \
  --public-dir public \
  --work-dir .build/work \
  --servers server_1 server_2 server_3 \
  --server-1-policy epgshare-only \
  --minimum-server-rows server_1=4000 \
  --minimum-server-rows server_2=10500 \
  --minimum-server-rows server_3=9400 \
  --icon-config .build/channel-sync/channel_icons.csv \
  --public-base-url "$EPG_PUBLIC_BASE_URL" \
  --minimum-coverage "${EPG_MINIMUM_COVERAGE:-80}"
```

The row floors remain 4,000 / 10,500 / 9,400. They are evaluated against the
manifest-verified authoritative runnable counts, never total Sheet rows and
never the smaller post-quarantine effective counts. The effective CSV remains
the sole mapping input used to generate public files.

The workflow downloads the combined guide and its small official sectioned ID
catalog once. The synchronizer requires at least 25,000 IDs in each input and
in their exact, case-sensitive intersection. It accepts at most 64 differing
IDs and at most 0.25% of the union, uses only the intersection for automatic
approval, and requires every one-file-only ID to have one deterministic market
route. It then parses the guide once and creates the temporary spool used by
the builder.
The companion catalog prevents an incomplete source or an unknown dummy section
from becoming an automatic real-channel match. Its catalog-wide preflight also
rejects any ID classified as both real and dummy, and any real ID with
contradictory or multiple-country market evidence. Normalization-confusable IDs
remain non-approvable runtime competitors so omitting them cannot manufacture
false uniqueness. A single conservative blocker can represent a confusable
family only when all members share one catalog kind and one identical
`(feed, region)` route; a mixed real/dummy or differing feed/market family fails
preflight. A real `ALL`-route candidate may still serve an exact existing
mapping, while a strong identity collision keeps a routed new proposal in
`REVIEW`. Local fixtures may use repeated
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
guides are downloaded when an eligible mapping requests them or when an
explicit REVIEW recheck has current native-ID candidates to validate.

## GitHub configuration

### Repository variables

| Name | Required | Purpose |
|---|---:|---|
| `GOOGLE_SHEET_ID` | Yes | ID between `/d/` and `/edit` in the private Sheet URL. |
| `GOOGLE_SHEET_TAB` | Yes | Must be `Mappings` for the supplied workbook. |
| `EPG_PUBLIC_BASE_URL` | Recommended | `https://sky2135.github.io/skytv-epg` or an approved custom domain. |
| `EPG_MINIMUM_COVERAGE` | No | Percentage gate; workflow default is `80`. |
| `ALLOW_INSECURE_PANEL_HTTP` | No | Set to `true` only if a provider cannot serve HTTPS. |
| `EPG_REVIEW_APPLY_LIMIT` | No | Scheduled total existing-`REVIEW` apply cap. Unset/`0` means no such writes. |
| `EPG_USE_GEMINI_AI` | No | `true` (case-insensitive) opts scheduled Workflow 1 into Gemini; unset or any other value means off. A nonzero `EPG_REVIEW_APPLY_LIMIT` is also required before an AI call. |

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

- `.github/workflows/channel_inventory_sync.yml` runs daily at 02:17 in
  `America/Toronto`, before publication, and can also be dispatched manually.
  Scheduled settings append missing rows and recheck all servers in `apply`,
  but the existing-`REVIEW` cap defaults to `0` and Gemini defaults off. Manual
  dispatch defaults to no new-row write, `dry-run`, a zero REVIEW cap, and
  Gemini off. Gemini requires both the `GEMINI_API_KEY` secret and an explicit
  manual opt-in or `EPG_USE_GEMINI_AI=true`.
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
- Automatic matching requires at least 25,000 unique IDs in the XML catalog,
  the sectioned companion text catalog, and their exact, case-sensitive shared
  set. XML-only plus text-only drift is accepted only when it is no more than
  64 IDs and no more than 0.25% of the union; all drift IDs are excluded from
  automatic approval. Larger drift and non-ASCII, whitespace, or control
  characters in a drifting ID stop the run. So does an XML-only or text-only ID
  whose market route is `ALL`/unknown or otherwise unresolved.
- Text-catalog real/dummy ambiguity and contradictory or multiple-country
  evidence are catalog-wide preflight failures. Case- or Unicode-whitespace
  normalization-confusable IDs remain runtime ambiguity blockers but cannot be
  approved automatically. A blocker may represent one engine-safe confusable
  family only when all members share the same catalog kind and exact
  `(feed, region)` route; a family mixing real/dummy status or differing by
  feed/market route stops the entire preflight.
- A shared real `ALL`-route candidate remains available for exact existing
  mappings. If it shares a strong exact/station identity with a routed proposed
  target, that new proposal remains disabled in `REVIEW`.
- XMLTV parse: at most 20 million elements and 10,000 child elements in one
  record. EPGShare and generated outputs remain DTD-free. A native Server 2/3
  panel may declare only an inert `<!DOCTYPE tv>` or
  `<!DOCTYPE tv SYSTEM "xmltv.dtd">`; internal subsets, entities, PUBLIC or
  network/local-file identifiers, and non-XMLTV declarations are rejected.
  Neither parser loads an external DTD or entity.
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
  guide are activated automatically. Verified placeholder families may receive
  an approved dummy guide and decorative headings may be disabled as `IGNORE`;
  ambiguous, adult-real, unsafe numbered, or weak-guide identities remain review.
- Existing `REVIEW` rows are retried by the daily Workflow 1 recheck. One run
  may persist only the explicit total cap across all lanes; the scheduled cap
  defaults to zero. Opt-in AI may consider at most 200 affected rows and only
  remaining total-cap capacity; any safe remainder stays deferred. AI can activate only the exact target independently selected by
  both local rankers and must pass every current catalog, programme, provider,
  Sheet, and alert gate.
- Provider channel totals, published-output totals, and EPG-covered totals do
  not define the review backlog. Use `summary.json` for the exact eligible,
  checked, matched, unresolved, skipped, and deferred counters for the selected
  scope.
- A row-level ambiguous result goes to `REVIEW`; a text catalog that is itself
  contradictory about real/dummy status or country market fails the full
  preflight before rows are written.
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
