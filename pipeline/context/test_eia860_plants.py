"""`pipeline/context/eia860_plants.py` against a recorded trim of the EIA-860 2025 final zip
(`pipeline/context/fixtures/eia860_plant_sample.zip`: member `2___Plant_Y2025.xlsx`, title row,
header and the ten plant rows of `xls/eia8602025.zip` fetched 2026-09-19 for the plants in the
EIA-860M retirement fixture plus one plant whose grid voltage is stated as 0)."""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from pipeline.connectors.base import ParseError
from pipeline.context.eia860_plants import extract_plant_rows, find_plant_member

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "eia860_plant_sample.zip"


@pytest.fixture(scope="module")
def plants():
    return extract_plant_rows(
        FIXTURE.read_bytes(),
        source_url="https://example.org/eia8602025.zip",
        retrieved_at="2026-09-19T20:48:22Z",
    )


def by_id(plants):
    return {r["plant_id"]: r for r in plants.to_dict("records")}


def test_one_row_per_plant_with_the_report_year(plants):
    assert len(plants) == len(set(plants["plant_id"])) == 10
    assert set(plants["report_year"]) == {2025}
    assert {"1", "145", "869", "1241", "2364", "2828", "6166"} <= set(plants["plant_id"])


def test_grid_fields_for_a_retiring_plant(plants):
    rockport = by_id(plants)["6166"]
    assert rockport["nerc_region"] == "RFC"
    assert rockport["transmission_owner"]
    assert rockport["grid_voltage_kv"] and all(v > 0 for v in rockport["grid_voltage_kv"])
    assert rockport["grid_voltage_kv"] == sorted(rockport["grid_voltage_kv"], reverse=True)


def test_zero_kv_is_not_a_voltage(plants):
    zeros = [
        r
        for r in plants.to_dict("records")
        if r["plant_id"] not in {"1", "145", "869", "1241", "2364", "2828", "6166", "6155", "3122"}
    ]
    assert len(zeros) == 1 and zeros[0]["grid_voltage_kv"] == []


def test_provenance(plants):
    row = plants.iloc[0]
    assert row["source_id"] == "us.eia.860" and row["licence"] == "public-domain"
    assert row["source_url"] == "https://example.org/eia8602025.zip"


def test_member_discovery_and_a_missing_member():
    assert find_plant_member(["LayoutY2025.xlsx", "2___Plant_Y2025.xlsx"]) == ("2___Plant_Y2025.xlsx", 2025)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ParseError):
        extract_plant_rows(buf.getvalue(), source_url="u", retrieved_at="t")
