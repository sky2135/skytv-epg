# Smart Coverage Fallback

Smart Coverage Fallback is the terminal safe lane for a channel that does not
have a verified real schedule. It enables a local, per-stream synthetic guide
instead of guessing an EPGShare or panel channel.

The production order is:

1. deterministic real EPGShare match after every local gate;
2. exact verified native schedule on Server 2 or Server 3;
3. Google-Search-grounded Gemini verification of an already discovered real
   candidate;
4. truthful local synthetic guide for the safe unresolved remainder; and
5. `IGNORE` for a decorative non-channel or quarantine for a risky identity.

Server 1 can use EPGShare for an external real schedule or the local synthetic
lane. Its provider-native XMLTV and panel IDs are forbidden.

## Safety boundary

A fallback candidate must still exist under the same current provider identity
and must not have an `OPEN` alert, provider/Sheet drift, or an untracked manual
target. Those rows remain disabled and protected. An explicit decorative
heading becomes `IGNORE` and does not receive a programme grid.

The fallback also handles a safe row when real matching has no candidate, a
candidate fails its programme or semantic gate, or grounded AI abstains, fails,
or is unavailable. An AI error is not permission to guess a real schedule.

Synthetic mappings are written as:

- `action=AUTO_DUMMY`
- `source=dummy`
- `epg_feed=DUMMY_CHANNELS`
- `epg_id=Synthetic.<family>.local`
- a hash-bound `coverage-fallback-v1` note

Before a machine-prefilled target is replaced, its exact `source`, `epg_feed`,
and `epg_id` are length-encoded in a bounded
`coverage-fallback-rollback-v1` record. The notes contain a SHA-256 of that
prior target. The change is therefore reversible and detectable if its audit
binding drifts.

The builder gives every server/stream pair its own deterministic schedule
identity. It uses richer wording only when positive row evidence identifies a
supported event, movie, artist, music, adult, or continuous-24/7 family. Every
other fallback says:

```text
Schedule unavailable — <channel>
```

This wording avoids inventing programme details. Local synthetic rows do not
request EPGShare dummy programmes.

## Real-schedule upgrades

An enabled row carrying valid `coverage-fallback-v1` provenance remains an
automatic upgrade candidate. Each later run may replace it with a real
EPGShare or Server 2/3 native schedule only when that real lane passes every
current gate. Untracked manual targets remain protected, and a verified real
mapping is never downgraded merely to improve a coverage statistic.

## Run limits

`--coverage-fallback-limit COUNT` bounds local synthetic proposals. Production
values are `0`, `500`, `2500`, `5000`, and `30000`. Existing-row persistence is
also bounded by `--review-apply-limit`, whose values are `0`, `25`, `100`,
`500`, `2500`, `5000`, and `30000`. The total apply limit covers real, native,
synthetic, `IGNORE`, and grounded-AI decisions together. Google writes remain
split into batches of at most 500 rows and 2 MiB.

Scheduled Workflow 1 defaults both limits to `30000`, allowing the dated
backlog to drain without row-by-row human matching. Either repository variable
may override its scheduled limit, and `0` is the kill switch. Manual dispatch
retains zero defaults and requires an explicit mode and limits.

## Metrics

The private sync summary retains candidate, selected, persisted, deferred,
native-replacement, and protection counters. Public indexes and per-server
manifests additionally expose `guideCoverage`, which keeps these outcomes
separate:

- verified EPGShare real schedules;
- native panel real schedules;
- local synthetic guides, including their disclosed title class;
- ignored non-channels;
- quarantined and disabled review rows; and
- other uncovered rows.

It reports percentages against literal loaded rows, channel rows excluding
`IGNORE`, and actionable non-quarantined rows. Useful coverage is real plus
local synthetic coverage; it is never described as real-EPG accuracy.

The final September 30, 2026 offline decision base has 49,759 rows and 41,801
assigned targets (84.00%): 10,526 EPGShare real, 3,977 retained native, and
27,298 local synthetic. It leaves 5,389 native candidates for credentialed
per-row validation, 2,120 OPEN-alert rows quarantined, and 449 manual targets
protected. At least 2,983 native candidates must resolve to reach 90%; all
5,389 resolving would produce 47,190 assignments (94.84%). Assignment remains
distinct from actual programme coverage, which only production
`guideCoverage` may report.
