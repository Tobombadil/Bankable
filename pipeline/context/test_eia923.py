"""Tests for `pipeline.context.eia923` against `fixtures/eia923_generation_fuel_sample.zip`: 19
rows of the real `EIA923_Schedules_2_3_4_5_M_12_2025_Final.xlsx` (from `f923_2025.zip`, fetched
2026-09-19, 23,340,452 bytes, sha256 1bff7092…), keeping the five title rows and the full 97-column
header, and covering the cases the aggregation has to get right: a plant with two fuel rows for one
prime mover (Barry, 3), a plant mixing combustion and wind (Sand Point, 1), hydro with EIA's
3,412 Btu/kWh energy equivalence in the fuel column (Bankhead Dam, 2), solar, coal, nuclear,
landfill gas and plants whose annual net generation is negative.

No network: `fetch` is exercised through the index-page parser and a stub session only.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

import pytest

from pipeline.connectors.base import ParseError
from pipeline.context import eia923

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "eia923_generation_fuel_sample.zip"

INDEX_HTML = b"""
<table><tr><td>2026: EIA-923 June 2026 <a href="xls/f923_2026.zip" class="ico zip">ZIP</a></td>
<td>2025: EIA-923 <a href="xls/f923_2025.zip" class="ico zip">ZIP</a></td>
<td>2024: EIA-923 <a href="archive/xls/f923_2024.zip" class="ico zip">ZIP</a></td></tr></table>
<p>Annual release date: September 14, 2026; Final release 2025 data</p>
"""


@pytest.fixture(scope="module")
def frame():
    return eia923.parse(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def features(frame):
    return eia923.aggregate(
        frame,
        retrieved_at="2026-09-19T21:52:51Z",
        source_url="https://www.eia.gov/electricity/data/eia923/xls/f923_2025.zip",
        licence_id="us.eia.form923#test",
    )


# ------------------------------------------------------------------------------ file discovery
def test_index_parsing_ignores_the_robots_disallowed_archive():
    found = eia923.annual_zips(INDEX_HTML)
    assert set(found) == {2025, 2026}  # 2024 is under archive/, which robots.txt disallows
    url, year = eia923.find_annual_zip(INDEX_HTML)
    assert year == 2026 and url.endswith("xls/f923_2026.zip")


def test_index_without_a_link_raises():
    with pytest.raises(ParseError):
        eia923.annual_zips(b"<html>no files</html>")


def test_an_early_release_workbook_is_refused(tmp_path):
    """EIA publishes the current year monthly; only the previous year is final. A partial year
    must not become a capacity factor, so `parse` refuses it and `run` steps back a year."""
    path = tmp_path / "early.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EIA923_Schedules_2_3_4_5_M_06_2026_21AUG2026.xlsx", b"not read: refused first")
    with pytest.raises(ParseError, match="not a final release"):
        eia923.parse(path.read_bytes())


def test_a_zip_without_the_generation_workbook_raises(tmp_path):
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EIA923_Schedule_8_Annual_Envir_Infor_2025_Final.xlsx", b"wrong schedule")
    with pytest.raises(ParseError):
        eia923.parse(path.read_bytes())


# -------------------------------------------------------------------------------------- parsing
def test_parse_finds_the_header_under_the_title_rows(frame):
    assert len(frame) == 19
    assert "Plant Id" in frame.columns
    # The workbook's header cells carry embedded newlines; they are normalised to single spaces.
    assert "Net Generation (Megawatthours)" in frame.columns
    assert "Total Fuel Consumption MMBtu" in frame.columns
    assert frame.attrs["member"].endswith("_Final.xlsx")


def test_parse_rejects_a_workbook_without_the_sheet(tmp_path):
    import openpyxl

    book = openpyxl.Workbook()
    book.active.title = "Page 2 Stocks Data"
    buf = io.BytesIO()
    book.save(buf)
    path = tmp_path / "wrong.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EIA923_Schedules_2_3_4_5_M_12_2025_Final.xlsx", buf.getvalue())
    with pytest.raises(ParseError):
        eia923.parse(path.read_bytes())


# ---------------------------------------------------------------------------------- aggregation
def test_one_row_per_plant_summing_its_fuel_rows(features, frame):
    assert list(features.columns) == eia923.COLUMNS
    assert features["plant_id"].is_unique
    assert len(features) == frame["Plant Id"].nunique()
    barry = features.set_index("plant_id").loc["3"]
    assert barry["fuel_rows"] == 2
    assert barry["net_generation_mwh"] == pytest.approx(3919403 + 7540378)
    assert barry["total_fuel_mmbtu"] == pytest.approx(2787461 + 75192884)


def test_a_plant_keeps_every_fuel_code_it_reported(features):
    sand_point = features.set_index("plant_id").loc["1"]
    assert sand_point["fuel_types"] == ["DFO", "WND"]
    assert sand_point["plant_state"] == "AK"


def test_negative_annual_net_generation_is_preserved_not_clamped(features):
    assert (features["net_generation_mwh"] < 0).any()


def test_year_and_provenance_ride_every_row(features):
    assert set(features["data_year"]) == {2025}
    assert set(features["source_id"]) == {"us.eia.form923"}
    assert set(features["licence"]) == {"public-domain"}
    assert set(features["licence_id"]) == {"us.eia.form923#test"}
    assert set(features["retrieved_at"]) == {"2026-09-19T21:52:51Z"}


def test_hydro_carries_eias_energy_equivalence_rather_than_burned_fuel(features):
    """Bankhead Dam reports 514,451 MMBtu against 150,777 MWh -- exactly 3,412 Btu/kWh, EIA's
    equivalence for non-combustion generation. The connector stores it as filed; deciding that this
    is not a heat rate is the enrichment's job (`services/ingest/enrich.py`)."""
    row = features.set_index("plant_id").loc["2"]
    assert row["fuel_types"] == ["WAT"]
    ratio = row["total_fuel_mmbtu"] * 1000 / row["net_generation_mwh"]
    assert ratio == pytest.approx(3412, abs=1)


def test_empty_input_gives_an_empty_frame_with_the_contract_columns():
    import pandas as pd

    out = eia923.aggregate(pd.DataFrame(), retrieved_at="x", source_url="y")
    assert list(out.columns) == eia923.COLUMNS
    assert not len(out)


def test_plant_id_normalisation():
    assert eia923._plant_id(3.0) == "3"
    assert eia923._plant_id("  3 ") == "3"
    assert eia923._plant_id("Plant Id") is None
    assert eia923._plant_id(None) is None


def test_cli_prints_one_json_line_from_a_snapshot(tmp_path, capsys):
    """Guards the summary line: `thermal_plants` is a pandas aggregate (numpy int64), which
    `json.dumps` refuses without the repo's coercion."""
    eia923.main(["--snapshot", str(FIXTURE), "--out", str(tmp_path / "out.parquet")])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["source_id"] == eia923.SOURCE_ID
    assert payload["plants"] == 15
    assert payload["thermal_plants"] >= 1
