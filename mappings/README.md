# Approved mapping files required

The scheduled workflow intentionally does **not** run automatic/fuzzy matching.
It builds only from reviewed mappings.

Place these three files in this folder:

- `server_1_final_mapping.csv`
- `server_2_final_mapping.csv`
- `server_3_final_mapping.csv`

The easiest source is the Stage 2 output ZIP from the optimized notebook. Each
ZIP already contains the corresponding `server_X_final_mapping.csv` file.

Optional category-coverage reports may also be placed here:

- `server_1_channel_report.csv`
- `server_2_channel_report.csv`
- `server_3_channel_report.csv`

A final mapping may contain extra report columns. The frozen builder normalizes
it to the original mapping columns and rejects rows still marked `REVIEW`,
`UNMATCHED`, `NO_EPG`, `UNRESOLVED`, or `SKIP`.

Never put a server URL, username, password, playlist URL, or token in a CSV.
