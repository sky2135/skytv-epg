# SKY TV EPG Version 1 — validation report

**Local release status: PASS**  
Validated through: 2026-09-16 UTC

The catalog-rollover repair has passed a production-sized one-pass validation
against the currently published EPGShare XML and text catalog. The integrity
manifest has been resealed for the repaired files, and the complete repository
suite now passes.

The owner must still run the private Google Sheet/provider checks in
[`docs/REPAIR_CURRENT_SETUP_VERSION_1.md`](docs/REPAIR_CURRENT_SETUP_VERSION_1.md).
Those are the only checks requiring the owner's private accounts.

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

- Full repository suite: **265 tests passed, 0 failed**.
- Exact manual-workflow suite: **186 tests passed, 0 failed**.
- Frozen matcher regression: **222 matching and safety cases passed**.
- Independent adversarial catalog/auto-match/sync audit: **136 focused tests
  passed with no remaining approval-safety blocker**.
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
row-accurate approval counts, malformed/truncated gzip and XML, DTD/entity
rejection, bounded streaming, exact opaque channel IDs,
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

## The two reported failures are separate

### Current exit-code-2 failure

The current error occurs during public EPGShare catalog validation. It is not
caused by Server 3 credentials and happens before Google Sheet mapping writes.

- `SERVER_3_PASSWORD: ***` is GitHub Actions masking a configured secret; it
  does not indicate that the password is literally `***` or that login failed.
- `Warning: Provider credentials may be sent over unencrypted HTTP.` is a
  transport-security warning because the provider URL uses HTTP. It should be
  addressed by switching to the provider's HTTPS endpoint if one exists, but it
  did not cause this exact-ID mismatch.
- `Process completed with exit code 2` is the workflow's controlled stop after
  the old exact-catalog check rejected the 20-ID upstream rollover.

### Earlier attached `summary.json` failure

The attached report was generated at `2026-09-15T17:31:51Z` by the older
write-enabled workflow. It records 49,016 inventory rows, 24,588 new streams,
1,499 possible stream-ID reuse alerts, zero durably confirmed alert rows, and
zero appended mapping rows. The safety boundary correctly blocked both the
mapping append and build snapshot because Google did not authoritatively confirm
the `Sync Alerts` append.

That earlier event was a Google Sheet durable-write verification failure. It is
not evidence for, and did not cause, today's public XML/TXT catalog mismatch.
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
| `scripts/build_epg_streaming.py` | `b85556ddf99466bb01fa7baa93be2a3bebb694aba856bfc97b82673a9f005bba` |
| `src/skytv_epg_auto_match_v1.py` | `e2f175e3fd5be2cbe814305ef8eb5ae05dddce054e3ca0e46b1aab44988a049c` |
| `scripts/auto_match_inventory.py` | `647f7bab7d11e9021639f67038a9d2faba54356c85da1b5513ec2cbcd882a66b` |
| `scripts/epg_catalog_stream.py` | `fb04f5f9097bffe96ba4f3a421b4d53f1ce6110dc80e390ee45a91f2d99a9821` |
| `scripts/epg_selection_spool.py` | `c0ad4e485972b206515f2205e37cf056593cb1105035a6031ef95b5371cd1336` |
| `scripts/sync_channel_inventory.py` | `72646cf46067a8a580c513e43dee6e673f00f8682629fbe181ea3cbc81a8785b` |
| `.github/workflows/main.yml` | `eea26e0dd9959e82c85d87d3bcca4519f8f3123f68c4b6ca9f75836b12d4f122` |
| `.github/workflows/channel_inventory_sync.yml` | `0f4a477b18012436691bf08ffb884eaa818fc14bbaa3ac871b55bc37f97a4eb4` |
| `requirements-sync.txt` | `cb80ac4377fa3656ea135c65273fdc1b6ba7f5ce198f1f14ccb13de0e3ed593f` |
| `MATCHER_INTEGRITY.json` | `96918aa8d1550ab2497bb23ae73120c5fcd728717faa06aaa651805b7d1f0fb4` |

## External validation still required

This local repair did not use the owner's provider credentials, Google service
account, private Sheet, or GitHub Pages environment. Keep the current Sheet and
repository, install the repaired overlay, confirm the exact service account is
an Editor, run the channel workflow with writes off, and then run it with writes
on. Do not run Workflow 2 until the write-enabled run is green.
