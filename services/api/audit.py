"""The audit log (docs/04-standards.md S-4; docs/21-data-model.md §3.10, §6.1).

Reuses the existing append-only `event` table rather than a new one: `event.subject_type` already
includes `user | account | api_key` and `event.event_type` already includes `key_issued |
key_revoked | admin_edit` (docs/21 §7.3, §3.10) — an admin write recorded here is
indistinguishable in shape from a pipeline event, which is the point (`docs/21` §6.1: "one code
path, one predicate"). `actor_type = 'user'` and a non-null `reason` are required, matching
US-905 AC3.

**Identifiers never land in the log in the clear** (docs/50-audit-2026-09-18.md §3.1: "erasure
writes the erased email and name into the append-only audit log"). The log is append-only by
design (docs/04 DA-3), so an email or name written into `before`/`after` could never be erased
afterwards — every writer that needs to reference a personal identifier stores
`hash_identifier(value)` instead: SHA-256 over a server-side pepper (`AUDIT_HASH_PEPPER`) and the
normalised value. The pepper is what makes the hash useless to anyone holding a database dump but
not the environment; without it a plain SHA-256 of an email address is a dictionary lookup away
from the address. The same function keys the `suppression` table (`services/alerts/suppression.py`),
so "was this address erased?" is answerable without ever storing the address.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid as _uuid
from typing import Any

from sqlalchemy.orm import Session

from services.db.models import Event, User

#: The dev-only fallback pepper. Deliberately low entropy and obviously not a secret, so a hash
#: produced with it is recognisable as a development artefact; `audit_pepper` refuses to use it
#: when `ENVIRONMENT` (or its alias `APP_ENV`) says production. Tests set `AUDIT_HASH_PEPPER` to
#: their own obviously fake value rather than relying on this.
DEV_PEPPER = "dev-pepper-not-a-secret"
_PRODUCTION_ENVS = ("production", "prod")


def is_production() -> bool:
    """`ENVIRONMENT=production` (the variable Compose sets and `services/api/auth.py::session_secret`
    reads) is the one switch that turns "log and carry on" into "refuse": the pepper fallback here
    and the sender-identity check in `services/alerts/mail.py` both read it. `APP_ENV` is accepted
    as an alias so an operator's existing env file keeps working. Unset means development (this
    sandbox, CI, a laptop)."""
    for name in ("ENVIRONMENT", "APP_ENV"):
        if os.environ.get(name, "").strip().lower() in _PRODUCTION_ENVS:
            return True
    return False


def audit_pepper() -> str:
    pepper = os.environ.get("AUDIT_HASH_PEPPER", "")
    if pepper:
        return pepper
    if is_production():
        raise RuntimeError(
            "AUDIT_HASH_PEPPER must be set in production (CLAUDE.md: secrets from environment)"
        )
    return DEV_PEPPER


def normalise_identifier(value: str) -> str:
    """Case-folded and stripped, so `Alice@Example.com ` and `alice@example.com` hash alike — the
    property the suppression store needs (an unsubscribe recorded from a mail client's
    capitalised copy of the address must still suppress the lower-cased row)."""
    return value.strip().casefold()


def hash_identifier(value: str | None) -> str | None:
    """`sha256(pepper || 0x00 || normalised value)` as lowercase hex, or `None` for `None`/blank
    (a missing identifier is recorded as missing, never as the hash of the empty string, which
    would otherwise be one constant value across every anonymised row)."""
    if value is None or not value.strip():
        return None
    digest = hashlib.sha256()
    digest.update(audit_pepper().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(normalise_identifier(value).encode("utf-8"))
    return digest.hexdigest()


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
