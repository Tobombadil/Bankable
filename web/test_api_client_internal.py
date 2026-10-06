"""The in-process API client presents the site's service identity (web/api_client.py
`build_client`): every page's API calls come from one process, so without it the whole site
shares one anonymous rate-limit bucket and fails after a few page views (2026-09-26)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from services.api.app import app as api_app
from services.api.ratelimit import TIER_LIMITS, default_limiter
from web.api_client import ApiError, build_client

PATH = "/v1/sources"


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_INTERNAL_TOKEN", raising=False)
    default_limiter.reset()


def test_an_anonymous_in_process_caller_is_limited_which_is_why_the_site_needs_the_token() -> None:
    anonymous = TestClient(api_app, base_url="http://api-internal")
    statuses = [anonymous.get(PATH).status_code for _ in range(TIER_LIMITS["public"] + 1)]
    assert statuses[-1] == 429


def test_the_in_process_site_client_is_not_limited_as_an_anonymous_caller() -> None:
    client = build_client(api_base_url="")
    try:
        for _ in range(TIER_LIMITS["public"] + 5):
            client.get(PATH)
    except ApiError as err:  # pragma: no cover - the failure being guarded against
        pytest.fail(f"in-process site client was rate-limited: {err}")


def test_the_in_process_client_reuses_a_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_INTERNAL_TOKEN", "configured-token")
    client = build_client(api_base_url="")
    assert client._transport.headers["X-Internal-Token"] == "configured-token"  # type: ignore[attr-defined]


# ---------------------------------------------------------------- the visitor's address (audit F1)
def test_the_request_hook_sends_the_visitor_address_only_inside_a_request() -> None:
    import httpx

    from web.api_client import _attach_visitor_ip, _visitor_ip

    outside = httpx.Request("GET", "http://api/v1/sources", headers={"X-Visitor-IP": "192.0.2.1"})
    _attach_visitor_ip(outside)
    assert "X-Visitor-IP" not in outside.headers  # never a stale or caller-supplied value
    token = _visitor_ip.set("203.0.113.5")
    try:
        inside = httpx.Request("GET", "http://api/v1/sources")
        _attach_visitor_ip(inside)
        assert inside.headers["X-Visitor-IP"] == "203.0.113.5"
    finally:
        _visitor_ip.reset(token)


def test_both_transports_carry_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    from web.api_client import _attach_visitor_ip

    monkeypatch.setenv("API_INTERNAL_TOKEN", "configured-token")
    remote = build_client(api_base_url="http://api:8000")
    in_process = build_client(api_base_url="")
    for client in (remote, in_process):
        hooks = client._transport.event_hooks["request"]  # type: ignore[attr-defined]
        assert _attach_visitor_ip in hooks
    remote.close()


def test_the_middleware_records_only_a_well_formed_address() -> None:
    import asyncio

    from web.api_client import VisitorIpMiddleware, current_visitor_ip

    seen: list[str | None] = []

    async def inner(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        seen.append(current_visitor_ip())

    middleware = VisitorIpMiddleware(inner)
    for client in (("203.0.113.5", 1), ("testclient", 1), None):
        asyncio.run(middleware({"type": "http", "client": client}, None, None))  # type: ignore[arg-type]
    assert seen == ["203.0.113.5", None, None]
    assert current_visitor_ip() is None  # reset after each request


def test_two_site_visitors_get_two_login_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the site: the auditor's reproduction was 25 sign-ups through `/register`
    from distinct addresses, with every one after the 10th refused, because the API saw only the
    site's own address. Here one visitor spends the 20-per-hour login bucket and a second visitor,
    at another address, is still answered normally."""
    from web.app import app as web_app

    monkeypatch.setenv("API_INTERNAL_TOKEN", "site-token-for-this-test")
    previous = getattr(web_app.state, "api_client", None)
    web_app.state.api_client = build_client(api_base_url="")
    try:
        form = {"email": "nobody@example.invalid", "password": "wrong password 123", "next": "/account"}
        origin = {"origin": "http://testserver"}
        first = TestClient(web_app, client=("203.0.113.5", 1))
        statuses = [first.post("/login", data=form, headers=origin).status_code for _ in range(21)]
        assert statuses[:20] == [401] * 20
        assert statuses[20] == 429
        second = TestClient(web_app, client=("198.51.100.9", 1))
        assert second.post("/login", data=form, headers=origin).status_code == 401
    finally:
        web_app.state.api_client = previous
