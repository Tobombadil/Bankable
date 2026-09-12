"""Parser tests for us.iso.ercot.gen_queue against a recorded GIS Report (docs/04 E-6)."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_iso_ercot_gen_queue.connector import latest_gis_document

SOURCE_ID = "us.iso.ercot.gen_queue"
URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ercot_gis_report.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_parse_returns_source_shaped_rows(parsed):
    _, _, rows, _ = parsed
    assert len(rows) == 25
    assert {"Queue ID", "Project Name", "Status", "Capacity (MW)", "Generation Type"} <= set(rows[0])


def test_canonical_record_carries_the_provenance_quartet(parsed):
    c, raw, _, df = parsed
    assert len(df) == 25
    for col in ("source_id", "source_url", "retrieved_at", "licence_id"):
        assert df[col].notna().all()
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["source_url"]) == {URL}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["retrieved_at"].iloc[0] == raw.retrieved_at_iso


def test_record_id_is_the_queue_id_and_unique(parsed):
    _, _, rows, df = parsed
    assert not df["record_id"].duplicated().any()
    assert df["record_id"].iloc[0] == f"{SOURCE_ID}:{df['source_record_id'].iloc[0]}"
    assert set(df["source_record_id"]) == {str(r["Queue ID"]).strip() for r in rows}


def test_status_harmonisation_uses_the_ercot_rules(parsed):
    _, _, _, df = parsed
    assert set(df["lifecycle_state"]) <= {"studied", "built", "under_construction"}
    assert (df.loc[df["status_raw"] == "Completed", "lifecycle_state"] == "built").all()
    assert df["status_rule"].str.startswith("ercot.").all()


def test_raw_payload_is_kept_per_row(parsed):
    _, _, rows, df = parsed
    import json

    payload = json.loads(df["raw"].iloc[0])
    assert payload["Queue ID"] == str(rows[0]["Queue ID"])
    assert "Generation Type" in payload


def test_technology_and_capacity_are_normalised(parsed):
    _, _, _, df = parsed
    assert df["technology"].notna().all()
    assert set(df["kind"]) <= {"generation", "storage", "transmission", "load", "other"}
    assert (df["capacity_mw"].dropna() > 0).all()


def test_parse_rejects_a_non_xlsx_payload():
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL)
    with pytest.raises(ParseError):
        c.parse(raw)


def test_latest_gis_document_picks_the_newest_report_not_the_battery_report():
    listing = {
        "ListDocsByRptTypeRes": {
            "DocumentList": [
                {
                    "Document": {
                        "ConstructedName": "RPT.x.Co-located_Battery_Identification_Report_August_2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-09-09T10:23:32-05:00",
                        "DocID": "1",
                    }
                },
                {
                    "Document": {
                        "ConstructedName": "RPT.x.GIS_Report_July2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-08-01T14:38:04-05:00",
                        "DocID": "2",
                    }
                },
                {
                    "Document": {
                        "ConstructedName": "RPT.x.GIS_Report_August2026.xlsx",
                        "Extension": "xlsx",
                        "PublishDate": "2026-09-01T14:38:04-05:00",
                        "DocID": "3",
                    }
                },
            ]
        }
    }
    assert latest_gis_document(listing)["DocID"] == "3"


def test_latest_gis_document_raises_when_the_listing_has_none():
    with pytest.raises(ParseError):
        latest_gis_document({"ListDocsByRptTypeRes": {"DocumentList": []}})
