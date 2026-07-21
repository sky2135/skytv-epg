# Reusable Smart Rules v8 knowledge

`approved_channel_aliases.csv` stores generic channel identity + market + ordered
EPG ID choices. It never stores server credentials, URLs or stream IDs. Every
saved target is looked up in the current live catalog before it can be used.

`schedule_equivalence_groups.json` is optional. Add a group only after the v8
schedule fingerprint tools confirm that the IDs carry materially identical
programme title/time sequences. Name similarity alone is not enough.

The Colab notebook automatically loads copies uploaded to:

```text
/content/approved_channel_aliases.csv
/content/schedule_equivalence_groups.json
```
