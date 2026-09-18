# Smart guide coverage design

## Status and objective

This document is a proposed production policy, not current write authority.
The existing Matching Lab remains a shadow/proposal process, and its guarded
writer still accepts only exact owner-approved rows. Implement and calibrate
this design in shadow mode before granting any tier automatic authority.

The objective is to give at least 99% of actionable channels a useful guide
without claiming that 99% have a genuine one-to-one EPGShare schedule. The
system uses two honest outcomes:

1. a verified real schedule when catalog, identity, and programme evidence all
   agree; or
2. a channel-specific synthetic guide when a stable real schedule cannot be
   proven.

A wrong real schedule is worse than an accurate synthetic description. AI may
review a bounded local shortlist, but it cannot invent an EPG ID or override a
hard conflict.

## Audit baseline

The September 18, 2026 00:41 UTC shadow bundle contained 23,381 disabled
`REVIEW` rows:

| Classification | Rows |
|---|---:|
| No candidate | 6,656 |
| Low-score abstention | 5,695 |
| Prefilled EPG ID skipped by current Lab | 6,251 |
| Protected-semantics conflict | 1,008 |
| Low-confidence review candidate | 971 |
| Other review candidate, including 721 strong proposals | 1,841 |
| Candidate failed the programme gate | 395 |
| Blocked by an OPEN alert | 553 |
| Unsupported channel class | 11 |
| **Total** | **23,381** |

Only 7,487 rows, or 32.02% of the backlog, had any compatible top candidate
with a passing programme guide. Even assuming every one of the 6,251 prefilled
IDs could be trusted would put the uncalibrated real-guide ceiling at 58.76%.
Threshold relaxation therefore cannot deliver 99% real-guide coverage.

The complete Mapping snapshot had 49,759 rows and 26,378 active rows. Giving
every non-alert backlog row either a verified real guide or a personalized
synthetic guide produces 49,206 covered rows, or 98.89% of the complete table.
It is 100% of the 49,206 actionable, non-quarantined rows. Resolving or safely
classifying at least 56 of the 553 alert rows crosses 99% of the literal table.

These counts describe one frozen audit. The newer September 18 XML has the
same 27,128 exact IDs as its official TXT catalog but different source bytes
and programme evidence. Recompute all decisions and coverage counts from the
new paired snapshot before any canary.

### Fresh September 18 v2 verification

The completed version-2 shadow run used the fresh archive captured for
September 18 at 09:17 EDT, the matching official TXT catalog, and the current
Mapping and Sync Alerts snapshots. Its bundle passed an independent replay
validation against both CSV snapshots. The 23,381 rows were classified as:

| Version-2 state | Rows |
|---|---:|
| Shadow real-match evidence (`AUTO_ELIGIBLE`; still no write authority) | 460 |
| Human review | 3,526 |
| Programme verification failed | 753 |
| Protected-semantics or row conflict | 1,380 |
| No safe candidate / low evidence | 16,709 |
| Existing OPEN alert | 553 |
| **Total** | **23,381** |

Of the 460 shadow-eligible real matches, 459 passed the standard non-sensitive
lane and one passed the conservative unique strict-exact lane. The lower count
than the raw 765-row threshold estimate is intentional: explicit language,
number, Plus, direction, timeshift, edition, and South-Asian risk gates removed
otherwise plausible fuzzy rows. All `auto_apply_eligible` values remain false.

The run also re-examined prefilled REVIEW targets without ranking bias. Across
the complete Mapping snapshot, all 6,305 such rows had known system/native
provenance: 5,585 protected Server 2/3 native candidates, 549 strict current
`auto-map-v1` candidates, and 171 exact Server 1 migration candidates. Untracked
operator choices remain protected. The local fallback may replace only the
last two machine-generated groups after normal validation fails, and it stores
an exact reversible prior-target record.

## Decision lanes

Every lane first passes the common gates in the next section. Scores are
integer parts per million internally; decimal values below are for readability.
They are evidence scores, not probabilities.

| Lane | Required local evidence | Intended result | Audit yield |
|---|---|---|---:|
| Trusted exact | Human/curated/strict exact identity; or relaxed/compact/token-bag exact with score >= 0.60 and post-family margin >= 0.15 | Real EPG | 76 of 104 clean structural rows met the numeric gate |
| Standard automatic | Non-South-Asian, non-variant row; score >= 0.80 and margin >= 0.10 | Real EPG after calibration | 765 rows, 764 non-South-Asian |
| Standard AI-assisted | Score >= 0.75 and margin >= 0.15; both fixed local rankers and AI `HIGH` choose the same opaque candidate; target stable across two source snapshots | Real EPG after calibration | Upper bound 849 rows, 844 non-South-Asian |
| Conservative language/variant | Curated, human, or strict exact identity; optional fuzzy candidate requires score >= 0.90 and margin >= 0.20 plus exact protected-semantic agreement | Real EPG only with unusually strong evidence | No meaningful new South-Asian fuzzy yield in the audited set |
| Personalized synthetic | No candidate, programme failure, dynamic event, continuous channel, unsafe ambiguity, or score below a real-guide lane | Honest channel-derived XMLTV | Covers the safe remainder |

The currently approved strong gate, score >= 0.82 and margin >= 0.08, selected
721 rows: 720 non-South-Asian and one South-Asian row. The proposed standard
automatic gate adds only 44 rows before the trusted-exact union. Combining the
standard gate with the proposed structural-exact rule yields 810 rows in the
audited snapshot.

Do not lower a real-guide gate based on score alone. Below 0.80 the audit found
plausible-looking errors such as `RTE 2` selecting `RTE 2FM`, `DAZN F1`
selecting `DAZN 1`, and `Polsat X` selecting generic `Polsat HD`. The context
model must add TV-versus-radio, locality, sport, and edition evidence before
these cases can enter an automatic lane.

### Cohort rules

Use the conservative lane when any of these is true:

- the route is India, Pakistan, Nepal, Sri Lanka, Bangladesh, or an unresolved
  South-Asian diaspora route;
- the name/category explicitly identifies a language feed;
- the identity carries a channel number, Plus, Extra, Alternate, East/West,
  timeshift, locality, league, or other edition-bearing qualifier; or
- candidate and provider metadata disagree on TV/radio medium, language,
  content family, or locality.

Unknown metadata must not be interpreted as English or as evidence of
agreement. In the audited backlog, 93.86% of rows had `primary_language=und`,
82.73% had blank country codes, and 61.60% had an unknown region. Market route
and parsed identity evidence are more reliable than those incomplete fields.

## Common hard gates

No real automatic match may proceed when any of these is present:

- OPEN Sync Alert, provider identity drift, Mapping row drift, or stale run;
- XML/TXT source mismatch, stale catalog, case-colliding ID, dummy ID, or an ID
  missing from either exact catalog;
- `ALL`, unknown, ambiguous, or diaspora route without one explicit target
  market;
- adult, PPV/event, continuous/24x7, decorative heading, generic numbered
  slot, or other class intended for a synthetic guide;
- fewer than two meaningful identity tokens, except a validated station call
  sign;
- market, language, content family, number, Plus, Extra, Alternate,
  direction, timeshift, locality, league, or TV/radio contradiction;
- unresolved same-market candidates after equivalent schedule families are
  collapsed;
- fewer than two informative programmes, first useful programme outside the
  near-term window, or less than six hours of useful future coverage;
- AI disagreement, invalid response, weak confidence, unavailable replay
  evidence, or an AI choice outside the local shortlist; or
- an active customer-report quarantine or negative alias lock.

HD, SD, and UHD IDs may form one ambiguity family only when current schedule
fingerprints contain at least four informative programmes and are at least
0.95 similar with corresponding starts within five minutes. Name similarity
alone cannot establish schedule equivalence.

## Prefilled-ID correction

The prior version-1 Lab rejected every disabled `REVIEW` row with an `epg_id` as
`MANUAL_CANDIDATE_PRESENT` and did not rank it. That is not a trust signal.
There were 6,305 such rows in the Mapping snapshot: 5,755 used panel source and
496 used EPGShare source. They contained 5,575 distinct strings, but only 379
distinct IDs existed exactly in the frozen official catalog. Only 618 rows had
an exact catalog ID, and only 443 passed the current programme gate.

Version 2 replaces that blanket conflict with this flow:

1. Treat the prefilled value as untrusted evidence.
2. If it is an exact, case-unique XML/TXT ID, revalidate route, semantics,
   programme coverage, and bound provenance.
3. Rank the row normally. A prefilled ID may be retained only when it is the
   independently selected candidate or carries valid human/bound provenance.
4. If the value is absent or fails validation, do not preserve its bias;
   continue normal retrieval and then use a synthetic guide if no lane passes.
5. Never turn a panel ID into an EPGShare ID merely because the strings look
   alike.

## Personalized synthetic guides

The existing 12,879 active dummy mappings show the scale and main templates:

| Family | Active rows | Synthetic presentation |
|---|---:|---|
| PPV events | 6,618 | Parsed event name, participants, date, and Eastern Time |
| Movie channels | 2,040 | Language/genre-aware title such as `Hindi Action & Adventure Movies` |
| Continuous 24x7 | 1,673 | Clean channel-derived title with `24/7` subtitle |
| Flo events | 866 | Parsed event or `No event currently scheduled` |
| ESPN+ | 586 | Parsed event and Eastern Time; every audited name contained a date/time |
| Blank/general fallback | 573 | Clean channel name, never generic `24/7 Programming` when identity is usable |
| Adult | 344 | Bounded neutral adult-programming label |
| Music Choice | 132 | Genre or station name plus `Music 24/7` |
| Triller | 37 | Parsed event or upcoming-event placeholder |
| Sports fallback | 10 | Sport/channel-derived continuous label |

Singer groups should use the parsed performer, for example `Akhil - Songs
24/7`, rather than a generic title. Movie groups should retain meaningful
language and genre but remove server decorations, quality markers, and slot
numbers when the number is not content identity.

Use long, deterministic blocks to control output size: normally 12 or 24 hours
for continuous channels and one event-specific window plus bounded pre/post
blocks for PPV. Preserve the original name separately for traceability. Parse
dates with an explicit timezone and display event time as `ET`, using
`America/New_York` rules so EDT/EST daylight transitions are correct. A
malformed or missing date must fall back to an honest upcoming/no-event title,
never a guessed start time.

## Event-slot alerts

The audited Sync Alerts file contained 3,437 OPEN possible-reuse records but
only 2,120 unique keys; 1,317 were repeated alerts. In 98.84% of those records
the category stayed the same while the provider-updated channel name changed.
Of the 553 blocked backlog proposals, 497 were ESPN+ slots and another 56 were
other live-event groups.

For a proven provider-controlled event bank, define stable identity as
`(server_id, stream_id, category_id, event-role)`. Treat the changing channel
name as versioned event payload used by the synthetic guide. Deduplicate OPEN
alerts by stable key and current identity revision. This exception must be
restricted to positively classified event banks; a renamed ordinary linear
channel remains a possible stream-ID-reuse alert and stays quarantined.

## Customer feedback contract

The app should report a mapping revision, not write a correction. Publish an
opaque, non-reversible report token with each channel's app metadata. Do not
expose raw provider URLs, credentials, or private stream identifiers.

Suggested authenticated request:

```http
POST /v1/epg-feedback
Idempotency-Key: <client-generated UUID>
Content-Type: application/json

{
  "report_token": "<opaque signed token>",
  "mapping_revision": "<sha256>",
  "programme_start": "2026-09-18T01:00:00Z",
  "reason": "wrong_channel",
  "comment": "optional bounded text"
}
```

Allowed reasons should be a short enum such as `wrong_channel`, `wrong_time`,
`wrong_title`, `missing_programme`, and `other`. Validate token, revision,
timestamp, authentication, size, and idempotency; rate-limit by account and
installation. Store only the minimum data needed to investigate abuse and
quality.

Feedback actions:

- one independent report places the mapping on a watch list;
- two distinct customer accounts within 24 hours, or three within seven days,
  roll the real match back to its personalized synthetic guide and open a
  `CUSTOMER_REPORTED` quarantine;
- reports never choose a replacement EPG ID and never teach alias memory;
- an admin-confirmed correction may teach after exact current catalog and
  programme validation; and
- an automatic match may teach only after seven complaint-free days, the same
  target on two fresh source snapshots, and the identical bound target on at
  least two unchanged servers.

Keep a negative alias lock until an admin resolves the incident. Retain the
previous Mapping preimage so every rollback is exact and auditable.

## Staged rollout

1. Rerun the complete shadow analysis on the fresh paired XML/TXT snapshot.
2. Add the missing protected semantics and the prefilled-ID revalidation lane;
   freeze boundary and adversarial tests.
3. Deploy personalized synthetic titles first because they improve the guide
   without changing real schedule identity.
4. Correct event-slot identity handling and deduplicate existing alerts. Verify
   that ordinary linear renames still quarantine.
5. Label a representative corpus by market and risk cohort. Require measured
   precision of at least 99% before enabling a real automatic tier.
6. Apply 25 trusted/current approvals under the existing shared lock. Stop on
   any confirmed wrong mapping, unexpected complaint cluster, drift, or failed
   postread.
7. Expand in bounded stages: 100 rows, 500 rows, 10% of eligible rows, then the
   remainder. Hold each stage through at least one fresh EPG snapshot.
8. Enable the standard automatic lane before the AI-assisted lane. South-Asian
   and explicit-variant fuzzy rows remain synthetic until separately
   calibrated.
9. Let complaint-free, cross-server, cross-snapshot evidence improve future
   exact alias coverage; never train directly from an unverified complaint.

Track real/synthetic coverage separately, plus confirmed-wrong rate, complaint
rate, rollback time, alert age, programme-gate failures, per-market precision,
and the number of synthetic guides later upgraded to verified real schedules.
The 99% objective is useful-guide coverage; the dashboard must never relabel it
as real-match accuracy.

## Implementation boundaries

The repository now implements the version-2 shadow lane, channel-derived
synthetic builder, bounded coverage fallback, and strict event-slot identity
handling without changing the frozen v7/v8 matcher sources. The customer
feedback endpoint and UI remain application work. The maintained touch points
are:

- `matching_lab/policy.py`, `models.py`, `pipeline.py`, `normalization.py`,
  `retrieval.py`, and `validation.py` for lane evidence and replay validation;
- `scripts/ai_review_policy.py` for same-candidate advisory rules;
- `scripts/sync_channel_inventory.py` for event-slot identity and alert
  deduplication;
- the guarded apply lane for signed policy canaries, exact rollback state, and
  customer quarantine checks; and
- the guide/event builder for personalized synthetic programme generation.

Every policy boundary needs tests at one unit below, exactly at, and one unit
above its threshold. Required regressions include South-Asian fuzzy abstention,
all protected variants, TV versus radio, `RTE 2`/`RTE 2FM`, `DAZN F1`/`DAZN 1`,
prefilled valid and invalid IDs, event name churn versus linear rename, alert
deduplication, malformed event names, EDT/EST transitions, AI attempts to
invent or override, stale source/row guards, complaint rollback, and feedback
that cannot poison learned aliases.
