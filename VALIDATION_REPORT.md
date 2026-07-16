# Validation report

**Package:** SKY TV EPG optimized Smart Rules v7.0 / Builder v7.1  
**Validated:** 2026-07-15  
**Result:** PASS, with the live-provider test still to be completed in the user's GitHub repository.

## Frozen algorithm verification

- Supplied notebook SHA-256: `c3388902d30a2f9930282cefe7d08f6e936057d78fc62174210cd32641ac1110`
- Frozen engine SHA-256: `8040562b85758a6b0c7b59a7d0e7918f313f3ccf7829498401a8815d097bddf4`
- Optimized notebook SHA-256: `7f40bd70b56f4471c54e043fd5611379357a607dab7d436d3354351c19ea5681`
- The optimized notebook's two original engine cells have the same hashes as the supplied notebook.
- No matcher or XMLTV builder function body was edited.
- `MATCHER_INTEGRITY.json` records the frozen engine, function, and source-cell hashes used by the automated test.

## Automated test results

Command:

```bash
python -m unittest discover -s tests -v
```

Result: **5 tests passed**.

1. `test_frozen_engine_hash` — frozen engine matches the recorded SHA-256.
2. `test_optimized_notebook_contains_unchanged_engine_cells` — the optimized notebook contains the original engine cells unchanged.
3. `test_optimized_outputs_equal_frozen_outputs` — both engines pass all 54 built-in v7 safety cases and return exactly equal DataFrames, including row order, column order, values, and dtypes. The optimized second prepare call reuses the exact same candidate index.
4. `test_parallel_catalog_download_preserves_original_order` — concurrent catalog downloads preserve the original feed, candidate, and dummy order even when a later feed completes first.
5. `test_three_servers_share_source_downloads` — a local end-to-end three-server build produced all three gzip XMLTV files while requesting a shared EPG source only once and the required panel XML only once.

Additional checks:

- Notebook schema validation: **PASS** (`nbformat` v4, 15 cells).
- Python syntax compilation: **PASS** for all 8 source, runner, and test files.
- GitHub Actions YAML parse and expected-job structure: **PASS**.
- Notebook outputs: **empty**; no prior execution output was carried into the optimized notebook.
- Repository credential/embedded panel-URL scan: **no credential values or playable URLs found**.

## What was deliberately not changed

The following remain inside the frozen engine exactly as supplied: all Smart Rules v7 aliases, regexes, normalization, region rules, candidate filtering, scoring, thresholds, margins, tie-breaking, panel schedule acceptance, safety checks, canonical-ID conflict handling, programme filtering, XMLTV writing, validation, and refresh-plan calculations.

The speed improvements are outside that boundary: exact-catalog index reuse, deterministic concurrent text-catalog downloads, a shared XML download cache for the three production builds, and separation of interactive matching from unattended reviewed-mapping builds.

## Required live test

This environment could not authenticate to the user's IPTV panels and the package intentionally does not contain the three private approved mapping CSVs or GitHub secrets. Therefore, provider downloads and the final public GitHub Pages addresses must be verified with the first manual **Update TiviMate EPG** workflow run.

Before that run, add:

```text
mappings/server_1_final_mapping.csv
mappings/server_2_final_mapping.csv
mappings/server_3_final_mapping.csv
```

Add `SERVER_X_BASE_URL`, `SERVER_X_USERNAME`, and `SERVER_X_PASSWORD` GitHub Actions secrets only for mappings that contain panel XMLTV rows. A successful first run is the acceptance test for real source availability, panel access, current mappings, and Pages publication.
