# Movie identity coverage audit

Run **Diagnose provider transport** with mode `vod_identity_coverage` to count
identity hints supplied in the movie inventories of all three servers. The
workflow retains a small aggregate `report.json` artifact for seven days.

The diagnostic requests `get_vod_categories` and `get_vod_streams` once per
server through the existing bounded transport. Servers run concurrently with
isolated sessions. It does not request individual movie details, rebuild the
catalogue, publish Pages, or change provider data. Credentials remain in Actions
secrets; run this diagnostic in GitHub Actions rather than on a workstation.

Counts describe catalogue entries, including different versions of a film.
They do not establish that the supplied identifier belongs to the listed film.
TMDB and IMDb coverage overlap: `withAnySupportedId` is the union, and
`withBoth` is the intersection. Conflicting valid aliases are counted separately.
Runtime, cast and director presence are supporting metadata, not film IDs.

Language groups are separate views and can overlap. Explicit category labels
include full English/Hindi/Punjabi names and unambiguous `[EN]`/`[HI]` labels.
Subtitle labels are separate. `[PA]`/`[PAN]` without a full Punjabi label are
ambiguous with country labels. `INDIA` does not establish Hindi. Original and
spoken language fields are hints; none of these views verifies audio tracks.
The current normalizer view preserves production behavior for comparison.

Check each server's `success` and `countsAvailable` before interpreting counts.
Failures use fixed error codes and clear partial coverage counts. The report
contains no raw movie titles, film IDs, provider URLs, credentials or payloads.
The public app export can be audited separately for verified matches; absence
of an IMDb field there does not establish absence in an upstream inventory or
an individual movie's detail response.

## Inspect recently added provider records

Use mode `vod_recent_samples` to inspect the five most recently added movies
with explicit English, Hindi and Punjabi labels on every server. Clear language
markers in the title take precedence over category labels, so a title marked
`(Hindi)`, `[FRN]` or `[CHI]` is not selected as English because of its category.
Ordinary words in film names are not language markers. The report retains the
selection evidence and flags title/category conflicts without changing the
provider metadata or production catalogue rules.
Selection uses the provider's `added` timestamp, not a film's release date.
Unclassified languages and entries with unusable added timestamps are reported
rather than guessed. Subtitle labels do not establish audio language.

This mode also attempts a bounded `get_vod_info` request for each selected movie.
The report keeps list metadata and detail metadata separate and adds no TMDB
enrichment. Detail failures are recorded using fixed error codes; the existing
authentication, redirect and download checks remain in force.

The `vod-recent-samples` artifact contains actual provider metadata with an
explicit export allowlist. Credentials, authenticated/provider URLs, playback
sources and unsupported fields are withheld. Redaction is reported, so this is
an inspectable metadata sample rather than a complete unfiltered API response.
Only the fixed `report.json` file is retained for seven days.
