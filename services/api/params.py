"""Filter-grammar helpers (docs/04-standards.md API-3): unknown query parameters are always a
`400 unknown_parameter`, never silently ignored."""

from __future__ import annotations

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
