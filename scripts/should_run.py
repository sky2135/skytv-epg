#!/usr/bin/env python3
"""Decide whether at least the requested number of hours passed since success."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_utc(value: str) -> datetime:
    cleaned = str(value or "").strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def emit(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    line = f"{name}={value}\n"
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        print(line, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--interval-hours", type=float, default=72.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    due = False
    reason = ""
    last_success = ""
    next_due = ""

    if args.force:
        due = True
        reason = "manual workflow dispatch"
    elif not args.state.is_file():
        due = True
        reason = "state file is missing"
    else:
        try:
            payload = json.loads(args.state.read_text(encoding="utf-8-sig"))
            last_success = str(payload.get("lastSuccessUtc", "")).strip()
            if not last_success:
                due = True
                reason = "lastSuccessUtc is missing"
            else:
                previous = parse_utc(last_success)
                due_at = previous + timedelta(hours=float(args.interval_hours))
                next_due = due_at.isoformat().replace("+00:00", "Z")
                due = now >= due_at
                reason = (
                    f"{float(args.interval_hours):g}-hour interval reached"
                    if due
                    else "not yet due"
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            due = True
            reason = f"state could not be read: {type(exc).__name__}"

    emit("due", "true" if due else "false")
    emit("reason", reason.replace("\n", " "))
    emit("last_success_utc", last_success)
    emit("next_due_utc", next_due)
    print(
        json.dumps(
            {
                "due": due,
                "reason": reason,
                "checkedAtUtc": now.isoformat().replace("+00:00", "Z"),
                "lastSuccessUtc": last_success or None,
                "nextDueUtc": next_due or None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
