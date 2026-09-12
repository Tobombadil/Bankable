"""Parser tests for us.iso.ercot.large_load_queue (EMIL catalogue watch).

Two fixtures: the real catalogue as it stood on 2026-09-12 (no Protocol 3.2.7 product exists) and
the same catalogue with one synthetic product row, proving the selector fires the day ERCOT
publishes the report.
"""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_iso_ercot_large_load_queue.connector import is_large_load_product

SOURCE_ID = "us.iso.ercot.large_load_queue"
URL = "https://www.ercot.com/api/1/services/read/common/filter-emil-items-search.json"


def test_todays_catalogue_contains_no_large_load_status_report():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ercot_emil_catalogue.json", URL, "application/json")
    rows = c.parse(raw)
    assert rows == []
    df = c.normalize(rows, raw)
    assert len(df) == 0
    assert list(df.columns) == c.columns


def test_the_selector_fires_when_the_report_appears():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ercot_emil_catalogue_with_report.json", URL, "application/json")
    rows = c.parse(raw)
    assert len(rows) == 1
    df = c.normalize(rows, raw)
    assert df["source_record_id"].iloc[0] == "np3-990-xx"
    assert df["kind"].iloc[0] == "load"
    assert df["lifecycle_state"].iloc[0] == "announced"
    assert df["source_url"].iloc[0].endswith("id=NP3-990-XX")
    for col in ("source_id", "source_url", "retrieved_at", "licence_id"):
        assert df[col].notna().all()


def test_secure_regional_planning_rows_are_not_the_report():
    assert not is_large_load_product(
        {
            "productName_s": "STEC Medina County Large Load Project",
            "productDescription_s": "Data for projects that have undergone Regional Planning Group Review.",
            "securityClassification_s": "Secure",
        }
    )
    assert not is_large_load_product(
        {
            "productName_s": "CPS Energy Large Load Additions Project",
            "productDescription_s": "Data for projects that have undergone Regional Planning Group Review.",
            "securityClassification_s": "Public",
        }
    )
    assert is_large_load_product(
        {
            "productName_s": "Large Load Interconnection Status Report",
            "productDescription_s": "Monthly aggregated status of Large Load Interconnection requests.",
            "securityClassification_s": "Public",
        }
    )


def test_a_non_list_catalogue_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ercot_emil_catalogue.json", URL, "application/json")
    raw.content = b'{"error": "not a list"}'
    with pytest.raises(ParseError):
        c.parse(raw)
