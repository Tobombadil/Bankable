"""Migrations 0040 (`private_*` tables, `checklist_template_item`) and 0042 (the tracker's kinds):
up and down on SQLite and on Postgres, and the migrations' schema equal to the models'.

SQLite: 0037/0038's shape. The store starts at the schema `Base.metadata` builds (which declares the
nine tables), is stamped `0040`, walked down (the tables go) and up again (they return), and the
inspector must then read exactly what the models create: columns, nullability, keys, indexes,
unique and check constraints, and foreign keys with their cascades.

Postgres: runs when `POSTGIS_TEST_URL` names a disposable database already at head (CI's
`migrations` job leaves one; the name must contain `test` or `ci`). It walks 0040 down and up, reads
the column types back, and drives the authorisation core and invitations through real Postgres
(`FOR UPDATE`, partial unique indexes, cascades) inside a transaction it rolls back.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from services.db.base import Base
from services.projects import kinds, models
from services.projects.models import PRIVATE_TABLES

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
MIGRATION_PATH = MIGRATIONS_DIR / "versions" / "0040_private_projects.py"
TABLES = {*PRIVATE_TABLES, "checklist_template_item"}
POSTGIS_URL = os.environ.get("POSTGIS_TEST_URL")
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def _config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0040", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _down_revision() -> str:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    revision = ScriptDirectory.from_config(cfg).get_revision("0040")
    assert revision is not None
    return str(revision.down_revision)


def _snapshot(engine: sa.Engine) -> dict[str, Any]:
    inspector = sa.inspect(engine)
    shape: dict[str, Any] = {}
    for table in sorted(TABLES):
        shape[table] = {
            "columns": {(c["name"], bool(c["nullable"])) for c in inspector.get_columns(table)},
            "pk": (
                inspector.get_pk_constraint(table)["name"],
                tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            ),
            "indexes": {
                (i["name"], bool(i["unique"]), tuple(i["column_names"])) for i in inspector.get_indexes(table)
            },
            "uniques": {
                (u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
            },
            "checks": {c["name"] for c in inspector.get_check_constraints(table)},
            "fks": {
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    (fk.get("options") or {}).get("ondelete"),
                )
                for fk in inspector.get_foreign_keys(table)
            },
        }
    return shape


# ------------------------------------------------------------------------------------------ SQLite
@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0040.db'}"


def test_0040_round_trip_matches_the_models_on_sqlite(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    from_models = _snapshot(engine)
    assert from_models["private_project_member"]["indexes"] >= {
        ("uq_private_project_member_one_owner", True, ("project_id",)),
    }
    cfg = _config(sqlite_url)
    command.stamp(cfg, "0040")

    command.downgrade(cfg, _down_revision())
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    assert "user" in sa.inspect(engine).get_table_names()

    command.upgrade(cfg, "0040")
    assert "kind" not in {column["name"] for column in sa.inspect(engine).get_columns("private_project")}
    # 0041 (prices) and 0042 (the tracker's kinds) bring the tables to today's models.
    command.upgrade(cfg, "head")
    assert _snapshot(engine) == from_models

    # The partial unique indexes behave on SQLite as the models declare.
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(sa.text("INSERT INTO private_project (id, public_id, title) VALUES ('p', 'prj_1', 't')"))
        insert = sa.text(
            "INSERT INTO private_project_member (id, project_id, user_id, role) VALUES (:id, 'p', :u, :role)"
        )
        conn.execute(insert, {"id": "m1", "u": "u1", "role": "owner"})
        conn.execute(insert, {"id": "m2", "u": "u2", "role": "editor"})
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(insert, {"id": "m3", "u": "u3", "role": "owner"})
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO private_project_item (id, project_id, name, folder, pillar, status) "
                    "VALUES ('i', 'p', 'PPA', 'contracts', 'counterparties', 'verified')"
                )
            )
    command.downgrade(cfg, _down_revision())
    command.upgrade(cfg, "head")
    assert TABLES <= set(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_0040_is_on_the_single_line_after_the_newest_numbered_revision() -> None:
    """One head, with 0040 on its line. Not "0040 is the head": 0041 (prices) revises 0040, and a
    pin on the head breaks whenever a later revision lands (tests/test_migration_0033.py)."""
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "0040" in {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    # 0038 on this lane's base; the coordinator relinks to 0039 when the site finder lands first.
    assert _down_revision() in ("0038", "0039")


def test_0040_freezes_the_models_vocabularies() -> None:
    migration = _migration()
    for name in (
        "PROJECT_STAGES",
        "PROJECT_TECHNOLOGIES",
        "POINT_PRECISIONS",
        "MEMBER_ROLES",
        "INVITATION_ROLES",
        "LINK_RECORD_TYPES",
        "LINK_RELATIONS",
        "CHECKLIST_FOLDERS",
        "ITEM_PILLARS",
        "ITEM_STATUSES",
        "EVIDENCE_STATES",
        "EVIDENCE_RULES",
        "ACTIVITY_ACTIONS",
        "TITLE_MAX_CHARS",
        "ITEM_NAME_MAX_CHARS",
        "NOTE_MAX_CHARS",
        "PARAMS_ABSENT",
    ):
        assert getattr(migration, name) == getattr(models, name), name
    assert set(migration.TABLES) == TABLES
    assert "verified" not in migration.ITEM_STATUSES


# --------------------------------------------------------------------------- 0042: the tracker's kinds
MIGRATION_0042_PATH = MIGRATIONS_DIR / "versions" / "0042_tracker_kinds.py"
#: What 0042 adds, by table.
ADDED_COLUMNS = {
    "private_project": {"kind", "parent_project_id", "submitted_on"},
    "private_project_member": {"last_viewed_at", "email_changes"},
}


def _migration_0042() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0042", MIGRATION_0042_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _0042_down() -> str:
    return str(_migration_0042().down_revision)


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def _checks(engine: sa.Engine, table: str) -> set[str]:
    return {check["name"] for check in sa.inspect(engine).get_check_constraints(table)}


def test_0042_freezes_the_kinds_and_their_stages() -> None:
    migration = _migration_0042()
    assert migration.ITEM_KINDS == kinds.ITEM_KINDS
    assert migration.PROJECT_STAGES == kinds.PROJECT_STAGES == models.PROJECT_STAGES
    assert migration.KIND_STAGES == dict(kinds.KIND_STAGES)
    # 0041 (prices) on this lane's base; the coordinator relinks it if the site finder's revision
    # lands on 0041 first, so the test follows whatever 0042 revises.
    assert migration.down_revision in ("0041", "0039")
    stage_check = dict(migration.NEW_CHECKS)["stage_for_kind"]
    assert stage_check == kinds.stage_for_kind_condition()


def test_0042_round_trip_on_sqlite_keeps_projects_and_refuses_to_drop_bids(sqlite_url: str) -> None:
    """Up from 0041 with a project in the table: it becomes a `project` and keeps its stage; the
    schema is then the models'. Each kind takes only its own stages; only a bid has a project or a
    submitted date; deleting a bid's project leaves the bid standing alone. Down is refused while a
    bid exists, and runs clean (0040's project-only stage check back) once none does."""
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    from_models = _snapshot(engine)
    cfg = _config(sqlite_url)
    command.stamp(cfg, "head")

    command.downgrade(cfg, _0042_down())
    for table, added in ADDED_COLUMNS.items():
        assert not added & _columns(engine, table), table
    checks = _checks(engine, "private_project")
    assert "ck_private_project_stage_vocab" in checks and "ck_private_project_stage_for_kind" not in checks

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO private_project (id, public_id, title, stage) "
                "VALUES ('p', 'prj_1', 't', 'development')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO private_project_member (id, project_id, user_id, role) "
                "VALUES ('m', 'p', 'u', 'owner')"
            )
        )
    command.upgrade(cfg, "0042")
    assert _snapshot(engine) == from_models
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT kind, stage FROM private_project")).one() == (
            "project",
            "development",
        )
        assert conn.execute(
            sa.text("SELECT last_viewed_at, email_changes FROM private_project_member")
        ).one() == (
            None,
            0,
        )

    insert = sa.text(
        "INSERT INTO private_project (id, public_id, title, kind, stage, parent_project_id, submitted_on) "
        "VALUES (:id, :pid, 't', :kind, :stage, :parent, :submitted)"
    )

    def row(**values: object) -> dict[str, object]:
        n = values.pop("n")
        base: dict[str, object] = {"id": f"x{n}", "pid": f"prj_x{n}", "parent": None, "submitted": None}
        return {**base, **values}

    refused = [
        row(n=1, kind="bid", stage="development"),
        row(n=2, kind="project", stage="won"),
        row(n=3, kind="investment", stage="preparing"),
        row(n=4, kind="portfolio", stage="prospecting"),
        row(n=5, kind="project", stage="prospecting", parent="p"),
        row(n=6, kind="investment", stage="screening", submitted="2026-10-01"),
    ]
    for values in refused:
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(insert, values)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(insert, row(n=7, kind="bid", stage="considering"))
        conn.execute(sa.text("UPDATE private_project SET parent_project_id = id WHERE id = 'x7'"))

    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(insert, row(n=8, kind="bid", stage="submitted", parent="p", submitted="2026-10-20"))
        conn.execute(insert, row(n=9, kind="investment", stage="term_sheet"))
        conn.execute(sa.text("DELETE FROM private_project WHERE id = 'p'"))
        assert (
            conn.execute(sa.text("SELECT parent_project_id FROM private_project WHERE id = 'x8'")).scalar()
            is None
        )

    with pytest.raises(RuntimeError, match=r"bid=1, investment=1"):
        command.downgrade(cfg, _0042_down())
    assert _snapshot(engine) == from_models
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM private_project WHERE kind <> 'project'"))
    command.downgrade(cfg, _0042_down())
    for table, added in ADDED_COLUMNS.items():
        assert not added & _columns(engine, table), table
    assert "ck_private_project_stage_vocab" in _checks(engine, "private_project")
    assert "ix_private_project_parent_project_id" not in {
        index["name"] for index in sa.inspect(engine).get_indexes("private_project")
    }
    command.upgrade(cfg, "head")
    assert _snapshot(engine) == from_models
    engine.dispose()


# ---------------------------------------------------------------------------------------- Postgres
pg_only = pytest.mark.skipif(not POSTGIS_URL, reason="POSTGIS_TEST_URL not set")


@pytest.fixture()
def pg_engine() -> Iterator[sa.Engine]:
    assert POSTGIS_URL is not None
    name = make_url(POSTGIS_URL).database or ""
    if not {"test", "ci"} & set(name.lower().split("_")):
        raise RuntimeError(f"refusing database {name!r}: its name must contain `test` or `ci`")
    engine = sa.create_engine(POSTGIS_URL, future=True)
    head = ScriptDirectory.from_config(_config(POSTGIS_URL)).get_current_head()
    with engine.connect() as conn:
        current = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert current == head, f"migrate the test database first: at {current}, head is {head}"
    yield engine
    engine.dispose()


@pg_only
def test_0040_down_and_up_on_postgres(pg_engine: sa.Engine) -> None:
    cfg = _config(str(POSTGIS_URL))
    command.downgrade(cfg, _down_revision())
    assert not TABLES & set(sa.inspect(pg_engine).get_table_names())
    command.upgrade(cfg, "head")
    inspector = sa.inspect(pg_engine)
    assert TABLES <= set(inspector.get_table_names())
    with pg_engine.connect() as conn:
        types = {
            (row.table_name, row.column_name): (
                row.data_type,
                row.numeric_precision,
                row.numeric_scale,
                row.character_maximum_length,
            )
            for row in conn.execute(
                sa.text(
                    "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale, "
                    "character_maximum_length FROM information_schema.columns "
                    "WHERE table_name LIKE 'private_project%' OR table_name = 'checklist_template_item'"
                )
            )
        }
    assert types[("private_project", "id")][0] == "uuid"
    assert types[("private_project", "lat")][:3] == ("numeric", 9, 6)
    assert types[("private_project", "capacity_mw")][:3] == ("numeric", 12, 3)
    assert types[("private_project", "created_at")][0] == "timestamp with time zone"
    assert types[("private_project_link", "params")][0] == "jsonb"
    assert types[("private_project_activity", "detail")][0] == "jsonb"
    assert types[("checklist_template_item", "technologies")][0] == "ARRAY"
    assert types[("private_project_invitation", "token_hash")] == ("character", None, None, 64)
    model_columns = {
        (t.name, c.name) for t in Base.metadata.sorted_tables if t.name in TABLES for c in t.columns
    }
    assert set(types) == model_columns
    checks = {c["name"] for c in inspector.get_check_constraints("private_project_item")}
    assert "ck_private_project_item_status_vocab" in checks
    fks = {
        fk["referred_table"]: fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("private_project_member")
    }
    assert fks == {"private_project": "CASCADE", "user": None}


@pg_only
def test_the_core_runs_on_postgres(pg_engine: sa.Engine) -> None:
    from services.api.auth import register_user
    from services.api.errors import ProblemError
    from services.projects.authz import authorize, create_project, delete_project, transfer_ownership
    from services.projects.checklist import readiness, set_item_status
    from services.projects.conftest import TEST_PASSWORD, session_ctx
    from services.projects.invitations import accept_invitation, create_invitation
    from services.projects.models import PrivateProjectItem, PrivateProjectLink, PrivateProjectMember

    with pg_engine.connect() as conn:
        outer = conn.begin()
        db = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            users = []
            for email in ("pg-owner@example.org", "pg-partner@example.org"):
                user = register_user(db, email=email, password=TEST_PASSWORD)
                user.email_verified_at = NOW
                users.append(user)
            db.flush()
            owner, partner = users
            created = create_project(
                db,
                session_ctx(owner),
                fields={"title": "Postgres deal", "lat": "32.448736", "lon": -99.733144},
                now=NOW,
            )
            access = authorize(db, session_ctx(owner), created.project.public_id, "invite")
            assert str(access.project.lat) == "32.448736"
            issued = create_invitation(db, access, email=" PG-Partner@example.org", role="editor", now=NOW)
            joined = accept_invitation(db, session_ctx(partner), issued.token, now=NOW)
            assert joined.role == "editor"
            with pytest.raises(ProblemError):
                accept_invitation(db, session_ctx(partner), issued.token, now=NOW)
            item = (
                db.execute(
                    sa.select(PrivateProjectItem).where(PrivateProjectItem.project_id == access.project.id)
                )
                .scalars()
                .first()
            )
            assert item is not None
            set_item_status(db, joined, item.id, "done")
            assert readiness(db, joined)[0].done == 1
            transfer_ownership(
                db,
                authorize(db, session_ctx(owner), created.project.public_id, "manage_members"),
                partner.public_id,
            )
            roles = dict(
                db.execute(
                    sa.select(PrivateProjectMember.user_id, PrivateProjectMember.role).where(
                        PrivateProjectMember.project_id == access.project.id
                    )
                ).all()
            )
            assert roles == {owner.id: "editor", partner.id: "owner"}
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.add(PrivateProjectMember(project_id=access.project.id, user_id=owner.id, role="owner"))
                db.flush()
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.execute(
                    sa.update(PrivateProjectItem)
                    .where(PrivateProjectItem.id == item.id)
                    .values(status="verified")
                )
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.add(
                    PrivateProjectLink(
                        project_id=access.project.id, record_type="proposal", relation="nearby"
                    )
                )
                db.flush()
            db.add(
                PrivateProjectLink(
                    project_id=access.project.id,
                    record_type="corridor",
                    params={"kv": 138},
                    relation="candidate_site",
                )
            )
            db.flush()
            project_uuid = access.project.id
            delete_project(db, authorize(db, session_ctx(partner), created.project.public_id, "delete"))
            for table in PRIVATE_TABLES:
                rows = Base.metadata.tables[table]
                key = rows.c.id if table == "private_project" else rows.c.project_id
                count = db.execute(
                    sa.select(sa.func.count()).select_from(rows).where(key == project_uuid)
                ).scalar()
                assert count == 0, table
        finally:
            db.close()
            outer.rollback()


@pg_only
def test_text_postgres_cannot_store_is_refused_before_it_gets_there(pg_engine: sa.Engine) -> None:
    """Postgres `text` cannot hold NUL. The driver's `DataError` lists the statement's parameters
    (the title and the coordinates; an invitation's address), and a 500 carries that text into the
    app log and the error report. Validation refuses it first with a 400 that repeats nothing."""
    from services.api.auth import register_user
    from services.api.errors import ProblemError
    from services.projects.authz import authorize, create_project
    from services.projects.checklist import add_custom_item
    from services.projects.conftest import TEST_PASSWORD, session_ctx
    from services.projects.invitations import create_invitation

    with pg_engine.connect() as conn:
        outer = conn.begin()
        db = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            owner = register_user(db, email="pg-nul@example.org", password=TEST_PASSWORD)
            owner.email_verified_at = NOW
            db.flush()
            with pytest.raises(ProblemError) as excinfo:
                create_project(
                    db,
                    session_ctx(owner),
                    fields={"title": "SENTINEL secret\x00deal", "lat": "12.345678", "lon": "-1.5"},
                    now=NOW,
                )
            assert excinfo.value.status == 400 and "SENTINEL" not in str(excinfo.value.detail)
            created = create_project(db, session_ctx(owner), fields={"title": "Clean"}, now=NOW)
            access = authorize(db, session_ctx(owner), created.project.public_id, "invite")
            with pytest.raises(ProblemError) as excinfo:
                create_invitation(db, access, email="sentinel\x00@example.org", role="viewer", now=NOW)
            assert excinfo.value.status == 400 and "sentinel" not in str(excinfo.value.detail)
            with pytest.raises(ProblemError) as excinfo:
                add_custom_item(db, access, name="SENTINEL\x00item", folder="permits")
            assert excinfo.value.status == 400
            with pytest.raises(ProblemError) as excinfo:
                create_project(
                    db,
                    session_ctx(owner),
                    fields={"title": "From a record"},
                    origin_type="proposal",
                    origin_public_id="prop_0000000001\x00",
                )
            assert excinfo.value.status == 400
        finally:
            db.close()
            outer.rollback()


@pg_only
def test_a_concurrent_reinvite_waits_for_the_first_and_replaces_it(pg_engine: sa.Engine) -> None:
    """Two owners' tabs invite the same address at once. The second waits on the project's lock,
    then finds the first's open invitation and replaces it. Without the lock both read "no open
    invitation", and the second INSERT fails on the partial unique index with an `IntegrityError`
    whose message carries the address."""
    import threading
    import uuid

    from services.api.auth import register_user
    from services.projects.authz import authorize, create_project
    from services.projects.conftest import TEST_PASSWORD, session_ctx
    from services.projects.invitations import create_invitation
    from services.projects.models import PrivateProject, PrivateProjectInvitation

    tag = uuid.uuid4().hex[:10]
    address = f"race-{tag}@example.org"
    factory = sa.orm.sessionmaker(bind=pg_engine, expire_on_commit=False)
    with factory() as setup:
        owner = register_user(setup, email=f"race-owner-{tag}@example.org", password=TEST_PASSWORD)
        owner.email_verified_at = NOW
        setup.flush()
        project_public_id = create_project(
            setup, session_ctx(owner), fields={"title": "Race"}, now=NOW
        ).project.public_id
        setup.commit()
        owner_id, account_id = owner.id, owner.account_id

    first_holds_lock = threading.Event()
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with factory() as db:
                db.execute(sa.text("SET lock_timeout = '10s'"))
                user = db.get(type(owner), owner_id)
                assert user is not None
                access = authorize(db, session_ctx(user), project_public_id, "invite")
                create_invitation(db, access, email=address, role="viewer", now=NOW)
                first_holds_lock.set()
                # Commit only once the second request is waiting on a lock.
                with pg_engine.connect() as watcher:
                    for _ in range(200):
                        waiting = watcher.execute(
                            sa.text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                        ).scalar()
                        if waiting:
                            break
                        threading.Event().wait(0.05)
                    else:
                        raise AssertionError("the second request never waited on a lock")
                db.commit()
        except BaseException as exc:
            errors.append(exc)
            first_holds_lock.set()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert first_holds_lock.wait(30)
        with factory() as db:
            db.execute(sa.text("SET lock_timeout = '10s'"))
            user = db.get(type(owner), owner_id)
            assert user is not None
            access = authorize(db, session_ctx(user), project_public_id, "invite")
            second = create_invitation(db, access, email=address, role="editor", now=NOW)
            db.commit()
        thread.join(30)
        assert not errors, errors
        with factory() as check:
            project = check.execute(
                sa.select(PrivateProject).where(PrivateProject.public_id == project_public_id)
            ).scalar_one()
            rows = (
                check.execute(
                    sa.select(PrivateProjectInvitation).where(
                        PrivateProjectInvitation.project_id == project.id
                    )
                )
                .scalars()
                .all()
            )
            open_rows = [r for r in rows if r.revoked_at is None and r.accepted_at is None]
            assert [r.id for r in open_rows] == [second.invitation.id]
            assert len(rows) == 2 and open_rows[0].role == "editor"
    finally:
        thread.join(30)
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM private_project WHERE public_id = :p"), {"p": project_public_id}
            )
            conn.execute(sa.text('DELETE FROM "user" WHERE id = :u'), {"u": owner_id})
            conn.execute(sa.text("DELETE FROM account WHERE id = :a"), {"a": account_id})


@pg_only
def test_0042_down_and_up_on_postgres(pg_engine: sa.Engine) -> None:
    cfg = _config(str(POSTGIS_URL))
    command.downgrade(cfg, _0042_down())
    inspector = sa.inspect(pg_engine)
    for table, added in ADDED_COLUMNS.items():
        assert not added & {c["name"] for c in inspector.get_columns(table)}, table
    old_checks = {c["name"] for c in inspector.get_check_constraints("private_project")}
    assert "ck_private_project_stage_vocab" in old_checks
    command.upgrade(cfg, "head")
    inspector = sa.inspect(pg_engine)
    with pg_engine.connect() as conn:
        types = dict(
            conn.execute(
                sa.text(
                    "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                    "WHERE table_name IN ('private_project', 'private_project_member')"
                )
            ).all()
        )
    assert types["private_project.kind"] == "text"
    assert types["private_project.parent_project_id"] == "uuid"
    assert types["private_project.submitted_on"] == "date"
    assert types["private_project_member.last_viewed_at"] == "timestamp with time zone"
    assert types["private_project_member.email_changes"] == "boolean"
    checks = {c["name"] for c in inspector.get_check_constraints("private_project")}
    assert {
        "ck_private_project_kind_vocab",
        "ck_private_project_stage_for_kind",
        "ck_private_project_parent_bid_only",
        "ck_private_project_parent_not_self",
        "ck_private_project_submitted_bid_only",
    } <= checks
    assert "ck_private_project_stage_vocab" not in checks
    parent_fk = [
        fk
        for fk in inspector.get_foreign_keys("private_project")
        if fk["constrained_columns"] == ["parent_project_id"]
    ]
    assert [fk["options"].get("ondelete") for fk in parent_fk] == ["SET NULL"]


@pg_only
def test_bids_attach_to_projects_and_outlive_them_on_postgres(pg_engine: sa.Engine) -> None:
    from services.api.auth import register_user
    from services.api.errors import ProblemError
    from services.projects.authz import authorize, create_project, delete_project
    from services.projects.conftest import TEST_PASSWORD, session_ctx
    from services.projects.models import PrivateProject

    with pg_engine.connect() as conn:
        outer = conn.begin()
        db = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            owner = register_user(db, email="pg-tracker@example.org", password=TEST_PASSWORD)
            owner.email_verified_at = NOW
            db.flush()
            ctx = session_ctx(owner)
            project = create_project(db, ctx, fields={"title": "Site"}, now=NOW).project
            bid = create_project(
                db,
                ctx,
                fields={
                    "title": "Bid",
                    "kind": "bid",
                    "stage": "submitted",
                    "parent_project_id": project.public_id,
                    "submitted_on": "2026-10-20",
                },
                now=NOW,
            ).project
            assert bid.parent_project_id == project.id and str(bid.submitted_on) == "2026-10-20"
            with pytest.raises(ProblemError):
                create_project(db, ctx, fields={"title": "x", "kind": "bid", "stage": "development"}, now=NOW)
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.execute(
                    sa.update(PrivateProject).where(PrivateProject.id == project.id).values(stage="won")
                )
            delete_project(db, authorize(db, ctx, project.public_id, "delete"))
            reloaded = db.get(PrivateProject, bid.id)
            assert reloaded is not None and reloaded.parent_project_id is None
        finally:
            db.close()
            outer.rollback()
