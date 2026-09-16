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

## If the next run reports an XML/text catalog mismatch

The message below comes from an older Version 1 revision that required the two
separately published EPGShare files to change at exactly the same instant:

```text
ERROR: The XML and official text catalogs do not declare the same exact IDs.
```

Install the current Version 1 `repo-overlay`; do not change the Google Sheet or
server credentials for this error. The current workflow safely tolerates only
a small EPGShare publication rollover. The XML catalog, text catalog, and their
exact case-sensitive intersection must each contain at least 25,000 IDs. The
total XML-only plus text-only difference must be no more than 64 IDs **and** no
more than 0.25% of the union. Only shared IDs can be approved automatically;
one-file-only IDs are quarantined in `REVIEW`. A larger difference, or a
differing ID containing non-ASCII, whitespace, or control characters, still
stops the run safely before a Sheet write. Every XML-only and text-only ID must
also resolve to one deterministic country/market. An `ALL`/unknown or otherwise
unresolved route stops the run because an unroutable shadow cannot reliably
block a false unique match.

The current workflow also stops the whole unattended-matching preflight if the
official text catalog declares one ID in both real and dummy sections, gives a
real ID country evidence that contradicts its suffix, or places it in multiple
distinct country markets. These source-wide contradictions are not appended as
ordinary review rows, because silently omitting an ID could make a similar
channel appear falsely unique. By contrast, case- or Unicode-whitespace
normalization-confusable IDs remain conservative runtime competitors: they may
block automatic approval and leave an affected new channel in `REVIEW`, but
they can never be approved automatically themselves. One conservative blocker
may represent IDs that collapse to the same engine-safe identity after
casefold/Unicode-whitespace cleanup only when every member shares the same
catalog kind (all real or all dummy) and identical exact route—the same feed
and market. If that family mixes real/dummy status or differs by feed or market,
the whole preflight stops instead. Separately, a real candidate with the
unscoped `ALL` route may still serve an exact existing mapping. If it shares a
strong exact/station identity with a market-routed new proposal, that proposal
stays disabled in `REVIEW` rather than being approved as falsely unique.

These lines are unrelated to the catalog mismatch:

```text
SERVER_3_PASSWORD: ***
Warning: Provider credentials may be sent over unencrypted HTTP.
```

The asterisks are GitHub's secret masking, not a rejected password. The HTTP
warning means at least one provider URL uses unencrypted `http://`; it does not
cause XML/text catalog comparison to fail. A credential problem is reported
separately as HTTP 401 or invalid credentials. Ask the provider for HTTPS when
available, but do not rotate a password merely to fix this catalog error.

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
    automatically matched channels, channels left for review, **EPG catalog
    alignment mode**, and the shared/XML-only/text-only EPG ID counts.
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

For a healthy source, **EPG catalog alignment mode** is `exact` or
`bounded-drift`. In `bounded-drift` mode, XML-only and text-only counts are the
quarantined rollover IDs; they are not automatic matches. The downloaded
`summary.json` records the same evidence as
`epgshare_catalog_corroboration_mode`,
`epgshare_shared_catalog_channels`,
`epgshare_xml_only_catalog_channels`,
`epgshare_text_only_catalog_channels`,
`epgshare_catalog_drift_channels`, and a non-identifying
`epgshare_catalog_drift_sha256` fingerprint.

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
