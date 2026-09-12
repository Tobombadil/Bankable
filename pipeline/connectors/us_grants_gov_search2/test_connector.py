"""Parser tests for us.grants_gov.search2 against a recorded Search2 response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError

SOURCE_ID = "us.grants_gov.search2"
URL = "https://api.grants.gov/v1/api/search2"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_opportunity_fields_are_the_docs21_ones(parsed):
    c, _, _, df = parsed
    assert c.kind == "opportunity"
    for col in ("kind", "issuer", "title", "jurisdiction", "technologies", "open_at", "due_at", "status"):
        assert col in df.columns
    assert "capacity_mw" not in df.columns  # proposal fields do not leak in
    assert set(df["kind"]) == {"foa"}
    assert set(df["jurisdiction"]) == {"US"}


def test_status_vocabulary_is_the_opportunity_one(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert df["status_rule"].str.startswith("grants_gov.").all()


def test_a_posted_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    posted = df[df["status_raw"] == "posted"]
    passed = posted[posted["due_at"] < posted["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()
    assert set(passed["status_rule"]) <= {"grants_gov.posted_deadline_passed"}


def test_identifiers_carry_the_opportunity_number(parsed):
    _, _, rows, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["grants_gov_number"] == rows[0]["number"]
    assert df["source_record_id"].iloc[0] == str(rows[0]["id"])


def test_html_entities_in_titles_are_unescaped(parsed):
    _, _, _, df = parsed
    assert not df["title"].str.contains("&ndash;|&amp;", regex=True, na=False).any()


def test_technologies_are_tokens_not_prose(parsed):
    _, _, _, df = parsed
    tokens = {t for v in df["technologies"] for t in str(v).split("|") if t}
    assert tokens <= {
        "solar_pv",
        "wind",
        "wind_offshore",
        "bess",
        "hydro",
        "nuclear",
        "hydrogen",
        "geothermal",
        "biomass",
        "ccs",
        "gas",
        "transmission",
        "heat",
        "ev_charging",
        "efficiency",
        "microgrid",
        "metering",
        "pumped_storage",
    }


def test_an_error_body_with_http_200_fails_closed():
    """docs/04 E-6: the source's "HTTP 200 with an error body" failure mode."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2_error.json", URL, "application/json")
    with pytest.raises(ParseError):
        c.parse(raw)
