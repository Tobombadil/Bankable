"""Tests for `pipeline.context.agstar` against `fixtures/agstar_digesters_sample.xlsx`: the real
`agstar-livestock-ad-database.xlsx` fetched from
https://www.epa.gov/sites/default/files/2020-10/agstar-livestock-ad-database.xlsx on 2026-09-19
(98,977 bytes; page says "based on data available through June 2024"; 473 + 98 rows), trimmed to
18 rows of "Operational and Construction" (including three Construction rows and the one row
whose City is blank) and 7 rows of "Shutdown".
"""

from __future__ import annotations

import io
import pathlib

import openpyxl
import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context import fuels
from pipeline.context.agstar import SOURCE_ID, build_rows, find_workbook_url, parse_workbook

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "agstar_digesters_sample.xlsx"


@pytest.fixture(scope="module")
def parsed():
    return parse_workbook(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def built(parsed):
    return build_rows(
        parsed,
        retrieved_at="2026-09-19T15:20:37Z",
        source_url="https://www.epa.gov/sites/default/files/2020-10/agstar-livestock-ad-database.xlsx",
        licence_id="us.epa.agstar#test",
    )


def test_find_workbook_url_follows_the_page_link():
    html = b'<a href="https://www.epa.gov/sites/default/files/2031-01/agstar-livestock-ad-database.xlsx">'
    assert find_workbook_url(html).endswith("2031-01/agstar-livestock-ad-database.xlsx")
    with pytest.raises(ParseError):
        find_workbook_url(b"nothing")


def test_both_sheets_are_read_by_column_name(parsed):
    assert len(parsed) == 25
    assert set(parsed["sheet"]) == {"Operational and Construction", "Shutdown"}
    # The Shutdown sheet's extra columns exist on the stacked frame and are blank elsewhere.
    assert "Year Shutdown" in parsed.columns and "Reason for Closure" in parsed.columns
    assert parsed.loc[parsed["sheet"] != "Shutdown", "Year Shutdown"].isna().all()
    # One of the seven real shutdown rows has no Year Shutdown in the source; the rest do.
    assert parsed.loc[parsed["sheet"] == "Shutdown", "Year Shutdown"].notna().sum() == 6


def test_a_changed_layout_raises_parse_error():
    wb = openpyxl.Workbook()
    wb.active.title = "Operational and Construction"
    wb.active.append(["Farm", "Where"])
    wb.create_sheet("Shutdown").append(["Farm", "Where"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError):
        parse_workbook(buf.getvalue())


def test_statuses_route_to_assets_and_proposals(built):
    assets, proposals, dropped = built
    assert set(assets["status_raw"]) == {"Operational", "Shut down"}
    assert set(assets["status"]) == {"operating", "retired"}
    assert set(proposals["status_raw"]) == {"Construction"}
    assert len(proposals) == 3
    assert dropped == {}
    assert len(assets) + len(proposals) == 25
    assert list(assets.columns) == fuels.ASSET_COLUMNS


def test_feature_set_and_provenance(built):
    assets, _, _ = built
    row = assets[assets["name"] == "Cargill - Sandy River Farm Digester"].iloc[0]
    assert row["source_asset_id"] == fuels.content_key("Cargill - Sandy River Farm Digester", "AR")
    assert row["technology"] == "farm_digester"
    assert row["technology_raw"] == "Covered Lagoon"
    assert row["feedstock"] == "Swine"
    assert row["capacity_value"] == pytest.approx(1814400.0) and row["capacity_unit"] == "cu-ft/day"
    assert row["commissioned_year"] == 2008
    assert row["attributes"]["swine"] == 4200.0
    assert row["attributes"]["lcfs_pathway"] == 0.0 and row["attributes"]["awarded_usda_funding"] == 0.0
    assert all(isinstance(v, float) for v in row["attributes"].values())
    assert row["attributes_text"]["biogas_end_uses"] == "Flared Full-time"
    assert row["attributes_text"]["project_type"] == "Farm Scale"
    assert row["developer_raw"].startswith("Martin Construction Resource LLC")
    assert row["operator_name"] is None and row["owner_raw"] is None
    assert row["source_id"] == SOURCE_ID
    assert row["licence"] == "public-domain" and row["licence_id"] == "us.epa.agstar#test"
    assert row["retrieved_at"] == "2026-09-19T15:20:37Z"


def test_rows_are_unplaced_with_a_county_centroid_and_fips(built):
    assets, _, _ = built
    row = assets[assets["name"] == "Cargill - Sandy River Farm Digester"].iloc[0]
    assert row["lon"] is None and row["lat"] is None
    assert row["state_code"] == "US-AR" and row["county_name"] == "Conway"
    assert row["county_fips"] == "05029"
    assert row["placement_precision"] == "county_centroid"
    assert row["centroid_lon"] == pytest.approx(-92.689249, abs=1e-3)


def test_shutdown_rows_carry_closure_year_and_reason(built):
    assets, _, _ = built
    shut = assets[assets["status"] == "retired"]
    assert len(shut) == 7
    assert shut["attributes"].map(lambda a: a.get("year_shutdown")).notna().sum() == 6
    assert shut["attributes_text"].map(lambda a: a.get("sheet")).eq("Shutdown").all()


def test_keys_are_unique_and_a_usda_flag_reads_true(built):
    assets, proposals, _ = built
    keys = pd.concat([assets["source_asset_id"], proposals["source_asset_id"]])
    assert keys.is_unique
    flagged = assets[assets["attributes"].map(lambda a: a.get("awarded_usda_funding") == 1.0)]
    assert len(flagged) >= 1
