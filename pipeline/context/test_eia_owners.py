"""Tests for `pipeline.context.eia_owners` against the recorded 60-row Ownership-sheet fixture
(`tests/fixtures/eia860_owner_2024_sample.zip`, trimmed from the real 2024 archive, 2026-09-18;
see `tests/fixtures/README.md`). The fixture deliberately keeps the three messy real rows this
module has to handle: one row with no Generator ID, and two rows with a blank `Percent Owned`.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from conftest import fixture_path
from pipeline.context.eia_owners import OUTPUT_COLUMNS, build_owner_rows, extract_owner_sheet

FIXTURE = "eia860_owner_2024_sample.zip"


@pytest.fixture(scope="module")
def sheet():
    df, year = extract_owner_sheet(fixture_path(FIXTURE).read_bytes())
    return df, year


@pytest.fixture(scope="module")
def owners(sheet):
    df, year = sheet
    return build_owner_rows(
        df,
        year=year,
        source_url="https://www.eia.gov/electricity/data/eia860/archive/xls/eia8602024.zip",
        retrieved_at="2026-09-18T00:00:00Z",
    )


def test_extract_owner_sheet_finds_the_2024_member_and_sixty_raw_rows(sheet):
    df, year = sheet
    assert year == 2024
    assert len(df) == 60
    assert {"Plant Code", "Generator ID", "Owner Name", "Percent Owned"} <= set(df.columns)


def test_output_has_exactly_the_documented_columns(owners):
    assert list(owners.columns) == OUTPUT_COLUMNS


def test_the_row_with_no_generator_id_is_dropped(owners):
    # 60 raw rows, 1 with no Generator ID at all (Cranberry Point Energy Storage) -> 59 kept.
    assert len(owners) == 59
    assert "Enel Energy Storage Holdings LLC" not in set(owners["owner_name"])


def test_dtypes(owners):
    assert owners["source_plant_id"].map(type).eq(str).all()
    assert owners["generator_id"].map(type).eq(str).all()
    assert owners["owner_name"].map(type).eq(str).all()
    assert pd.api.types.is_float_dtype(owners["ownership_pct"])
    assert owners["as_of"].map(lambda v: isinstance(v, dt.date)).all()


def test_ownership_pct_is_a_percentage_not_a_fraction(owners):
    # The source column is a 0-1 fraction; two blank cells stay null rather than being dropped.
    non_null = owners["ownership_pct"].dropna()
    assert len(non_null) == 57
    assert non_null.between(0, 100).all()
    assert non_null.max() == pytest.approx(100.0)
    row = owners[(owners["source_plant_id"] == "10") & (owners["generator_id"] == "1")]
    assert set(row["ownership_pct"]) == {60.0, 40.0}


def test_blank_percent_owned_rows_are_kept_with_a_null_pct(owners):
    row = owners[owners["owner_name"] == "Florida Municipal Power Agency"]
    assert len(row) == 1
    assert row["ownership_pct"].isna().all()


def test_as_of_is_december_31_of_the_data_year(owners):
    assert set(owners["as_of"]) == {dt.date(2024, 12, 31)}


def test_provenance_columns(owners):
    row = owners.iloc[0]
    assert row["source_url"] == "https://www.eia.gov/electricity/data/eia860/archive/xls/eia8602024.zip"
    assert row["retrieved_at"] == "2026-09-18T00:00:00Z"
    assert row["licence"] == "public-domain"


# ---------------------------------------------------------------- Schedule 3 nameplate join (2026-09-19)
def _zip_with_generator_member(owner_zip: bytes, rows: list[tuple[int, str, float | None]]) -> bytes:
    """The fixture's owner member plus a synthetic `3_1_Generator_Y2024.xlsx` whose "Operable"
    sheet has the real layout: a title row, then the header row, then `rows`."""
    import io
    import zipfile

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Operable"
    ws.append(["2024 Form EIA-860 Data - Schedule 3, 'Generator Data' (Operable Units Only)"])
    ws.append(["Utility ID", "Plant Code", "Generator ID", "Technology", "Nameplate Capacity (MW)"])
    for plant, gen, mw in rows:
        ws.append([1, plant, gen, "Conventional Steam Coal", mw])
    xlsx = io.BytesIO()
    wb.save(xlsx)

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(owner_zip)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("3_1_Generator_Y2024.xlsx", xlsx.getvalue())
    return out.getvalue()


def test_fixture_has_no_generator_member_so_capacity_columns_are_null(owners):
    from pipeline.context.eia_owners import extract_generator_capacity

    assert extract_generator_capacity(fixture_path(FIXTURE).read_bytes()) is None
    assert owners["generator_capacity_mw"].isna().all()
    assert owners["plant_capacity_mw"].isna().all()
    assert pd.api.types.is_float_dtype(owners["generator_capacity_mw"])
    assert pd.api.types.is_float_dtype(owners["plant_capacity_mw"])


def test_generator_status_is_carried_from_the_sheet(owners):
    assert owners["generator_status"].notna().all()
    assert set(owners["generator_status"]) <= {
        "OP",
        "SB",
        "OS",
        "RE",
        "CN",
        "P",
        "L",
        "T",
        "U",
        "V",
        "TS",
        "IP",
        "OA",
    }
    assert (
        owners[(owners["source_plant_id"] == "10") & (owners["generator_id"] == "1")]["generator_status"]
        == "OP"
    ).all()


def test_generator_nameplate_joins_per_generator_and_plant_total_counts_unlisted_units(sheet):
    """Plant 10 has generators 1 and 2 on Schedule 4 (60/40 and 100 % rows); the synthetic
    Schedule 3 also lists generator 3 (wholly operator-owned, so absent from Schedule 4) -- the
    plant total must include it, because the loader's share is "share of the plant's nameplate"."""
    from pipeline.context.eia_owners import extract_generator_capacity

    df, year = sheet
    archive = _zip_with_generator_member(
        fixture_path(FIXTURE).read_bytes(),
        [(10, "1", 250.0), (10, "2", 250.0), (10, "3", 500.0), (26, "1", 0.0)],
    )
    generators = extract_generator_capacity(archive)
    assert generators is not None
    assert len(generators) == 4
    owners = build_owner_rows(
        df,
        year=year,
        source_url="https://example.test/eia860.zip",
        retrieved_at="2026-09-19T00:00:00Z",
        generators=generators,
    )
    assert list(owners.columns) == OUTPUT_COLUMNS
    gen1 = owners[(owners["source_plant_id"] == "10") & (owners["generator_id"] == "1")]
    assert set(gen1["generator_capacity_mw"]) == {250.0}
    assert set(gen1["plant_capacity_mw"]) == {1000.0}
    # A reported 0 MW nameplate is "unknown", not a zero-MW unit; a plant with only such rows has no total.
    p26 = owners[owners["source_plant_id"] == "26"]
    assert p26["generator_capacity_mw"].isna().all()
    assert p26["plant_capacity_mw"].isna().all()
    # A plant Schedule 3 does not list at all keeps both columns null.
    other = owners[~owners["source_plant_id"].isin({"10", "26"})]
    assert other["generator_capacity_mw"].isna().all()
    assert other["plant_capacity_mw"].isna().all()


def test_snapshot_metadata_reads_the_run_record_for_a_store_snapshot(tmp_path, monkeypatch):
    import json

    from pipeline.context import eia_owners

    monkeypatch.setattr(eia_owners, "RUNS_DIR", tmp_path)
    stamp = "20260919T204822Z"
    (tmp_path / f"{stamp}.json").write_text(
        json.dumps(
            {"snapshot": {"fetched_url": "https://www.eia.gov/electricity/data/eia860/xls/eia8602025.zip"}}
        )
    )
    retrieved_at, url = eia_owners._snapshot_metadata(tmp_path / f"{stamp}.zip", 2025)
    assert retrieved_at == "2026-09-19T20:48:22Z"
    assert url == "https://www.eia.gov/electricity/data/eia860/xls/eia8602025.zip"


def test_snapshot_metadata_falls_back_to_the_archive_template_for_a_browser_download(tmp_path):
    from pipeline.context.eia_owners import _snapshot_metadata

    retrieved_at, url = _snapshot_metadata(tmp_path / "eia8602024.zip", 2024)
    assert url == "https://www.eia.gov/electricity/data/eia860/archive/xls/eia8602024.zip"
    assert retrieved_at.endswith("Z")
