"""Identifier-free interaction counters for the built-infrastructure context layer
(docs/00-PLAN.md decision 2026-09-14: "the context layer ships with a measurement, not an
assumption"; docs/21 §3.21; `services/db/models.py::UI_EVENT_NAMES`/`UiEvent`).

`POST /v1/ui-events` never requires auth and never logs its body: the whole privacy design is
that a stored row cannot identify anyone (no user id, session id, IP, user agent or referrer is
ever read off the request, let alone stored), so the caller's IP is used *only* as the key for the
per-IP rate limiter below — never stored, never logged, discarded the instant the check returns.
`GET /admin/v1/ui-events/summary` is the operator-only read side, weekly counts per event name.

Mounted onto `services.api.app.app` with one `include_router` call, per this task's "extend,
don't rewrite" instruction for that file.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_admin
from services.api.common import ensure_aware, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, validation_error
from services.api.params import check_allowed
from services.api.serialize import build_envelope, build_licence_summary, build_meta
from services.db.models import UI_EVENT_NAMES, UiEvent

router = APIRouter()

#: docs/00-PLAN.md 2026-09-14: "ships with an identifier-free measurement" — a low ceiling on an
#: unauthenticated, no-key endpoint (docs/23 §6's public-tier row is 60/*hour*; this is 60/*minute*,
#: applied with the same process-local limiter and interface, keyed by IP for rate-limiting purposes
#: only — see the module docstring for why the IP itself is never stored).
UI_EVENT_RATE_LIMIT_PER_MINUTE = 60

#: Allowed `props` keys per event name and each value's expected type/max length
#: (`None` length = no cap beyond the generic identifier-like check below). Matches
#: `services/db/models.py::UI_EVENT_NAMES`'s inline comment exactly.
_ALLOWED_PROPS: dict[str, dict[str, tuple[type, int | None]]] = {
    "map.layer_toggled": {"layer": (str, 32), "on": (bool, None)},
    "map.region_jumped": {"region": (str, 16)},
    "map.basemap_failed": {},
    "auth.registered": {"layers": (str, 64)},
    "alert.created": {},
}
_MAX_PROPS_KEYS = 8

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_TOKEN_LIKE_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def _looks_identifying(value: str) -> bool:
    """Email, IPv4/IPv6, UUID, or a >=20-char run of URL-safe token characters — the brief's
    exact identifier shapes. `ipaddress.ip_address` is used for IPs rather than a hand-rolled
    regex so both address families are covered by one, correct check."""
    if _EMAIL_RE.match(value):
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if _UUID_RE.match(value):
        return True
    return bool(_TOKEN_LIKE_RE.match(value))


def _enforce_rate_limit(request: Request) -> None:
    from services.api.ratelimit import default_limiter

    client_ip = request.client.host if request.client else "unknown"
    result = default_limiter.check(
        f"ui-events:{client_ip}", limit=UI_EVENT_RATE_LIMIT_PER_MINUTE, window_seconds=60
    )
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


def _validate_event(name: Any, props: Any, path: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(name, str) or name not in UI_EVENT_NAMES:
        raise validation_error("name", "must be one of the allowlisted UI event names", path)
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise validation_error("props", "must be an object", path)
    if len(props) > _MAX_PROPS_KEYS:
        raise validation_error("props", "too many properties", path)
    allowed = _ALLOWED_PROPS[name]
    for key, value in props.items():
        spec = allowed.get(key)
        # Deliberately generic messages throughout (module docstring: never log the body) — no
        # branch below echoes the offending key or value back to the caller.
        if spec is None:
            raise validation_error("props", "property not allowed for this event", path)
        expected_type, max_len = spec
        if expected_type is bool:
            if not isinstance(value, bool):
                raise validation_error("props", "invalid property value", path)
        else:
            if not isinstance(value, str) or (max_len is not None and len(value) > max_len):
                raise validation_error("props", "invalid property value", path)
            if _looks_identifying(value):
                raise validation_error("props", "invalid property value", path)
    return name, props


@router.post("/v1/ui-events", status_code=202)
def create_ui_event(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _enforce_rate_limit(request)
    name, props = _validate_event(body.get("name"), body.get("props"), request.url.path)
    db.add(UiEvent(name=name, props=props))
    return Response(status_code=202)


@router.get("/admin/v1/ui-events/summary")
def admin_ui_events_summary(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"weeks"})
    weeks_param = request.query_params.get("weeks", "8")
    try:
        weeks = int(weeks_param)
    except ValueError as exc:
        raise validation_error("weeks", "must be an integer", request.url.path) from exc
    if weeks < 1 or weeks > 52:
        raise validation_error("weeks", "must be between 1 and 52", request.url.path)

    cutoff = utcnow() - dt.timedelta(weeks=weeks)
    rows = list(db.scalars(select(UiEvent).where(UiEvent.occurred_at >= cutoff)).all())

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        occurred = ensure_aware(row.occurred_at)
        iso_year, iso_week, _ = occurred.isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"
        key = (week_label, row.name)
        bucket = buckets.setdefault(
            key, {"week": week_label, "name": row.name, "count": 0, "count_on": 0, "count_off": 0}
        )
        bucket["count"] += 1
        if row.name == "map.layer_toggled":
            on_value = (row.props or {}).get("on")
            if on_value is True:
                bucket["count_on"] += 1
            elif on_value is False:
                bucket["count_off"] += 1

    data = []
    for key in sorted(buckets):
        b = buckets[key]
        entry: dict[str, Any] = {"week": b["week"], "name": b["name"], "count": b["count"]}
        if b["name"] == "map.layer_toggled":
            entry["count_on"] = b["count_on"]
            entry["count_off"] = b["count_off"]
        data.append(entry)

    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )
