"""Invitations (docs/19 §2.6 rules 1-7; §5.6 names the first eight tests)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from services.api.auth import PUBLIC_CONTEXT, AuthContext
from services.api.errors import ProblemError
from services.db.base import Base
from services.db.models import User
from services.projects import invitations
from services.projects.authz import (
    ProjectAccess,
    RoleForbidden,
    authorize,
    create_project,
    set_archived,
    transfer_ownership,
)
from services.projects.conftest import UserFactory, api_key_ctx, session_ctx
from services.projects.invitations import (
    INVALID_TITLE,
    IssuedInvitation,
    accept_invitation,
    create_invitation,
    expire_invitations,
    list_invitations,
    preview_invitation,
    revoke_invitation,
    revoke_user_invitations,
)
from services.projects.models import (
    INVITATION_TTL,
    MAX_INVITATIONS_PER_USER_PER_DAY,
    MAX_PENDING_INVITATIONS_PER_PROJECT,
    PrivateProjectActivity,
    PrivateProjectInvitation,
    PrivateProjectMember,
)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def owner(make_user: UserFactory) -> User:
    return make_user("owner@example.org", name="Olu Owner")


@pytest.fixture()
def access(db: Session, owner: User) -> ProjectAccess:
    created = create_project(db, session_ctx(owner), fields={"title": "Abilene solar"}, now=NOW)
    return authorize(db, session_ctx(owner), created.project.public_id, "invite")


def _problem(call: object) -> tuple[str, int, str, str | None]:
    assert callable(call)
    with pytest.raises(ProblemError) as excinfo:
        call()
    exc = excinfo.value
    return exc.code, exc.status, exc.title, exc.detail


def _invalid() -> tuple[str, int, str, str | None]:
    return ("not_found", 404, INVALID_TITLE, invitations.INVALID_DETAIL)


def test_token_is_single_use(db: Session, access: ProjectAccess, make_user: UserFactory) -> None:
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="editor", now=NOW)
    joined = accept_invitation(db, session_ctx(invitee), issued.token, now=NOW + dt.timedelta(hours=1))
    assert joined.role == "editor" and joined.project.id == access.project.id
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.accepted_at is not None and row.accepted_by_user_id == invitee.id
    assert row.email is None  # cleared on accept
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), issued.token)) == _invalid()
    other = make_user("partner2@example.org")
    assert _problem(lambda: accept_invitation(db, session_ctx(other), issued.token)) == _invalid()
    assert authorize(db, session_ctx(invitee), access.project.public_id, "edit").role == "editor"
    joined_rows = (
        db.execute(sa.select(PrivateProjectActivity).where(PrivateProjectActivity.action == "member_joined"))
        .scalars()
        .all()
    )
    assert [r.detail["role"] for r in joined_rows] == ["editor"]


def test_expired_token_refused(db: Session, access: ProjectAccess, make_user: UserFactory) -> None:
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    assert issued.invitation.expires_at == NOW + INVITATION_TTL == NOW + dt.timedelta(days=7)
    just_after = NOW + INVITATION_TTL + dt.timedelta(seconds=1)
    assert (
        _problem(lambda: accept_invitation(db, session_ctx(invitee), issued.token, now=just_after))
        == _invalid()
    )
    assert (
        _problem(lambda: preview_invitation(db, session_ctx(invitee), issued.token, now=just_after))
        == _invalid()
    )
    assert accept_invitation(
        db, session_ctx(invitee), issued.token, now=NOW + INVITATION_TTL - dt.timedelta(seconds=1)
    )


def test_revoked_token_refused(db: Session, access: ProjectAccess, make_user: UserFactory) -> None:
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    revoke_invitation(db, access, issued.invitation.public_id, now=NOW)
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.revoked_at is not None and row.email is None
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), issued.token, now=NOW)) == _invalid()
    # Revoking twice, or an id from nowhere, is the project's ordinary 404.
    for public_id in (issued.invitation.public_id, "inv_0000000000"):
        code, status, *_ = _problem(lambda public_id=public_id: revoke_invitation(db, access, public_id))
        assert (code, status) == ("not_found", 404)
    # The revocation stamp alone refuses, even on a row whose address was never cleared.
    second = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    second.invitation.revoked_at = NOW
    db.flush()
    assert second.invitation.email is not None
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), second.token, now=NOW)) == _invalid()


def test_wrong_or_unverified_email_refused(
    db: Session, access: ProjectAccess, make_user: UserFactory
) -> None:
    issued = create_invitation(db, access, email="Partner@Example.org", role="editor", now=NOW)
    wrong = make_user("someone.else@example.org")
    unverified = make_user("partner@example.org", verified=False)
    for ctx in (session_ctx(wrong), session_ctx(unverified), PUBLIC_CONTEXT, api_key_ctx(db, wrong)):
        assert _problem(lambda ctx=ctx: accept_invitation(db, ctx, issued.token, now=NOW)) == _invalid()
        assert _problem(lambda ctx=ctx: preview_invitation(db, ctx, issued.token, now=NOW)) == _invalid()
    # Verifying the address is what turns the same account into the invitee: trimmed, any case.
    unverified.email_verified_at = NOW
    db.flush()
    preview = preview_invitation(db, session_ctx(unverified), issued.token, now=NOW)
    assert preview.project.title == "Abilene solar" and preview.inviter_name == "Olu Owner"
    assert accept_invitation(db, session_ctx(unverified), issued.token, now=NOW).role == "editor"


@pytest.mark.parametrize(
    "invited, impostor",
    [
        # `straße.de` (xn--strae-oqa.de) is a separate, registrable IDN domain from `strasse.de`.
        ("kate@strasse.de", "kate@straße.de"),
        ("kate@straße.de", "kate@strasse.de"),
        # Final and medial sigma are different mailboxes to the account system (`str.lower`).
        ("nikoσ@example.gr", "nikoς@example.gr"),
        ("ΝΙΚΟΣ@example.gr", "νικοσ@example.gr"),
    ],
)
def test_case_folding_never_matches_another_account(
    db: Session, access: ProjectAccess, make_user: UserFactory, invited: str, impostor: str
) -> None:
    """Rule 1 is the account system's identity (`email.lower()`, the address its verification mail
    went to), never `str.casefold()`, under which each pair here is one address. An account
    verified at the other address must not open a leaked link."""
    assert invited.strip().casefold() == impostor.strip().casefold()
    assert invited.strip().lower() != impostor.strip().lower()
    issued = create_invitation(db, access, email=invited, role="editor", now=NOW)
    attacker = make_user(impostor)
    assert attacker.email == impostor.lower() and attacker.email_verified_at is not None
    assert _problem(lambda: accept_invitation(db, session_ctx(attacker), issued.token, now=NOW)) == _invalid()
    assert (
        _problem(lambda: preview_invitation(db, session_ctx(attacker), issued.token, now=NOW)) == _invalid()
    )
    with pytest.raises(ProblemError):
        authorize(db, session_ctx(attacker), access.project.public_id, "read")
    # Each address has its own open invitation: inviting one never replaces the other's link.
    second = create_invitation(db, access, email=impostor, role="viewer", now=NOW)
    assert len(list_invitations(db, access, now=NOW)) == 2
    assert accept_invitation(db, session_ctx(attacker), second.token, now=NOW).role == "viewer"
    # The invited account itself still gets in.
    invitee = make_user(invited)
    assert accept_invitation(db, session_ctx(invitee), issued.token, now=NOW).role == "editor"


def test_the_address_hash_is_the_account_systems_identity() -> None:
    assert invitations.canonical_address("  Kate@Strasse.DE ") == "kate@strasse.de"
    assert invitations.address_hash(" Kate@Strasse.DE") == invitations.address_hash("kate@strasse.de")
    assert invitations.address_hash("kate@straße.de") != invitations.address_hash("kate@strasse.de")
    assert invitations.address_hash("nikoς@example.gr") != invitations.address_hash("nikoσ@example.gr")
    assert invitations.address_hash(None) is None and invitations.address_hash("  ") is None
    # Peppered: the hash of an address is not its bare SHA-256.
    bare = hashlib.sha256(b"kate@strasse.de").hexdigest()
    assert invitations.address_hash("kate@strasse.de") != bare


def test_errors_are_indistinguishable(
    db: Session, access: ProjectAccess, make_user: UserFactory, owner: User
) -> None:
    invitee = make_user("partner@example.org")
    ctx = session_ctx(invitee)
    used = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    accept_invitation(db, ctx, used.token, now=NOW)
    expired = create_invitation(
        db, access, email="late@example.org", role="viewer", now=NOW - dt.timedelta(days=8)
    )
    revoked = create_invitation(db, access, email="revoked@example.org", role="viewer", now=NOW)
    revoke_invitation(db, access, revoked.invitation.public_id, now=NOW)
    wrong_address = create_invitation(db, access, email="not-them@example.org", role="viewer", now=NOW)
    archived_access = create_project(db, session_ctx(owner), fields={"title": "Shelved"}, now=NOW)
    archived = create_invitation(
        db,
        authorize(db, session_ctx(owner), archived_access.project.public_id, "invite"),
        email="partner@example.org",
        role="viewer",
        now=NOW,
    )
    set_archived(db, authorize(db, session_ctx(owner), archived_access.project.public_id, "archive"), True)
    late_ctx = session_ctx(make_user("late@example.org"))
    revoked_ctx = session_ctx(make_user("revoked@example.org"))
    answers = {
        "used": _problem(lambda: accept_invitation(db, ctx, used.token, now=NOW)),
        "expired": _problem(lambda: accept_invitation(db, late_ctx, expired.token, now=NOW)),
        "revoked": _problem(lambda: accept_invitation(db, revoked_ctx, revoked.token, now=NOW)),
        "wrong address": _problem(lambda: accept_invitation(db, ctx, wrong_address.token, now=NOW)),
        "archived project": _problem(lambda: accept_invitation(db, ctx, archived.token, now=NOW)),
        "made up": _problem(lambda: accept_invitation(db, ctx, "x" * 43, now=NOW)),
        "empty": _problem(lambda: accept_invitation(db, ctx, "", now=NOW)),
        "too long": _problem(lambda: accept_invitation(db, ctx, "x" * 500, now=NOW)),
        "not a string": _problem(lambda: accept_invitation(db, ctx, None, now=NOW)),
        "signed out": _problem(lambda: accept_invitation(db, PUBLIC_CONTEXT, wrong_address.token, now=NOW)),
    }
    assert set(answers.values()) == {_invalid()}, answers


def test_token_stored_only_as_hash(db: Session, access: ProjectAccess) -> None:
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    db.commit()
    assert len(issued.token) >= 43  # secrets.token_urlsafe(32)
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.token_hash == hashlib.sha256(issued.token.encode()).hexdigest()
    every_value = " ".join(
        str(v)
        for table in ("private_project_invitation", "private_project_activity")
        for r in db.execute(sa.select(Base.metadata.tables[table])).all()
        for v in r
    )
    assert issued.token not in every_value
    assert "partner@example.org" not in str(
        db.execute(sa.select(PrivateProjectActivity.detail)).scalars().all()
    )
    assert issued.token not in repr(issued)
    assert "token" not in {f.name for f in dataclasses.fields(IssuedInvitation) if f.repr}
    assert issued.link.endswith(f"/my/invitations/accept?t={issued.token}")


def test_pending_and_daily_limits(db: Session, access: ProjectAccess, make_user: UserFactory) -> None:
    for n in range(MAX_PENDING_INVITATIONS_PER_PROJECT):
        create_invitation(db, access, email=f"p{n}@example.org", role="viewer", now=NOW)
    code, status, *_ = _problem(
        lambda: create_invitation(db, access, email="p10@example.org", role="viewer", now=NOW)
    )
    assert (code, status) == ("conflict", 409)
    # Re-inviting a pending address replaces its link and needs no free slot.
    create_invitation(db, access, email="p0@example.org", role="editor", now=NOW)
    assert len(list_invitations(db, access, now=NOW)) == MAX_PENDING_INVITATIONS_PER_PROJECT
    # Expired invitations stop counting against the project.
    create_invitation(db, access, email="p10@example.org", role="viewer", now=NOW + dt.timedelta(days=8))

    # The daily limit is per inviter, across all their projects, over a rolling day.
    busy = make_user("busy@example.org")
    projects = [
        authorize(
            db,
            session_ctx(busy),
            create_project(db, session_ctx(busy), fields={"title": f"P{n}"}).project.public_id,
            "invite",
        )
        for n in range(3)
    ]
    sent = 0
    for project in projects[:2]:
        for _ in range(MAX_PENDING_INVITATIONS_PER_PROJECT):
            create_invitation(
                db, project, email=f"d{sent}@example.org", role="viewer", now=NOW + dt.timedelta(seconds=sent)
            )
            sent += 1
    assert sent == MAX_INVITATIONS_PER_USER_PER_DAY
    with pytest.raises(ProblemError) as excinfo:
        create_invitation(
            db, projects[2], email="one-more@example.org", role="viewer", now=NOW + dt.timedelta(hours=1)
        )
    assert excinfo.value.code == "quota_exceeded" and excinfo.value.status == 429
    assert excinfo.value.headers is not None
    assert int(excinfo.value.headers["Retry-After"]) == 23 * 3600  # until the first of the 20 is a day old
    create_invitation(
        db, projects[2], email="one-more@example.org", role="viewer", now=NOW + dt.timedelta(days=1)
    )


def test_only_owner_lists_invitations(db: Session, access: ProjectAccess, make_user: UserFactory) -> None:
    create_invitation(db, access, email="partner@example.org", role="editor", now=NOW)
    editor_user, viewer_user = make_user("editor@example.org"), make_user("viewer@example.org")
    for user, role in ((editor_user, "editor"), (viewer_user, "viewer")):
        db.add(PrivateProjectMember(project_id=access.project.id, user_id=user.id, role=role, added_at=NOW))
    db.flush()
    for user in (editor_user, viewer_user):
        member = authorize(db, session_ctx(user), access.project.public_id, "read")
        with pytest.raises(RoleForbidden):
            list_invitations(db, member, now=NOW)
        with pytest.raises(RoleForbidden):
            create_invitation(db, member, email="x@example.org", role="viewer", now=NOW)
        with pytest.raises(RoleForbidden):
            revoke_invitation(db, member, "inv_0000000000", now=NOW)
    listed = list_invitations(db, access, now=NOW)
    assert [i.email for i in listed] == ["partner@example.org"]
    outsider = make_user("outsider@example.org")
    with pytest.raises(ProblemError) as excinfo:
        authorize(db, session_ctx(outsider), access.project.public_id, "invite")
    assert excinfo.value.status == 404


def test_reinviting_an_address_replaces_its_link(
    db: Session, access: ProjectAccess, make_user: UserFactory
) -> None:
    invitee = make_user("partner@example.org")
    first = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    second = create_invitation(db, access, email=" PARTNER@example.org ", role="editor", now=NOW)
    assert first.token != second.token
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), first.token, now=NOW)) == _invalid()
    assert accept_invitation(db, session_ctx(invitee), second.token, now=NOW).role == "editor"
    rotated = (
        db.execute(
            sa.select(PrivateProjectActivity).where(PrivateProjectActivity.action == "invitation_revoked")
        )
        .scalars()
        .one()
    )
    assert rotated.detail == {"invitation_id": first.invitation.public_id, "rotated": True}


def test_an_existing_member_keeps_their_role_and_the_invitation_is_spent(
    db: Session, access: ProjectAccess, make_user: UserFactory
) -> None:
    member = make_user("partner@example.org")
    db.add(PrivateProjectMember(project_id=access.project.id, user_id=member.id, role="editor", added_at=NOW))
    db.flush()
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    assert accept_invitation(db, session_ctx(member), issued.token, now=NOW).role == "editor"
    assert _problem(lambda: accept_invitation(db, session_ctx(member), issued.token, now=NOW)) == _invalid()


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "no-at-sign",
        "two@@example.org",
        "a@b",
        "a b@example.org",
        "x" * 250 + "@example.org",
        None,
        # Postgres text cannot hold NUL; the driver's error would carry the address into logs.
        "part\x00ner@example.org",
        "partner@example.org\x00",
        "partner\x7f@example.org",
        "partner\x1b@example.org",
        "partner\ud800@example.org",
    ],
)
def test_invitation_needs_one_address_and_an_invitable_role(
    db: Session, access: ProjectAccess, email: object
) -> None:
    code, status, *_ = _problem(lambda: create_invitation(db, access, email=email, role="viewer", now=NOW))  # type: ignore[arg-type]
    assert (code, status) == ("validation_error", 400)


@pytest.mark.parametrize("role", ["owner", "admin", ""])
def test_an_invitation_never_grants_ownership(db: Session, access: ProjectAccess, role: str) -> None:
    code, *_ = _problem(lambda: create_invitation(db, access, email="p@example.org", role=role, now=NOW))
    assert code == "validation_error"


def _statements(db: Session, call: object) -> list[tuple[str, tuple[object, ...]]]:
    """Every SQL statement `call()` sends, with its parameters."""
    assert callable(call)
    seen: list[tuple[str, tuple[object, ...]]] = []

    def capture(_conn: object, _cursor: object, statement: str, parameters: object, *_: object) -> None:
        values = tuple(parameters.values()) if isinstance(parameters, dict) else tuple(parameters or ())  # type: ignore[call-overload]
        seen.append((statement, values))

    engine = db.get_bind()
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        call()
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    return seen


def test_creating_an_invitation_reveals_nothing_about_accounts(
    db: Session, access: ProjectAccess, make_user: UserFactory
) -> None:
    """Creating an invitation never looks the address up among accounts: the statements are the
    same whether or not the address has one, none filters `user` by email, and the address is a
    parameter of the invitation's own INSERT only. (The session may reload the inviter's own row
    by its primary key; that is not a lookup.)"""
    make_user("has-account@example.org")
    runs = {}
    for address in ("has-account@example.org", "no-account@example.org"):
        db.expire_all()  # both runs start alike: the session reloads what it reads
        runs[address] = _statements(
            db, lambda address=address: create_invitation(db, access, email=address, role="viewer", now=NOW)
        )
    texts = {address: [sql for sql, _ in statements] for address, statements in runs.items()}
    assert texts["has-account@example.org"] == texts["no-account@example.org"]
    for address, statements in runs.items():
        for sql, values in statements:
            assert '"user".email =' not in sql and "user.email =" not in sql
            assert "lower(" not in sql.lower()
            if address in values:
                assert sql.startswith("INSERT INTO private_project_invitation"), sql


def test_expiry_clears_the_address(db: Session, access: ProjectAccess) -> None:
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    assert expire_invitations(db, now=NOW + dt.timedelta(days=6)) == 0
    assert expire_invitations(db, now=NOW + dt.timedelta(days=7, seconds=1)) == 1
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.email is None and row.email_hash
    assert list_invitations(db, access, now=NOW) == []


def test_erasure_helper_revokes_sent_and_addressed_invitations(
    db: Session, access: ProjectAccess, owner: User, make_user: UserFactory
) -> None:
    invitee = make_user("partner@example.org")
    create_invitation(db, access, email="someone@example.org", role="viewer", now=NOW)
    other_owner = make_user("other@example.org")
    theirs = create_project(db, session_ctx(other_owner), fields={"title": "Theirs"}, now=NOW)
    to_invitee = create_invitation(
        db,
        authorize(db, session_ctx(other_owner), theirs.project.public_id, "invite"),
        email="partner@example.org",
        role="viewer",
        now=NOW,
    )
    assert revoke_user_invitations(db, owner, now=NOW) == (1, 0)
    assert revoke_user_invitations(db, invitee, now=NOW) == (0, 1)
    row = db.get(PrivateProjectInvitation, to_invitee.invitation.id)
    assert row is not None and row.revoked_at is not None and row.email is None
    open_rows = db.execute(
        sa.select(sa.func.count())
        .select_from(PrivateProjectInvitation)
        .where(PrivateProjectInvitation.email.is_not(None))
    ).scalar()
    assert open_rows == 0


def test_invitations_are_delivered_by_copied_link_only() -> None:
    """Rule 7 (owner, 2026-10-22): no automated channel for invitations is enabled, so this module
    holds no email port and sends nothing."""
    names = set(vars(invitations))
    assert not names & {"EmailPort", "ResendEmailAdapter", "send_verification_email", "httpx", "smtplib"}


def test_accept_by_the_owner_of_a_project_uses_the_owners_role(
    db: Session, access: ProjectAccess, owner: User
) -> None:
    issued = create_invitation(db, access, email=owner.email or "", role="viewer", now=NOW)
    ctx: AuthContext = session_ctx(owner)
    assert accept_invitation(db, ctx, issued.token, now=NOW).role == "owner"


def _as_postgres(db: Session, call: object) -> list[str]:
    """The SELECTs `call()` runs through the session, compiled for Postgres (SQLite renders no row
    locks), so a test can read the locks the code asks for."""
    assert callable(call)
    seen: list[str] = []

    def capture(state: Any) -> None:
        if state.is_select:
            seen.append(" ".join(str(state.statement.compile(dialect=postgresql.dialect())).split()))

    sa.event.listen(db, "do_orm_execute", capture)
    try:
        call()
    finally:
        sa.event.remove(db, "do_orm_execute", capture)
    return seen


def test_the_limits_are_counted_under_locks(db: Session, access: ProjectAccess, owner: User) -> None:
    """Rule 6: the inviter's row, then the project's, are locked before either count, and the
    address's open invitation is locked before it is replaced, so two concurrent invitations cannot
    both pass a limit or both insert an open row (which would fail on the unique index with the
    address in the error)."""

    def first(sql: list[str], *parts: str) -> int:
        return next(i for i, s in enumerate(sql) if all(part in s for part in parts))

    create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    sql = _as_postgres(
        db, lambda: create_invitation(db, access, email="partner@example.org", role="editor", now=NOW)
    )
    user_lock = first(sql, 'SELECT "user".id FROM "user" WHERE', "FOR NO KEY UPDATE")
    project_lock = first(sql, "SELECT private_project.id FROM private_project WHERE", "FOR NO KEY UPDATE")
    daily = first(sql, "SELECT private_project_invitation.created_at FROM")
    replaced = first(sql, "private_project_invitation.email_hash =")
    pending = first(sql, "count(*)", "FROM private_project_invitation")
    assert user_lock < project_lock < daily < replaced < pending
    assert sql[replaced].endswith("FOR UPDATE")
    assert not sql[daily].endswith("UPDATE") and not sql[pending].endswith("UPDATE")
    # The active-project cap is counted under the user's lock too.
    sql = _as_postgres(
        db, lambda: create_project(db, session_ctx(owner), fields={"title": "Second"}, now=NOW)
    )
    user_lock = first(sql, 'SELECT "user".id FROM "user" WHERE', "FOR NO KEY UPDATE")
    assert user_lock < first(sql, "count(*)", "private_project_member.role")


def test_revoke_locks_the_row_and_an_accepted_invitation_stays_accepted(
    db: Session, access: ProjectAccess, make_user: UserFactory
) -> None:
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    pending = create_invitation(db, access, email="later@example.org", role="viewer", now=NOW)
    sql = _as_postgres(db, lambda: revoke_invitation(db, access, pending.invitation.public_id, now=NOW))
    lookups = [s for s in sql if "private_project_invitation.public_id =" in s]
    assert len(lookups) == 1 and lookups[0].endswith("FOR UPDATE")
    accept_invitation(db, session_ctx(invitee), issued.token, now=NOW)
    code, status, *_ = _problem(lambda: revoke_invitation(db, access, issued.invitation.public_id, now=NOW))
    assert (code, status) == ("not_found", 404)
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.accepted_at is not None and row.revoked_at is None
    assert authorize(db, session_ctx(invitee), access.project.public_id, "read").role == "viewer"


def test_an_invitation_ends_with_its_senders_ownership(
    db: Session, access: ProjectAccess, owner: User, make_user: UserFactory
) -> None:
    """A transfer revokes the outgoing owner's open invitations and clears their addresses, and an
    invitation whose sender no longer owns the project is refused at acceptance whatever route
    took the role away."""
    editor = make_user("editor@example.org")
    db.add(PrivateProjectMember(project_id=access.project.id, user_id=editor.id, role="editor", added_at=NOW))
    db.flush()
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="viewer", now=NOW)
    manager = authorize(db, session_ctx(owner), access.project.public_id, "manage_members")
    transfer_ownership(db, manager, editor.public_id, now=NOW)
    row = db.get(PrivateProjectInvitation, issued.invitation.id)
    assert row is not None and row.revoked_at is not None and row.email is None
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), issued.token, now=NOW)) == _invalid()
    revoked = (
        db.execute(
            sa.select(PrivateProjectActivity.detail).where(
                PrivateProjectActivity.action == "invitation_revoked"
            )
        )
        .scalars()
        .all()
    )
    assert revoked == [{"invitation_id": issued.invitation.public_id}]

    # The new owner's own invitation works; demote them by hand (no transfer, so nothing is
    # revoked) and the same link is refused, with the same answer as every other failure.
    new_owner = authorize(db, session_ctx(editor), access.project.public_id, "invite")
    theirs = create_invitation(db, new_owner, email="partner@example.org", role="viewer", now=NOW)
    db.execute(
        sa.update(PrivateProjectMember)
        .where(PrivateProjectMember.project_id == access.project.id)
        .values(role="viewer")
    )
    db.flush()
    db.execute(
        sa.update(PrivateProjectMember)
        .where(PrivateProjectMember.project_id == access.project.id, PrivateProjectMember.user_id == owner.id)
        .values(role="owner")
    )
    db.flush()
    assert _problem(lambda: accept_invitation(db, session_ctx(invitee), theirs.token, now=NOW)) == _invalid()
    assert _problem(lambda: preview_invitation(db, session_ctx(invitee), theirs.token, now=NOW)) == _invalid()


def test_the_preview_names_the_sender_but_never_their_address(
    db: Session, access: ProjectAccess, make_user: UserFactory, owner: User
) -> None:
    invitee = make_user("partner@example.org")
    issued = create_invitation(db, access, email="partner@example.org", role="editor", now=NOW)
    preview = preview_invitation(db, session_ctx(invitee), issued.token, now=NOW)
    assert {f.name for f in dataclasses.fields(preview)} == {"invitation", "project", "inviter_name"}
    assert preview.inviter_name == "Olu Owner"
    assert owner.email is not None and owner.email not in repr(preview)
