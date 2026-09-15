# SKY TV EPG Version 1 — validation report

**Local release status: PASS**  
Validated: 2026-09-15 UTC

The owner still must complete the report-only provider check and first GitHub
Pages deployment in
[`docs/START_HERE_VERSION_1.md`](docs/START_HERE_VERSION_1.md). Those steps
validate private credentials and external services that cannot be exercised in
this release environment.

## Automated code checks

- Full repository suite: **153 tests passed, 0 failed**.
- Focused streaming and channel-inventory suite: **100 tests passed**.
- Python compilation: `scripts`, `src`, and `tests` passed.
- Installed dependency consistency: `pip check` passed.
- Integrity-manifest hashes: passed as part of the full test suite.
- Workflow parse: 3 YAML files, 21 shell `run` blocks, and 8 embedded Python
  programs parsed or compiled successfully; duplicate YAML keys were rejected.
- No private-key, GitHub-token, or Google-API-key signature was found in the
  Version 1 overlay.

Regression coverage includes malformed/truncated gzip and XML, DTD rejection,
bounded streaming, exact channel identities, Google Sheet append verification,
formula-safe text, stream-ID reuse quarantine, provider outage behavior,
password/private-address and credential-shaped username reflection checks,
path/symlink safety, deterministic
gzip bytes, Server 1 EPGShare-only enforcement, and app schema compatibility.
Rows that are not runtime-eligible—including `REVIEW` rows—are excluded from
schedules, personalization metadata, and detailed public reports.

## Mapping artifacts

- Seed CSV SHA-256:
  `ed9eb8ed230cd628d92ad900349ae60212641d97b2d7d86fffc1e2f640dbb5d0`
- Google Sheet workbook SHA-256:
  `c0a1d9fcba5c8c6badb37836c513a5f6d206eff705819156f848886c8c738b09`
- Mapping rows: **25,170**, all unique by `(server_id, stream_id)`.
- Server rows: Server 1 **4,202**; Server 2 **11,025**; Server 3 **9,943**.
- Sources: `dummy` **11,939**; `epgshare01` **9,062**; `panel` **4,169**.
- Review state: **171** rows; all 171 are disabled.
- Workbook: 4 expected sheets, 0 formulas, 0 error cells, 0 macros, 0 external
  links, and exact bounded validation/table ranges.
- Every one of the workbook's 830,610 mapping cells matches the seed after the
  seed's CSV-only spreadsheet-literal escape is removed.
- All five worksheet previews passed visual inspection.

## Version 1 file integrity

| File | SHA-256 |
|---|---|
| `scripts/build_epg_streaming.py` | `6f5802c317d941cf6da19bfdc356e4c7c242dbf640f6c42bb3206899c7a9f39a` |
| `scripts/export_google_sheet_seed.py` | `c5530d0a2dd63c62b8ffafed102651859e0155fe9ba5b731d5ced1ab6e17d80b` |
| `scripts/sync_channel_inventory.py` | `b85d3458b53c9d09c9ea461ac987ec5d8cb2696536b5761d32c320de9f0ddff8` |
| `.github/workflows/main.yml` | `8f3b7976bd462211d091742f6d4bfa97de438ccf76f4be39f5dc5e2d2cd7d44b` |
| `.github/workflows/channel_inventory_sync.yml` | `f046bc421d25fc111152586d5b75359e2212ce79da288a4a6172d7e6f95a85cc` |
| `requirements-sync.txt` | `19a8506776d25e36b399e48ea478120d524823f5037fdca728dce01eca3df86a` |

## External validation still required

This local pass did not use the owner's three provider accounts, Google
service-account key, private Google Sheet, or GitHub Pages environment. It also
did not repeat a production-sized combined-feed build with those private live
inputs. Follow Sections 11–15 of the setup guide in order. The first run is
deliberately report-only; do not enable the Sheet write until it succeeds for
all three configured providers.
