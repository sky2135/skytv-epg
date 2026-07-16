# SKY TV EPG — optimized v7.1 matcher and GitHub automation

This package keeps the proven Smart Rules v7 matcher and Builder v7.1 intact,
while making the surrounding runtime and three-server refresh process more
efficient.

## What is included

- `SKYTV_EPG_v7_1_Optimized_Matcher_Frozen.ipynb` — the Colab notebook. The
  original engine cells are unchanged; a separate performance cell is added.
- `src/skytv_epg_engine.py` — byte-for-byte assembly of the two original engine
  cells from the supplied notebook.
- `src/skytv_epg_optimizations.py` — deterministic wrappers for parallel text
  catalog downloads and exact-catalog index reuse.
- `scripts/build_all_servers.py` — production builder for three reviewed
  mappings. It downloads each distinct XMLTV source once per workflow run.
- `.github/workflows/update_epg.yml` — GitHub Actions build and Pages deployment.
- `tests/test_matcher_regression.py` — matcher/output equality and ordering tests.
- `MATCHER_INTEGRITY.json` — SHA-256 fingerprints of the frozen engine and key
  functions.
- `config/epg_sources.json` — the original source registry, including `BEIN1`.

## Important design rule

The scheduled job **never performs fuzzy channel matching**. It consumes only
mapping CSVs that have already been approved. This prevents a changed provider
channel name or changed catalog from silently attaching the wrong schedule.

The interactive Colab notebook remains the place to inventory channels, run the
v7 matcher, inspect doubtful rows, and approve a new mapping.

# Part A — prepare the three approved mapping files

Do this once before enabling the GitHub workflow, and repeat it only when the
channel lineup or mappings change.

1. Open `SKYTV_EPG_v7_1_Optimized_Matcher_Frozen.ipynb` in Google Colab.
2. Run the cells from the top.
3. Run Stage 1 for `server_1`, review doubtful rows as usual, and run Stage 2.
4. Download the Stage 2 output ZIP.
5. Extract `server_1_final_mapping.csv` and place it in this repository's
   `mappings` folder.
6. Repeat for `server_2` and `server_3`.

The final folder must contain:

```text
mappings/server_1_final_mapping.csv
mappings/server_2_final_mapping.csv
mappings/server_3_final_mapping.csv
```

The workflow deliberately stops with a clear error if any file is missing.

# Part B — create the GitHub repository

1. Sign in to GitHub and create a new repository, for example `skytv-epg`.
2. Upload the **contents** of this folder to the repository. Preserve the hidden
   `.github` folder and its `workflows/update_epg.yml` path.
3. Add the three approved mapping CSVs described above.
4. Commit the files to the default branch, normally `main`.

A public repository makes the mappings and configuration public. The server
credentials are still protected because they are stored only as encrypted
GitHub Actions secrets. Use a private repository only when the GitHub plan being
used supports the desired Pages publication setup.

# Part C — add GitHub Actions secrets

Open the repository, then go to:

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Create the following secrets for each server whose mapping contains panel rows.
A panel row has `source=panel`, `feed_key=PANEL`, or
`epg_feed=server xmltv.php`.

| Secret name | Value |
|---|---|
| `SERVER_1_BASE_URL` | Base URL only, such as `https://provider.example` |
| `SERVER_1_USERNAME` | Dedicated catalog account username |
| `SERVER_1_PASSWORD` | Dedicated catalog account password |
| `SERVER_2_BASE_URL` | Server 2 base URL |
| `SERVER_2_USERNAME` | Server 2 username |
| `SERVER_2_PASSWORD` | Server 2 password |
| `SERVER_3_BASE_URL` | Server 3 base URL |
| `SERVER_3_USERNAME` | Server 3 username |
| `SERVER_3_PASSWORD` | Server 3 password |

Secrets for a server are not required when that server's mapping uses only
EPGShare/dummy feeds.

Use a dedicated read-only/catalog account. Prefer HTTPS. With an HTTP panel,
credentials are not encrypted while travelling between the GitHub runner and
the panel.

Never put credentials, playable URLs, or tokens in a CSV, notebook, JSON file,
commit, issue, or workflow log.

# Part D — enable workflow permissions and GitHub Pages

## Workflow write permission

Go to:

`Settings` → `Actions` → `General` → `Workflow permissions`

Select **Read and write permissions**, then save. This allows the workflow to
record the timestamp of the last successful deployment in
`state/last_success.json`.

A branch-protection rule must also permit the GitHub Actions bot to update that
one state file. If the Pages deployment succeeds but the final state commit
fails, the due-check cannot remember the success and may rebuild too often.

## GitHub Pages

Go to:

`Settings` → `Pages` → `Build and deployment` → `Source`

Choose **GitHub Actions**.

# Part E — run the first build

1. Open the repository's **Actions** tab.
2. Select **Update TiviMate EPG**.
3. Select **Run workflow**.
4. Open the run and confirm that all three jobs are green:
   `due-check`, `build`, and `deploy`.
5. Open `Settings` → `Pages` to see the published site address.

A manual run always forces a build, even when 72 hours have not passed.

# Final TiviMate/TVMeta download addresses

For a repository named `skytv-epg`, the stable addresses are:

```text
https://YOUR_GITHUB_USERNAME.github.io/skytv-epg/epg/server_1_tivimate.xml.gz
https://YOUR_GITHUB_USERNAME.github.io/skytv-epg/epg/server_2_tivimate.xml.gz
https://YOUR_GITHUB_USERNAME.github.io/skytv-epg/epg/server_3_tivimate.xml.gz
```

Use the address for the corresponding server as the EPG source in TiviMate.
The Pages home page also lists the current files, generation time, and coverage.
A machine-readable index is published at:

```text
https://YOUR_GITHUB_USERNAME.github.io/skytv-epg/epg/index.json
```

# How the three-day schedule works

The workflow checks at minute 37 every six hours in the `America/Toronto` time
zone. The expensive build runs only when at least 72 hours have elapsed since
the last successful Pages deployment. This is more reliable than a day-of-month
`*/3` expression, which resets at month boundaries.

The actual refresh is therefore normally between 72 and 78 hours after the
previous successful deployment, plus any delay imposed by GitHub's scheduled
runner queue. A failed build does not advance the success timestamp, so the next
scheduled check retries it.

# What is and is not published

GitHub Pages receives only:

- the three `.xml.gz` files;
- a small manifest, validation file, and refresh plan per server;
- `epg/index.json`, `health.json`, and a simple download page.

The full builder reports are retained for 14 days as a GitHub Actions artifact
for repository collaborators. Credentials are never written to either output.

# Updating feeds or mappings later

## Add or change an EPGShare source

Edit `config/epg_sources.json`. The scheduled builder loads that file before it
validates the mappings. A mapping that references an unknown feed stops rather
than silently falling back.

## New, renamed, or removed channels

Run Stage 1 in the optimized notebook again for that server, review the changed
rows, run Stage 2, and replace only that server's
`mappings/server_X_final_mapping.csv`. Do not enable scheduled fuzzy rematching.

## Test without waiting three days

Use **Actions** → **Update TiviMate EPG** → **Run workflow**. A manual run is
forced and updates the successful-deployment timestamp after Pages is live.

# Common failures

### `Approved mapping files are required`

One or more `mappings/server_X_final_mapping.csv` files are missing or named
incorrectly.

### `Unknown EPG feed names in mapping`

The mapping references a source not present in `config/epg_sources.json`. Add the
exact reviewed source record or correct the mapping.

### `SERVER_X_... secret is missing`

That mapping contains panel rows. Add all three secrets for that server. The
base URL must not contain `/player_api.php`, `/xmltv.php`, a username, or a
password.

### Pages deployment succeeds but `Commit the new success timestamp` fails

Enable Actions **Read and write permissions** and adjust branch protection so
the workflow can push the state update.

### The workflow does not appear in Actions

Confirm this exact path exists on the default branch:

```text
.github/workflows/update_epg.yml
```

### TiviMate still shows old data

Open the Pages home page or `epg/index.json` and check `generatedAtIso`. If it is
current, trigger an EPG refresh inside TiviMate and verify that the configured
URL is the Pages URL for the correct server.

# Local validation commands

From the repository root:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_all_servers.py
```

The last command requires the three mapping files, Internet access, and panel
secrets in environment variables when panel schedules are used.

# Hosting limits to monitor

GitHub currently documents a 1 GB maximum published Pages site, a 10-minute
Pages deployment timeout, and a soft 100 GB/month bandwidth limit. The XMLTV
files are compressed, so a normal three-server EPG should be far below these
limits, but check the `compressedBytes` values in `epg/index.json` after the
first run. Move the output to object storage/CDN if the published site or
traffic approaches those limits.
