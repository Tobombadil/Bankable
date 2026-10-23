"""The one way any code reaches a private project (docs/19 §5.1; ADR 0009 decision 1).

`authorize(db, ctx, project_public_id, action)` denies by default. It returns a `ProjectAccess`
only when the signed-in user holds a role that allows `action` (docs/19 §2.6). Every other case
raises the **same** `404 not_found` problem an unknown id gets:

- no session, or a session for a disabled or anonymised user, or one without a verified email;
- an API key, even an `admin` one (v1 is session only);
- a non-member, or a removed member (membership is read on every call, never cached);
- an archived project, for anything but `read`, `archive` (which also unarchives) and `delete`;
- a malformed id (refused before any query).

A member whose role is below the action gets `403 forbidden_role` (`RoleForbidden`). A platform
operator or owner gets nothing extra: no admin route reads private projects, and this function
has no bypass.

**One query, either way.** Project and membership are read in one statement that joins on the
caller's user id, so a missing project and someone else's project take the same path and return
the same answer: there is no timing or body difference to tell them apart.

The membership mutations (create, edit, archive, delete, role change, removal, leave, transfer)
live here too, so the one-owner invariant and the role table have a single home. Each takes the
`ProjectAccess` that `authorize` returned for the matching action; none takes a project id from
a request body.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal, get_args

from fastapi import Depends, Path
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, get_auth_context
from services.api.deps import get_db
from services.api.errors import ProblemError
from services.db.models import User
from services.projects.models import (
    LINK_RECORD_TYPES,
    MAX_ACTIVE_PROJECTS_PER_USER,
    MEMBER_ROLES,
    POINT_PRECISIONS,
    PROJECT_FIELDS,
    PROJECT_ID_PREFIX,
    PROJECT_STAGES,
    PROJECT_TECHNOLOGIES,
    TITLE_MAX_CHARS,
    PrivateProject,
    PrivateProjectMember,
    add_activity,
    has_control_characters,
    is_private_public_id,
    is_record_public_id,
    utcnow,
)

Action = Literal["read", "edit", "invite", "manage_members", "delete", "leave", "archive"]
Role = Literal["owner", "editor", "viewer"]
ACTIONS: Final[tuple[str, ...]] = get_args(Action)

#: docs/19 §2.6, as a table. `archive` covers archive and unarchive (PATCH, owner only, §6.1).
#: An owner cannot `leave`: ownership must be transferred first.
ROLE_ACTIONS: Final[Mapping[str, frozenset[str]]] = {
    "owner": frozenset({"read", "edit", "invite", "manage_members", "delete", "archive"}),
    "editor": frozenset({"read", "edit", "leave"}),
    "viewer": frozenset({"read", "leave"}),
}
#: What an archived project still allows (docs/19 §5.1).
ARCHIVED_ACTIONS: Final = frozenset({"read", "archive", "delete"})

NOT_FOUND_TITLE: Final = "Not found"
#: The same words `services.api.errors.not_found` uses for every other gated or absent record.
NOT_FOUND_DETAIL: Final = "No such record is visible on this tier."


def project_not_found() -> ProblemError:
    """The one 404 for anything private the caller may not see. Sub-resources (a note, a link, an
    invitation in a project the caller can read) use it too, so their ids leak nothing either."""
    return ProblemError("not_found", NOT_FOUND_TITLE, detail=NOT_FOUND_DETAIL)


class RoleForbidden(ProblemError):
    """`403 forbidden_role` (docs/19 §5.1): a member whose role does not allow the action.

    `services.api.errors.ERROR_CODES` has no `forbidden_role` row yet: the coordinator adds it with
    the merge, beside the code in `api/openapi.yaml`'s Problem schema. Until then this class takes
    its 403 from the `forbidden_tier` row and replaces the code, so the body says `forbidden_role`
    either way and no route ever raises an unregistered code."""

    def __init__(self, *, action: str, role: str) -> None:
        needs = sorted(r for r, allowed in ROLE_ACTIONS.items() if action in allowed)
        detail = (
            "Transfer ownership before leaving this project."
            if action == "leave" and role == "owner"
            else f"This needs the {' or '.join(needs)} role in this project."
        )
        super().__init__("forbidden_tier", "Your role in this project does not allow this", detail=detail)
        self.code = "forbidden_role"
        self.action = action
        self.role = role


@dataclass(frozen=True)
class ProjectAccess:
    """What `authorize` hands a route: the project, the caller's membership and the action it was
    checked for. Writes take `project.id` from here, never from a request."""

    project: PrivateProject
    member: PrivateProjectMember
    user: User
    action: str

    @property
    def role(self) -> str:
        return self.member.role

    @property
    def project_id(self) -> _uuid.UUID:
        return self.project.id

    def allows(self, action: str) -> bool:
        return action in ROLE_ACTIONS[self.member.role]


# ------------------------------------------------------------------------------------- the gate
def workspace_user(ctx: AuthContext) -> User | None:
    """The signed-in user who may hold private projects, or `None`: a session (never an API key) for
    an active, non-anonymised user with a verified email (docs/19 §6.1)."""
    if ctx.api_key is not None or ctx.user is None:
        return None
    user = ctx.user
    if user.status != "active" or user.anonymised_at is not None:
        return None
    if user.email_verified_at is None or not user.email:
        return None
    return user


def require_workspace_user(ctx: AuthContext) -> User:
    """For the routes that name no project (list, create, pins, export, accept): `401` without a
    usable session, `403` until the email is verified (the alerts precedent's wording)."""
    if ctx.api_key is not None or ctx.user is None:
        raise ProblemError("unauthenticated", "A signed-in session is required")
    if ctx.user.status != "active" or ctx.user.anonymised_at is not None:
        raise ProblemError("unauthenticated", "A signed-in session is required")
    user = workspace_user(ctx)
    if user is None:
        raise ProblemError("forbidden_tier", "Verify your email address first")
    return user


def authorize(db: Session, ctx: AuthContext, project_public_id: str, action: Action) -> ProjectAccess:
    """The only way any code reaches a private project. Returns (project, member, role) when the
    signed-in user holds a role that allows `action` (docs/19 §2.6); raises the same `not_found`
    problem an unknown id gets in every other case, and `RoleForbidden` to a member whose role is
    too low."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    user = workspace_user(ctx)
    if user is None or not is_private_public_id(PROJECT_ID_PREFIX, project_public_id):
        raise project_not_found()
    row = db.execute(
        select(PrivateProject, PrivateProjectMember)
        .join(
            PrivateProjectMember,
            and_(
                PrivateProjectMember.project_id == PrivateProject.id, PrivateProjectMember.user_id == user.id
            ),
        )
        .where(PrivateProject.public_id == project_public_id)
        # Never trust a row this session loaded earlier: a removal must bite on the next call.
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise project_not_found()
    project, member = row
    if project.archived_at is not None and action not in ARCHIVED_ACTIONS:
        raise project_not_found()
    if action not in ROLE_ACTIONS[member.role]:
        raise RoleForbidden(action=action, role=member.role)
    return ProjectAccess(project=project, member=member, user=user, action=action)


class ProjectAccessDependency:
    """`Depends(project_access("edit"))` on every `/v1/my/projects/{project_id}` route (docs/19
    §5.1). A class rather than a closure so `services/api/test_my_projects.py` can find it on every
    route with `isinstance`."""

    def __init__(self, action: Action) -> None:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        self.action: Action = action

    def __call__(
        self,
        # `Path()`, never a query parameter: on a route without `{project_id}` the request fails
        # validation instead of reading a private id from a query string (which proxies log).
        project_id: Annotated[str, Path()],
        db: Annotated[Session, Depends(get_db)],
        ctx: Annotated[AuthContext, Depends(get_auth_context)],
    ) -> ProjectAccess:
        return authorize(db, ctx, project_id, self.action)

    def __repr__(self) -> str:
        return f"project_access({self.action!r})"


def project_access(action: Action) -> ProjectAccessDependency:
    return ProjectAccessDependency(action)


def member_projects(db: Session, ctx: AuthContext, *, include_archived: bool = False) -> list[ProjectAccess]:
    """The caller's projects, owned and shared, newest change first: the list form of the same
    rule (a member may read), for `GET /v1/my/projects` and the map pins. Each is a `read` access,
    as `authorize(…, "read")` would return it. Empty for anyone `authorize` would refuse."""
    user = workspace_user(ctx)
    if user is None:
        return []
    query = (
        select(PrivateProject, PrivateProjectMember)
        .join(PrivateProjectMember, PrivateProjectMember.project_id == PrivateProject.id)
        .where(PrivateProjectMember.user_id == user.id)
        .order_by(PrivateProject.updated_at.desc(), PrivateProject.id.desc())
        .execution_options(populate_existing=True)
    )
    if not include_archived:
        query = query.where(PrivateProject.archived_at.is_(None))
    return [
        ProjectAccess(project=project, member=member, user=user, action="read")
        for project, member in db.execute(query).all()
    ]


def granted_project_ids(accesses: Sequence[ProjectAccess]) -> list[_uuid.UUID]:
    """The project ids behind accesses that `authorize` or `member_projects` returned. The batch
    readers (`member_counts`, `checklist.progress_by_project`) take accesses, never bare ids, so
    no id from a request can reach them; anything else is a programming error."""
    ids: list[_uuid.UUID] = []
    for access in accesses:
        if not isinstance(access, ProjectAccess):
            raise TypeError("pass the ProjectAccess values authorize or member_projects returned")
        require_action(access, "read")
        ids.append(access.project.id)
    return ids


def lock_user(db: Session, user: User) -> None:
    """Serialises one user's quota checks (active projects, invitations a day): Postgres takes
    `FOR NO KEY UPDATE` on their `user` row until the transaction ends, so two concurrent requests
    cannot both pass a count and both insert. `NO KEY` never blocks the foreign-key checks of rows
    that reference the user. SQLite (development and tests) renders no lock."""
    db.execute(select(User.id).where(User.id == user.id).with_for_update(key_share=True))


def lock_project(db: Session, project: PrivateProject) -> None:
    """Serialises a project's invitation and ownership changes, as `lock_user` does a user's.
    Take `lock_user` first when both are needed."""
    db.execute(
        select(PrivateProject.id).where(PrivateProject.id == project.id).with_for_update(key_share=True)
    )


# ------------------------------------------------------------------------------------ the fields
#: The fields a user sets (docs/19 §2.1). `archived` is separate (`set_archived`).
EDITABLE_FIELDS: Final = PROJECT_FIELDS


def _invalid(field: str, message: str) -> ProblemError:
    return ProblemError(
        "validation_error", "Invalid request", detail=message, errors=[{"field": field, "message": message}]
    )


def _decimal(field: str, value: object, *, places: int, low: Decimal, high: Decimal) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise _invalid(field, f"{field} must be a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise _invalid(field, f"{field} must be a number") from None
    if not number.is_finite() or (isinstance(value, float) and not math.isfinite(value)):
        raise _invalid(field, f"{field} must be a number")
    if number < low or number > high:
        raise _invalid(field, f"{field} must be between {low} and {high}")
    return number.quantize(Decimal(1).scaleb(-places))


def _clean_fields(values: Mapping[str, object]) -> dict[str, Any]:
    """Validates and normalises the user-set fields present in `values`. Unknown keys are refused,
    so a body can never set an owner, an id, a timestamp or an origin through here."""
    unknown = sorted(set(values) - set(EDITABLE_FIELDS))
    if unknown:
        raise _invalid(unknown[0], f"{unknown[0]} cannot be set")
    clean: dict[str, Any] = {}
    if "title" in values:
        title = values["title"]
        if not isinstance(title, str) or not title.strip():
            raise _invalid("title", "title is required")
        title = " ".join(title.split())
        if len(title) > TITLE_MAX_CHARS:
            raise _invalid("title", f"title is at most {TITLE_MAX_CHARS} characters")
        if has_control_characters(title):
            raise _invalid("title", "title contains a control character")
        clean["title"] = title
    if "technology" in values:
        technology = values["technology"]
        if technology is not None and technology not in PROJECT_TECHNOLOGIES:
            raise _invalid("technology", "technology is not one of Inframation's classes")
        clean["technology"] = technology
    if "stage" in values:
        if values["stage"] not in PROJECT_STAGES:
            raise _invalid("stage", "stage is not one of " + ", ".join(PROJECT_STAGES))
        clean["stage"] = values["stage"]
    big = Decimal("999999999.999")
    if "capacity_mw" in values:
        clean["capacity_mw"] = _decimal(
            "capacity_mw", values["capacity_mw"], places=3, low=Decimal(0), high=big
        )
    if "storage_mwh" in values:
        clean["storage_mwh"] = _decimal(
            "storage_mwh", values["storage_mwh"], places=3, low=Decimal(0), high=big
        )
    if "lat" in values:
        clean["lat"] = _decimal("lat", values["lat"], places=6, low=Decimal(-90), high=Decimal(90))
    if "lon" in values:
        clean["lon"] = _decimal("lon", values["lon"], places=6, low=Decimal(-180), high=Decimal(180))
    if "point_precision" in values:
        precision = values["point_precision"]
        if precision is not None and precision not in POINT_PRECISIONS:
            raise _invalid("point_precision", "point_precision is not one of " + ", ".join(POINT_PRECISIONS))
        clean["point_precision"] = precision
    return clean


def _with_point(project: PrivateProject, clean: dict[str, Any]) -> dict[str, Any]:
    """`clean` plus the map point it leaves the project with, validated before anything is set (a
    refused edit must leave the row untouched). Both coordinates or neither; a moved pin is the
    user's own (`user`) unless a precision is given; no point, no precision."""
    lat = clean.get("lat", project.lat)
    lon = clean.get("lon", project.lon)
    if (lat is None) != (lon is None):
        raise _invalid("lat", "lat and lon are set together")
    if lat is None:
        precision = None
    elif clean.get("point_precision") is not None:
        precision = clean["point_precision"]
    elif lat != project.lat or lon != project.lon or project.point_precision is None:
        precision = "user"
    else:
        precision = project.point_precision
    return {**clean, "lat": lat, "lon": lon, "point_precision": precision}


# ------------------------------------------------------------------------------- the mutations
def create_project(
    db: Session,
    ctx: AuthContext,
    *,
    fields: Mapping[str, object],
    origin_type: str | None = None,
    origin_public_id: str | None = None,
    now: dt.datetime | None = None,
) -> ProjectAccess:
    """A new project, its owner membership, its checklist and a `created` activity row, in the
    caller's transaction. `fields` takes `EDITABLE_FIELDS` only (title required). `origin_*` names
    the record it was started from, already checked by the caller to be visible (docs/19 §1).
    At most `MAX_ACTIVE_PROJECTS_PER_USER` active owned projects per user."""
    from services.projects.checklist import instantiate_items

    user = require_workspace_user(ctx)
    if "title" not in fields:
        raise _invalid("title", "title is required")
    if (origin_type is None) != (origin_public_id is None):
        raise _invalid("from", "record_type and public_id are given together")
    if origin_type is not None and origin_type not in LINK_RECORD_TYPES:
        raise _invalid("from.record_type", "record_type is not a linkable record type")
    if origin_type is not None and not is_record_public_id(origin_type, origin_public_id):
        raise _invalid("from.public_id", "public_id is not an id of that record type")
    # Every check on the body before the first statement that takes a lock.
    clean = _clean_fields(fields)
    lock_user(db, user)
    active_owned = db.scalar(
        select(func.count())
        .select_from(PrivateProjectMember)
        .join(PrivateProject, PrivateProject.id == PrivateProjectMember.project_id)
        .where(
            PrivateProjectMember.user_id == user.id,
            PrivateProjectMember.role == "owner",
            PrivateProject.archived_at.is_(None),
        )
    )
    if (active_owned or 0) >= MAX_ACTIVE_PROJECTS_PER_USER:
        raise ProblemError(
            "conflict",
            "Project limit reached",
            detail=f"At most {MAX_ACTIVE_PROJECTS_PER_USER} active projects; archive or delete one first.",
        )
    now = now or utcnow()
    project = PrivateProject(
        origin_type=origin_type, origin_public_id=origin_public_id, created_at=now, updated_at=now
    )
    for name, value in _with_point(project, clean).items():
        setattr(project, name, value)
    db.add(project)
    db.flush()
    member = PrivateProjectMember(
        project_id=project.id, user_id=user.id, role="owner", added_by_user_id=user.id, added_at=now
    )
    db.add(member)
    db.flush()
    instantiate_items(db, project, now=now)
    detail = {"origin_type": origin_type, "origin_public_id": origin_public_id} if origin_type else None
    add_activity(db, project_id=project.id, actor_user_id=user.id, action="created", detail=detail, now=now)
    db.flush()
    return ProjectAccess(project=project, member=member, user=user, action="edit")


def require_action(access: ProjectAccess, action: str) -> None:
    """Defence in depth: every mutation re-checks the member's role and the archive rule itself, so
    a viewer's `ProjectAccess`, or one taken for `read` on an archived project, cannot be passed to
    a write it does not allow."""
    if not access.allows(action):
        raise RoleForbidden(action=action, role=access.role)
    if access.project.archived_at is not None and action not in ARCHIVED_ACTIONS:
        raise project_not_found()


def update_project(
    db: Session, access: ProjectAccess, changes: Mapping[str, object], *, now: dt.datetime | None = None
) -> list[str]:
    """Applies `changes` (`EDITABLE_FIELDS` only) and returns the names of the fields that changed.
    Records one `field_changed` row with the field names, never the values."""
    require_action(access, "edit")
    project = access.project
    clean = _clean_fields(changes)
    target = _with_point(project, clean) if {"lat", "lon", "point_precision"} & set(clean) else clean
    changed = [name for name, value in target.items() if getattr(project, name) != value]
    if not changed:
        return []
    for name in changed:
        setattr(project, name, target[name])
    now = now or utcnow()
    project.updated_at = now
    add_activity(
        db,
        project_id=project.id,
        actor_user_id=access.user.id,
        action="field_changed",
        detail={"fields": sorted(changed)},
        now=now,
    )
    db.flush()
    return sorted(changed)


def set_archived(
    db: Session, access: ProjectAccess, archived: bool, *, now: dt.datetime | None = None
) -> bool:
    """Archive or unarchive (owner). Returns whether anything changed."""
    require_action(access, "archive")
    project = access.project
    if (project.archived_at is not None) == archived:
        return False
    now = now or utcnow()
    project.archived_at = now if archived else None
    project.updated_at = now
    action = "archived" if archived else "unarchived"
    add_activity(db, project_id=project.id, actor_user_id=access.user.id, action=action, now=now)
    db.flush()
    return True


def delete_project(db: Session, access: ProjectAccess) -> None:
    """Permanent (owner). Every child row goes by `ON DELETE CASCADE`."""
    require_action(access, "delete")
    project_id = access.project.id
    db.flush()
    db.execute(delete(PrivateProject).where(PrivateProject.id == project_id))
    db.expire_all()


@dataclass(frozen=True)
class MemberView:
    """One member as the other members see them (docs/19 §2.6): no email address and no internal
    id, so a route that serialises it cannot leak either (§5.5: other members' emails are never
    shown or exported). The caller's own row and address are on `ProjectAccess`."""

    user_public_id: str
    name: str | None
    role: str
    added_at: dt.datetime


def project_members(db: Session, access: ProjectAccess) -> list[MemberView]:
    """The members, owner first, then in the order they joined. Any member may read them
    (docs/19 §2.6)."""
    require_action(access, "read")
    order = {role: index for index, role in enumerate(MEMBER_ROLES)}
    rows = db.execute(
        select(PrivateProjectMember.role, PrivateProjectMember.added_at, User.public_id, User.name)
        .join(User, User.id == PrivateProjectMember.user_id)
        .where(PrivateProjectMember.project_id == access.project.id)
        .order_by(PrivateProjectMember.added_at, PrivateProjectMember.id)
    ).all()
    views = [
        MemberView(user_public_id=public_id, name=name, role=role, added_at=added_at)
        for role, added_at, public_id, name in rows
    ]
    return sorted(views, key=lambda view: order[view.role])


def _member_by_user_public_id(
    db: Session, access: ProjectAccess, user_public_id: str
) -> PrivateProjectMember:
    member = db.execute(
        select(PrivateProjectMember)
        .join(User, User.id == PrivateProjectMember.user_id)
        .where(PrivateProjectMember.project_id == access.project.id, User.public_id == user_public_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if member is None:
        raise project_not_found()
    return member


def change_member_role(
    db: Session, access: ProjectAccess, user_public_id: str, role: str, *, now: dt.datetime | None = None
) -> PrivateProjectMember:
    """Owner sets another member to `editor` or `viewer`. Ownership moves only by
    `transfer_ownership`."""
    require_action(access, "manage_members")
    if role not in ("editor", "viewer"):
        raise _invalid("role", "role is editor or viewer; ownership moves by transfer")
    member = _member_by_user_public_id(db, access, user_public_id)
    if member.role == "owner":
        raise _invalid("role", "transfer ownership to change the owner's role")
    if member.role != role:
        add_activity(
            db,
            project_id=access.project.id,
            actor_user_id=access.user.id,
            action="role_changed",
            detail={"user_id": user_public_id, "from_role": member.role, "to_role": role},
            now=now,
        )
        member.role = role
        db.flush()
    return member


def remove_member(
    db: Session, access: ProjectAccess, user_public_id: str, *, now: dt.datetime | None = None
) -> None:
    """Owner removes another member, or a member removes themselves (`leave`). Access ends on the
    removed user's next request: nothing is cached in their session."""
    if user_public_id == access.user.public_id:
        leave_project(db, access, now=now)
        return
    require_action(access, "manage_members")
    member = _member_by_user_public_id(db, access, user_public_id)
    if member.role == "owner":  # pragma: no cover - the caller is the owner, and owners are unique
        raise _invalid("user_id", "the owner cannot be removed")
    db.delete(member)
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="member_removed",
        detail={"user_id": user_public_id},
        now=now,
    )
    db.flush()


def leave_project(db: Session, access: ProjectAccess, *, now: dt.datetime | None = None) -> None:
    """A member leaves. The owner cannot (`403 forbidden_role`): transfer first (docs/19 §2.6)."""
    require_action(access, "leave")
    project_id = access.project.id
    db.delete(access.member)
    add_activity(
        db,
        project_id=project_id,
        actor_user_id=access.user.id,
        action="member_removed",
        detail={"user_id": access.user.public_id},
        now=now,
    )
    db.flush()


def transfer_ownership(
    db: Session, access: ProjectAccess, user_public_id: str, *, now: dt.datetime | None = None
) -> PrivateProjectMember:
    """Owner hands the project to an existing member; the old owner becomes an editor. The demotion
    is flushed before the promotion, because the database allows one owner per project.

    The outgoing owner's open invitations are revoked and their addresses cleared: an invitation
    speaks for the owner who sent it, and the new owner sends their own (acceptance also refuses
    one whose sender no longer owns the project, `invitations._checked`)."""
    from services.projects.invitations import revoke_project_invitations

    require_action(access, "manage_members")
    lock_project(db, access.project)
    # Under the lock, read the caller's role afresh: a transfer that committed while this one
    # waited has already made them an editor.
    caller = _member_by_user_public_id(db, access, access.user.public_id)
    if caller.role != "owner":
        raise RoleForbidden(action="manage_members", role=caller.role)
    target = _member_by_user_public_id(db, access, user_public_id)
    if target.id == access.member.id:
        raise _invalid("user_id", "you already own this project")
    revoke_project_invitations(db, access, now=now)
    access.member.role = "editor"
    db.flush()
    target.role = "owner"
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="ownership_transferred",
        detail={"from_user_id": access.user.public_id, "to_user_id": user_public_id},
        now=now,
    )
    db.flush()
    return target


def member_counts(db: Session, accesses: Sequence[ProjectAccess]) -> dict[_uuid.UUID, int]:
    """Members per project (keyed by `access.project.id`), for lists and the detail header. Takes
    the accesses `authorize` or `member_projects` returned, never ids (`granted_project_ids`)."""
    project_ids = granted_project_ids(accesses)
    if not project_ids:
        return {}
    rows = db.execute(
        select(PrivateProjectMember.project_id, func.count())
        .where(PrivateProjectMember.project_id.in_(list(project_ids)))
        .group_by(PrivateProjectMember.project_id)
    ).all()
    return {project_id: int(count) for project_id, count in rows}
