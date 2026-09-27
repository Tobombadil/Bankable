"""The public list pages pass every API list filter through, so a link narrows the page exactly as
the same query narrows the API (2026-09-27: `/proposals?state=US-TX` was dropped here and showed
2,077 rows where the API returns 806).

Two halves: the passthrough tuples in `web/app.py` are pinned to the API's own filter sets
(`services/api/records.py`), so a filter added to the API cannot be silently dropped by the site;
and the pages are driven against a fake transport to show what reaches the API -- the filter, on
both the list call and the breakdown count, and not an unknown parameter. The fake transport is
duplicated from `web/test_slippage_view.py` because no `web/test_*.py` imports another (this repo's
convention).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.api.records import OPPORTUNITY_FILTERS, PROPOSAL_FILTERS, SYNC_FILTERS
from web.api_client import ApiClient
from web.app import (
    BREAKDOWN_FILTERS,
    OPPORTUNITY_PASSTHROUGH_FILTERS,
    PROPOSAL_PASSTHROUGH_FILTERS,
)
from web.app import app as web_app


def test_proposal_passthrough_is_every_api_list_filter() -> None:
    # `lifecycle_state` is resolved by the page itself (active by default, include_withdrawn).
    expected = (set(PROPOSAL_FILTERS) - {"lifecycle_state"}) | set(SYNC_FILTERS) | {"q"}
    assert set(PROPOSAL_PASSTHROUGH_FILTERS) == expected
    assert len(PROPOSAL_PASSTHROUGH_FILTERS) == len(set(PROPOSAL_PASSTHROUGH_FILTERS))


def test_opportunity_passthrough_is_every_api_list_filter() -> None:
    # `status` is resolved by the page itself (`opportunity_status_param`).
    expected = (set(OPPORTUNITY_FILTERS) - {"status"}) | set(SYNC_FILTERS) | {"q"}
    assert set(OPPORTUNITY_PASSTHROUGH_FILTERS) == expected
    assert len(OPPORTUNITY_PASSTHROUGH_FILTERS) == len(set(OPPORTUNITY_PASSTHROUGH_FILTERS))


def test_breakdown_counts_the_same_filters_the_list_applies() -> None:
    assert set(BREAKDOWN_FILTERS) == set(PROPOSAL_PASSTHROUGH_FILTERS)


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, dict(json or {})))
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, dict(json if json is not None else params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [params for _method, called, params in self.calls if called == url]


HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "storage"}],
        "proposal_kind": [{"value": "storage"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}
EMPTY_LIST: dict[str, Any] = {
    "data": [],
    "meta": {"total": 0, "total_is_estimate": False},
    "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
    "licence_summary": [],
}


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/proposals": (200, EMPTY_LIST),
            "/v1/opportunities": (200, EMPTY_LIST),
            "/v1/proposals/geo": (200, {"data": {"totals": {"lifecycle_state_counts": {}}}}),
        }
    )
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def test_a_linked_state_filter_reaches_the_list_and_the_breakdown(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        response = client.get("/proposals?technology=storage&state=US-TX&utm_source=newsletter")
    assert response.status_code == 200
    (list_params,) = transport.params_for("/v1/proposals")
    assert list_params["state"] == "US-TX"
    assert list_params["technology"] == "storage"
    assert "utm_source" not in list_params  # an unknown parameter is still not forwarded
    breakdown = transport.params_for("/v1/proposals/geo")
    assert breakdown and all(params.get("state") == "US-TX" for params in breakdown)


def test_a_linked_opportunity_filter_reaches_the_list(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        response = client.get("/opportunities?budget_currency=EUR&budget_amount[gte]=1000000&issuer_id=org_x")
    assert response.status_code == 200
    (list_params,) = transport.params_for("/v1/opportunities")
    assert list_params["budget_currency"] == "EUR"
    assert list_params["budget_amount[gte]"] == "1000000"
    assert list_params["issuer_id"] == "org_x"
