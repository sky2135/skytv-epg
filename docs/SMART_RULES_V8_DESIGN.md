# Smart Rules v8 design

## Objective

Smart Rules v8 must understand naming structure rather than depend on an
increasing list of full provider-name patches. The design keeps the tested v7
engine intact, places a contextual resolver in front of it and makes every
automatic decision explainable.

## 1. Parsed channel model

Every provider row becomes a `ChannelContextV8` with separate fields:

- `original_name` — untouched provider text used by safety/event rules;
- `core_name` — brand identity after structural wrapper removal;
- `strict_key` — normalized identity with technical tokens removed;
- `relaxed_key` — strict identity with optional words such as TV/network
  removed;
- `compact_key` — spacing/punctuation-insensitive identity;
- `edition_key` — identity without directional/plus/alternate markers;
- `quality` — SD, HD or UHD preference;
- `languages`, `content`, `numbers`, `direction`, `timeshift`;
- `wrapper_tokens` — metadata that was removed from the brand;
- `explicit_market`, `route_plan`, `route_reason`;
- `south_asian_context`.

The parser recognizes pipe segments, known colon prefixes, underscore wrappers,
parenthetical market suffixes, bare structural market suffixes, Toronto
wrappers, provider labels and short language codes. It does not remove a token
merely because it happens to appear somewhere in a channel name.

## 2. Evidence and routing

Market evidence has strength. A trailing `| USA`, `(USA)`, `Toronto` or similar
explicit edition is stronger than a category-language inference. Weak attached
prefixes such as `UK-` can be overridden by a stronger category when providers
mislabel imported channels.

South-Asian channels with an explicit USA/North-America edition use this route:

```text
US -> CA
```

The India catalog is deliberately absent from that plan. An unavailable North
American schedule returns `REVIEW` or `UNMATCHED`, not a plausible-looking but
wrong India guide.

Unsupported country categories such as Australia, Malaysia, Philippines,
France, Turkey and others keep their own route code. They do not accidentally
fall through to India simply because a channel name contains a South-Asian
word.

## 3. Candidate model and indexes

Each live catalog ID becomes a `CandidateContextV8` with the same semantic
fields. Four deterministic per-region indexes are built:

1. strict identity;
2. edition-aware identity;
3. optional-descriptor-relaxed identity;
4. compact punctuation/spacing identity.

The complete ordered catalog is fingerprinted. Reusing the identical catalog
reuses the indexes; changing an ID, feed, region, dummy ID or order rebuilds
them.

## 4. Compatibility gates

An exact text key is not sufficient by itself. A candidate is rejected when
meaningful fields conflict:

- different +1/timeshift;
- extra/alternate mismatch;
- East vs West/Pacific mismatch;
- conflicting channel number;
- incompatible language;
- disjoint meaningful content/edition words;
- Gold/News/Music/Movies/Plus and similar descriptor differences.

Quality does not define a different schedule family. It is a deterministic
preference inside an already compatible family: a 4K/FHD provider stream can
use an HD schedule when no separate 4K schedule exists.

## 5. Decision order

The resolver uses this order:

1. untouched-name safety, adult, event, PPV, 24/7 and dummy rules;
2. approved generic knowledge whose target exists in the current catalog;
3. verified pre-panel aliases and established source-specific rules;
4. contextual structural exact indexes;
5. a panel EPG ID already proven to contain useful current/future programmes;
6. verified station/local/exact compatibility rules;
7. contextual fuzzy suggestion;
8. explicit `UNMATCHED` reason.

The pre-panel compatibility position preserves known v7 decisions that were
intentionally stronger than unreliable panel IDs. Generic fuzzy decisions are
not inherited.

## 6. Fuzzy policy

Fuzzy candidates are filtered by the same semantic compatibility gates and
ranked within the selected route plan. Duplicate IDs/families are collapsed
before the margin is calculated.

Regardless of score, a fuzzy-only decision is:

```text
REVIEW / epgshare_candidate
```

It can never be `AUTO_EPGSHARE`.

## 7. Approved knowledge

Approved aliases are keyed by normalized alias + allowed markets. Re-registering
one key merges ordered target IDs rather than creating a sequence of patches.
Targets are tried in priority order and used only when present in the active
catalog and compatible with an explicit route.

Exported memory intentionally excludes:

- server and stream IDs;
- credentials;
- panel URLs;
- playlist or playable URLs.

Only rows explicitly marked `MANUAL` or `APPROVED` become learned knowledge.

The PTC Chak De policy is represented as data:

```text
PTC.CHAK.DE.in / PTC.Chak.De.in
then PTC.NEWS.in / PTC.News.in only if the dedicated ID disappears
```

## 8. Programme schedule equivalence

For selected IDs, v8 can stream-parse an XMLTV file and create a compact future
fingerprint from normalized programme title + five-minute start bucket. Pairwise
similarity matches the same title within a configurable start tolerance.

Only groups meeting a minimum programme count and similarity threshold are
registered. Registered groups affect family de-duplication; they do not permit
an unrelated name to bypass routing or semantic compatibility.

## 9. Explainability

Every output row adds:

- `parsed_channel_name`;
- `detected_region`;
- `route_plan`;
- `route_reason`;
- `provider_wrappers`;
- `language_hints`;
- `match_method`;
- specific reason text.

This turns a review row into a diagnosable decision rather than a bare score.

## 10. Production boundary

Interactive matching and unattended programme refresh are separate:

- the Colab notebook inventories and matches channels;
- a person reviews uncertain rows and saves final mappings;
- GitHub Actions uses those approved mappings only;
- Builder v7.1 downloads schedules and generates XMLTV without rematching.

This boundary is the principal safeguard against silent mapping drift.
