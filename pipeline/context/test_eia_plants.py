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


# ---------------------------------------------------------------- lane R1: retired and retiring
def test_retired_and_retiring_plants_from_the_recorded_workbook():
    """The recorded trim of the August 2026 workbook (tests/fixtures/eia860m_operating_retired.xlsx):
    one row per plant across both sheets, with the plant status rule of
    pipeline/context/retirements.py applied."""
    from conftest import fixture_path
    from pipeline.context.retirements import parse_generator_sheets

    sheets = parse_generator_sheets(fixture_path("eia860m_operating_retired.xlsx").read_bytes())
    out = aggregate_plants(
        sheets.operating, retrieved_at="2026-09-27T15:15:28Z", retired=sheets.retired, as_of=sheets.as_of
    )
    rows = {r["source_plant_id"]: r for r in out.to_dict("records")}
    assert len(out) == len(set(out["source_plant_id"])) == 9
    # Fully retired: Rush Island and Homer City exist only on the Retired sheet.
    for pid, year in (("6155", 2024), ("3122", 2024)):
        assert rows[pid]["status"] == "retired" and rows[pid]["retirement_year"] == year
    homer = rows["3122"]
    assert homer["capacity_mw"] == pytest.approx(2012.0)
    assert homer["lon"] is not None and homer["attributes"]["retirement"]["first_retired"] == "2023-07"
    # Every unit scheduled (Rockport, La Cygne), or most of the MW (Merrimack's two coal units).
    assert rows["6166"]["status"] == "retiring" and rows["6166"]["retirement_year"] == 2028
    assert rows["1241"]["status"] == "retiring" and rows["1241"]["retirement_year"] == 2032
    assert rows["2364"]["status"] == "retiring"
    assert rows["2364"]["attributes"]["retirement"]["status_rule"] == "majority_mw_retiring"
    # A minority of the MW (Cardinal unit 3, 650 of 1,880 MW): operating, but the date is kept.
    cardinal = rows["2828"]
    assert cardinal["status"] == "operating" and cardinal["retirement_year"] == 2028
    assert cardinal["attributes"]["retirement"]["retiring_mw"] == pytest.approx(650.0)
    # A unit retired in 1985 does not make Dresden anything but operating, and dates nothing.
    assert rows["869"]["status"] == "operating" and pd.isna(rows["869"]["retirement_year"])
    assert rows["869"]["attributes"]["retirement"]["retired_units"] == 1
    # No retirement at all: no block, and the balancing authority is carried for every plant.
    assert "retirement" not in rows["145"]["attributes"]
    assert rows["145"]["attributes"]["balancing_authority_code"]
    assert rows["6166"]["attributes"]["retirement"]["as_of"] == "2026-08"


def test_without_a_retired_sheet_the_operating_rows_still_classify(plants):
    assert set(plants["status"]) == {"operating"}
    assert plants["retirement_year"].isna().all()


def test_cli_builds_from_another_data_root_with_its_run_record(tmp_path):
    """`--data-root` (2026-10-07): the plants file on the shared data root had been built from the
    July workbook before lane R1, so it held operating plants only and 1,814 plants of the
    retirements run had no asset. Rebuilding it from a worktree must read that root's snapshot and
    run record (the workbook URL) and write under that root, not the checkout's own `data/`."""
    import json

    from conftest import fixture_path
    from pipeline.context.eia_plants import main

    run_ts = "20261007T103613Z"
    url = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"
    snapshots = tmp_path / "snapshots" / "us.eia.860m"
    runs = tmp_path / "runs" / "us.eia.860m"
    snapshots.mkdir(parents=True)
    runs.mkdir(parents=True)
    (snapshots / f"{run_ts}.xlsx").write_bytes(fixture_path("eia860m_operating_retired.xlsx").read_bytes())
    (runs / f"{run_ts}.json").write_text(json.dumps({"snapshot": {"fetched_url": url}}))

    main(["--data-root", str(tmp_path), "--latest-snapshot"])

    out = pd.read_parquet(tmp_path / "normalized" / "context" / "us.eia.860m.plants.parquet")
    assert set(out["source_url"]) == {url}
    assert set(out["retrieved_at"]) == {"2026-10-07T10:36:13Z"}
    assert set(out["status"]) == {"operating", "retiring", "retired"}
    retired = out[out["status"] == "retired"]
    assert len(retired) == 2 and retired["lon"].notna().all() and retired["lat"].notna().all()
