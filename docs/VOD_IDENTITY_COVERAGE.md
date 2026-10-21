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
