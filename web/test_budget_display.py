"""The opportunity page shows a placeholder budget (0 or less) as a dash, like no budget (lane I3,
2026-09-29). The loader already stores TED's 0 / -1 placeholders as null; this covers a value that
reaches the page another way (an admin edit, or a store loaded before the fix).

Since lane rfp-value (2026-10-22) the page also says what the figure is, from the API's `value`
block (`kind_label`, `amount_note`), instead of printing every amount as "Budget"."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from markupsafe import escape

from web.viewmodels import flatten_opportunity


def _entity(amount: Any, currency: str | None = "EUR") -> dict[str, Any]:
    return {
        "public_id": "opp_1",
        "slug": "opp-1",
        "title": "Test tender",
        "budget_amount": amount,
        "budget_currency": currency,
        "provenance": [],
    }


@pytest.mark.parametrize("amount", [0, 0.0, -1, -1.0, None])
def test_a_placeholder_or_missing_budget_is_not_an_amount(amount: Any) -> None:
    record = flatten_opportunity(_entity(amount))
    assert record["budget_amount"] is None
    assert record["budget_currency"] == "EUR"  # the stated currency is not the page's to drop


def test_a_positive_budget_is_shown_as_stated() -> None:
    assert flatten_opportunity(_entity(1_250_000.0))["budget_amount"] == 1_250_000.0


# ----------------------------------------------- what the figure is (lane rfp-value, 2026-10-22)
# Review of the value lane: the page printed every amount as "Budget", so Grants.gov notices that
# showed a dash before would read "Budget 2,147,483,647 USD" (a field maximum), "Budget 999,999,999
# USD" (a run of nines), "Budget 1 USD", and an award ceiling (the most one award can be) as the
# programme's budget. The page now reads the API's `value.kind_label` and `value.amount_note`.
PLACEHOLDER_NOTE = "the largest number the source's field holds, not a figure"


def _valued(amount: float, currency: str, kind_label: str, note: str | None = None) -> dict[str, Any]:
    value = {"amount": amount, "currency": currency, "kind_label": kind_label, "amount_note": note}
    return {**_entity(amount, currency), "value": value}


def test_a_placeholder_is_a_dash_with_the_stated_number_and_the_reason() -> None:
    record = flatten_opportunity(
        _valued(2_147_483_647.0, "USD", "Estimated total programme funding", PLACEHOLDER_NOTE)
    )
    assert record["budget_amount"] is None  # never printed as the notice's value
    assert (record["value_label"], record["value_stated"], record["value_note"]) == (
        "Estimated total programme funding",
        "2,147,483,647 USD",
        PLACEHOLDER_NOTE,
    )
    unit = flatten_opportunity(_valued(0.26, "EUR", "Total awarded value", "under 100, a unit price"))
    assert unit["value_stated"] == "0.26 EUR"


def test_an_award_ceiling_is_labelled_as_one_and_no_value_is_a_dash() -> None:
    ceiling = flatten_opportunity(_valued(20_000_000.0, "USD", "Award ceiling"))
    assert (ceiling["value_label"], ceiling["budget_amount"], ceiling["value_note"]) == (
        "Award ceiling",
        20_000_000.0,
        None,
    )
    nothing = flatten_opportunity({**_entity(None, None), "value": None})
    assert (nothing["value_label"], nothing["budget_amount"]) == ("Value", None)
    # An API without `value` keeps the old label.
    assert flatten_opportunity(_entity(1_250_000.0))["value_label"] == "Budget"


class _Transport:
    """The API, answered from a dict (duplicated per web test file, as web/test_record_company.py)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            return httpx.Response(200, json=self.responses[url])
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(self, url: str, **_: Any) -> httpx.Response:
        return self._respond(url)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return self._respond(url)

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return self._respond(url)

    def close(self) -> None:
        pass


def _page(entity: dict[str, Any]) -> str:
    from fastapi.testclient import TestClient

    from web.api_client import ApiClient
    from web.app import app as web_app

    row = {
        "public_id": "opp_1",
        "slug": "opp-1",
        "title": "Grid modernisation grants",
        "kind": "foa",
        "status": "open",
        "identifiers": {},
        "provenance": [],
        **entity,
    }
    responses = {
        "/v1/health": {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}},
        "/v1/meta/vocabularies": {"data": {}},
        "/v1/sources": {"data": []},
        "/v1/opportunities": {
            "data": [row],
            "meta": {"total": 1, "total_is_estimate": False},
            "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        },
    }
    try:
        with TestClient(web_app) as client:
            web_app.state.api_client = ApiClient(_Transport(responses))
            web_app.state.lag_days_default = None
            resp = client.get("/opportunities/opp-1")
            assert resp.status_code == 200, resp.text[:500]
            return resp.text
    finally:
        for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
            web_app.state.__dict__.pop(key, None)


def test_the_page_prints_what_the_figure_is_never_a_placeholder_as_a_budget() -> None:
    placeholder = _page(
        _valued(2_147_483_647.0, "USD", "Estimated total programme funding", PLACEHOLDER_NOTE)
    )
    assert "<dt>Estimated total programme funding</dt>" in placeholder
    assert f"<small>stated as 2,147,483,647 USD: {escape(PLACEHOLDER_NOTE)}</small>" in placeholder
    assert "<dt>Budget</dt>" not in placeholder
    ceiling = _page(_valued(20_000_000.0, "USD", "Award ceiling"))
    # Lane pages (2026-10-23): the figure in its currency's own form (docs/31 §4), on the value row.
    assert '<dt>Award ceiling</dt><dd><span class="value-figure tnum">$20,000,000</span>' in ceiling
