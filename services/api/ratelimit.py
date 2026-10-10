"""Per-tier rate limits, search windows and daily caps (docs/23-api-spec-outline.md §6, §8;
docs/04-standards.md API-6). `services/README.md` (Sprint 2) recorded rate-limit headers as static
placeholders; a later sprint replaced them with a real in-memory limiter behind an interface a
Redis implementation can drop into later without touching call sites (`docs/20-architecture.md`
§7: "Redis is not needed until sustained request rates exceed roughly 50 requests per second
across the fleet").

**What a request is counted in** (docs/23 §6 "As built", 2026-10-10):

- the tier's **read** window (`TIER_LIMITS`, per hour): every metered request, whatever its method;
- the tier's **search** window (`SEARCH_LIMITS`, per hour): a `GET` whose `q` is non-empty, read
  the way every list route reads it. A search is a read, so it spends one unit of each: the search
  figure is a sub-limit inside the read figure, never extra capacity on top of it;
- the **daily cap** (`DAILY_CAPS`, per UTC day): every metered request, bulk streams included. A
  key's `api_key.daily_quota` replaces its tier's figure (docs/21 §3.17 "Daily request cap");
- the admin row has neither a search window nor a daily cap (docs/23 §6: "—").

A request spends one unit from each of its windows or from none: a request one window refuses
costs nothing in the others (`InMemoryRateLimiter.consume`), so a caller polling past an hourly
limit does not burn its daily cap, and one past its daily cap keeps getting `quota_exceeded`
rather than drifting into `rate_limited`. The `RateLimit-*` headers describe the window closest
to exhaustion (fewest requests left; on a tie, the later reset), and `RateLimit-Policy` names that
one window, so a client that paces itself on the headers is never refused without warning.

**Recorded limitation.** Counts live in this process's memory (`default_limiter`). They are per
process and an api restart empties them, daily counts included. The one-server beta runs one api
process (one replica, `WEB_CONCURRENCY=1`: `compose.single.yml` over `compose.prod.yml`; docs/64), so
a caller has one budget there; with N processes or replicas it would have up to N. docs/20 §7's
Postgres `rate_bucket` window is not built.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from services.api.client_ip import is_internal_request, rate_limit_address
from services.api.deps import get_db
from services.api.errors import ProblemError

if TYPE_CHECKING:
    from services.db.models import ApiKey

#: docs/23 §6 "Read endpoints", per hour, keyed by the resolved tier. Also every request's window:
#: writes have no window of their own yet (docs/23 §6 "Writes" is not built), so they spend here.
TIER_LIMITS: dict[str, int] = {
    "public": 60,
    "free_account": 300,
    "pro": 600,
    "api": 6000,
    "admin": 1200,
}
#: docs/23 §6 "Search (`q=`)", per hour. `None` is the table's "—": no search window at all. Every
#: tier in `TIER_LIMITS` has an entry, so a tier added there without one fails loudly (KeyError).
SEARCH_LIMITS: dict[str, int | None] = {
    "public": 20,
    "free_account": 60,
    "pro": 120,
    "api": 600,
    "admin": None,
}
#: docs/23 §6 "Daily cap", per UTC day (00:00 to 00:00). `None` is the table's "—" (admin).
DAILY_CAPS: dict[str, int | None] = {
    "public": 1_000,
    "free_account": 3_000,
    "pro": 10_000,
    "api": 50_000,
    "admin": None,
}
WINDOW_SECONDS = 3600
#: A UTC day. Unix time has no leap seconds, so a window aligned to a multiple of this starts at
#: 00:00 UTC exactly and `RateLimit-Reset` on it is the time to midnight UTC (docs/23 §8).
DAY_SECONDS = 86_400

#: Paths that meter a credential on a window of their own instead of the tier's read window
#: (`services/api/bulk.py`: "the key's own `bulk` bucket and nothing from its `read` bucket").
#: They still count against the daily cap.
OWN_BUCKET_PREFIXES = ("/v1/bulk/",)


@dataclass(frozen=True)
class PlanQuota:
    """The "Bulk / export" column of docs/23 §6 for one plan: `None` means the plan has no such
    allowance at all (a `403 forbidden_tier` on the endpoint, never an unbounded quota)."""

    exports_per_day: int | None
    export_rows_max: int | None
    bulk_requests_per_hour: int | None


#: docs/23 §6 / api/openapi.yaml `x-rate-limits.tiers`, keyed by the resolved entitlement. Pro:
#: "5 exports / day, 10,000 rows each" (US-603 AC1's "10,000 default, configurable per plan" lives
#: here, per plan). API: "20 bulk requests / hour"; the table names no export allowance for the
#: API plan, and docs/23 §3.2 says an API plan has "everything in [Pro]", so it inherits Pro's
#: export figures rather than getting none -- a documented reading, recorded in services/README.md.
#: `admin` (an operator's own account) exports on Pro's figures too; bulk stays API-key-only
#: (`read:bulk` is a key scope, docs/23 §5). Tiers absent here have neither allowance.
PLAN_QUOTAS: dict[str, PlanQuota] = {
    "pro": PlanQuota(exports_per_day=5, export_rows_max=10_000, bulk_requests_per_hour=None),
    "api": PlanQuota(exports_per_day=5, export_rows_max=10_000, bulk_requests_per_hour=20),
    "admin": PlanQuota(exports_per_day=5, export_rows_max=10_000, bulk_requests_per_hour=None),
}
NO_QUOTA = PlanQuota(exports_per_day=None, export_rows_max=None, bulk_requests_per_hour=None)


def plan_quota(tier: str) -> PlanQuota:
    return PLAN_QUOTAS.get(tier, NO_QUOTA)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


@dataclass(frozen=True)
class Bucket:
    """One window a request is counted in. `kind` is `read`, `search`, `daily` or `bulk`;
    `RateLimit-Policy` names it as `policy="<tier>-<kind>"`. A bucket with `charge=False` is a
    guard: its room is checked with the others, but it is never spent (the route spends it)."""

    key: str
    limit: int
    window_seconds: int
    tier: str
    kind: str
    charge: bool = True

    @property
    def policy(self) -> str:
        return f"{self.tier}-{self.kind}"


class RateLimiter(Protocol):
    """Interface a Redis-backed limiter implements identically (`docs/20` §7's stated upgrade
    path). `check` spends one unit of one window, refused or not; `consume` spends one unit of
    each window or of none."""

    def check(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> RateLimitResult: ...

    def consume(self, buckets: Sequence[Bucket]) -> list[RateLimitResult]: ...


def _window_start(now: float, window_seconds: int) -> int:
    return int(now // window_seconds) * window_seconds


def _reset_seconds(now: float, window_start: int, window_seconds: int) -> int:
    """Whole seconds until the window ends, rounded up, so `Retry-After` never names a moment
    before the reset (it is at least 1 inside a window)."""
    return max(1, math.ceil(window_start + window_seconds - now))


class InMemoryRateLimiter:
    """Fixed-window counters keyed by bucket key, process-local: hourly windows start on the hour
    and daily windows at 00:00 UTC (both aligned to the Unix epoch). One live window per key, and
    windows that have ended are swept at most once a minute, so memory is bounded by the keys seen
    in the current day rather than growing for the life of the process.

    `clock` is `time.time` unless a test freezes it (`default_limiter.clock = lambda: ...`).
    """

    SWEEP_INTERVAL_SECONDS = 60.0

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        #: key -> (window_start, window_seconds, count)
        self._windows: dict[str, tuple[int, int, int]] = {}
        self._next_sweep = 0.0

    def _used(self, key: str, window_start: int) -> int:
        entry = self._windows.get(key)
        return entry[2] if entry is not None and entry[0] == window_start else 0

    def _sweep(self, now: float) -> None:
        if now < self._next_sweep:
            return
        self._next_sweep = now + self.SWEEP_INTERVAL_SECONDS
        ended = [k for k, (start, length, _count) in self._windows.items() if start + length <= now]
        for key in ended:
            del self._windows[key]

    def check(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> RateLimitResult:
        """One unit of one window, spent whether or not it is allowed (the single-window callers
        rely on that: login, intake, unsubscribe, privacy requests, bulk)."""
        now = self.clock()
        window_start = _window_start(now, window_seconds)
        with self._lock:
            self._sweep(now)
            count = self._used(key, window_start) + 1
            self._windows[key] = (window_start, window_seconds, count)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_seconds=_reset_seconds(now, window_start, window_seconds),
        )

    def consume(self, buckets: Sequence[Bucket]) -> list[RateLimitResult]:
        """All or nothing, under one lock: when every bucket has room, each charged bucket spends
        one unit; when any is full, none does. Results are in `buckets` order, each saying whether
        that bucket had room and what is left in it afterwards."""
        now = self.clock()
        starts = [_window_start(now, b.window_seconds) for b in buckets]
        with self._lock:
            self._sweep(now)
            used = [self._used(b.key, start) for b, start in zip(buckets, starts, strict=True)]
            admitted = all(u < b.limit for b, u in zip(buckets, used, strict=True))
            if admitted:
                for b, start, u in zip(buckets, starts, used, strict=True):
                    if b.charge:
                        self._windows[b.key] = (start, b.window_seconds, u + 1)
        results = []
        for b, start, u in zip(buckets, starts, used, strict=True):
            spent = u + 1 if admitted and b.charge else u
            results.append(
                RateLimitResult(
                    allowed=u < b.limit,
                    limit=b.limit,
                    remaining=max(0, b.limit - spent),
                    reset_seconds=_reset_seconds(now, start, b.window_seconds),
                )
            )
        return results

    def reset(self) -> None:
        """Test helper — not part of the `RateLimiter` protocol."""
        with self._lock:
            self._windows.clear()
            self._next_sweep = 0.0


#: One process-wide limiter, mirroring `services/api/deps.py`'s single `_sessionmaker` pattern.
#: `services/api/app.py`'s middleware, the app-wide `meter_request` dependency and the routes that
#: keep a window of their own share it, so a caller's budget is one budget whichever path spends it.
default_limiter = InMemoryRateLimiter()


def policy_header(
    tier: str, result: RateLimitResult, *, bucket: str = "read", window_seconds: int = WINDOW_SECONDS
) -> str:
    """`RateLimit-Policy` (docs/23 §6): the tier's `read` window by default; `/v1/bulk/*` names its
    own `bulk` window (`api/openapi.yaml` bulkProposals: "`RateLimit-Policy: bulk`"), a search its
    `search` window and the daily cap `daily` with `w=86400`."""
    return f'{result.limit};w={window_seconds};policy="{tier}-{bucket}"'


def rate_limit_headers(bucket: Bucket, result: RateLimitResult) -> dict[str, str]:
    return {
        "RateLimit-Limit": str(result.limit),
        "RateLimit-Remaining": str(result.remaining),
        "RateLimit-Reset": str(result.reset_seconds),
        "RateLimit-Policy": policy_header(
            bucket.tier, result, bucket=bucket.kind, window_seconds=bucket.window_seconds
        ),
    }


# ------------------------------------------------------------------------------- counting rules
def read_key(tier: str, subject: str) -> str:
    """The hourly read window's key, unchanged since the limiter was introduced (`public:<address>`,
    `pro:<user id>`, `api:<key public id>`)."""
    return f"{tier}:{subject}"


def search_key(tier: str, subject: str) -> str:
    return f"search:{tier}:{subject}"


def daily_key(subject: str) -> str:
    """Per credential, not per tier: an account that upgrades mid-day keeps today's count and gets
    the new tier's cap; one that downgrades does not get a fresh day."""
    return f"daily:{subject}"


def bulk_key(key_public_id: str) -> str:
    """The key `services/api/bulk.py::_bulk_rate_limit` spends (pinned by a test)."""
    return f"bulk:{key_public_id}"


def is_search(request: Request) -> bool:
    """docs/23 §6 "Search (`q=`)": a `GET` carrying a non-empty `q`, read exactly as the list routes
    read it (`request.query_params.get("q")`, truthy), so the window counts the requests that run a
    search: an empty `q=` searches nothing and is not counted. Any path counts, because the check
    runs before routing; a `q` on a path that takes none is a `400 unknown_parameter` anyway."""
    return request.method == "GET" and bool(request.query_params.get("q"))


def daily_cap_for(tier: str, api_key: ApiKey | None = None) -> int | None:
    """The tier's daily cap, or the key's own `daily_quota` when one is set (docs/21 §3.17; set by
    an operator at issue, `POST /admin/v1/keys`). The override applies both ways, above or below
    the tier's figure; a negative value is read as 0."""
    override = getattr(api_key, "daily_quota", None) if api_key is not None else None
    if isinstance(override, int) and not isinstance(override, bool):
        return max(0, override)
    return DAILY_CAPS[tier]


def request_buckets(
    tier: str,
    subject: str,
    *,
    search: bool,
    daily_cap: int | None,
    read: bool = True,
) -> list[Bucket]:
    """The windows one request is counted in (module docstring)."""
    buckets: list[Bucket] = []
    if read:
        buckets.append(Bucket(read_key(tier, subject), TIER_LIMITS[tier], WINDOW_SECONDS, tier, "read"))
        search_limit = SEARCH_LIMITS[tier]
        if search and search_limit is not None:
            buckets.append(Bucket(search_key(tier, subject), search_limit, WINDOW_SECONDS, tier, "search"))
    if daily_cap is not None:
        buckets.append(Bucket(daily_key(subject), daily_cap, DAY_SECONDS, tier, "daily"))
    return buckets


_NOUNS = {"read": "requests", "search": "searches (`q=`)", "bulk": "bulk requests"}


@dataclass(frozen=True)
class Decision:
    """The outcome of metering one request: `problem` is the `429` to answer with, or `None`;
    `headers` are the `RateLimit-*` fields of the window closest to exhaustion (on a refusal, of
    the refusing window, plus `Retry-After`)."""

    results: list[tuple[Bucket, RateLimitResult]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    problem: ProblemError | None = None

    def headers_for(self, kind: str) -> dict[str, str] | None:
        for bucket, result in self.results:
            if bucket.kind == kind:
                return rate_limit_headers(bucket, result)
        return None


def meter(
    buckets: Sequence[Bucket], *, holder: str = "this caller", limiter: RateLimiter | None = None
) -> Decision:
    """Spend one unit of every bucket or of none, and say how to answer. A spent daily cap is
    `429 quota_exceeded` with `Retry-After` to 00:00 UTC (docs/23 §8), whatever else is spent; an
    hourly refusal is `429 rate_limited` with `Retry-After` to the latest refusing window's reset.
    `holder` names whose daily cap it is in the problem's `detail` ("this key", "this address")."""
    if not buckets:
        return Decision()
    results = list(zip(buckets, (limiter or default_limiter).consume(buckets), strict=True))
    refused = [(b, r) for b, r in results if not r.allowed]
    if not refused:
        bucket, result = min(results, key=lambda pair: (pair[1].remaining, -pair[1].reset_seconds))
        return Decision(results=results, headers=rate_limit_headers(bucket, result))
    daily = [(b, r) for b, r in refused if b.kind == "daily"]
    bucket, result = daily[0] if daily else max(refused, key=lambda pair: pair[1].reset_seconds)
    headers = {**rate_limit_headers(bucket, result), "Retry-After": str(result.reset_seconds)}
    if daily:
        problem = ProblemError(
            "quota_exceeded",
            "Daily quota exhausted",
            detail=f"{bucket.limit:,} requests per day on {holder}; resets at 00:00 UTC.",
            headers=headers,
        )
    else:
        problem = ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {bucket.limit} {_NOUNS.get(bucket.kind, 'requests')} in the current window.",
            headers=headers,
        )
    return Decision(results=results, headers=headers, problem=problem)


def meter_anonymous(request: Request) -> Decision:
    """An anonymous caller, or one whose credential resolved to nothing: the public tier's windows,
    keyed on the visitor's address (`services/api/client_ip.py`), never on Caddy's or web's."""
    return meter(
        request_buckets(
            "public", rate_limit_address(request), search=is_search(request), daily_cap=DAILY_CAPS["public"]
        ),
        holder="this address",
    )


#: Where a request's metering result waits for `services/api/app.py`'s middleware, which copies
#: it onto the response.
STATE_HEADERS = "rate_limit_headers"


def meter_request(request: Request, db: Annotated[Session, Depends(get_db)]) -> None:
    """App-wide dependency (`services/api/app.py` `FastAPI(dependencies=...)`) for every request
    that presents a credential; anonymous requests return at once (the middleware meters those,
    on every path, matched or not). It replaced `auth.meter_credentialed_request` (removed 2026-10-10)
    so that a request's read, search and daily windows are spent in one all-or-nothing step.

    - The site's own server-side calls (`X-Internal-Token`) are exempt from every window, as they
      were from the hourly one: one internal address carries every visitor, so counting it would
      spend the public daily cap for the whole site (docs/23 §6 "As built").
    - A credential that resolves to nothing is metered as anonymous, on the caller's address.
    - A valid one is metered on its tier (`auth.credential_tier`), keyed by API key, else user;
      `/v1/bulk/*` spends the daily cap only, with the key's bulk window as an unspent guard so a
      bulk request the route will refuse costs nothing.

    The `AuthContext` is resolved here and left on `request.state.auth_context`, where
    `get_auth_context` finds it (`auth.resolve_request_auth`'s per-request cache), so nothing is
    resolved twice and `auth._charge_unresolved_credential` never spends a second unit. The read
    window's own headers are left on `request.state.credential_rate_limit`, the cache
    `auth.charge_credential` (and so `pro.py::_rate_limit_headers`) returns instead of charging
    again; `/v1/me` reads its `read_per_hour` from them."""
    from services.api.auth import PUBLIC_CONTEXT, build_auth_context, credential_tier

    session_cookie = request.cookies.get("session")
    authorization = request.headers.get("authorization")
    if not (session_cookie or authorization):
        return
    ctx = getattr(request.state, "auth_context", None)
    if ctx is None:
        ctx = build_auth_context(db, session_cookie=session_cookie, authorization=authorization)
        request.state.auth_context = ctx
    if is_internal_request(request):
        return
    if ctx is PUBLIC_CONTEXT:
        decision = meter_anonymous(request)
    else:
        tier = credential_tier(ctx)
        subject = ctx.api_key.public_id if ctx.api_key else str(ctx.user.id) if ctx.user else "anon"
        daily_cap = daily_cap_for(tier, ctx.api_key)
        if request.url.path.startswith(OWN_BUCKET_PREFIXES):
            buckets = request_buckets(tier, subject, search=False, daily_cap=daily_cap, read=False)
            bulk_limit = plan_quota(ctx.entitlement).bulk_requests_per_hour
            if ctx.api_key is not None and bulk_limit is not None:
                guard = Bucket(
                    bulk_key(ctx.api_key.public_id),
                    bulk_limit,
                    WINDOW_SECONDS,
                    ctx.entitlement,
                    "bulk",
                    charge=False,
                )
                buckets.append(guard)
        else:
            buckets = request_buckets(tier, subject, search=is_search(request), daily_cap=daily_cap)
        decision = meter(buckets, holder="this key" if ctx.api_key else "this user")
        read_headers = decision.headers_for("read")
        if decision.problem is None and read_headers is not None:
            request.state.credential_rate_limit = read_headers
    if decision.problem is not None:
        raise decision.problem
    setattr(request.state, STATE_HEADERS, decision.headers)
