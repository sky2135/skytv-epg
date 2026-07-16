#!/usr/bin/env python3
"""Record a successful Pages deployment for the 72-hour due check."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()

    args.state.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "lastSuccessUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "workflowRunId": os.environ.get("GITHUB_RUN_ID", ""),
        "workflowRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "sourceCommit": os.environ.get("GITHUB_SHA", ""),
    }
    args.state.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
