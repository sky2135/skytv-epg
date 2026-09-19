from __future__ import annotations

import contextlib
import io
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from matching_lab import ai
from matching_lab.__main__ import main
from matching_lab.models import ContractError, ProposalRecord
from matching_lab.pipeline import _ai_config_sha256
from tests.test_matching_lab_ai import _proposal


def _proposal_set(count: int) -> tuple[ProposalRecord, ...]:
    base = _proposal()
    proposals: list[ProposalRecord] = []
    for index in range(count):
        draft = replace(
            base,
            proposal_id="0" * 64,
            stream_id=str(10_000 + index),
            row_guard_sha256=f"{index + 1:064x}",
            provider_identity_sha256=f"{index + 10_001:064x}",
        )
        proposals.append(replace(draft, proposal_id=draft.computed_id()))
    return tuple(proposals)


class MatchingLabAIShardingTests(unittest.TestCase):
    def test_shards_are_disjoint_complete_balanced_and_replay_stable(self) -> None:
        proposals = _proposal_set(53)
        shard_count = 7
        shards = tuple(
            ai.proposals_for_advisory_shard(
                proposals,
                shard_count=shard_count,
                shard_index=index,
            )
            for index in range(shard_count)
        )
        keys = tuple(
            {(proposal.server_id, proposal.stream_id) for proposal in shard}
            for shard in shards
        )

        self.assertEqual(set().union(*keys), {
            (proposal.server_id, proposal.stream_id) for proposal in proposals
        })
        for left in range(shard_count):
            for right in range(left + 1, shard_count):
                self.assertTrue(keys[left].isdisjoint(keys[right]))
        self.assertLessEqual(max(map(len, shards)) - min(map(len, shards)), 1)

        sibling_run = tuple(
            replace(
                replace(proposal, proposal_id="0" * 64, run_id="a" * 64),
                proposal_id=replace(
                    proposal, proposal_id="0" * 64, run_id="a" * 64
                ).computed_id(),
            )
            for proposal in proposals
        )
        replay_keys = {
            (proposal.server_id, proposal.stream_id)
            for proposal in ai.proposals_for_advisory_shard(
                tuple(reversed(sibling_run)),
                shard_count=shard_count,
                shard_index=3,
            )
        }
        self.assertEqual(replay_keys, keys[3])

    def test_default_shard_preserves_order_and_membership(self) -> None:
        proposals = _proposal_set(5)
        self.assertEqual(ai.proposals_for_advisory_shard(proposals), proposals)

    def test_invalid_shard_contracts_fail_closed(self) -> None:
        for count, index in (
            (0, 0),
            (65, 0),
            (1, -1),
            (2, 2),
            (True, 0),
            (2, False),
        ):
            with self.subTest(count=count, index=index):
                with self.assertRaises(ContractError):
                    ai.validate_advisory_shard(count, index)

    def test_ai_config_hash_binds_both_shard_values(self) -> None:
        common = {
            "enabled": True,
            "model": ai.DEFAULT_MODEL,
            "maximum_requests": 1_000,
            "cache_namespace_sha256": "f" * 64,
        }
        first = _ai_config_sha256(shard_count=4, shard_index=0, **common)
        sibling = _ai_config_sha256(shard_count=4, shard_index=1, **common)
        resized = _ai_config_sha256(shard_count=5, shard_index=0, **common)

        self.assertNotEqual(first, sibling)
        self.assertNotEqual(first, resized)
        self.assertEqual(
            first,
            _ai_config_sha256(shard_count=4, shard_index=0, **common),
        )


class MatchingLabAIShardingCLITests(unittest.TestCase):
    def _base_arguments(self) -> list[str]:
        return [
            "shadow",
            "--mappings-csv",
            "mappings.csv",
            "--epg-xml",
            "all.xml.gz",
            "--epg-text",
            "all.txt",
            "--output-dir",
            "bundle",
            "--as-of",
            "2026-09-18T12:00:00Z",
        ]

    def test_cli_rejects_invalid_or_inactive_shard_combinations(self) -> None:
        cases = (
            ([], ["--ai-shard-count", "2"]),
            (["--use-ai", "--ai-cache", "cache.sqlite3"], ["--ai-shard-count", "0"]),
            (["--use-ai", "--ai-cache", "cache.sqlite3"], ["--ai-shard-count", "65"]),
            (
                ["--use-ai", "--ai-cache", "cache.sqlite3"],
                ["--ai-shard-count", "2", "--ai-shard-index", "2"],
            ),
        )
        for prefix, shard_arguments in cases:
            with self.subTest(arguments=prefix + shard_arguments):
                with mock.patch("matching_lab.pipeline.run_shadow") as runner:
                    with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
                        with contextlib.redirect_stderr(io.StringIO()):
                            status = main(
                                self._base_arguments() + prefix + shard_arguments
                            )
                self.assertEqual(status, 2)
                runner.assert_not_called()

    def test_cli_passes_valid_shard_configuration_to_pipeline(self) -> None:
        result = SimpleNamespace(
            proposal_count=0,
            run_id="1" * 64,
            output_dir=Path("bundle"),
        )
        with mock.patch(
            "matching_lab.pipeline.run_shadow",
            return_value=result,
        ) as runner:
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
                with contextlib.redirect_stdout(io.StringIO()):
                    status = main(
                        self._base_arguments()
                        + [
                            "--use-ai",
                            "--ai-cache",
                            "cache.sqlite3",
                            "--ai-shard-count",
                            "4",
                            "--ai-shard-index",
                            "2",
                        ]
                    )

        self.assertEqual(status, 0)
        self.assertEqual(runner.call_args.kwargs["ai_shard_count"], 4)
        self.assertEqual(runner.call_args.kwargs["ai_shard_index"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
