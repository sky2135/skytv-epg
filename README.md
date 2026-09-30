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
is compared by `(server_id, stream_id)`. It then decides each safe identity in
this order: deterministic EPGShare real schedule, verified Server 2/3 native
schedule, Google-Search-grounded Gemini verification of a supplied real
candidate, and finally a truthful per-stream local synthetic guide. Decorative
non-channels become `IGNORE`; `OPEN` alerts, identity drift, and untracked manual
targets remain quarantined or protected. This is an unattended backlog process,
not a request for someone to match channels row by row. Existing Sheet rows are
never deleted or silently remapped. The same single EPGShare parse is reused to build the
TiviMate XMLTV and app JSON outputs, which are validated and deployed through
GitHub Pages.

Server 1 external real programme data is sourced only from EPGShare; unresolved
safe rows may use a local synthetic guide. Server 1 credentials are used only
by the inventory step to discover its channel list and are not available to the
EPG-building step.

If a Server 2/3 native panel is temporarily unavailable after bounded retries,
the production builder leaves the private Sheet mapping unchanged and uses a
disclosed per-stream local synthetic guide for that build. Manifests count this
separately as `nativePanelUnavailableSyntheticStreams`. Authentication, TLS,
redirect, payload-validation, and local I/O failures remain hard failures.

## Workflows

- `1 - Sync channels to Google Sheet` — runs daily at **02:17 Toronto time** and
  can also be dispatched manually. The scheduled run appends missing channels,
  rechecks existing `REVIEW` rows on all servers, and defaults the total apply
  and synthetic fallback limits to `30000`. Scheduled Gemini turns on when its
  API-key secret exists unless `EPG_USE_GEMINI_AI=false`. Manual dispatch stays
  conservative: no new-row write, `dry-run`, Gemini off, and zero write limits
  until explicitly changed.
- `2 - Build and publish EPG` — daily and manual inventory, build, validation,
  and GitHub Pages deployment.

Both workflows use the same non-cancelling concurrency group, so they cannot
write to the Sheet at the same time. Neither workflow commits generated output
or mapping data to a Git branch.

## Matching Lab (proposal-only)

The optional external Matching Lab evaluates the complete disabled `REVIEW`
backlog without changing either production workflow. It combines bounded
multi-signal retrieval, symmetric protected-semantics checks, current programme
validation, optional advisory-only AI, deterministic private proposal bundles,
and an optional local observation ledger with chain-consistency checks. It has
no Mapping-write path; even its
strongest shadow state carries no apply authority. Start with
[the design decision](docs/MATCHING_LAB_DESIGN.md) and
[the operating guide](docs/MATCHING_LAB_OPERATIONS.md). The production
[smart guide coverage policy](docs/SMART_GUIDE_COVERAGE_DESIGN.md) describes
how calibrated real matches, channel-specific synthetic guides, event-slot
handling, and customer feedback can provide near-complete useful coverage
without presenting uncertain matches as real schedules. The concise
[autonomous coverage contract](docs/AUTONOMOUS_COVERAGE.md) defines production
decision order, controls, and coverage semantics. The implemented
[Smart Coverage Fallback](docs/SMART_COVERAGE_FALLBACK.md) explains rollback
evidence for local synthetic guides. A standalone private
approval package can bind an owner's decision to one exact unexpired run, and a
prepare-only command can stage a deterministic canary of at most 25 rows. Both
artifacts remain non-authoritative and preserve existing automated Mapping notes
when reviewer notes are blank. A separate manual `apply` subcommand can write
at most 25 exact approvals only after full approval-ID confirmation and fresh
catalog, programme, provider, Mapping, and OPEN-alert checks; it performs an
atomic batch update and authoritative postread. It must run from a secure runner
holding the same shared lock as the unchanged production workflows. No live
write was performed as part of implementing or testing this lane.

## Google Sheet

The supplied workbook and CSV seed both begin with the same 25,170 historical
mapping rows. They include 11,939 approved dummy-guide placeholders and 171
disabled rows that begin in `REVIEW`. Production automation re-evaluates safe
rows and gives the unresolved remainder a truthful local guide; protected or
quarantined rows remain excluded. The files are migration starters, not proof of the
providers' complete current lineups. The CSV seed is a frozen backup and never
updates. The imported private Google Sheet becomes the live mapping authority.
The first successful strict inventory sync asks each server for its live list
and appends every missing valid, uniquely identified row returned in that run.
Safe exact EPGShare matches with a verified programme guide are enabled
automatically. Verified Server 2/3 native schedules may become `KEEP_PANEL`.
Safe rows not proven real become `AUTO_DUMMY` with a truthful, unique local
guide, and decorative headings become disabled `IGNORE`. Later runs retry local
coverage fallbacks as real-schedule upgrade candidates.

Workflow 1 also rechecks existing `REVIEW` rows and earlier
`coverage-fallback-v1` synthetics. Smart Rules always run first. A real match is
enabled only after the exact-ID, region, catalog, and programme checks used for
a new channel all pass. One `review_apply_limit` of `0`, `25`, `100`, `500`,
`2500`, `5000`, or `30000` caps the total persisted across deterministic,
native, synthetic, heading, and AI lanes. Scheduled runs default to `30000`;
manual dispatch defaults to `0`.
For Server 2 and Server 3, the same run may also recover an exact native ID
from current API/M3U evidence, but only after whole-catalog name uniqueness,
current programme, and immediate pre-write provider checks pass. Server 1
has no native-real lane.
Grounded Gemini verification is limited to the smaller of 200 affected rows or
the capacity remaining under the total apply limit. It is scheduled
automatically when `GEMINI_API_KEY` exists unless explicitly disabled with
`EPG_USE_GEMINI_AI=false`. Gemini cannot invent an ID: it sees only opaque
choices from a local shortlist. A row is enabled as real only when both local
rankers agree, Gemini returns `HIGH` for that exact choice, one complete
positive identity claim is supported by at least two independent web
authorities, and every catalog, programme, provider, Sheet, alert, score,
margin, and semantics gate passes. An abstention or AI failure does not block
the run; the safe unresolved row receives a local synthetic guide. Server 1
remains EPGShare-only for external real schedules.

Smart Rules rebuild durable alias memory from the private Sheet every run. One
enabled current human `MANUAL`/`APPROVED` row may teach its exact alias, market,
and EPGShare target. Automatic or `ai-verified-v2` evidence is weaker and must
agree on the identical target across at least two unchanged servers before it
can teach. Conflicts, alerts, dummy IDs, ambiguous IDs, and provider drift are
excluded. Gemini never edits matcher code or a public knowledge file; its
strictly verified Sheet decisions can become reusable evidence only on a later
run after the cross-server threshold is met.
The detailed `summary.json` records how many evidence rows and alias groups were
examined, registered, or rejected.

The displayed workflow summary is intentionally compact: one per-server channel
status table plus the most important results from that run. The downloadable
`summary.json` artifact keeps the detailed eligible, verified, unresolved,
skipped, deferred, native, AI, learning, and catalog counters. Gemini `HIGH`
responses and strictly verified rows actually enabled in the Sheet are separate
counters, so a dry-run is never presented as a write. Do not infer the review
backlog by subtracting one output count from the provider's total channel count,
because disabled, missing, ignored, quarantined, and already mapped rows are
different states. Published `guideCoverage` data separately reports literal,
channel-only, and actionable denominators plus verified EPGShare real, native
real, local synthetic, ignored, quarantined, and uncovered outcomes. Useful
coverage must never be presented as real-match accuracy.

The final September 30, 2026 offline decision base contains all 49,759 Mapping
rows. It assigns 41,801 targets (84.00%): 10,526 EPGShare real, 3,977 retained
native, and 27,298 truthful local synthetic. It quarantines 5,389 native
candidates for the credentialed live XMLTV verifier, 2,120 OPEN-alert rows, and
449 protected manual candidates. Resolving at least 2,983 of the native cohort
as either verified native or a definitive-miss synthetic crosses 90%; resolving
the complete native cohort reaches 47,190 assigned rows (94.84%). These are
target-assignment figures, not measured programme coverage or real-match
accuracy; the production `guideCoverage` object is authoritative after build.

New rows contain conservative metadata suggestions for sorting and review.
Automatic schedule approval does **not** approve personalization metadata:
`metadata_status=review` remains until a person checks language, region, genre,
sport, and religion. A fuzzy-only matcher result is never approved; AI can act
only as a second verifier for a supplied candidate already supported by two
independent strong local rankings and all current safety gates.
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
  but a strong station-identity collision prevents real approval and sends an
  otherwise safe proposal to local synthetic coverage.
- Selected schedules are staged in disk-backed SQLite.
- XML and JSON outputs are written as deterministic gzip streams.
- Untrusted downloads, XML structure, compressed/expanded sizes, Sheet rows,
  duplicate identities, and output sizes are bounded and validated.
- Generated Pages output is published atomically only after all checks pass.
- The Pages payload is kept below a 900 MiB safety ceiling for GitHub Pages'
  1 GB published-site limit.
- Safe row-level uncertainty fails to a disclosed local synthetic guide. Severe
  stream-identity changes remain quarantined; catalog-wide provenance
  contradictions stop the run before a Sheet write.
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
