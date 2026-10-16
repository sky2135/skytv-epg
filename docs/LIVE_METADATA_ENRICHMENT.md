# Live channel enrichment

The daily publication joins active channel identities to the public iptv-org
channels, feeds, countries and logos catalogs. Joins require exact names,
aliases or channel IDs and reject conflicting country editions. Broadcast
languages come from feed evidence or explicit provider language labels.
Country alone does not establish a language. Unknown values remain unknown
when the available evidence is ambiguous; manual and locked values remain
authoritative.

`EPG/server_N_metadata.json.gz` remains schema v1. Enriched rows include
`metadataEvidence` with the source, channel ID, method and changed facets.
Every row includes `logoProvenance`: image kind, identity source/match,
imageStatus and brandVerified. A reachable image is separate from a reviewed
brand identity. Generated category artwork and portraits are identified
explicitly. Permanent failed image checks are withheld; temporary network
failures remain visible as unverified. Image checks are bounded and cached,
so some URLs can remain unchecked until a later refresh.

Exact registry spelling aliases can improve EPGShare matching only when a
single same-country EPG identity exists. Canonical/relaxed collisions,
different editions and competing source IDs block the alias. The existing
current inventory, quarantine, source-catalog corroboration and informative
future-programme gates still apply. The registry never supplies a schedule.

`EPG/server_N_epg.json.gz` keeps the original three-item tuples. Rich source
facts are in the opt-in object-row v2 feed documented in
[LIVE_PROGRAMME_V2.md](LIVE_PROGRAMME_V2.md). The SkyTVPlayer catalog keeps
`epg.programme` on v1 and advertises v2 separately as `epg.programmeRich`.
Clients must explicitly implement v2 before consuming that reference.

The pre-deployment auditor checks compressed checksums, sizes, server/time
and mapping parity, exact projection of v2 back to v1, logo parity and field
counts. Aggregate production measurements are published at
`reports/live_enrichment.json`. Real programme detail counts exclude
synthetic guides. Synthetic coverage is never counted as a real schedule.

Public registry documentation: <https://github.com/iptv-org/api>.
The registry cache records source URLs, fetch time and SHA-256 checksums.
