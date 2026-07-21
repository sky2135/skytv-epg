# XMLTV icon policy

Channel logos are metadata only. They never influence Smart Rules matching.

## Exactness

The system accepts an icon only through an exact EPG ID, exact provider channel name, exact mapping-row URL, or the exact `<channel id>` in a source XMLTV file. It does not infer a logo from a similar filename or fuzzy channel name.

## Hosting

Preferred order:

1. source XMLTV icon;
2. exact external URL for personal testing;
3. locally hosted reviewed asset for production, when redistribution is permitted.

Do not mirror an entire third-party logo repository. Keep only the subset needed by the approved lineup and record attribution and permission.

## Security

The icon layer accepts only absolute HTTP(S) URLs, rejects URLs containing embedded credentials, rejects parent-directory local paths, and copies only supported image extensions.
