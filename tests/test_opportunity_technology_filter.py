"""The opportunity technology filter offers, and matches, the tokens opportunities actually carry
(audit 2026-09-30, frontend F2).

Before: the `/opportunities` select was built from the *proposal* vocabulary (`solar`, `gas_cc`,
`wind_offshore`, ...), which no opportunity carries, so every choice returned only the untagged
all-source rows; and the loader split stored lists on `,`/`;` but not on the `|` the connectors
write, so 72 rows held one joined element (`solar_pv|nuclear`) that no value matched. These tests
load rows through the loader, read them through the real API, and render the real page over it.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.connectors.opportunity import OPPORTUNITY_TECHNOLOGIES, TECH_KEYWORDS, classify_technologies
from pipeline.connectors.registry import SourceEntry
from services.db.models import Opportunity
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from web.api_client import ApiClient
from web.app import app as web_app

SOURCE = "us.test.tenders"


def _row(suffix: str, technologies: str) -> dict[str, Any]:
    return {
        "record_id": f"{SOURCE}:N{suffix}",
        "source_id": SOURCE,
        "source_record_id": f"N{suffix}",
        "source_url": f"https://example.org/tenders#N{suffix}",
        "retrieved_at": "2026-09-29T05:00:00Z",
        "licence_id": f"{SOURCE}#irrelevant",
        "kind": "tender",
        "issuer": None,
        "title": f"Tender {suffix}",
        "summary": None,
        "jurisdiction": "US-AZ",
        "technologies": technologies,
        "capacity_sought_mw": None,
        "budget_amount": None,
        "budget_currency": None,
        "open_at": None,
        "due_at": "2099-06-01T00:00:00Z",
        "status": "open",
        "status_raw": "open",
        "status_rule": None,
        "identifiers": json.dumps({}),
        "raw": json.dumps({"n": suffix}),
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
    # As the connectors write them (`technologies_str`): pipe-joined.
    rows = [_row("1", "solar_pv|nuclear"), _row("2", "gas"), _row("3", "")]
    load_dataframe(db, source, "opportunity", pd.DataFrame(rows), None)
    db.commit()
    return {o.title: o for o in db.query(Opportunity).all()}


def test_the_loader_splits_the_connectors_pipe_joined_lists(world: dict[str, Opportunity]) -> None:
    assert world["Tender 1"].technologies == ["solar_pv", "nuclear"]
    assert world["Tender 2"].technologies == ["gas"]
    assert world["Tender 3"].technologies == []


def _titles(client: TestClient, technologies: str) -> list[str]:
    resp = client.get("/v1/opportunities", params={"technologies": technologies})
    assert resp.status_code == 200, resp.text
    return sorted(o["title"] for o in resp.json()["data"])


def test_a_stored_token_selects_its_tagged_rows(client: TestClient, world: dict[str, Opportunity]) -> None:
    # Tender 3 is untagged (all-source) and matches every value, as documented.
    assert _titles(client, "nuclear") == ["Tender 1", "Tender 3"]
    assert _titles(client, "solar_pv") == ["Tender 1", "Tender 3"]
    assert _titles(client, "gas") == ["Tender 2", "Tender 3"]


def test_the_vocabulary_is_the_opportunity_vocabulary(client: TestClient) -> None:
    data = client.get("/v1/meta/vocabularies").json()["data"]
    values = [v["value"] for v in data["opportunity_technology"]]
    assert values == list(OPPORTUNITY_TECHNOLOGIES)
    # Every token the classifier can emit is offered, and nothing else.
    assert set(values) == {token for _, token in TECH_KEYWORDS}
    assert classify_technologies("Offshore wind and battery storage tender") == [
        "wind_offshore",
        "wind",
        "bess",
    ]
    assert {"solar_pv", "bess", "gas", "heat", "ev_charging", "efficiency"} <= set(values)
    # The two vocabularies differ: the proposal filter's tokens are not what opportunities carry.
    assert "solar" not in values and "gas_cc" not in values


class _ApiTransport:
    """The web app's `Transport`, answered by the real API test client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return self.client.get(url, params=clean)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self.client.post(url, json=json)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.client.request(method, url, json=json, params=params)

    def close(self) -> None:
        return None


@pytest.fixture()
def site(client: TestClient) -> Iterator[TestClient]:
    with TestClient(web_app) as web:
        web_app.state.api_client = ApiClient(_ApiTransport(client))
        web_app.state.lag_days_default = None
        yield web
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _options(body: str) -> list[str]:
    select = re.search(r'<select id="f-technology" name="technologies">(.*?)</select>', body, re.S)
    assert select is not None
    return [html.unescape(v) for v in re.findall(r'<option value="([^"]*)"', select.group(1)) if v]


def test_the_page_offers_the_stored_tokens_and_filters_by_them(
    site: TestClient, world: dict[str, Opportunity]
) -> None:
    page = site.get("/opportunities")
    assert page.status_code == 200, page.text[:500]
    assert _options(page.text) == list(OPPORTUNITY_TECHNOLOGIES)
    filtered = site.get("/opportunities", params={"technologies": "nuclear"})
    assert filtered.status_code == 200
    assert "Tender 1" in filtered.text and "Tender 2" not in filtered.text
    assert re.search(r'<option value="nuclear"\s+selected', filtered.text)
