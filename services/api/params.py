"""Filter-grammar helpers (docs/04-standards.md API-3): unknown query parameters are always a
`400 unknown_parameter`, never silently ignored."""

from __future__ import annotations

import re
from typing import overload

from fastapi import Request

from services.api.errors import unknown_parameter, validation_error


def check_allowed(request: Request, allowed: set[str]) -> None:
    for key in request.query_params:
        if key not in allowed:
            raise unknown_parameter(key, request.url.path)


@overload
def csv_param(value: str) -> list[str]: ...
@overload
def csv_param(value: None) -> None: ...
def csv_param(value: str | None) -> list[str] | None:
    """Split `a,b,c` into `["a", "b", "c"]` (docs/23 §7 "comma-separated for OR"). Overloaded so
    that the common call pattern `if v := qp.get(name): ...csv_param(v)...` type-checks without a
    spurious `list[str] | None` at every call site — `v` is already narrowed to `str` there."""
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


#: The list-endpoint query parameters every resource accepts, regardless of its own filters
#: (`services/api/app.py`'s `list_organizations`/`list_events`, `services/api/records.py`'s
#: `list_proposals`/`list_opportunities`, …).
LIST_COMMON = {"limit", "cursor", "include", "sort", "q"}


def int_param(request: Request, name: str) -> int | None:
    """An optional integer query parameter. A value that is not an integer is a 400 `validation_error`
    on every route, the behaviour `services/api/admin_records.py` alone had until 2026-09-26 (docs/42
    §8): the public, assets and admin-sources routes let `int()` raise instead."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise validation_error(name, f"{name} must be an integer", request.url.path) from exc


def page_limit(request: Request, *, default: int = 50, maximum: int = 200) -> int:
    """`limit` on a paged list (docs/23 §7, api/openapi.yaml `Limit`: minimum 1, maximum 200, or
    `BulkLimit`'s 1,000): absent is `default`; outside `1..maximum` is a `400 validation_error`.
    It used to be clamped silently, so `limit=500` answered 200 rows and said nothing (backend audit
    2026-10-07, API-5; docs/04 API-3: nothing a caller sends is silently changed)."""
    value = int_param(request, "limit")
    if value is None:
        return default
    if not 1 <= value <= maximum:
        raise validation_error("limit", f"limit must be between 1 and {maximum}", request.url.path)
    return value


def include_values(request: Request, allowed: frozenset[str]) -> frozenset[str]:
    """`include` as a set, each value one this resource implements (docs/23 §7); any other value is a
    `400 validation_error` naming the ones it takes, never ignored (backend audit 2026-10-07, API-5:
    `include=bogus` answered 200)."""
    values = frozenset(csv_param(request.query_params.get("include") or "") or [])
    unknown = sorted(values - allowed)
    if unknown:
        takes = ", ".join(f"`{v}`" for v in sorted(allowed)) or "nothing"
        raise validation_error(
            "include",
            f"include takes only {takes} on this resource (got {', '.join(unknown)})",
            request.url.path,
        )
    return values


def sort_spec(request: Request, allowlist: set[str], default: str) -> tuple[str, bool]:
    raw = request.query_params.get("sort", default)
    token = raw.split(",")[0]
    ascending = not token.startswith("-")
    field = token[1:] if not ascending else token
    if field not in allowlist:
        raise validation_error(
            "sort", f"sort field {field!r} is not allowlisted for this resource", request.url.path
        )
    return field, ascending


def wants_csv(request: Request) -> bool:
    """True when the client's most-preferred `Accept` media type is `text/csv` (docs/23 §1:
    "`Accept: text/csv` on list endpoints returns an export (Pro+)") -- an explicit ask, never a
    `*/*` or a browser's HTML-first list. The list routes branch on it before paginating and hand
    the request to `services/api/exports.py::csv_list_response`."""
    first = request.headers.get("accept", "").split(",")[0].strip().split(";")[0].strip().lower()
    return first == "text/csv"


#: The largest `event.seq` (a Postgres `bigint`); a larger number cannot be bound at all.
SEQ_MAX = 2**63 - 1


def since_seq(value: str, instance: str) -> int | None:
    """`since` as an event `seq` when it is ASCII digits only, `None` when it is something else (the
    caller then reads it as a date-time), and `400` when the number exceeds a `bigint`. Before,
    `str.isdigit()` accepted digits such as `²` that `int()` refuses, and an over-long number
    reached the driver; both were 500s (backend audit 2026-09-30 F9)."""
    if not re.fullmatch(r"[0-9]+", value):
        return None
    number = int(value)
    if number > SEQ_MAX:
        raise validation_error("since", f"since must be an event seq no larger than {SEQ_MAX}", instance)
    return number
