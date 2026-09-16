# SKY TV EPG Version 1 — validation report

**Local release status: PASS**  
Validated through: 2026-09-16 UTC

The catalog-rollover, Workflow 2 snapshot-guard, native-panel XMLTV, and
Server 2/3 REVIEW-backlog repairs have passed focused adversarial validation.
The integrity manifest has been resealed for the repaired files, and the
complete repository suite now passes.

## Server 2/3 native REVIEW recovery

The read-only backlog report found 26,288 current REVIEW rows, but its original
native selector required a native ID to have already been saved in the Sheet.
That made a fresh provider/M3U ID invisible and produced zero native candidates.
The corrected Workflow 1 path re-derives native evidence from the current run
instead of trusting the aggregate report or a stale Sheet value.

Automatic `KEEP_PANEL` approval now requires all of the following:

- Server 2 or Server 3; Server 1 is rejected before native file access;
- the exact current stream is present, unchanged, disabled, in `REVIEW`, and
  absent from the complete `OPEN` alert quarantine;
- an untouched automatic-discovery row with a blank ID, or the same already
  stored native ID and native source controls;
- a fresh API ID, or an M3U `tvg-id` recovered by exact numeric stream-ID and
  exact normalized-name join;
- no API/M3U disagreement;
- an exact case-unique ID in a complete current native XMLTV catalog;
- one unambiguous compatible XMLTV display name; and
- the two-programme/six-hour current schedule gate.

The Sheet writer requires the exact verified native update in a separate
in-memory allowlist. Forged `KEEP_PANEL` rows, Server 1 native rows, unrelated
cell changes, concurrent Sheet edits, or alert changes fail before or after the
single atomic request. EPGShare and native approvals share the existing maximum
of 1,000 deterministic updates per apply run. Dry-run performs every validation
but changes no Sheet row.

Adversarial coverage includes conflicting API/M3U IDs, trimmed IDs, duplicate
M3U attributes, conflicting ID aliases, M3U name and stream-ID mismatches,
manual or partial Sheet edits, ambiguous display names, case-only
XMLTV ID collisions, placeholder schedules, unsafe DTD/entity declarations,
symlink and file-swap attacks, native source outage, writer-allowlist forgery,
alert races, and the non-negotiable Server 1 boundary.

The owner must still run the private Google Sheet/provider checks in
[`docs/REPAIR_CURRENT_SETUP_VERSION_1.md`](docs/REPAIR_CURRENT_SETUP_VERSION_1.md).
Those are the only checks requiring the owner's private accounts.

## Workflow 2 quarantine-aware snapshot guard

The reported Workflow 2 failure was:

```text
ERROR: Private mapping snapshot failed the runnable-row truncation guard:
server_3=8,763 (minimum 9,400)
```

This was a guard-layer conflict, not Sheet truncation and not resource
exhaustion. Server 3 had 9,943 authoritative runnable rows. The private
`Sync Alerts` state correctly excluded 1,180 of those rows, leaving 8,763
output-eligible rows. The old code incorrectly applied the 9,400
snapshot-integrity floor after that deliberate quarantine.

The corrected Version 1 design keeps both protections:

- fixed floors remain **4,000 / 10,500 / 9,400** and are applied to the final
  authoritative pre-quarantine runnable snapshot;
- the effective snapshot remains the only mapping used for publication;
- every `OPEN` identity stays disabled and absent from XML, JSON, metadata, and
  personalization;
- authoritative and effective snapshots must have the same exact ordered
  `(server_id, stream_id)` identities;
- the only permitted row delta is the exact quarantine transformation;
- already-disabled or `REVIEW` rows provide no floor credit;
- duplicate alert keys count once and orphan alert identities fail closed; and
- a private manifest SHA-256-binds both snapshots and the deduplicated
  quarantine census before the EPG source is processed.

The production-shaped regression fixture proves the intended arithmetic:

| Server 3 measurement | Rows |
|---|---:|
| Authoritative runnable | 9,943 |
| Safely excluded by OPEN alerts | 1,180 |
| Effective/output-eligible | 8,763 |
| Fixed integrity minimum | 9,400 |

The attached resource report recorded about 380 MiB peak RSS, zero swap, and a
14.62-second controlled exit, confirming that RAM, disk, and timeout limits did
not cause this event.

## Native-panel XMLTV declaration repair

The next Workflow 2 run passed every snapshot-integrity floor and then stopped
with:

```text
WARNING: server_2 native panel transport is unencrypted because
ALLOW_INSECURE_PANEL_HTTP is TRUE.
ERROR: XML source contains a forbidden DTD or entity declaration.
```

The log order proves that this input was Server 2's downloaded native
`xmltv.php` document. EPGShare had already been parsed into, and successfully
reused from, the sealed one-pass spool. The old byte-prefix guard reported the
same generic error for both a standard XMLTV header such as
`<!DOCTYPE tv SYSTEM "xmltv.dtd">` and a status-200 HTML error page beginning
`<!DOCTYPE html>`, so the retained run log cannot distinguish which of those
two payloads Server 2 returned.

The corrected parser handles both cases safely:

- only native `panel:server_2` and `panel:server_3` inputs may contain an inert
  `<!DOCTYPE tv>` or exact `<!DOCTYPE tv SYSTEM "xmltv.dtd">` declaration;
- EPGShare and generated XMLTV remain DTD-free;
- a bounded, encoding-aware structural prolog check replaces the raw substring
  search, so comments and CDATA are not mistaken for declarations;
- internal subsets, general or parameter entities, PUBLIC declarations,
  network/local-file identifiers, empty identifiers, and non-`tv` declarations
  are rejected before programme ingestion;
- both XML parsers are configured never to load a DTD, external entity, local
  file, or network resource;
- HTML and JSON panel responses are classified as non-XML rather than as a
  misleading DTD error, and partial downloads are removed; and
- future parser failures name the safe source key, such as `panel:server_2`.

The end-to-end fixture now builds Server 2 from a panel XMLTV document carrying
the standard inert declaration. Separate adversarial tests cover plain and gzip
input, long prologs, UTF-16 entity attacks, internal/external/parameter
entities, HTML/JSON responses, DTD-like comment/CDATA text, exact panel-only
scope, and proof that a local `xmltv.dtd` file is never loaded.

## Catalog-rollover repair

EPGShare's current XML and official text catalog were published at different
times and temporarily describe slightly different ID sets. The installed
workflow correctly stopped with the older strict-exact-set rule, producing:

```text
ERROR: The XML and official text catalogs do not declare the same exact IDs.
```

The repaired Version 1 logic handles only a tightly bounded upstream rollover:

- both catalogs must independently contain at least 25,000 exact IDs;
- drift must be no more than **64 IDs** and no more than **0.25%** of the exact
  union;
- drift IDs must pass strict character and structure checks;
- only the exact, case-sensitive XML/TXT intersection may be approved
  automatically;
- XML-only and TXT-only IDs remain `REVIEW` and cannot receive automatic EPG
  approval;
- case-fold collisions across the union, country-section conflicts, and
  XML-only shadow candidates remain conservative blockers;
- normalization-confusable families must retain one catalog kind and one exact
  `(feed, region)` route, while mixed families fail before matching;
- an unscoped `ALL`-route candidate with the same allowlisted exact/station
  identity keeps the routed proposal disabled in `REVIEW`; and
- larger, malformed, or suspicious divergence still fails before any Google
  Sheet write.

This preserves the safety purpose of the old check while allowing a small,
verifiable non-atomic source rollout to complete without silently approving the
20 disputed IDs.

## Automated code and workflow checks

- Full repository suite: **404 tests passed, 0 failed**.
- Stage 2 native/analyzer/synchronizer suite: **168 tests passed, 0 failed**,
  including **125** synchronizer tests and **14** dedicated native XMLTV
  adversarial tests.
- Full 25,170-row Version 1 seed snapshot-bundle exercise: **passed**, including
  the exact Server 3 census of 9,943 authoritative, 1,180 quarantined, and 8,763
  effective runnable rows.
- New quarantine-guard regressions cover the production 9,943 → 8,763 case,
  genuine authoritative truncation, dropped/reordered identities, hidden name
  or EPG edits, stale hashes, duplicate and orphan alerts, pre-disabled rows,
  reserved-marker spoofing, per-server accounting, and last-good publication
  preservation.
- Frozen matcher regression: **222 matching and safety cases passed**.
- Independent adversarial review found no remaining approval-safety blocker
  after whole-catalog name-collision, raw-source parity, immediate provider
  revalidation, and user-facing summary corrections.
- Production-sized live XML/TXT one-pass validation after the final
  Unicode/case/route hardening: **passed**.
- Python compilation, installed-dependency consistency, integrity-manifest
  verification, and all three workflow `actionlint` checks passed.
- No private-key, GitHub-token, or Google-API-key signature was found in the
  repository overlay in the preceding release audit.

Regression coverage includes exact-alignment mode, both bounded-drift limits,
oversized and unsafe drift rejection, exact-intersection-only approval,
XML-only/TXT-only quarantine, Unicode-whitespace and case-confusable IDs,
union-wide case collisions, XML-only collision shadows, text-catalog country
section conflicts, real/dummy ambiguity, cross-market and cross-feed confusable
families, unscoped `ALL`-route competitors, one-character strict identities,
row-accurate approval counts, malformed/truncated gzip and XML, panel-only
inert XMLTV declarations, DTD/entity attack rejection, bounded streaming,
exact opaque channel IDs,
descriptor-bound source hashing, atomic path replacement, near-term guide
coverage, adult-label disguises, Google native-table appends, partial or
uncertain Google responses, exact 33-cell rereads, formula-safe text, stream-ID
reuse quarantine, provider outages, secret reflection, Server 1 EPGShare-only
enforcement, deterministic output, truthful pre-downloaded-source provenance,
and app schema compatibility.

## Production-sized current-source validation

The currently published EPGShare ALL source and official companion text catalog
were processed by the repaired code in one streaming pass:

| Measurement | Result |
|---|---:|
| Catalog alignment mode | `bounded-drift` |
| Compressed XML | 205,642,832 bytes |
| Expanded XML | 1,945,216,128 bytes |
| XML exact IDs | 27,114 |
| Text-catalog exact IDs | 27,128 |
| Exact shared IDs | 27,111 |
| XML-only IDs | 3 |
| Text-only IDs | 17 |
| Total symmetric drift | 20 |
| Top-level XML records | 2,767,623 |
| Programmes | 2,740,254 |
| Complete parse and sealed SQLite spool | passed |

Source SHA-256:

```text
XML    85b98e89474c3444d9b8ee7e309abdb6595fe57d1c2c23feb838a1a725974e29
TXT    5ecbc18ec04b67e05c3c9c2c15360f15f58ef04b753632221ab028a7bd9f53a9
DRIFT  3d8901fb8496f8f6415c5871d5f3dd64ec105bb93b53f4bcc08d3e946901d754
```

The XML was last modified at 2026-09-15 16:39:13 UTC and the text catalog at
2026-09-15 22:35:57 UTC. Their roughly six-hour publication gap, together with
the small 20-ID difference, identifies an upstream non-atomic catalog rollout
rather than a provider-login or Google Sheet problem. If the source pair is
unchanged when the owner runs the repaired workflow, the expected summary is
`bounded-drift`, 27,111 shared IDs, 3 XML-only IDs, and 17 TXT-only IDs.

## The reported failures are separate

### Latest Workflow 2 runnable-floor failure

The latest error happened after the private Sheet refresh and before EPG source
processing. It is the quarantine/floor collision documented at the start of
this report. It did not undo the 43 newly stored alerts, modify the Sheet, or
replace the last successful public output.

### Prior XML/text exit-code-2 failure

That earlier error occurred during public EPGShare catalog validation. It was not
caused by Server 3 credentials and happens before Google Sheet mapping writes.

- `SERVER_3_PASSWORD: ***` is GitHub Actions masking a configured secret; it
  does not indicate that the password is literally `***` or that login failed.
- `Warning: Provider credentials may be sent over unencrypted HTTP.` is a
  transport-security warning because the provider URL uses HTTP. It should be
  addressed by switching to the provider's HTTPS endpoint if one exists, but it
  did not cause that exact-ID mismatch.
- `Process completed with exit code 2` is the workflow's controlled stop after
  the old exact-catalog check rejected the 20-ID upstream rollover.

### Earlier attached `summary.json` failure

The attached report was generated at `2026-09-15T17:31:51Z` by the older
write-enabled workflow. It records 49,016 inventory rows, 24,588 new streams,
1,499 possible stream-ID reuse alerts, zero durably confirmed alert rows, and
zero appended mapping rows. The safety boundary correctly blocked both the
mapping append and build snapshot because Google did not authoritatively confirm
the `Sync Alerts` append.

That initial event was a Google Sheet durable-write verification failure. It is
not evidence for, and did not cause, the later public XML/TXT catalog mismatch
or the latest snapshot-floor collision.
The repaired Sheet writer uses a logical RAW row append with `INSERT_ROWS`,
checks exact committed ranges and cell counts, expands an imported native table
only after values exist, and authoritatively rereads the rows. A real write to
the owner's private Sheet remains part of the documented OFF-then-ON test.

## Mapping artifacts

- Seed CSV SHA-256:
  `ed9eb8ed230cd628d92ad900349ae60212641d97b2d7d86fffc1e2f640dbb5d0`
- Google Sheet workbook SHA-256:
  `c0a1d9fcba5c8c6badb37836c513a5f6d206eff705819156f848886c8c738b09`
- Mapping rows: **25,170**, unique by `(server_id, stream_id)`.
- Server rows: Server 1 **4,202**; Server 2 **11,025**; Server 3 **9,943**.
- Sources: `dummy` **11,939**; `epgshare01` **9,062**; `panel` **4,169**.
- Review state: **171** rows; all are disabled.
- Workbook: 4 expected sheets, no formulas, macros, error cells, or external
  links; every mapping cell matches the CSV seed after spreadsheet-literal
  escaping is removed.

## Critical Version 1 file integrity

| File | SHA-256 |
|---|---|
| `scripts/build_epg_streaming.py` | `3b88fbd4e41284607fdde2783d618864b3fb15b54ff2747ec4b5c3e557e56d97` |
| `src/skytv_epg_auto_match_v1.py` | `8f346761daa5da73fcd5abe79d582c22557cdd1b8496ec90ef3e47174f61deaa` |
| `scripts/auto_match_inventory.py` | `cbf40f15c0acd7f58d13449674774804fa566b1fba76c2abb73cc2e29b6d1922` |
| `scripts/epg_catalog_stream.py` | `fb04f5f9097bffe96ba4f3a421b4d53f1ce6110dc80e390ee45a91f2d99a9821` |
| `scripts/epg_selection_spool.py` | `c0ad4e485972b206515f2205e37cf056593cb1105035a6031ef95b5371cd1336` |
| `scripts/analyze_review_backlog.py` | `d5af5112922e4d1a7ce85dd34ceffa3cb732d5680e60fb7e5e6eb2f4550d3ab2` |
| `scripts/native_epg_review.py` | `89e6145f2b855b6cbab43b32582985b68af3365b7cbc9e5c0232ce23bb274fee` |
| `scripts/sync_channel_inventory.py` | `aa814787ac5186b31671248de41cb002822897c02a3e073e3d3fd963cc6663e1` |
| `.github/workflows/main.yml` | `23770174a7690423b9c835c789ebb6ca43c701bdf448b65a1970132e5c0e4a11` |
| `.github/workflows/channel_inventory_sync.yml` | `8dfde002cb5dda72b26376ea4b4fc19b71063e2140d379f4d3c104648be2a885` |
| `requirements-sync.txt` | `cb80ac4377fa3656ea135c65273fdc1b6ba7f5ce198f1f14ccb13de0e3ed593f` |
| `MATCHER_INTEGRITY.json` | `c8bc69922b47816e9950326c1e6c53d0d5e0133f732cf3bf9dc3c9bd9177bbca` |

## External validation still required

This local repair did not use the owner's provider credentials, Google service
account, private Sheet, or GitHub Pages environment. Keep the current Sheet,
repository, `main` branch, and Pages setup. Install the affected replacement
files, then run Workflow 1 first in `dry-run` mode with server scope `all` and
Gemini off. If the summary is healthy, rerun in `apply` mode. Each apply may
enable up to 1,000 exact verified EPGShare/native mappings; rerun only while the
summary reports verified matches deferred by the write limit. Do not bulk-mark
open alerts `RESOLVED` and do not enable unresolved rows manually.
