"""The one authorisation function (docs/19 §5.1, §5.6): deny by default, the role table of §2.6,
the same 404 for every outsider, removal biting on the next request, and the membership
mutations that keep one owner per project."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.auth import PUBLIC_CONTEXT, AuthContext, create_session
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.db.base import Base
from services.db.models import Event, User
from services.projects import authz
from services.projects.authz import (
    ACTIONS,
    ProjectAccess,
    RoleForbidden,
    authorize,
    change_member_role,
    create_project,
    delete_project,
    leave_project,
    member_projects,
    project_access,
    project_members,
    remove_member,
    require_workspace_user,
    set_archived,
    transfer_ownership,
    update_project,
)
from services.projects.conftest import UserFactory, api_key_ctx, session_ctx
from services.projects.models import (
    MAX_ACTIVE_PROJECTS_PER_USER,
    PrivateProject,
    PrivateProjectActivity,
    PrivateProjectItem,
    PrivateProjectMember,
    new_private_public_id,
)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


@dataclass
class World:
    project: PrivateProject
    users: dict[str, User]
    contexts: dict[str, AuthContext]


def _add_member(db: Session, project: PrivateProject, user: User, role: str) -> PrivateProjectMember:
    member = PrivateProjectMember(project_id=project.id, user_id=user.id, role=role, added_at=NOW)
    db.add(member)
    db.flush()
    return member


@pytest.fixture()
def world(db: Session, make_user: UserFactory) -> World:
    owner = make_user("owner@example.org")
    access = create_project(db, session_ctx(owner), fields={"title": "Abilene solar"}, now=NOW)
    project = access.project
    users = {
        "owner": owner,
        "editor": make_user("editor@example.org"),
        "viewer": make_user("viewer@example.org"),
        "non_member": make_user("outsider@example.org"),
        "removed_member": make_user("removed@example.org"),
        "anonymised_member": make_user("gone@example.org", status="anonymised"),
        "disabled_member": make_user("disabled@example.org", status="disabled"),
        "unverified_member": make_user("unverified@example.org", verified=False),
        "admin_session": make_user("operator@example.org", role="owner"),
    }
    _add_member(db, project, users["editor"], "editor")
    _add_member(db, project, users["viewer"], "viewer")
    removed = _add_member(db, project, users["removed_member"], "editor")
    for name in ("anonymised_member", "disabled_member", "unverified_member"):
        _add_member(db, project, users[name], "editor")
    db.delete(removed)
    db.flush()
    contexts = {name: session_ctx(user) for name, user in users.items()}
    contexts["api_key"] = api_key_ctx(db, owner)
    contexts["no_session"] = PUBLIC_CONTEXT
    return World(project=project, users=users, contexts=contexts)


#: docs/19 §2.6 as the test reads it; `archive` covers archive and unarchive.
LIVE_ALLOWED = {
    "owner": {"read", "edit", "invite", "manage_members", "delete", "archive"},
    "editor": {"read", "edit", "leave"},
    "viewer": {"read", "leave"},
}
OUTSIDERS = (
    "non_member",
    "removed_member",
    "anonymised_member",
    "disabled_member",
    "unverified_member",
    "admin_session",
    "api_key",
    "no_session",
)


def _outcome(db: Session, ctx: AuthContext, project_id: str, action: str) -> str:
    try:
        access = authorize(db, ctx, project_id, action)  # type: ignore[arg-type]
    except RoleForbidden as exc:
        assert exc.status == 403 and exc.code == "forbidden_role"
        return "403"
    except ProblemError as exc:
        assert exc.status == 404 and exc.code == "not_found"
        return "404"
    assert access.project.public_id == project_id
    return "ok"


@pytest.mark.parametrize("archived", [False, True], ids=["live", "archived"])
def test_role_action_matrix(db: Session, world: World, archived: bool) -> None:
    if archived:
        world.project.archived_at = NOW
        db.flush()
    project_id = world.project.public_id
    for subject in (*LIVE_ALLOWED, *OUTSIDERS):
        for action in ACTIONS:
            got = _outcome(db, world.contexts[subject], project_id, action)
            if subject in OUTSIDERS:
                expected = "404"
            elif archived and action not in {"read", "archive", "delete"}:
                expected = "404"
            else:
                expected = "ok" if action in LIVE_ALLOWED[subject] else "403"
            assert got == expected, (subject, action, archived)
    for action in ACTIONS:
        assert _outcome(db, world.contexts["owner"], "prj_not-a-crockford-id", action) == "404"
        assert _outcome(db, world.contexts["owner"], "prop_0000000001", action) == "404"
        assert _outcome(db, world.contexts["owner"], "", action) == "404"


def test_owner_leave_is_refused_with_a_transfer_hint(db: Session, world: World) -> None:
    with pytest.raises(RoleForbidden) as excinfo:
        authorize(db, world.contexts["owner"], world.project.public_id, "leave")
    assert "Transfer ownership" in (excinfo.value.detail or "")


def test_unknown_action_is_a_programming_error(db: Session, world: World) -> None:
    with pytest.raises(ValueError):
        authorize(db, world.contexts["owner"], world.project.public_id, "publish")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        project_access("publish")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- over HTTP, no oracle
def _app(factory: sessionmaker[Session]) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)

    @app.get("/v1/my/projects/{project_id}")
    def detail(access: Annotated[ProjectAccess, Depends(project_access("read"))]) -> dict[str, Any]:
        return {"public_id": access.project.public_id, "role": access.role}

    def _db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture()
def http(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(_app(db_sessionmaker)) as client:
        yield client


def _cookie(db: Session, user: User) -> dict[str, str]:
    _, value = create_session(db, user)
    db.commit()
    return {"Cookie": f"session={value}"}


def _count_statements(db_sessionmaker: sessionmaker[Session]) -> list[str]:
    seen: list[str] = []
    engine = db_sessionmaker.kw["bind"]

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    return seen


def test_outsider_gets_identical_404_for_real_and_missing_ids(
    db: Session, world: World, http: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    outsider = _cookie(db, world.users["non_member"])
    owner = _cookie(db, world.users["owner"])
    real = world.project.public_id
    missing = new_private_public_id("prj")
    assert http.get(f"/v1/my/projects/{real}", headers=owner).status_code == 200

    statements = _count_statements(db_sessionmaker)

    def probe(project_id: str, **kwargs: Any) -> tuple[int, dict[str, Any], int]:
        start = len(statements)
        response = http.get(f"/v1/my/projects/{project_id}", **kwargs)
        body = response.json()
        body.pop("request_id")
        body["instance"] = body["instance"].replace(project_id, "{id}")
        assert response.headers["content-type"] == "application/problem+json"
        return response.status_code, body, len(statements) - start

    real_answer = probe(real, headers=outsider)
    missing_answer = probe(missing, headers=outsider)
    assert real_answer == missing_answer
    assert real_answer[0] == 404 and real_answer[1]["code"] == "not_found"
    # Neither the body nor the work done tells a private project from a missing one.
    assert real_answer[2] == missing_answer[2]
    for other in (
        probe("prj_bad", headers=outsider)[:2],
        probe(real)[:2],
        probe(real, headers={"Authorization": "Bearer bk_live_" + "x" * 43})[:2],
    ):
        assert other == real_answer[:2]


def test_api_key_never_opens_a_private_project_even_as_admin(
    db: Session, world: World, http: TestClient
) -> None:
    from services.api.auth import generate_api_key
    from services.db.models import ApiKey

    secret, key_hash, last4 = generate_api_key()
    db.add(
        ApiKey(
            public_id="key_admin0000",
            account_id=world.users["owner"].account_id,
            created_by_user_id=world.users["owner"].id,
            name="admin",
            last4=last4,
            key_hash=key_hash,
            scopes=["admin:*", "read:bulk"],
            tier="api",
        )
    )
    db.commit()
    response = http.get(
        f"/v1/my/projects/{world.project.public_id}", headers={"Authorization": f"Bearer {secret}"}
    )
    assert response.status_code == 404


def test_removed_member_refused_on_next_request(db: Session, world: World, http: TestClient) -> None:
    editor = world.users["editor"]
    cookie = _cookie(db, editor)
    url = f"/v1/my/projects/{world.project.public_id}"
    assert http.get(url, headers=cookie).json()["role"] == "editor"
    # In-process too: the editor's access was loaded into this session a moment ago.
    assert authorize(db, world.contexts["editor"], world.project.public_id, "edit").role == "editor"

    owner_access = authorize(db, world.contexts["owner"], world.project.public_id, "manage_members")
    remove_member(db, owner_access, editor.public_id, now=NOW)
    db.commit()

    assert http.get(url, headers=cookie).status_code == 404
    with pytest.raises(ProblemError) as excinfo:
        authorize(db, world.contexts["editor"], world.project.public_id, "read")
    assert excinfo.value.status == 404


def test_project_id_is_read_from_the_path_only(
    db_sessionmaker: sessionmaker[Session], db: Session, world: World
) -> None:
    """A route that forgot `{project_id}` must not read a private id from the query string."""
    app = _app(db_sessionmaker)

    @app.get("/v1/my/misrouted")
    def misrouted(access: Annotated[ProjectAccess, Depends(project_access("read"))]) -> dict[str, Any]:
        return {"public_id": access.project.public_id}  # pragma: no cover - never reached

    with TestClient(app) as client:
        owner = _cookie(db, world.users["owner"])
        response = client.get(f"/v1/my/misrouted?project_id={world.project.public_id}", headers=owner)
    assert response.status_code != 200
    assert world.project.public_id not in response.text


def test_project_access_is_one_class_per_action() -> None:
    dependency = project_access("edit")
    assert isinstance(dependency, authz.ProjectAccessDependency)
    assert dependency.action == "edit"
    assert repr(dependency) == "project_access('edit')"


# ------------------------------------------------------------------------------ the mutations
def test_create_project_makes_an_owner_row_a_checklist_and_a_history(
    db: Session, make_user: UserFactory
) -> None:
    user = make_user()
    access = create_project(
        db,
        session_ctx(user),
        fields={
            "title": "  Wind   at Sweetwater ",
            "technology": "wind",
            "capacity_mw": "150.5",
            "lat": 32.47,
            "lon": -100.4,
        },
        origin_type="proposal",
        origin_public_id="prop_0000000001",
        now=NOW,
    )
    project = access.project
    assert access.role == "owner" and project.title == "Wind at Sweetwater"
    assert project.point_precision == "user" and str(project.capacity_mw) == "150.500"
    members = (
        db.execute(sa.select(PrivateProjectMember).where(PrivateProjectMember.project_id == project.id))
        .scalars()
        .all()
    )
    assert [(m.user_id, m.role) for m in members] == [(user.id, "owner")]
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProjectItem)).scalar() == 32
    activity = db.execute(sa.select(PrivateProjectActivity)).scalars().one()
    assert activity.action == "created"
    assert activity.detail == {
        "kind": "project",
        "origin_type": "proposal",
        "origin_public_id": "prop_0000000001",
    }
    assert project.kind == "project" and project.stage == "prospecting"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "ok", "owner_user_id": "usr_1"},
        {"title": "ok", "project_id": "prj_1"},
        {"title": "ok", "public_id": "prj_1"},
        {"title": "ok", "id": "1"},
        {"title": "ok", "archived_at": "2026-10-22"},
        {"title": "ok", "stage": "verified"},
        {"title": "ok", "technology": "unknown"},
        {"title": "ok", "capacity_mw": -1},
        {"title": "ok", "capacity_mw": "lots"},
        {"title": "ok", "capacity_mw": True},
        {"title": "ok", "capacity_mw": float("nan")},
        {"title": "ok", "lat": 30},
        {"title": "ok", "lat": 91, "lon": 0},
        {"title": "ok", "point_precision": "rooftop", "lat": 1, "lon": 1},
    ],
)
def test_create_project_refuses_bad_fields_and_any_id_or_owner(
    db: Session, make_user: UserFactory, fields: dict[str, object]
) -> None:
    with pytest.raises(ProblemError) as excinfo:
        create_project(db, session_ctx(make_user()), fields=fields)
    assert excinfo.value.code == "validation_error"


def test_create_project_needs_a_verified_session(db: Session, make_user: UserFactory) -> None:
    owner = make_user()
    for ctx, code in (
        (PUBLIC_CONTEXT, "unauthenticated"),
        (api_key_ctx(db, owner), "unauthenticated"),
        (session_ctx(make_user(status="disabled")), "unauthenticated"),
        (session_ctx(make_user(verified=False)), "forbidden_tier"),
    ):
        with pytest.raises(ProblemError) as excinfo:
            create_project(db, ctx, fields={"title": "x"})
        assert excinfo.value.code == code
    assert require_workspace_user(session_ctx(owner)) is owner


def test_active_project_limit(db: Session, make_user: UserFactory) -> None:
    user = make_user()
    ctx = session_ctx(user)
    accesses = [
        create_project(db, ctx, fields={"title": f"Deal {n}"}) for n in range(MAX_ACTIVE_PROJECTS_PER_USER)
    ]
    with pytest.raises(ProblemError) as excinfo:
        create_project(db, ctx, fields={"title": "One too many"})
    assert excinfo.value.code == "conflict"
    set_archived(db, accesses[0], True)
    create_project(db, ctx, fields={"title": "Fits again"})


def test_update_records_field_names_never_values(db: Session, world: World) -> None:
    access = authorize(db, world.contexts["editor"], world.project.public_id, "edit")
    changed = update_project(
        db, access, {"title": "Secret new name", "stage": "development", "lat": 31, "lon": -99}, now=NOW
    )
    assert changed == ["lat", "lon", "point_precision", "stage", "title"]
    assert world.project.point_precision == "user"
    row = (
        db.execute(sa.select(PrivateProjectActivity).where(PrivateProjectActivity.action == "field_changed"))
        .scalars()
        .one()
    )
    assert row.detail == {"fields": ["lat", "lon", "point_precision", "stage", "title"]}
    assert "Secret" not in str(row.detail)
    assert update_project(db, access, {"stage": "development"}) == []
    update_project(db, access, {"lat": None, "lon": None})
    assert world.project.point_precision is None
    with pytest.raises(ProblemError):
        update_project(db, access, {"lat": 10})
    assert world.project.lat is None  # a refused edit leaves the row untouched
    viewer = authorize(db, world.contexts["viewer"], world.project.public_id, "read")
    with pytest.raises(RoleForbidden):
        update_project(db, viewer, {"title": "x"})


def test_archive_freezes_the_project_until_unarchived(db: Session, world: World) -> None:
    owner = authorize(db, world.contexts["owner"], world.project.public_id, "archive")
    editor = authorize(db, world.contexts["editor"], world.project.public_id, "edit")
    assert set_archived(db, owner, True, now=NOW) is True
    assert set_archived(db, owner, True) is False
    with pytest.raises(ProblemError) as excinfo:
        update_project(db, editor, {"title": "x"})
    assert excinfo.value.status == 404
    assert member_projects(db, world.contexts["owner"]) == []
    assert [
        a.project.public_id for a in member_projects(db, world.contexts["owner"], include_archived=True)
    ] == [world.project.public_id]
    with pytest.raises(RoleForbidden):
        set_archived(db, editor, False)
    assert set_archived(db, owner, False) is True
    update_project(db, editor, {"title": "x"})


def test_delete_removes_everything_and_then_404s(db: Session, world: World) -> None:
    project_id = world.project.public_id
    editor = authorize(db, world.contexts["editor"], project_id, "read")
    with pytest.raises(RoleForbidden):
        delete_project(db, editor)
    delete_project(db, authorize(db, world.contexts["owner"], project_id, "delete"))
    db.commit()
    for name in (
        "private_project",
        "private_project_member",
        "private_project_item",
        "private_project_activity",
    ):
        assert db.execute(sa.select(sa.func.count()).select_from(Base.metadata.tables[name])).scalar() == 0
    with pytest.raises(ProblemError) as excinfo:
        authorize(db, world.contexts["owner"], project_id, "read")
    assert excinfo.value.status == 404


def test_transfer_keeps_one_owner_and_the_old_owner_stays_as_editor(db: Session, world: World) -> None:
    owner = authorize(db, world.contexts["owner"], world.project.public_id, "manage_members")
    viewer = world.users["viewer"]
    transfer_ownership(db, owner, viewer.public_id, now=NOW)
    db.commit()
    roles = {
        m.user_public_id: m.role
        for m in project_members(db, authorize(db, world.contexts["viewer"], world.project.public_id, "read"))
    }
    assert roles[viewer.public_id] == "owner" and roles[world.users["owner"].public_id] == "editor"
    assert list(roles.values()).count("owner") == 1
    with pytest.raises(RoleForbidden):
        authorize(db, world.contexts["owner"], world.project.public_id, "manage_members")
    new_owner = authorize(db, world.contexts["viewer"], world.project.public_id, "manage_members")
    with pytest.raises(ProblemError):
        transfer_ownership(db, new_owner, viewer.public_id)
    with pytest.raises(ProblemError) as excinfo:
        transfer_ownership(db, new_owner, world.users["non_member"].public_id)
    assert excinfo.value.status == 404
    activity = (
        db.execute(
            sa.select(PrivateProjectActivity).where(PrivateProjectActivity.action == "ownership_transferred")
        )
        .scalars()
        .one()
    )
    assert activity.detail == {"from_user_id": world.users["owner"].public_id, "to_user_id": viewer.public_id}


def test_a_transfer_rereads_the_owner_under_the_project_lock(db: Session, world: World) -> None:
    """Two transfers sent at once both pass `authorize`. The second waits on the project's lock;
    when it runs, the first has already made its caller an editor, so it is refused instead of
    breaking the one-owner index with an `IntegrityError`."""
    owner = authorize(db, world.contexts["owner"], world.project.public_id, "manage_members")
    # What the first transfer committed, unseen by this session's copy of the caller's row.
    db.execute(
        sa.update(PrivateProjectMember)
        .where(PrivateProjectMember.id == owner.member.id)
        .values(role="editor")
        .execution_options(synchronize_session=False)
    )
    db.execute(
        sa.update(PrivateProjectMember)
        .where(
            PrivateProjectMember.project_id == world.project.id,
            PrivateProjectMember.user_id == world.users["editor"].id,
        )
        .values(role="owner")
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(RoleForbidden):
        transfer_ownership(db, owner, world.users["viewer"].public_id, now=NOW)
    roles = dict(
        db.execute(
            sa.select(PrivateProjectMember.user_id, PrivateProjectMember.role).where(
                PrivateProjectMember.project_id == world.project.id
            )
        ).all()
    )
    assert roles[world.users["editor"].id] == "owner" and roles[world.users["viewer"].id] == "viewer"


def test_roles_change_and_members_leave(db: Session, world: World) -> None:
    project_id = world.project.public_id
    owner = authorize(db, world.contexts["owner"], project_id, "manage_members")
    editor_user, viewer_user = world.users["editor"], world.users["viewer"]
    assert change_member_role(db, owner, viewer_user.public_id, "editor").role == "editor"
    for bad_role in ("owner", "admin"):
        with pytest.raises(ProblemError):
            change_member_role(db, owner, viewer_user.public_id, bad_role)
    with pytest.raises(ProblemError):
        change_member_role(db, owner, world.users["owner"].public_id, "viewer")
    editor = authorize(db, world.contexts["editor"], project_id, "read")
    with pytest.raises(RoleForbidden):
        change_member_role(db, editor, viewer_user.public_id, "viewer")
    with pytest.raises(RoleForbidden):
        remove_member(db, editor, viewer_user.public_id)
    remove_member(db, editor, editor_user.public_id)  # removing yourself is leaving
    with pytest.raises(ProblemError):
        authorize(db, world.contexts["editor"], project_id, "read")
    viewer = authorize(db, world.contexts["viewer"], project_id, "leave")
    leave_project(db, viewer)
    with pytest.raises(RoleForbidden):
        remove_member(db, owner, world.users["owner"].public_id)
    remaining = (
        db.execute(
            sa.select(PrivateProjectMember.role).where(PrivateProjectMember.project_id == world.project.id)
        )
        .scalars()
        .all()
    )
    assert "owner" in remaining and "viewer" not in remaining


def test_member_projects_lists_only_the_callers_memberships(
    db: Session, world: World, make_user: UserFactory
) -> None:
    other_owner = make_user()
    create_project(db, session_ctx(other_owner), fields={"title": "Not yours"})
    listed = member_projects(db, world.contexts["viewer"])
    assert [a.project.public_id for a in listed] == [world.project.public_id]
    assert [(a.role, a.action, a.user.id) for a in listed] == [("viewer", "read", world.users["viewer"].id)]
    for name in OUTSIDERS:
        assert member_projects(db, world.contexts[name]) == []


def test_private_operations_write_no_event(db: Session, world: World) -> None:
    """docs/19 §4.1: private work never touches `event` (which feeds the public feed, alerts,
    webhooks and social drafting) or the admin audit log, which shares that table."""
    before = db.execute(sa.select(sa.func.count()).select_from(Event)).scalar()
    project_id = world.project.public_id
    owner = authorize(db, world.contexts["owner"], project_id, "edit")
    update_project(db, owner, {"title": "Renamed"})
    change_member_role(db, owner, world.users["viewer"].public_id, "editor")
    transfer_ownership(db, owner, world.users["editor"].public_id)
    new_owner = authorize(db, world.contexts["editor"], project_id, "archive")
    set_archived(db, new_owner, True)
    set_archived(db, new_owner, False)
    delete_project(db, new_owner)
    db.flush()
    assert db.execute(sa.select(sa.func.count()).select_from(Event)).scalar() == before


SENTINEL_TITLE = "SENTINEL secret\x00deal"


@pytest.mark.parametrize(
    "title", [SENTINEL_TITLE, "SENTINEL\x7fdeal", "SENTINEL \ud800 deal", "SENTINEL\x1b[2Jdeal", "\x00"]
)
def test_a_title_a_database_cannot_store_is_refused_before_any_write(
    db: Session, make_user: UserFactory, world: World, title: str
) -> None:
    """Postgres refuses NUL in `text` with a `DataError` whose message lists the statement's
    parameters (the title, the coordinates): a 500 that would carry them into the app log and the
    error report. Validation refuses it first, with a 400 that repeats nothing."""
    creator = make_user()
    before = db.execute(sa.select(sa.func.count()).select_from(PrivateProject)).scalar()
    with pytest.raises(ProblemError) as excinfo:
        create_project(
            db, session_ctx(creator), fields={"title": title, "lat": "12.345678", "lon": "-1.5"}, now=NOW
        )
    assert (excinfo.value.code, excinfo.value.status) == ("validation_error", 400)
    assert "SENTINEL" not in str(excinfo.value.detail) and "12.345678" not in str(excinfo.value.errors)
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProject)).scalar() == before
    editor = authorize(db, world.contexts["editor"], world.project.public_id, "edit")
    with pytest.raises(ProblemError) as excinfo:
        update_project(db, editor, {"title": title})
    assert excinfo.value.status == 400 and world.project.title == "Abilene solar"


@pytest.mark.parametrize(
    "origin_type, origin_public_id",
    [
        ("proposal", "prop_1"),
        ("proposal", "opp_0000000001"),
        ("proposal", "prop_0000000001\x00"),
        ("proposal", "SENTINEL secret deal"),
        ("organization", "prop_0000000001"),
        ("corridor", "has spaces"),
        ("corridor", "x" * 65),
    ],
)
def test_the_origin_must_have_the_shape_of_its_record_types_id(
    db: Session, make_user: UserFactory, origin_type: str, origin_public_id: str
) -> None:
    ctx = session_ctx(make_user())
    with pytest.raises(ProblemError) as excinfo:
        create_project(
            db,
            ctx,
            fields={"title": "From a record"},
            origin_type=origin_type,
            origin_public_id=origin_public_id,
        )
    assert (excinfo.value.code, excinfo.value.status) == ("validation_error", 400)
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProject)).scalar() == 0


def test_a_well_formed_origin_is_recorded(db: Session, make_user: UserFactory) -> None:
    for origin_type, origin_public_id in (
        ("proposal", "prop_0000000001"),
        ("interconnection_point", "poi_0000000ABC"),
        ("corridor", "ercot-138kv-0042"),
    ):
        access = create_project(
            db,
            session_ctx(make_user()),
            fields={"title": "From a record"},
            origin_type=origin_type,
            origin_public_id=origin_public_id,
        )
        assert (access.project.origin_type, access.project.origin_public_id) == (
            origin_type,
            origin_public_id,
        )


def test_batch_readers_take_accesses_never_ids(db: Session, world: World, make_user: UserFactory) -> None:
    """`member_counts` and `checklist.progress_by_project` read private rows for many projects at
    once, so they take the accesses `authorize` or `member_projects` returned: an id from a
    request has no route in."""
    from services.projects.checklist import progress_by_project

    theirs = create_project(db, session_ctx(make_user()), fields={"title": "Someone else's"})
    for reader in (authz.member_counts, progress_by_project):
        with pytest.raises(TypeError):
            reader(db, [theirs.project.id])  # type: ignore[list-item]
        with pytest.raises(TypeError):
            reader(db, [theirs.project.public_id])  # type: ignore[list-item]
    mine = member_projects(db, world.contexts["viewer"])
    assert authz.member_counts(db, mine) == {world.project.id: 6}
    assert set(progress_by_project(db, mine)) == {world.project.id}
    assert authz.member_counts(db, []) == {}


def test_members_are_listed_without_email_addresses(db: Session, world: World) -> None:
    """docs/19 §5.5: other members' addresses are never shown or exported, so the core does not
    hand them to a route at all."""
    viewer = authorize(db, world.contexts["viewer"], world.project.public_id, "read")
    members = project_members(db, viewer)
    assert {f.name for f in dataclasses.fields(authz.MemberView)} == {
        "user_public_id",
        "name",
        "role",
        "added_at",
    }
    assert [m.role for m in members][:1] == ["owner"]
    assert world.users["owner"].public_id in {m.user_public_id for m in members}
    assert "@" not in repr(members)


def test_the_private_tables_use_random_primary_keys(db: Session, world: World) -> None:
    """Item, link and evidence ids reach URLs and activity detail; a time-ordered uuid7 would date
    each row, so every private row's key is a random uuid4."""
    item = (
        db.execute(sa.select(PrivateProjectItem).where(PrivateProjectItem.project_id == world.project.id))
        .scalars()
        .first()
    )
    assert item is not None and item.id.version == 4
    assert world.project.id.version == 4
    member = db.execute(sa.select(PrivateProjectMember)).scalars().first()
    activity = db.execute(sa.select(PrivateProjectActivity)).scalars().first()
    assert member is not None and member.id.version == 4
    assert activity is not None and activity.id.version == 4
