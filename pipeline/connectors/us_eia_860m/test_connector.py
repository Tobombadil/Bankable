"""Parser tests for us.eia.860m against a recorded Planned sheet, plus the index-page scrape."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_eia_860m.connector import find_xlsx_links

SOURCE_ID = "us.eia.860m"
URL = "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("eia860m_planned.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_planned_sheet_header_is_on_the_third_row(parsed):
    _, _, rows, _ = parsed
    assert {"Plant ID", "Generator ID", "Status", "Technology"} <= set(rows[0])


def test_record_id_is_plant_plus_generator(parsed):
    _, _, rows, df = parsed
    expected = f"{int(rows[0]['Plant ID'])}-{str(rows[0]['Generator ID']).strip()}"
    assert df["source_record_id"].iloc[0] == expected
    assert not df["record_id"].duplicated().any()
    assert df["eia_plant_id"].notna().all()


def test_planned_statuses_map_onto_the_documented_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["lifecycle_state"]) <= {"announced", "filed", "permitted", "under_construction"}
    assert (df["status_rule"] == "eia860m.map").all()


def test_construction_complete_is_not_built(parsed):
    _, _, _, df = parsed
    ts = df[df["status_raw"].str.startswith("(TS)", na=False)]
    assert set(ts["lifecycle_state"]) <= {"under_construction"}


def test_index_links_are_returned_in_page_order():
    html = (
        '<a href="/electricity/data/eia860m/xls/december_generator2026.xlsx">dec</a>'
        '<a href="/electricity/data/eia860m/archive/xls/november_generator2026.xlsx">nov</a>'
        '<a href="/electricity/data/eia860m/xls/december_generator2026.xlsx">dup</a>'
    )
    links = find_xlsx_links(html)
    assert links == [
        "https://www.eia.gov/electricity/data/eia860m/xls/december_generator2026.xlsx",
        "https://www.eia.gov/electricity/data/eia860m/archive/xls/november_generator2026.xlsx",
    ]


def test_an_html_placeholder_month_is_a_parse_error():
    """A guessed/future month answers HTTP 200 with HTML (docs/02 §7)."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL)
    raw.content = b"<!doctype html><html><body>Page not found</body></html>"
    with pytest.raises(ParseError):
        c.parse(raw)
