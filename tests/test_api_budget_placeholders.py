"""A placeholder budget (0 or -1) is an unknown budget, so it sorts with the nulls, last (lane I3,
2026-09-29).

TED states `estimated-value-glo` as 0 or -1 on notices with no value: 9 of the 279 TED notices with
an amount in the dev snapshot (8 at 0 EUR, 1 at -1 PLN). Stored as numbers, they sorted *first* on
`?budget_currency=EUR&sort=budget_amount` and printed "-1 PLN" on the page. The placeholder is now
dropped where it enters (the connectors, `pipeline.connectors.opportunity.known_budget`) and again
where every connector's rows enter the store (`services.ingest.loader`, which also cleans parquet
snapshots written before the fix). The API's own null ordering (`NULLS LAST` both ways,
`services/api/pagination.py`) and the one-currency rule of lane E16 are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.api.conftest import make_visible_opportunity
from services.db.models import Opportunity
from services.ingest.loader import load_dataframe, upsert_licence_and_source

SOURCE = "us.test.tenders"
#: (suffix, amount as the source states it, currency)
STATED = (("1", 0.0, "EUR"), ("2", -1.0, "EUR"), ("3", 20_000_000.0, "EUR"), ("4", 5_000_000.0, "EUR"))


def _row(suffix: str, amount: float, currency: str) -> dict[str, Any]:
    return {
        "record_id": f"{SOURCE}:N{suffix}",
        "source_id": SOURCE,
        "source_record_id": f"N{suffix}",
        "source_url": f"https://example.org/tenders#N{suffix}",
        "retrieved_at": "2026-09-29T05:00:00Z",
        "licence_id": f"{SOURCE}#irrelevant",
        "kind": "tender",
        "issuer": None,
        "title": f"Test RFP {suffix}",
        "summary": None,
        "jurisdiction": "US-AZ",
        "technologies": "solar_pv",
        "capacity_sought_mw": None,
        "budget_amount": amount,
        "budget_currency": currency,
        "open_at": None,
        "due_at": None,
        "status": "open",
        "status_raw": "open",
        "status_rule": None,
        "identifiers": json.dumps({}),
        "raw": json.dumps({"estimated-value-glo": amount}),
    }


@pytest.fixture()
def world(db: Session) -> dict[str, Opportunity]:
    entry = SourceEntry.from_yaml(
        {
            "id": SOURCE,
            "name": "Test tenders",
            "jurisdiction": "US",
            "category": "procurement",
            "operator": "Test agency",
            "url": "https://example.org/tenders",
            "access": "api",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    source = upsert_licence_and_source(db, entry, "test")
    rows = {s: make_visible_opportunity(db, source, public_id_suffix=s) for s, _, _ in STATED}
    # The connector's rows reach the store through the loader, as every scheduled run does.
    load_dataframe(db, source, "opportunity", pd.DataFrame([_row(*r) for r in STATED]), None)
    db.commit()
    return rows


def _amounts(client: TestClient, sort: str) -> list[float | None]:
    resp = client.get("/v1/opportunities", params={"sort": sort, "budget_currency": "EUR"})
    assert resp.status_code == 200, resp.text
    return [o["budget_amount"] for o in resp.json()["data"]]


def test_placeholder_budgets_are_stored_as_unknown(db: Session, world: dict[str, Opportunity]) -> None:
    for opp in world.values():
        db.refresh(opp)
    assert world["1"].budget_amount is None
    assert world["2"].budget_amount is None
    assert float(world["3"].budget_amount or 0) == 20_000_000.0
    # The stated currency is kept: the currency rule of lane E16 still selects these rows.
    assert {o.budget_currency for o in world.values()} == {"EUR"}


def test_placeholder_budgets_sort_last_in_both_directions(
    client: TestClient, world: dict[str, Opportunity]
) -> None:
    assert _amounts(client, "budget_amount") == [5_000_000.0, 20_000_000.0, None, None]
    assert _amounts(client, "-budget_amount") == [20_000_000.0, 5_000_000.0, None, None]


def test_the_one_currency_rule_is_unchanged(client: TestClient, world: dict[str, Opportunity]) -> None:
    resp = client.get("/v1/opportunities", params={"sort": "budget_amount"})
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "sort"
