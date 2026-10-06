"""Who may hold saved searches and email alerts, and on what terms (owner decision 2026-09-30,
`docs/26-platform-posture.md` §7).

Owner, 2026-09-30: stay noncommercial; registered users get free email alerts with a cap and a
web page to manage them; paid tiers stay inactive; the posture is decided again at day 30 from
alert activation and customer conversations.

**One posture constant, not a second switch.** Whether free alerts are on is read from
`services.billing.router.PAID_TIERS_ACTIVE` -- the constant `docs/26` §3 precondition (i) already
computes once at import from `PLATFORM_POSTURE` -- at call time, as a module attribute. Free
alerts are on exactly when paid tiers are off, so no deployment can have one without the other,
and a test that flips that constant flips both (`services/billing/test_router.py` does the same
for checkout).

**The rule.** `alert_plan_for(ctx)`:

* `pro`, `api` and `admin` entitlements hold the paid plan, under either posture, unchanged:
  `SAVED_SEARCH_QUOTA` saved searches, every delivery mode, every channel.
* A `public` account holds the free plan only while free alerts are on, and only through a
  signed-in session (an API key is the API tier's shape, not this one): `FREE_ALERT_CAP` saved
  searches, a daily or weekly email digest. Immediate delivery (US-502 AC1 "within 15 minutes")
  and the private RSS feed stay with the paid plan, which is not on sale; webhooks were never a
  saved-search channel (`services/alerts/evaluate.py`).
* Anything else holds no plan, and the routes answer exactly what `require_entitlement("pro")`
  always answered (`401 unauthenticated`, `403 forbidden_tier`).

Creating a free alert also needs a verified email address (`email_verified_at`): the alert
writes to that address every day or week, and an unverified one may belong to someone else.
Reading, pausing and deleting do not, so an account can always stop what it started.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from services.api.auth import AuthContext, get_auth_context, require_entitlement
from services.billing import router as billing_router
from services.db.models import SAVED_SEARCH_DELIVERY_MODES

#: US-501 AC1's paid limit ("a user may hold up to 25 saved searches (limit configurable)").
SAVED_SEARCH_QUOTA = 25
#: The free plan's cap when `FREE_ALERT_CAP` is unset, empty or not a non-negative integer.
FREE_ALERT_CAP_DEFAULT = 10
FREE_ALERT_CAP_ENV = "FREE_ALERT_CAP"
#: The digests the alert cycle delivers on a cadence (`services/alerts/evaluate.py::is_due`).
FREE_ALERT_DELIVERY_MODES: tuple[str, ...] = ("daily", "weekly")
FREE_ALERT_CHANNELS: tuple[str, ...] = ("email",)
PAID_ALERT_CHANNELS: tuple[str, ...] = ("email", "rss")

#: Entitlements that carry the paid plan (`services/api/auth.py::_ENTITLEMENT_ORDER` from `pro` up).
_PAID_ENTITLEMENTS = frozenset({"pro", "api", "admin"})


def parse_free_alert_cap(raw: str | None) -> int:
    """`FREE_ALERT_CAP` as a non-negative integer; anything else is the default, never an error at
    import (a typo must not take the API down, and must not remove the cap either)."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return FREE_ALERT_CAP_DEFAULT
    return value if value >= 0 else FREE_ALERT_CAP_DEFAULT


#: Read once at import, like the posture itself (`services/posture.py`): one value per process.
FREE_ALERT_CAP = parse_free_alert_cap(os.environ.get(FREE_ALERT_CAP_ENV))


def free_alerts_active() -> bool:
    """True exactly while paid tiers are inactive, i.e. under the `noncommercial` posture."""
    return not billing_router.PAID_TIERS_ACTIVE


@dataclass(frozen=True)
class AlertPlan:
    """What one caller may hold. `basis` is `paid` or `free`."""

    basis: str
    quota: int
    delivery_modes: tuple[str, ...]
    channels: tuple[str, ...]
    #: Creating a saved search needs `user.email_verified_at` (the free plan only).
    requires_verified_email: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "quota": self.quota,
            "delivery_modes": list(self.delivery_modes),
            "channels": list(self.channels),
            "requires_verified_email": self.requires_verified_email,
        }


def paid_plan() -> AlertPlan:
    return AlertPlan(
        basis="paid",
        quota=SAVED_SEARCH_QUOTA,
        delivery_modes=tuple(m for m in SAVED_SEARCH_DELIVERY_MODES if m != "none"),
        channels=PAID_ALERT_CHANNELS,
        requires_verified_email=False,
    )


def free_plan() -> AlertPlan:
    return AlertPlan(
        basis="free",
        quota=FREE_ALERT_CAP,
        delivery_modes=FREE_ALERT_DELIVERY_MODES,
        channels=FREE_ALERT_CHANNELS,
        requires_verified_email=True,
    )


def alert_plan_for(ctx: AuthContext) -> AlertPlan | None:
    """The plan this credential holds, or `None` (module docstring, "The rule")."""
    if not ctx.is_authenticated:
        return None
    if ctx.entitlement in _PAID_ENTITLEMENTS:
        return paid_plan()
    if ctx.entitlement == "public" and ctx.user is not None and free_alerts_active():
        return free_plan()
    return None


_require_pro = require_entitlement("pro")


def require_alert_access() -> Callable[[AuthContext], AuthContext]:
    """The dependency on every saved-search and alert-history route. A caller with a plan passes;
    anyone else gets `require_entitlement("pro")`'s own answer, byte for byte, so the commercial
    posture behaves exactly as it did before this module existed."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if alert_plan_for(ctx) is not None:
            return ctx
        return _require_pro(ctx)

    return _dep


def free_alerts_summary() -> dict[str, object]:
    """`GET /v1/health`'s `free_alerts`: what the public site says about free alerts, from the
    same constant the routes enforce."""
    return {
        "active": free_alerts_active(),
        "cap": FREE_ALERT_CAP,
        "delivery_modes": list(FREE_ALERT_DELIVERY_MODES),
    }


__all__ = [
    "FREE_ALERT_CAP",
    "FREE_ALERT_CAP_DEFAULT",
    "FREE_ALERT_CHANNELS",
    "FREE_ALERT_DELIVERY_MODES",
    "SAVED_SEARCH_QUOTA",
    "AlertPlan",
    "alert_plan_for",
    "free_alerts_active",
    "free_alerts_summary",
    "free_plan",
    "paid_plan",
    "parse_free_alert_cap",
    "require_alert_access",
]
