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


def test_a_junk_credential_does_not_escape_the_public_bucket(client, db):
    """API audit 2026-09-18 (S3): a request carrying any `Authorization` header skipped the
    middleware's anonymous bucket and, when the credential resolved to nothing, was served on the
    public tier unmetered. The auth dependency now meters such requests on the caller's IP bucket."""
    from services.api.ratelimit import default_limiter

    for _ in range(TIER_LIMITS["public"] - 1):
        default_limiter.check("public:testclient", limit=TIER_LIMITS["public"])
    ok = client.get("/v1/proposals", headers={"Authorization": "Bearer totally-invalid"})
    assert ok.status_code == 200
    limited = client.get("/v1/proposals", headers={"Authorization": "Bearer totally-invalid"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]


def test_cache_control_is_private_for_any_credentialed_request(client):
    """API audit 2026-09-18 (S1): every /v1 GET was `public, max-age=300`, including `/v1/me`
    and live Pro rows. Anonymous responses stay shareable; anything carrying a credential is not."""
    anon = client.get("/v1/proposals")
    assert anon.headers["Cache-Control"] == "public, max-age=300"
    assert "Authorization" in anon.headers["Vary"] and "Cookie" in anon.headers["Vary"]
    cred = client.get("/v1/proposals", headers={"Authorization": "Bearer anything"})
    assert cred.headers["Cache-Control"] == "private, no-store"
    cookie = client.get("/v1/proposals", headers={"Cookie": "session=anything"})
    assert cookie.headers["Cache-Control"] == "private, no-store"


def test_internal_token_bypasses_the_anonymous_bucket(client, monkeypatch):
    """The public site calls the API server-side from one address for every visitor; with a
    matching `X-Internal-Token` those calls are not metered on the anonymous per-IP bucket
    (live probe 2026-09-18: a sitemap render exhausted 60/h and every map pan returned 429)."""
    from services.api.ratelimit import default_limiter

    monkeypatch.setenv("API_INTERNAL_TOKEN", "test-internal-token-not-a-secret")
    for _ in range(TIER_LIMITS["public"]):
        default_limiter.check("public:testclient", limit=TIER_LIMITS["public"])
    assert client.get("/v1/proposals").status_code == 429
    ok = client.get("/v1/proposals", headers={"X-Internal-Token": "test-internal-token-not-a-secret"})
    assert ok.status_code == 200
    wrong = client.get("/v1/proposals", headers={"X-Internal-Token": "wrong"})
    assert wrong.status_code == 429
