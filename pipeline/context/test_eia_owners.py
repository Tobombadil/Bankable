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
