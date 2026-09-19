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
        "--ai-shard-count",
        type=int,
        default=1,
        help=(
            "Number of deterministic AI advisory shards (1-64). Use sibling "
            "shard indexes to cover a pool larger than --ai-limit."
        ),
    )
    shadow.add_argument(
        "--ai-shard-index",
        type=int,
        default=0,
        help="Zero-based AI advisory shard to run (smaller than --ai-shard-count).",
    )
    shadow.add_argument(
        "--ledger",
        type=Path,
        help="Optional private SQLite observation ledger.",
    )
    dummy_shadow = subparsers.add_parser(
        "dummy-shadow",
        help=(
            "Classify verified no-schedule REVIEW rows from saved inputs without "
            "writing any mapping."
        ),
    )
    dummy_shadow.add_argument("--mappings-csv", type=Path, required=True)
    dummy_shadow.add_argument("--alerts-csv", type=Path, required=True)
    dummy_shadow.add_argument("--epg-xml", type=Path, required=True)
    dummy_shadow.add_argument("--epg-text", type=Path, required=True)
    dummy_shadow.add_argument("--output-dir", type=Path, required=True)
    dummy_shadow.add_argument(
        "--as-of",
        required=True,
        help="Frozen UTC evidence time. Reuse it for deterministic replay.",
    )
    dummy_shadow.add_argument(
        "--servers",
        nargs="+",
        choices=SUPPORTED_SERVERS,
        default=list(SUPPORTED_SERVERS),
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
    validate_dummy = subparsers.add_parser(
        "validate-dummy",
        help="Strictly validate a private dummy-shadow bundle without writing state.",
    )
    validate_dummy.add_argument("--bundle-dir", type=Path, required=True)
    validate_dummy.add_argument("--mappings-csv", type=Path)
    validate_dummy.add_argument("--alerts-csv", type=Path)
    validate_dummy.add_argument(
        "--as-of",
        help="Optional canonical UTC validation time; defaults to current UTC.",
    )
    analyze = subparsers.add_parser(
        "analyze-rules",
        help="Mine private, read-only rule candidates from a validated bundle.",
    )
    analyze.add_argument("--bundle-dir", type=Path, required=True)
    analyze.add_argument("--output-dir", type=Path, required=True)
    analyze.add_argument(
        "--as-of",
        required=True,
        help="Canonical UTC validation time for deterministic replay.",
    )
    analyze.add_argument("--mappings-csv", type=Path)
    analyze.add_argument("--alerts-csv", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "shadow":
            from .ai import validate_advisory_shard
            from .pipeline import run_shadow

            validate_advisory_shard(args.ai_shard_count, args.ai_shard_index)
            if not args.use_ai and (
                args.ai_shard_count != 1 or args.ai_shard_index != 0
            ):
                raise ContractError(
                    "--ai-shard-count and --ai-shard-index require --use-ai."
                )
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
                ai_shard_count=args.ai_shard_count,
                ai_shard_index=args.ai_shard_index,
                ledger_path=args.ledger,
            )
        elif args.command == "dummy-shadow":
            from .dummy_shadow import run_dummy_shadow

            dummy_result = run_dummy_shadow(
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                all_source_file=args.epg_xml,
                all_source_catalog_file=args.epg_text,
                output_dir=args.output_dir,
                as_of=args.as_of,
                servers=args.servers,
            )
            print(
                "Matching Lab dummy shadow complete: "
                f"{dummy_result.classification_count:,} private classifications; "
                f"run {dummy_result.run_id[:12]}..."
            )
            print(f"Private output directory: {dummy_result.output_dir}")
            return 0
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
        elif args.command == "validate-dummy":
            from .dummy_validation import validate_dummy_bundle

            validation = validate_dummy_bundle(
                args.bundle_dir,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
                as_of=args.as_of,
            )
            print(
                "Matching Lab dummy bundle valid: "
                f"{validation.classification_count:,} private classifications; "
                f"run {validation.run_id[:12]}..."
            )
            print(
                "Current snapshot checks: "
                f"Mappings={'yes' if validation.mappings_checked else 'no'}, "
                f"Sync Alerts={'yes' if validation.alerts_checked else 'no'}"
            )
            return 0
        elif args.command == "analyze-rules":
            from .rule_analysis import analyze_rule_candidates

            analysis = analyze_rule_candidates(
                bundle_dir=args.bundle_dir,
                output_dir=args.output_dir,
                as_of=args.as_of,
                mappings_csv=args.mappings_csv,
                alerts_csv=args.alerts_csv,
            )
            print(
                "Matching Lab rule analysis complete: "
                f"{analysis.candidate_count:,} private candidates "
                f"({analysis.consistent_count:,} consistent, "
                f"{analysis.contradictory_count:,} contradictory)."
            )
            print(f"Private output directory: {analysis.output_dir}")
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
