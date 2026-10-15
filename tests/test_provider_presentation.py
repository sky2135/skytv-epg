from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import provider_presentation as presentation  # noqa: E402


def machine_heading() -> dict[str, str]:
    return {
        "enabled": "FALSE", "action": "IGNORE", "source": "dummy",
        "epg_feed": "DUMMY_CHANNELS", "epg_id": "Blank.Dummy.us",
        "channel_name": "#### USA CW #####", "category_name": "|NA| USA CW & MY",
        "reason": presentation.HEADING_CLASSIFICATION_REASON,
        "notes": "auto-map-v1 method=heading_placeholder; market=US; fixture",
    }


class ProviderPresentationTests(unittest.TestCase):
    def test_business_title_is_exact(self) -> None:
        self.assertEqual(
            presentation.BRAND_TITLE,
            "Welcome to SkyMedia IPTV - 💬 +12368462036",
        )

    def test_category_corroborated_heading_survives_decoration_and_font_styles(self) -> None:
        for name in (
            "#### USA CW #####", "## USA CW ##", "***** USA CW *****",
            "═══ USA CW ═══", "╔══ USA CW ══╗", "── USA CW ──",
            "◆◆◆ USA CW ◆◆◆", "＃＃＃ ＵＳＡ ＣＷ ＃＃＃",
            "### 𝗨𝗦𝗔 𝗖𝗪 ###", "### USA  CW ######",
            "### 𝙐𝙎𝘼 𝘾𝙒 ###",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    presentation.heading_identity(name, "|NA| USA CW & MY"),
                    "usa cw",
                )
                self.assertTrue(presentation.is_same_group_heading(
                    "#### USA CW #####", name, "|NA| USA CW & MY", "|NA| USA CW & MY"
                ))

    def test_normal_channels_dates_numbers_and_injection_lookalikes_are_rejected(self) -> None:
        for name, category in (
            ("USA CW", "|NA| USA CW & MY"),
            ("# USA CW #", "|NA| USA CW & MY"),
            ("### USA CW", "|NA| USA CW & MY"),
            ("USA CW ###", "|NA| USA CW & MY"),
            ("### CNN HD ###", "|NA| USA NEWS"),
            ("### USA CW 1 ###", "|NA| USA CW & MY"),
            ("### USA CW 2026-10-15 ###", "|NA| USA CW & MY"),
            ("### USA CW | Event 001 ###", "|NA| USA CW EVENTS"),
            ("### <USA CW> ###", "|NA| USA CW & MY"),
            ("### https://USA/CW ###", "|NA| USA CW & MY"),
            ("### USA CW; alert ###", "|NA| USA CW alert"),
            ("### USA CW\u202e ###", "|NA| USA CW & MY"),
            ("### USA\nCW ###", "|NA| USA CW & MY"),
            ("### USA CW ###", ""),
            ("### USA CW ###", "|NA| USA NEWS"),
            ("### ADULT ###", "ADULT"),
            ("### USA CW ###", "|NA| USA CW ADULT"),
        ):
            with self.subTest(name=name, category=category):
                self.assertIsNone(presentation.heading_identity(name, category))

    def test_category_and_inner_identity_changes_are_not_stylistic(self) -> None:
        for new_name, old_category, new_category in (
            ("### USA MY ###", "|NA| USA CW & MY", "|NA| USA CW & MY"),
            ("### USA CW ###", "|NA| USA CW & MY", "|NA| USA CW"),
            ("### USA CW ###", "|NA| USA CW & MY", "|CA| USA CW & MY"),
            ("### USA CW ###", "|NA| USA CW & MY", "|NA| USA CW & MY ADULT"),
        ):
            with self.subTest(new_name=new_name, category=new_category):
                self.assertFalse(presentation.is_same_group_heading(
                    "#### USA CW #####", new_name, old_category, new_category
                ))

    def test_only_exact_supported_numeric_category_labels_are_headings(self) -> None:
        for name, category, expected in (
            ("### 4K UHD ###", "4K UHD", "4k uhd"),
            ("### 8K ###", "|US| 8K", "8k"),
            ("### 24 / 7 MOVIES ###", "24/7 MOVIES", "24/7 movies"),
            ("### 4K UHD ###", "8K UHD", None),
            ("### ESPN 4K 001 ###", "ESPN 4K 001", None),
            ("### NFL 24/7 01 ###", "NFL 24/7 01", None),
            ("### 2026/10/15 ###", "2026/10/15", None),
        ):
            with self.subTest(name=name, category=category):
                self.assertEqual(presentation.heading_identity(name, category), expected)

    def test_legacy_enabled_dummy_heading_requires_machine_provenance(self) -> None:
        row = machine_heading()
        row.update({
            "enabled": "TRUE", "action": "AUTO_DUMMY",
            "reason": presentation.HEADING_LEGACY_CLASSIFICATION_REASON,
        })
        self.assertTrue(presentation.is_machine_group_heading(row))
        empty_notes = dict(row, notes="")
        self.assertTrue(presentation.is_machine_group_heading(empty_notes))
        self.assertTrue(presentation.is_machine_group_heading(dict(empty_notes, epg_id=" Blank.Dummy.US ", epg_feed=" dummy_channels ", source=" DUMMY ")))
        self.assertFalse(presentation.is_machine_group_heading(dict(empty_notes, notes="manually disabled advertising")))
        for changes in ({"enabled": "FALSE"}, {"notes": "manual heading"}, {"reason": "manual selection"}):
            with self.subTest(changes=changes):
                self.assertFalse(presentation.is_machine_group_heading(dict(row, **changes)))

    def test_machine_proof_permits_unrelated_category_without_widening_default(self) -> None:
        row = machine_heading()
        row["category_name"] = "|NA| USA LOCAL"
        self.assertIsNone(presentation.heading_identity(row["channel_name"], row["category_name"]))
        self.assertTrue(presentation.is_machine_group_heading(row))
        self.assertFalse(presentation.is_same_group_heading(
            row["channel_name"], "*** 𝗨𝗦𝗔 𝗖𝗪 ***", row["category_name"], row["category_name"]
        ))
        self.assertTrue(presentation.is_same_group_heading(
            row["channel_name"], "*** 𝗨𝗦𝗔 𝗖𝗪 ***", row["category_name"], row["category_name"],
            require_category_corroboration=False,
        ))
        self.assertFalse(presentation.is_same_group_heading(
            row["channel_name"], "*** 𝗨𝗦𝗔 𝗖𝗪 ***", row["category_name"], "|NA| OTHER",
            require_category_corroboration=False,
        ))
        for changes in ({"reason": "Operator chose IGNORE"}, {"notes": "manual heading"}, {"action": "MANUAL"}):
            with self.subTest(changes=changes):
                self.assertFalse(presentation.is_machine_group_heading(dict(row, **changes)))

    def test_machine_proof_does_not_admit_events_urls_adult_or_ordinary_labels(self) -> None:
        for name in ("USA CW", "### USA CW 001 ###", "### 2026-10-16 ###", "### https://USA/CW ###", "### ADULT ###"):
            with self.subTest(name=name):
                self.assertIsNone(presentation.heading_identity(
                    name, "|NA| USA LOCAL", require_category_corroboration=False,
                ))

    def test_only_explicit_machine_ignore_classification_qualifies(self) -> None:
        original = machine_heading()
        self.assertTrue(presentation.is_machine_group_heading(original))
        for changes in (
            {"enabled": "TRUE"}, {"enabled": ""}, {"action": "MANUAL"},
            {"action": "REVIEW"}, {"source": "epgshare01"},
            {"epg_feed": "ALL_SOURCES1"}, {"epg_id": "CW.us2"},
            {"reason": "Operator chose IGNORE"},
            {"reason": "Possible stream reuse: quarantined"},
            {"notes": "Operator quoted auto-map-v1 method=heading_placeholder; fixture"},
            {"notes": "auto-map-v1 method=canonical_identity; fixture"},
            {"genre": "adult"}, {"content_rating": "adult"},
        ):
            with self.subTest(changes=changes):
                row = dict(original, **changes)
                self.assertFalse(presentation.is_machine_group_heading(row))


if __name__ == "__main__":
    unittest.main()
