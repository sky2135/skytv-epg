from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_vod_catalogs as vod  # noqa: E402


NOW = "2026-10-02T16:30:00Z"


def sample_source() -> dict[str, object]:
    return {
        "get_vod_categories": [
            {"category_id": "10", "category_name": "English Action Movies"},
            {"category_id": "20", "category_name": "Unclassified"},
        ],
        "get_vod_streams": [
            {
                "stream_id": 101,
                "name": "Example Film",
                "category_id": "10",
                "stream_icon": "https://images.example/poster.jpg?account=private",
                "direct_source": "http://provider.invalid/movie/user/password/101.mp4",
                "container_extension": "mp4",
            },
            {"stream_id": 102, "name": "Unknown Film", "category_id": "20"},
        ],
        "vod_details": {
            "101": {
                "movie_data": {"stream_id": 101},
                "info": {
                    "genre": "Action, Drama",
                    "release_date": "2025-04-03",
                    "adult": False,
                    "tmdb_id": 98765,
                    "poster_path": "/tmdb-poster.jpg",
                    "backdrop_path": ["/tmdb-backdrop.jpg"],
                },
            }
        },
        "get_series_categories": [
            {"category_id": "30", "category_name": "Spanish Adult Series"}
        ],
        "get_series": [
            {
                "series_id": "201",
                "name": "Example Series",
                "category_id": "30",
                "genre": "Thriller",
                "releaseDate": "2024",
                "cover": "https://cdn.example/series.jpg?token=discard-me",
            }
        ],
        "series_details": {"201": {"info": {"tmdb": "45678"}}},
    }


class VodNormalizationTests(unittest.TestCase):
    def test_required_metadata_is_explicit_without_publishing_playback_data(self) -> None:
        source = sample_source()
        movies, stats = vod.normalize_catalog(
            source["get_vod_streams"],
            source["get_vod_categories"],
            source["vod_details"],
            kind="movie",
            label="test movies",
        )

        self.assertEqual([row["id"] for row in movies], ["101", "102"])
        first = movies[0]
        self.assertEqual(first["language"], "en")
        self.assertEqual(first["languages"], ["en"])
        self.assertEqual(first["genre"], "action")
        self.assertEqual(first["genres"], ["action", "drama"])
        self.assertEqual(first["releaseDate"], "2025-04-03")
        self.assertIs(first["adult"], False)
        self.assertEqual(first["tmdbId"], "98765")
        self.assertEqual(
            first["artwork"],
            {
                "posterUrl": "https://image.tmdb.org/t/p/w500/tmdb-poster.jpg",
                "backdropUrl": "https://image.tmdb.org/t/p/original/tmdb-backdrop.jpg",
            },
        )
        self.assertEqual(movies[1]["language"], "und")
        self.assertEqual(movies[1]["genre"], "unknown")
        self.assertIsNone(movies[1]["releaseDate"])
        self.assertIsNone(movies[1]["adult"])
        self.assertIsNone(movies[1]["tmdbId"])
        encoded = json.dumps(movies)
        self.assertNotIn("direct_source", encoded)
        self.assertNotIn("container_extension", encoded)
        self.assertNotIn("provider.invalid", encoded)
        self.assertEqual(stats["completeness"]["tmdbIdKnown"], 1)

    def test_category_evidence_classifies_series_without_country_guessing(self) -> None:
        source = sample_source()
        series, _ = vod.normalize_catalog(
            source["get_series"],
            source["get_series_categories"],
            source["series_details"],
            kind="series",
            label="test series",
        )

        self.assertEqual(len(series), 1)
        item = series[0]
        self.assertEqual(item["language"], "es")
        self.assertEqual(item["genre"], "thriller")
        self.assertIn("adult", item["genres"])
        self.assertIs(item["adult"], True)
        self.assertEqual(item["releaseDate"], "2024")
        self.assertEqual(item["tmdbId"], "45678")
        self.assertEqual(
            item["artwork"]["posterUrl"], "https://cdn.example/series.jpg"
        )

    def test_strong_adult_evidence_overrides_contradictory_false(self) -> None:
        rows = [{"stream_id": "1", "name": "Film", "category_id": "9", "is_adult": 0}]
        categories = [{"category_id": "9", "category_name": "XXX Adult Movies"}]
        items, _ = vod.normalize_catalog(
            rows, categories, {}, kind="movie", label="adult movies"
        )
        self.assertIs(items[0]["adult"], True)
        self.assertIn("adult", items[0]["genres"])

        for unsafe_value in (False, None):
            with self.subTest(unsafe_value=unsafe_value):
                tampered = {**items[0], "adult": unsafe_value}
                with self.assertRaisesRegex(vod.CatalogBuildError, "strong adult evidence"):
                    vod._validate_catalog_item(tampered, label="tampered movie")

        genre_items, _ = vod.normalize_catalog(
            [{"stream_id": "2", "name": "Film", "genre": "Adult"}],
            [],
            {},
            kind="movie",
            label="adult genre movies",
        )
        self.assertIs(genre_items[0]["adult"], True)

    def test_multiple_explicit_languages_are_multilingual(self) -> None:
        rows = [{"stream_id": "1", "name": "Film", "language": "English / French"}]
        items, _ = vod.normalize_catalog(
            rows, [], {}, kind="movie", label="bilingual movies"
        )
        self.assertEqual(items[0]["language"], "mul")
        self.assertEqual(items[0]["languages"], ["en", "fr"])

    def test_unnamed_multilingual_evidence_stays_unknown(self) -> None:
        for raw_language in ("English bilingual", "Multilingual"):
            with self.subTest(raw_language=raw_language):
                items, _ = vod.normalize_catalog(
                    [{"stream_id": "1", "name": "Film", "language": raw_language}],
                    [],
                    {},
                    kind="movie",
                    label="incomplete multilingual movies",
                )
                self.assertEqual(items[0]["language"], "und")
                self.assertEqual(items[0]["languages"], ["und"])

    def test_conflicting_duplicate_provider_ids_fail_closed(self) -> None:
        rows = [
            {"stream_id": "1", "name": "First"},
            {"stream_id": "1", "name": "Different"},
        ]
        with self.assertRaisesRegex(vod.CatalogBuildError, "conflicting rows"):
            vod.normalize_catalog(rows, [], {}, kind="movie", label="movies")

    def test_artwork_rejects_credentials_and_removes_queries(self) -> None:
        self.assertIsNone(
            vod.normalize_artwork_url(
                "https://user:pass@example.test/poster.jpg", tmdb_size="w500"
            )
        )
        self.assertEqual(
            vod.normalize_artwork_url(
                "https://example.test/poster.jpg?token=sensitive#part",
                tmdb_size="w500",
            ),
            "https://example.test/poster.jpg",
        )
        self.assertIsNone(
            vod.normalize_artwork_url(
                "https://cdn.provider.invalid/poster.jpg",
                tmdb_size="w500",
                blocked_hostname="provider.invalid",
            )
        )

    def test_panel_base_requires_a_credential_free_https_url(self) -> None:
        self.assertEqual(
            vod.normalize_base_url(
                "https://EXAMPLE.test:8443/player_api.php",
                allow_insecure_http=False,
            ),
            "https://example.test:8443",
        )
        with self.assertRaises(vod.CatalogBuildError):
            vod.normalize_base_url(
                "https://user:password@example.test/player_api.php",
                allow_insecure_http=False,
            )
        with self.assertRaises(vod.CatalogBuildError):
            vod.normalize_base_url(
                "http://example.test", allow_insecure_http=False
            )


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        redirect: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.is_redirect = redirect
        self.is_permanent_redirect = redirect
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int) -> object:
        del chunk_size
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


class FailingStreamResponse(FakeResponse):
    def iter_content(self, chunk_size: int) -> object:
        del chunk_size
        yield b'{"partial":'
        raise vod.requests.exceptions.ChunkedEncodingError(
            "failed at https://provider.invalid/player_api.php?username=user&password=password"
        )


class VodTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = vod.ServerConfig(
            server_id="server_1",
            base_url="https://provider.invalid",
            username="user",
            password="password",
        )

    def test_panel_fetch_is_bounded_and_closes_the_response(self) -> None:
        response = FakeResponse([b"123", b"45"])
        session = FakeSession(response)
        with mock.patch.object(vod, "MAX_RESPONSE_BYTES", 4):
            with self.assertRaisesRegex(vod.CatalogBuildError, "too large"):
                vod.request_panel_json(session, self.config, "get_vod_streams")
        self.assertTrue(response.closed)
        _, kwargs = session.calls[0]
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["stream"], True)

    def test_panel_redirect_is_rejected_without_echoing_location(self) -> None:
        secret_location = "https://other.invalid/?password=secret"
        response = FakeResponse(
            [], redirect=True, headers={"Location": secret_location}
        )
        with self.assertRaises(vod.CatalogBuildError) as caught:
            vod.request_panel_json(
                FakeSession(response), self.config, "get_vod_streams"
            )
        self.assertNotIn(secret_location, str(caught.exception))
        self.assertTrue(response.closed)

    def test_streaming_failure_is_sanitized_and_closes_response(self) -> None:
        response = FailingStreamResponse([])
        with self.assertRaises(vod.CatalogBuildError) as caught:
            vod.request_panel_json(
                FakeSession(response), self.config, "get_vod_streams"
            )
        self.assertEqual(
            str(caught.exception),
            "server_1: provider response download failed.",
        )
        self.assertNotIn("username", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))
        self.assertTrue(response.closed)

    def test_live_empty_catalog_cannot_replace_the_existing_release(self) -> None:
        responses = {
            "get_vod_categories": [],
            "get_vod_streams": [],
            "get_series_categories": [],
            "get_series": [],
        }
        with mock.patch.object(
            vod,
            "request_panel_json",
            side_effect=lambda _session, _config, action, **_kwargs: responses[action],
        ):
            with self.assertRaisesRegex(vod.CatalogBuildError, "refusing to replace"):
                vod.fetch_live_inputs(
                    mock.Mock(), self.config, fetch_details=False
                )


class VodPublishingTests(unittest.TestCase):
    def test_live_publication_removes_provider_hosted_artwork(self) -> None:
        source = sample_source()
        source["get_series"][0]["cover"] = (
            "https://cdn.provider.invalid/posters/series.jpg?token=private"
        )
        config = vod.ServerConfig(
            server_id="server_1",
            base_url="https://provider.invalid",
            username="account-name",
            password="very-secret-password",
        )
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            vod.publish_catalogs(
                output_dir,
                [("server_1", source, config)],
                generated_at=NOW,
            )
            index = json.loads((output_dir / "index.json").read_text("utf-8"))
            manifest = json.loads(
                (output_dir / index["servers"][0]["manifestFile"]).read_text(
                    "utf-8"
                )
            )
            series_path = output_dir / manifest["catalogs"]["series"]["file"]
            with gzip.open(series_path, "rt", encoding="utf-8") as handle:
                series = json.load(handle)
            self.assertIsNone(series["items"][0]["artwork"]["posterUrl"])
            self.assertNotIn("provider.invalid", json.dumps(series))
            vod.validate_publication(output_dir)

    def test_data_and_manifests_are_hashed_before_index_is_replaced(self) -> None:
        source = sample_source()
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            replacements: list[str] = []
            real_replace = os.replace

            def recording_replace(source_path: object, destination_path: object) -> None:
                replacements.append(Path(destination_path).name)
                real_replace(source_path, destination_path)

            with mock.patch.object(vod.os, "replace", side_effect=recording_replace):
                result = vod.publish_catalogs(
                    output_dir,
                    [("server_1", source, None)],
                    generated_at=NOW,
                )

            self.assertEqual(replacements[-1], "index.json")
            self.assertRegex(replacements[0], r"server_1_movies\.[0-9a-f]{16}\.json\.gz")
            self.assertRegex(replacements[1], r"server_1_series\.[0-9a-f]{16}\.json\.gz")
            self.assertRegex(
                replacements[2], r"server_1_vod_manifest\.[0-9a-f]{16}\.json"
            )
            self.assertEqual(result["serverCount"], 1)

            index = json.loads((output_dir / "index.json").read_text("utf-8"))
            server = index["servers"][0]
            manifest_path = output_dir / server["manifestFile"]
            self.assertEqual(
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                server["manifestSha256"],
            )
            manifest = json.loads(manifest_path.read_text("utf-8"))
            for catalog in manifest["catalogs"].values():
                path = output_dir / catalog["file"]
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), catalog["sha256"]
                )
                self.assertEqual(path.read_bytes()[4:8], b"\x00\x00\x00\x00")
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
                self.assertEqual(payload["itemCount"], len(payload["items"]))
                self.assertIn(payload["contentType"], {"movie", "series"})
            self.assertEqual(
                vod.validate_publication(output_dir),
                {"serverCount": 1, "movieCount": 2, "seriesCount": 1},
            )

    def test_validator_rejects_catalog_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            vod.publish_catalogs(
                output_dir,
                [("server_1", sample_source(), None)],
                generated_at=NOW,
            )
            index = json.loads((output_dir / "index.json").read_text("utf-8"))
            manifest = json.loads(
                (output_dir / index["servers"][0]["manifestFile"]).read_text("utf-8")
            )
            movie_path = output_dir / manifest["catalogs"]["movies"]["file"]
            raw = movie_path.read_bytes()
            movie_path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0x01]))

            with self.assertRaisesRegex(vod.CatalogBuildError, "SHA-256"):
                vod.validate_publication(output_dir)

    def test_validator_rejects_path_traversal_in_release_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            vod.publish_catalogs(
                output_dir,
                [("server_1", sample_source(), None)],
                generated_at=NOW,
            )
            index_path = output_dir / "index.json"
            index = json.loads(index_path.read_text("utf-8"))
            index["servers"][0]["manifestFile"] = "../outside.json"
            index_path.write_text(json.dumps(index), encoding="utf-8")

            with self.assertRaisesRegex(vod.CatalogBuildError, "file name"):
                vod.validate_publication(output_dir)

    def test_validator_rejects_an_incomplete_expected_server_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp) / "VOD"
            vod.publish_catalogs(
                output_dir,
                [("server_1", sample_source(), None)],
                generated_at=NOW,
            )
            with self.assertRaisesRegex(vod.CatalogBuildError, "missing server_2"):
                vod.validate_publication(
                    output_dir,
                    expected_servers=("server_1", "server_2"),
                )

    def test_provider_secret_reflection_stops_before_any_output(self) -> None:
        source = sample_source()
        source["get_vod_streams"][0]["name"] = "leaked very-secret-password"
        config = vod.ServerConfig(
            server_id="server_1",
            base_url="https://provider.invalid",
            username="account-name",
            password="very-secret-password",
        )
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            with self.assertRaisesRegex(vod.CatalogBuildError, "reflected"):
                vod.publish_catalogs(
                    output_dir,
                    [("server_1", source, config)],
                    generated_at=NOW,
                )
            self.assertEqual(list(output_dir.iterdir()), [])

    def test_percent_encoded_provider_secret_is_also_rejected(self) -> None:
        source = sample_source()
        secret = "very-secret-password"
        source["get_vod_streams"][0]["name"] = "".join(
            f"%{byte:02x}" for byte in secret.encode("utf-8")
        )
        config = vod.ServerConfig(
            server_id="server_1",
            base_url="https://provider.invalid",
            username="account-name",
            password=secret,
        )
        with tempfile.TemporaryDirectory() as raw_temp:
            with self.assertRaisesRegex(vod.CatalogBuildError, "reflected"):
                vod.publish_catalogs(
                    Path(raw_temp),
                    [("server_1", source, config)],
                    generated_at=NOW,
                )

    def test_offline_bundle_cli_requires_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "server_1_vod_bundle.json").write_text(
                json.dumps(sample_source()), encoding="utf-8"
            )

            exit_code = vod.main(
                [
                    "--server",
                    "server_1",
                    "--input-dir",
                    str(input_dir),
                    "--output-dir",
                    str(output_dir),
                    "--now-utc",
                    NOW,
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / "index.json").is_file())
            self.assertEqual(
                json.loads((output_dir / "index.json").read_text("utf-8"))[
                    "generatedAt"
                ],
                NOW,
            )
            self.assertEqual(
                vod.main(["--validate-only", "--output-dir", str(output_dir)]), 0
            )


if __name__ == "__main__":
    unittest.main()
