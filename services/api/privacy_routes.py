"""`/v1/privacy/requests` and `/admin/v1/privacy-requests`: the deletion/correction route for
people named in the records the platform indexes (docs/50-audit-2026-09-18.md §3.1: "the privacy
notice promises a filer-deletion route that does not exist"; US-910; CLAUDE.md "honour deletion
requests").

`web/templates/legal/privacy.html` §3 says a deletion request "from someone named in a filing we
index" is handled as a deletion task. Before this module the only route was a `mailto:`; the admin
`DELETE /admin/v1/users/{id}` flow covers *account holders* only. This is the minimum that makes
the notice true:

- `POST /v1/privacy/requests` — public, no session, no API key. Stores `{kind: erasure |
  correction, record_public_id, contact_email, message}` as a `privacy_request` row and answers
  `202` with the request's own public id. It emails nothing (CLAUDE.md: outbound communication to
  real people is drafted by agents and sent by a human), records no IP or user agent, and does not
  confirm whether `record_public_id` exists — a request about a record the caller cannot see is
  still a request, and answering "no such record" would leak existence (docs/21 §8 item 3).
  Per-IP rate-limited through the shared `default_limiter` at the same 20-per-window shape as the
  other public unauthenticated routes (`services/api/unsubscribe_routes.py`, `auth_routes.py`).
- `GET /admin/v1/privacy-requests` and `GET /admin/v1/privacy-requests/{id}` — operator reads,
  `status` filter, cursor-paginated oldest-open-first like the task queue.
- `PATCH /admin/v1/privacy-requests/{id}` — status/notes with a `reason`; closing (`done` or
  `rejected`) clears `contact_email`, the one piece of personal data the row holds, and writes an
  audit event carrying only its hash (`services/api/audit.hash_identifier`).

The coordinator mounts `router` on `services.api.app.app` (`app.include_router(privacy_router)`)
and merges the OpenAPI paths; `tests/test_api_privacy_requests.py` builds a standalone app so it
passes before either happens.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import hash_identifier, record_audit_event
from services.api.auth import AuthContext, iter_client_ip_prefix, require_admin
from services.api.common import iso, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import DEFAULT_LIMIT, clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.ratelimit import default_limiter
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
)
from services.db.models import PRIVACY_REQUEST_KINDS, PRIVACY_REQUEST_STATUSES, PrivacyRequest
from services.ids import public_id

router = APIRouter()

_PRIVACY_REQUEST_LIMIT = 20
_MAX_MESSAGE_CHARS = 4000
#: `<prefix>_<crockford>` (`services/ids.py`): the shape of every public id the site shows.
_PUBLIC_ID_RE = re.compile(r"^[a-z]{2,8}_[0-9A-HJKMNP-TV-Z]{10,32}$")
#: Deliberately loose (one `@`, no spaces, a dot in the domain): the address is only ever used by
#: a human replying, so a stricter grammar buys nothing and rejects real addresses.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _rate_limit(request: Request) -> None:
    key = f"privacy-request:{iter_client_ip_prefix(request) or 'unknown'}"
    result = default_limiter.check(key, limit=_PRIVACY_REQUEST_LIMIT)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


def serialize_privacy_request(row: PrivacyRequest, *, admin: bool) -> dict[str, Any]:
    """The public (`202`) shape carries no personal data back at all — the caller already knows
    their own address; the admin shape adds the contact and message the operator needs."""
    data: dict[str, Any] = {
        "public_id": row.public_id,
        "kind": row.kind,
        "record_public_id": row.record_public_id,
        "status": row.status,
        "created_at": iso(row.created_at),
        "completed_at": iso(row.completed_at),
    }
    if admin:
        data["contact_email"] = row.contact_email
        data["message"] = row.message
    return data


def _find_request(db: Session, request_id: str) -> PrivacyRequest | None:
    return db.scalar(select(PrivacyRequest).where(PrivacyRequest.public_id == request_id))


# ------------------------------------------------------------------------------------- public
@router.post("/v1/privacy/requests", status_code=202)
def create_privacy_request(
    body: dict[str, Any],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    _rate_limit(request)
    instance = request.url.path

    kind = body.get("kind")
    if kind not in PRIVACY_REQUEST_KINDS:
        raise validation_error("kind", f"kind must be one of {PRIVACY_REQUEST_KINDS}", instance)
    record_public_id = body.get("record_public_id")
    if not isinstance(record_public_id, str) or not _PUBLIC_ID_RE.match(record_public_id.strip()):
        raise validation_error("record_public_id", "record_public_id must be a platform public id", instance)
    contact_email = body.get("contact_email")
    if not isinstance(contact_email, str) or not _EMAIL_RE.match(contact_email.strip()):
        raise validation_error("contact_email", "contact_email must be an email address", instance)
    message = body.get("message")
    if message is not None and not isinstance(message, str):
        raise validation_error("message", "message must be a string", instance)
    if isinstance(message, str) and len(message) > _MAX_MESSAGE_CHARS:
        raise validation_error(
            "message", f"message must be at most {_MAX_MESSAGE_CHARS} characters", instance
        )

    row = PrivacyRequest(
        public_id="",
        kind=kind,
        record_public_id=record_public_id.strip(),
        contact_email=contact_email.strip(),
        message=(message or "").strip() or None,
        status="open",
    )
    db.add(row)
    db.flush()
    row.public_id = public_id("prq", row.id)
    db.flush()
    return build_envelope(
        serialize_privacy_request(row, admin=False),
        meta=build_meta(lag_days=0, tier="public"),
        licence_summary=build_licence_summary([]),
    )


# -------------------------------------------------------------------------------------- admin
@router.get("/admin/v1/privacy-requests")
def admin_list_privacy_requests(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "status", "kind"})
    stmt = select(PrivacyRequest)
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(PrivacyRequest.status.in_(csv_param(status_filter)))
    if kind_filter := request.query_params.get("kind"):
        stmt = stmt.where(PrivacyRequest.kind.in_(csv_param(kind_filter)))
    limit_raw = request.query_params.get("limit")
    limit = clamp_limit(int(limit_raw)) if limit_raw is not None else DEFAULT_LIMIT
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=PrivacyRequest.created_at,
        id_column=PrivacyRequest.public_id,
        ascending=True,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    return build_list_envelope(
        [serialize_privacy_request(r, admin=True) for r in rows],
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/privacy-requests/{request_id}")
def admin_get_privacy_request(
    request_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    row = _find_request(db, request_id)
    if row is None:
        raise not_found(request.url.path)
    return build_envelope(
        serialize_privacy_request(row, admin=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.patch("/admin/v1/privacy-requests/{request_id}")
def admin_update_privacy_request(
    request_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    instance = request.url.path
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise validation_error("reason", "reason is required", instance)
    row = _find_request(db, request_id)
    if row is None:
        raise not_found(instance)
    status_value = body.get("status")
    if status_value not in PRIVACY_REQUEST_STATUSES:
        raise validation_error("status", f"status must be one of {PRIVACY_REQUEST_STATUSES}", instance)
    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required")

    before = {"status": row.status, "contact_email_hash": hash_identifier(row.contact_email)}
    row.status = status_value
    closing = status_value in ("done", "rejected")
    if closing:
        row.completed_at = utcnow()
        row.contact_email = None  # the one personal field: cleared the moment it is no longer needed
    record_audit_event(
        db,
        subject_type="privacy_request",
        subject_id=row.id,
        event_type="admin_edit",
        actor=ctx.user,
        reason=reason,
        before=before,
        after={"status": status_value, "contact_email": "cleared" if closing else "retained"},
    )
    db.flush()
    return build_envelope(
        serialize_privacy_request(row, admin=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router", "serialize_privacy_request"]
