# Reusable Smart Rules v8 knowledge

`approved_channel_aliases.csv` stores generic channel identity + market + ordered
EPG ID choices. It never stores server credentials, URLs or stream IDs. Every
saved target is looked up in the current live catalog before it can be used.

`regions` is the channel's real market. `target_regions` is normally blank. It
may name a different catalog storage region only for an audited, exact alias
whose target ID is written in the same row. These exceptions do not widen fuzzy
search: they still require one explicit market, matching protected channel
details, current programme data, and the repository's pinned knowledge hash.

`schedule_equivalence_groups.json` is optional. Add a group only after the v8
schedule fingerprint tools confirm that the IDs carry materially identical
programme title/time sequences. Name similarity alone is not enough.

The Colab notebook automatically loads copies uploaded to:

```text
/content/approved_channel_aliases.csv
/content/schedule_equivalence_groups.json
```
