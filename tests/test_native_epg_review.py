from __future__ import annotations

import gzip
import html
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import native_epg_review as native  # noqa: E402


def xmltv_document(
    *,
    channel_ids: list[str],
    programmes: list[tuple[str, str, str, str]],
    display_names: dict[str, list[str]] | None = None,
    doctype: str = "",
) -> bytes:
    names = display_names or {}
    padded_ids = list(channel_ids)
    for index in range(len(padded_ids), native.MIN_NATIVE_CATALOG_IDS):
        padded_ids.append(f"Fixture.Padding.{index:03d}")
    channel_xml: list[str] = []
    for channel_id in padded_ids:
        values = names.get(channel_id, [channel_id])
        children = "".join(
            f"<display-name>{html.escape(value)}</display-name>" for value in values
        )
        channel_xml.append(
            f'<channel id="{html.escape(channel_id, quote=True)}">{children}</channel>'
        )
    programme_xml = "".join(
        (
            f'<programme channel="{html.escape(channel_id, quote=True)}" '
            f'start="{start}" stop="{stop}">'
            f"<title>{html.escape(title)}</title></programme>"
        )
        for channel_id, start, stop, title in programmes
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{doctype}\n<tv>{''.join(channel_xml)}{programme_xml}</tv>"
    ).encode("utf-8")


def useful_programmes(channel_id: str) -> list[tuple[str, str, str, str]]:
    return [
        (
            channel_id,
            "20260101000000 +0000",
            "20260101030000 +0000",
            "Morning News",
        ),
        (
            channel_id,
            "20260101030000 +0000",
            "20260101060000 +0000",
            "Evening News",
        ),
    ]


class NativeEpgReviewTests(unittest.TestCase):
    NOW = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    def validate(
        self,
        document: bytes,
        requested_ids: set[str],
        *,
        server_id: str = "server_2",
        gzip_source: bool = False,
    ) -> native.NativeValidation:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / ("native.xml.gz" if gzip_source else "native.xml")
            source.write_bytes(gzip.compress(document) if gzip_source else document)
            return native.validate_native_xmltv(
                source,
                server_id=server_id,
                requested_ids=requested_ids,
                now_epoch=self.NOW,
            )

    def test_exact_case_unique_id_and_cleaned_display_names_are_returned(self) -> None:
        result = self.validate(
            xmltv_document(
                channel_ids=["Exact.ID"],
                display_names={
                    "Exact.ID": [
                        "  Exact   Channel  ",
                        "Chaîne Exacte",
                        "Exact Channel",
                    ]
                },
                programmes=useful_programmes("Exact.ID"),
            ),
            {"Exact.ID", "Missing.ID"},
            gzip_source=True,
        )
        self.assertEqual(result.requested_ids, 2)
        self.assertEqual(result.verified_ids, frozenset({"Exact.ID"}))
        self.assertEqual(
            result.display_names_by_id,
            {"Exact.ID": ("Chaîne Exacte", "Exact Channel")},
        )
        with self.assertRaises(TypeError):
            result.display_names_by_id["Injected.ID"] = ("Injected",)  # type: ignore[index]

    def test_native_name_compatibility_is_exact_and_conservative(self) -> None:
        compatible = (
            ("US: CNN HD", ["CNN"], True),
            ("ＵＫ：ＢＢＣ ONE ＦＨＤ", ["BBC-One"], True),
            ("[CA] CP24 UHD", ["CP24"], True),
            ("凤凰卫视高清", ["凤凰卫视高清"], True),
            ("USA Network HD", ["USA Network"], True),
            ("USA Network HD", ["Network"], False),
            ("BBC One HD", ["BBC Two"], False),
            ("SPORTS 1 HD", ["Sports 1"], False),
            ("12345", ["12345"], False),
            ("HD Net", ["Net"], False),
        )
        for provider_name, display_names, expected in compatible:
            with self.subTest(provider_name=provider_name):
                self.assertEqual(
                    native.native_names_compatible(provider_name, display_names),
                    expected,
                )

    def test_casefold_collision_excludes_both_ids_and_their_names(self) -> None:
        result = self.validate(
            xmltv_document(
                channel_ids=["Case.ID", "case.id", "Good.ID"],
                programmes=(
                    useful_programmes("Case.ID")
                    + useful_programmes("case.id")
                    + useful_programmes("Good.ID")
                ),
            ),
            {"Case.ID", "case.id", "Good.ID"},
        )
        self.assertEqual(result.verified_ids, frozenset({"Good.ID"}))
        self.assertEqual(set(result.display_names_by_id), {"Good.ID"})

    def test_duplicate_exact_channel_id_is_never_eligible(self) -> None:
        channels = [f"Fixture.Padding.{index:03d}" for index in range(99)]
        channel_xml = "".join(
            f'<channel id="{channel_id}"><display-name>{channel_id}</display-name></channel>'
            for channel_id in channels
        )
        duplicate_xml = (
            '<channel id="Dup.ID"><display-name>Right News</display-name></channel>'
            '<channel id="Dup.ID"><display-name>Wrong Sports</display-name></channel>'
        )
        programmes = "".join(
            f'<programme channel="{channel_id}" start="{start}" stop="{stop}">'
            f'<title>{title}</title></programme>'
            for channel_id, start, stop, title in useful_programmes("Dup.ID")
        )
        document = (
            '<?xml version="1.0" encoding="UTF-8"?><tv>'
            + channel_xml
            + duplicate_xml
            + programmes
            + "</tv>"
        ).encode("utf-8")

        result = self.validate(document, {"Dup.ID"})

        self.assertEqual(result.verified_ids, frozenset())
        self.assertNotIn("Dup.ID", result.display_names_by_id)

    def test_unrequested_id_with_same_display_name_is_reported_ambiguous(self) -> None:
        result = self.validate(
            xmltv_document(
                channel_ids=["Requested.ID", "Unrequested.ID"],
                display_names={
                    "Requested.ID": ["Shared Station"],
                    "Unrequested.ID": ["Shared Station"],
                },
                programmes=useful_programmes("Requested.ID"),
            ),
            {"Requested.ID"},
        )

        self.assertEqual(result.verified_ids, frozenset())
        self.assertNotIn("Requested.ID", result.display_names_by_id)
        self.assertEqual(
            result.ambiguous_display_name_keys,
            frozenset({"shared station"}),
        )

    def test_programme_gate_rejects_weak_duplicate_and_placeholder_schedules(self) -> None:
        programmes = useful_programmes("Good.ID") + [
            (
                "One.ID",
                "20260101000000 +0000",
                "20260101070000 +0000",
                "One Long Show",
            ),
            (
                "Short.ID",
                "20260101000000 +0000",
                "20260101030000 +0000",
                "First Show",
            ),
            (
                "Short.ID",
                "20260101030000 +0000",
                "20260101055900 +0000",
                "Second Show",
            ),
            (
                "Placeholder.ID",
                "20260101000000 +0000",
                "20260101030000 +0000",
                "No Information",
            ),
            (
                "Placeholder.ID",
                "20260101030000 +0000",
                "20260101070000 +0000",
                "No Information",
            ),
            (
                "Duplicate.ID",
                "20260101000000 +0000",
                "20260101070000 +0000",
                "First Copy",
            ),
            (
                "Duplicate.ID",
                "20260101000000 +0000",
                "20260101070000 +0000",
                "Second Copy",
            ),
            (
                "Late.ID",
                "20260101060100 +0000",
                "20260101120000 +0000",
                "Late First Show",
            ),
            (
                "Late.ID",
                "20260101120000 +0000",
                "20260101180000 +0000",
                "Late Second Show",
            ),
        ]
        requested = {
            "Good.ID",
            "One.ID",
            "Short.ID",
            "Placeholder.ID",
            "Duplicate.ID",
            "Late.ID",
        }
        result = self.validate(
            xmltv_document(channel_ids=sorted(requested), programmes=programmes),
            requested,
        )
        self.assertEqual(result.verified_ids, frozenset({"Good.ID"}))

    def test_inert_xmltv_doctype_is_allowed_but_entities_are_forbidden(self) -> None:
        valid = xmltv_document(
            channel_ids=["Safe.ID"],
            programmes=useful_programmes("Safe.ID"),
            doctype='<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        )
        self.assertEqual(
            self.validate(valid, {"Safe.ID"}).verified_ids,
            frozenset({"Safe.ID"}),
        )

        unsafe = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE tv [<!ENTITY leak SYSTEM "file:///etc/passwd">]>\n'
            '<tv><channel id="Unsafe.ID"><display-name>&leak;</display-name>'
            "</channel></tv>"
        ).encode("utf-8")
        with self.assertRaisesRegex(native.NativeReviewError, "security preflight"):
            self.validate(unsafe, {"Unsafe.ID"})

    def test_server_one_and_unknown_servers_are_forbidden_before_file_access(self) -> None:
        missing = Path("/definitely/not/a/native/source.xml")
        with self.assertRaisesRegex(
            native.NativeReviewError,
            "Server 1 native EPG validation is forbidden",
        ):
            native.validate_native_xmltv(
                missing,
                server_id="server_1",
                requested_ids={"Never.Native"},
                now_epoch=self.NOW,
            )
        with self.assertRaisesRegex(native.NativeReviewError, "unsupported server"):
            native.validate_native_xmltv(
                missing,
                server_id="server_4",
                requested_ids={"Never.Native"},
                now_epoch=self.NOW,
            )

    def test_completeness_floor_is_fixed_for_one_candidate(self) -> None:
        document = (
            '<?xml version="1.0"?><tv><channel id="Only.ID">'
            "<display-name>Only</display-name></channel>"
            '<programme channel="Only.ID" start="20260101000000 +0000" '
            'stop="20260101030000 +0000"><title>First</title></programme>'
            '<programme channel="Only.ID" start="20260101030000 +0000" '
            'stop="20260101060000 +0000"><title>Second</title></programme></tv>'
        ).encode("utf-8")
        with self.assertRaisesRegex(native.NativeReviewError, "completeness floor"):
            self.validate(document, {"Only.ID"})

    def test_display_name_overflow_makes_candidate_unsafe(self) -> None:
        names = [
            f"Name {index}"
            for index in range(native.MAX_NATIVE_DISPLAY_NAMES_PER_ID + 1)
        ]
        result = self.validate(
            xmltv_document(
                channel_ids=["Overflow.ID"],
                display_names={"Overflow.ID": names},
                programmes=useful_programmes("Overflow.ID"),
            ),
            {"Overflow.ID"},
        )
        self.assertFalse(result.verified_ids)
        self.assertNotIn("Overflow.ID", result.display_names_by_id)

    def test_channels_declared_after_programmes_are_rejected(self) -> None:
        channels = "".join(
            f'<channel id="Padding.{index:03d}"><display-name>P</display-name></channel>'
            for index in range(native.MIN_NATIVE_CATALOG_IDS)
        )
        document = (
            '<?xml version="1.0"?><tv>'
            f"{channels}"
            '<programme channel="Padding.000" start="20260101000000 +0000" '
            'stop="20260101030000 +0000"><title>First</title></programme>'
            '<channel id="Late.ID"><display-name>Late</display-name></channel>'
            "</tv>"
        ).encode("utf-8")
        with self.assertRaisesRegex(native.NativeReviewError, "after programmes"):
            self.validate(document, {"Padding.000"})

    def test_symlink_source_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.xml"
            target.write_bytes(
                xmltv_document(
                    channel_ids=["Safe.ID"],
                    programmes=useful_programmes("Safe.ID"),
                )
            )
            source = root / "source.xml"
            source.symlink_to(target)
            with self.assertRaisesRegex(
                native.NativeReviewError, "unavailable|regular file"
            ):
                native.validate_native_xmltv(
                    source,
                    server_id="server_2",
                    requested_ids={"Safe.ID"},
                    now_epoch=self.NOW,
                )

    def test_blank_candidate_is_rejected_but_empty_set_is_a_noop(self) -> None:
        with self.assertRaisesRegex(native.NativeReviewError, "candidate set"):
            native.validate_native_xmltv(
                Path("missing.xml"),
                server_id="server_2",
                requested_ids={"   "},
                now_epoch=self.NOW,
            )
        result = native.validate_native_xmltv(
            Path("missing.xml"),
            server_id="server_2",
            requested_ids=set(),
            now_epoch=self.NOW,
        )
        self.assertEqual(result, native.NativeValidation(frozenset(), 0))

    def test_oversized_ids_are_rejected_instead_of_truncated(self) -> None:
        oversized = "X" * 301
        with self.assertRaisesRegex(native.NativeReviewError, "candidate set"):
            native.validate_native_xmltv(
                Path("missing.xml"),
                server_id="server_2",
                requested_ids={oversized},
                now_epoch=self.NOW,
            )

        document = xmltv_document(
            channel_ids=[oversized],
            programmes=useful_programmes(oversized),
        )
        with self.assertRaisesRegex(native.NativeReviewError, "source"):
            self.validate(document, {"Good.ID"})


if __name__ == "__main__":
    unittest.main()
