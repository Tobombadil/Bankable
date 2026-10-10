"""The suppression store (docs/50-audit-2026-09-18.md §3.1 "no suppression store"; docs/13
§1/§4 checklists: an unsubscribe or erasure must stop every future send to that address).

Two writers and one reader, all keyed on `services.api.audit.hash_identifier` so the table never
holds an address:

- `suppress(db, email, reason)`: written on erasure (`services/api/admin_people.py`, at deletion-
  task completion) and on unsubscribe (`services/api/unsubscribe_routes.py`). Idempotent per
  `(hash, reason)`.
- `is_suppressed(db, email)`: read by `services/alerts/evaluate.py` before a digest is handed to
  the mail port, and by anything that would draft to an address. `services/social` drafts social
  posts, not messages to addresses, so it has no call site today; when an outreach drafting path
  lands it calls this before touching a contact.
- `lift_on_verified_sign_up(db, email)`: the one deletion. A new account whose address is proven by
  its verification link (`services/api/auth_routes.py::verify_email`) lifts that address's
  `erasure` row, so the person who came back gets the alerts they set up (owner decision,
  2026-10-10). Every other reason stays: an unsubscribe, a bounce or a complaint is the person's,
  or their mail server's, own instruction, which a new sign-up does not withdraw.

A suppressed digest is written as an `Alert` row with `status = "suppressed"` and a non-personal
`error`, so the operator can see that the cycle ran and why nothing went out.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import hash_identifier
from services.db.models import SUPPRESSION_REASONS, Suppression


def suppress(db: Session, email: str | None, reason: str) -> Suppression | None:
    """Records `(hash(email), reason)`; returns the existing row on a repeat, `None` for a blank
    address (nothing to suppress). `reason` must be one of `SUPPRESSION_REASONS`."""
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"reason must be one of {SUPPRESSION_REASONS}, got {reason!r}")
    digest = hash_identifier(email)
    if digest is None:
        return None
    existing = db.scalar(
        select(Suppression).where(Suppression.email_hash == digest, Suppression.reason == reason)
    )
    if existing is not None:
        return existing
    row = Suppression(email_hash=digest, reason=reason)
    db.add(row)
    db.flush()
    return row


def is_suppressed(db: Session, email: str | None) -> bool:
    """True when any reason is on file for the address. A blank address is treated as suppressed:
    there is nothing lawful to send to."""
    digest = hash_identifier(email)
    if digest is None:
        return True
    return db.scalar(select(Suppression.id).where(Suppression.email_hash == digest).limit(1)) is not None


#: Reasons a fresh, verified sign-up with the same address lifts (module docstring).
LIFTED_BY_VERIFIED_SIGN_UP = ("erasure",)


def lift_on_verified_sign_up(db: Session, email: str | None) -> list[str]:
    """Delete the `LIFTED_BY_VERIFIED_SIGN_UP` rows for `email` and return the reasons lifted (empty
    when there were none). Call it only once the address is proven: registration alone proves
    nothing, since anyone can type someone else's address."""
    digest = hash_identifier(email)
    if digest is None:
        return []
    rows = db.scalars(
        select(Suppression).where(
            Suppression.email_hash == digest, Suppression.reason.in_(LIFTED_BY_VERIFIED_SIGN_UP)
        )
    ).all()
    for row in rows:
        db.delete(row)
    db.flush()
    return sorted(row.reason for row in rows)


__all__ = ["LIFTED_BY_VERIFIED_SIGN_UP", "is_suppressed", "lift_on_verified_sign_up", "suppress"]
