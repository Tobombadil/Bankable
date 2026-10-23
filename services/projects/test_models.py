"""The private-project tables (docs/19 §4.1): vocabularies, constraints, cascades, ids, and the
activity guard. Run on SQLite with foreign keys on, as every store connection is."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.db.base import Base
from services.db.models import Event, User
from services.projects import models
from services.projects.conftest import UserFactory
from services.projects.models import (
    ITEM_STATUSES,
    PRIVATE_ID_PATTERN,
    PRIVATE_TABLES,
    PrivateProject,
    PrivateProjectActivity,
    PrivateProjectEvidence,
    PrivateProjectInvitation,
    PrivateProjectItem,
    PrivateProjectLink,
    PrivateProjectMember,
    PrivateProjectNote,
    add_activity,
    is_private_public_id,
    new_private_public_id,
)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def _project(db: Session, **fields: object) -> PrivateProject:
    project = PrivateProject(title=fields.pop("title", "Deal"), **fields)
    db.add(project)
    db.flush()
    return project


def _member(db: Session, project: PrivateProject, user: User, role: str) -> PrivateProjectMember:
    member = PrivateProjectMember(project_id=project.id, user_id=user.id, role=role)
    db.add(member)
    db.flush()
    return member


def test_item_status_vocabulary_has_no_verified(db: Session) -> None:
    """docs/19 §2.3: a status is the user's own statement; "verified" needs documents and a reviewer."""
    assert ITEM_STATUSES == ("not_started", "in_progress", "done", "not_applicable")
    assert not {"verified", "certified"} & set(ITEM_STATUSES)
    project = _project(db)
    db.add(PrivateProjectItem(project_id=project.id, name="PPA", folder="contracts", pillar="counterparties"))
    db.flush()
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        db.execute(sa.update(PrivateProjectItem).values(status="verified"))


def test_no_table_carries_a_score_or_a_certification() -> None:
    for name in (*PRIVATE_TABLES, "checklist_template_item"):
        columns = {c.name for c in Base.metadata.tables[name].columns}
        assert not {c for c in columns if "score" in c or "certif" in c or "verified" in c}, name


def test_every_name_fits_postgres_identifiers() -> None:
    """Postgres truncates nothing and refuses an identifier over 63 bytes; SQLite has no limit, so
    only this test (and the Postgres round trip) would notice."""
    for name in (*PRIVATE_TABLES, "checklist_template_item"):
        table = Base.metadata.tables[name]
        names = [table.name, *(c.name for c in table.columns)]
        names += [str(c.name) for c in table.constraints] + [str(i.name) for i in table.indexes]
        for identifier in names:
            assert len(identifier.encode()) <= 63, identifier


def test_technology_vocabulary_is_inframations_classes_plus_other() -> None:
    from pipeline.normalize import TECH_RULES

    assert set(models.PROJECT_TECHNOLOGIES) == {tech for _, tech, _ in TECH_RULES} | {"other"}
    assert "unknown" not in models.PROJECT_TECHNOLOGIES


def test_ownership_is_a_member_row_and_one_owner_per_project(db: Session, make_user: UserFactory) -> None:
    columns = {c.name for c in PrivateProject.__table__.columns}
    assert not {c for c in columns if "owner" in c or c.endswith("user_id")}
    project = _project(db)
    first, second = make_user(), make_user()
    _member(db, project, first, "owner")
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        _member(db, project, second, "owner")
    _member(db, project, second, "editor")
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        _member(db, project, second, "viewer")


def test_private_tables_reference_only_private_tables_and_users() -> None:
    """docs/19 §4.1: no foreign key into a public table, so merges, takedowns and source gating
    never cascade into private rows."""
    allowed = {*PRIVATE_TABLES, "user"}
    for name in PRIVATE_TABLES:
        for fk in Base.metadata.tables[name].foreign_keys:
            assert fk.column.table.name in allowed, (name, fk.target_fullname)
    link_targets = {fk.column.table.name for fk in PrivateProjectLink.__table__.foreign_keys}
    assert link_targets == {"private_project", "user"}


def test_deleting_a_project_cascades_to_everything_in_it(db: Session, make_user: UserFactory) -> None:
    user = make_user()
    project = _project(db)
    _member(db, project, user, "owner")
    item = PrivateProjectItem(project_id=project.id, name="PPA", folder="contracts", pillar="counterparties")
    db.add(item)
    db.flush()
    db.add_all(
        [
            PrivateProjectLink(
                project_id=project.id,
                record_type="proposal",
                record_public_id="prop_0000000001",
                relation="this_project",
            ),
            PrivateProjectEvidence(
                project_id=project.id,
                item_id=item.id,
                record_type="proposal",
                record_public_id="prop_0000000001",
                rule="queue_position",
            ),
            PrivateProjectNote(project_id=project.id, author_user_id=user.id, body="note"),
            PrivateProjectInvitation(
                project_id=project.id,
                email="x@example.org",
                email_hash="h",
                token_hash="0" * 64,
                role="viewer",
                invited_by_user_id=user.id,
                created_at=NOW,
                expires_at=NOW + dt.timedelta(days=7),
            ),
            PrivateProjectActivity(project_id=project.id, actor_user_id=user.id, action="created"),
        ]
    )
    db.flush()
    db.execute(sa.delete(PrivateProject).where(PrivateProject.id == project.id))
    for name in PRIVATE_TABLES:
        rows = sa.select(sa.func.count()).select_from(Base.metadata.tables[name])
        assert db.execute(rows).scalar() == 0, name
    assert db.get(User, user.id) is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"stage": "permitted"},
        {"technology": "unknown"},
        {"capacity_mw": -1},
        {"storage_mwh": -0.5},
        {"lat": 30, "point_precision": "user"},
        {"lat": 91, "lon": 0, "point_precision": "user"},
        {"lat": 0, "lon": 181, "point_precision": "user"},
        {"lat": 1, "lon": 1},
        {"point_precision": "user"},
        {"lat": 1, "lon": 1, "point_precision": "rooftop"},
        {"origin_type": "proposal"},
        {"origin_type": "event", "origin_public_id": "evt_0000000001"},
    ],
)
def test_project_constraints_refuse_bad_rows(db: Session, fields: dict[str, object]) -> None:
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        _project(db, **{"title": "Deal", **fields})


def test_project_constraints_accept_a_full_row(db: Session) -> None:
    project = _project(
        db,
        title="Solar near Abilene",
        technology="solar_storage",
        capacity_mw=200,
        storage_mwh=400,
        stage="site_control",
        lat=32.45,
        lon=-99.73,
        point_precision="county_centroid",
        origin_type="proposal",
        origin_public_id="prop_0000000001",
    )
    assert project.stage == "site_control"


@pytest.mark.parametrize(
    "link",
    [
        {"record_type": "proposal", "record_public_id": None, "relation": "this_project"},
        {"record_type": "corridor", "record_public_id": None, "relation": "candidate_site"},
        {
            "record_type": "proposal",
            "record_public_id": "prop_1",
            "relation": "this_project",
            "params": {"a": 1},
        },
        {"record_type": "event", "record_public_id": "evt_1", "relation": "nearby"},
        {"record_type": "asset", "record_public_id": "asset_1", "relation": "owns"},
    ],
)
def test_link_constraints(db: Session, link: dict[str, object]) -> None:
    project = _project(db)
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        db.add(PrivateProjectLink(project_id=project.id, **link))
        db.flush()


def test_corridor_link_by_params_and_duplicate_links(db: Session) -> None:
    project = _project(db)
    db.add(
        PrivateProjectLink(
            project_id=project.id, record_type="corridor", params={"kv": 138}, relation="candidate_site"
        )
    )
    db.add(
        PrivateProjectLink(
            project_id=project.id, record_type="asset", record_public_id="asset_1", relation="grid_connection"
        )
    )
    db.flush()
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        db.add(
            PrivateProjectLink(
                project_id=project.id,
                record_type="asset",
                record_public_id="asset_1",
                relation="grid_connection",
            )
        )
        db.flush()


def test_note_and_invitation_constraints(db: Session, make_user: UserFactory) -> None:
    user = make_user()
    project = _project(db)
    with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
        db.add(PrivateProjectNote(project_id=project.id, author_user_id=user.id, body="x" * 20_001))
        db.flush()
    db.add(PrivateProjectNote(project_id=project.id, author_user_id=None, body="x" * 20_000))
    db.flush()

    def invitation(**overrides: object) -> PrivateProjectInvitation:
        values: dict[str, object] = {
            "project_id": project.id,
            "email": "x@example.org",
            "email_hash": "h1",
            "token_hash": uuid.uuid4().hex * 2,
            "role": "viewer",
            "invited_by_user_id": user.id,
            "created_at": NOW,
            "expires_at": NOW + dt.timedelta(days=7),
        }
        values.update(overrides)
        return PrivateProjectInvitation(**values)

    db.add(invitation())
    db.flush()
    for bad in ({"role": "owner"}, {"expires_at": NOW}, {"email_hash": "h1"}):
        with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
            db.add(invitation(**{"email_hash": "h2", **bad}))
            db.flush()
    # A closed invitation (address cleared) no longer holds the address's one open slot.
    db.add(invitation(email=None, revoked_at=NOW))
    db.flush()


def test_private_public_ids_are_random_shaped_and_unrelated_to_the_key(db: Session) -> None:
    project = _project(db)
    assert is_private_public_id("prj", project.public_id)
    from services.ids import public_id

    assert project.public_id != public_id("prj", project.id)
    ids = {new_private_public_id("prj") for _ in range(200)}
    assert len(ids) == 200
    for prefix in ("prj", "nte", "inv"):
        value = new_private_public_id(prefix)
        assert PRIVATE_ID_PATTERN.search(f"seen {value} here")
        assert is_private_public_id(prefix, value)
    assert not is_private_public_id("prj", "prj_ILOU000000")
    assert not is_private_public_id("prj", "prop_0000000001")
    assert not is_private_public_id("prj", None)
    with pytest.raises(ValueError):
        new_private_public_id("prop")


def test_activity_detail_holds_ids_and_names_never_values(db: Session) -> None:
    project = _project(db)
    row = add_activity(
        db,
        project_id=project.id,
        actor_user_id=None,
        action="field_changed",
        detail={"fields": ["title", "stage"], "item_id": str(uuid.uuid4()), "rotated": True},
        now=NOW,
    )
    db.flush()
    assert row.detail["fields"] == ["title", "stage"]
    for bad in (
        {"title": "Secret deal"},
        {"body": "x"},
        {"email": "a@example.org"},
        {"fields": ["Secret deal name"]},
        {"user_id": "a@example.org"},
        {"record_public_id": "x" * 65},
        {"status": 3},
    ):
        with pytest.raises(ValueError):
            add_activity(db, project_id=project.id, actor_user_id=None, action="field_changed", detail=bad)
    with pytest.raises(ValueError):
        add_activity(db, project_id=project.id, actor_user_id=None, action="published")


@pytest.mark.parametrize(
    "detail",
    [
        # A one-word title or name used to pass the old single token pattern; each key now has its
        # own shape, so none of these does.
        {"fields": ["Abilene"]},
        {"fields": ["abilene"]},
        {"fields": []},
        {"status": "Abilene"},
        {"role": "Abilene"},
        {"relation": "abilene"},
        {"record_type": "abilene"},
        {"user_id": "Abilene"},
        {"user_id": "usr_abilene"},
        {"invitation_id": "Abilene"},
        {"note_id": "inv_0000000001"},
        {"item_id": "Abilene"},
        {"origin_public_id": "Abilene solar"},
        # A reference must match the record type beside it, which must be there.
        {"record_type": "proposal", "record_public_id": "Abilene"},
        {"record_type": "site", "record_public_id": "prop_0000000001"},
        {"record_public_id": "prop_0000000001"},
        {"origin_type": "proposal", "origin_public_id": "ercot-138kv-0042"},
        {"rotated": "yes"},
        {"template_key": "Power Purchase Agreement"},
        {"status": ["done"]},
    ],
)
def test_each_activity_key_takes_only_its_own_shape(db: Session, detail: dict[str, object]) -> None:
    project = _project(db)
    with pytest.raises(ValueError):
        add_activity(db, project_id=project.id, actor_user_id=None, action="field_changed", detail=detail)


def test_activity_keys_accept_what_the_core_writes(db: Session, make_user: UserFactory) -> None:
    project = _project(db)
    user = make_user()
    detail = {
        "fields": ("title", "checklist"),
        "item_id": str(uuid.uuid4()),
        "evidence_id": str(uuid.uuid4()),
        "link_id": str(uuid.uuid4()),
        "note_id": new_private_public_id("nte"),
        "invitation_id": new_private_public_id("inv"),
        "user_id": user.public_id,
        "from_user_id": user.public_id,
        "to_user_id": user.public_id,
        "status": "done",
        "role": "editor",
        "from_role": "viewer",
        "to_role": "editor",
        "kind": "bid",
        "record_type": "proposal",
        "record_public_id": "prop_0000000001",
        "origin_type": "corridor",
        "origin_public_id": "ercot-138kv-0042",
        "relation": "this_project",
        "template_key": "interconnection_agreement",
        "rotated": True,
    }
    assert set(detail) == models.ACTIVITY_DETAIL_KEYS
    row = add_activity(
        db, project_id=project.id, actor_user_id=user.id, action="field_changed", detail=detail
    )
    db.flush()
    assert row.detail["fields"] == ["title", "checklist"]


@pytest.mark.parametrize(
    "value, line_breaks_allowed",
    [
        ("a\x00b", False),
        ("\x01", False),
        ("tab\there", True),
        ("line\nbreak", True),
        ("cr\rhere", True),
        ("del\x7f", False),
        ("c1\x85", False),
        ("c1\x9f", False),
        ("esc\x1b[0m", False),
        ("lone \ud800 surrogate", False),
        ("lone \udfff surrogate", False),
    ],
)
def test_control_characters_are_found(value: str, line_breaks_allowed: bool) -> None:
    assert models.has_control_characters(value)
    assert models.has_control_characters(value, allow_line_breaks=True) is not line_breaks_allowed


@pytest.mark.parametrize(
    "value", ["Abilene solar", "Straße 7 · 120 MW", "Ñandú — 東京", "emoji 🌞 ok", "zero‍width joiner"]
)
def test_ordinary_text_has_no_control_characters(value: str) -> None:
    assert not models.has_control_characters(value)
    assert not models.has_control_characters(value, allow_line_breaks=True)


def test_record_public_ids_are_checked_by_shape_per_type() -> None:
    good = {
        "proposal": "prop_0000000001",
        "site": "site_0000000001",
        "asset": "asset_0000000001",
        "opportunity": "opp_0000000001",
        "organization": "org_0000000001",
        "interconnection_point": "poi_0000000001",
        "corridor": "ercot-138kv-0042",
    }
    assert set(good) == set(models.LINK_RECORD_TYPES)
    for record_type, value in good.items():
        assert models.is_record_public_id(record_type, value), record_type
    for record_type, value in (
        ("proposal", "opp_0000000001"),
        ("proposal", "prop_000000001"),  # nine digits
        ("proposal", "prop_000000000I"),  # not Crockford
        ("proposal", "prop_0000000001 "),
        ("proposal", "prop_0000000001\x00"),
        ("project", "prop_0000000001"),
        ("corridor", "-starts-with-a-dash"),
        ("corridor", "has space"),
        ("corridor", "x" * 65),
        ("proposal", None),
        (None, "prop_0000000001"),
    ):
        assert not models.is_record_public_id(record_type, value), (record_type, value)


def test_activity_vocabulary_matches_the_spec() -> None:
    assert len(models.ACTIVITY_ACTIONS) == 19
    assert {"created", "member_joined", "ownership_transferred", "exported"} <= set(models.ACTIVITY_ACTIONS)


def test_nothing_here_writes_event(db: Session) -> None:
    before = db.execute(sa.select(sa.func.count()).select_from(Event)).scalar()
    project = _project(db)
    add_activity(db, project_id=project.id, actor_user_id=None, action="created")
    db.flush()
    assert db.execute(sa.select(sa.func.count()).select_from(Event)).scalar() == before
