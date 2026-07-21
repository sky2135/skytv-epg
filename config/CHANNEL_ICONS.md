# Channel icon configuration

The scheduled builder automatically keeps an exact `<icon src="..."/>` from the selected source XMLTV channel whenever that source provides one.

Use `channel_icons.csv` only for missing or incorrect icons. Logo matching is exact and never fuzzy.

## Columns

- `enabled`: `true`/`false`; blank means enabled.
- `server_id`: `server_1`, `server_2`, `server_3`, or `*` for all servers.
- `epg_id`: exact XMLTV EPG ID. This is the preferred key.
- `channel_name`: exact visible provider channel name; useful only when the same EPG ID needs different branding on one server.
- `icon_url`: complete `http://` or `https://` image URL.
- `local_file`: path relative to `assets/logos/`, for example `india/ptc-punjabi-in.png`.
- `priority`: higher number wins when duplicate exact rows exist; default is `100`.
- `notes`: documentation only.

Provide either `icon_url` or `local_file`. When both are present, `icon_url` wins.

## Examples

External URL:

```csv
enabled,server_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,PTC.PUNJABI.in,,https://example.org/logos/ptc-punjabi.png,,100,Verified logo
```

Locally hosted file:

```csv
enabled,server_id,epg_id,channel_name,icon_url,local_file,priority,notes
true,*,PTC.PUNJABI.in,,,india/ptc-punjabi-in.png,100,Stored in assets/logos
```

For a custom GitHub Pages domain, create the repository Actions variable `EPG_PUBLIC_BASE_URL` with the base URL, such as `https://epg.example.com`. For the normal `username.github.io/repository` address, the workflow derives the URL automatically.
