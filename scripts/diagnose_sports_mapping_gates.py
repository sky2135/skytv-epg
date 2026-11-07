#!/usr/bin/env python3
"""Read-only aggregate mapping gates for the seven exact sports photo labels."""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

import diagnose_uk_mapping_gates as uk


GROUPS = ("SuperSportsPSLFHD", "SkySportsPremierLeagueFHD", "Premier2FHD",
          "SuperSportsFootballFHD", "SuperSportsLaLigaFHD", "SuperSportsPremierLeagueFHD", "TSN1")
LABELS = dict(zip((
    "supersportspsl(fhd)", "skysportspremierleague(fhd)", "premier2(fhd)",
    "supersportsfootball(fhd)", "supersportslaliga(fhd)", "supersportspremierleague(fhd)", "tsn1",
), GROUPS))
KNOWN_EPG_IDS = {
    "PSL.za": "supersport_psl_za",
    "SS.Football.za": "supersport_football_za", "SS.Football.ng": "supersport_football_ng",
    "SS.La.Liga.za": "supersport_laliga_za", "SS.La.Liga.ng": "supersport_laliga_ng",
    "SS.Premier.League.za": "supersport_premier_league_za",
    "SS.Premier.League.ng": "supersport_premier_league_ng",
    "SkySp.PL.HD.uk": "sky_premier_league_uk",
    "Sky.Sports.Premier.League.HD.ie": "sky_premier_league_ie",
    "Sky.Sports.Premier.League.ie": "sky_premier_league_ie_sd",
    "Premier.Sports.2.HD.uk": "premier_sports_2_uk", "Premier.Sports.2.HD.ie": "premier_sports_2_ie",
    **{f"TSN.{number}.ca2": f"tsn_{number}_ca2" for number in range(1, 6)},
}
DEFAULT_OUTPUT = Path(".build/sports-mapping-gates/report.json")


def classify_row(row: Mapping[str, str]) -> tuple[str | None, tuple[str, ...]]:
    # Case, whitespace and compatibility typography only. FHD, parentheses,
    # market prefixes, channel numbers and the Sky/SuperSports brands survive.
    key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(row.get("channel_name", ""))).casefold())
    return LABELS.get(key), ()


def summarize(table: uk.inventory.MappingTable) -> dict[str, Any]:
    report = uk.summarize(table, groups=GROUPS, classifier=classify_row, known_epg_ids=KNOWN_EPG_IDS)
    report["countryMetadataIsNotMatchApproval"] = True
    return report


def diagnose() -> dict[str, Any]:
    return uk.diagnose(summarizer=summarize)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = diagnose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    uk.inventory.atomic_write_bytes(args.output, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({
        "selectedRows": report["selectedRows"],
        "groupsWithRows": sum(item["rowCount"] > 0 for item in report["groups"]),
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
