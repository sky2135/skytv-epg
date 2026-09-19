from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import icon_variant_rules as rules  # noqa: E402


class IconVariantRuleTests(unittest.TestCase):
    def test_variant_families_are_fixed(self) -> None:
        self.assertEqual(
            rules.MOVIE_VARIANTS,
            (
                "movie-clapperboard",
                "movie-reel",
                "movie-projector",
                "movie-filmstrip",
                "movie-ticket",
            ),
        )
        self.assertEqual(
            rules.MUSIC_VARIANTS,
            (
                "music-notes",
                "music-microphone",
                "music-headphones",
                "music-guitar",
                "music-dhol",
                "music-sitar",
            ),
        )

    def test_numbered_names_and_resolution_labels_share_a_pattern(self) -> None:
        expected = rules.series_pattern_key(
            "English Movies 24/7", "English Adventure Movies 1 HD"
        )
        for name in (
            "English Adventure Movies 2 FHD",
            "ENGLISH-ADVENTURE_MOVIES_3 (1080p)",
            "English Adventure Movies 4 HEVC 4K",
            "English Adventure Movies 5 24/7 UHD",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    rules.series_pattern_key("English Movies 24/7", name),
                    expected,
                )

        self.assertEqual(
            rules.select_movie_variant(
                "English Movies 24/7", "English Adventure Movies 1 HD"
            ),
            rules.select_movie_variant(
                "English Movies 24/7", "English Adventure Movies 9 UHD"
            ),
        )

    def test_explicit_part_numbers_group_but_years_remain_distinct(self) -> None:
        self.assertEqual(
            rules.normalize_series_name("Tamil Collection Vol. 1 UHD"),
            rules.normalize_series_name("Tamil Collection Volume 2 HD"),
        )
        self.assertNotEqual(
            rules.normalize_series_name("Movies 2025"),
            rules.normalize_series_name("Movies 2026"),
        )

    def test_movie_semantics_use_only_the_movie_family(self) -> None:
        cases = {
            "Premium Box Office 1": "movie-ticket",
            "Classic Movies 1": "movie-reel",
            "Action Thriller Movies 1": "movie-clapperboard",
            "Family Comedy Movies 1": "movie-filmstrip",
            "Romance Drama Movies 1": "movie-projector",
        }
        for channel_name, expected in cases.items():
            with self.subTest(channel_name=channel_name):
                chosen = rules.select_movie_variant("Movies 24/7", channel_name)
                self.assertEqual(chosen, expected)
                self.assertIn(chosen, rules.MOVIE_VARIANTS)

    def test_music_category_semantics_and_named_singer_fallback(self) -> None:
        cases = (
            ("Punjabi Music", "Bhangra Hits", False, "music-dhol"),
            ("Music", "Classic Rock", False, "music-guitar"),
            ("Music", "Acoustic Country", False, "music-guitar"),
            ("Ghazal", "Classical Qawwali", False, "music-sitar"),
            ("Devotional Music", "Morning Bhajans", False, "music-sitar"),
            ("Electronic Music", "Dance DJ", False, "music-headphones"),
            ("US Music Choice", "Smooth Jazz", False, "music-notes"),
            ("Music", "Deluxe Rap (1080p)", False, "music-microphone"),
            ("US Music Choice", "Pop Hits", False, "music-notes"),
            ("Punjabi Singers 24/7", "Named Singer", True, "music-microphone"),
        )
        for category, channel, named_singer, expected in cases:
            with self.subTest(category=category, channel=channel):
                chosen = rules.select_music_variant(
                    category, channel, named_singer=named_singer
                )
                self.assertEqual(chosen, expected)
                self.assertIn(chosen, rules.MUSIC_VARIANTS)

    def test_non_semantic_selection_is_sha256_and_order_independent(self) -> None:
        records = [
            ("Movies 24/7", "Aurora Movies 1"),
            ("Movies 24/7", "Nebula Features 1"),
            ("Movies 24/7", "Orchid Screen 1"),
        ]
        forward = {
            item: rules.select_movie_variant(*item)
            for item in records
        }
        reverse = {
            item: rules.select_movie_variant(*item)
            for item in reversed(records)
        }
        self.assertEqual(forward, reverse)

        for item in records:
            key = rules.series_pattern_key(*item)
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            expected = rules.MOVIE_VARIANTS[
                int.from_bytes(digest[:8], "big") % len(rules.MOVIE_VARIANTS)
            ]
            self.assertEqual(forward[item], expected)

    def test_ordered_pattern_boundaries_receive_different_movie_icons(self) -> None:
        records = [
            ("server_1|movies", "Movies", "Movie 1 HD"),
            ("server_1|movies", "Movies", "Movie 2 4K"),
            ("server_1|movies", "Movies", "Cinema 1"),
            ("server_1|movies", "Movies", "Cinema 2"),
            ("server_1|movies", "Movies", "Classic Film 1"),
            ("server_1|movies", "Movies", "Classic Film 2"),
        ]
        allocated = rules.allocate_movie_variants(records)
        patterns = [
            rules.series_pattern_key(category, channel)
            for _scope, category, channel in records[::2]
        ]
        chosen = [allocated[("server_1|movies", pattern)] for pattern in patterns]
        self.assertEqual(len(set(chosen[:2])), 2)
        self.assertNotEqual(chosen[1], chosen[2])
        self.assertEqual(
            allocated[
                (
                    "server_1|movies",
                    rules.series_pattern_key("Movies", "Movie 1 HD"),
                )
            ],
            allocated[
                (
                    "server_1|movies",
                    rules.series_pattern_key("Movies", "Movie 2 4K"),
                )
            ],
        )

    def test_category_scope_and_unknown_family_rejection(self) -> None:
        self.assertNotEqual(
            rules.series_pattern_key("Hindi Movies", "Movie 1"),
            rules.series_pattern_key("English Movies", "Movie 1"),
        )
        self.assertIn(
            rules.select_icon_variant("movies", "Hindi Movies", "Movie 1"),
            rules.MOVIE_VARIANTS,
        )
        with self.assertRaisesRegex(ValueError, "Unsupported icon family"):
            rules.select_icon_variant("sports", "Sports", "Channel 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
