"""Tests for `pipeline.context.ethanol_capacity` against `fixtures/eia_ethanol_capacity_sample.xlsx`:
the real `ethanolcapacity.xlsx` fetched from https://www.eia.gov/petroleum/ethanolcapacity/ on
2026-09-19 (23,446 bytes, "as of January 1, 2025"), trimmed to the title/header rows, the whole
of PADD 1, the Kansas block of PADD 2 (which carries EIA's `(s)` suppression symbol and two
formula cells in the Mb/d column), the whole of PADD 5, the U.S. Total row and the footnotes.
"""

from __future__ import annotations

import io
import pathlib

import openpyxl
import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context import fuels
from pipeline.context.ethanol_capacity import (
    SOURCE_ID,
    SUPPRESSED_NOTE,
    build_assets,
    parse_capacity_table,
)

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "eia_ethanol_capacity_sample.xlsx"


@pytest.fixture(scope="module")
def parsed():
    return parse_capacity_table(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def assets(parsed):
    table, year = parsed
    return build_assets(
        table,
        retrieved_at="2026-09-19T15:20:38Z",
        source_url="https://www.eia.gov/petroleum/ethanolcapacity/ethanolcapacity.xlsx",
        as_of_year=year,
        licence_id="us.eia.ethanol_capacity#test",
    )


def test_as_of_year_comes_from_the_title_cell(parsed):
    _, year = parsed
    assert year == 2025


def test_only_plant_rows_survive_and_state_is_forward_filled(parsed):
    table, _ = parsed
    # PADD 1 (2 plants), Illinois' first row (1), Kansas (12), PADD 5: California (3) + Oregon (1)
    # = 19 plant rows; PADD subtotal rows, the U.S. Total row and the footnotes are not plants.
    assert len(table) == 19
    assert table["state_name"].isna().sum() == 0
    assert set(table["state_name"]) == {
        "New York",
        "Pennsylvania",
        "Illinois",
        "Kansas",
        "California",
        "Oregon",
    }
    assert set(table["padd"]) == {"PADD 1", "PADD 2", "PADD 5"}
    assert "U.S. Total" not in set(table["respondent"])


def test_suppression_symbol_is_missing_not_zero(parsed):
    table, _ = parsed
    mgp = table[table["respondent"] == "MGP Ingredients Inc"].iloc[0]
    # MMgal/yr is 6; the Mb/d column carried "(s)" -- the parser reads MMgal/yr only, so this row
    # is a normal value. The suppressed MMgal/yr case is exercised through the assembled frame
    # below with an in-test row.
    assert mgp["capacity_mmgal_yr"] == pytest.approx(6.0)
    assert not bool(mgp["capacity_suppressed"])


def test_a_suppressed_capacity_cell_yields_null_and_a_note():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["U.S. Fuel Ethanol Plant Production Capacity as of January 1, 2030"])
    ws.append([])
    ws.append(["State", "Respondent", "City", "MMgal/yr", "Mb/d"])
    ws.append(["PADD 9", None, None, "1", "(s)"])
    ws.append(["Texas", "Tiny Ethanol LLC", "Nowhere", "(s)", "(s)"])
    buf = io.BytesIO()
    wb.save(buf)
    table, year = parse_capacity_table(buf.getvalue())
    assert year == 2030
    assert len(table) == 1
    assert pd.isna(table.iloc[0]["capacity_mmgal_yr"]) and bool(table.iloc[0]["capacity_suppressed"])
    df = build_assets(table, retrieved_at="t", source_url="u", as_of_year=year, licence_id="l")
    row = df.iloc[0]
    assert row["capacity_value"] is None or pd.isna(row["capacity_value"])
    assert row["attributes"] == {"as_of_year": 2030.0}
    assert row["attributes_text"]["capacity_note"] == SUPPRESSED_NOTE


def test_a_changed_layout_raises_parse_error():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Some other report"])
    ws.append(["Plant", "Owner"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError):
        parse_capacity_table(buf.getvalue())


def test_contract_columns_and_provenance(assets):
    assert list(assets.columns) == fuels.ASSET_COLUMNS
    row = assets.iloc[0]
    assert row["source_id"] == SOURCE_ID
    assert row["source_url"].endswith("ethanolcapacity.xlsx")
    assert row["retrieved_at"] == "2026-09-19T15:20:38Z"
    assert row["licence"] == "public-domain"
    assert row["licence_id"] == "us.eia.ethanol_capacity#test"
    assert row["raw"]["respondent"] == row["operator_name"]


def test_feature_set_is_capacity_operator_and_placement_without_a_point(assets):
    row = assets[assets["source_asset_id"] == "NY-western-new-york-energy-llc-medina"].iloc[0]
    assert row["name"] == "Western New York Energy LLC (Medina, NY)"
    assert row["status"] == "operating"
    assert row["technology"] == "ethanol"
    assert row["capacity_value"] == pytest.approx(62.0)
    assert row["capacity_unit"] == "MMgal/yr"
    assert row["attributes"] == {"nameplate_capacity_mmgal_yr": 62.0, "as_of_year": 2025.0}
    assert row["attributes_text"] == {"city": "Medina", "padd": "PADD 1", "state_name": "New York"}
    assert row["owner_raw"] == "Western New York Energy LLC"
    assert row["feedstock"] is None  # the table carries no feedstock; nothing is assumed
    assert row["state_code"] == "US-NY"
    assert row["lon"] is None and row["lat"] is None  # a city is not a coordinate
    assert row["placement_precision"] == "state_centroid"
    assert row["centroid_lat"] > 40


def test_keys_are_unique_across_a_respondent_with_several_plants(assets):
    # The fixture's Kansas block has no repeated respondent+city, but the key must still be a
    # per-plant identity, not a per-company one.
    assert assets["source_asset_id"].is_unique
    assert len(assets) == 19


def test_assets_survive_the_parquet_round_trip(assets, tmp_path):
    out = fuels.write_context_parquet(assets, tmp_path / "x.parquet")
    back = pd.read_parquet(out)
    assert list(back.columns) == fuels.ASSET_COLUMNS
    assert len(back) == len(assets)
    assert back["attributes"].iloc[0].startswith("{")
