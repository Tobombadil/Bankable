"""Tests for `pipeline.context.eia_plants` against a small in-test workbook (openpyxl), not a
recorded fixture -- there is no upstream fixture convention for the Operating sheet yet and the
shapes under test (mixed technology, a zero-nameplate unit, a (0, 0) coordinate) are easiest to
construct directly."""

from __future__ import annotations

import io

import openpyxl
import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context.eia_plants import aggregate_plants, parse_operating_sheet

COLUMNS = [
    "Entity ID",
    "Entity Name",
    "Plant ID",
    "Plant Name",
    "Plant State",
    "County",
    "Generator ID",
    "Nameplate Capacity (MW)",
    "Technology",
    "Operating Year",
    "Latitude",
    "Longitude",
]


def _workbook(rows: list[list[object]], *, trailing_note: bool = False) -> bytes:
    """Build an .xlsx with two junk title rows above the header, like the real workbook (header
    on the third row, `header=2`)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Operating"
    ws.append(["EIA-860M", None, None])
    ws.append(["Form EIA-860M annual data (test fixture)"])
    ws.append(COLUMNS)
    for row in rows:
        ws.append(row)
    if trailing_note:
        ws.append([None] * len(COLUMNS))
        ws.append(["Note: totals exclude retired units."] + [None] * (len(COLUMNS) - 1))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    # Plant 1000: three units, mixed technology, one zero-nameplate unit, a valid coordinate.
    [
        63001,
        "Sunrise Power LLC",
        1000,
        "Sunrise Solar",
        "TX",
        "Nolan",
        "1",
        100.0,
        "Solar Photovoltaic",
        2015,
        30.0,
        -90.0,
    ],
    [
        63001,
        "Sunrise Power LLC",
        1000,
        "Sunrise Solar",
        "TX",
        "Nolan",
        "2",
        50.0,
        "Solar Photovoltaic",
        2016,
        30.0,
        -90.0,
    ],
    [
        63001,
        "Sunrise Power LLC",
        1000,
        "Sunrise Solar",
        "TX",
        "Nolan",
        "3",
        0.0,
        "Onshore Wind Turbine",
        2010,
        30.0,
        -90.0,
    ],
    # Plant 2000: one unit, a (0, 0) placeholder coordinate that must be rejected.
    [
        63002,
        "Bay Gas Co",
        2000,
        "Bay Peaker",
        "LA",
        "Orleans",
        "1",
        25.0,
        "Natural Gas Fired Combustion Turbine",
        2005,
        0.0,
        0.0,
    ],
]


@pytest.fixture(scope="module")
def plants():
    content = _workbook(ROWS, trailing_note=True)
    sheet = parse_operating_sheet(content)
    return aggregate_plants(
        sheet, retrieved_at="2026-09-15T00:00:00Z", source_url="https://example.org/wb.xlsx"
    )


def test_operating_sheet_header_is_on_the_third_row():
    content = _workbook(ROWS)
    df = parse_operating_sheet(content)
    assert {"Plant ID", "Technology", "Nameplate Capacity (MW)"} <= set(df.columns)
    assert len(df) == len(ROWS)


def test_trailing_note_rows_without_a_plant_id_are_dropped():
    content = _workbook(ROWS, trailing_note=True)
    df = parse_operating_sheet(content)
    assert len(df) == len(ROWS)
    assert df["Plant ID"].notna().all()


def test_a_changed_layout_raises_parse_error():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Operating"
    ws.append(["junk"])
    ws.append(["junk"])
    ws.append(["Not Plant ID", "Something Else"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError):
        parse_operating_sheet(buf.getvalue())


def test_one_row_per_plant(plants):
    assert len(plants) == 2
    assert set(plants["source_plant_id"]) == {"1000", "2000"}


def test_dominant_technology_wins_by_nameplate_share(plants):
    row = plants[plants["source_plant_id"] == "1000"].iloc[0]
    assert row["technology_raw"] == "Solar Photovoltaic"
    assert row["technology"] == "solar"


def test_technologies_split_sums_nameplate_by_raw_label(plants):
    row = plants[plants["source_plant_id"] == "1000"].iloc[0]
    assert row["technologies"] == {"Solar Photovoltaic": 150.0, "Onshore Wind Turbine": 0.0}


def test_zero_nameplate_is_treated_as_missing_not_zero_mw(plants):
    row = plants[plants["source_plant_id"] == "1000"].iloc[0]
    # 100 + 50, the zero-nameplate wind unit excluded from the capacity sum (pipeline.normalize
    # .capacity's rule) though it still appears, at 0.0, in the technologies split above.
    assert row["capacity_mw"] == pytest.approx(150.0)
    assert row["generator_count"] == 3
    assert row["earliest_operating_year"] == 2010


def test_valid_coordinate_is_kept(plants):
    row = plants[plants["source_plant_id"] == "1000"].iloc[0]
    assert row["lon"] == pytest.approx(-90.0)
    assert row["lat"] == pytest.approx(30.0)


def test_a_00_coordinate_is_rejected(plants):
    # `aggregate_plants` returns Python `None`, which a numeric pandas column (mixed with the
    # other plant's real coordinate) represents as NaN once assembled into a DataFrame -- the
    # same representation `capacity_mw`'s "missing" case already gets.
    row = plants[plants["source_plant_id"] == "2000"].iloc[0]
    assert pd.isna(row["lon"])
    assert pd.isna(row["lat"])
    assert row["technology"] == "gas_ct"
    assert row["capacity_mw"] == pytest.approx(25.0)


def test_provenance_columns(plants):
    row = plants.iloc[0]
    assert row["source_id"] == "us.eia.860m"
    assert row["source_url"] == "https://example.org/wb.xlsx"
    assert row["retrieved_at"] == "2026-09-15T00:00:00Z"
    assert row["licence"] == "public-domain"
    assert row["country"] == "US"
