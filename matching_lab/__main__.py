"""Command-line entry point for the private SKY TV EPG Matching Lab."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .models import ContractError
from .validation import validate_bundle


SUPPORTED_SERVERS = ("server_1", "server_2", "server_3")
DEFAULT_AI_MODEL = "gemini-3.5-flash-lite"
MAX_ADVISORY_REQUESTS = 1_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m matching_lab",
        description=(
            "Run the private, proposal-only SKY TV EPG Matching Lab. "
            "This CLI exposes no Google Sheets write operation."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    shadow = subparsers.add_parser("shadow", help="Generate a private proposal bundle.")
    shadow.add_argument("--mappings-csv", type=Path, required=True)
    shadow.add_argument("--alerts-csv", type=Path)
    shadow.add_argument("--epg-xml", type=Path, required=True)
    shadow.add_argument("--epg-text", type=Path, required=True)
    shadow.add_argument("--output-dir", type=Path, required=True)
    shadow.add_argument(
        "--as-of",
        required=True,
        help="Frozen UTC evidence time. Reuse the same value for deterministic replay.",
    )
    shadow.add_argument(
        "--servers",
        nargs="+",
        choices=SUPPORTED_SERVERS,
        default=list(SUPPORTED_SERVERS),
    )
    shadow.add_argument(
        "--use-ai",
        action="store_true",
        help=(
            "Attach advisory-only Gemini evidence. Requires GEMINI_API_KEY and "
            "--ai-cache; AI can only demote or prioritize proposals."
        ),
    )
    shadow.add_argument(
        "--ai-cache",
        type=Path,
        help="Durable private SQLite replay cache required with --use-ai.",
    )
    shadow.add_argument("--ai-model", default=DEFAULT_AI_MODEL)
    shadow.add_argument(
        "--ai-limit",
        type=int,
        default=MAX_ADVISORY_REQUESTS,
        help=f"Maximum advisory requests (1-{MAX_ADVISORY_REQUESTS}).",
    )
    shadow.add_argument(
        "--ledger",
        type=Path,
        help="Optional private SQLite observation ledger.",
    )
    validate = subparsers.add_parser(
        "validate", help="Strictly validate a private bundle without writing state."
    )
    validate.add_argument("--bundle-dir", type=Path, required=True)
    validate.add_argument("--mappings-csv", type=Path)
    validate.add_argument("--alerts-csv", type=Path)
    validate.add_argument(
        "--as-of",
        help="Optional canonical UTC validation time; defaults to current UTC.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "shadow":
            from .pipeline import run_shadow

            ai_api_key = os.environ.get("GEMINI_API_KEY", "") if args.use_ai else ""
            if args.use_ai and not ai_api_key.strip():
                raise ContractError(
                    "--use-ai requires GEMINI_API_KEY in the process environment."
                )
            if args.use_ai and args.ai_cache is None:
                raise ContractError("--use-ai requires --ai-cache for replay safety.")
            if not args.use_ai and args.ai_cache is not None:
                raise ContractError("--ai-cache is only valid together with --use-ai.")
            result = run_shadow(
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                all_source_file=args.epg_xml,
                all_source_catalog_file=args.epg_text,
                output_dir=args.output_dir,
                as_of=args.as_of,
                servers=args.servers,
                ai_api_key=ai_api_key,
                ai_model=args.ai_model,
                ai_cache_path=args.ai_cache,
                ai_max_requests=args.ai_limit,
                ledger_path=args.ledger,
            )
        elif args.command == "validate":
            validation = validate_bundle(
                args.bundle_dir,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                as_of=args.as_of,
            )
            print(
                f"Matching Lab bundle valid: {validation.proposal_count:,} private "
                f"proposals; run {validation.run_id[:12]}..."
            )
            print(
                "Current snapshot checks: "
                f"Mappings={'yes' if validation.mappings_checked else 'no'}, "
                f"Sync Alerts={'yes' if validation.alerts_checked else 'no'}"
            )
            return 0
        else:  # pragma: no cover - argparse enforces the command set.
            raise ContractError("Unknown Matching Lab command.")
    except ContractError as exc:
        print(f"Matching Lab stopped safely: {exc}", file=sys.stderr)
        return 2
    print(
        f"Matching Lab shadow run complete: {result.proposal_count:,} private proposals; "
        f"run {result.run_id[:12]}..."
    )
    print(f"Private output directory: {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
