# SKY TV EPG Version 1 — validation report

**Local release status: PASS**  
Validated: 2026-09-15 UTC

The repaired code, current public EPGShare source, frozen matcher, one-pass
SQLite handoff, and workflow definitions have passed. The owner must still run
the private Google Sheet/provider checks in
[`docs/REPAIR_CURRENT_SETUP_VERSION_1.md`](docs/REPAIR_CURRENT_SETUP_VERSION_1.md).
Those are the only checks requiring the owner's private accounts.

## Automated code and workflow checks

- Full repository suite: **243 tests passed, 0 failed**.
- Exact manual-workflow suite: **164 tests passed, 0 failed**.
- Frozen matcher regression: **222 matching/safety cases passed**.
- Python compilation and installed-dependency consistency passed.
- Both Version 1 EPG workflows passed `actionlint`; their Bash blocks, embedded
  Python, command-line arguments, pinned action SHAs, and artifact boundaries
  were independently checked.
- Integrity schema 6 verifies the frozen matcher plus the automatic-match,
  catalog-stream, SQLite-spool, Google-sync, builder, workflow, and knowledge
  file hashes.
- No private-key, GitHub-token, or Google-API-key signature was found in the
  repository overlay.

Regression coverage includes malformed/truncated gzip and XML, DTD/entity
rejection, bounded streaming, exact opaque channel IDs, Unicode-whitespace and
case-confusable IDs, descriptor-bound source hashing, atomic path replacement,
near-term guide coverage, fullwidth/invisible/accented and cross-script
adult-label disguises, imported Google native-table appends, partial or uncertain
Google responses, exact 33-cell
rereads, formula-safe text, stream-ID reuse quarantine, provider outages,
secret reflection, Server 1 EPGShare-only enforcement, deterministic output,
truthful pre-downloaded-source provenance, a three-write table-finalization
ceiling with a final read-only verification, and app schema compatibility.

## Production-sized source validation

The 2026-09-15 EPGShare ALL source and its official companion text catalog were
processed by the final code:

- compressed XML: **205,642,832 bytes**;
- expanded XML: **1,945,216,128 bytes**;
- unique channel IDs: **27,114**;
- top-level XML records: **2,767,623**;
- programmes: **2,740,254**;
- XML and text-catalog exact ID sets: **identical**;
- safe automatic-matcher inputs: **26,190 real + 163 dummy**;
- live one-pass preflight, complete parse, and sealed SQLite spool: **passed**;
- local audit runtime: approximately **1 minute 57 seconds**.

Source SHA-256:

```text
XML  85b98e89474c3444d9b8ee7e309abdb6595fe57d1c2c23feb838a1a725974e29
TXT  f30b93a1fec6bef66e63cf7a3bce733315e4a663ae5d1c1baee9cd4bdcdcc305
```

The source contained legitimate dotless IDs, internal ASCII spaces, and 36 IDs
with non-ASCII whitespace. All remain available for exact existing mappings;
the normalization-confusable set is conservatively excluded from unattended
new-channel matching.

## Attached failure diagnosis

The supplied `summary.json` is a write-enabled report generated at
`2026-09-15T17:31:51Z`. It records 24,588 new streams and 1,499 new reuse
alerts, but zero durable alert rows and zero mapping rows. The safety boundary
correctly blocked the mapping append and build snapshot when the first Google
table append could not be confirmed.

The report came from the older workflow revision because it lacks the
`auto_match_*` fields that the repaired Version 1 workflow always emits. The
new overlay must therefore be installed before another write test.

The repaired writer now uses Google's logical RAW row append with `INSERT_ROWS`,
checks the exact committed ranges and cell counts, expands an imported native
table only after values exist, and authoritatively rereads the rows. Unit tests
cover the header-only imported `Sync Alerts` table that failed. A real write to
the owner's private Sheet is deliberately left to the documented OFF-then-ON
test.

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
| `scripts/build_epg_streaming.py` | `b85556ddf99466bb01fa7baa93be2a3bebb694aba856bfc97b82673a9f005bba` |
| `src/skytv_epg_auto_match_v1.py` | `e2f175e3fd5be2cbe814305ef8eb5ae05dddce054e3ca0e46b1aab44988a049c` |
| `scripts/auto_match_inventory.py` | `29dc4ce5304dda30d5df1b01f83075108ec42ad150cb6699e0c54ac4b9df2dcf` |
| `scripts/epg_catalog_stream.py` | `bfbdd7ee05eba3074e38919fdccdcfe5f1dad3195b87dad53f1e40100e413a5f` |
| `scripts/epg_selection_spool.py` | `c0ad4e485972b206515f2205e37cf056593cb1105035a6031ef95b5371cd1336` |
| `scripts/sync_channel_inventory.py` | `72646cf46067a8a580c513e43dee6e673f00f8682629fbe181ea3cbc81a8785b` |
| `.github/workflows/main.yml` | `3f5314fb84e66d1b9aa20486c61e5ebc9ea4e809e8f562cbd96c0de8147e52db` |
| `.github/workflows/channel_inventory_sync.yml` | `d928bd229ed307f1e6df85fd25decc31d0f8ba3b260420e7245cb2010baa8819` |
| `requirements-sync.txt` | `cb80ac4377fa3656ea135c65273fdc1b6ba7f5ce198f1f14ccb13de0e3ed593f` |

## External validation still required

This release did not use the owner's provider credentials, Google service
account, private Sheet, or GitHub Pages environment. Keep the current Sheet and
repository, install the overlay, confirm the exact service account is an Editor,
run the channel workflow with writes off, then run it with writes on. Do not run
Workflow 2 until the write-enabled run is green.
