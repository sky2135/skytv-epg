# SKY TV EPG v8.4 GitHub validation report

Validated on 2026-07-21.

## Automated tests

```text
18 unittest methods                         PASS
Smart Rules v7 + v8.4 regression rows      222 / 222 PASS
Legacy engine SHA-256 boundary              PASS
Contextual v8.4 source/notebook boundary    PASS
Ordered parallel catalog behavior           PASS
Approved alias-memory round trip            PASS
Schedule fingerprint equivalence            PASS
Exact icon configuration                    PASS
Source XMLTV icon extraction                PASS
Unsafe icon URL/path rejection              PASS
Streaming gzip XMLTV icon insertion         PASS
Three-server shared-source production build PASS
Rolling 72-hour scheduler cases             4 / 4 PASS
Source and panel icon end-to-end output     PASS
```

## Static validation

```text
All Python source files compile             PASS
All nine notebook code cells compile        PASS
Notebook saved outputs removed              PASS
Workflow YAML parses                        PASS
No private server credentials included      PASS
```

## Production boundaries

- Scheduled builds consume approved mapping CSV files only.
- Smart Rules v8.4 is tested but is not run unattended against live lineups.
- Icon enrichment is exact and independent from matching.
- Live provider connectivity must be confirmed by the first forced GitHub run because credentials are not present in the validation environment.
