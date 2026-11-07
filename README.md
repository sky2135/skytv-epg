# SKY TV

## SkyTVPlayer movie and TV database

Live EPG and VOD refreshes run independently. The scheduled **2 - Refresh live
EPG** workflow updates channels, schedules, logos, and live metadata. Run
**Refresh VOD catalogs** from GitHub Actions when movies, series, categories,
languages, or release metadata need refreshing. VOD also runs daily at 4:47 a.m.
America/New_York time. Both workflows support manual runs.

Each refresh publishes alongside the other component's last validated snapshot.
Builds can overlap; composition and deployment share one lock. The app index's
live-guide hashes are refreshed during composition while the original live and
VOD generation timestamps remain intact. An invalid component cannot replace
the current site. See [independent publication](docs/INDEPENDENT_PUBLICATION.md)
for recovery and matching-cache behavior.

Native guides for servers 2 and 3 retry temporary failures with longer waits
and honor provider retry deadlines. A last working guide is encrypted before
being cached. During an outage, it can supply only exact channel IDs with
programme times still valid near the build time, and expires after 36 hours.
Expired or missing schedules use the existing disclosed channel placeholders;
cached reuse and provider outages remain visible in health reports.

TiviMate compatibility links are rebuilt from the current authenticated provider
inventory. Exact playlist IDs and names are exported only after confirming the
same server, stream and channel identity. A shared ID is used only when every
provider stream carrying it resolves to the same schedule. Conflicting or unsafe
attributes are reported without replacing another channel's guide.

Server 1 has reviewed EPGShare connections for the UK Sky Sports lineup. These
reuse the existing one-pass schedule selection and require a current or upcoming
programme within six hours plus current provider identity confirmation. Explicit
exclusions, manual mappings and identity quarantines remain protected. The private
Sheet snapshot stays unchanged; applied runtime connections are disclosed in build
provenance. Server 1 never reads provider programme schedules.

Each server publishes a `server_X_guide_link_health.json` summary and a companion
CSV in `reports/server_X/`. Reports distinguish real schedules, placeholders,
expired data and exact-ID conflicts. Omitted private rows contribute aggregate
counts only; their identities are excluded from public reports. These checks use
the builder's existing schedule statistics rather than downloading or parsing the
guide again.

**Recover unavailable native EPG** checks affected sources every two hours and
requests a live refresh after genuine current and upcoming schedules return.
It skips another refresh when one is already active or recently started.
**Diagnose provider transport**, in `native_epg` mode, checks servers 2 and 3
without publishing anything. A first valid source fetch seeds the private cache.

Movie and TV catalogs cover all three servers, with optional cached TMDB
metadata, provider episode IDs, and small Home feeds in `public/skytvplayer/`.
Movies and series use verified gzip shards so Android can load Home without
scanning the provider's full VOD library.

See [the SkyTVPlayer integration guide](docs/SKYTVPLAYER_CATALOG.md) for the
file contract, requested shelf order, TMDB secret setup, and Android reader
requirements. Continue Watching and Up Next use private app watch progress.
