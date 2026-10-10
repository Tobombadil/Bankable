"""Search windows and daily caps, request by request (docs/23-api-spec-outline.md §6, §8; docs/10
US-702 AC1-AC2; `services/api/ratelimit.py`). The Lane P1 row of docs/00-PLAN.md (2026-09-30)
left "daily caps and per-search buckets are missing" open; these tests close it on every tier:

- each tier's search window (`q=`) refuses with `429 rate_limited` once spent, and a refused
  search spends nothing from the read window;
- each tier's daily cap refuses with `429 quota_exceeded`, `Retry-After` and `RateLimit-Reset` to
  00:00 UTC on a frozen clock, and opens again after midnight;
- a key's `daily_quota` replaces its tier's cap, in either direction;
- the admin row has neither window;
- a credential that resolves to nothing is metered on the anonymous windows, search and daily too;
- the site's own calls (`X-Internal-Token`) are exempt from both, so one internal address cannot
  spend the public daily cap for every visitor;
- `/v1/bulk/*` counts against the daily cap and not the read window, and a bulk request the bulk
  window refuses costs no daily unit.

Windows are filled by spending units directly on the shared `default_limiter` under the keys the
app uses (`ratelimit.read_key` and friends), the same technique `tests/test_api_pro_ratelimit.py`
uses, rather than by issuing thousands of requests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.common import API_HOST
from services.api.ratelimit import (
    DAILY_CAPS,
    DAY_SECONDS,
    SEARCH_LIMITS,
    TIER_LIMITS,
    bulk_key,
    daily_key,
    default_limiter,
    read_key,
    search_key,
)
from tests.conftest import login, make_account, make_api_key, make_user

#: 2026-10-09T18:30:00Z, 19,800 seconds before midnight UTC and 1,800 before the hour.
EVENING = dt.datetime(2026, 10, 9, 18, 30, tzinfo=dt.UTC).timestamp()
NEXT_DAY = dt.datetime(2026, 10, 10, 0, 0, 1, tzinfo=dt.UTC).timestamp()
TO_MIDNIGHT = "19800"
TO_THE_HOUR = "1800"
TOKEN = "test-internal-token-not-a-secret"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(default_limiter, "clock", lambda: EVENING)
    yield


@dataclass
class Caller:
    tier: str
    subject: str
    headers: dict[str, str] = field(default_factory=dict)


def _caller(client: TestClient, db: Session, tier: str, *, daily_quota: int | None = None) -> Caller:
    """A caller metered on `tier`: anonymous (`public`), a signed-in account with no paid plan
    (`free_account`), a Pro session, an API-plan key, or an operator session (`admin`)."""
    if tier == "public":
        return Caller("public", "testclient")  # TestClient's peer address
    if tier == "api":
        account = make_account(db, entitlement="api", name="Api Co")
        user = make_user(db, account, email="api@example.com")
        key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
        key.daily_quota = daily_quota
        db.commit()
        return Caller("api", key.public_id, {"Authorization": f"Bearer {secret}"})
    entitlement = {"free_account": "public", "pro": "pro", "admin": "admin"}[tier]
    account = make_account(db, entitlement=entitlement, name=f"{tier} account")
    user = make_user(
        db, account, email=f"{tier}@example.com", role="operator" if tier == "admin" else "member"
    )
    db.commit()
    login(client, db, user)
    return Caller(tier, str(user.id))


def _spend(key: str, units: int, *, limit: int, window_seconds: int = 3600) -> None:
    for _ in range(units):
        default_limiter.check(key, limit=limit, window_seconds=window_seconds)


def _left(key: str, *, limit: int, window_seconds: int = 3600) -> int:
    """Units left in a window, read by spending one (so the answer is one less than before)."""
    return default_limiter.check(key, limit=limit, window_seconds=window_seconds).remaining + 1


METERED = ["public", "free_account", "pro", "api"]


# --------------------------------------------------------------------------------- search windows
@pytest.mark.parametrize("tier", METERED)
def test_each_tiers_search_window(client: TestClient, db: Session, tier: str) -> None:
    who = _caller(client, db, tier)
    limit = SEARCH_LIMITS[tier]
    assert limit is not None
    _spend(search_key(tier, who.subject), limit - 1, limit=limit)

    last = client.get("/v1/proposals", params={"q": "solar"}, headers=who.headers)
    assert last.status_code == 200, last.text
    assert last.headers["RateLimit-Policy"] == f'{limit};w=3600;policy="{tier}-search"'
    assert (last.headers["RateLimit-Limit"], last.headers["RateLimit-Remaining"]) == (str(limit), "0")
    assert last.headers["RateLimit-Reset"] == TO_THE_HOUR

    refused = client.get("/v1/organizations", params={"q": "acme"}, headers=who.headers)
    assert refused.status_code == 429
    body = refused.json()
    assert (body["code"], body["status"], body["type"]) == (
        "rate_limited",
        429,
        f"{API_HOST}/errors/rate_limited",
    )
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.headers["Retry-After"] == TO_THE_HOUR
    assert refused.headers["RateLimit-Policy"].endswith(f'policy="{tier}-search"')
    assert refused.headers["RateLimit-Remaining"] == "0"

    # A read without `q` is still served, and the refused search spent nothing from the read window:
    # two reads (the last search and this one), not three.
    plain = client.get("/v1/proposals", headers=who.headers)
    assert plain.status_code == 200
    assert plain.headers["RateLimit-Policy"] == f'{TIER_LIMITS[tier]};w=3600;policy="{tier}-read"'
    assert plain.headers["RateLimit-Remaining"] == str(TIER_LIMITS[tier] - 2)


@pytest.mark.parametrize("tier", METERED)
def test_a_search_spends_the_read_window_too(client: TestClient, db: Session, tier: str) -> None:
    """docs/23 §6 lists search beside read; a search is a read on a read endpoint, so the search
    figure is a sub-limit of the read figure, never extra capacity (60 + 20 an hour on public)."""
    who = _caller(client, db, tier)
    _spend(read_key(tier, who.subject), TIER_LIMITS[tier] - 1, limit=TIER_LIMITS[tier])
    assert client.get("/v1/proposals", params={"q": "x"}, headers=who.headers).status_code == 200
    over = client.get("/v1/proposals", params={"q": "x"}, headers=who.headers)
    assert over.status_code == 429 and over.json()["code"] == "rate_limited"
    assert over.headers["RateLimit-Policy"].endswith(f'policy="{tier}-read"')
    # the refused request spent no search unit
    limit = SEARCH_LIMITS[tier]
    assert limit is not None
    assert _left(search_key(tier, who.subject), limit=limit) == limit - 1


def test_an_empty_q_is_not_a_search(client: TestClient) -> None:
    _spend(search_key("public", "testclient"), 20, limit=20)
    assert client.get("/v1/proposals", params={"q": ""}).status_code == 200
    assert client.get("/v1/proposals", params={"q": "x"}).status_code == 429


# ----------------------------------------------------------------------------------- daily caps
@pytest.mark.parametrize("tier", METERED)
def test_each_tiers_daily_cap(
    client: TestClient, db: Session, tier: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = _caller(client, db, tier)
    cap = DAILY_CAPS[tier]
    assert cap is not None
    _spend(daily_key(who.subject), cap - 1, limit=cap, window_seconds=DAY_SECONDS)

    last = client.get("/v1/sources", headers=who.headers)
    assert last.status_code == 200, last.text
    # the daily window is now the one closest to exhaustion, so the headers name it
    assert last.headers["RateLimit-Policy"] == f'{cap};w=86400;policy="{tier}-daily"'
    assert (last.headers["RateLimit-Limit"], last.headers["RateLimit-Remaining"]) == (str(cap), "0")
    assert last.headers["RateLimit-Reset"] == TO_MIDNIGHT

    refused = client.get("/v1/proposals", headers=who.headers)
    assert refused.status_code == 429
    body = refused.json()
    assert body["code"] == "quota_exceeded"
    assert (body["status"], body["type"]) == (429, f"{API_HOST}/errors/quota_exceeded")
    assert body["title"] == "Daily quota exhausted"
    assert body["request_id"] == refused.headers["X-Request-Id"]
    assert body["instance"] == "/v1/proposals"
    assert refused.headers["Retry-After"] == TO_MIDNIGHT
    assert refused.headers["RateLimit-Reset"] == TO_MIDNIGHT
    assert refused.headers["RateLimit-Policy"] == f'{cap};w=86400;policy="{tier}-daily"'
    assert refused.headers["RateLimit-Remaining"] == "0"
    # a search past the cap is quota_exceeded too, and spends no search or read unit
    assert (
        client.get("/v1/proposals", params={"q": "x"}, headers=who.headers).json()["code"] == "quota_exceeded"
    )
    assert _left(read_key(tier, who.subject), limit=TIER_LIMITS[tier]) == TIER_LIMITS[tier] - 1

    monkeypatch.setattr(default_limiter, "clock", lambda: NEXT_DAY)
    again = client.get("/v1/proposals", headers=who.headers)
    assert again.status_code == 200
    assert again.headers["RateLimit-Policy"].endswith(f'policy="{tier}-read"')


def test_every_metered_response_carries_the_four_headers(client: TestClient, db: Session) -> None:
    for tier in METERED:
        who = _caller(client, db, tier)
        response = client.get("/v1/licences", headers=who.headers)
        assert response.status_code == 200
        for name in ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "RateLimit-Policy"):
            assert name in response.headers, (tier, name)
        assert response.headers["RateLimit-Policy"].endswith(f'policy="{tier}-read"')
        client.cookies.clear()


# ------------------------------------------------------------------------------ per-key override
def test_a_keys_daily_quota_below_the_tier_cap(client: TestClient, db: Session) -> None:
    who = _caller(client, db, "api", daily_quota=3)
    statuses = [client.get("/v1/sources", headers=who.headers) for _ in range(4)]
    assert [r.status_code for r in statuses] == [200, 200, 200, 429]
    assert statuses[2].headers["RateLimit-Policy"] == '3;w=86400;policy="api-daily"'
    assert statuses[3].json()["code"] == "quota_exceeded"
    assert statuses[3].json()["detail"] == "3 requests per day on this key; resets at 00:00 UTC."
    assert statuses[3].headers["Retry-After"] == TO_MIDNIGHT


def test_a_keys_daily_quota_above_the_tier_cap(client: TestClient, db: Session) -> None:
    """A `read:public` key on an unpaid account is a `free_account` caller (3,000 a day); an
    operator-issued `daily_quota` of 5,000 raises that key's cap and only that key's."""
    account = make_account(db, entitlement="public", name="Raised Co")
    user = make_user(db, account, email="raised@example.com")
    key, secret = make_api_key(db, account, user, scopes=["read:public"])
    key.daily_quota = 5_000
    db.commit()
    headers = {"Authorization": f"Bearer {secret}"}
    _spend(daily_key(key.public_id), 4_999, limit=5_000, window_seconds=DAY_SECONDS)
    last = client.get("/v1/sources", headers=headers)
    assert last.status_code == 200
    assert last.headers["RateLimit-Policy"] == '5000;w=86400;policy="free_account-daily"'
    assert client.get("/v1/sources", headers=headers).json()["code"] == "quota_exceeded"


def test_v1_me_still_reports_the_read_window_when_the_daily_one_is_closer(
    client: TestClient, db: Session
) -> None:
    """`pro.py`'s `/v1/me` derives `limits.read_per_hour` from the read window's cached headers;
    the response's own headers follow the closest window (here the key's 3-a-day quota)."""
    who = _caller(client, db, "api", daily_quota=3)
    me = client.get("/v1/me", headers=who.headers)
    assert me.status_code == 200
    limits = me.json()["data"]["limits"]
    assert limits["read_per_hour"] == TIER_LIMITS["api"]
    # The search window and the daily cap the limiter applies, the key's own quota included.
    assert limits["search_per_hour"] == SEARCH_LIMITS["api"]
    assert limits["daily_cap"] == 3
    assert me.headers["RateLimit-Limit"] == "3"
    assert me.headers["RateLimit-Policy"] == '3;w=86400;policy="api-daily"'


# ------------------------------------------------------------------------------------------ admin
def test_the_admin_row_has_no_search_window_and_no_daily_cap(client: TestClient, db: Session) -> None:
    """docs/23 §6: Admin (operator session) 1,200 / hour; search "—"; daily cap "—"."""
    who = _caller(client, db, "admin")
    # spent windows the admin row would be counted in if it had them: nothing refuses
    _spend(daily_key(who.subject), 2, limit=1, window_seconds=DAY_SECONDS)
    _spend(search_key("admin", who.subject), 2, limit=1)
    for n in range(25):  # more searches than the public (20) row allows in an hour
        response = client.get("/v1/proposals", params={"q": "x"})
        assert response.status_code == 200, (n, response.text)
    assert response.headers["RateLimit-Policy"] == '1200;w=3600;policy="admin-read"'
    assert response.headers["RateLimit-Remaining"] == str(1200 - 25)


# --------------------------------------------------------------------------- unresolved credential
def test_a_junk_credential_is_metered_on_the_anonymous_search_and_daily_windows(client: TestClient) -> None:
    junk = {"Authorization": "Bearer not-a-key"}
    first = client.get("/v1/proposals", params={"q": "x"}, headers=junk)
    assert first.status_code == 200
    assert first.headers["RateLimit-Policy"] == '20;w=3600;policy="public-search"'
    assert first.headers["RateLimit-Remaining"] == "19"
    _spend(daily_key("testclient"), DAILY_CAPS["public"] or 0, limit=1_000, window_seconds=DAY_SECONDS)
    refused = client.get("/v1/sources", headers=junk)
    assert refused.status_code == 429 and refused.json()["code"] == "quota_exceeded"
    assert refused.json()["detail"] == "1,000 requests per day on this address; resets at 00:00 UTC."
    assert refused.headers["Retry-After"] == TO_MIDNIGHT
    # the same address without the junk header is the same caller
    assert client.get("/v1/sources").json()["code"] == "quota_exceeded"


# ------------------------------------------------------------------------- the site's own calls
@pytest.fixture()
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("API_INTERNAL_TOKEN", TOKEN)
    return TOKEN


def test_the_sites_calls_do_not_spend_the_public_daily_cap(client: TestClient, internal_token: str) -> None:
    """Every page view reaches the API from the web container's one address. Without the
    exemption the site's first 1,000 calls of the day would close the API to every visitor until
    midnight UTC; with it, neither the site's address nor the visitor's is counted."""
    for address in ("testclient", "203.0.113.5"):
        _spend(daily_key(address), 1_000, limit=1_000, window_seconds=DAY_SECONDS)
        _spend(search_key("public", address), 20, limit=20)
    site = {"X-Internal-Token": internal_token, "X-Visitor-IP": "203.0.113.5"}
    for _ in range(3):
        response = client.get("/v1/proposals", params={"q": "solar"}, headers=site)
        assert response.status_code == 200 and "RateLimit-Limit" not in response.headers
    # a forwarded visitor session is exempt too
    with_cookie = client.get(
        "/v1/proposals", params={"q": "solar"}, headers={**site, "Cookie": "session=stale"}
    )
    assert with_cookie.status_code == 200
    # the same address without the site's identity is refused: the exemption is what protects it
    assert client.get("/v1/proposals").json()["code"] == "quota_exceeded"


def test_the_web_client_presents_the_sites_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`web/api_client.py` in-process mode (tests, local development) mints and sends the token
    itself; production's HTTP mode sends the configured one and deploy.sh refuses to deploy
    without it. Either way the site's searches do not spend the public windows."""
    from web.api_client import build_client

    monkeypatch.setenv("API_INTERNAL_TOKEN", TOKEN)
    api = build_client(api_base_url="")  # "" selects the in-process transport
    try:
        _spend(daily_key("testclient"), 1_000, limit=1_000, window_seconds=DAY_SECONDS)
        for _ in range((SEARCH_LIMITS["public"] or 0) + 5):
            api.get("/v1/proposals", params={"q": "solar"})  # raises ApiError on a 429
    finally:
        api.close()


# ------------------------------------------------------------------------------------------ bulk
def test_a_bulk_request_spends_the_daily_cap_and_not_the_read_window(client: TestClient, db: Session) -> None:
    who = _caller(client, db, "api")
    response = client.get("/v1/bulk/events", headers=who.headers)
    assert response.status_code == 200, response.text
    # the bulk route's own window names the headers (services/api/bulk.py)
    assert response.headers["RateLimit-Policy"] == '20;w=3600;policy="api-bulk"'
    assert _left(daily_key(who.subject), limit=50_000, window_seconds=DAY_SECONDS) == 50_000 - 1
    assert _left(read_key("api", who.subject), limit=6_000) == 6_000
    # `ratelimit.bulk_key` is the key bulk.py spends: one request, one unit
    assert _left(bulk_key(who.subject), limit=20) == 20 - 1


def test_a_refused_bulk_request_costs_no_daily_unit(client: TestClient, db: Session) -> None:
    who = _caller(client, db, "api")
    _spend(bulk_key(who.subject), 20, limit=20)
    for _ in range(5):
        refused = client.get("/v1/bulk/proposals", headers=who.headers)
        assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"
        assert refused.headers["RateLimit-Policy"] == '20;w=3600;policy="api-bulk"'
    assert _left(daily_key(who.subject), limit=50_000, window_seconds=DAY_SECONDS) == 50_000


def test_a_spent_daily_cap_refuses_bulk_with_quota_exceeded(client: TestClient, db: Session) -> None:
    who = _caller(client, db, "api", daily_quota=1)
    assert client.get("/v1/bulk/events", headers=who.headers).status_code == 200
    refused = client.get("/v1/bulk/events", headers=who.headers)
    assert refused.status_code == 429
    assert refused.json()["code"] == "quota_exceeded"
    assert refused.headers["Retry-After"] == TO_MIDNIGHT
