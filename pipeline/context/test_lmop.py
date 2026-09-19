"""Tests for `pipeline.context.lmop` against `fixtures/lmop_composite_sample.xlsx`: the real
`lmopcompositedata.xlsx` (2024-09 build) fetched from
https://www.epa.gov/system/files/documents/2024-09/lmopcompositedata.xlsx on 2026-09-19
(1,465,367 bytes, 3,399 database rows), trimmed to the whole Summary and Field Descriptions sheets
plus 27 "LMOP Database" rows: every row of the multi-landfill projects 1016-0 (Operational, three
landfills) and 1034-0 (Shutdown, three landfills), project 394-0 (Shutdown; its landfill carries
no Latitude -- all 15 such asset-status rows in the build are Shutdown), up to four rows each of
the real project statuses (Operational, Shutdown, Planned, Construction) and one row of each
landfill-potential class (Candidate, Low Potential, Unknown, Future Potential).
"""

from __future__ import annotations

import io
import pathlib

import openpyxl
import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context import fuels
from pipeline.context.lmop import SOURCE_ID, build_rows, find_composite_url, parse_workbook

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "lmop_composite_sample.xlsx"


@pytest.fixture(scope="module")
def parsed():
    return parse_workbook(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def built(parsed):
    df, _ = parsed
    return build_rows(
        df,
        retrieved_at="2026-09-19T15:20:34Z",
        source_url="https://www.epa.gov/system/files/documents/2024-09/lmopcompositedata.xlsx",
        licence_id="us.epa.lmop#test",
    )


def test_find_composite_url_follows_whatever_month_the_page_links():
    html = b'<a href="https://www.epa.gov/system/files/documents/2027-03/lmopcompositedata.xlsx">x</a>'
    assert find_composite_url(html).endswith("2027-03/lmopcompositedata.xlsx")
    with pytest.raises(ParseError):
        find_composite_url(b"<html>no link</html>")


def test_parse_reads_the_database_sheet_and_the_cover_sheet(parsed):
    df, summary = parsed
    assert len(df) == 27
    assert {"Project ID", "Current Project Status", "Latitude", "Rated MW Capacity"} <= set(df.columns)
    # docs/13 §2.12 cover-sheet check: the Summary sheet carries the status vocabulary and a
    # data-quality caveat, and no copyright or reuse notice.
    assert "Operational: Project or expansion is online." in summary
    assert "copyright" not in summary.lower()


def test_a_changed_layout_raises_parse_error():
    wb = openpyxl.Workbook()
    wb.active.title = "Summary"
    ws = wb.create_sheet("LMOP Database")
    ws.append(["Something", "Else"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError):
        parse_workbook(buf.getvalue())


def test_statuses_route_to_assets_proposals_or_dropped(built):
    assets, proposals, dropped = built
    assert set(assets["status_raw"]) == {"Operational", "Shutdown"}
    assert set(assets["status"]) == {"operating", "retired"}
    assert set(proposals["status_raw"]) == {"Planned", "Construction"}
    assert set(proposals["status"]) == {"unknown"}
    assert set(dropped) == {"Candidate", "Low Potential", "Unknown", "Future Potential"}
    assert list(assets.columns) == fuels.ASSET_COLUMNS
    assert list(proposals.columns) == fuels.ASSET_COLUMNS


def test_multi_landfill_project_collapses_to_one_asset(built):
    assets, _, _ = built
    row = assets[assets["source_asset_id"] == "1016-0"].iloc[0]
    assert row["status"] == "operating"
    assert row["attributes"]["landfill_count"] == 3.0
    assert row["attributes_text"]["landfill_names"].count(";") == 2
    assert row["attributes_text"]["landfill_ids"].count(";") == 2
    assert row["capacity_mw"] == pytest.approx(22.5)
    assert row["capacity_value"] == pytest.approx(7.56) and row["capacity_unit"] == "mmscfd"
    assert assets["source_asset_id"].is_unique
    shut = assets[assets["source_asset_id"] == "1034-0"].iloc[0]
    assert shut["status"] == "retired" and shut["attributes"]["landfill_count"] == 3.0


def test_feature_set_and_provenance(built):
    assets, _, _ = built
    row = assets[assets["source_asset_id"] == "167079-0"].iloc[0]
    assert row["name"] == "Project #1 - Anchorage Regional Landfill"
    assert row["technology"] == "lfg_electricity"
    assert row["technology_raw"] == "Reciprocating Engine"
    assert row["operator_name"] == "Doyon Utilities, LLC" == row["owner_raw"] == row["developer_raw"]
    assert row["commissioned_year"] == 2012
    assert row["attributes"]["rated_mw"] == 5.6
    assert row["attributes"]["project_start_year"] == 2012.0
    assert row["attributes_text"]["end_users"] == "Joint Base Elmendorf Richardson (JBER)"
    assert row["attributes_text"]["landfill_owner"] == "Municipality of Anchorage, AK"
    assert all(isinstance(v, float) for v in row["attributes"].values())
    assert row["source_id"] == SOURCE_ID
    assert row["licence"] == "public-domain" and row["licence_id"] == "us.epa.lmop#test"
    assert row["retrieved_at"] == "2026-09-19T15:20:34Z"
    assert row["raw"]["Landfill ID"] == 1994  # the source cell is numeric; `raw` keeps it verbatim


def test_exact_coordinate_from_the_landfill_row(built):
    assets, _, _ = built
    row = assets[assets["source_asset_id"] == "167079-0"].iloc[0]
    assert row["lon"] == pytest.approx(-149.602138) and row["lat"] == pytest.approx(61.293281)
    assert row["placement_precision"] == "exact"
    assert row["state_code"] == "US-AK" and row["county_name"] == "Anchorage"
    assert row["county_fips"] == "02020"


def test_a_project_without_a_latitude_is_unplaced_with_a_county_centroid(built):
    assets, _, _ = built
    row = assets[assets["source_asset_id"] == "394-0"].iloc[0]  # Duarte LF, CA: no Latitude in the source
    assert pd.isna(row["lon"]) and pd.isna(row["lat"])  # None -> NaN in a float column (see test_eia_plants)
    assert row["status_raw"] == "Shutdown"
    assert row["county_name"] == "Los Angeles" and row["county_fips"] == "06037"
    assert row["placement_precision"] == "county_centroid"
    assert pd.notna(row["centroid_lon"])


def test_proposals_keep_the_same_shape_for_the_proposals_lane(built):
    _, proposals, _ = built
    row = proposals.iloc[0]
    assert row["source_id"] == SOURCE_ID
    assert row["status_raw"] in {"Planned", "Construction"}
    assert row["attributes"].get("landfill_count") == 1.0
