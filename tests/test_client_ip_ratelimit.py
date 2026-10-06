"""Per-visitor rate limits behind the production proxies (devops audit 2026-09-30 F1, architect A3,
backend F1 and F8; `services/api/client_ip.py`).

Three properties, each tested against the real app and the real limiter:

- distinct clients get distinct buckets, whether they reach the API directly (their peer address)
  or through the site (`X-Visitor-IP` alongside the internal token);
- a forwarded address is refused unless it comes with the internal token: `X-Visitor-IP` alone,
  `X-Visitor-IP` with a wrong token and a raw `X-Forwarded-For` all leave the caller in its own
  peer's bucket (uvicorn's own `X-Forwarded-For` handling is pinned in `infra/test_entrypoint.py`);
- a credential that does not resolve is metered on every GET route, not only on the routes that ask
  for an `AuthContext`, and a valid credential stays exempt.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from services.api.app import app
from services.api.client_ip import client_ip, client_ip_prefix, rate_limit_address
from services.api.ratelimit import TIER_LIMITS, default_limiter
from tests.conftest import login, make_account, make_api_key, make_user

TOKEN = "test-internal-token-not-a-secret"
PUBLIC = TIER_LIMITS["public"]


@pytest.fixture()
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("API_INTERNAL_TOKEN", TOKEN)
    return TOKEN


Peer = Callable[[str], TestClient]


@pytest.fixture()
def peer(client: TestClient) -> Peer:
    """`TestClient` bound to a chosen peer address, sharing `client`'s DB override."""

    def make(address: str) -> TestClient:
        return TestClient(app, client=(address, 40000))

    return make


def _exhaust(key: str) -> None:
    for _ in range(PUBLIC):
        default_limiter.check(key, limit=PUBLIC)


def _request(
    headers: dict[str, str] | None = None, client: tuple[str, int] | None = ("10.0.0.2", 1)
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


# ------------------------------------------------------------------------------ distinct buckets
def test_two_direct_clients_get_two_public_buckets(peer: Peer) -> None:
    first, second = peer("203.0.113.5"), peer("198.51.100.9")
    _exhaust("public:203.0.113.5")
    assert first.get("/v1/sources").status_code == 429
    response = second.get("/v1/sources")
    assert response.status_code == 200
    assert response.headers["RateLimit-Remaining"] == str(PUBLIC - 1)


def test_two_visitors_through_the_site_get_two_login_buckets(client: TestClient, internal_token: str) -> None:
    """The site's server-side login call carries the visitor's address; the auth limiter keys on it.
    Before, every site login shared one `auth-login:<web /24>` bucket of 20 per hour."""
    body = {"email": "nobody@example.com", "password": "wrong password 123"}
    visitor_a = {"X-Internal-Token": internal_token, "X-Visitor-IP": "203.0.113.5"}
    visitor_b = {"X-Internal-Token": internal_token, "X-Visitor-IP": "198.51.100.9"}
    statuses = [client.post("/v1/auth/login", json=body, headers=visitor_a).status_code for _ in range(21)]
    assert statuses[:20] == [401] * 20
    assert statuses[20] == 429
    assert client.post("/v1/auth/login", json=body, headers=visitor_b).status_code == 401


def test_ipv6_clients_share_a_bucket_per_64(peer: Peer) -> None:
    _exhaust("public:2001:db8:1:2::/64")
    assert peer("2001:db8:1:2::1").get("/v1/sources").status_code == 429
    assert peer("2001:db8:1:2:ffff::9").get("/v1/sources").status_code == 429
    assert peer("2001:db8:1:3::1").get("/v1/sources").status_code == 200


# ------------------------------------------------------------------------------ spoofing refused
def test_x_forwarded_for_from_the_internet_is_not_believed(peer: Peer) -> None:
    attacker = peer("203.0.113.5")
    _exhaust("public:203.0.113.5")
    spoofed = attacker.get("/v1/sources", headers={"X-Forwarded-For": "198.51.100.9"})
    assert spoofed.status_code == 429


def test_visitor_header_without_the_token_is_ignored(peer: Peer, internal_token: str) -> None:
    attacker = peer("203.0.113.5")
    _exhaust("public:203.0.113.5")
    assert attacker.get("/v1/sources", headers={"X-Visitor-IP": "198.51.100.9"}).status_code == 429
    wrong = {"X-Visitor-IP": "198.51.100.9", "X-Internal-Token": "guess"}
    assert attacker.get("/v1/sources", headers=wrong).status_code == 429


def test_login_bucket_cannot_be_escaped_with_a_visitor_header(
    client: TestClient, internal_token: str
) -> None:
    body = {"email": "nobody@example.com", "password": "wrong password 123"}
    for n in range(20):
        client.post("/v1/auth/login", json=body, headers={"X-Visitor-IP": f"198.51.{n}.9"})
    rotated = client.post("/v1/auth/login", json=body, headers={"X-Visitor-IP": "192.0.2.77"})
    assert rotated.status_code == 429


def test_client_ip_resolution_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_INTERNAL_TOKEN", TOKEN)
    assert client_ip(_request()) == "10.0.0.2"
    assert client_ip(_request({"X-Visitor-IP": "203.0.113.5"})) == "10.0.0.2"
    assert client_ip(_request({"X-Visitor-IP": "203.0.113.5", "X-Internal-Token": TOKEN})) == "203.0.113.5"
    # a malformed or list-valued header is not an address: fall back to the peer
    for junk in ("203.0.113.5, 1.2.3.4", "not-an-ip", ""):
        assert client_ip(_request({"X-Visitor-IP": junk, "X-Internal-Token": TOKEN})) == "10.0.0.2"
    assert client_ip(_request(client=None)) == "unknown"
    assert rate_limit_address(_request({"X-Visitor-IP": "2001:db8::5", "X-Internal-Token": TOKEN})) == (
        "2001:db8::/64"
    )
    assert (
        client_ip_prefix(_request({"X-Visitor-IP": "203.0.113.5", "X-Internal-Token": TOKEN}))
        == "203.0.113.0"
    )
    assert client_ip_prefix(_request(client=None)) is None
    monkeypatch.setenv("API_INTERNAL_TOKEN", "")
    # an empty configured token never matches, not even an empty header
    assert client_ip(_request({"X-Visitor-IP": "203.0.113.5", "X-Internal-Token": ""})) == "10.0.0.2"


# ------------------------------------------------------------------------------ junk credentials
def _get_paths() -> list[str]:
    """Every GET path the app serves, path parameters filled with a dummy. Read from the OpenAPI
    document: FastAPI keeps included routers nested, so `app.routes` alone lists only a third."""
    paths = [p for p, ops in app.openapi()["paths"].items() if "get" in ops]
    return sorted({re.sub(r"\{[^}]+\}", "x", p) for p in paths})


@pytest.mark.parametrize(
    "credential",
    [{"Authorization": "Bearer junk"}, {"Authorization": "x"}, {"Cookie": "session=junk"}],
    ids=["bearer", "bare", "cookie"],
)
def test_a_junk_credential_is_metered_on_every_get_route(
    client: TestClient, credential: dict[str, str]
) -> None:
    """Backend audit F8: 13 public GET routes never resolved auth, so any `Authorization` header
    skipped the public bucket there (30/30 served with the bucket exhausted). Every GET route now
    answers 429 once the caller's bucket is spent, whatever it presents."""
    paths = _get_paths()
    assert {"/v1/assets", "/v1/assets/geo", "/v1/sources", "/v1/licences", "/v1/organizations"} <= set(paths)
    assert any(p.startswith("/feeds/") for p in paths)
    _exhaust("public:testclient")
    served = {p: client.get(p, headers=credential).status_code for p in paths}
    assert {p: s for p, s in served.items() if s != 429} == {}


def test_a_junk_credential_below_the_limit_is_served_and_counted_once(client: TestClient) -> None:
    response = client.get("/v1/sources", headers={"Authorization": "Bearer junk"})
    assert response.status_code == 200
    # one request, one unit: the app-wide dependency and `get_auth_context` share one charge
    assert default_limiter.check("public:testclient", limit=PUBLIC).remaining == PUBLIC - 2
    client.get("/v1/proposals", headers={"Authorization": "Bearer junk"})
    assert default_limiter.check("public:testclient", limit=PUBLIC).remaining == PUBLIC - 4


def test_a_valid_api_key_does_not_draw_on_the_public_bucket(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    _exhaust("public:testclient")
    for path in ("/v1/sources", "/v1/assets", "/v1/organizations", "/v1/proposals"):
        assert client.get(path, headers={"Authorization": f"Bearer {secret}"}).status_code == 200, path


def test_the_site_forwarding_a_stale_cookie_is_not_metered(client: TestClient, internal_token: str) -> None:
    """The site forwards visitors' cookies and is exempt from the anonymous bucket; a stale cookie
    on its request must not reintroduce a bucket (keyed on anyone) for it."""
    _exhaust("public:testclient")
    headers = {"X-Internal-Token": internal_token, "Cookie": "session=stale", "X-Visitor-IP": "203.0.113.5"}
    assert client.get("/v1/sources", headers=headers).status_code == 200
    assert client.get("/v1/proposals", headers=headers).status_code == 200


# ------------------------------------------------------------------------- valid credentials (QA-3)
FREE = TIER_LIMITS["free_account"]


def test_a_free_session_on_a_list_route_is_metered_at_the_free_account_tier(
    client: TestClient,
    db,  # type: ignore[no-untyped-def]
) -> None:
    """QA audit QA-3: a free account pulled 150 pages of `/v1/proposals` with no `RateLimit-*`
    headers. A signed-in account is now metered at docs/23 §6's `free_account` row (300 an hour),
    keyed by user, on every route, and gets 429 with `Retry-After` when the bucket is spent."""
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    first = client.get("/v1/proposals")
    assert first.status_code == 200
    assert first.headers["RateLimit-Limit"] == str(FREE)
    assert first.headers["RateLimit-Remaining"] == str(FREE - 1)
    assert first.headers["RateLimit-Policy"] == f'{FREE};w=3600;policy="free_account-read"'
    for _ in range(FREE - 2):
        default_limiter.check(f"free_account:{user.id}", limit=FREE)
    last = client.get("/v1/sources")
    assert last.status_code == 200 and last.headers["RateLimit-Remaining"] == "0"
    over = client.get("/v1/proposals")
    assert over.status_code == 429
    assert over.json()["code"] == "rate_limited"
    assert int(over.headers["Retry-After"]) >= 0 and over.headers["RateLimit-Remaining"] == "0"


def test_a_key_is_metered_per_key_at_its_tier_once_per_request(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    headers = {"Authorization": f"Bearer {secret}"}
    remaining = [
        int(client.get(path, headers=headers).headers["RateLimit-Remaining"])
        for path in (
            "/v1/proposals",  # metered only by the app-wide dependency
            "/v1/me",  # also asks `_rate_limit_headers`: same charge, not a second one
            "/v1/assets",
        )
    ]
    assert remaining == [599, 598, 597]
    for _ in range(597):
        default_limiter.check(f"pro:{key.public_id}", limit=600)
    assert client.get("/v1/organizations", headers=headers).status_code == 429


def test_a_public_scope_key_counts_as_a_free_account(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:public"])
    db.commit()
    response = client.get("/v1/proposals", headers={"Authorization": f"Bearer {secret}"})
    assert response.headers["RateLimit-Limit"] == str(FREE)


def test_the_sites_own_calls_with_a_visitor_session_stay_exempt(
    client: TestClient,
    db,
    internal_token: str,  # type: ignore[no-untyped-def]
) -> None:
    """The site forwards its visitors' cookies on server-side page calls; those carry the internal
    token and are not metered on any bucket (a page view makes several API calls)."""
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    headers = {"X-Internal-Token": internal_token, "X-Visitor-IP": "203.0.113.5"}
    for _ in range(3):
        response = client.get("/v1/proposals", headers=headers)
        assert response.status_code == 200 and "RateLimit-Limit" not in response.headers
    assert default_limiter.check(f"free_account:{user.id}", limit=FREE).remaining == FREE - 1
