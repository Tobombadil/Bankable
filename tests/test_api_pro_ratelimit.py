"""Real per-tier token-bucket rate limits (task brief item 2: "rate-limit headers become real
per-tier token buckets, in-memory for now, interface ready for Redis";
docs/23-api-spec-outline.md §6)."""

from __future__ import annotations

from services.api.ratelimit import TIER_LIMITS, InMemoryRateLimiter, RateLimiter, policy_header
from tests.conftest import login, make_account, make_api_key, make_user


def test_limiter_allows_up_to_the_limit_then_blocks():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        result = limiter.check("k", limit=5)
        assert result.allowed is True
    blocked = limiter.check("k", limit=5)
    assert blocked.allowed is False
    assert blocked.remaining == 0


def test_limiter_counts_are_independent_per_key():
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        limiter.check("a", limit=3)
    assert limiter.check("a", limit=3).allowed is False
    assert limiter.check("b", limit=3).allowed is True


def test_limiter_satisfies_the_rate_limiter_protocol():
    limiter: RateLimiter = InMemoryRateLimiter()
    result = limiter.check("x", limit=10)
    assert result.limit == 10


def test_policy_header_shape():
    limiter = InMemoryRateLimiter()
    result = limiter.check("k", limit=60)
    header = policy_header("public", result)
    assert header == '60;w=3600;policy="public-read"'


def test_tier_limits_match_docs_23_defaults():
    assert TIER_LIMITS["public"] == 60
    assert TIER_LIMITS["pro"] == 600
    assert TIER_LIMITS["api"] == 6000


def test_response_headers_reflect_the_callers_tier(client, db):
    resp = client.get("/v1/proposals")
    assert resp.headers["RateLimit-Limit"] == "60"
    assert 'policy="public-read"' in resp.headers["RateLimit-Policy"]

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    pro_resp = client.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"})
    assert pro_resp.headers["RateLimit-Limit"] == "600"
    assert 'policy="pro-read"' in pro_resp.headers["RateLimit-Policy"]


def test_pro_route_429s_once_the_bucket_is_exhausted(client, db):
    """Uses a low-entitlement caller's own per-key bucket exhaustion rather than looping 600
    times: the limiter is exercised directly against the same shared `default_limiter` a real
    request would hit, keyed the same way `services/api/pro.py::_rate_limit_headers` keys it."""
    from services.api.ratelimit import default_limiter

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    # Exhaust the bucket this user's session will be keyed under: each `check()` call consumes
    # exactly one unit, so fill it to one below the limit with direct calls first.
    for _ in range(600 - 1):
        default_limiter.check(f"pro:{user.id}", limit=600)
    ok = client.get("/v1/me")
    assert ok.status_code == 200
    limited = client.get("/v1/me")
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert "Retry-After" in limited.headers
