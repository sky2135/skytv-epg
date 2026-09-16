# SKY TV EPG — Version 1

This is the production setup for the SKY TV EPG repository.

Start with [docs/START_HERE_VERSION_1.md](docs/START_HERE_VERSION_1.md). It is
written as a beginning-to-end checklist and assumes no GitHub or Google Cloud
experience.

If an existing run failed while writing `Sync Alerts`, use
[docs/REPAIR_CURRENT_SETUP_VERSION_1.md](docs/REPAIR_CURRENT_SETUP_VERSION_1.md)
to repair it without replacing the Sheet or creating another repository.

## Version 1 setup

- Keep this existing repository and its `main` branch.
- Keep the existing `live-data` branch and live-sports workflow unchanged.
- Do not create a `gh-pages` branch.
- Import the supplied Version 1 workbook into Google Sheets.
- Keep the Google Sheet private; the workflows connect through a dedicated
  Google service account.
- Do not commit a generated mapping CSV. A temporary CSV snapshot exists only
  inside each GitHub Actions run.

The daily workflow asks all three configured providers for their live-channel
inventories. Every valid unique stream returned by the API or playlist fallback
is compared by `(server_id, stream_id)`. For an unseen pair, Version 1 tries the
frozen Smart Rules matcher against the complete EPGShare catalog. It enables a
row only when there is one exact, region-consistent real channel ID confirmed
case-sensitively in both the XML and official text catalog, and that same
downloaded guide contains a useful current/future schedule. All uncertain rows
are appended disabled for review. An optional manual Workflow 1 recheck can
retry existing disabled `REVIEW` rows with the same safety rules; ordinary
runs do not change them. Existing Sheet rows are never deleted or silently
remapped. The same single EPGShare parse is reused to build the
TiviMate XMLTV and app JSON outputs, which are validated and deployed through
GitHub Pages.

Server 1 programme data is always sourced from EPGShare. Server 1 credentials
are used only by the inventory step to discover its channel list; they are not
available to the EPG-building step.

## Workflows

- `1 - Sync channels to Google Sheet` — manual first-run and on-demand channel
  inventory check. It can also recheck the existing `REVIEW` backlog with
  Smart Rules and, optionally, Gemini suggestions. Its safe defaults make no
  Sheet changes.
- `2 - Build and publish EPG` — daily and manual inventory, build, validation,
  and GitHub Pages deployment.

Both workflows use the same non-cancelling concurrency group, so they cannot
write to the Sheet at the same time. Neither workflow commits generated output
or mapping data to a Git branch.

## Google Sheet

The supplied workbook and CSV seed both begin with the same 25,170 historical
mapping rows. They include 11,939 approved dummy-guide placeholders and 171
disabled rows that remain in `REVIEW`. Those 171 rows stay private and are
excluded from schedules, public metadata, and personalization until reviewed,
approved, and enabled. The files are migration starters, not proof of the
providers' complete current lineups. The CSV seed is a frozen backup and never
updates. The imported private Google Sheet becomes the live mapping authority.
The first successful strict inventory sync asks each server for its live list
and appends every missing valid, uniquely identified row returned in that run.
Safe exact EPGShare matches with a verified programme guide are enabled
automatically; the rest use `enabled=FALSE` and `action=REVIEW`. Later runs
repeat that exact-key comparison automatically.

Workflow 1 can also recheck those existing `REVIEW` rows. Smart Rules always
run first. A match is enabled only after the same exact-ID, region, catalog,
and programme checks used for a new channel all pass. One apply run changes at
most 1,000 such verified rows; rerun the workflow to continue a larger backlog.
For Server 2 and Server 3, the same run may also recover an exact native ID
from current API/M3U evidence, but only after whole-catalog name uniqueness,
current programme, and immediate pre-write provider checks pass. Server 1
remains EPGShare-only.
Optional Gemini review is limited to 50 unresolved rows per run. Gemini can
never approve or enable a row. A `HIGH` suggestion stores only an exact,
locally verified EPGShare candidate. An abstention or lower-confidence answer
stores only an `ai-review-v1` manual-review marker and leaves the existing
source and ID unchanged. API errors make no row change, so they can be retried.
Every AI-processed row remains `enabled=FALSE` and `action=REVIEW` until a
person verifies it. Server 1 suggestions and approvals are EPGShare-only;
native Server 1 EPG is never used.

Smart Rules can also reuse strong human decisions during that one run. The
same cleaned channel alias and exact current EPGShare ID must already be
enabled and human-approved on at least two different servers. Conflicts,
alerts, dummy IDs, ambiguous IDs, automatic matches, and AI review rows cannot
teach the rule. The candidate must still pass the normal market, catalog, and
programme checks. This run-local evidence is rebuilt from the Sheet each time;
Gemini never writes a permanent rule or a separate knowledge file.
The run summary records how many evidence rows and alias groups were examined,
registered, or rejected.

The workflow summary reports the actual eligible, verified, unresolved,
skipped, and deferred counts for the selected scope. Gemini `HIGH` findings and
suggestions actually saved in the Sheet are separate counters, so a dry-run is
never presented as a write. Do not infer the review backlog by subtracting one
output count from the provider's total channel count, because disabled,
missing, ignored, quarantined, and already mapped rows are different states.

New rows contain conservative metadata suggestions for sorting and review.
Automatic schedule approval does **not** approve personalization metadata:
`metadata_status=review` remains until a person checks language, region, genre,
sport, and religion. A fuzzy name match is never approved unattended.
Possible stream-ID reuse is recorded in the private `Sync Alerts` tab and stays
quarantined from builds while the alert status is `OPEN`.

Workflow 2 keeps two private same-run mapping snapshots: the final authoritative
Sheet read and the effective snapshot after applying those quarantines. A
hash-bound manifest proves that both contain the same channel identities and
that the only allowed differences are the exact quarantine fields. Fixed
row-count floors apply to the authoritative pre-quarantine runnable rows, while
all `OPEN` identities remain disabled and absent from XML, JSON, metadata, and
personalization output. This prevents a deliberate quarantine from being
mistaken for lost Sheet data without weakening either safety check.

Schedule mapping and personalization review are separate jobs. All 25,170
starter rows contain automatically inferred, unlocked metadata rather than
human-approved metadata. The starter has 22,720 undetermined primary languages,
11,447 unknown regions, and 8,030 unknown genres. Unknown values deliberately
do not match a specific language, region, genre, sport, or religion preference.
They can be reviewed gradually in the private Sheet without blocking ordinary
guide generation.

Version 1 publishes the metadata and taxonomy that a custom app needs for
personalized groups. It does not add preference screens or filtering code to an
app whose source code is not in this repository.

## Published files

For this repository, the main endpoints are:

```text
https://sky2135.github.io/skytv-epg/health.json
https://sky2135.github.io/skytv-epg/epg/server_1_tivimate.xml.gz
https://sky2135.github.io/skytv-epg/epg/server_2_tivimate.xml.gz
https://sky2135.github.io/skytv-epg/epg/server_3_tivimate.xml.gz
https://sky2135.github.io/skytv-epg/EPG/server_1_epg.json.gz
https://sky2135.github.io/skytv-epg/EPG/server_1_metadata.json.gz
```

Equivalent Server 2 and Server 3 app files, indexes, taxonomy, and validation
reports are generated automatically.

## Implementation safety

- The roughly 2 GB expanded EPGShare document is parsed once with
  `lxml.etree.iterparse`; the selected SQLite spool is verified and reused by
  the output builder instead of parsing the XML a second time.
- Its channel-ID set is checked against EPGShare's small sectioned companion
  catalog before automatic matching, including real/dummy provenance. Because
  EPGShare can replace those two public files hours apart during a non-atomic
  rollover, Version 1 permits only a tightly bounded rollover difference: the
  XML set, text set, and exact shared set must each contain at least 25,000 IDs;
  the total difference must be no more than 64 IDs and no more than 0.25% of
  their union.
  Only exact, case-sensitive shared IDs can be approved automatically. IDs seen
  in only one file are quarantined from automatic approval; a larger or unsafe
  non-ASCII difference stops the run. Every one-file-only ID must also resolve
  to one deterministic country/market; an `ALL`/unknown or otherwise unresolved
  route stops the run because that shadow could not reliably block false
  uniqueness. The entire unattended-matching preflight also stops if the
  official text catalog assigns one ID to both real and dummy sections,
  contradicts an ID's country with a section, or assigns it to multiple country
  markets. Those contradictions are not merely ignored, because omission could
  make a similar channel look falsely unique.
  Case/Unicode-whitespace normalization-confusable identities remain
  non-approvable matcher competitors: they can block a false unique match but
  can never receive automatic approval themselves. One conservative blocker
  may represent an engine-safe confusable family only when every member has the
  same catalog kind (all real or all dummy) and the identical exact route—the
  same feed and market. If the family mixes real/dummy status or differs by
  feed or market, the entire preflight stops. Separately, an unscoped real
  candidate routed as `ALL` remains available for an exact existing mapping,
  but a strong station-identity collision with a routed new proposal keeps that
  proposal disabled in `REVIEW`.
- Selected schedules are staged in disk-backed SQLite.
- XML and JSON outputs are written as deterministic gzip streams.
- Untrusted downloads, XML structure, compressed/expanded sizes, Sheet rows,
  duplicate identities, and output sizes are bounded and validated.
- Generated Pages output is published atomically only after all checks pass.
- The Pages payload is kept below a 900 MiB safety ceiling for GitHub Pages'
  1 GB published-site limit.
- Row-level uncertain mappings and severe stream-identity changes fail safe to
  review; catalog-wide provenance contradictions stop the run before a Sheet
  write.
- The private mapping snapshot and every generated upload pass
  credential-safety checks before the builder or Pages upload can continue;
  gzip outputs are checked after streaming decompression.
- The authoritative/effective private snapshot pair is SHA-256-bound and
  compared row by row before the fixed 4,000 / 10,500 / 9,400 integrity floors
  are evaluated. Missing rows, reordered identities, stale manifests, or any
  non-quarantine field change stop before the EPG source is processed.

## Developer validation

```bash
python -m pip install --only-binary=:all: -r requirements.txt -r requirements-sync.txt
python -m unittest discover -s tests -v
```

See [docs/TECHNICAL_REFERENCE_VERSION_1.md](docs/TECHNICAL_REFERENCE_VERSION_1.md)
for the schemas, command-line contracts, failure behavior, and security model.
Files whose names contain v7 or v8 are frozen historical matcher components;
their internal names are intentionally retained for compatibility and are not
the current product version.
