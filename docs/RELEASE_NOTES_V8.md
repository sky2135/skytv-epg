# Smart Rules v8.4 GitHub release notes

## Matcher

- GitHub source now contains the complete Smart Rules v8.4 contextual engine from the validated Colab notebook.
- The legacy v7 compatibility engine remains byte-for-byte unchanged.
- Scheduled builds still use approved mappings only and never perform unattended rematching.
- The embedded regression suite contains 222 matcher and safety checks.

## Icon-aware XMLTV output

A separate exact icon layer now enriches each generated XMLTV file.

Priority:

1. exact URL in a mapping row;
2. exact `config/channel_icons.csv` override;
3. exact icon from the source XMLTV ID;
4. no icon.

There is no fuzzy logo matching. Source XMLTV files are parsed at most once per workflow run for all three servers.

Local permitted assets in `assets/logos/` are copied to GitHub Pages. Each server manifest and `epg/index.json` report icon coverage.

## Schedule visibility

The rolling 72-hour design is unchanged, but the workflow now makes intentional non-build checks clearer:

- the due-check writes a Markdown summary;
- a visible `Refresh not due` job explains the skip;
- last-success and next-due timestamps are displayed;
- manual runs expose `force_build`, enabled by default.

## Upgrade safety

The upgrade overlay excludes `mappings/`, `state/`, and generated `public/` files. It is intended for an existing repository.
