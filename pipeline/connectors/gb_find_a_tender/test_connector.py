"""Parser tests for gb.find_a_tender against a recorded OCDS release package."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.gb_find_a_tender.connector import is_energy, release_cpvs, strip_contacts

SOURCE_ID = "gb.find_a_tender"
URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_only_energy_cpv_releases_are_kept(parsed):
    _, _, rows, df = parsed
    assert len(rows) == 8  # 11 releases in the fixture, 3 of them non-energy
    assert all(is_energy(r["cpv"]) for r in rows)
    assert len(df) == 8


def test_cpv_codes_are_collected_from_items_and_classification():
    rel = {
        "tender": {
            "classification": {"scheme": "CPV", "id": "09310000"},
            "items": [{"additionalClassifications": [{"scheme": "CPV", "id": "45231400"}]}],
        }
    }
    assert release_cpvs(rel) == ["09310000", "45231400"]
    assert is_energy(["79710000"]) is False


def test_ocid_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {r["ocid"] for r in rows}
    assert not df["record_id"].duplicated().any()


def test_status_and_tags_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert df["status_rule"].str.startswith("find_a_tender.").all()
    assert set(df["jurisdiction"]) == {"GB"}


def test_contact_points_are_stripped_before_the_snapshot_is_stored():
    package = {
        "releases": [
            {
                "parties": [{"name": "A Council", "contactPoint": {"name": "Jane", "email": "j@x.gov.uk"}}],
                "buyer": {"name": "A Council", "contactPoint": {"email": "j@x.gov.uk"}},
            }
        ]
    }
    cleaned = strip_contacts(package)
    assert "contactPoint" not in cleaned["releases"][0]["parties"][0]
    assert "contactPoint" not in cleaned["releases"][0]["buyer"]
    assert cleaned["releases"][0]["parties"][0]["name"] == "A Council"


def test_the_recorded_fixture_holds_no_contact_personal_data():
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    assert b"contactPoint" not in raw.content


def test_the_latest_release_per_ocid_wins():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    doc = json.loads(raw.content)
    rel = doc["pages"][0]["releases"][0]
    older = json.loads(json.dumps(rel))
    older["date"] = "2000-01-01T00:00:00Z"
    older["tender"]["title"] = "stale"
    doc["pages"][0]["releases"].append(older)
    raw.content = json.dumps(doc).encode()
    rows = c.parse(raw)
    row = next(r for r in rows if r["ocid"] == rel["ocid"])
    assert row["tender"]["title"] != "stale"


def test_a_page_without_releases_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"detail": "error"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
