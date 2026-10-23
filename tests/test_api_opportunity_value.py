"""`value` on opportunities, `sort=value` and the `min_value`/`max_value` bounds (lane rfp-value,
2026-10-22; `services/api/opportunity_value.py`).

Each record's `identifiers["value"]` is built here exactly as the loader builds it
(`services/ingest/opportunity_value.with_value`) from the recorded ECB history slice, so the
equivalents below are the fixture's arithmetic: EUR 1,000,000 on 2026-10-06 at USD 1.1269 per euro
is USD 1,126,900; GBP 800,000,000 at 1.1269 / 0.8488 is USD 1,062,111,215.83. Budgets stay in their
own currency (`budget_amount`, lane E16); `value.usd_equivalent` is the one cross-currency scale.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import fixture_path
from pipeline import fx
from services.api.conftest import (
    make_attribution_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
)
from services.db.models import Opportunity
from services.ingest.opportunity_value import with_value
from tests.conftest import login, make_account, make_user

SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text())
READ_AT = "2026-10-22T01:00:00Z"

#: suffix -> (amount, currency, publication date, connector block)
ROWS: dict[str, tuple[float | None, str | None, dt.date | None, dict[str, Any]]] = {
    "usd": (
        5_000_000,
        "USD",
        dt.date(2026, 9, 17),
        {"kind": "estimated_value", "source_field": "bid_estimate_amount"},
    ),
    "eur": (
        1_000_000,
        "EUR",
        dt.date(2026, 10, 6),
        {"kind": "estimated_value", "source_field": "total-value"},
    ),
    "ron": (247_670, "RON", dt.date(2026, 10, 6), {"kind": "awarded_value", "source_field": "total-value"}),
    "gbp": (
        800_000_000,
        "GBP",
        dt.date(2026, 10, 6),
        {
            "kind": "estimated_value",
            "source_field": "tender.value.amount",
            "also_stated": [
                {
                    "kind": "estimated_value_incl_vat",
                    "amount": 960_000_000.0,
                    "currency": "GBP",
                    "source_field": "tender.value.amountGross",
                }
            ],
        },
    ),
    "mkd": (
        10_000_000,
        "MKD",
        dt.date(2026, 10, 6),
        {"kind": "estimated_value", "source_field": "total-value"},
    ),
    "none": (None, None, dt.date(2026, 10, 6), {}),
    # Grants.gov 363912's stated total is the largest 32-bit integer: shown, never ranked first.
    "int32": (
        2_147_483_647,
        "USD",
        dt.date(2026, 10, 1),
        {"kind": "total_programme_funding", "source_field": "synopsis.estimatedFunding"},
    ),
    "grant": (
        410_000_000,
        "USD",
        dt.date(2026, 9, 8),
        {
            "kind": "total_programme_funding",
            "source_field": "synopsis.estimatedFunding",
            "also_stated": [
                {
                    "kind": "award_ceiling",
                    "amount": 1e8,
                    "currency": "USD",
                    "source_field": "synopsis.awardCeiling",
                }
            ],
            "expected_awards": 100,
            "retrieved_at": READ_AT,
            "endpoint": "https://api.grants.gov/v1/api/fetchOpportunity",
        },
    ),
}
#: Largest USD equivalent first; MKD (no ECB rate), the placeholder and the notice with no amount last.
DESC = ["gbp", "grant", "usd", "eur", "ron"]
ASC = ["ron", "eur", "usd", "grant", "gbp"]
NULLS = {"mkd", "none", "int32"}


@pytest.fixture(scope="module")
def table() -> fx.RateTable:
    text = fixture_path("ecb_eurofxref_hist.csv").read_text(encoding="utf-8")
    return fx.RateTable(fx.parse_hist_csv(text), retrieved_at="2026-10-22T01:01:38Z", origin="fetched")


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture()
def world(db: Session, table: fx.RateTable) -> dict[str, Any]:
    lic = make_attribution_licence(db)
    src = make_public_source(db, lic)
    issuer = make_org(db, "Value Issuing Agency")
    rows: dict[str, Opportunity] = {}
    for suffix, (amount, currency, opened, block) in ROWS.items():
        opp = make_visible_opportunity(db, src, public_id_suffix=suffix)
        fields = with_value(
            {
                "budget_amount": amount,
                "budget_currency": currency,
                "open_at": opened,
                "identifiers": {"value": block} if block else {},
            },
            table,
        )
        opp.budget_amount, opp.budget_currency, opp.open_at = amount, currency, opened
        opp.identifiers = fields["identifiers"]
        opp.issuer_org_id = issuer.id
        stamp = {"source_id": src.id, "licence_id": lic.id, "retrieved_at": "2026-10-21T00:00:00+00:00"}
        opp.field_provenance = {"budget_amount": stamp, "identifiers": stamp}
        rows[suffix] = opp
    db.commit()
    return {"rows": rows, "issuer": issuer, "source": src, "licence": lic}


def _suffixes(world: dict[str, Any], body: dict[str, Any]) -> list[str]:
    by_id = {o.public_id: s for s, o in world["rows"].items()}
    return [by_id[o["public_id"]] for o in body["data"]]


def _get(client: TestClient, path: str = "/v1/opportunities", **params: str) -> dict[str, Any]:
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _problem(resp: Any, field: str, code: str = "validation_error") -> None:
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == code and resp.json()["errors"][0]["field"] == field


def _assert_valid(schema_name: str, instance: object) -> None:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def test_sort_by_value_ranks_across_currencies_largest_first_nulls_last(
    client: TestClient, world: dict[str, Any]
) -> None:
    body = _get(client, sort="-value")
    order = _suffixes(world, body)
    assert order[:5] == DESC and set(order[5:]) == NULLS
    usd = [o["value"]["usd_equivalent"] if o["value"] else None for o in body["data"]]
    assert usd[:5] == [1_062_111_215.83, 410_000_000.0, 5_000_000.0, 1_126_900.0, 52_158.35]
    assert usd[5:] == [None, None, None]
    ascending = _suffixes(world, _get(client, sort="value"))
    assert ascending[:5] == ASC and set(ascending[5:]) == NULLS  # nulls last both ways
    # The placeholder keeps its stated amount and says why it is not ranked.
    int32 = _get(client, f"/v1/opportunities/{world['rows']['int32'].public_id}")["data"]
    assert int32["budget_amount"] == 2_147_483_647.0 and int32["value"]["amount"] == 2_147_483_647.0
    assert int32["value"]["usd_equivalent"] is None
    assert int32["value"]["amount_note"] == "the largest number the source's field holds, not a figure"


@pytest.mark.parametrize(("sort", "expected"), [("-value", DESC), ("value", ASC)])
def test_value_pages_keep_the_order_through_the_cursor(
    client: TestClient, world: dict[str, Any], sort: str, expected: list[str]
) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"sort": sort, "limit": "2"}
        if cursor:
            params["cursor"] = cursor
        body = _get(client, **params)
        seen += _suffixes(world, body)
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break
    assert seen[:5] == expected and set(seen[5:]) == NULLS and len(seen) == len(ROWS)


def test_min_and_max_value_bound_the_usd_equivalent(client: TestClient, world: dict[str, Any]) -> None:
    assert _suffixes(world, _get(client, sort="-value", min_value="1000000")) == [
        "gbp",
        "grant",
        "usd",
        "eur",
    ]
    assert _suffixes(world, _get(client, sort="-value", max_value="5000000")) == ["usd", "eur", "ron"]
    both = _get(client, sort="-value", min_value="1000000", max_value="410000000", include="count")
    assert _suffixes(world, both) == ["grant", "usd", "eur"] and both["meta"]["total"] == 3
    # A record with no equivalent matches no bound.
    assert NULLS.isdisjoint(_suffixes(world, _get(client, min_value="0")))


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"min_value": "abc"}, "min_value"),
        ({"max_value": "-1"}, "max_value"),
        ({"min_value": "10", "max_value": "5"}, "max_value"),
    ],
)
def test_bad_value_bounds_are_a_400_naming_the_bound(
    client: TestClient, world: dict[str, Any], params: dict[str, str], field: str
) -> None:
    _problem(client.get("/v1/opportunities", params=params), field)


def test_the_value_block_says_what_the_amount_is_and_credits_both_sources(
    client: TestClient, world: dict[str, Any]
) -> None:
    body = _get(client, sort="-value")
    rows = dict(zip(_suffixes(world, body), body["data"], strict=True))
    eur = rows["eur"]
    assert (eur["budget_amount"], eur["budget_currency"]) == (1_000_000.0, "EUR")  # unchanged columns
    value = eur["value"]
    assert {k: value[k] for k in ("amount", "currency", "kind", "kind_label")} == {
        "amount": 1_000_000.0,
        "currency": "EUR",
        "kind": "estimated_value",
        "kind_label": "Estimated value",
    }
    assert (value["usd_equivalent"], value["usd_is_approximate"]) == (1_126_900.0, True)
    assert (value["fx_rate"], value["fx_date"]) == (1.1269, "2026-10-06")
    credit = value["fx_source"]
    assert credit["source_id"] == "eu.ecb.eurofxref" and credit["rate_date"] == "2026-10-06"
    assert credit["retrieved_at"] == "2026-10-22T01:01:38Z"
    assert "European Central Bank" in credit["attribution_text"]
    assert "calculated by Inframation" in credit["attribution_text"]  # the ECB's terms: say it is ours
    prov = value["provenance"]
    assert prov["source_id"] == world["source"].id and prov["licence_id"] == world["licence"].id
    assert prov["source_url"].startswith(world["source"].url)
    assert prov["source_field"] == "total-value" and prov["attribution_text"] == "Source: Test ISO"
    assert prov["retrieved_at"]
    # USD is not converted and so not approximate; no ECB credit on it.
    usd = rows["usd"]["value"]
    assert (usd["usd_is_approximate"], usd["fx_source"], usd["fx_date"]) == (False, None, None)
    # A Grants.gov figure carries its own read time, endpoint, other figures and awards.
    grant = rows["grant"]["value"]
    assert grant["kind_label"] == "Estimated total programme funding"
    assert grant["provenance"]["retrieved_at"] == READ_AT
    assert grant["provenance"]["endpoint"] == "https://api.grants.gov/v1/api/fetchOpportunity"
    assert grant["expected_awards"] == 100
    assert grant["also_stated"] == [
        {
            "kind": "award_ceiling",
            "kind_label": "Award ceiling",
            "amount": 1e8,
            "currency": "USD",
            "source_field": "synopsis.awardCeiling",
            "amount_note": None,
        }
    ]
    # The unquoted currency says why it has no equivalent; no amount, no block.
    assert rows["mkd"]["value"]["usd_equivalent"] is None
    assert rows["mkd"]["value"]["fx_note"] == "no ECB reference rate for MKD"
    # Nothing was converted, so nothing is approximate (review of lane rfp-value: it said true).
    assert rows["mkd"]["value"]["usd_is_approximate"] is False
    assert rows["int32"]["value"]["usd_is_approximate"] is False
    # The published rate reproduces the equivalent: GBP 800,000,000 x (1.1269 / 0.8488).
    gbp = rows["gbp"]["value"]
    assert round(gbp["amount"] * gbp["fx_rate"], 2) == gbp["usd_equivalent"] == 1_062_111_215.83
    assert rows["none"]["value"] is None


def test_the_page_credit_names_the_ecb_only_when_it_converted_something(
    client: TestClient, world: dict[str, Any]
) -> None:
    body = _get(client, sort="-value")
    sources = {s["source_id"]: s for s in body["licence_summary"]["sources"]}
    ecb = sources["eu.ecb.eurofxref"]
    assert ecb["record_count"] == 3  # EUR, RON, GBP; USD and MKD were not converted
    assert ecb["reuse_class"] == "attribution" and ecb["requires_link_back"] is True
    assert "ECB euro foreign exchange reference rates" in body["licence_summary"]["attribution_line"]
    assert "European Central Bank" in ecb["attribution_text"]
    usd_only = _get(client, sort="-value", max_value="5000000", min_value="5000000")
    assert {s["source_id"] for s in usd_only["licence_summary"]["sources"]} == {world["source"].id}


def test_list_and_detail_validate_against_the_contract(client: TestClient, world: dict[str, Any]) -> None:
    body = _get(client, sort="-value")
    _assert_valid("OpportunityListResponse", body)
    for suffix in ("eur", "grant", "mkd", "none"):
        detail = _get(client, f"/v1/opportunities/{world['rows'][suffix].public_id}")
        _assert_valid("OpportunityDetailResponse", detail)
    eur = _get(client, f"/v1/opportunities/{world['rows']['eur'].public_id}")
    assert eur["data"]["value"]["usd_equivalent"] == 1_126_900.0
    assert "eu.ecb.eurofxref" in {s["source_id"] for s in eur["licence_summary"]["sources"]}


def test_an_operator_override_does_not_print_a_stale_equivalent(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    opp = db.get(Opportunity, world["rows"]["eur"].id)
    assert opp is not None
    opp.budget_amount = 2_000_000
    opp.overrides = {"budget_amount": {"by": "operator"}}
    db.commit()
    value = _get(client, f"/v1/opportunities/{opp.public_id}")["data"]["value"]
    assert value["amount"] == 2_000_000.0 and value["usd_equivalent"] is None and value["fx_source"] is None


def test_the_issuer_list_sorts_by_value_too(client: TestClient, world: dict[str, Any]) -> None:
    body = _get(
        client, f"/v1/organizations/{world['issuer'].public_id}/opportunities", sort="-value", limit="3"
    )
    assert _suffixes(world, body) == DESC[:3]
    page_two = _get(
        client,
        f"/v1/organizations/{world['issuer'].public_id}/opportunities",
        sort="-value",
        limit="3",
        cursor=body["page"]["next_cursor"],
    )
    assert _suffixes(world, page_two)[:2] == DESC[3:]


def _login(client: TestClient, db: Session) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)


def test_the_csv_twin_and_an_export_take_the_same_sort_and_bounds(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _login(client, db)
    resp = client.get(
        "/v1/opportunities", params={"sort": "-value", "min_value": "1000000"}, headers={"Accept": "text/csv"}
    )
    assert resp.status_code == 200, resp.text
    body = "\n".join(line for line in resp.text.splitlines() if not line.startswith("#"))
    rows = list(csv.DictReader(io.StringIO(body)))
    assert [(r["budget_amount"], r["budget_currency"]) for r in rows] == [
        ("800000000.0", "GBP"),
        ("410000000.0", "USD"),
        ("5000000.0", "USD"),
        ("1000000.0", "EUR"),
    ]
    # The file shows what it is ordered by, and what each figure is (review of lane rfp-value).
    assert [(r["value_kind"], r["usd_equivalent"], r["fx_date"]) for r in rows] == [
        ("estimated_value", "1062111215.83", "2026-10-06"),
        ("total_programme_funding", "410000000.0", ""),
        ("estimated_value", "5000000.0", ""),
        ("estimated_value", "1126900.0", "2026-10-06"),
    ]
    header = [line for line in resp.text.splitlines() if line.startswith("#")]
    assert any("European Central Bank" in line for line in header)  # the converted rows credit the ECB
    export = client.post(
        "/v1/exports", json={"entity": "opportunity", "query": {"sort": "-value", "max_value": 5_000_000}}
    )
    assert export.status_code == 202, export.text
    assert export.json()["data"]["status"] == "ready" and export.json()["data"]["row_count"] == 3


def test_a_saved_search_refuses_the_value_bounds_it_cannot_alert_on(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _login(client, db)
    resp = client.post(
        "/v1/saved-searches", json={"name": "big", "entity": "opportunity", "query": {"min_value": 1_000_000}}
    )
    _problem(resp, "min_value", code="unknown_parameter")


def test_the_served_identifiers_leave_the_stored_block_to_value(
    client: TestClient, world: dict[str, Any]
) -> None:
    """Review of lane rfp-value (2026-10-22): `identifiers` repeated the stored block, internal keys
    (`fx_retrieved_at`) included. `value` is its served form."""
    body = _get(client, sort="-value")
    assert all("value" not in row["identifiers"] for row in body["data"])
    assert "fx_retrieved_at" not in json.dumps(body)
    detail = _get(client, f"/v1/opportunities/{world['rows']['grant'].public_id}")["data"]
    assert "value" not in detail["identifiers"] and detail["value"]["kind"] == "total_programme_funding"


def _add(db: Session, world: dict[str, Any], table: fx.RateTable, suffix: str, **fields: Any) -> Opportunity:
    opp = make_visible_opportunity(db, world["source"], public_id_suffix=suffix)
    built = with_value(fields, table)
    opp.budget_amount, opp.budget_currency = fields["budget_amount"], fields.get("budget_currency")
    opp.open_at, opp.identifiers = fields.get("open_at"), built["identifiers"]
    db.commit()
    return opp


def test_a_placeholder_beside_the_figure_that_leads_carries_its_note(
    client: TestClient, db: Session, world: dict[str, Any], table: fx.RateTable
) -> None:
    """Grants.gov 363912: ranked on its 250,000,000 award ceiling, labelled as one; the 2,147,483,647
    total it also states is listed with the reason it is not a figure."""
    block = {
        "kind": "award_ceiling",
        "source_field": "synopsis.awardCeiling",
        "also_stated": [
            {
                "kind": "total_programme_funding",
                "amount": 2_147_483_647.0,
                "currency": "USD",
                "source_field": "synopsis.estimatedFunding",
            }
        ],
    }
    opp = _add(
        db,
        world,
        table,
        "ceiling",
        budget_amount=250_000_000,
        budget_currency="USD",
        identifiers={"value": block},
    )
    value = _get(client, f"/v1/opportunities/{opp.public_id}")["data"]["value"]
    assert (value["kind_label"], value["usd_equivalent"], value["amount_note"]) == (
        "Award ceiling",
        250_000_000.0,
        None,
    )
    assert (
        value["also_stated"][0]["amount_note"] == "the largest number the source's field holds, not a figure"
    )
    _assert_valid("OpportunityDetailResponse", _get(client, f"/v1/opportunities/{opp.public_id}"))


def test_an_amount_beyond_the_cent_keeps_its_equivalent_and_zero_is_no_value(
    client: TestClient, db: Session, world: dict[str, Any], table: fx.RateTable
) -> None:
    """`budget_amount` rounds to the cent; the stored block does too, so the API does not drop the
    equivalent as stale while the sort still ranks it (review of lane rfp-value)."""
    cents = _add(
        db,
        world,
        table,
        "cents",
        budget_amount=1_234.567,
        budget_currency="EUR",
        open_at=dt.date(2026, 10, 6),
        identifiers={},
    )
    value = _get(client, f"/v1/opportunities/{cents.public_id}")["data"]["value"]
    assert (value["amount"], value["usd_equivalent"]) == (1_234.57, round(1_234.57 * 1.1269, 2))
    zero = _add(db, world, table, "zero", budget_amount=0, budget_currency="EUR", identifiers={})
    detail = _get(client, f"/v1/opportunities/{zero.public_id}")
    assert detail["data"]["value"] is None
    _assert_valid("OpportunityDetailResponse", detail)


def test_an_operator_edit_restates_the_value_the_list_sorts_on(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    """Review of lane rfp-value: an admin edit of the amount left the stored equivalent of the old
    one, so the list ranked a figure the record no longer showed."""
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email="operator@example.com", role="operator")
    db.commit()
    login(client, db, user)
    eur = world["rows"]["eur"]
    resp = client.patch(
        f"/admin/v1/opportunities/{eur.public_id}",
        json={"budget_amount": 500_000_000, "reason": "the issuer corrected the estimate"},
    )
    assert resp.status_code == 200, resp.text
    value = _get(client, f"/v1/opportunities/{eur.public_id}")["data"]["value"]
    assert (value["amount"], value["usd_equivalent"], value["fx_date"]) == (5e8, 563_450_000.0, "2026-10-06")
    assert _suffixes(world, _get(client, sort="-value"))[:3] == ["gbp", "eur", "grant"]
