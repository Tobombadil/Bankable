"""Parser tests for mdb.worldbank.procnotices against a recorded API response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.mdb_worldbank_procnotices.connector import SECTOR_CODES

SOURCE_ID = "mdb.worldbank.procnotices"
URL = "https://search.worldbank.org/api/v2/procnotices"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("worldbank_procnotices.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_the_confirmed_sector_filter_is_the_dotted_path():
    c = connector_for(SOURCE_ID)
    params = c.params(0)
    assert "sector.sector_code" in params
    assert params["sector.sector_code"] == "^".join(SECTOR_CODES)
    assert "LU" in SECTOR_CODES and "LT" in SECTOR_CODES


def test_every_recorded_notice_is_in_an_energy_sector(parsed):
    _, _, rows, _ = parsed
    for r in rows:
        codes = {s["sector_code"] for s in r.get("sector") or []}
        assert codes & set(SECTOR_CODES)


def test_notice_id_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {str(r["id"]) for r in rows}
    assert not df["record_id"].duplicated().any()


def test_notice_types_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert (df.loc[df["status_raw"] == "Contract Award", "status"] == "awarded").all()
    assert df["status_rule"].str.startswith("worldbank.").all()


def test_an_open_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    open_types = df[df["status_raw"].isin(["Invitation for Bids", "Request for Expression of Interest"])]
    passed = open_types[open_types["due_at"] < open_types["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()


def test_contact_personal_data_is_dropped_at_parse_and_redact(parsed):
    c, _, rows, _ = parsed
    assert not any(k.startswith("contact_") and k != "contact_organization" for r in rows for k in r)
    payload = json.dumps(
        {
            "pages": [
                {
                    "procnotices": [
                        {
                            "id": "OP1",
                            "contact_email": "a@b.org",
                            "contact_name": "A Person",
                            "contact_organization": "Ministry",
                        }
                    ]
                }
            ]
        }
    ).encode()
    cleaned = json.loads(c.redact(payload))
    notice = cleaned["pages"][0]["procnotices"][0]
    assert "contact_email" not in notice and "contact_name" not in notice
    assert notice["contact_organization"] == "Ministry"


def test_sector_codes_and_country_reach_the_canonical_record(parsed):
    _, _, _, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["sector_codes"]
    assert df["jurisdiction"].str.len().le(6).all()


def test_a_page_without_notices_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("worldbank_procnotices.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"error": "bad"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
