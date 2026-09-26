"""Admin read of the nightly M-11 visibility audit (`services/visibility_audit/run.py`; docs/10
§5 M-11; docs/04 R-4 "M-11 = 0 in the nightly audit"; docs/40 §4 row 11).

    GET /admin/v1/visibility-audits          latest first, cursor-paginated summaries
    GET /admin/v1/visibility-audits/latest   the most recent run in full (breach list, served checks)

Reads the `event` rows the job writes (`event_type = visibility_audit`, `subject_type = source`,
`actor_type = system`) — no table of its own. Not part of `GET /admin/v1/audit`, which is
`actor_type = user` by definition (docs/21 §6.1); the list here is by event type instead. Mounted
by `services/api/app.py` with one `include_router` call, the same way the other admin modules are.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_admin
from services.api.deps import get_db
from services.api.errors import not_found
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, int_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
)
from services.db.models import Event
from services.ids import public_id
from services.visibility_audit.run import EVENT_TYPE, SUBJECT_TYPE, summarise

router = APIRouter()


def _audit_events_stmt() -> Any:
    return select(Event).where(Event.event_type == EVENT_TYPE, Event.subject_type == SUBJECT_TYPE)


def serialize_visibility_audit(event: Event, *, full: bool) -> dict[str, Any]:
    """The persisted result, keyed by the event that holds it. `full=False` drops the row lists
    (`breaches`, `served.checks`, `gated_sources`) the way the job's own log line does."""
    payload: dict[str, Any] = dict(event.after or {})
    body = payload if full else summarise(payload)
    return {
        "id": public_id("evt", event.id),
        "seq": event.seq,
        "recorded_at": event.recorded_at.isoformat(),
        **body,
    }


@router.get("/admin/v1/visibility-audits")
def admin_list_visibility_audits(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor"})
    limit = clamp_limit(int_param(request, "limit"))
    rows, next_cursor, has_more = paginate(
        db,
        _audit_events_stmt(),
        sort_column=Event.seq,
        id_column=Event.id,
        ascending=False,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_visibility_audit(e, full=False) for e in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/visibility-audits/latest")
def admin_get_latest_visibility_audit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, set())
    event = db.scalar(_audit_events_stmt().order_by(Event.seq.desc()).limit(1))
    if event is None:
        raise not_found(request.url.path)
    return build_envelope(
        serialize_visibility_audit(event, full=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router", "serialize_visibility_audit"]
