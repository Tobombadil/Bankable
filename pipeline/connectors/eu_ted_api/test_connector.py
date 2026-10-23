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


def test_a_zero_or_negative_value_is_an_unknown_budget(parsed):
    """TED states 0 or -1 where a notice gives no value (9 of 279 dev-snapshot notices with an
    amount, 2026-09-13). They are unknown budgets, not the smallest ones (lane I3)."""
    c, raw, rows, _ = parsed
    edited = [dict(r) for r in rows]
    edited[0].update({"estimated-value-glo": -1, "total-value": None, "estimated-value-cur-glo": "PLN"})
    edited[1].update({"estimated-value-glo": 0, "total-value": 0, "estimated-value-cur-glo": "EUR"})
    edited[2].update({"estimated-value-glo": 1250.5, "estimated-value-cur-glo": "EUR"})
    df = c.normalize(edited, raw)
    assert df["budget_amount"].isna()[0]
    assert df["budget_amount"].isna()[1]
    assert df.loc[2, "budget_amount"] == 1250.5
    assert list(df.loc[:1, "budget_currency"]) == ["PLN", "EUR"]  # the stated currency is kept


# ------------------------------------------------- what the value is (lane rfp-value, 2026-10-22)
@pytest.mark.parametrize(
    ("pubnum", "kind", "amount", "currency"),
    [
        ("619292-2026", "awarded_value", 4_673_237.67, "PLN"),  # result form: BT-161 notice value
        ("619362-2026", "awarded_value", 21_100_000.0, "DKK"),
        ("619343-2026", "estimated_value", 1_678_875.0, "CZK"),  # competition form: an estimate
        ("622360-2026", "contract_value", 500_000.0, "EUR"),  # direct award (VEAT)
        ("619495-2026", "contract_value", 10_132_381.64, "RON"),  # contract modification
    ],
)
def test_the_value_kind_follows_the_notice_form(parsed, pubnum, kind, amount, currency):
    _, _, _, df = parsed
    row = df.set_index("source_record_id").loc[pubnum]
    assert (row["budget_amount"], row["budget_currency"]) == (amount, currency)
    value = json.loads(row["identifiers"])["value"]
    assert value == {"kind": kind, "source_field": "total-value"}


def test_the_estimate_list_is_read_and_listed_beside_an_awarded_total():
    """The two notices of the 2026-10-20 store that stated `estimated-value-glo` carried it as a
    one-element list of strings, which the connector did not read, so they had no budget; these are
    one of them's own values (a result notice)."""
    from pipeline.connectors.eu_ted_api.connector import stated_value

    row = {
        "form-type": "result",
        "estimated-value-glo": ["3290325.87"],
        "estimated-value-cur-glo": ["EUR"],
        "total-value": 2427893.62,
        "total-value-cur": ["EUR"],
    }
    value = stated_value(row)
    assert value is not None
    assert (value.primary.kind, value.amount, value.currency) == ("awarded_value", 2_427_893.62, "EUR")
    assert [(f.kind, f.amount, f.source_field) for f in value.also_stated] == [
        ("estimated_value", 3_290_325.87, "estimated-value-glo")
    ]
    # On a competition form the estimate leads, and an equal total is not listed twice.
    competition = {**row, "form-type": "competition", "total-value": 3290325.87}
    value = stated_value(competition)
    assert value is not None and value.primary.source_field == "estimated-value-glo"
    assert value.also_stated == ()
    # A notice stating neither figure has no value (619389-2026 in the fixture), and no kind.
    assert stated_value({"form-type": "competition", "total-value": None}) is None
