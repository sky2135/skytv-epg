# Version 1 migration sources — not the production mapping

The private Google Sheet is the Version 1 production mapping authority. The
CSV files in this folder are retained only as audited migration inputs and
frozen regression fixtures:

- `server_1_final_mapping.csv`
- `server_2_final_mapping.csv`
- `server_3_final_mapping.csv`

The Sheet synchronizer and production EPG builder do not edit these files. New
provider channels are appended to the private Google Sheet with `enabled=FALSE`
and `action=REVIEW`; they are not committed to this folder or any Git branch.
An operator must verify the row, approve its action, and explicitly set
`enabled=TRUE` before it can enter any public output.

Optional category-coverage reports may also be placed here:

- `server_1_channel_report.csv`
- `server_2_channel_report.csv`
- `server_3_channel_report.csv`

The one-time `scripts/export_google_sheet_seed.py` exporter merges these three
historical files, normalizes them to the 33-column Version 1 schema, and creates
the plain CSV seed used to build the supplied workbook. The user does not need
to run the exporter during normal setup. The CSV seed is frozen and never
receives future channels; the imported private Google Sheet becomes the live
mapping authority.

The Sheet's first strict inventory bootstrap adds every missing valid, uniquely
identified live channel returned by Server 1, Server 2, and Server 3.

For current instructions and contracts, see:

- [`../docs/START_HERE_VERSION_1.md`](../docs/START_HERE_VERSION_1.md)
- [`../docs/TECHNICAL_REFERENCE_VERSION_1.md`](../docs/TECHNICAL_REFERENCE_VERSION_1.md)

Never put a server URL, username, password, playlist URL, service-account key,
or token in a mapping CSV or in the Google Sheet.
