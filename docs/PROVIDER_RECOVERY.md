# Provider availability recovery

The October 18, 2026 publication failure had two independent causes:

- Server 2 moved its XMLTV redirect to `epg-xml.dunze.org`. The configured
  provider returned that target, and a separate HTTPS request without account
  parameters returned XMLTV containing channel and programme elements.
- Server 1 returned 14,860 API records, including one incomplete channel
  identity. The strict inventory parser rejected the collection, and the
  authenticated M3U fallback returned HTTP 404. Both tested application
  identities received the same API results; changing request headers was not
  supported by the evidence.

The XMLTV downloader permits only the exact verified public hostname. External
requests use a fresh session without provider credentials, query parameters,
cookies or ambient authentication. Unknown hosts, insecure redirects and
lookalike domains remain rejected.

Inventory recovery handles only a small, explicit class of malformed records:
an exact positive numeric stream ID with blank channel-name fields. Recovery
is limited to ten records and one percent of the response. The remaining
records must pass the unchanged strict parser and credential checks. Missing
identities, conflicting IDs, untrusted values and larger corrupt collections
still stop ingestion or use the existing validated fallback.

An identified malformed record is quarantined for the current run. Its
authoritative Sheet row is preserved, excluded from matching and missing
reports, and disabled only in the effective publication snapshot. New malformed
records are never appended. It is not counted as a valid provider channel or
used to satisfy channel-count floors. The temporary restriction ends when the
provider returns a valid record; it does not create an OPEN alert requiring
manual closure. The workflow reports the aggregate quarantine count explicitly.

The independent health monitor continues to fail for unavailable providers,
stale publications and insufficient real future schedules. A successful build
must publish a new availability report before an older degraded report clears.

The manual **Diagnose provider transport** workflow uses existing repository
secrets for requests to configured provider endpoints. It prints only HTTP
statuses, row counts, fixed diagnostic codes and verified anonymous-host
evidence. It never logs provider response bodies, account values, channel
identities or credential-bearing URLs. Anonymous XMLTV sampling is a transport
check; the normal publication pipeline still validates the full guide.
