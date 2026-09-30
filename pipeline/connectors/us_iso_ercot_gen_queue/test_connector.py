"""Parser tests for us.iso.ercot.gen_queue against a recorded GIS Report (docs/04 E-6)."""

from __future__ import annotations

import pandas as pd
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
    _, _, rows, df = parsed
    assert set(df["lifecycle_state"]) <= {"studied", "contracted", "under_construction", "built"}
    assert df["status_rule"].str.startswith("ercot.").all()
    # The fixture's 25 rows: 2 Active with no milestone, 4 with only an IA, 19 synchronised.
    assert df["lifecycle_state"].value_counts().to_dict() == {"built": 19, "contracted": 4, "studied": 2}
    # built only where ERCOT approved synchronisation; "Completed" alone (IA signed) is contracted
    synced = [pd.notna(r.get("Approved for Synchronization")) for r in rows]
    assert ((df["lifecycle_state"] == "built") == pd.Series(synced)).all()
    ia_only = (df["status_raw"] == "Completed") & (df["status_rule"] == "ercot.ia_signed")
    assert (df.loc[ia_only, "lifecycle_state"] == "contracted").all() and ia_only.sum() == 4
    # status_raw stays gridstatus's label, never the canonical state
    assert set(df["status_raw"]) == {"Completed", "Active"}


def test_restate_status_reads_each_stored_rows_raw_payload(parsed):
    c, _, _, df = parsed
    stored = df.copy()
    stored.loc[stored["status_raw"] == "Completed", "lifecycle_state"] = "built"  # the pre-2026-09-30 map
    restated = c.restate_status(stored)
    assert restated is not None
    assert list(restated.index) == list(stored.index)
    assert (restated["lifecycle_state"] == df["lifecycle_state"]).all()
    assert (restated["status_rule"] == df["status_rule"]).all()


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
