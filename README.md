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
row only when there is one exact, region-consistent real channel ID and that
same downloaded guide contains a useful current/future schedule. All uncertain
rows are appended disabled for review. Existing Sheet rows are never deleted
or silently remapped. The same single EPGShare parse is reused to build the
TiviMate XMLTV and app JSON outputs, which are validated and deployed through
GitHub Pages.

Server 1 programme data is always sourced from EPGShare. Server 1 credentials
are used only by the inventory step to discover its channel list; they are not
available to the EPG-building step.

## Workflows

- `1 - Sync channels to Google Sheet` — manual first-run and on-demand channel
  inventory check; its safe default is report-only.
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

New rows contain conservative metadata suggestions for sorting and review.
Automatic schedule approval does **not** approve personalization metadata:
`metadata_status=review` remains until a person checks language, region, genre,
sport, and religion. A fuzzy name match is never approved unattended.
Possible stream-ID reuse is recorded in the private `Sync Alerts` tab and stays
quarantined from builds while the alert status is `OPEN`.

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
- Its exact channel-ID set is checked against EPGShare's small sectioned
  companion catalog before automatic matching, including real/dummy provenance.
- Selected schedules are staged in disk-backed SQLite.
- XML and JSON outputs are written as deterministic gzip streams.
- Untrusted downloads, XML structure, compressed/expanded sizes, Sheet rows,
  duplicate identities, and output sizes are bounded and validated.
- Generated Pages output is published atomically only after all checks pass.
- The Pages payload is kept below a 900 MiB safety ceiling for GitHub Pages'
  1 GB published-site limit.
- Uncertain mappings and severe stream-identity changes fail safe to review.
- The private mapping snapshot and every generated upload pass
  credential-safety checks before the builder or Pages upload can continue;
  gzip outputs are checked after streaming decompression.

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
