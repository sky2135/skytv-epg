# Channel icon configuration

The scheduled builder keeps a safe exact `<icon src="..."/>` from the selected
source XMLTV channel when no exact mapping or reviewed override selects other
artwork.

Use `channel_icons.csv` only for missing or incorrect icons. Logo matching is exact and never fuzzy.

## Columns

- `enabled`: `true`/`false`; blank means enabled.
- `server_id`: `server_1`, `server_2`, `server_3`, or `*` for all servers.
- `stream_id`: exact provider stream ID. This is the safest key for custom
  person, programme, and 24/7-channel artwork.
- `epg_id`: exact XMLTV EPG ID. Use it only when the ID is unique and stable.
- `channel_name`: exact visible provider channel name. Custom person and 24/7
  artwork should also include the exact `stream_id`.
- `icon_url`: complete `http://` or `https://` image URL.
- `local_file`: path relative to `assets/logos/`, for example `india/ptc-punjabi-in.png`.
- `priority`: higher number wins when duplicate exact rows exist; default is `100`.
- `notes`: documentation only.

Provide either `icon_url` or `local_file`. When both are present, `icon_url` wins.
When more than one identity field is present, every supplied field must match.
Never assign a person image using a shared dummy EPG ID.

## Examples

External URL:

```csv
enabled,server_id,stream_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,,PTC.PUNJABI.in,,https://example.org/logos/ptc-punjabi.png,,100,Verified logo
```

Locally hosted file:

```csv
enabled,server_id,stream_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,,PTC.PUNJABI.in,,,india/ptc-punjabi-in.png,100,Stored in assets/logos
```

For a custom GitHub Pages domain, create the repository Actions variable
`EPG_PUBLIC_BASE_URL` with the base URL, such as `https://epg.example.com`.
For the normal `username.github.io/repository` address, the workflow derives
the URL automatically.

## Generated fallback rows

Run the exact-coverage generator after receiving a new mapping snapshot and
matching EPGShare catalog:

```bash
python scripts/generate_missing_icon_overrides.py \
  --mapping-csv /path/to/Mappings.csv \
  --source-xmltv /path/to/epg_ripper_ALL_SOURCES1.xml.gz \
  --base-config config/channel_icons.csv \
  --output-config .build/private-icons/channel_icons.csv
```

The output contains private provider stream identities. Keep it inside
`.build/`; never commit or upload it. The command refuses to overwrite the
checked-in base config. The normal publishing workflow creates this private
file automatically and gives it directly to the EPG builder.

Every generated row uses the exact server ID, stream ID, and channel name, so a
shared dummy EPG ID cannot send one channel's artwork to another channel.
Named 24/7 channels are matched only by an exact category and anchored name
pattern. An approved real portrait from the public subject catalog wins over an
original name-and-motif fallback. There is no fuzzy person matching.

Fallbacks are required for synthetic/dummy guides and native panel guides
because those sources do not publish an icon through the production builder.
They are also required when an EPGShare icon URL is not safe to publish. For
example, production rejects icon URLs with query strings or fragments because
they can contain private tokens. The fallback files are PNG for broad IPTV
player support. Generic category art uses priority `10`, named fallback art uses
priority `300`, and an approved portrait uses priority `400`.
