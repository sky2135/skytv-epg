# SKY TV EPG Version 1 — validation report

**Local release status: PASS — affected-files release sealed**  
Validated through: **2026-09-18 15:59 UTC**

The catalog-rollover, Workflow 2 snapshot-guard, native-panel XMLTV, and
Server 2/3 REVIEW-backlog repairs previously passed focused adversarial
validation. The current release also adds the scheduled all-server REVIEW
recheck, durable Smart Rule memory, strict Gemini verification, and deterministic
`AUTO_DUMMY`/`IGNORE` outcomes. The sealed tree passed all **587** repository
tests, Python compilation, workflow parsing, integrity-hash verification, the
private-workbook shadow acceptance run, and an independent final audit.

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
independently verified batches of at most 500 rows. One explicit cap of `0`,
`25`, `100`, `500`, `2500`, or
`5000` covers the total EPGShare, native, synthetic, decorative-heading, and
strict-AI existing-`REVIEW` writes; its default is `0`. AI receives only the
capacity remaining under that total and never splits a compatible cluster.
Dry-run performs every validation but changes no Sheet row.

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

The final sealed tree passed **587 of 587** repository tests. The relevant
Workflow 1 Python modules compile, both workflow YAML files parse, and every
manifest-bound production file matches its recorded SHA-256. Adversarial tests
also prove that raw, recursively percent-encoded, base64-reflected, HTML-entity,
and mixed-encoding credential material is rejected before Sheet persistence or
Gemini transport.

The attached private workbook was processed read-only as a production-shaped
acceptance corpus. The original workbook SHA-256 remained
`e1b4d8d361128edaaed196fa0ed6a7cb3a129b62c3070d255d0925ccb20159a6`.
Aggregate results were:

| Workbook shadow measurement | Result |
|---|---:|
| Exact-name holdout recovered / wrong | 199 / 0 |
| Quality-stripped holdout recovered / wrong | 1,202 / 0 |
| Ambiguous-name cohort recovered / wrong | 3 / 0 |
| Cross-market cohort recovered / wrong | 382 / 0 |
| Verified placeholder/heading decisions | 944 |
| Strict EPGShare candidates before live programme gate | 14 |
| Total safe shadow actions | 958 |
| Repeated decisions after simulated apply | 0 |

The workbook does not contain a complete current EPGShare programme snapshot
or current native XMLTV files. Therefore, the 14 real-guide candidates remain
pre-gate opportunities rather than promised production writes; the live run
must still pass exact XML/TXT catalog, programme, provider, Sheet, and alert
checks. The acceptance run changed no workbook or Google Sheet row.

The final audit must cover the production Server 3 quarantine census,
authoritative truncation, dropped or reordered identities, unauthorized EPG or
name edits, stale hashes, duplicate and orphan alerts, pre-disabled rows,
reserved-marker spoofing, per-server accounting, last-good publication
preservation, strict AI agreement, encoded credential rejection, current
provider terminal rereads, deterministic caps, scheduled defaults, and
idempotent workbook processing.

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

| Sealed production file | SHA-256 |
|---|---|
| `src/skytv_epg_auto_match_v1.py` | `db49198de05cb6c618ff11a0408081eeba581eb9b9a05f231a0d04a3ce70d7e8` |
| `scripts/auto_match_inventory.py` | `5e88f400c66fd7bcbd8876810660fcd123e78c925f9022faaf7070f7eb81c7a0` |
| `scripts/ai_review_gemini.py` | `b2cb275375f77648dad8bf9572eb299b42876a80d53fa405a1fd862ed244c1ca` |
| `scripts/ai_review_policy.py` | `c398297cbf4eabef74c8a02af2a567d78724505b26779904ecdf96fe58442000` |
| `scripts/sync_channel_inventory.py` | `e91476606f3770063fca7c060d1cbd716040edfa1b5ceccbf034bfbdc48d6253` |
| `.github/workflows/channel_inventory_sync.yml` | `239ca26c1fbd5c62668dd4609db786ee12da09d2dc9eebf34f5766ff3c74f72f` |

## External validation still required

This local repair did not use the owner's provider credentials, Google service
account, private Sheet, or GitHub Pages environment. Keep the current Sheet,
repository, `main` branch, and Pages setup. Install the affected replacement
files, confirm the existing `GEMINI_API_KEY` repository secret only if AI will
be used, then run Workflow 1 first with its safe defaults: no new-row write,
REVIEW `dry-run`, Gemini off, and total REVIEW apply cap `0`. After reviewing
that result, use `apply` with a 25-row canary. Scheduled REVIEW writes remain
off until `EPG_REVIEW_APPLY_LIMIT` is set to a nonzero allowed value, and
scheduled Gemini remains off until `EPG_USE_GEMINI_AI=true`. The displayed
summary stays compact; use its downloaded
`summary.json` for detailed eligible, skipped, deferred, native, AI, learning,
and catalog counters. Do not bulk-mark open alerts `RESOLVED` and do not enable
unresolved rows manually.
