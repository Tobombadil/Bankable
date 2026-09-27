"""`sort=budget_amount` never orders budgets across currencies (2026-09-27, lane E16).

Lane E15 made `budget_amount[gte]` require exactly one `budget_currency` and left the sort open: on
the dev store (nine budget currencies) `GET /v1/opportunities?sort=-budget_amount` put HUF 3,449,282,316
(about EUR 8.9 million) first and EUR 294,000,000 nowhere on the first page. The sort now follows the
filter's rule (`services/api/records.py::check_budget_sort`): with exactly one `budget_currency` it
orders within that currency (the facet also narrows the rows to it); otherwise it is a
`400 validation_error` naming `sort`. Every surface that takes an opportunity sort is covered here:
the list, its `Accept: text/csv` twin, `POST /v1/exports`, and `/v1/organizations/{id}/opportunities`
(which gains the `budget_currency` facet so the sort is usable there). A saved search refuses `sort`
outright, as before.
"""

from __future__ import annotations

import csv
import io
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_opportunity
from services.db.models import Export, Opportunity, Organization
from tests.conftest import login, make_account, make_user

#: (suffix, amount, currency): the largest number is a HUF amount, the largest EUR amount is smaller
#: than two foreign-currency numbers -- the dev store's shape.
BUDGETS = (
    ("1", 3_449_282_316, "HUF"),
    ("2", 1_266_866_061.69, "CZK"),
    ("3", 294_000_000, "EUR"),
    ("4", 51_500_000, "EUR"),
    ("5", 102_603_023, "USD"),
    ("6", 20_000_000, "EUR"),
    ("7", None, "EUR"),
    ("8", None, None),
)


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    issuer = make_org(db, "Budget Issuing Agency")
    rows: dict[str, Opportunity] = {}
    for suffix, amount, currency in BUDGETS:
        opp = make_visible_opportunity(db, src, public_id_suffix=suffix)
        opp.budget_amount, opp.budget_currency = amount, currency
        opp.issuer_org_id = issuer.id
        rows[suffix] = opp
    db.commit()
    return {"issuer": issuer, "rows": rows}


def _problem(resp: Any, field: str, code: str = "validation_error") -> None:
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == code
    assert body["errors"][0]["field"] == field


def _budgets(resp: Any) -> list[tuple[float | None, str | None]]:
    assert resp.status_code == 200, resp.text
    return [(o["budget_amount"], o["budget_currency"]) for o in resp.json()["data"]]


def _login(client: TestClient, db: Session) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "budget_amount"},
        {"sort": "-budget_amount"},
        {"sort": "due_at,-budget_amount"},  # any position, not only the first token
        {"sort": "-budget_amount", "budget_currency": "EUR,USD"},
    ],
)
def test_a_budget_sort_without_exactly_one_currency_is_a_400_naming_sort(
    client: TestClient, world: dict[str, Any], params: dict[str, str]
) -> None:
    _problem(client.get("/v1/opportunities", params=params), "sort")
    _problem(
        client.get(f"/v1/organizations/{world['issuer'].public_id}/opportunities", params=params), "sort"
    )


def test_a_budget_sort_with_one_currency_orders_within_it_and_pages(
    client: TestClient, world: dict[str, Any]
) -> None:
    desc = _budgets(
        client.get("/v1/opportunities", params={"sort": "-budget_amount", "budget_currency": "EUR"})
    )
    # Only EUR rows, largest first, a stated-currency row with no amount last.
    assert desc == [(294_000_000.0, "EUR"), (51_500_000.0, "EUR"), (20_000_000.0, "EUR"), (None, "EUR")]
    asc = _budgets(
        client.get("/v1/opportunities", params={"sort": "budget_amount", "budget_currency": "EUR"})
    )
    assert [a for a, _ in asc if a is not None] == [20_000_000.0, 51_500_000.0, 294_000_000.0]
    # Keyset paging across the sort gives the same order one row at a time.
    seen: list[float | None] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"sort": "-budget_amount", "budget_currency": "EUR", "limit": "1"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/opportunities", params=params).json()
        seen += [o["budget_amount"] for o in body["data"]]
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break
    assert seen == [a for a, _ in desc]
    # The same on the issuer's own list, which now takes the facet.
    org_desc = _budgets(
        client.get(
            f"/v1/organizations/{world['issuer'].public_id}/opportunities",
            params={"sort": "-budget_amount", "budget_currency": "EUR"},
        )
    )
    assert org_desc == desc
    assert {
        c
        for _, c in _budgets(
            client.get(
                f"/v1/organizations/{world['issuer'].public_id}/opportunities",
                params={"budget_currency": "CZK"},
            )
        )
    } == {"CZK"}


def test_other_sorts_are_unaffected(client: TestClient, world: dict[str, Any]) -> None:
    for sort in ("due_at", "-last_changed", "open_at"):
        assert len(_budgets(client.get("/v1/opportunities", params={"sort": sort}))) == len(BUDGETS)


def test_accept_text_csv_refuses_a_cross_currency_budget_sort_before_any_export_exists(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _login(client, db)
    resp = client.get("/v1/opportunities", params={"sort": "-budget_amount"}, headers={"Accept": "text/csv"})
    _problem(resp, "sort")
    assert db.query(Export).count() == 0
    ok = client.get(
        "/v1/opportunities",
        params={"sort": "-budget_amount", "budget_currency": "EUR"},
        headers={"Accept": "text/csv"},
    )
    assert ok.status_code == 200, ok.text
    body = "\n".join(line for line in ok.text.splitlines() if not line.startswith("#"))
    rows = list(csv.DictReader(io.StringIO(body)))
    assert [r["budget_currency"] for r in rows] == ["EUR"] * 4
    assert [r["budget_amount"] for r in rows][:3] == ["294000000.0", "51500000.0", "20000000.0"]


def test_an_export_query_with_a_cross_currency_budget_sort_is_refused(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _login(client, db)
    for query in ({"sort": "-budget_amount"}, {"sort": "budget_amount", "budget_currency": ["EUR", "USD"]}):
        _problem(client.post("/v1/exports", json={"entity": "opportunity", "query": query}), "sort")
    assert db.query(Export).count() == 0
    ok = client.post(
        "/v1/exports",
        json={"entity": "opportunity", "query": {"sort": "-budget_amount", "budget_currency": "EUR"}},
    )
    assert ok.status_code == 202, ok.text
    assert ok.json()["data"]["status"] == "ready"
    assert ok.json()["data"]["row_count"] == 4


def test_the_export_ordering_refuses_a_stored_cross_currency_budget_sort(
    db: Session, world: dict[str, Any]
) -> None:
    """Defence in depth: the one ordering path every export goes through refuses it too, whatever
    reached it (a row stored before the check), and the export fails rather than ordering."""
    from services.api.exports import generate_export

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    export = Export(
        public_id="exp_legacy",
        account_id=account.id,
        user_id=user.id,
        entity="opportunity",
        query={"sort": "-budget_amount"},
        tier="pro",
        status="queued",
        row_cap=100,
    )
    db.add(export)
    db.flush()
    generate_export(db, export)
    assert export.status == "failed"
    assert "budget_currency" in (export.error or "")


def test_a_saved_search_still_refuses_sort(client: TestClient, db: Session, world: dict[str, Any]) -> None:
    _login(client, db)
    resp = client.post(
        "/v1/saved-searches",
        json={
            "name": "n",
            "entity": "opportunity",
            "query": {"sort": "-budget_amount", "budget_currency": "EUR"},
        },
    )
    _problem(resp, "sort", code="unknown_parameter")


def test_the_issuer_list_documents_and_applies_budget_currency(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    other = make_org(db, "Another Agency")
    db.commit()
    assert isinstance(other, Organization)
    resp = client.get(f"/v1/organizations/{other.public_id}/opportunities", params={"budget_currency": "EUR"})
    assert _budgets(resp) == []
    _problem(
        client.get(
            f"/v1/organizations/{world['issuer'].public_id}/opportunities", params={"budget_currency": "euro"}
        ),
        "budget_currency",
    )
