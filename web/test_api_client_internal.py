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
