# EPG automation operations

The daily **2 - Build and publish EPG** workflow runs at 04:37 in
`America/Toronto`. It discovers all available providers, matches new channels,
rechecks eligible REVIEW and authenticated fallback mappings, creates one
private snapshot, generates icons, builds guides, validates, and deploys.
GitHub scheduling is best effort. **1 - Sync channels to Google Sheet** remains
a manual preview/repair tool; its separate daily run has been removed.

## Matching and retry behavior

Deterministic EPGShare matching runs first. Verified native schedules are
available on Servers 2 and 3. Grounded Gemini verifies only supplied local
candidates and cannot invent IDs. Every current catalog, market, programme,
provider, Sheet, and OPEN-alert gate remains required.

An authenticated `coverage-fallback-v1` row is durable retry state. It can keep
its disclosed placeholder active while deterministic, native, and bounded AI
lanes seek a verified real schedule. The default AI window rotates by a full
budget each day, interleaving servers. AI abstention, quota failure, or deferral
does not remove a safe fallback from future attempts. A new channel must first
be appended to the private Sheet before the existing-row AI path can use it.

Bulk discovery retains percentage and absolute approval ceilings. Verified
proposals above the ceiling are appended disabled in REVIEW with provenance,
then reconsidered by later bounded rechecks. This does not clear alerts, alter
manual classifications, or permit mass identity changes.

Existing healthy native and manual mappings retain their source policy. This
change does not force them onto EPGShare. A channel without a corresponding
real EPGShare schedule can remain explicitly synthetic.

## Controls

Existing repository variables remain effective:

| Variable | Default | Meaning |
|---|---:|---|
| `EPG_REVIEW_APPLY_LIMIT` | `30000` | Total existing-row decisions across all lanes. Allowed: 0, 25, 100, 500, 2500, 5000, 30000. |
| `EPG_COVERAGE_FALLBACK_LIMIT` | `30000` | Synthetic proposals. Allowed: 0, 500, 2500, 5000, 30000. |
| `EPG_USE_GEMINI_AI` | Automatic | Gemini runs when its key exists unless explicitly disabled with `false`. At most 200 rows, also bounded by remaining apply capacity. |
| `EPG_MAX_PUBLICATION_AGE_HOURS` | `36` | Maximum publication age accepted by the independent public health check. |
| `EPG_MINIMUM_FUTURE_HOURS` | `6` | Minimum real future programme endpoint required by the public health check. |

Zero disables the corresponding matching write/proposal lane. The shared limit
validator is used by both the publisher and manual sync, including `30000`
fallback support. Manual sync retains its conservative dispatch defaults.

## Efficient source processing

The daily run downloads one EPGShare XML and one text catalog. The first XML
traversal produces the sealed SQLite programme selection spool and a complete
icon/display-name sidecar, including unselected IDs needed for ambiguity
checks. Icon generation verifies the sidecar against the compressed source and
reuses it without a second XML traversal. The builder reuses the sealed spool.
Both sidecars remain temporary runner files.

## Health and coverage

`guideCoverage` preserves separate EPGShare, native, and synthetic counts.
`futureProgrammeCoverage` additionally reports source-separated future streams,
streams reaching six hours, and latest programme stops. Useful coverage still
includes placeholders; it is not a real EPGShare match rate or accuracy score.

Health includes generation time, a default 36-hour validity deadline, and
provider availability. `degraded` means discovery or a native source was
unavailable when the build ran. Only fixed server identifiers and counts are
published; private provider diagnostics stay private. Healthy unaffected guides
can still deploy, with the degraded state disclosed.

**Check published EPG freshness** runs independently every four hours, without
Google or provider credentials and without the Sheet writer lock. It checks
both publication age and real future endpoints against the current clock.
Synthetic guide horizons cannot satisfy the real-source check. Missing,
malformed, mixed, stale, or degraded publication data produces a failed check
and an aggregate Actions report. GitHub's normal workflow notification settings
control delivery; no extra messaging service is configured.

An old `status: ok` alone does not prove current health. After a failed build,
inspect this independent check and the failed production step. Keep the last
validated publication while the cause is repaired; do not lower identity,
catalog, or programme gates merely to make the workflow green.

## Change validation

The credential-free CI workflow runs the offline test suite on Ubuntu/Python
3.12 for relevant pull requests and changes to main. Production retains its
regression, secret scanning, source-binding, snapshot, and output validation.
All protected script and workflow edits must update `MATCHER_INTEGRITY.json`.
