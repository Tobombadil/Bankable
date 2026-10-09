"""RFC 9457 problem details (api/openapi.yaml `Problem`; docs/04-standards.md API-4/E-17)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from services.api.common import API_HOST, current_request_id

ERROR_CODES = {
    "validation_error": 400,
    "unknown_parameter": 400,
    "invalid_cursor": 400,
    "unauthenticated": 401,
    "forbidden_tier": 403,
    "licence_gated": 403,
    "seat_limit": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "unpublished": 410,
    "conflict": 409,
    "paid_tiers_inactive": 403,
    "gate_unmet": 422,
    "rate_limited": 429,
    "quota_exceeded": 429,
    "sor_unavailable": 503,
    "unavailable": 503,
    "internal_error": 500,
}


class ProblemError(Exception):
    """Raised anywhere in a route to produce an RFC 9457 body (docs/23 §8)."""

    def __init__(
        self,
        code: str,
        title: str,
        *,
        detail: str | None = None,
        errors: list[dict[str, str]] | None = None,
        instance: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown problem code {code!r}")
        self.code = code
        self.status = ERROR_CODES[code]
        self.title = title
        self.detail = detail
        self.errors = errors
        self.instance = instance
        #: Sprint 2 and alerts' addition: `429 rate_limited`/`quota_exceeded` carry `Retry-After`
        #: (docs/23 §6, §8) — the one error family whose response needs a header beyond the
        #: standard set every response already gets from `services/api/app.py`'s middleware.
        self.headers = headers
        super().__init__(title)

    def to_body(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"{API_HOST}/errors/{self.code}",
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "request_id": current_request_id(),
            "instance": self.instance or request.url.path,
        }
        if self.detail:
            body["detail"] = self.detail
        if self.errors:
            body["errors"] = self.errors
        return body


async def problem_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Registered only for `ProblemError` (`app.add_exception_handler(ProblemError, ...)` in
    services/api/app.py); the `Exception` parameter type matches Starlette's handler signature —
    a narrower `ProblemError` annotation does not satisfy `add_exception_handler`'s type."""
    if not isinstance(exc, ProblemError):  # pragma: no cover - registration guarantees this
        raise exc
    return JSONResponse(
        status_code=exc.status,
        content=exc.to_body(request),
        media_type="application/problem+json",
        headers=exc.headers,
    )


#: Status -> `(code, title)` for an `HTTPException` the framework raises itself: an unknown path
#: (404) or a method the path does not take (405). Backend audit 2026-10-07, API-4: both answered
#: FastAPI's `{"detail": ...}` as plain JSON.
_FRAMEWORK_CODES: dict[int, tuple[str, str, str]] = {
    404: ("not_found", "Not found", "No such resource."),
    405: ("method_not_allowed", "Method not allowed", "This path does not take that method."),
}


async def framework_http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Registered for Starlette's `HTTPException` (`services/api/app.py`): the router's own 404 and
    405 as RFC 9457 bodies (docs/23 §8), `Allow` kept on a 405. Any other status is not raised by
    this service; it keeps the framework's answer rather than borrowing a code."""
    from fastapi.exception_handlers import http_exception_handler
    from starlette.exceptions import HTTPException

    if not isinstance(exc, HTTPException):  # pragma: no cover - registration guarantees this
        raise exc
    known = _FRAMEWORK_CODES.get(exc.status_code)
    if known is None:
        return await http_exception_handler(request, exc)  # type: ignore[return-value]
    code, title, detail = known
    headers = dict(exc.headers) if exc.headers else None
    problem = ProblemError(code, title, detail=detail, instance=request.url.path, headers=headers)
    return await problem_exception_handler(request, problem)


_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


async def request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Registered for FastAPI's `RequestValidationError`: a malformed body or a missing required
    parameter is `400 validation_error` with `errors[]` (docs/23 §8), not FastAPI's 422 list
    (backend audit 2026-10-07, API-4; open in the W4 decision of 2026-10-06)."""
    from fastapi.exceptions import RequestValidationError

    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration guarantees this
        raise exc
    errors = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ()) if part not in _LOCATIONS]
        errors.append({"field": ".".join(loc) or "body", "message": str(error.get("msg", "invalid"))})
    problem = ProblemError(
        "validation_error",
        "Invalid request",
        detail="The request body or a parameter is malformed.",
        errors=errors or [{"field": "body", "message": "invalid"}],
        instance=request.url.path,
    )
    return await problem_exception_handler(request, problem)


def not_found(instance: str, detail: str = "No such record is visible on this tier.") -> ProblemError:
    """docs/21 §8 item 3: a gated or absent record answers `404`, never `403` — existence must
    not leak."""
    return ProblemError("not_found", "Not found", detail=detail, instance=instance)


def unknown_parameter(name: str, instance: str) -> ProblemError:
    return ProblemError(
        "unknown_parameter",
        "Unknown query parameter",
        detail=f'Parameter "{name}" is not a filter on this resource; see /v1/meta/vocabularies.',
        errors=[{"field": name, "message": "unknown parameter"}],
        instance=instance,
    )


def validation_error(field: str, message: str, instance: str) -> ProblemError:
    return ProblemError(
        "validation_error",
        "Invalid request",
        detail=message,
        errors=[{"field": field, "message": message}],
        instance=instance,
    )


def invalid_cursor(instance: str) -> ProblemError:
    return ProblemError(
        "invalid_cursor",
        "Cursor expired or malformed",
        detail="Cursors are valid for 24 hours; restart from the first page.",
        instance=instance,
    )
