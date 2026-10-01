# Autonomous guide coverage

Production schedule matching is unattended. The workflow evaluates every safe
channel identity and does not require an operator to work through `REVIEW` rows
one by one.

## Decision order

For each current `(server_id, stream_id)` identity, the workflow uses the first
terminal outcome that passes its gates:

1. **Deterministic EPGShare real schedule.** The exact catalog, market,
   protected-semantics, programme, provider, Sheet, and alert checks must pass.
2. **Verified native real schedule on Server 2 or Server 3.** The current panel
   ID, unique compatible display name, programme horizon, and terminal provider
   reread must pass. Server 1 never enters this lane.
3. **Google-Search-grounded Gemini real schedule.** Gemini receives only a
   bounded local candidate list and cannot create an EPG ID. `HIGH` is accepted
   only for the locally selected opaque candidate when one complete positive
   identity claim contains every discriminating provider token and is supported
   by at least two independent web authorities. All deterministic catalog,
   programme, provider, Sheet, alert, score, margin, and semantics gates still
   apply.
4. **Truthful local synthetic guide.** A safe unresolved row receives a unique
   per-stream schedule. AI abstention, invalid output, quota failure, or outage
   also reaches this lane. A generic channel uses its cleaned channel name;
   richer programme wording is used only when the row positively identifies a
   supported event, movie, artist, music, adult, or continuous-24/7 family.
5. **Ignore or quarantine.** Decorative non-channels become `IGNORE`. An `OPEN`
   identity alert, provider/Sheet drift, or an untracked manual target remains
   disabled and protected rather than being overwritten.

An existing `coverage-fallback-v1` synthetic row is an upgrade candidate on a
later run. It may move to a real schedule only after a real lane passes every
current gate. A verified real schedule is never replaced merely to increase a
coverage percentage.

Server 1 may use EPGShare for an external real schedule or a local synthetic
guide. Its provider-native XMLTV and panel IDs are forbidden. Servers 2 and 3
may additionally use a verified native panel schedule.

## Scheduled and manual controls

The scheduled Workflow 1 run uses `apply` across all servers. Its default total
REVIEW apply limit and synthetic proposal limit are both `30000`; repository
variables may set either limit to another allowed value, including `0` to
disable that class of scheduled writes.

When `GEMINI_API_KEY` exists, scheduled grounded review is enabled unless
`EPG_USE_GEMINI_AI=false`. Setting the variable to `false` is the explicit
kill switch. Manual dispatch remains conservative: no new-row write, `dry-run`,
Gemini off, and both write limits at `0` until the operator selects otherwise.

## Coverage reporting

Published indexes and per-server manifests contain `guideCoverage`. It reports
three denominators separately:

- every literal loaded Mapping row;
- channel rows after explicit `IGNORE` non-channels are removed; and
- actionable, runtime-eligible, non-quarantined rows.

The counts keep `verifiedEpgShareReal`, `nativePanelReal`, `localSynthetic`,
`ignoredNonChannel`, `quarantinedReview`, `disabledReview`, and other
`uncovered` rows separate. `realGuide` is the sum of the two external real
sources. `usefulGuide` is `realGuide + localSynthetic`. Reconciliation fields
prove that the mutually exclusive outcomes add back to their denominators.

Useful-guide coverage is not real-match accuracy. A local synthetic guide is
valuable because it avoids a blank grid without pretending that an external
schedule was verified.

## Final offline decision base

The **September 30, 2026** hash-bound run covers all 49,759 Mapping identities
and emits 16,990 safe offline patches. Its target-assignment reconciliation is:

| Decision bucket | Rows |
|---|---:|
| EPGShare real, retained plus newly verified | 10,526 |
| Retained native | 3,977 |
| Local synthetic, retained plus newly classified | 27,298 |
| Native revalidation required | 5,389 |
| OPEN-alert quarantine | 2,120 |
| Protected manual target | 449 |
| **Total** | **49,759** |

The offline assignment rate is 41,801 / 49,759, or 84.00%. This is deliberately
below 90 because an offline process without panel credentials cannot safely
claim that a native ID has a programme guide or a definitive miss. The
credentialed production verifier decides that cohort per row. At least 2,983
of the 5,389 native rows must resolve to cross 90%; resolving all of them yields
47,190 assigned rows, or 94.84%.

Assignment is not programme coverage or real-match accuracy. Production
manifests recompute actual source-separated `guideCoverage` from retained
programme rows after the live native stage and final build.
