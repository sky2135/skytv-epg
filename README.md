# SKY TV

## SkyTVPlayer movie and TV database

Live EPG and VOD refreshes run independently. The scheduled **2 - Refresh live
EPG** workflow updates channels, schedules, logos, and live metadata. Run
**Refresh VOD catalogs** from GitHub Actions when movies, series, categories,
languages, or release metadata need refreshing. Both workflows also support
manual runs; VOD has no automatic schedule.

Each refresh publishes alongside the other component's last validated snapshot.
Builds can overlap; composition and deployment share one lock. The app index's
live-guide hashes are refreshed during composition while the original live and
VOD generation timestamps remain intact. An invalid component cannot replace
the current site. See [independent publication](docs/INDEPENDENT_PUBLICATION.md)
for recovery and matching-cache behavior.

Movie and TV catalogs cover all three servers, with optional cached TMDB
metadata, provider episode IDs, and small Home feeds in `public/skytvplayer/`.
Movies and series use verified gzip shards so Android can load Home without
scanning the provider's full VOD library.

See [the SkyTVPlayer integration guide](docs/SKYTVPLAYER_CATALOG.md) for the
file contract, requested shelf order, TMDB secret setup, and Android reader
requirements. Continue Watching and Up Next use private app watch progress.
