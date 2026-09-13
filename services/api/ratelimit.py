"""Per-tier token-bucket rate limiting (docs/23-api-spec-outline.md §6; docs/04-standards.md
API-6). `services/README.md` (Sprint 2) recorded rate-limit headers as static placeholders; this
sprint replaces them with a real in-memory limiter behind an interface a Redis implementation can
drop into later without touching call sites (`docs/20-architecture.md` §7: "Redis is not needed
until sustained request rates exceed roughly 50 requests per second across the fleet").
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

#: docs/23 §6 table, keyed by the resolved tier (this module does not distinguish the search/bulk
#: sub-windows that table also lists — a documented narrowing, not a silent drop: every request on
#: a tier shares that tier's single `read` bucket this sprint, recorded in services/README.md).
TIER_LIMITS: dict[str, int] = {
    "public": 60,
    "free_account": 300,
    "pro": 600,
    "api": 6000,
    "admin": 1200,
}
WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class RateLimiter(Protocol):
    """Interface a Redis-backed limiter implements identically (`docs/20` §7's stated upgrade
    path) — `check` is the only method call sites use."""

    def check(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """Fixed-window counter per `(key, window_start)`, process-local. Good enough for a single API
    process (docs/20 §7's stated threshold for needing Redis is far above what this sprint's tests
    or a solo-operator deployment produce) and exercises the exact interface a shared-store
    implementation would.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def check(self, key: str, *, limit: int, window_seconds: int = WINDOW_SECONDS) -> RateLimitResult:
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds
        bucket_key = (key, window_start)
        with self._lock:
            count = self._counts.get(bucket_key, 0) + 1
            self._counts[bucket_key] = count
            # Bound memory: drop windows other than the current one for this key.
            for existing in [k for k in self._counts if k[0] == key and k[1] != window_start]:
                del self._counts[existing]
        remaining = max(0, limit - count)
        reset_seconds = int(window_start + window_seconds - now)
        return RateLimitResult(
            allowed=count <= limit, limit=limit, remaining=remaining, reset_seconds=max(0, reset_seconds)
        )

    def reset(self) -> None:
        """Test helper — not part of the `RateLimiter` protocol."""
        with self._lock:
            self._counts.clear()


#: One process-wide limiter, mirroring `services/api/deps.py`'s single `_sessionmaker` pattern.
#: `services/api/app.py`'s middleware and `services/api/pro.py`'s routes share it so a caller's
#: budget is one budget regardless of which endpoint spends it.
default_limiter = InMemoryRateLimiter()


def policy_header(tier: str, result: RateLimitResult) -> str:
    return f'{result.limit};w={WINDOW_SECONDS};policy="{tier}-read"'
