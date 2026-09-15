# SKY TV EPG Version 1 — complete setup

Follow these sections in order. Do not skip ahead.

## The setup you will use

- Keep the existing repository: `https://github.com/sky2135/skytv-epg`.
- Use its existing `main` branch.
- Do **not** create a new repository.
- Do **not** create a `gh-pages` branch.
- Do **not** delete the existing `live-data` branch or the live-sports workflow.
- Keep the private Google Sheet as the mapping master. Do not publish it and do
  not upload the mapping CSV to GitHub.

## 1. Collect what you need

Have these ready before starting:

- the downloaded file `SKYTV_EPG_VERSION_1_COMPLETE_SETUP.zip`;
- a Google account;
- access to the GitHub account that owns `sky2135/skytv-epg`;
- the base URL, username, and password for Server 1;
- the base URL, username, and password for Server 2; and
- the base URL, username, and password for Server 3.

A provider username may be any nonempty length. Each provider password must
contain at least four characters; use a long, random password whenever the
provider allows it. Every configured password is searched throughout private
snapshots and generated files, including common serialized forms. A common
password can therefore match ordinary EPG text and safely stop the run; ask the
provider to rotate it to a longer, random value instead of bypassing the check.
Short or common usernames are checked in credential fields and URLs, while
distinctive usernames also receive a raw-content search.

A server base URL normally looks like one of these:

```text
https://provider.example.com
https://provider.example.com:1234
```

Do not add `/player_api.php`, `/get.php`, a username, or a password to the base
URL.

## 2. Unzip the Version 1 package

1. Find `SKYTV_EPG_VERSION_1_COMPLETE_SETUP.zip` on your computer.
2. Right-click it and choose **Extract All** on Windows, or double-click it on
   macOS.
3. Open the extracted folder.
4. Confirm that it contains:
   - `repo-overlay`;
   - `spreadsheet`; and
   - `START_HERE.md`.
5. Do not upload the ZIP itself to GitHub.

## 3. Put the Version 1 files in the existing GitHub repository

1. Open `https://github.com/sky2135/skytv-epg`.
2. Above the file list, check that the branch selector says **main**.
3. Click **Add file**, then **Upload files**.
4. On your computer, open the package's `repo-overlay` folder.
5. Drag all files and folders **inside** `repo-overlay` onto the GitHub upload
   page. Do not drag the `repo-overlay` folder itself.
6. Make sure the `.github` folder is included. On macOS, press
   **Command + Shift + .** if hidden folders are not visible.
7. In **Commit message**, enter:

   ```text
   Install SKY TV EPG Version 1
   ```

8. Choose **Commit directly to the main branch**.
9. Click **Commit changes**.

If GitHub does not allow a direct commit, choose **Create a new branch for this
commit and start a pull request**, create the pull request, and then click
**Merge pull request**. Return to `main` before continuing.

After the commit, confirm these files exist in the repository:

```text
.github/workflows/main.yml
.github/workflows/channel_inventory_sync.yml
scripts/build_epg_streaming.py
scripts/sync_channel_inventory.py
requirements.txt
requirements-sync.txt
```

If `.github/workflows/channel_inventory_sync.yml` is missing, open the existing
`.github/workflows` folder on GitHub, click **Add file** → **Upload files**, and
upload that file from `repo-overlay/.github/workflows`.

## 4. Create the Google Sheet from the supplied workbook

1. Open `https://sheets.google.com` and sign in.
2. Create a **Blank spreadsheet**.
3. In the blank spreadsheet, click **File** → **Import**.
4. Click **Upload** → **Browse**.
5. Select:

   ```text
   spreadsheet/SKYTV_EPG_VERSION_1_GOOGLE_SHEET.xlsx
   ```

6. For **Import location**, choose **Replace spreadsheet**.
7. Click **Import data**.
8. Rename the spreadsheet to:

   ```text
   SKY TV EPG Version 1 Mapping
   ```

9. Confirm that the workbook includes both of these tabs:

   ```text
   Mappings
   Sync Alerts
   ```

10. Do not rename or delete either tab, and do not change either tab's first
    header row.
11. Confirm that `Sync Alerts` has these 11 columns in this exact order:

    ```text
    detected_at
    server_id
    stream_id
    alert_type
    sheet_channel_name
    provider_channel_name
    sheet_category_name
    provider_category_name
    action_taken
    status
    review_notes
    ```

The workbook's `Mappings` tab and
`SKYTV_EPG_VERSION_1_MAPPING_SEED.csv` both start with the same 25,170
historical rows. The CSV seed is only a frozen, plain-text backup. It never
updates and never receives future channels. Do not upload it to GitHub and do
not use it instead of the workbook during this setup.

The private Google Sheet becomes the live mapping after Sections 11 and 12.
The inventory run in Section 12 adds channels that are not already present.
After that successful run, `Mappings` contains every valid, unique live stream
returned by the configured API or M3U for each server during that run.

The 25,170 starter rows are historical schedule mappings, not a completely
human-reviewed personalization catalog. They include 11,939 approved
dummy-guide placeholders and 171 disabled rows that remain in `REVIEW`. Those
171 rows stay private and are excluded from schedules, public metadata, and
personalization until you review, approve, and enable them. All starter metadata
is automatically inferred and unlocked. In particular, 22,720 rows start with
an undetermined primary language, 11,447 with an unknown region, and 8,030 with
an unknown genre. This is safe: an unknown value does not enter a specific
language, region, genre, sport, or religion group. Section 13 explains how to
improve this metadata gradually.

## 5. Copy the Google Sheet ID

Look at the address bar while the Sheet is open. It will look like this:

```text
https://docs.google.com/spreadsheets/d/1AbCdEfExample123/edit
```

Copy only the part between `/d/` and `/edit`:

```text
1AbCdEfExample123
```

Save this value temporarily. It will become the GitHub variable
`GOOGLE_SHEET_ID`.

## 6. Create the Google service account that can add new channels

### 6A. Create a Google Cloud project

1. Open `https://console.cloud.google.com` with the same Google account.
2. Click the project selector at the top of the page.
3. Click **New Project**.
4. Enter this project name:

   ```text
   SKY TV EPG Version 1
   ```

5. Click **Create**.
6. When creation finishes, select that project at the top of the page.

### 6B. Enable the Google Sheets API

1. Open the navigation menu.
2. Click **APIs & Services** → **Library**.
3. Search for `Google Sheets API`.
4. Open **Google Sheets API**.
5. Click **Enable**.

Only the Google Sheets API is required for this setup.

### 6C. Create the service account

1. Open the navigation menu.
2. Click **IAM & Admin** → **Service Accounts**.
3. Click **Create service account**.
4. Enter:

   ```text
   Service account name: SKY TV EPG Sheet Writer
   Service account ID:   skytv-epg-sheet-writer
   Description:          Adds newly discovered IPTV channels to the mapping Sheet
   ```

5. Click **Create and continue**.
6. Do not select a Google Cloud role; this service account receives access only
   to the specific Sheet in the next section.
7. Click **Continue**, then **Done**.
8. In the service-account list, copy its email address. It ends with:

   ```text
   .iam.gserviceaccount.com
   ```

### 6D. Download one JSON key

1. Click the service account's email address.
2. Open the **Keys** tab.
3. Click **Add key** → **Create new key**.
4. Choose **JSON**.
5. Click **Create**.
6. A `.json` file downloads to your computer. Keep it private. Never upload it
   to GitHub, Google Sheets, email, or the repository.

If Google says service-account key creation is blocked, use a personal Google
Cloud project or ask the administrator of the Google account to allow this
project. Do not continue until the JSON key has been created.

## 7. Give the service account access to the Sheet

1. Return to `SKY TV EPG Version 1 Mapping` in Google Sheets.
2. Click **Share** in the top-right corner.
3. Paste the service-account email copied in Section 6C.
4. Select **Editor**.
5. If a **Notify people** box is shown, turn it off.
6. Click **Share** or **Send**.

Do not make the service account an owner. Editor access is enough.

## 8. Keep the Google Sheet private

Version 1 connects directly to Google Sheets. It does not need a published CSV
URL.

1. In the Google Sheet, click **Share**.
2. Under **General access**, select **Restricted**.
3. Confirm that your Google account is the owner and the service-account email
   is an Editor.
4. Click **Done**.
5. Do not use **File** → **Share** → **Publish to web**.

If this Sheet was published during an earlier attempt, open **File** →
**Share** → **Publish to web** → **Published content and settings**, then click
**Stop publishing**.

Never put server URLs, usernames, passwords, the service-account JSON, or any
other secret in the Sheet.

## 9. Add the GitHub variables and secrets

Open `https://github.com/sky2135/skytv-epg`, then click **Settings** →
**Secrets and variables** → **Actions**.

### 9A. Add repository variables

Open the **Variables** tab. Click **New repository variable** for each row:

| Name | Value |
|---|---|
| `GOOGLE_SHEET_ID` | The Sheet ID copied in Section 5 |
| `GOOGLE_SHEET_TAB` | `Mappings` |
| `EPG_PUBLIC_BASE_URL` | `https://sky2135.github.io/skytv-epg` |

If an old variable named `EPG_MAPPING_CSV_URL` exists, delete it. Version 1
reads the private Sheet directly and does not use that URL.

Leave `EPG_MINIMUM_COVERAGE` unset; Version 1 uses its safe default.

If every server base URL begins with `https://`, leave
`ALLOW_INSECURE_PANEL_HTTP` unset. If at least one provider supports only an
`http://` URL, add this variable:

| Name | Value |
|---|---|
| `ALLOW_INSECURE_PANEL_HTTP` | `true` |

Only use that setting when the provider gives you no HTTPS address. An HTTP
connection sends the server username and password without transport encryption.

### 9B. Add repository secrets

Open the **Secrets** tab. Click **New repository secret** for each row:

| Secret name | Paste this value |
|---|---|
| `SERVER_1_BASE_URL` | Server 1 base URL |
| `SERVER_1_USERNAME` | Server 1 username |
| `SERVER_1_PASSWORD` | Server 1 password |
| `SERVER_2_BASE_URL` | Server 2 base URL |
| `SERVER_2_USERNAME` | Server 2 username |
| `SERVER_2_PASSWORD` | Server 2 password |
| `SERVER_3_BASE_URL` | Server 3 base URL |
| `SERVER_3_USERNAME` | Server 3 username |
| `SERVER_3_PASSWORD` | Server 3 password |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The complete contents of the downloaded JSON key file |

For `GOOGLE_SERVICE_ACCOUNT_JSON`:

1. Open the downloaded `.json` key in Notepad, TextEdit, or another plain-text
   editor.
2. Select everything in the file.
3. Copy it.
4. Paste the complete text into the GitHub secret value box.
5. Click **Add secret**.

Server 1 credentials are used only to discover Server 1's channel list. The EPG
builder still forces Server 1 programme data to EPGShare and does not download
Server 1's native guide.

## 10. Enable GitHub Pages

1. In the repository, open **Settings**.
2. In the left sidebar, click **Pages**.
3. Under **Build and deployment**, find **Source**.
4. Select **GitHub Actions**.
5. Do not select **Deploy from a branch**.
6. Do not create or select a `gh-pages` branch.

The supplied workflow uploads a validated Pages artifact and deploys it without
committing generated XML or JSON files to Git history.

This free setup assumes that the existing repository remains public. Do not
change its visibility during setup. GitHub Pages for a private repository may
require a paid GitHub plan and a different Pages permission.

Use this GitHub Pages target only for personal, noncommercial use within its
published-site and bandwidth limits. GitHub does not allow Pages to act as free
hosting for an online business or commercial software service. Before serving
paying customers or high traffic, replace the Pages deployment with suitable
object storage and a CDN.

## 11. Run the first channel inventory check without changing the Sheet

1. Open the repository's **Actions** tab.
2. In the left sidebar, click **1 - Sync channels to Google Sheet**.
3. Click **Run workflow**.
4. Confirm the branch is **main**.
5. Leave **Add missing channels to Google Sheet** turned off. It is off by
   default, which makes this first run report-only.
6. Click the green **Run workflow** button.
7. Refresh the page if necessary, then click the new run.
8. Wait for the run to finish with a green check mark.
9. Read the run's summary. Confirm **Channels found at providers** is not zero
   and the run has no warning about a failed server.
10. The run summary shows how many channels were found, how many are new, and
    how many existing channels were not seen during this check.
11. If you want a copy of those counts, download the artifact whose name begins
    with `skytv-channel-sync-summary-`. It contains only `summary.json` and is
    kept for seven days.

This first run is a safety check. It does not write to Google Sheets.

## 12. Run the inventory again and add newly returned channels

Only continue after Section 11 finishes successfully for all three servers.

1. Return to **Actions** → **1 - Sync channels to Google Sheet**.
2. Click **Run workflow**.
3. Confirm the branch is **main**.
4. Turn on **Add missing channels to Google Sheet** for this second run.
5. Click **Run workflow**.
6. Wait for the green check mark.
7. Open the run summary and note the numbers of rows added and open alerts.
8. Return to the Google Sheet and reload the page.
9. Open the `Mappings` tab.
10. Scroll to the bottom or filter the `action` column for `REVIEW`. Newly
    discovered channels are appended there with `enabled=FALSE`.
11. Open `Sync Alerts` and filter the `status` column for `OPEN`.

The sync never edits or deletes an existing mapping row. It appends only a
previously unseen `(server_id, stream_id)` pair. A channel that disappears from
a provider is counted in the workflow summary but is not automatically removed.

## 13. Review newly discovered channels in Google Sheets

1. Open the `Mappings` tab.
2. Use the filter on the `action` column and select only `REVIEW`.
3. For each new channel you want in the guide:
   - check `channel_name` and `category_name`;
   - check `region_code`, `genre`, `primary_language`, and the other metadata;
   - enter or verify the exact EPGShare channel ID in `epg_id`;
   - set `source` to `epgshare01`;
   - set `epg_feed` to `ALL_SOURCES1`;
   - set `action` to `APPROVED`;
   - set `metadata_status` to `approved` after checking its classification; and
   - set `metadata_locked` to `TRUE` when the metadata should no longer be
     automatically inferred; then
   - set `enabled` to `TRUE` only after every check above is complete.
4. If you do not know the exact EPG ID, leave the row as `REVIEW`. Do not guess.
5. If you never want a channel, set `enabled` to `FALSE` and `action` to
   `IGNORE`. Do not delete the row, because the next inventory run would find
   and add it again.

Every new row starts with `enabled=FALSE`, `action=REVIEW`, and
`metadata_status=review`, so its provider-supplied text cannot enter any public
guide or metadata file before you approve and enable it. A Server 2 or Server 3
row may show the provider's native `epg_channel_id` as an
unapproved candidate. Verify both its source and ID before changing `action` to
`APPROVED`.

For Server 1, always use `epgshare01`; native Server 1 EPG is blocked. Servers 2
and 3 can use native panel EPG when deliberately configured, but EPGShare is the
normal choice for a new row.

Google Sheets saves changes automatically. Wait until the Sheet says
**Saved to Drive** before running the EPG build.

### Find an exact EPGShare ID

Use these steps whenever an `epgshare01` row has a blank or uncertain `epg_id`:

1. Open the official
   [EPGShare ALL Sources ID list (PDF)](https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.pdf).
2. Press **Ctrl + F** on Windows or **Command + F** on macOS.
3. Search for the channel name without quality labels such as `HD`, `FHD`, or
   `4K` if the first search finds nothing.
4. Compare the displayed channel, country, network, and regional edition. Do
   not choose an ID only because part of the name is similar.
5. Copy the complete ID exactly as shown and paste it into the row's `epg_id`
   cell. Keep `source=epgshare01` and `epg_feed=ALL_SOURCES1`.
6. If the PDF is difficult to search, use the official
   [plain-text ID list](https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.txt)
   instead.
7. If the channel is absent or two IDs are still possible, leave
   `action=REVIEW`. Do not guess.

### Review existing metadata for personalized groups

An active schedule mapping can still have incomplete personalization metadata.
The starter deliberately leaves uncertain values unknown instead of inventing
a language, region, genre, sport, or religion.

1. In `Mappings`, filter `primary_language` to `und`.
2. Review the channels whose language you know. If its language code is listed
   in the `Taxonomy` tab, enter that code in both `primary_language` and
   `language_codes`. Otherwise leave `und` until the code is verified.
3. Filter `region_code` to `unknown`, then review the rows whose region you can
   verify.
4. Filter `genre` to `unknown`, then review the rows whose genre you can verify.
5. For a sports row, use `sport_codes` only for a verified sport. A general
   sports channel should use `multi_sport`, not `cricket`.
6. For a religious row, use `religion_codes` only for a verified faith. Language
   by itself is not evidence of religion.
7. When all edited metadata on a row has been checked, set
   `metadata_status=approved`, `metadata_source=manual`,
   `metadata_confidence=1`, and `metadata_locked=TRUE`.
8. Leave uncertain values unchanged. Unknown values are safely excluded from
   preference groups that require those dimensions.

Start with the languages and interests your app users actually select. You do
not need to classify all 25,170 rows before the normal EPG build can run.

### Resolve an `OPEN` Sync Alert

An `OPEN` alert keeps that server and stream ID out of the effective build even
if its `Mappings` row looks approved. The quarantine stays active during later
runs and temporary provider outages until you resolve it.

1. Open the `Sync Alerts` tab.
2. Filter the `status` column to show only `OPEN`.
3. Read the alert's `server_id` and `stream_id`.
4. Open the `Mappings` tab.
5. Filter `server_id` and `stream_id` to find that exact mapping row.
6. Compare `sheet_channel_name` with `provider_channel_name` and compare the two
   category-name fields shown in the alert.
7. Fix and verify the `Mappings` row first:
   - make `channel_name` match the provider's current channel name;
   - correct `canonical_name` and `category_name` if needed;
   - recheck `epg_id`, `source`, and `action`;
   - recheck the language, region, genre, and other metadata; and
   - leave `action=REVIEW` if any identity or EPG choice is uncertain.
8. Wait until Google Sheets says **Saved to Drive**.
9. Return to the same row in `Sync Alerts`.
10. Optionally write what you checked in `review_notes`.
11. Change `status` from `OPEN` to `RESOLVED`. Do not enter another status.
12. Wait until Google Sheets says **Saved to Drive** again.
13. Run **2 - Build and publish EPG**.
14. When it finishes, reload `Sync Alerts` and filter `status` to `OPEN`.
15. Confirm there is no new `OPEN` row for the same `server_id` and `stream_id`,
    and confirm the build has a green check mark.

The old row stays `RESOLVED` as history. If the provider details and `Mappings`
row still disagree, Version 1 appends a **new** `OPEN` row for that identity.
Repeat these steps and do not force the channel into the guide.

## 14. Run the first EPG build and Pages deployment

1. Open the repository's **Actions** tab.
2. In the left sidebar, click **2 - Build and publish EPG**.
3. Click **Run workflow**.
4. Confirm the branch is **main**.
5. Click the green **Run workflow** button.
6. Open the new run and wait for both jobs to finish with green check marks:
   - **Stream, validate, and package EPG outputs**; and
   - **Deploy validated EPG to GitHub Pages**.
7. Open the run summary and confirm it says the streaming EPG build succeeded.
8. If needed, download the artifact beginning with `skytv-epg-diagnostics-`.

The first build can take much longer than the inventory check because it
downloads and streams the combined EPG file.

## 15. Verify the live files

Open each of these links in a browser.

Health check:

```text
https://sky2135.github.io/skytv-epg/health.json
```

It must show `"status": "ok"` and a recent `generatedAtUtc` value.

TiviMate XMLTV files:

```text
https://sky2135.github.io/skytv-epg/epg/server_1_tivimate.xml.gz
https://sky2135.github.io/skytv-epg/epg/server_2_tivimate.xml.gz
https://sky2135.github.io/skytv-epg/epg/server_3_tivimate.xml.gz
```

SKY TV app programme files:

```text
https://sky2135.github.io/skytv-epg/EPG/server_1_epg.json.gz
https://sky2135.github.io/skytv-epg/EPG/server_2_epg.json.gz
https://sky2135.github.io/skytv-epg/EPG/server_3_epg.json.gz
```

SKY TV app personalization files:

```text
https://sky2135.github.io/skytv-epg/EPG/server_1_metadata.json.gz
https://sky2135.github.io/skytv-epg/EPG/server_2_metadata.json.gz
https://sky2135.github.io/skytv-epg/EPG/server_3_metadata.json.gz
```

Useful indexes and reports:

```text
https://sky2135.github.io/skytv-epg/epg/index.json
https://sky2135.github.io/skytv-epg/EPG/index.json
https://sky2135.github.io/skytv-epg/EPG/taxonomy.json
https://sky2135.github.io/skytv-epg/reports/server_1/server_1_validation.json
https://sky2135.github.io/skytv-epg/reports/server_2/server_2_validation.json
https://sky2135.github.io/skytv-epg/reports/server_3/server_3_validation.json
```

A `.gz` link may download instead of opening in the browser. That is normal.

Use the matching lowercase `/epg/` XMLTV URL in TiviMate. Use the uppercase
`/EPG/` JSON and metadata URLs in the custom app.

These files supply the data needed for personalized groups, but they do not add
preference screens or filtering code to the custom app itself. That app change
must be made in the app's own source code, which is not part of this repository
or setup package.

## 16. Normal future operation

After the first setup, the system runs automatically:

1. Every day at 04:37 Toronto time, the main workflow checks all three servers.
2. New streams are appended to `Mappings` with `enabled=FALSE` and
   `action=REVIEW`. They stay private and excluded from every published file.
3. Existing mapping rows and your manual classifications are never overwritten.
4. The same run reads a temporary local snapshot of the private Sheet, builds
   and validates the guide, and deploys the XML and JSON outputs.
5. The standalone **1 - Sync channels to Google Sheet** workflow is manual-only.
   Use it for the initial setup or whenever you want an extra inventory check.
6. The existing live-sports workflow continues independently.

If a provider is temporarily unavailable during the daily run, Version 1 keeps
the last trusted rows in the Sheet. It does not erase channels because a server
failed to answer once.

Your regular task is:

1. Open the Google Sheet.
2. Filter `action` to `REVIEW`.
3. For each row you can verify, complete every approval check in Section 13 and
   set `enabled=TRUE` last. Leave uncertain rows disabled and in review.
4. Gradually review the unknown personalization metadata described in Section
   13, starting with the languages and interests your users select.
5. Open `Sync Alerts`, filter `status` to `OPEN`, and follow the alert-resolution
   steps in Section 13.
6. Run **2 - Build and publish EPG** manually when you want approved changes published
   immediately; otherwise the next daily build will publish them.

## 17. Safe recovery steps

### If the inventory workflow fails

1. Open the failed run in **Actions**.
2. Open the red job and read the first red error.
3. Use this checklist:
   - **401 / invalid credentials:** copy that server's exact username and
     password into the matching GitHub secrets. Ask the provider for long,
     unique values when that option is available.
   - **Connection failed:** check the server base URL and port.
   - **Too few channels:** wait and run the report-only check again. The first
     setup deliberately requires at least 4,000 Server 1 channels, 10,500
     Server 2 channels, and 9,400 Server 3 channels so a truncated provider
     response cannot become the new inventory. Do not lower these limits.
   - **Stream IDs overlap only ... / mass append blocked:** do not lower or
     bypass this protection. Confirm that each `SERVER_n_BASE_URL`,
     `SERVER_n_USERNAME`, and `SERVER_n_PASSWORD` secret belongs to the intended
     server account, then rerun in report-only mode. If the provider truly
     replaced all stream IDs, stop and reconcile the old and new IDs manually
     or seek technical help; never bulk-append the new namespace into the old
     Sheet.
   - **Credential-safety check:** do not bypass it. If no credential was
     intentionally placed in a mapping or report, a common provider password
     may have matched ordinary EPG text. Ask the provider to rotate that
     password to a longer, random value, update the matching GitHub secret, and
     rerun in report-only mode. The error deliberately does not print the
     credential.
   - **HTTP is blocked:** first ask the provider for HTTPS; only then set
     `ALLOW_INSECURE_PANEL_HTTP=true` if no HTTPS endpoint exists.
   - **Google 403 / permission denied:** confirm the Google Sheets API is
     enabled and the service-account email has Editor access to the Sheet.
   - **Wrong spreadsheet or tab:** check `GOOGLE_SHEET_ID` and confirm the tab
     names are exactly `Mappings` and `Sync Alerts`.
   - **Open Sync Alerts / quarantined channels:** fix and verify the matching
     `Mappings` row first, change the alert status from `OPEN` to `RESOLVED`,
     wait for **Saved to Drive**, and rerun the report-only check.
4. Run the inventory workflow again in report-only mode before enabling its
   Sheet update option.

### If the EPG build fails

1. Do not delete the Pages site. The last successful deployment remains live.
2. Open the failed run and read the first red error.
3. If the error says the Sheet cannot be read, confirm the Google Sheets API is
   enabled, `GOOGLE_SHEET_ID` is correct, and the service-account email still
   has Editor access.
4. If a recent Sheet edit caused the problem, open the Sheet and use
   **File** → **Version history** → **See version history** to restore the last
   working version.
5. Wait until the Sheet says **Saved to Drive**.
6. Manually run **2 - Build and publish EPG** again.

### If the service-account JSON key is exposed

1. In Google Cloud, open **IAM & Admin** → **Service Accounts**.
2. Open `SKY TV EPG Sheet Writer` → **Keys**.
3. Delete the exposed key.
4. Create a new JSON key.
5. Replace the GitHub secret `GOOGLE_SERVICE_ACCOUNT_JSON` with the new JSON
   file's complete contents.
6. Run the inventory workflow in report-only mode to confirm access.

### If an incorrect channel was appended

Do not delete it. In the Sheet, set:

```text
enabled = FALSE
action  = IGNORE
```

The inventory sync then recognizes the row as already known and will not add a
duplicate.

### If Google Sheets reports a Version 1 size limit

Version 1 stops before the active `Mappings` tab exceeds 150,000 data rows or
the active `Sync Alerts` tab exceeds 10,000 data rows or 8 MiB. The starter has
25,170 mapping rows, so this is not expected during initial setup.

For a `Sync Alerts` limit:

1. Resolve every alert that you can verify. Never archive an `OPEN` alert.
2. In the same private spreadsheet, add a tab named `Sync Alerts Archive`.
3. Copy the 11-column header and the oldest `RESOLVED` rows into that archive
   tab using **Paste special** → **Values only**.
4. Confirm the copied row count and values before continuing.
5. Delete only those copied `RESOLVED` rows from the active `Sync Alerts` tab.
6. Keep every `OPEN` row in the active tab.
7. Run **1 - Sync channels to Google Sheet** in report-only mode, then run it
   with Sheet updates only after the report-only run succeeds.

For a `Mappings` limit, do not delete or move rows. Removing an identity makes
the inventory see it as new and add it again. Instead:

1. In GitHub, open **Actions** → **2 - Build and publish EPG**.
2. Open the **...** menu and choose **Disable workflow**. The last successful
   Pages deployment stays live.
3. Request an updated Version 1 package with a larger mapping-capacity design.
   Do not change the limit yourself.
4. Re-enable the workflow only after installing and testing that update.

## 18. Final setup checklist

- [ ] Version 1 overlay committed to `main`
- [ ] `Mappings` workbook imported into Google Sheets
- [ ] Both `Mappings` and `Sync Alerts` tabs present with unchanged headers
- [ ] Google Sheets API enabled
- [ ] Service account created and JSON key saved as a GitHub secret
- [ ] Google Sheet shared with the service account as Editor
- [ ] Google Sheet kept private and shared only with the required accounts
- [ ] Required GitHub variables created
- [ ] All ten GitHub secrets created
- [ ] GitHub Pages source set to **GitHub Actions**
- [ ] Inventory report-only run completed successfully
- [ ] Inventory write run completed successfully
- [ ] New `REVIEW` rows checked; approved rows set to `enabled=TRUE`, and
      uncertain rows left disabled
- [ ] Important language, region, genre, sport, and religion metadata reviewed;
      uncertain values deliberately left unknown
- [ ] `OPEN` Sync Alerts reviewed and resolved, or deliberately left quarantined
- [ ] First EPG build and Pages deployment completed successfully
- [ ] `health.json` shows `status: ok`
- [ ] TiviMate and custom-app URLs tested

## Official references

- [Upload files to an existing GitHub repository](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)
- [Create GitHub Actions variables](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables)
- [Create GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
- [Run a workflow manually](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
- [Configure GitHub Pages to use GitHub Actions](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
- [GitHub Pages availability and usage limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- [Import an Excel workbook into Google Sheets](https://support.google.com/docs/answer/12236443)
- [Stop publishing a Google Sheet](https://support.google.com/docs/answer/183965)
- [Enable a Google API](https://docs.cloud.google.com/apis/docs/getting-started)
- [Create a Google service account](https://docs.cloud.google.com/iam/docs/service-accounts-create)
- [Create a JSON service-account key](https://docs.cloud.google.com/iam/docs/keys-create-delete)
- [Share a Google Sheet with an editor](https://support.google.com/docs/answer/2494822)
- [EPGShare file and channel-ID guide](https://epgshare01.online/epgshare01/0_READ_ME_FIRST_AS_I_CONTAIN_VERY_HELPFUL_INFO.html)
