"""`pipeline.normalize.classify_tech` pinned raw -> class over every distinct `technology_raw` the
normalised frames carry (audit 2026-09-30, data scientist F4).

`data/eval/technology_classes.csv` is the table: one row per distinct raw string in the frames of
the sources whose `technology` the classifier produces (EIA-860M and its plant context, NESO,
CAISO, ERCOT, NYISO, the federal permits dashboard, Virginia DEQ), with the class it must get. A
rule-order change that moves any of them fails here and has to be accepted by editing the table.
`data/normalized/` is not tracked, so the coverage test (every raw string in the frames is in the
table) runs only where the frames exist.
"""

from __future__ import annotations

import csv
import pathlib

import pandas as pd
import pytest

from pipeline.normalize import TECH_RULES, classify_tech
from pipeline.resolve import TECH_FAMILIES

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "eval" / "technology_classes.csv"
NORMALIZED = ROOT / "data" / "normalized"
#: Frames whose `technology` comes from `classify_tech` (the others set their own token).
CLASSIFIED_FRAMES = (
    "context/us.eia.860m.plants.parquet",
    "gb.neso.tec_register/*.parquet",
    "us.eia.860m/*.parquet",
    "us.iso.caiso.gen_queue/*.parquet",
    "us.iso.ercot.gen_queue/*.parquet",
    "us.iso.nyiso.gen_queue/*.parquet",
    "us.permits_dashboard/*.parquet",
    "us.va.deq.data_center_air_sites/*.parquet",
)


def _table() -> list[dict[str, str]]:
    with TABLE.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_every_raw_string_in_the_table_gets_its_pinned_class() -> None:
    rows = _table()
    assert len(rows) >= 160
    wrong = [
        (r["technology_raw"], classify_tech(r["technology_raw"]), (r["technology"], r["kind"]))
        for r in rows
        if classify_tech(r["technology_raw"]) != (r["technology"], r["kind"])
    ]
    assert wrong == []


@pytest.mark.parametrize(
    ("raw", "tech"),
    [
        # NESO writes the words the other way round (140 rows, plus compounds).
        ("Wind Offshore", "wind_offshore"),
        ("Demand;Wind Offshore", "wind_offshore"),
        ("Interconnector;Wind Offshore", "wind_offshore"),
        ("Offshore Wind Turbine", "wind_offshore"),
        ("Wind: Federal Offshore", "wind_offshore"),
        ("Wind: Other than Federal Offshore", "wind"),
        ("Wind Onshore", "wind"),
        # NESO "Pump Storage" (18 rows).
        ("Pump Storage", "pumped_storage"),
        ("Hydroelectric Pumped Storage", "pumped_storage"),
        # ERCOT's STP Unit 2 uprate was filed as gas steam.
        ("Nuclear - Steam Turbine other than Combined-Cycle", "nuclear"),
        ("Gas - Steam Turbine other than Combined-Cycle", "gas_steam"),
        ("Steam Turbine", "gas_steam"),
        # Marine.
        ("Tidal", "marine"),
        (
            "Non-Federal Hydropower - Licenses (including Non-Federal Marine and Hydrokinetic Projects)",
            "hydro",
        ),
        # An LNG terminal "in State Water" is not hydro.
        (
            "Liquefied Natural Gas Terminal Facilities (Onshore or in State Water), and associated "
            "Natural Gas Pipelines",
            "gas_other",
        ),
        ("Water - Other", "hydro"),
    ],
)
def test_the_audited_rule_order_cases(raw: str, tech: str) -> None:
    assert classify_tech(raw)[0] == tech


def test_every_class_the_rules_emit_has_a_resolver_family() -> None:
    """The resolver's technology veto reads `TECH_FAMILIES`; a class missing there is treated as
    unknown and compatible with everything, which is how a new class silently loses its veto."""
    classes = {tech for _, tech, _ in TECH_RULES}
    assert classes - set(TECH_FAMILIES) == set()


def test_every_raw_string_in_the_frames_is_in_the_table() -> None:
    paths = [p for pattern in CLASSIFIED_FRAMES for p in sorted(NORMALIZED.glob(pattern))]
    if not paths:
        pytest.skip("data/normalized is not present (untracked)")
    seen: set[str] = set()
    for path in paths:
        values = pd.read_parquet(path, columns=["technology_raw"])["technology_raw"].dropna()
        seen |= {str(v) for v in values if str(v).strip()}
    listed = {r["technology_raw"] for r in _table()}
    assert sorted(seen - listed) == []
