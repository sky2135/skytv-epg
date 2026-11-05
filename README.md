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
