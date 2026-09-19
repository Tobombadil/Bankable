"""`/v1/alerts/unsubscribe`: the public, no-session endpoint the `unsubscribe_token` on every
`Alert` row (`services/alerts/evaluate.py`) exists for — US-908 AC1 "alert unsubscribe works" and
US-502 AC3 "unsubscribing stops that alert within one delivery cycle" (docs/10-prd-mvp.md).

Before this module, `services/db/models.py`'s `Alert.unsubscribe_token` was populated on every
digest alert and put in the email body, but nothing read it back (docs/40-launch-runbook.md §4 row
9). The token itself is the credential — no session, no CSRF check (this is meant to work from a
one-click mail-client link and from a plain `<form>` on `web/legal.py`'s confirmation page) — so
the only defenses are: a per-IP rate limit (shared `services.api.ratelimit.default_limiter`, same
20-per-window shape `services/api/auth_routes.py` uses for its own public, unauthenticated
endpoints) and a generic `404` on an unrecognised token that never says more than "not found"
(CLAUDE.md: store the minimum personal data; docs/21 §8 item 3's "existence must not leak"
principle, applied here to *tokens* rather than public ids).

**Decisions**
1. Removing `email` from `saved_search.channels` is the whole effect (the task's own wording:
   "remove `email` from that saved search's `channels`") — not a blanket pause-everything. A
   search with `channels = ["email", "rss"]` keeps its `rss` feed live and only stops the email
   digest; a search with only `["email"]` empties out and is paused per the task's "if `channels`
   becomes empty set status `paused`" rule, since an active saved search with zero channels would
   otherwise still burn a watermark-advancing evaluation pass for no visible effect
   (`services/alerts/evaluate.py` `run_alert_cycle` iterates every `status="active"` search).
2. Idempotent second use (task: "a second use of the same token → 200 idempotent"): if `email` is
   already absent from `channels` (either because this token was already used, or because the
   saved search never had the `email` channel in the first place — the token still resolves via
   the `Alert` row, which is unaffected either way), the route answers the same `200` body without
   writing a second `Event` — `Event.idempotency_key` is unique, and the task names this as
   "idempotent", not "replays the same audit trail twice".
3. `Event.subject_type = "saved_search"` (task: "subject_type 'saved_search' if allowed by
   constraints, else 'user'") — `services/db/models.py`'s `Event.__table_args__` constrains only
   `actor_type` (`subject_type_vocab` exists on `Document`, not `Event`), so nothing blocks it and
   the more specific subject is used, matching `services/billing/entitlement.py`'s
   `entitlement_changed` event pointing at the `account` it actually changed rather than a user.
4. Mirrors `services/billing/entitlement.py`'s non-pipeline-event convention exactly:
   `published_at = public_at = now` (a system action has no ingest lag to hide) even though this
   event is never surfaced on a public timeline (every public feed filters
   `subject_type in {proposal, opportunity}`, `services/alerts/evaluate.py`'s own
   `_new_events_for_search`) — the honest timestamp costs nothing and matches the existing
   admin-edit/entitlement-change precedent rather than inventing a third convention.
5. **Suppression** (docs/50-audit-2026-09-18.md §3.1): every successful unsubscribe also writes
   the recipient's hashed address to the `suppression` table (`services/alerts/suppression.py`,
   reason `unsubscribe`), so the address is refused by the sender even if another saved search
   on the same user still lists `email` — the CAN-SPAM/CASL reading of "unsubscribe" is the
   address, not one list.
6. **RFC 8058 one-click** (`List-Unsubscribe-Post: List-Unsubscribe=One-Click`, set by
   `services/alerts/mail.py`): a mail provider `POST`s the `https:` target from `List-Unsubscribe`
   with a form-encoded body `List-Unsubscribe=One-Click` and no JSON. The `POST` route therefore
   reads the token from the query string first and only then from a JSON body, and never
   requires either content type.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.alerts.suppression import suppress
from services.api.auth import iter_client_ip_prefix
from services.api.common import utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError
from services.api.ratelimit import default_limiter
from services.db.models import Alert, Event, SavedSearch

router = APIRouter()

_UNSUBSCRIBE_LIMIT = 20


def _rate_limit(request: Request) -> None:
    key = f"alert-unsubscribe:{iter_client_ip_prefix(request) or 'unknown'}"
    result = default_limiter.check(key, limit=_UNSUBSCRIBE_LIMIT)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


def _invalid_token(request: Request) -> ProblemError:
    """Generic on purpose (module docstring): the same body whether the token never existed, was
    mistyped, or belongs to an alert whose saved search has since been deleted -- never a hint
    that distinguishes those cases."""
    return ProblemError(
        "not_found",
        "Not found",
        detail="This unsubscribe link is not recognized.",
        instance=request.url.path,
    )


def _unsubscribe(db: Session, request: Request, token: str) -> dict[str, Any]:
    _rate_limit(request)

    token = (token or "").strip()
    if not token:
        raise _invalid_token(request)

    alert = db.scalar(select(Alert).where(Alert.unsubscribe_token == token))
    if alert is None:
        raise _invalid_token(request)

    search = db.get(SavedSearch, alert.saved_search_id)
    if search is None:  # pragma: no cover - every alert is written with a real saved_search_id
        raise _invalid_token(request)

    # Decision 5: the address itself is suppressed (hash only), idempotently, before the
    # channel edit — so even a repeat call on an already-edited search leaves the store correct.
    suppress(db, alert.recipient, "unsubscribe")

    before_channels = list(search.channels)
    if "email" in before_channels:
        before_status = search.status
        after_channels = [c for c in before_channels if c != "email"]
        after_status = "paused" if not after_channels else before_status

        search.channels = after_channels
        search.status = after_status
        db.flush()

        now = utcnow()
        event = Event(
            subject_type="saved_search",
            subject_id=search.id,
            event_type="alert_unsubscribed",
            observed_at=now,
            published_at=now,
            public_at=now,
            before={"channels": before_channels, "status": before_status},
            after={"channels": after_channels, "status": after_status},
            changed_keys=["channels"] if after_status == before_status else ["channels", "status"],
            actor_type="system",
            reason="unsubscribe via token",
            idempotency_key=f"alert_unsubscribed:{alert.id}:{_uuid.uuid4()}",
        )
        db.add(event)
        db.flush()

    return {"unsubscribed": True, "saved_search_name": search.name}


@router.post("/v1/alerts/unsubscribe")
async def unsubscribe_alert(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    """Decision 6: `?token=` (the RFC 8058 one-click `POST`, form-encoded body ignored) or a JSON
    body `{"token": ...}` (the web confirmation page, `web/legal.py`)."""
    if not token:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            token = str(body.get("token") or "")
    return _unsubscribe(db, request, token)


@router.get("/v1/alerts/unsubscribe")
def unsubscribe_alert_one_click(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    return _unsubscribe(db, request, token)


__all__ = ["router"]
