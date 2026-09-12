"""Parser tests for eu.ted.api against a recorded TED search response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.eu_ted_api.connector import cpv_technologies, first_text

SOURCE_ID = "eu.ted.api"
URL = "https://api.ted.europa.eu/v3/notices/search"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ted_search.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_publication_number_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {str(r["publication-number"]) for r in rows}
    assert not df["record_id"].duplicated().any()
    assert df["source_url"].str.startswith("https://ted.europa.eu/en/notice/").all()


def test_multilingual_titles_prefer_english():
    assert first_text({"eng": "Wind farm", "pol": "Farma wiatrowa"}) == "Wind farm"
    assert first_text({"pol": "Farma wiatrowa"}) == "Farma wiatrowa"
    assert first_text(["a", "b"]) == "a"
    assert first_text(None) is None


def test_notice_types_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "awarded", "cancelled", "unknown"}
    assert (df.loc[df["status_raw"] == "can-standard", "status"] == "awarded").all()
    assert (df.loc[df["status_raw"] == "pmc", "status"] == "announced").all()


def test_no_unmapped_statuses_in_the_recorded_sample(parsed):
    _, _, _, df = parsed
    assert not df["status_rule"].str.endswith(".unmapped").any()


def test_a_competition_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    comp = df[(df["status_raw"] == "cn-standard") & df["due_at"].notna()]
    passed = comp[comp["due_at"] < comp["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()


def test_cpv_codes_drive_technology_tokens():
    assert cpv_technologies(["09331000"]) == ["solar_pv"]
    assert cpv_technologies(["45251100"]) == ["wind"]
    assert cpv_technologies(["99999999"]) == []


def test_buyer_country_becomes_iso_3166_alpha_2(parsed):
    _, _, _, df = parsed
    assert df["jurisdiction"].str.len().eq(2).all()


def test_no_contact_personal_data_is_requested_or_stored(parsed):
    c, _, rows, _ = parsed
    assert not any(k.lower().startswith("contact") or "email" in k.lower() for r in rows for k in r)
    assert not any("contact" in f.lower() for f in c.__class__.__module__ and [])


def test_identifiers_carry_the_notice_id_and_cpv(parsed):
    _, _, _, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["ted_notice_id"] == df["source_record_id"].iloc[0]
    assert isinstance(ids["cpv"], list)


def test_a_page_without_notices_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("ted_search.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"error": "bad query"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
