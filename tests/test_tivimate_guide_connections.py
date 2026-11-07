from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_epg_streaming as builder
import provider_guide_links as links
from tests.test_build_epg_streaming import _mapping_row, _mapping_bytes

NOW = 1_800_000_000


def mapping(name="UK || SKY SPORTS PLUS", sid="101", **overrides):
    raw = _mapping_row(server_id="server_1", stream_id=sid, channel_name=name,
                       category_name="SPORTS | SKY UK (4K)", epg_id="")
    raw.update(enabled="FALSE", action="REVIEW")
    raw.update(overrides)
    parsed = builder.parse_mapping_csv(_mapping_bytes([raw]), {raw["server_id"]}, require_enabled_servers=False)[0]
    return replace(parsed, reason=str(overrides.get("reason", "")))


def inventory(rows):
    values = [{"server_id": row.server_id, "stream_id": row.stream_id,
               "channel_name": row.channel_name, "category_name": row.category_name,
               "provider_playlist_epg_id_alias": epg_id,
               "provider_playlist_name_aliases": json.dumps([row.channel_name]),
               "provider_playlist_link_status": "verified" if epg_id else "name_only",
               "provider_playlist_identity_source": "player_api"} for row, epg_id in rows]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=values[0])
    writer.writeheader()
    writer.writerows(values)
    return out.getvalue().encode()


class GuideConnectionIntegrationTests(unittest.TestCase):
    def test_generic_identity_keeps_timeshift_and_channel_boundaries(self):
        for left, right in [("ITV +1", "ITV1"), ("BBC One +1", "BBC One 1"),
                            ("UK || SKY SPORTS +", "UK || SKY SPORTS")]:
            self.assertFalse(links.same_channel_name(left, right))
        self.assertTrue(links.same_channel_name("IN:  Aaj_Tak", "IN: Aaj_Tak"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = builder.create_database(Path(self.temp.name) / "test.sqlite")
        self.addCleanup(self.db.close)

    def programme(self, channel, start=NOW - 60, stop=NOW + 1800, title="Verified sport"):
        self.db.execute("INSERT OR IGNORE INTO channels VALUES(?,?,?,?,?)", ("epgshare01", channel, channel, channel, ""))
        self.db.execute("INSERT OR IGNORE INTO programmes VALUES(?,?,?,?,?,?,?,?,?,?)", ("epgshare01", channel, start, stop, title, "", "", "[]", 0, "{}"))
        if start == NOW - 60 and stop == NOW + 1800:
            self.db.execute("INSERT OR IGNORE INTO programmes VALUES(?,?,?,?,?,?,?,?,?,?)", ("epgshare01", channel, stop, NOW + 7 * 3600, "Next real sporting event", "", "", "[]", 0, "{}"))

    def overlay(self, rows, content=None):
        return links.apply_provider_playlist_aliases(rows, content or inventory([(r, "Provider  Exact.uk") for r in rows]),
            {r.server_id for r in rows}, quarantine_reason=builder.EFFECTIVE_QUARANTINE_REASON)

    def test_review_plus_gets_sport_not_4seven_and_exact_playlist_id(self):
        self.programme("SkySp+HD.uk")
        self.programme("4seven.uk", title="Gogglebox")
        rows, proof, _ = self.overlay([mapping()])
        rows, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertTrue(rows[0].runtime_eligible)
        self.assertEqual(rows[0].epg_id, "SkySp+HD.uk")
        stats = builder.load_schedule_stats(self.db, NOW)
        self.assertEqual(stats[("epgshare01", "SkySp+HD.uk")].current_rows, 1)
        entries, streams, _ = builder.build_xml_entries(rows=rows, connection=self.db, schedule_stats=stats, icon_overrides=builder.empty_icon_overrides())
        self.assertEqual(entries["Provider  Exact.uk"].channel_key, "SkySp+HD.uk")
        self.assertIn("101", streams)
        self.assertEqual(audit[0]["outcome"], "connected")

    def test_expired_or_far_future_schedule_cannot_activate_review(self):
        self.programme("SkySp+HD.uk", NOW - 3600, NOW)
        self.programme("SkySp+.uk", NOW + 86400, NOW + 90000)
        rows, proof, _ = self.overlay([mapping()])
        rows, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertFalse(rows[0].runtime_eligible)
        self.assertEqual(audit[0]["outcome"], "no_current_or_upcoming_schedule")

    def test_no_authenticated_identity_no_activation(self):
        row = mapping()
        self.programme("SkySp+HD.uk")
        rows, audit = builder.apply_reviewed_guide_connections([row], self.db, now_epoch=NOW, provider_links={})
        self.assertEqual(rows, [row])
        self.assertEqual(audit, [])

    def test_source_placeholder_rows_cannot_activate_a_real_guide(self):
        self.programme("SkySp+HD.uk", NOW - 60, NOW + 3 * 3600, "No information")
        self.programme("SkySp+HD.uk", NOW + 3 * 3600, NOW + 12 * 3600, "No information")
        rows, proof, _ = self.overlay([mapping()])
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertFalse(rows[0].runtime_eligible)

    def test_exclusions_and_both_quarantines_stay_excluded(self):
        self.programme("SkySp+HD.uk")
        for row in [mapping(action="IGNORE"), mapping(action="SKIP"),
                    mapping(reason=builder.EFFECTIVE_QUARANTINE_REASON), mapping(reason=builder.LEGACY_EFFECTIVE_QUARANTINE_REASON)]:
            rows, proof, _ = self.overlay([row])
            result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
            self.assertFalse(result[0].runtime_eligible)
            self.assertEqual(audit, [])

    def test_changed_identity_and_shared_ids_do_not_get_aliases(self):
        row = mapping()
        changed = replace(row, channel_name="UK || 4seven")
        rows, proof, _ = self.overlay([row], inventory([(changed, "4seven.uk")]))
        self.assertFalse(proof[("server_1", "101")]["identity_verified"])
        self.assertEqual(rows[0].provider_epg_id_aliases, ())
        output, proof, _ = self.overlay([row, mapping(sid="102")])
        self.assertTrue(all(not r.provider_epg_id_aliases for r in output))
        self.assertTrue(all(p["status"] == "shared_id" for p in proof.values()))

    def test_unsafe_attribute_is_flagged_without_losing_healthy_channels(self):
        bad, good = mapping(), mapping(sid="102")
        rows, proof, _ = self.overlay([bad, good], inventory([
            (bad, "https://provider.invalid/guide"), (good, "GoodProviderID"),
        ]))
        self.assertEqual(rows[0].provider_epg_id_aliases, ())
        self.assertEqual(proof[("server_1", "101")]["status"], "invalid_id")
        self.assertEqual(rows[1].provider_epg_id_aliases, ("GoodProviderID",))

    def test_shared_id_connects_only_when_every_member_has_same_real_schedule(self):
        hd = mapping(name="UK || SKY SPORTS PLUS HD", sid="101")
        fhd = mapping(name="UK || SKY SPORTS PLUS FHD", sid="102")
        content = inventory([(hd, ""), (fhd, "")]).decode()
        parsed = list(csv.DictReader(io.StringIO(content)))
        for record in parsed:
            record.update(provider_playlist_link_status="shared_id",
                          provider_playlist_shared_epg_id="SharedPlus.uk",
                          provider_playlist_shared_stream_count="2")
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=parsed[0])
        writer.writeheader()
        writer.writerows(parsed)
        rows, proof, _ = self.overlay([hd, fhd], out.getvalue().encode())
        self.programme("SkySp+HD.uk")
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        connected, count = links.apply_shared_playlist_aliases(rows, proof)
        self.assertEqual(count, 2)
        self.assertTrue(all(r.provider_epg_id_aliases == ("SharedPlus.uk",) for r in connected))
        proof[("server_1", "101")].update(status="shared_id", shared_stream_count=3)
        proof[("server_1", "102")].update(status="shared_id", shared_stream_count=3)
        _, count = links.apply_shared_playlist_aliases(rows, proof)
        self.assertEqual(count, 0)

    def test_bad_plus_provider_id_does_not_replace_4seven_canonical_guide(self):
        plus = mapping()
        four = mapping(name="4seven", sid="102", enabled="TRUE", action="APPROVED", epg_id="4seven.uk")
        self.programme("SkySp+HD.uk")
        self.programme("4seven.uk", title="Gogglebox")
        rows, proof, _ = self.overlay([plus, four], inventory([(plus, "4seven.uk"), (four, "FourSevenProvider")]))
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        stats = builder.load_schedule_stats(self.db, NOW)
        entries, _, conflicts = builder.build_xml_entries(rows=rows, connection=self.db,
            schedule_stats=stats, icon_overrides=builder.empty_icon_overrides())
        self.assertEqual(entries["4seven.uk"].channel_key, "4seven.uk")
        _, records = builder.tivimate_guide_health.build_guide_link_health(rows=rows, entries=entries,
            schedule_stats=stats, generated_at=NOW, provider_links=proof)
        self.assertEqual(records[0]["provider_epg_id_status"], "wrong_schedule")

    def test_server1_legacy_native_id_becomes_epgshare_only(self):
        row = mapping(enabled="TRUE", action="KEEP_PANEL", source="panel", epg_feed="PANEL", epg_id="wrong-native-id")
        self.programme("SkySp+HD.uk")
        rows, proof, _ = self.overlay([row])
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(rows[0].source_key, "epgshare01")
        self.assertEqual(rows[0].epg_id, "SkySp+HD.uk")
        self.assertFalse(rows[0].source_policy_blocked)
