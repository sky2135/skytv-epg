# Repair the current Version 1 setup

Use this short procedure if the run ended with:

```text
Google Sheets did not durably store every new OPEN stream-ID reuse alert
```

The failed run did not add mapping rows or alerts. Keep the existing private
Google Sheet, repository, `main` branch, and Pages setup.

## What the attached error report confirms

- The option was **on**: `sheet_mode` is `write`.
- The report was created at **1:31:51 PM Toronto time on September 15, 2026**
  (`2026-09-15T17:31:51Z`).
- It found 24,588 new streams and 1,499 possible stream-ID reuse alerts.
- It durably added **zero** alerts and **zero** mapping rows. The safety lock
  correctly blocked the mapping append and build snapshot after Google rejected
  the first alert append.
- `safe_to_append=true` means the provider identity check passed. It does not
  mean Google stored anything.
- This report came from an older workflow revision: it does not contain the
  Version 1 `auto_match_*` result fields that the repaired workflow always
  writes. Installing the new `repo-overlay` is therefore required before the
  next test.

## Install the repaired files

1. Download and extract the repaired Version 1 ZIP.
2. Open the existing `sky2135/skytv-epg` repository on GitHub.
3. Confirm the selected branch is `main`.
4. Click **Add file** → **Upload files**.
5. Drag everything **inside** the ZIP's `repo-overlay` folder into the upload
   page. Include the hidden `.github` folder.
6. Use commit message `Repair SKY TV EPG Version 1` and commit to `main`.
7. Do not create a new repository or `gh-pages` branch.
8. Wait until GitHub shows that commit on the `main` branch before continuing.

## Confirm Google access

1. Open the existing mapping Sheet.
2. Click **Share**.
3. Open the same service-account JSON key whose complete contents were saved in
   the GitHub secret `GOOGLE_SERVICE_ACCOUNT_JSON`. Find and copy only its
   `client_email` value.
4. Confirm that **this exact email** is listed as **Editor** in the Sheet.
5. In Google Sheets, open **Data** → **Protect sheets and ranges**. If either
   `Mappings` or `Sync Alerts` is protected, allow that same service-account
   email to edit it.
6. Keep the Sheet **Restricted**.
7. Do not reimport the workbook and do not delete either tab.

## Test, then write

1. In **Actions**, wait until no Version 1 workflow is queued or running.
   Do not edit `Mappings` or `Sync Alerts` while either workflow is running.
2. Open **1 - Sync channels to Google Sheet**.
3. Run it on `main` with **Add missing channels to Google Sheet** turned
   **off**.
4. Open the new run and confirm it shows the repaired commit named
   `Repair SKY TV EPG Version 1`.
5. Wait for a green check mark.
6. Its summary should say `dry-run`. **Rows added = 0** and **Alerts added = 0**
   are correct for this test; the automatic-match and review counts are previews.
7. This green check proves the read/calculation path works; the next run is the
   actual Editor/write test. Do not continue if this run is red.
8. Run the same workflow again with **Add missing channels to Google Sheet**
   turned **on**.
9. Wait for a green check mark, then reload the Sheet.
10. In the run summary, check **Rows added**, **Alerts added**, **Open alerts**,
   automatically matched channels, and channels left for review.
11. If the providers have not changed since the attached report, expect about
   24,588 mapping rows added and 1,499 alerts added/open. The exact live counts
   may change. Automatic matches plus review rows must equal the new-channel
   count.
12. Confirm the `Mappings` and `Sync Alerts` row counts increased. A successful
   rerun after rows were already added can correctly report zero newly added.
13. Run **2 - Build and publish EPG** on `main`.

Workflow 2 keeps every unresolved `OPEN` identity quarantined and excluded. You
normally do not need to resolve every alert before building unaffected channels.
If the build reports a **runnable-row truncation guard**, the quarantine reduced
one server below its safety floor. Do not lower that floor. Resolve enough of
that server's alerts using the Sheet instructions, or provide the exact error
for review, and then run Workflow 2 again.

The 742 historical rows not seen at the providers are retained rather than
deleted. That is why, if the source is unchanged, the Sheet will have 49,758
mapping data rows even though the live provider inventory contained 49,016.

If the write run says that the automatic-approval batch exceeded its Version 1
safety limit, do not bypass the limit. Nothing has been written yet. Copy that
exact error and provide it for a one-time review of the unusually large batch.

The write-enabled run stores new reuse alerts first. It adds new mapping rows
only after an authoritative re-read confirms those alerts are durable. A new
channel is enabled automatically only when one exact EPGShare identity and its
useful near-term programme guide pass every safety check. The guide must start
within six hours, contain at least two useful time slots, and extend at least
six hours ahead. Uncertain rows remain disabled with `action=REVIEW`. Server 1
always uses EPGShare.

## If Google still rejects the write

- **HTTP 403:** recheck the exact service-account email, Editor access, and
  protected ranges. Do not change the table format.
- **HTTP 429:** wait a few minutes, then safely rerun it.
- **HTTP 500–599:** Google had a temporary failure; safely rerun it.
- **HTTP 400 table/layout rejection only:** use the table fallback below.

For the HTTP 400 table/layout fallback:

1. In `Sync Alerts`, click any table cell.
2. Click the dropdown beside the table name `SyncAlertsTable` above the header.
3. Choose **Revert to unformatted data** and confirm.
4. Wait for **Saved to Drive**, then repeat the off/on test sequence. Do not
   change `Mappings` unless a later HTTP 400 error specifically names it.
5. Only if a later error names `Mappings`, repeat there for `MappingsTable`.
6. If that option is absent, the tab is already a classic range; do nothing to
   it.
7. Reverting retains the cell data but removes the native table's styling and
   table features. Never choose **Delete table**—that removes its data. Never
   delete rows/tabs, clear formatting, or reimport the workbook.

For a completely new installation, follow
[`START_HERE_VERSION_1.md`](START_HERE_VERSION_1.md) from Section 1.
