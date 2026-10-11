"""A filtered proposal list names what it is about, and a record no register lists says so
(lane P, 2026-10-10; `web/list_subject.py`, `proposals_list.html`, `proposal_detail.html`).

- `/proposals?sponsor_id=...` (where every company-pipeline count leads) is headed "Proposals
  sponsored by <name>", "and its subsidiaries" for the group, with a link back to the company page;
  `/proposals?interconnection_point_id=...` is headed with the point's name and links back to it;
- the filter form carries the subject as hidden fields, so changing a filter keeps it;
- a view an alert cannot watch yet (`sponsor_scope` beyond `self`, `listed`) offers no alert;
- the record page prints one line under its status when the API says `listed: false`.

A fake `Transport` stands in for the API (duplicated, not imported: no `web/test_*.py` imports
another).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from html import unescape
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app

ORG_ID = "org_01CYPRESS"
POINT_ID = "poi_01ABCDEF"


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        return self._respond(url)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, dict(kwargs.get("params") or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


def _row(n: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": f"prop_{n:03d}",
        "slug": f"p-{n}",
        "name_canonical": f"Project {n}",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 10.0,
        "iso": "ERCOT",
        "jurisdiction": "US-TX",
        "lifecycle_state": "filed",
        "status_raw": None,
        "listed": True,
        "delisted_at": None,
        "identifiers": {},
        "source_count": 1,
        "interconnection_point": None,
        "provenance": [
            {
                "source_id": "us.iso.ercot.gen_queue",
                "source_name": "ERCOT GIS Report",
                "source_url": "https://example.org/q",
                "retrieved_at": "2026-10-07T00:00:00Z",
                "reuse_class": "open",
                "attribution_text": "ERCOT",
                "source_record_id": "Q1",
                "gone_at": None,
            }
        ],
    }
    row.update(extra)
    return row


def _envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": {"sources": []},
    }


ORG = {"public_id": ORG_ID, "slug": "cypress-creek", "name_canonical": "CYPRESS CREEK RENEWABLES LLC"}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as test_client:
        yield test_client
    for key in ("api_client", "lag_days_default", "coverage_facts", "coverage_data", "build_info"):
        web_app.state.__dict__.pop(key, None)


def _install(rows: list[dict[str, Any]], **extra: tuple[int, Any]) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (
                200,
                {"data": {"technology": [{"value": "solar"}], "proposal_kind": [], "slip_bucket": []}},
            ),
            "/v1/proposals": (200, _envelope(rows)),
            "/v1/sources": (200, {"data": []}),
            **extra,
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _h1(html: str) -> str:
    match = re.search(r"<h1>(.*?)</h1>", html, re.S)
    assert match is not None
    return " ".join(unescape(match.group(1)).split())


def _form(html: str) -> str:
    return html.split('<form id="filters"', 1)[1].split("</form>", 1)[0]


# ---------------------------------------------------------------------------- the subject
def test_a_companys_group_list_names_it_and_links_back(client: TestClient) -> None:
    _install([_row(1)], **{f"/v1/organizations/{ORG_ID}": (200, {"data": ORG})})
    body = client.get(f"/proposals?sponsor_id={ORG_ID}&sponsor_scope=all&listed=true").text
    assert _h1(body) == "Proposals sponsored by Cypress Creek Renewables LLC and its subsidiaries"
    assert (
        "<title>Proposals sponsored by Cypress Creek Renewables LLC and its subsidiaries — Infraque</title>"
        in body
    )
    assert (
        '<p>Company page: <a href="/organizations/cypress-creek">Cypress Creek Renewables LLC</a></p>' in body
    )
    form = _form(body)
    assert f'<input type="hidden" name="sponsor_id" value="{ORG_ID}">' in form
    assert '<input type="hidden" name="sponsor_scope" value="all">' in form
    # An alert would not carry the group or `listed` yet, so the view offers none.
    assert "/alerts/new" not in body


def test_one_company_by_slug_and_without_a_scope(client: TestClient) -> None:
    transport = _install([_row(1)], **{"/v1/organizations": (200, {"data": [ORG]})})
    body = client.get("/proposals?sponsor_id=cypress-creek").text
    assert _h1(body) == "Proposals sponsored by Cypress Creek Renewables LLC"
    assert ("/v1/organizations", {"slug": "cypress-creek", "limit": 1}) in transport.calls
    assert "/alerts/new" in body  # a plain sponsor filter is an alert like any other


def test_several_companies_are_counted_not_named(client: TestClient) -> None:
    _install([_row(1)])
    body = client.get(f"/proposals?sponsor_id={ORG_ID},org_01OTHER").text
    assert _h1(body) == "Proposals sponsored by any of 2 companies"
    assert "Company page:" not in body


def test_an_unreadable_company_leaves_the_list_as_it_was(client: TestClient) -> None:
    _install([_row(1)])  # the organisation call answers 404
    body = client.get(f"/proposals?sponsor_id={ORG_ID}").text
    assert _h1(body) == "Proposals"
    assert "<title>Proposals — Infraque</title>" in body


def test_a_connection_points_list_names_it_from_its_own_rows(client: TestClient) -> None:
    embed = {"public_id": POINT_ID, "name": "Lamar 345 kV", "url": "https://x/p"}
    transport = _install([_row(1, interconnection_point=embed)])
    body = client.get(f"/proposals?interconnection_point_id={POINT_ID}").text
    assert _h1(body) == "Proposals connecting at Lamar 345 kV"
    assert f'<p>Connection point: <a href="/interconnection-points/{POINT_ID}">Lamar 345 kV</a></p>' in body
    assert f'<input type="hidden" name="interconnection_point_id" value="{POINT_ID}">' in _form(body)
    assert not any(url.startswith("/v1/interconnection-points") for url, _ in transport.calls)


def test_an_empty_point_list_reads_the_points_name(client: TestClient) -> None:
    _install([], **{f"/v1/interconnection-points/{POINT_ID}": (200, {"data": {"name": "Lamar 345 kV"}})})
    body = client.get(f"/proposals?interconnection_point_id={POINT_ID}").text
    assert _h1(body) == "Proposals connecting at Lamar 345 kV"


def test_an_unfiltered_list_is_unchanged(client: TestClient) -> None:
    _install([_row(1)])
    body = client.get("/proposals").text
    assert _h1(body) == "Proposals"
    assert 'type="hidden" name="sponsor_id"' not in body


def test_the_new_filters_reach_the_api(client: TestClient) -> None:
    transport = _install([_row(1)], **{f"/v1/organizations/{ORG_ID}": (200, {"data": ORG})})
    client.get(f"/proposals?sponsor_id={ORG_ID}&sponsor_scope=children&listed=false&iso=ERCOT")
    list_params = next(p for url, p in transport.calls if url == "/v1/proposals")
    assert (list_params["sponsor_scope"], list_params["listed"], list_params["iso"]) == (
        "children",
        "false",
        "ERCOT",
    )


# -------------------------------------------------------------------------- the record page
def test_a_record_no_register_lists_says_so_under_its_status(client: TestClient) -> None:
    _install([_row(1, listed=False, delisted_at="2026-09-15T04:00:00Z")])
    body = client.get("/proposals/p-1").text
    line = re.search(r'<p class="attribution-line" id="not-listed">(.*?)</p>', body, re.S)
    assert line is not None
    assert " ".join(unescape(re.sub(r"<[^>]+>", "", line.group(1))).split()) == (
        "No longer listed in any register since 15 Sep 2026."
    )
    assert (
        body.index('class="detail-header"') < body.index('id="not-listed"') < body.index('class="field-grid"')
    )


def test_a_listed_record_has_no_such_line(client: TestClient) -> None:
    _install([_row(1)])
    assert 'id="not-listed"' not in client.get("/proposals/p-1").text
