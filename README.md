# SKY TV EPG — Smart Rules v8.4 + approved-mapping GitHub builder

This repository contains:

- the Smart Rules v8.4 contextual matcher used in Colab to create reviewed mappings;
- the unchanged Smart Rules v7 compatibility engine and Builder v7.1;
- a three-server GitHub Actions build that uses approved mapping CSV files only;
- exact XMLTV channel-icon enrichment;
- GitHub Pages publication for TVMeta and TiviMate.

## Safety boundary

The scheduled GitHub workflow never rematches channels. It reads only:

```text
mappings/server_1_final_mapping.csv
mappings/server_2_final_mapping.csv
mappings/server_3_final_mapping.csv
```

A provider rename therefore cannot silently change an approved channel-to-EPG decision during an unattended refresh.

Smart Rules v8.4 remains available in:

```text
SKYTV_EPG_v8_4_Colab_Only.ipynb
src/skytv_epg_contextual_v8.py
```

Use the notebook when a server lineup changes, review uncertain rows, then replace only the affected approved mapping CSV.

## Repository layout

Upload the contents of this package directly to the root of a dedicated repository:

```text
.github/workflows/main.yml
assets/logos/
config/channel_icons.csv
config/epg_sources.json
knowledge/
mappings/
scripts/
src/
state/last_success.json
tests/
README.md
requirements.txt
```

Do not upload another outer folder above `.github`.

## Scheduled refresh behavior

The workflow is intentionally a two-stage scheduler:

1. GitHub starts a lightweight due-check every six hours at minute 37.
2. The expensive build and deploy run only after at least 72 hours have elapsed since the last successful deployment.

Therefore, a scheduled run can legitimately show:

```text
due-check   success
build       skipped
deploy      skipped
```

That means the current published EPG is still active and the next refresh is not due yet. GitHub displays a skipped job as a successful workflow result.

The v8.4 workflow adds a visible `Refresh not due - published EPG remains active` job and writes the last-success and next-due timestamps into the run summary.

A manual run defaults to `force_build = true`, so it builds and deploys immediately even when 72 hours have not elapsed.

The rolling due-check is preferable to a calendar expression such as “every third day of the month,” which does not produce a reliable 72-hour interval across month boundaries.

## XMLTV channel icons

The generated `.xml.gz` files can now contain:

```xml
<channel id="PTC.PUNJABI.in">
  <display-name>PTC Punjabi</display-name>
  <icon src="https://example.org/logos/ptc-punjabi.png"/>
</channel>
```

The icon layer is separate from channel matching and never changes an EPG decision.

### Exact icon priority

1. An exact icon URL already carried in an approved mapping row, if such a column exists.
2. An exact row in `config/channel_icons.csv`.
3. An exact `<icon>` already supplied by the selected source XMLTV channel.
4. No icon.

Logo filenames and channel names are never fuzzily matched. A missing icon is safer than the wrong network logo.

### External URL override

Add an exact row to `config/channel_icons.csv`:

```csv
enabled,server_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,PTC.PUNJABI.in,,https://example.org/ptc-punjabi.png,,100,Verified URL
```

### Locally hosted logo

1. Put the permitted image in:

```text
assets/logos/india/ptc-punjabi-in.png
```

2. Add:

```csv
enabled,server_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,PTC.PUNJABI.in,,,india/ptc-punjabi-in.png,100,Locally hosted
```

The build copies approved local files to `public/logos/` and uses the GitHub Pages URL in the XMLTV file.

For a normal repository Pages address, the workflow derives the base URL automatically. For a custom domain, create this Actions repository variable:

```text
EPG_PUBLIC_BASE_URL=https://epg.example.com
```

Keep attribution and usage records in:

```text
assets/logos/ATTRIBUTION.md
```

## Recommendation for `tv-logo/tv-logos`

Use it as a selective source, not as an automatically mirrored or fuzzy-matched database.

Recommended production policy:

1. Keep source XMLTV icons when available.
2. For a missing or incorrect logo, verify the exact channel and country manually.
3. During personal testing, an exact raw image URL can be entered in `channel_icons.csv`.
4. For long-term reliability, host only the small reviewed subset you need under `assets/logos/`, provided you have permission to redistribute it and record attribution.
5. Do not copy the entire third-party repository into this project.

The upstream project states that direct raw links can break, asks for reference when redistributing, and asks service operators to contact the maintainer. Channel logos are also trademarks belonging to their owners. Review those terms before public redistribution.

## Required approved mappings

The automated build stops when any required file is missing:

```text
mappings/server_1_final_mapping.csv
mappings/server_2_final_mapping.csv
mappings/server_3_final_mapping.csv
```

The upgrade overlay deliberately excludes `mappings/`, `state/`, and generated `public/` files so an existing repository is not reset.

## GitHub secrets

Add credentials only for a server whose approved mapping contains panel XMLTV rows:

```text
SERVER_1_BASE_URL
SERVER_1_USERNAME
SERVER_1_PASSWORD
SERVER_2_BASE_URL
SERVER_2_USERNAME
SERVER_2_PASSWORD
SERVER_3_BASE_URL
SERVER_3_USERNAME
SERVER_3_PASSWORD
```

The base URL should be the provider origin, including its required port or path, but not `/player_api.php`, `/xmltv.php`, username, or password.

## GitHub settings

Enable write permission for the successful-deployment timestamp:

```text
Settings
→ Actions
→ General
→ Workflow permissions
→ Read and write permissions
```

Enable Pages:

```text
Settings
→ Pages
→ Build and deployment
→ Source
→ GitHub Actions
```

A branch-protection rule must allow `github-actions[bot]` to update:

```text
state/last_success.json
```

## First run

Open:

```text
Actions
→ Update TVMeta and TiviMate EPG v8.4
→ Run workflow
```

Leave `force_build` enabled for the first run.

A forced successful run should execute:

```text
Check whether refresh is due
Build approved EPG files and icons
Deploy EPG files to GitHub Pages
```

## Published files

For a repository named `skytv-epg`:

```text
https://YOUR_USERNAME.github.io/skytv-epg/epg/server_1_tivimate.xml.gz
https://YOUR_USERNAME.github.io/skytv-epg/epg/server_2_tivimate.xml.gz
https://YOUR_USERNAME.github.io/skytv-epg/epg/server_3_tivimate.xml.gz
```

Status and coverage:

```text
https://YOUR_USERNAME.github.io/skytv-epg/epg/index.json
```

The index includes programme coverage and icon coverage for each server.

## Private diagnostic reports

Each actual build retains a private GitHub Actions artifact for 14 days. It includes per-server reports, including:

```text
server_X_icon_report.csv
server_X_tivimate_manifest.json
server_X_validation.json
server_X_missing_epg_ids.csv
server_X_missing_future_epg_ids.csv
```

## Local validation

From the repository root:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The test suite verifies Smart Rules v8.4, all embedded regression cases, frozen-engine integrity, ordered shared downloads, exact icon behavior, XMLTV icon insertion, and a three-server end-to-end build.
