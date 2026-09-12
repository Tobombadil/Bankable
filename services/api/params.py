"""Filter-grammar helpers (docs/04-standards.md API-3): unknown query parameters are always a
`400 unknown_parameter`, never silently ignored."""

from __future__ import annotations

from typing import overload

from fastapi import Request

from services.api.errors import unknown_parameter


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
