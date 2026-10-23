"""Sharing a project with named collaborators (docs/19 §2.6). Each rule is pinned by
`test_invitations.py`:

1. **Tied to one address.** An invitation names one email address and a role (`editor` or
   `viewer`). Only a signed-in user whose verified address equals it can accept it. "Equals" is
   the account system's own identity: trimmed and lower-cased (`canonical_address`), because
   `services.api.auth.register_user` stores `email.lower()` and sends verification mail to that
   exact string. It is never Unicode case folding (`str.casefold()`, which the suppression store's
   `hash_identifier` uses): folding maps `ß` to `ss` and `ς` to `σ`, so an account verified on
   another domain (`kate@straße.de`, a separate IDN domain from `kate@strasse.de`) or at another
   mailbox would match an invitation sent to someone else, and a leaked link would work for it.
2. **A real token, stored as a hash.** `secrets.token_urlsafe(32)`, shown once in the link, stored
   only as its SHA-256. Lookup is by hash, so there is no timing oracle.
3. **Single use.** Acceptance locks the row, checks it is unused, unrevoked and unexpired, adds the
   membership, stamps `accepted_at` and clears the stored address in one transaction. Revocation
   locks the row too, so a revoke that races an acceptance finds it accepted and changes nothing.
4. **Expires** after 7 days. The owner can revoke it at any time. It also stops working when its
   sender no longer owns the project: a transfer revokes the outgoing owner's open invitations, and
   acceptance checks the sender still holds the owner role.
5. **No listing, no oracle.** Only the owner lists a project's invitations. A wrong, expired, used
   or revoked token, or a wrong or unverified address, all get the same problem, and once the
   token matches every remaining check runs whichever one fails.
6. **Limits:** 10 pending invitations per project; 20 new invitations per user per rolling day.
   Both counts are taken under row locks on the inviter and the project (`authz.lock_user`,
   `authz.lock_project`), so concurrent requests cannot pass one count together.
7. **Delivery** is a link the owner copies and sends themselves (owner, 2026-10-22). Nothing here
   sends email; no automated channel for invitations is enabled (`CLAUDE.md` outbound rule).

Creating an invitation never looks up whether the address has an account, so it reveals nothing
about who uses Inframation. Inviting an address that already has an open invitation replaces it
(the old link stops working), because the token cannot be shown twice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import math
import re
import secrets
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from services.api.audit import audit_pepper
from services.api.auth import AuthContext
from services.api.common import WEB_HOST
from services.api.errors import ProblemError
from services.db.models import User
from services.projects.authz import (
    ProjectAccess,
    lock_project,
    lock_user,
    project_not_found,
    require_action,
    workspace_user,
)
from services.projects.models import (
    INVITATION_ROLES,
    INVITATION_TTL,
    MAX_INVITATIONS_PER_USER_PER_DAY,
    MAX_PENDING_INVITATIONS_PER_PROJECT,
    PrivateProject,
    PrivateProjectInvitation,
    PrivateProjectMember,
    add_activity,
    as_utc,
    has_control_characters,
    utcnow,
)

INVALID_TITLE: Final = "This invitation is not valid for this account"
INVALID_DETAIL: Final = (
    "Ask the person who invited you for a new link, and sign in with the address it was sent to."
)
#: `secrets.token_urlsafe(32)` is 43 characters; anything far longer is refused before hashing.
TOKEN_MAX_CHARS: Final = 128
DAILY_WINDOW: Final = dt.timedelta(days=1)
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s.]{2,63}")
EMAIL_MAX_CHARS: Final = 254


def invalid_invitation() -> ProblemError:
    """The one answer for every way an invitation can fail to check out (rule 5)."""
    return ProblemError("not_found", INVALID_TITLE, detail=INVALID_DETAIL)


def canonical_address(value: str) -> str:
    """An address as the account system identifies it: trimmed and lower-cased (rule 1). Never
    case-folded."""
    return value.strip().lower()


def address_hash(value: str | None) -> str | None:
    """`sha256(pepper || 0x00 || canonical_address(value))` as hex, or `None` for a blank value: the
    construction and pepper of `services.api.audit.hash_identifier`, with the account system's
    normalisation in place of its case folding (rule 1). Keys open invitations per project and
    finds a user's invitations at erasure."""
    if value is None or not value.strip():
        return None
    digest = hashlib.sha256()
    digest.update(audit_pepper().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_address(value).encode("utf-8"))
    return digest.hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitation_link(token: str) -> str:
    """The private link the owner copies (docs/19 §6.2: `/my/invitations/accept?t=…`). The proxy
    log drops the `t` parameter on that path (`infra/compose/Caddyfile`)."""
    return f"{WEB_HOST}/my/invitations/accept?t={quote(token, safe='')}"


@dataclass(frozen=True)
class IssuedInvitation:
    """A new invitation and its token. The token exists only here, once: it is never stored,
    logged or repeated, and it is kept out of `repr` so a traceback cannot print it."""

    invitation: PrivateProjectInvitation
    token: str = field(repr=False)

    @property
    def link(self) -> str:
        return invitation_link(self.token)


@dataclass(frozen=True)
class InvitationPreview:
    """What the accept page shows once the token and the address both check out: the project, the
    role and expiry (on `invitation`), and the sender's name, never their address."""

    invitation: PrivateProjectInvitation
    project: PrivateProject
    inviter_name: str | None


def _is_open(invitation: PrivateProjectInvitation, now: dt.datetime) -> bool:
    return (
        invitation.accepted_at is None
        and invitation.revoked_at is None
        and invitation.email is not None
        and as_utc(invitation.expires_at) > now
    )


def _open_filter(now: dt.datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        PrivateProjectInvitation.accepted_at.is_(None),
        PrivateProjectInvitation.revoked_at.is_(None),
        PrivateProjectInvitation.email.is_not(None),
        PrivateProjectInvitation.expires_at > now,
    )


def _bulk_update(db: Session, statement: Any) -> int:
    """One UPDATE over many rows. Pending changes are flushed first and every loaded row is expired
    after, so no object in the session keeps a stale address or state."""
    db.flush()
    result = db.execute(statement.execution_options(synchronize_session=False))
    db.expire_all()
    return int(getattr(result, "rowcount", 0) or 0)


def expire_invitations(
    db: Session, *, now: dt.datetime | None = None, project_id: _uuid.UUID | None = None
) -> int:
    """Clears the address of every invitation past its expiry (docs/19 §5.4: the address goes on
    accept, revoke or expiry). The retention job calls this; `create_invitation` does it for the
    project in hand. Returns how many rows changed."""
    now = now or utcnow()
    statement = (
        update(PrivateProjectInvitation)
        .where(
            PrivateProjectInvitation.accepted_at.is_(None),
            PrivateProjectInvitation.revoked_at.is_(None),
            PrivateProjectInvitation.email.is_not(None),
            PrivateProjectInvitation.expires_at <= now,
        )
        .values(email=None)
    )
    if project_id is not None:
        statement = statement.where(PrivateProjectInvitation.project_id == project_id)
    return _bulk_update(db, statement)


def _invalid_email(message: str) -> ProblemError:
    return ProblemError(
        "validation_error",
        "Invalid request",
        detail=f"email {message}",
        errors=[{"field": "email", "message": message}],
    )


def _clean_email(email: object) -> str:
    if not isinstance(email, str):
        raise _invalid_email("is required")
    cleaned = email.strip()
    if has_control_characters(cleaned):
        raise _invalid_email("contains a control character")
    if len(cleaned) > EMAIL_MAX_CHARS or not _EMAIL.fullmatch(cleaned):
        raise _invalid_email("is not an email address")
    return cleaned


def create_invitation(
    db: Session, access: ProjectAccess, *, email: str, role: str, now: dt.datetime | None = None
) -> IssuedInvitation:
    """Owner invites one address as `editor` or `viewer` and gets the link to send themselves."""
    require_action(access, "invite")
    address = _clean_email(email)
    if role not in INVITATION_ROLES:
        raise ProblemError(
            "validation_error",
            "Invalid request",
            detail="role is editor or viewer",
            errors=[{"field": "role", "message": "role is editor or viewer"}],
        )
    email_hash = address_hash(address)
    if email_hash is None:  # pragma: no cover - a validated address is never blank
        raise invalid_invitation()
    now = now or utcnow()
    project = access.project
    # Rule 6: the counts below are read under these locks, held to the end of the transaction.
    lock_user(db, access.user)
    lock_project(db, project)
    sent_today = db.execute(
        select(PrivateProjectInvitation.created_at).where(
            PrivateProjectInvitation.invited_by_user_id == access.user.id,
            PrivateProjectInvitation.created_at > now - DAILY_WINDOW,
        )
    ).scalars()
    window = sorted(as_utc(created) for created in sent_today)
    if len(window) >= MAX_INVITATIONS_PER_USER_PER_DAY:
        retry = window[len(window) - MAX_INVITATIONS_PER_USER_PER_DAY] + DAILY_WINDOW - now
        raise ProblemError(
            "quota_exceeded",
            "Invitation limit reached",
            detail=f"At most {MAX_INVITATIONS_PER_USER_PER_DAY} new invitations a day.",
            headers={"Retry-After": str(max(1, math.ceil(retry.total_seconds())))},
        )
    expire_invitations(db, now=now, project_id=project.id)
    replaced = db.execute(
        select(PrivateProjectInvitation)
        .where(
            PrivateProjectInvitation.project_id == project.id,
            PrivateProjectInvitation.email_hash == email_hash,
            *_open_filter(now),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    pending = db.scalar(
        select(func.count())
        .select_from(PrivateProjectInvitation)
        .where(PrivateProjectInvitation.project_id == project.id, *_open_filter(now))
    )
    # Every check before any write: a refused invitation leaves the replaced one standing.
    if (pending or 0) - int(replaced is not None) >= MAX_PENDING_INVITATIONS_PER_PROJECT:
        raise ProblemError(
            "conflict",
            "Too many pending invitations",
            detail=f"At most {MAX_PENDING_INVITATIONS_PER_PROJECT} pending invitations; revoke one first.",
        )
    if replaced is not None:
        _close(replaced, now=now)
        add_activity(
            db,
            project_id=project.id,
            actor_user_id=access.user.id,
            action="invitation_revoked",
            detail={"invitation_id": replaced.public_id, "rotated": True},
            now=now,
        )
        db.flush()
    token = secrets.token_urlsafe(32)
    invitation = PrivateProjectInvitation(
        project_id=project.id,
        email=address,
        email_hash=email_hash,
        token_hash=hash_token(token),
        role=role,
        invited_by_user_id=access.user.id,
        created_at=now,
        expires_at=now + INVITATION_TTL,
    )
    db.add(invitation)
    db.flush()
    add_activity(
        db,
        project_id=project.id,
        actor_user_id=access.user.id,
        action="member_invited",
        detail={"invitation_id": invitation.public_id, "role": role},
        now=now,
    )
    db.flush()
    return IssuedInvitation(invitation=invitation, token=token)


def list_invitations(
    db: Session, access: ProjectAccess, *, now: dt.datetime | None = None
) -> list[PrivateProjectInvitation]:
    """The project's pending invitations, newest first. Owner only (rule 5): an editor or viewer
    gets `403 forbidden_role`, an outsider never reaches here (`authorize` gave them a 404). The
    rows carry `token_hash` and `email_hash`; a route serialises `public_id`, `email`, `role`,
    `created_at` and `expires_at` only."""
    require_action(access, "invite")
    now = now or utcnow()
    rows = db.execute(
        select(PrivateProjectInvitation)
        .where(PrivateProjectInvitation.project_id == access.project.id, *_open_filter(now))
        .order_by(PrivateProjectInvitation.created_at.desc(), PrivateProjectInvitation.id.desc())
    ).scalars()
    return list(rows)


def _close(invitation: PrivateProjectInvitation, *, now: dt.datetime) -> None:
    invitation.revoked_at = now
    invitation.email = None


def revoke_invitation(
    db: Session, access: ProjectAccess, invitation_public_id: str, *, now: dt.datetime | None = None
) -> None:
    """Owner revokes a pending invitation. Unknown, used, revoked or someone else's: the same 404.
    The row is locked and read fresh, so a revoke racing an acceptance sees it accepted (rule 3)."""
    require_action(access, "invite")
    now = now or utcnow()
    invitation = db.execute(
        select(PrivateProjectInvitation)
        .where(
            PrivateProjectInvitation.public_id == invitation_public_id,
            PrivateProjectInvitation.project_id == access.project.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invitation is None or invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise project_not_found()
    _close(invitation, now=now)
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="invitation_revoked",
        detail={"invitation_id": invitation.public_id},
        now=now,
    )
    db.flush()


def revoke_project_invitations(db: Session, access: ProjectAccess, *, now: dt.datetime | None = None) -> int:
    """Revokes every open invitation of the project and clears their addresses, each with an
    `invitation_revoked` row (`authz.transfer_ownership` calls it for the outgoing owner). Returns
    how many were revoked. The rows are locked as they are read; on Postgres a row an acceptance
    committed meanwhile no longer matches the filter, so it is left alone."""
    require_action(access, "manage_members")
    now = now or utcnow()
    rows = db.execute(
        select(PrivateProjectInvitation)
        .where(
            PrivateProjectInvitation.project_id == access.project.id,
            PrivateProjectInvitation.accepted_at.is_(None),
            PrivateProjectInvitation.revoked_at.is_(None),
        )
        .order_by(PrivateProjectInvitation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars()
    revoked = 0
    for invitation in list(rows):
        _close(invitation, now=now)
        add_activity(
            db,
            project_id=access.project.id,
            actor_user_id=access.user.id,
            action="invitation_revoked",
            detail={"invitation_id": invitation.public_id},
            now=now,
        )
        revoked += 1
    db.flush()
    return revoked


def _same_address(invitation: PrivateProjectInvitation, user: User) -> bool:
    """Rule 1, twice over: the stored hash and, while the invitation is open, the stored address
    itself, both against the account's address under the account system's normalisation. Both
    comparisons always run."""
    account = user.email or ""
    expected = (address_hash(account) or "").encode("ascii")
    by_hash = hmac.compare_digest(invitation.email_hash.encode("ascii"), expected)
    stored = canonical_address(invitation.email or "").encode("utf-8")
    by_address = hmac.compare_digest(stored, canonical_address(account).encode("utf-8"))
    return by_hash and by_address and bool(account)


def _checked(
    db: Session, ctx: AuthContext, token: object, *, now: dt.datetime, lock: bool
) -> tuple[User, PrivateProjectInvitation, PrivateProject]:
    """Rules 1-5 in one place, for both the preview and the acceptance. Every refusal is
    `invalid_invitation()`. Once a token matches, every check runs before any is acted on, so a
    token holder learns nothing from the work done about which one failed."""
    user = workspace_user(ctx)
    if user is None or not isinstance(token, str) or not token or len(token) > TOKEN_MAX_CHARS:
        raise invalid_invitation()
    query = select(PrivateProjectInvitation).where(PrivateProjectInvitation.token_hash == hash_token(token))
    if lock:
        query = query.with_for_update()
    invitation = db.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    if invitation is None:
        raise invalid_invitation()
    is_open = _is_open(invitation, now)
    same_address = _same_address(invitation, user)
    project = db.get(PrivateProject, invitation.project_id, populate_existing=True)
    sender_owns = db.scalar(
        select(func.count())
        .select_from(PrivateProjectMember)
        .where(
            PrivateProjectMember.project_id == invitation.project_id,
            PrivateProjectMember.user_id == invitation.invited_by_user_id,
            PrivateProjectMember.role == "owner",
        )
    )
    if project is None or project.archived_at is not None or not (is_open and same_address and sender_owns):
        raise invalid_invitation()
    return user, invitation, project


def preview_invitation(
    db: Session, ctx: AuthContext, token: object, *, now: dt.datetime | None = None
) -> InvitationPreview:
    """Who invited you, the project and the role, shown only after the token and the address both
    check out (docs/19 §6.2). Consumes nothing."""
    _, invitation, project = _checked(db, ctx, token, now=now or utcnow(), lock=False)
    inviter_name = db.execute(
        select(User.name).where(User.id == invitation.invited_by_user_id)
    ).scalar_one_or_none()
    return InvitationPreview(invitation=invitation, project=project, inviter_name=inviter_name)


def accept_invitation(
    db: Session, ctx: AuthContext, token: object, *, now: dt.datetime | None = None
) -> ProjectAccess:
    """Single use (rule 3): lock, check, add the membership, stamp `accepted_at` and clear the
    address, in the caller's transaction. A user who is already a member keeps their role; the
    invitation is still consumed. Returns the new member's access (`read`)."""
    now = now or utcnow()
    user, invitation, project = _checked(db, ctx, token, now=now, lock=True)
    member = db.execute(
        select(PrivateProjectMember).where(
            PrivateProjectMember.project_id == project.id, PrivateProjectMember.user_id == user.id
        )
    ).scalar_one_or_none()
    joined = member is None
    if member is None:
        member = PrivateProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=invitation.role,
            added_by_user_id=invitation.invited_by_user_id,
            added_at=now,
        )
        db.add(member)
    invitation.accepted_at = now
    invitation.accepted_by_user_id = user.id
    invitation.email = None
    db.flush()
    if joined:
        add_activity(
            db,
            project_id=project.id,
            actor_user_id=user.id,
            action="member_joined",
            detail={"user_id": user.public_id, "role": member.role, "invitation_id": invitation.public_id},
            now=now,
        )
        db.flush()
    return ProjectAccess(project=project, member=member, user=user, action="read")


def revoke_user_invitations(db: Session, user: User, *, now: dt.datetime | None = None) -> tuple[int, int]:
    """For erasure (docs/19 §5.4 item 4): revokes the open invitations `user` sent, and those
    addressed to `user`'s email, clearing their addresses. Call it before the address itself is
    erased. Returns (sent, addressed)."""
    now = now or utcnow()
    still_open = (
        PrivateProjectInvitation.accepted_at.is_(None),
        PrivateProjectInvitation.revoked_at.is_(None),
    )
    sent = _bulk_update(
        db,
        update(PrivateProjectInvitation)
        .where(PrivateProjectInvitation.invited_by_user_id == user.id, *still_open)
        .values(revoked_at=now, email=None),
    )
    addressed = 0
    email_hash = address_hash(user.email)
    if email_hash is not None:
        addressed = _bulk_update(
            db,
            update(PrivateProjectInvitation)
            .where(PrivateProjectInvitation.email_hash == email_hash, *still_open)
            .values(revoked_at=now, email=None),
        )
    return sent, addressed
