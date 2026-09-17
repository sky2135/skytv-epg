from __future__ import annotations

import gzip
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
for import_path in (REPO_ROOT / "scripts", REPO_ROOT / "src"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import auto_match_inventory as integration  # noqa: E402


class _Engine:
    pass


class _Resolver:
    def __init__(self, *, approved_aliases=()) -> None:
        self.engine = _Engine()
        self.approved_aliases = list(approved_aliases)
        self.registered: list[dict[str, str]] = []

    def register_approved_aliases(self, rows) -> int:
        materialized = [dict(row) for row in rows]
        self.registered.extend(materialized)
        return len(materialized)


def _mapping(
    server_id: str,
    stream_id: str,
    epg_id: str,
    *,
    action: str = "APPROVED",
    source: str = "epgshare01",
    name: str = "US: Acme News",
    notes: str = "",
) -> dict[str, str]:
    return {
        "server_id": server_id,
        "stream_id": stream_id,
        "enabled": "TRUE",
        "action": action,
        "source": source,
        "epg_id": epg_id,
        "channel_name": name,
        "category_name": "US | News",
        "notes": notes,
    }


def _candidate(epg_id: str, *, region: str = "US") -> dict[str, str]:
    return {
        "epg_id": epg_id,
        "feed": "US2",
        "region": region,
        "display_name": epg_id,
        "normalized": epg_id.casefold(),
    }


def _mapping_keys(rows) -> frozenset[tuple[str, str]]:
    return frozenset((row["server_id"], row["stream_id"]) for row in rows)


def _deterministic_note(
    row: dict[str, str],
    *,
    method: str = "strict",
    market: str = "US",
    source_sha256: str = "a" * 64,
) -> str:
    return integration.automatic_mapping_provenance_note(
        server_id=row["server_id"],
        stream_id=row["stream_id"],
        channel_name=row["channel_name"],
        category_name=row["category_name"],
        epg_id=row["epg_id"],
        match_method=method,
        market=market,
        source_sha256=source_sha256,
    )


def _automatic_mapping(
    server_id: str,
    stream_id: str,
    epg_id: str = "Acme.News.us2",
    *,
    name: str = "US: Acme News",
    notes: str | None = None,
) -> dict[str, str]:
    row = _mapping(
        server_id,
        stream_id,
        epg_id,
        action="AUTO_EPGSHARE",
        name=name,
        notes="" if notes is None else notes,
    )
    if notes is None:
        row["notes"] = _deterministic_note(row)
    return row


class LearnedAliasMemoryTests(unittest.TestCase):
    def test_registers_only_repeated_human_approved_target(self) -> None:
        resolver = _Resolver()
        rows = [
            _mapping("server_1", "1", "Acme.News.us2", action="MANUAL"),
            _mapping("server_2", "2", "Acme.News.us2"),
            _mapping(
                "server_3", "3", "Acme.News.us2", action="AUTO_EPGSHARE"
            ),
            _mapping("server_3", "4", "Acme.News.us2", action="REVIEW"),
        ]

        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 2)
        self.assertEqual(result.considered_groups, 1)
        self.assertEqual(result.registered_aliases, 1)
        self.assertEqual(result.rejected_groups, 0)
        self.assertEqual(
            _mapping_keys(result.support_rows),
            frozenset({("server_1", "1"), ("server_2", "2")}),
        )
        self.assertNotEqual(result.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(len(resolver.registered), 1)
        self.assertEqual(
            resolver.registered[0]["relationship"],
            "durable_human_approved_memory",
        )
        self.assertEqual(resolver.registered[0]["epg_ids"], "Acme.News.us2")

    def test_one_human_approved_row_registers_durable_memory(self) -> None:
        resolver = _Resolver()
        rows = [_mapping("server_1", "1", "Acme.News.us2", action="MANUAL")]

        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 1)
        self.assertEqual(result.registered_aliases, 1)
        self.assertEqual(result.rejected_groups, 0)
        self.assertEqual(
            resolver.registered[0]["relationship"],
            "durable_human_approved_memory",
        )

    def test_quarantine_prevents_two_server_automatic_support(self) -> None:
        resolver = _Resolver()
        rows = [
            _automatic_mapping("server_1", "1"),
            _automatic_mapping("server_2", "2"),
        ]
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset({("server_2", "2")}),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 1)
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.rejected_groups, 1)
        self.assertEqual(resolver.registered, [])

    def test_missing_or_changed_provider_identity_cannot_teach_alias_memory(self) -> None:
        rows = [
            _automatic_mapping("server_1", "1"),
            _automatic_mapping("server_2", "2"),
            _automatic_mapping("server_3", "3"),
        ]
        safe_inventories = [
            (
                "server_1",
                [{"stream_id": "1", "name": "US: Acme News", "category_id": "cat"}],
                {"cat": "US | News"},
            ),
            (
                "server_2",
                [{"stream_id": "2", "name": "US: Renamed News", "category_id": "cat"}],
                {"cat": "US | News"},
            ),
            ("server_3", [], {"cat": "US | News"}),
        ]
        current = integration._current_unchanged_mapping_keys(
            mapping_rows=rows,
            safe_inventories=safe_inventories,
        )
        self.assertEqual(current, frozenset({("server_1", "1")}))

        resolver = _Resolver()
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=current,
        )
        self.assertEqual(result.evidence_rows, 1)
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(resolver.registered, [])

    def test_one_server_automatic_rows_cannot_create_global_memory(self) -> None:
        resolver = _Resolver()
        rows = [
            _automatic_mapping("server_1", "1"),
            _automatic_mapping("server_1", "2"),
        ]
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 2)
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.rejected_groups, 1)
        self.assertEqual(resolver.registered, [])

    def test_two_servers_with_strong_deterministic_provenance_register(self) -> None:
        resolver = _Resolver()
        rows = [
            _automatic_mapping("server_1", "1"),
            _automatic_mapping("server_2", "2"),
        ]
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 2)
        self.assertEqual(result.registered_aliases, 1)
        self.assertEqual(result.rejected_groups, 0)
        self.assertEqual(_mapping_keys(result.support_rows), _mapping_keys(rows))
        self.assertEqual(
            resolver.registered[0]["relationship"],
            "durable_automatic_verified_memory",
        )

    def test_deterministic_provenance_is_bound_to_identity_and_target(self) -> None:
        valid = _automatic_mapping("server_1", "1")
        self.assertEqual(
            integration._automatic_memory_provenance(
                valid, expected_market="US"
            ),
            "deterministic_bound_v2",
        )

        changed_target = dict(valid)
        changed_target["epg_id"] = "Other.News.us2"
        changed_identity = dict(valid)
        changed_identity["server_id"] = "server_2"
        legacy_unbound = dict(valid)
        legacy_unbound["notes"] = (
            "auto-map-v1 method=strict; market=US; "
            f"matcher={integration.STRICT_MATCHER_VERSION}; "
            f"matcher_build={integration.STRICT_MATCHER_BUILD_ID}; "
            f"matcher_sha256={integration.STRICT_MATCHER_SOURCE_SHA256}; "
            f"engine_sha256={integration.STRICT_ENGINE_SOURCE_SHA256}; "
            f"catalog_sha256={'a' * 64}; source_sha256={'a' * 64}"
        )
        for row in (changed_target, changed_identity, legacy_unbound):
            with self.subTest(row=row):
                self.assertIsNone(
                    integration._automatic_memory_provenance(
                        row, expected_market="US"
                    )
                )

    def test_unverified_or_tampered_automatic_provenance_cannot_teach(self) -> None:
        copied = _automatic_mapping(
            "server_1",
            "1",
            notes="ai-verified-v2 copied marker without bound fields",
        )
        valid = _automatic_mapping("server_2", "2", notes="")
        valid["notes"] = integration._ai_verified_v2_provenance_note(
            valid,
            match_method="strict",
            market="US",
            source_sha256="b" * 64,
            text_catalog_file_sha256="c" * 64,
            text_catalog_fingerprint_sha256="d" * 64,
            text_catalog_generated_token="20260915120000",
        )
        tampered = dict(valid)
        tampered["server_id"] = "server_3"
        tampered["stream_id"] = "3"
        # The copied note remains bound to server_2/2 and must fail closed.

        resolver = _Resolver()
        rows = [copied, valid, tampered]
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 1)
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.rejected_groups, 1)
        self.assertEqual(resolver.registered, [])

    def test_two_bound_ai_verified_servers_can_register(self) -> None:
        rows = [
            _automatic_mapping("server_1", "1", notes=""),
            _automatic_mapping("server_2", "2", notes=""),
        ]
        for row in rows:
            row["notes"] = integration._ai_verified_v2_provenance_note(
                row,
                match_method="strict",
                market="US",
                source_sha256="c" * 64,
                text_catalog_file_sha256="d" * 64,
                text_catalog_fingerprint_sha256="e" * 64,
                text_catalog_generated_token="20260915120000",
            )
        resolver = _Resolver()
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 2)
        self.assertEqual(result.registered_aliases, 1)
        self.assertEqual(
            resolver.registered[0]["relationship"],
            "durable_automatic_verified_memory",
        )

    def test_conflicting_targets_and_static_alias_conflicts_are_rejected(self) -> None:
        conflicting = _Resolver()
        conflict_rows = [
            _mapping("server_1", "1", "Acme.News.us2"),
            _mapping("server_2", "2", "Other.News.us2"),
        ]
        result = integration._learn_cross_server_approved_aliases(
            resolver=conflicting,
            mapping_rows=conflict_rows,
            corroborated_real_candidates=[
                _candidate("Acme.News.us2"),
                _candidate("Other.News.us2"),
            ],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(conflict_rows),
        )
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.rejected_groups, 1)

    def test_untrusted_conflicting_auto_row_cannot_poison_human_memory(self) -> None:
        resolver = _Resolver()
        human = _mapping(
            "server_1", "1", "Acme.News.us2", action="APPROVED"
        )
        forged = _automatic_mapping(
            "server_2",
            "2",
            "Other.News.us2",
            notes="auto-map-v1 method=strict; forged=true",
        )
        rows = [human, forged]
        result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[
                _candidate("Acme.News.us2"),
                _candidate("Other.News.us2"),
            ],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )

        self.assertEqual(result.evidence_rows, 1)
        self.assertEqual(result.registered_aliases, 1)
        self.assertEqual(result.rejected_groups, 0)
        self.assertEqual(resolver.registered[0]["epg_ids"], "Acme.News.us2")

        static_same = _Resolver(
            approved_aliases=[
                {
                    "alias": "acme news",
                    "regions": ("ALL",),
                    "epg_ids": ("Acme.News.us2",),
                    "relationship": "pinned_static_rule",
                }
            ]
        )
        static_rows = [
            _mapping("server_1", "1", "Acme.News.us2"),
            _mapping("server_2", "2", "Acme.News.us2"),
        ]
        result = integration._learn_cross_server_approved_aliases(
            resolver=static_same,
            mapping_rows=static_rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(static_rows),
        )
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.static_reused_groups, 1)
        self.assertEqual(result.rejected_groups, 0)
        self.assertEqual(result.support_rows, ())
        self.assertEqual(static_same.registered, [])
        self.assertEqual(
            static_same.approved_aliases[0]["relationship"], "pinned_static_rule"
        )

        static_conflict = _Resolver(
            approved_aliases=[
                {
                    "alias": "acme news",
                    "regions": ("US",),
                    "epg_ids": ("Other.News.us2",),
                }
            ]
        )
        result = integration._learn_cross_server_approved_aliases(
            resolver=static_conflict,
            mapping_rows=static_rows,
            corroborated_real_candidates=[_candidate("Acme.News.us2")],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(static_rows),
        )
        self.assertEqual(result.registered_aliases, 0)
        self.assertEqual(result.rejected_groups, 1)

    def test_global_and_case_ambiguous_catalog_targets_are_not_evidence(self) -> None:
        resolver = _Resolver()
        rows = [
            _mapping("server_1", "1", "Acme.News.us2"),
            _mapping("server_2", "2", "Acme.News.us2"),
        ]
        global_result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[
                _candidate("Acme.News.us2", region="ALL")
            ],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )
        self.assertEqual(global_result.evidence_rows, 0)

        ambiguous_result = integration._learn_cross_server_approved_aliases(
            resolver=resolver,
            mapping_rows=rows,
            corroborated_real_candidates=[
                _candidate("Acme.News.us2"),
                _candidate("acme.news.us2"),
            ],
            quarantined_keys=frozenset(),
            current_unchanged_keys=_mapping_keys(rows),
        )
        self.assertEqual(ambiguous_result.evidence_rows, 0)
        self.assertEqual(resolver.registered, [])

    def test_prior_ai_rows_do_not_starve_later_shortlist(self) -> None:
        keys = [("server_1", str(index)) for index in range(1, 52)]
        proposals = {
            key: SimpleNamespace(
                server_id=key[0],
                stream_id=key[1],
                channel_name="US: Acme News",
                category_name="US | News",
                eligible_for_finalization=False,
                explicit_market="US",
                route_plan=("US",),
                route_explicit=True,
            )
            for key in keys
        }
        rows_by_key = {
            key: {
                "notes": "ai-review-v1 prior suggestion" if index <= 50 else ""
            }
            for index, key in enumerate(keys, start=1)
        }
        candidates = [
            {
                "epg_id": "Acme.News.One.us2",
                "display_name": "Acme News One",
                "feed": "US2",
                "region": "US",
                "normalized": "acme news one",
            },
            {
                "epg_id": "Acme.News.Two.us2",
                "display_name": "Acme News Two",
                "feed": "US2",
                "region": "US",
                "normalized": "acme news two",
            },
        ]

        without_exclusion = integration._stage_ai_review_shortlists(
            proposals=proposals,
            review_keys=keys,
            corroborated_real_candidates=candidates,
        )
        self.assertEqual(len(without_exclusion), 51)
        self.assertIn(("server_1", "51"), {item.key for item in without_exclusion})

        excluded = integration._previous_ai_review_keys(keys, rows_by_key)
        with_exclusion = integration._stage_ai_review_shortlists(
            proposals=proposals,
            review_keys=keys,
            ai_excluded_keys=excluded,
            corroborated_real_candidates=candidates,
        )
        self.assertEqual(excluded, frozenset())
        self.assertEqual(len(with_exclusion), 51)

    def test_learned_alias_still_requires_programme_gate(self) -> None:
        approved_rows = [
            {
                **_mapping(
                    "server_1", "1", "Good.Channel.us2", name="US: Odd Label"
                ),
                "epg_feed": "ALL_SOURCES1",
            },
            {
                **_mapping(
                    "server_2",
                    "2",
                    "Good.Channel.us2",
                    action="MANUAL",
                    source="epgshare",
                    name="US: Odd Label",
                ),
                "epg_feed": "ALL_SOURCES1",
            },
        ]
        review = {
            "server_id": "server_3",
            "stream_id": "3",
            "enabled": "FALSE",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": "",
            "channel_name": "US: Odd Label",
            "category_id": "cat",
            "category_name": "US | News",
            "notes": "",
        }
        inventories = [
            SimpleNamespace(
                server_id=server_id,
                categories=[{"category_id": "cat", "category_name": "US | News"}],
                channels=[
                    {
                        "stream_id": stream_id,
                        "category_id": "cat",
                        "category_name": "US | News",
                        "name": "US: Odd Label",
                    }
                ],
            )
            for server_id, stream_id in (
                ("server_1", "1"),
                ("server_2", "2"),
                ("server_3", "3"),
            )
        ]
        xml = (
            '<?xml version="1.0"?><tv>'
            '<channel id="Good.Channel.us2"><display-name>Good</display-name></channel>'
            '<programme channel="Good.Channel.us2" start="20260915130000 +0000" '
            'stop="20260915170000 +0000"><title>Only One</title></programme>'
            "</tv>"
        ).encode("utf-8")
        text_catalog = (
            "20260915120000\n-- epg_ripper_US2 --\nGood.Channel.us2\n"
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            with gzip.open(source, "wb") as handle:
                handle.write(xml)
            catalog.write_bytes(text_catalog)
            outcome = integration.auto_match_and_spool(
                mapping_rows=[*approved_rows, review],
                inventories=inventories,
                new_rows=[],
                review_rows=[review],
                all_source_file=source,
                all_source_catalog_file=catalog,
                spool_out=spool,
                generated_at="2026-09-15T12:00:00Z",
                minimum_unique_channels=1,
            )

        self.assertEqual(outcome.learned_alias_registered, 1)
        self.assertEqual(
            _mapping_keys(outcome.learned_alias_support_rows),
            frozenset({("server_1", "1"), ("server_2", "2")}),
        )
        self.assertNotIn("learned_alias_support_rows", outcome.summary_fields())
        self.assertEqual(outcome.recheck_approved_rows, 0)
        self.assertEqual(outcome.rejected_programme_gates, 1)
        self.assertEqual(outcome.rows[0]["action"], "REVIEW")
        self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
        self.assertEqual(outcome.rows[0]["epg_id"], "Good.Channel.us2")


if __name__ == "__main__":
    unittest.main()
