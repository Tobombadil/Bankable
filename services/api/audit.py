"""The audit log (docs/04-standards.md S-4; docs/21-data-model.md §3.10, §6.1).

Reuses the existing append-only `event` table rather than a new one: `event.subject_type` already
includes `user | account | api_key` and `event.event_type` already includes `key_issued |
key_revoked | admin_edit` (docs/21 §7.3, §3.10) — an admin write recorded here is
indistinguishable in shape from a pipeline event, which is the point (`docs/21` §6.1: "one code
path, one predicate"). `actor_type = 'user'` and a non-null `reason` are required, matching
US-905 AC3.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from typing import Any

from sqlalchemy.orm import Session

from services.db.models import Event, User


def record_audit_event(
    db: Session,
    *,
    subject_type: str,
    subject_id: _uuid.UUID,
    event_type: str,
    actor: User,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> Event:
    if not reason.strip():
        raise ValueError("audit events require a non-empty reason (docs/04 S-4, US-905 AC3)")
    now = dt.datetime.now(dt.UTC)
    event = Event(
        subject_type=subject_type,
        subject_id=subject_id,
        event_type=event_type,
        observed_at=now,
        published_at=now,
        public_at=now,
        before=before,
        after=after,
        changed_keys=sorted((after or {}).keys()),
        actor_type="user",
        actor_user_id=actor.id,
        reason=reason,
        idempotency_key=f"audit:{subject_type}:{subject_id}:{event_type}:{_uuid.uuid4()}",
    )
    db.add(event)
    db.flush()
    return event
