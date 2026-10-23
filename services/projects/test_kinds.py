"""The tracker's item types (decision row 2026-10-23, "My pipeline is one central tracker";
`services/projects/kinds.py`): each kind's stages, its checklist pack, a bid's project and
submitted date, and the member's own view of an item (last visit, "email me changes")."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.api.errors import ProblemError
from services.projects import checklist, kinds
from services.projects.authz import (
    PARENT_REFUSED,
    authorize,
    create_project,
    delete_project,
    mark_viewed,
    member_projects,
    readable_bids,
    readable_parent,
    set_archived,
    set_email_changes,
    update_project,
)
from services.projects.conftest import UserFactory, session_ctx
from services.projects.models import (
    PrivateProject,
    PrivateProjectActivity,
    PrivateProjectItem,
    PrivateProjectMember,
)

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)


def _status(excinfo: pytest.ExceptionInfo[ProblemError]) -> tuple[int, str]:
    return excinfo.value.status, str(excinfo.value.detail)


# -------------------------------------------------------------------------------- vocabularies
def test_each_kind_has_its_own_stages_and_no_stage_is_shared() -> None:
    assert kinds.ITEM_KINDS == ("project", "bid", "investment")
    assert kinds.PROJECT_STAGES == (
        "prospecting",
        "site_control",
        "development",
        "ready_to_build",
        "construction",
        "operating",
    )
    assert kinds.BID_STAGES == (
        "considering",
        "preparing",
        "submitted",
        "shortlisted",
        "won",
        "lost",
        "withdrawn",
    )
    assert kinds.INVESTMENT_STAGES == (
        "screening",
        "diligence",
        "term_sheet",
        "committed",
        "closed",
        "passed",
    )
    assert len(kinds.ALL_STAGES) == len(set(kinds.ALL_STAGES)) == 19
    assert set(kinds.STAGE_LABELS) == set(kinds.ALL_STAGES)
    assert {kind: stages[0] for kind, stages in kinds.KIND_STAGES.items()} == dict(kinds.DEFAULT_STAGES)
    for kind, closed in kinds.CLOSED_STAGES.items():
        assert closed <= set(kinds.KIND_STAGES[kind])
    assert kinds.CLOSED_STAGES["bid"] == {"won", "lost", "withdrawn"}
    assert kinds.CLOSED_STAGES["investment"] == {"closed", "passed"}
    assert not {"verified", "certified"} & set(kinds.ALL_STAGES)


def test_track_creates_a_bid_from_a_notice_and_a_project_or_investment_from_a_project() -> None:
    assert kinds.TRACK_KINDS["opportunity"] == ("bid",)
    assert kinds.TRACK_KINDS["proposal"] == ("project", "investment")
    for record_type in ("site", "asset", "interconnection_point"):
        assert kinds.TRACK_KINDS[record_type] == ("project",)


@pytest.mark.parametrize(
    ("kind", "stage", "allowed"),
    [
        ("project", "development", True),
        ("bid", "submitted", True),
        ("investment", "term_sheet", True),
        ("bid", "development", False),
        ("project", "won", False),
        ("investment", "preparing", False),
        ("portfolio", "prospecting", False),
    ],
)
def test_the_database_ties_each_stage_to_its_kind(db: Session, kind: str, stage: str, allowed: bool) -> None:
    project = PrivateProject(title="x", kind=kind, stage=stage)
    if allowed:
        db.add(project)
        db.flush()
        return
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        db.add(project)
        db.flush()


# ------------------------------------------------------------------------------------ create
def test_a_new_item_takes_its_kinds_first_stage_and_its_kinds_checklist(
    db: Session, make_user: UserFactory
) -> None:
    ctx = session_ctx(make_user())
    made = {
        kind: create_project(db, ctx, fields={"title": kind, "kind": kind}, now=NOW).project
        for kind in kinds.ITEM_KINDS
    }
    assert {kind: p.stage for kind, p in made.items()} == dict(kinds.DEFAULT_STAGES)

    def keys(project: PrivateProject) -> list[str | None]:
        rows = db.execute(
            sa.select(PrivateProjectItem.template_key)
            .where(PrivateProjectItem.project_id == project.id)
            .order_by(PrivateProjectItem.sort_order)
        )
        return list(rows.scalars())

    assert keys(made["bid"]) == [item.key for item in checklist.bid_pack().items]
    assert len(keys(made["bid"])) == 8
    assert len(keys(made["project"])) == len(keys(made["investment"])) == 32
    created = db.execute(
        sa.select(PrivateProjectActivity.detail).where(PrivateProjectActivity.project_id == made["bid"].id)
    ).scalar_one()
    assert created == {"kind": "bid"}


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "portfolio"},
        {"kind": "bid", "stage": "development"},
        {"kind": "project", "stage": "won"},
        {"kind": "investment", "stage": "considering"},
        {"kind": "project", "submitted_on": "2026-10-01"},
        {"kind": "investment", "parent_project_id": "prj_0000000000"},
        {"kind": "bid", "submitted_on": "20 Oct 2026"},
        {"kind": "bid", "submitted_on": "2026-10-20T10:00:00"},
        {"kind": "bid", "submitted_on": 20261020},
        {"kind": "bid", "parent_project_id": "not-an-id"},
    ],
)
def test_create_refuses_a_stage_or_a_bid_field_outside_its_kind(
    db: Session, make_user: UserFactory, fields: dict[str, object]
) -> None:
    with pytest.raises(ProblemError) as excinfo:
        create_project(db, session_ctx(make_user()), fields={"title": "x", **fields}, now=NOW)
    assert excinfo.value.status == 400
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProject)).scalar() == 0


def test_a_bid_attaches_only_to_a_project_the_caller_can_edit_with_one_refusal(
    db: Session, make_user: UserFactory
) -> None:
    owner, partner, outsider = make_user(), make_user(), make_user()
    mine = create_project(db, session_ctx(owner), fields={"title": "Mine"}, now=NOW).project
    theirs = create_project(db, session_ctx(outsider), fields={"title": "Theirs"}, now=NOW).project
    shared = create_project(db, session_ctx(partner), fields={"title": "Shared read-only"}, now=NOW).project
    db.add(PrivateProjectMember(project_id=shared.id, user_id=owner.id, role="viewer", added_at=NOW))
    archived = create_project(db, session_ctx(owner), fields={"title": "Archived"}, now=NOW)
    set_archived(db, archived, True, now=NOW)
    investment = create_project(
        db, session_ctx(owner), fields={"title": "Fund", "kind": "investment"}, now=NOW
    )
    db.flush()
    ctx = session_ctx(owner)

    refusals = {}
    for name, parent_id in (
        ("someone else's", theirs.public_id),
        ("read-only", shared.public_id),
        ("archived", archived.project.public_id),
        ("unknown", "prj_0000000000000000000000000"),
    ):
        with pytest.raises(ProblemError) as excinfo:
            create_project(
                db, ctx, fields={"title": "Bid", "kind": "bid", "parent_project_id": parent_id}, now=NOW
            )
        refusals[name] = _status(excinfo)
    assert set(refusals.values()) == {(400, PARENT_REFUSED)}, refusals
    with pytest.raises(ProblemError) as excinfo:
        create_project(
            db,
            ctx,
            fields={"title": "Bid", "kind": "bid", "parent_project_id": investment.project.public_id},
            now=NOW,
        )
    assert excinfo.value.status == 400

    bid = create_project(
        db,
        ctx,
        fields={
            "title": "Bid",
            "kind": "bid",
            "parent_project_id": mine.public_id,
            "submitted_on": "2026-10-20",
        },
        now=NOW,
    )
    second = create_project(
        db, ctx, fields={"title": "Bid 2", "kind": "bid", "parent_project_id": mine.public_id}
    )
    assert bid.project.parent_project_id == mine.id and bid.project.submitted_on == dt.date(2026, 10, 20)
    project_access = authorize(db, ctx, mine.public_id, "read")
    assert {a.project.id for a in readable_bids(db, project_access)} == {bid.project.id, second.project.id}
    parent = readable_parent(db, bid)
    assert parent is not None and parent.project.id == mine.id

    # Deleting the project leaves its bids standing alone.
    delete_project(db, authorize(db, ctx, mine.public_id, "delete"))
    assert db.get(PrivateProject, bid.project.id).parent_project_id is None  # type: ignore[union-attr]


def test_a_bids_project_is_shown_only_to_members_of_that_project(db: Session, make_user: UserFactory) -> None:
    owner, partner = make_user(), make_user()
    project = create_project(db, session_ctx(owner), fields={"title": "Mine"}, now=NOW).project
    bid = create_project(
        db,
        session_ctx(owner),
        fields={"title": "Bid", "kind": "bid", "parent_project_id": project.public_id},
        now=NOW,
    ).project
    db.add(PrivateProjectMember(project_id=bid.id, user_id=partner.id, role="editor", added_at=NOW))
    db.flush()
    partner_view = authorize(db, session_ctx(partner), bid.public_id, "read")
    assert readable_parent(db, partner_view) is None
    # The project's other members never see a bid they are not members of.
    db.add(PrivateProjectMember(project_id=project.id, user_id=partner.id, role="viewer", added_at=NOW))
    other_bid = create_project(
        db,
        session_ctx(owner),
        fields={"title": "Private bid", "kind": "bid", "parent_project_id": project.public_id},
    ).project
    db.flush()
    seen = readable_bids(db, authorize(db, session_ctx(partner), project.public_id, "read"))
    assert [a.project.id for a in seen] == [bid.id]
    assert other_bid.id not in {a.project.id for a in seen}


# -------------------------------------------------------------------------------------- edit
def test_an_edit_keeps_the_kind_and_checks_the_stage_against_it(db: Session, make_user: UserFactory) -> None:
    ctx = session_ctx(make_user())
    bid = create_project(db, ctx, fields={"title": "Bid", "kind": "bid"}, now=NOW)
    project = create_project(db, ctx, fields={"title": "Project"}, now=NOW)
    with pytest.raises(ProblemError) as excinfo:
        update_project(db, bid, {"kind": "project"})
    assert excinfo.value.status == 400 and "kind is set when an item is created" in str(excinfo.value.detail)
    with pytest.raises(ProblemError):
        update_project(db, bid, {"stage": "development"})
    assert update_project(db, bid, {"stage": "submitted", "submitted_on": "2026-10-21"}, now=NOW) == [
        "stage",
        "submitted_on",
    ]
    assert update_project(db, bid, {"parent_project_id": project.project.public_id}) == ["parent_project_id"]
    assert bid.project.parent_project_id == project.project.id
    assert update_project(db, bid, {"parent_project_id": None, "submitted_on": None}) == [
        "parent_project_id",
        "submitted_on",
    ]
    with pytest.raises(ProblemError):
        update_project(db, project, {"submitted_on": "2026-10-21"})
    with pytest.raises(ProblemError):
        update_project(db, project, {"stage": "won"})
    history = db.execute(
        sa.select(PrivateProjectActivity.detail).where(
            PrivateProjectActivity.project_id == bid.project.id,
            PrivateProjectActivity.action == "field_changed",
        )
    ).scalars()
    for detail in history:
        assert set(detail) == {"fields"}


def test_last_visit_and_email_changes_are_the_members_own_and_never_in_the_history(
    db: Session, make_user: UserFactory
) -> None:
    owner, viewer = make_user(), make_user()
    access = create_project(db, session_ctx(owner), fields={"title": "Deal"}, now=NOW)
    db.add(PrivateProjectMember(project_id=access.project.id, user_id=viewer.id, role="viewer", added_at=NOW))
    db.flush()
    viewer_access = authorize(db, session_ctx(viewer), access.project.public_id, "read")
    assert viewer_access.member.last_viewed_at is None and viewer_access.member.email_changes is False
    stamped = mark_viewed(db, viewer_access, now=NOW + dt.timedelta(hours=1))
    assert set_email_changes(db, viewer_access, True) is True
    with pytest.raises(ProblemError):
        set_email_changes(db, viewer_access, "yes")
    owner_row = db.execute(
        sa.select(PrivateProjectMember).where(
            PrivateProjectMember.project_id == access.project.id, PrivateProjectMember.user_id == owner.id
        )
    ).scalar_one()
    assert owner_row.last_viewed_at is None and owner_row.email_changes is False
    viewer_row = db.get(PrivateProjectMember, viewer_access.member.id)
    assert viewer_row is not None and viewer_row.email_changes is True
    assert viewer_row.last_viewed_at is not None
    assert viewer_row.last_viewed_at.replace(tzinfo=dt.UTC) == stamped
    actions = db.execute(
        sa.select(PrivateProjectActivity.action).where(PrivateProjectActivity.project_id == access.project.id)
    ).scalars()
    assert list(actions) == ["created"]
    # An archived item still takes the member's own preference.
    set_archived(db, access, True, now=NOW)
    archived_view = authorize(db, session_ctx(viewer), access.project.public_id, "read")
    assert set_email_changes(db, archived_view, False) is False


def test_the_list_narrows_to_kinds(db: Session, make_user: UserFactory) -> None:
    ctx = session_ctx(make_user())
    for kind in ("project", "bid", "bid", "investment"):
        create_project(db, ctx, fields={"title": kind, "kind": kind}, now=NOW)
    assert len(member_projects(db, ctx)) == 4
    assert [a.project.kind for a in member_projects(db, ctx, kinds=["bid"])] == ["bid", "bid"]
    assert {a.project.kind for a in member_projects(db, ctx, kinds=["project", "investment"])} == {
        "project",
        "investment",
    }
    with pytest.raises(ValueError):
        member_projects(db, ctx, kinds=["portfolio"])


# ------------------------------------------------------------------------------------- packs
def test_the_bid_pack_is_public_names_only_and_shares_no_key_with_the_bankability_pack() -> None:
    pack = checklist.bid_pack()
    assert pack.pack_version == checklist.BID_PACK_VERSION == "bid-v1"
    assert [item.name for item in pack.items] == [
        "Notice of intent or registration",
        "Bid fee or deposit",
        "Site control evidence",
        "Interconnection status",
        "Pricing",
        "Credit support",
        "Required forms",
        "Submission confirmation",
    ]
    assert all(item.guidance is None for item in pack.items)
    assert not pack.keys() & checklist.generic_pack().keys()
    assert all(item.key.startswith("bid_") for item in pack.items)


def test_no_private_pack_may_take_a_public_packs_name() -> None:
    generic = checklist.generic_pack()
    for version in sorted(checklist.PUBLIC_PACK_VERSIONS):
        document = {
            "schema": checklist.PACK_SCHEMA_ID,
            "pack_version": version,
            "items": [{"key": "ppa", "folder": "contracts", "pillar": "counterparties", "name": "PPA"}],
        }
        with pytest.raises(checklist.PackError, match="reserved"):
            checklist.validate_pack(document, generic=generic)
