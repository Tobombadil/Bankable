"""Tests for `pipeline.context.gleif` against the trimmed golden-copy fixtures
(`pipeline/context/fixtures/gleif_rr_sample.csv.zip`, `gleif_lei2_sample.csv.zip`, cut from the
real 2026-09-20 publish).

The fixtures keep the three shapes the parser has to get right: three fund/sub-fund relationships
that are **not** ownership and must be dropped; one consolidation relationship whose child has no
row in the entity file, which must be dropped and counted rather than written with a blank name;
and the whole Tallgrass family, which is this lane's coverage test case — five of its operating
companies hold an LEI and none of them is the start node of a relationship record, so the curated
file (`data/vendored/organizations/parents.yaml`) is not replaceable by GLEIF for any of them.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context.gleif import (
    CONSOLIDATION_TYPES,
    OUTPUT_COLUMNS,
    BuildResult,
    build,
    latest_publish,
    read_entities,
    read_relationships,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
RR = FIXTURES / "gleif_rr_sample.csv.zip"
LEI2 = FIXTURES / "gleif_lei2_sample.csv.zip"


@pytest.fixture(scope="module")
def built() -> tuple[pd.DataFrame, BuildResult]:
    return build(RR, LEI2, source_url="https://www.gleif.org/", retrieved_at="2026-09-20T02:19:00Z")


def test_only_the_two_consolidation_types_survive(built):
    df, result = built
    assert result.relationships_seen == 145
    assert result.consolidation_relationships == 142
    assert set(df["relationship_type"]) <= set(CONSOLIDATION_TYPES)
    # the three fund/sub-fund rows in the fixture are ownership of nothing and are dropped
    assert result.relationships_seen - result.consolidation_relationships == 3


def test_a_relationship_whose_lei_has_no_entity_row_is_dropped_and_counted(built):
    df, result = built
    assert result.dropped_unnamed_lei == 1
    assert result.leis_wanted - result.leis_named == 1
    assert len(df) == result.consolidation_relationships - result.dropped_unnamed_lei == 141


def test_output_has_exactly_the_documented_columns(built):
    df, _ = built
    assert list(df.columns) == OUTPUT_COLUMNS


def test_start_node_is_the_child_and_end_node_the_parent(built):
    df, _ = built
    row = df[
        (df["child_legal_name"] == "GEORGIA POWER COMPANY")
        & (df["relationship_type"] == "IS_DIRECTLY_CONSOLIDATED_BY")
    ].iloc[0]
    assert row["parent_legal_name"] == "THE SOUTHERN COMPANY"
    assert row["child_country"] == "US"


def test_dates_are_dates_not_timestamps(built):
    df, _ = built
    dated = df[df["period_start"].notna()]
    assert len(dated)
    assert dated["period_start"].map(lambda v: isinstance(v, dt.date)).all()
    assert (df["licence"] == "cc0").all()


def test_tallgrass_operating_companies_hold_leis_but_no_level_two_record():
    """The curated file's nine children are not replaceable by GLEIF (docs/22 §17.3)."""
    entities = read_entities(LEI2, {"__none__"})
    assert entities == {}
    all_rows = read_relationships(RR, BuildResult())
    children = {r["child_lei"] for r in all_rows}
    # the five Tallgrass operating companies that do hold an LEI
    for lei in (
        "5493001PPWSDLETMIS87",  # TALLGRASS INTERSTATE GAS TRANSMISSION, LLC
        "W2ZGZGZKY5GGNY6F3V51",  # ROCKIES EXPRESS PIPELINE LLC
        "549300R4CX55WWLTX861",  # TRAILBLAZER PIPELINE COMPANY LLC
        "549300VXRTBPBK07QT94",  # RUBY PIPELINE, L.L.C.
        "MVIXRNC8YS7D5NZBAG33",  # EAST CHEYENNE GAS STORAGE, LLC
    ):
        assert lei not in children


def test_entity_reader_refuses_a_changed_column_layout(tmp_path: pathlib.Path):
    import csv
    import io
    import zipfile

    target = tmp_path / "bad.csv.zip"
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(["LEI", "Entity.SomethingElse"])
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("bad.csv", buffer.getvalue())
    with pytest.raises(ParseError, match="LEI CDF layout changed"):
        read_entities(target, {"X"})


def test_latest_publish_takes_the_newest_entry_and_the_csv_full_files():
    payload = {
        "data": [
            {
                "publish_date": "2026-09-20 00:00:00",
                "rr": {
                    "publish_date": "2026-09-20 00:00:00",
                    "full_file": {"csv": {"url": "https://example/rr.csv.zip", "record_count": 3, "size": 9}},
                },
                "lei2": {
                    "publish_date": "2026-09-20 00:00:00",
                    "full_file": {
                        "csv": {"url": "https://example/lei2.csv.zip", "record_count": 4, "size": 8}
                    },
                },
            }
        ]
    }
    files = latest_publish(payload)
    assert files["rr"].url.endswith("rr.csv.zip")
    assert files["lei2"].published_records == 4
    with pytest.raises(ParseError):
        latest_publish({"data": []})
