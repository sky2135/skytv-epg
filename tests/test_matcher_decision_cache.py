from __future__ import annotations

import hashlib
import hmac
import gzip
import base64
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "src", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import auto_match_inventory as integration
import matcher_decision_cache as cache
from test_auto_match_inventory import (
    GENERATED_AT, RuntimeFactory, inventory, mapping_row,
    text_catalog_bytes, xml_bytes,
)


class MatcherDecisionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cache_dir = self.base / "cache"
        self.run_number = 0
        self.environment = mock.patch.dict(os.environ, {
            "SKYTV_MATCH_CACHE_DIR": str(self.cache_dir),
            "SKYTV_MATCH_CACHE_KEY": "fixture private cache key of sufficient length",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_match(self, *, strong=True, provider=None, factory=None,
                  include_old=False, quarantined_keys=(), new_rows=None):
        factory = factory or RuntimeFactory()
        self.run_number += 1
        run_dir = self.base / f"run-{self.run_number}"
        run_dir.mkdir()
        source, text, spool = (run_dir / "all.xml.gz", run_dir / "all.txt",
                               run_dir / "selected.sqlite3")
        source.write_bytes(gzip.compress(xml_bytes(strong=strong, include_old=include_old), mtime=0))
        text.write_bytes(text_catalog_bytes(
            "Good.Channel.us2", *(("Old.Channel.us2",) if include_old else ())
        ))
        # Windows stat(path) and fstat(fd) can report different creation/change
        # timestamps for a new file. Immutable-input checks have their own
        # tests; these cache tests use known fixture bytes on that platform.
        fixture_bytes = text.read_bytes()
        reader = (mock.patch.object(integration, "_read_bound_text_catalog", return_value=(
            fixture_bytes, hashlib.sha256(fixture_bytes).hexdigest()
        )) if os.name == "nt" else nullcontext())
        with reader:
            result = integration.auto_match_and_spool(
                mapping_rows=[], inventories=[provider or inventory()],
                new_rows=(new_rows if new_rows is not None else [mapping_row(
                    server_id="server_1", stream_id="new-1", channel_name="US: Good Channel"
                )]),
                all_source_file=source, all_source_catalog_file=text,
                spool_out=spool, generated_at=GENERATED_AT,
                minimum_unique_channels=1, runtime_factory=factory,
                quarantined_keys=quarantined_keys,
            )
        return result, factory

    def envelope_path(self):
        return next(self.cache_dir.glob("*.json"))

    def resign(self, envelope):
        key = cache._environment_key()
        envelope["signature"] = hmac.new(
            key, cache._encoded(envelope["payload"]), hashlib.sha256
        ).hexdigest()
        self.envelope_path().write_bytes(cache._encoded(envelope))

    def test_warm_results_equal_cold_and_skip_resolver(self):
        cold, first = self.run_match()
        warm, second = self.run_match()
        self.assertEqual(cold.rows, warm.rows)
        self.assertEqual(cold.provisional_rows, warm.provisional_rows)
        self.assertEqual(cold.approved_rows, warm.approved_rows)
        self.assertEqual(len(first.resolver.calls), 1)
        self.assertEqual(second.resolver.calls, [])
        self.assertEqual(warm.matcher_cache_stats["hits"], 1)
        self.assertEqual(warm.matcher_cache_stats["misses"], 0)
        envelope = json.loads(self.envelope_path().read_bytes())
        record = envelope["payload"]["entries"]["new-1"]
        self.assertNotIn("catalog_source_sha256", record)
        self.assertNotIn("matcher_identity", record)
        self.assertNotIn("enabled", record)

    def test_warm_proposal_still_loses_current_programme_approval(self):
        strong, _ = self.run_match(strong=True)
        weak, factory = self.run_match(strong=False)
        self.assertEqual(weak.matcher_cache_stats["hits"], 1)
        self.assertEqual(factory.resolver.calls, [])
        self.assertNotEqual(strong.source_sha256, weak.source_sha256)
        self.assertEqual(strong.rows[0]["enabled"], "TRUE")
        self.assertEqual(weak.rows[0]["enabled"], "FALSE")
        self.assertEqual(weak.rows[0]["action"], "REVIEW")
        self.assertEqual(weak.rejected_programme_gates, 1)

    def test_negative_results_are_memoized_without_changing_abstention(self):
        cold, _ = self.run_match(factory=RuntimeFactory(target_id=""))
        warm, factory = self.run_match(factory=RuntimeFactory(target_id=""))
        self.assertEqual(cold.rows, warm.rows)
        self.assertEqual(warm.rows[0]["enabled"], "FALSE")
        self.assertEqual(warm.matcher_cache_stats["hits"], 1)
        self.assertEqual(factory.resolver.calls, [])

    def test_partial_cache_keeps_full_lineup_profiles_for_new_targets(self):
        provider = inventory()
        provider.channels.append({**provider.channels[0], "stream_id": "new-2"})
        first, _ = self.run_match(provider=provider)
        rows = [mapping_row(server_id="server_1", stream_id=stream_id,
                            channel_name="US: Good Channel")
                for stream_id in ("new-1", "new-2")]
        mixed, factory = self.run_match(provider=provider, new_rows=rows)
        self.assertEqual(mixed.matcher_cache_stats["hits"], 1)
        self.assertEqual(mixed.matcher_cache_stats["misses"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)
        self.assertEqual(factory.resolver.calls[0]["category_profile"], {"count": 2})
        with mock.patch.dict(os.environ, {"SKYTV_MATCH_CACHE_DIR": ""}):
            uncached, _ = self.run_match(provider=provider, new_rows=rows)
        self.assertEqual(mixed.rows, uncached.rows)

    def test_warm_cache_reapplies_current_catalog_ambiguity_veto(self):
        self.run_match()
        with mock.patch.object(integration, "_same_market_family_ambiguity_ids",
                               return_value=frozenset({"Good.Channel.us2"})):
            blocked, factory = self.run_match()
        self.assertEqual(blocked.matcher_cache_stats["hits"], 1)
        self.assertEqual(factory.resolver.calls, [])
        self.assertEqual(blocked.rows[0]["enabled"], "FALSE")

    def test_full_lineup_and_native_id_changes_invalidate(self):
        variants = []
        renamed = inventory()
        renamed.channels[0]["name"] = "US: Renamed Channel"
        variants.append(renamed)
        recategorized = inventory()
        recategorized.categories[0]["category_name"] = "US | Different category"
        variants.append(recategorized)
        native_changed = inventory(panel_id="new.native.identity")
        variants.append(native_changed)
        lineup_changed = inventory()
        lineup_changed.channels.append({**lineup_changed.channels[0],
                                        "stream_id": "unselected-extra"})
        variants.append(lineup_changed)
        for provider in variants:
            with self.subTest(provider=provider.channels):
                self.run_match()
                changed, factory = self.run_match(provider=provider)
                self.assertEqual(changed.matcher_cache_stats["hits"], 0)
                self.assertEqual(changed.matcher_cache_stats["invalidations"], 1)
                self.assertEqual(len(factory.resolver.calls), 1)

    def test_catalog_and_quarantine_changes_invalidate(self):
        self.run_match()
        catalog_changed, factory = self.run_match(include_old=True)
        self.assertEqual(catalog_changed.matcher_cache_stats["invalidations"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)
        self.run_match()
        quarantine_changed, factory = self.run_match(
            quarantined_keys={("server_3", "not-a-target")}
        )
        self.assertEqual(quarantine_changed.matcher_cache_stats["invalidations"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)

    def test_knowledge_actual_alias_and_rules_changes_invalidate(self):
        self.run_match()
        factory = RuntimeFactory()
        original = factory.__class__.__call__

        def changed_knowledge(instance, *args):
            return replace(original(instance, *args), approved_aliases_sha256="c" * 64)

        with mock.patch.object(RuntimeFactory, "__call__", changed_knowledge):
            changed, current = self.run_match(factory=factory)
        self.assertEqual(changed.matcher_cache_stats["invalidations"], 1)
        self.assertEqual(len(current.resolver.calls), 1)
        self.run_match()
        factory = RuntimeFactory()
        factory.resolver.approved_aliases = [{"alias": "Changed", "regions": ("US",),
                                              "epg_ids": ("Good.Channel.us2",)}]
        changed, _ = self.run_match(factory=factory)
        self.assertEqual(changed.matcher_cache_stats["invalidations"], 1)
        self.run_match()
        with mock.patch.object(cache, "rules_sha256", return_value="d" * 64):
            changed, factory = self.run_match()
        self.assertEqual(changed.matcher_cache_stats["invalidations"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)

    def test_learned_alias_dependency_changes_invalidate(self):
        self.run_match()
        original = integration._learn_cross_server_approved_aliases

        def changed(*args, **kwargs):
            return replace(original(*args, **kwargs), sha256="e" * 64)

        with mock.patch.object(integration, "_learn_cross_server_approved_aliases", changed):
            result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["invalidations"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)

    def test_tamper_cannot_supply_an_arbitrary_positive(self):
        self.run_match()
        envelope = json.loads(self.envelope_path().read_bytes())
        envelope["payload"]["entries"]["new-1"]["target_epg_id"] = "Attacker.Channel.us2"
        self.envelope_path().write_bytes(cache._encoded(envelope))
        result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["rejectedFiles"], 1)
        self.assertEqual(result.matcher_cache_stats["hits"], 0)
        self.assertEqual(len(factory.resolver.calls), 1)
        self.assertEqual(result.rows[0]["epg_id"], "Good.Channel.us2")

    def test_signed_malformed_schema_and_oversized_files_recompute(self):
        self.run_match()
        envelope = json.loads(self.envelope_path().read_bytes())
        envelope["payload"]["entries"]["new-1"]["eligible_for_finalization"] = "true"
        self.resign(envelope)
        result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["rejectedFiles"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)
        with mock.patch.object(cache, "MAX_FILE_BYTES", 10):
            result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["rejectedFiles"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)

    def test_corrupt_file_and_wrong_key_recompute(self):
        self.run_match()
        self.envelope_path().write_bytes(b"broken JSON")
        result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["rejectedFiles"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)
        with mock.patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "a different private signing key"}):
            result, factory = self.run_match()
        self.assertEqual(result.matcher_cache_stats["rejectedFiles"], 1)
        self.assertEqual(len(factory.resolver.calls), 1)

    def test_missing_key_disables_cache_instead_of_unsigned_positive(self):
        with mock.patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "",
                                          "GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            result, first = self.run_match()
            result, second = self.run_match()
        self.assertEqual(result.matcher_cache_stats["enabled"], 0)
        self.assertEqual(len(first.resolver.calls), 1)
        self.assertEqual(len(second.resolver.calls), 1)
        self.assertFalse(self.cache_dir.exists())

    def test_existing_google_credential_derives_stable_private_key(self):
        account = {"private_key": "fixture account private key of sufficient length",
                   "client_email": "fixture@example.invalid"}
        with mock.patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "",
                                          "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(account)}):
            self.run_match()
            account["client_email"] = "another@example.invalid"
            with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(account, indent=2)}):
                warm, factory = self.run_match()
        self.assertEqual(warm.matcher_cache_stats["hits"], 1)
        self.assertEqual(factory.resolver.calls, [])

    def test_base64_google_credential_uses_the_same_signing_key(self):
        account = json.dumps({"private_key": "fixture account private key of sufficient length"})
        with mock.patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "",
                                          "GOOGLE_SERVICE_ACCOUNT_JSON": account}):
            self.run_match()
            with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": base64.b64encode(
                account.encode("utf-8")
            ).decode("ascii")}):
                warm, factory = self.run_match()
        self.assertEqual(warm.matcher_cache_stats["hits"], 1)
        self.assertEqual(factory.resolver.calls, [])

    def test_transient_resolver_failures_are_not_memoized(self):
        factory = RuntimeFactory()
        factory.resolver.resolve = mock.Mock(side_effect=RuntimeError("temporary failure"))
        failed, _ = self.run_match(factory=factory)
        self.assertEqual(failed.rows[0]["enabled"], "FALSE")
        recovered, current = self.run_match()
        self.assertEqual(recovered.matcher_cache_stats["hits"], 0)
        self.assertEqual(len(current.resolver.calls), 1)
        self.assertEqual(recovered.rows[0]["enabled"], "TRUE")

    def test_private_cache_write_error_does_not_break_matching(self):
        self.cache_dir.write_bytes(b"not a directory")
        result, factory = self.run_match()
        self.assertEqual(len(factory.resolver.calls), 1)
        self.assertEqual(result.rows[0]["enabled"], "TRUE")
        self.assertEqual(result.matcher_cache_stats["writeErrors"], 1)


if __name__ == "__main__":
    unittest.main()
