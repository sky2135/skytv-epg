# Smart Coverage Fallback

Smart Coverage Fallback is an opt-in last stage for channels that do not have
a verified real schedule. It enables a local, per-stream synthetic guide rather
than guessing an EPGShare channel.

The decision order is:

1. Apply a deterministic real EPG match only after the existing catalog and
   programme gates pass.
2. Leave a bounded Smart/Gemini shortlist available for real-match review.
3. For the remaining eligible rows, use a local channel-derived guide.

## Safety boundary

The feature is off by default. It runs only after the ordinary deterministic
real-match and programme gates. A row must still be a disabled `REVIEW` row,
must still exist under the same provider identity, and must not be an OPEN-alert
or quarantined row, a decorative heading, or an explicit `IGNORE` result.

The original target must be one of these narrowly identifiable cases:

- a blank `epg_id`;
- a provisional target carrying the complete, current-build `auto-map-v1`
  provenance prefix, including the exact matcher and engine hashes and matching
  catalog/source hashes; or
- one of the two exact legacy Server 1 migration records.

An untracked or manually entered target is never replaced. Current Server 2/3
panel candidates are reserved for native schedule validation; when a current
native ID passes that gate, `KEEP_PANEL` wins over a synthetic proposal. Adult-
labelled streams remain barred from guessed real schedules; if otherwise
eligible, they receive an explicitly synthetic adult guide instead.

Synthetic mappings are written as:

- `action=AUTO_DUMMY`
- `source=dummy`
- `epg_feed=DUMMY_CHANNELS`
- `epg_id=Synthetic.<family>.local`
- a hash-bound `coverage-fallback-v1` note

Before replacement, the exact prior `source`, `epg_feed`, and `epg_id` are
length-encoded in the bounded `reason` cell as a
`coverage-fallback-rollback-v1` record. The notes also contain a SHA-256 of that
prior target. This makes a machine-prefilled replacement exactly reversible,
while the hash and fallback binding make accidental audit drift detectable.

The builder gives each server/stream its own deterministic schedule identity,
so two channels never share programme text merely because their marker family
is the same. Local synthetic rows do not request EPGShare dummy programmes.

## Run limits

`--coverage-fallback-limit COUNT` proposes fallback for at most `COUNT` rows in one run.
Allowed production values are `0`, `500`, `2500`, and `5000`; `0` is off. New
and existing-`REVIEW` candidates share this proposal cap fairly: the two lanes
alternate after each has been rotated and interleaved across servers. When new
channel appends are disabled, new rows are excluded from fallback selection so
they cannot consume capacity that can never be written. The existing Google
writer still revalidates provider identity and OPEN-alert state immediately
before each bounded update batch.

This proposal cap is not authority to edit an existing row. Existing
`REVIEW` persistence also requires `--review-apply-limit`, whose allowed values
are `0`, `25`, `100`, `500`, `2500`, and `5000`. Its default `0` forbids every
existing-`REVIEW` write, and its one total covers real, native, synthetic,
`IGNORE`, and AI lanes together.

For a manual pilot, run workflow **1 - Sync channels to Google Sheet**, select
`dry-run`, and choose `500`. Inspect the aggregate workflow summary, then run
`apply` with a `review_apply_limit` of `25` for the first canary. To enable
recurring proposals, set repository variable `EPG_COVERAGE_FALLBACK_LIMIT` to
an allowed value; set `EPG_REVIEW_APPLY_LIMIT` separately to authorize a
bounded scheduled existing-row rollout. Workflow 1 reads both rollout
variables. Workflow 2 reads `EPG_COVERAGE_FALLBACK_LIMIT` when building the
published guide. Removing the coverage variable or setting it to `0` disables
new synthetic approvals; removing the apply variable or setting it to `0`
forbids scheduled existing-`REVIEW` writes and leaves no capacity for Gemini
review.

## Metrics

The private sync summary reports:

- `coverage_fallback_candidate_rows`
- `coverage_fallback_applied_rows`
- `coverage_fallback_deferred_rows`
- `coverage_fallback_ai_deferred_rows`
- `coverage_fallback_suppressed_unwritten_new_rows`
- separate new-channel and REVIEW-backlog counts
- machine-prefilled candidate/applied counts
- exact legacy Server 1 candidate/applied counts
- protected manual and protected native counts
- `coverage_fallback_replaced_by_native_rows`

This mode is intended to raise useful guide coverage, not to claim 99% real-EPG
accuracy. A customer “wrong guide” report should quarantine the stream before a
later real remap; it should never directly teach or write a mapping without the
normal identity and schedule validation gates.
