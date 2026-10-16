# SKY TV

## SkyTVPlayer movie and TV database

The daily GitHub workflow also builds separate movie and TV catalogs from all
three servers, with optional cached TMDB metadata, available provider episode
IDs, and small Home feeds in `public/skytvplayer/`. The existing JSON EPG is
linked from the app index. Movies and series are split into verified gzip shards
so Android can load Home without scanning the provider's full VOD library.

See [the SkyTVPlayer integration guide](docs/SKYTVPLAYER_CATALOG.md) for the
file contract, requested shelf order, TMDB secret setup, and Android reader
requirements. Continue Watching and Up Next use private app watch progress.
