"""Admin Sites screen (`web/admin/sites.py`): recent site audit rows and flagged sites, read-only.

Same fake-`Transport` pattern as `web/test_sites.py`, duplicated rather than imported: `web.app.app`
against canned API envelopes, no database (`web` must not import `services.db`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.admin.sites import involved
from web.api_client import ApiClient
from web.app import app as web_app

ME = {"data": {"user": {"email": "operator@example.com", "role": "operator"}, "account": {}}}
SITE = {
    "public_id": "site_1M4KDKH9SEAESTS0B0TG2C5TV",
    "name": "St Gall IIIA Storage",
    "member_count": 2,
    "review_flag": None,
    "retired": False,
    "built_at": "2026-10-10T12:00:00+00:00",
    "rule_version": "2026-10-10.2",
}
REVIEW = {
    "data": {
        "audit": [
            {
                "kind": "lead_changed",
                "site": SITE,
                "detail": {"from": "prop_OLD", "to": "prop_NEW"},
                "rule_version": "2026-10-10.2",
                "recorded_at": "2026-10-10T12:00:00+00:00",
            },
            {
                "kind": "created",
                "site": SITE,
                "detail": {"members": [f"prop_{i}" for i in range(8)], "lead": "prop_0"},
                "rule_version": "2026-10-10.2",
                "recorded_at": "2026-10-09T12:00:00+00:00",
            },
        ],
        "flagged": [{**SITE, "public_id": "site_FLAGGED0000", "review_flag": "oversize", "member_count": 75}],
    }
}


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.params: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.params.append((url, dict(params or {})))
        status, body = self.responses.get(url, (404, {"title": "Not found"}))
        return httpx.Response(status, json=body)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return self.get(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app, follow_redirects=False) as c:
        yield c
    web_app.state.__dict__.pop("api_client", None)


def _install(responses: Mapping[str, tuple[int, Any]]) -> FakeTransport:
    transport = FakeTransport(responses)
    web_app.state.api_client = ApiClient(transport)
    return transport


def test_involved_names_what_each_kind_records() -> None:
    assert involved("lead_changed", {"from": "prop_A", "to": "prop_B"}) == "prop_A to prop_B"
    assert involved("split", {"into": {"site_2": ["prop_C"], "site_1": ["prop_A", "prop_B"]}}) == (
        "site_1: prop_A, prop_B; site_2: prop_C"
    )
    assert involved("merged", {"from": ["site_1", "site_2"]}) == "from site_1, site_2"
    assert involved("retired", {"members": ["prop_A"], "successor": None}) == "prop_A; successor none"
    assert involved("created", {"members": [f"p{i}" for i in range(8)], "lead": "p0"}) == (
        "p0, p1, p2, p3, p4, p5 and 2 more (lead p0)"
    )


def test_anonymous_is_redirected_to_login(client: TestClient) -> None:
    _install({"/v1/me": (401, {"title": "Unauthenticated"})})
    resp = client.get("/admin/sites")
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/admin/sites"


def test_the_page_lists_recent_changes_and_flagged_sites_with_links(client: TestClient) -> None:
    transport = _install({"/v1/me": (200, ME), "/admin/v1/sites/review": (200, REVIEW)})
    client.cookies.set("session", "s")
    resp = client.get("/admin/sites?limit=25")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert ("/admin/v1/sites/review", {"limit": "25"}) in transport.params
    assert '<a href="/sites/site_FLAGGED0000">site_FLAGGED0000</a>' in body and "oversize" in body
    assert "Lead changed" in body and "prop_OLD to prop_NEW" in body
    assert "prop_0, prop_1, prop_2, prop_3, prop_4, prop_5 and 2 more (lead prop_0)" in body
    assert f'href="/sites/{SITE["public_id"]}"' in body
    assert 'href="/admin/sites" aria-current="page"' in body
    assert "<form" not in body.split("Recent changes", 1)[0].split("<main", 1)[1]  # no write form


def test_an_api_problem_renders_the_notice(client: TestClient) -> None:
    _install({"/v1/me": (200, ME), "/admin/v1/sites/review": (500, {"title": "boom", "detail": "down"})})
    client.cookies.set("session", "s")
    resp = client.get("/admin/sites")
    assert resp.status_code == 500 and "boom" in resp.text and "No site is flagged." in resp.text
