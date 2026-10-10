"""Unit tests for the limiter's windows and counting rules (`services/api/ratelimit.py`; docs/23
§6, §8). The request-level behaviour, tier by tier, is in `tests/test_api_ratelimit_windows.py`."""

from __future__ import annotations

import datetime as dt
import pathlib
import types

import pytest
import yaml

from services.api.ratelimit import (
    DAILY_CAPS,
    DAY_SECONDS,
    SEARCH_LIMITS,
    TIER_LIMITS,
    WINDOW_SECONDS,
    Bucket,
    InMemoryRateLimiter,
    RateLimiter,
    daily_cap_for,
    daily_key,
    is_search,
    meter,
    request_buckets,
)

SPEC = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
#: 2026-10-09T18:30:00Z: 5.5 hours, 19,800 seconds, before midnight UTC.
EVENING = dt.datetime(2026, 10, 9, 18, 30, tzinfo=dt.UTC).timestamp()
MIDNIGHT = dt.datetime(2026, 10, 10, tzinfo=dt.UTC).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _bucket(
    key: str, limit: int, *, window: int = WINDOW_SECONDS, kind: str = "read", charge: bool = True
) -> Bucket:
    return Bucket(key, limit, window, "public", kind, charge)


# ------------------------------------------------------------------------------------ the tables
def test_every_tier_has_a_search_and_a_daily_entry() -> None:
    """A tier added to `TIER_LIMITS` without a search or daily figure must fail, not run unlimited."""
    assert set(SEARCH_LIMITS) == set(TIER_LIMITS) == set(DAILY_CAPS)


def test_the_tables_match_docs_23_and_the_spec() -> None:
    """docs/23 §6's table, as api/openapi.yaml `x-rate-limits.tiers` carries it."""
    tiers = yaml.safe_load(SPEC.read_text())["x-rate-limits"]["tiers"]
    names = {
        "public": "public_per_ip",
        "free_account": "free_account",
        "pro": "pro",
        "api": "api",
        "admin": "admin",
    }
    for tier, spec_name in names.items():
        row = tiers[spec_name]
        assert TIER_LIMITS[tier] == row["read_per_hour"], tier
        assert SEARCH_LIMITS[tier] == row.get("search_per_hour"), tier
        assert DAILY_CAPS[tier] == row.get("daily_cap"), tier
    assert (SEARCH_LIMITS["public"], DAILY_CAPS["public"]) == (20, 1_000)
    assert (SEARCH_LIMITS["free_account"], DAILY_CAPS["free_account"]) == (60, 3_000)
    assert (SEARCH_LIMITS["pro"], DAILY_CAPS["pro"]) == (120, 10_000)
    assert (SEARCH_LIMITS["api"], DAILY_CAPS["api"]) == (600, 50_000)
    assert (SEARCH_LIMITS["admin"], DAILY_CAPS["admin"]) == (None, None)


# ---------------------------------------------------------------------------- consume semantics
def test_consume_spends_one_unit_of_every_bucket_when_all_have_room() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    results = limiter.consume([_bucket("r", 3), _bucket("s", 2, kind="search")])
    assert [(r.allowed, r.remaining) for r in results] == [(True, 2), (True, 1)]


def test_consume_spends_nothing_when_one_bucket_is_full() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    read, search = _bucket("r", 10), _bucket("s", 1, kind="search")
    limiter.consume([read, search])
    refused = limiter.consume([read, search])
    assert [r.allowed for r in refused] == [True, False]
    # the read bucket kept its unit: 10 - 1 spent by the first call only
    assert refused[0].remaining == 9
    assert limiter.consume([read])[0].remaining == 8


def test_a_guard_is_checked_but_never_spent() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    guard = _bucket("g", 1, kind="bulk", charge=False)
    for _ in range(3):
        assert [r.allowed for r in limiter.consume([_bucket("d", 10), guard])] == [True, True]
    limiter.check("g", limit=1)  # the route spends it
    refused = limiter.consume([_bucket("d", 10), guard])
    assert [r.allowed for r in refused] == [True, False]
    assert refused[0].remaining == 7  # three admitted calls spent three; the refused one nothing


def test_check_still_spends_on_refusal() -> None:
    """The single-window callers (login, intake, bulk) rely on `check` counting refused calls."""
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    assert limiter.check("k", limit=1).allowed
    assert not limiter.check("k", limit=1).allowed
    assert limiter.consume([_bucket("k", 3)])[0].remaining == 0  # 2 spent, 1 left, now 0


def test_the_limiter_satisfies_the_protocol() -> None:
    limiter: RateLimiter = InMemoryRateLimiter()
    assert limiter.consume([_bucket("x", 5)])[0].limit == 5


# ------------------------------------------------------------------------------- the daily window
def test_a_daily_window_resets_at_midnight_utc() -> None:
    clock = Clock(EVENING)
    limiter = InMemoryRateLimiter(clock=clock)
    day = _bucket("d", 2, window=DAY_SECONDS, kind="daily")
    first = limiter.consume([day])[0]
    assert first.reset_seconds == 19_800  # 18:30:00 -> 00:00:00
    limiter.consume([day])
    assert not limiter.consume([day])[0].allowed
    clock.now = MIDNIGHT - 0.5
    late = limiter.consume([day])[0]
    assert not late.allowed and late.reset_seconds == 1  # rounded up: never "retry now"
    clock.now = MIDNIGHT
    assert limiter.consume([day])[0].allowed
    assert limiter.consume([day])[0].reset_seconds == DAY_SECONDS


def test_ended_windows_are_swept() -> None:
    clock = Clock(EVENING)
    limiter = InMemoryRateLimiter(clock=clock)
    limiter.check("hourly", limit=5)
    limiter.check("daily", limit=5, window_seconds=DAY_SECONDS)
    clock.now = EVENING + 2 * WINDOW_SECONDS  # the hourly window ended; the daily one has not
    limiter.check("other", limit=5)
    assert set(limiter._windows) == {"daily", "other"}


# ------------------------------------------------------------------------------- counting rules
def _request(method: str, query: dict[str, str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(method=method, query_params=query)


@pytest.mark.parametrize(
    ("method", "query", "expected"),
    [
        ("GET", {"q": "solar"}, True),
        ("GET", {"q": " "}, True),  # the routes search for it, so it is a search
        ("GET", {"q": ""}, False),
        ("GET", {"technology": "solar"}, False),
        ("POST", {"q": "solar"}, False),
    ],
)
def test_what_counts_as_a_search(method: str, query: dict[str, str], expected: bool) -> None:
    assert is_search(_request(method, query)) is expected  # type: ignore[arg-type]


def test_buckets_per_tier() -> None:
    kinds = {
        tier: [b.kind for b in request_buckets(tier, "s", search=True, daily_cap=DAILY_CAPS[tier])]
        for tier in TIER_LIMITS
    }
    assert (
        kinds["public"]
        == kinds["free_account"]
        == kinds["pro"]
        == kinds["api"]
        == ["read", "search", "daily"]
    )
    assert kinds["admin"] == ["read"]
    assert [b.kind for b in request_buckets("pro", "s", search=False, daily_cap=10)] == ["read", "daily"]
    assert [b.kind for b in request_buckets("api", "s", search=True, daily_cap=10, read=False)] == ["daily"]
    daily = request_buckets("pro", "key_1", search=False, daily_cap=7)[-1]
    assert (daily.key, daily.limit, daily.window_seconds) == (daily_key("key_1"), 7, DAY_SECONDS)


def test_a_keys_daily_quota_overrides_its_tier() -> None:
    def key(quota: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(daily_quota=quota)

    assert daily_cap_for("api") == 50_000
    assert daily_cap_for("api", key(None)) == 50_000  # type: ignore[arg-type]
    assert daily_cap_for("api", key(100)) == 100  # type: ignore[arg-type]
    assert daily_cap_for("free_account", key(5_000)) == 5_000  # type: ignore[arg-type]
    assert daily_cap_for("pro", key(0)) == 0  # type: ignore[arg-type]
    assert daily_cap_for("pro", key(-3)) == 0  # type: ignore[arg-type]
    assert daily_cap_for("admin") is None


# ------------------------------------------------------------------------------ meter decisions
def test_headers_name_the_window_closest_to_exhaustion() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    read = Bucket("r", 60, WINDOW_SECONDS, "public", "read")
    search = Bucket("s", 20, WINDOW_SECONDS, "public", "search")
    daily = Bucket("d", 1_000, DAY_SECONDS, "public", "daily")
    decision = meter([read, search, daily], limiter=limiter)
    assert decision.problem is None
    assert decision.headers["RateLimit-Policy"] == '20;w=3600;policy="public-search"'
    assert decision.headers["RateLimit-Remaining"] == "19"
    assert decision.headers_for("read") == {
        "RateLimit-Limit": "60",
        "RateLimit-Remaining": "59",
        "RateLimit-Reset": "1800",
        "RateLimit-Policy": '60;w=3600;policy="public-read"',
    }
    # equal remaining: the later reset wins (the daily window), the more cautious answer
    tied = meter(
        [Bucket("r2", 5, WINDOW_SECONDS, "pro", "read"), Bucket("d2", 5, DAY_SECONDS, "pro", "daily")],
        limiter=limiter,
    )
    assert tied.headers["RateLimit-Policy"] == '5;w=86400;policy="pro-daily"'


def test_a_spent_daily_cap_answers_quota_exceeded_even_when_an_hourly_window_is_spent_too() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    read = Bucket("r", 1, WINDOW_SECONDS, "public", "read")
    daily = Bucket("d", 1, DAY_SECONDS, "public", "daily")
    assert meter([read, daily], limiter=limiter).problem is None
    decision = meter([read, daily], limiter=limiter)
    assert decision.problem is not None
    assert (decision.problem.code, decision.problem.status) == ("quota_exceeded", 429)
    assert decision.headers["Retry-After"] == "19800"
    assert decision.headers["RateLimit-Policy"] == '1;w=86400;policy="public-daily"'
    assert decision.headers["RateLimit-Remaining"] == "0"


def test_an_hourly_refusal_is_rate_limited_until_the_latest_refusing_reset() -> None:
    limiter = InMemoryRateLimiter(clock=Clock(EVENING))
    search = Bucket("s", 1, WINDOW_SECONDS, "pro", "search")
    assert meter([search], limiter=limiter).problem is None
    decision = meter([search, Bucket("d", 10, DAY_SECONDS, "pro", "daily")], limiter=limiter)
    assert decision.problem is not None and decision.problem.code == "rate_limited"
    assert decision.headers["Retry-After"] == "1800"
    assert "searches" in (decision.problem.detail or "")
