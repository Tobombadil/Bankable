"""Migration 0043 (the data-room tables) and their models: up and down on SQLite with the schema equal
to the models', the vocabularies frozen in the migration equal to the models', one head, downgrade
refused while a document is stored, and the tables' references (private tables and users only)."""

from __future__ import annotations

import importlib.util
import os
import pathlib
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from services.db.base import Base
from services.projects import models

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
MIGRATION_PATH = MIGRATIONS_DIR / "versions" / "0043_data_rooms.py"
TABLES = set(models.DATA_ROOM_TABLES)


def _config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0043", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(engine: sa.Engine) -> dict[str, Any]:
    inspector = sa.inspect(engine)
    shape: dict[str, Any] = {}
    for table in sorted(TABLES):
        shape[table] = {
            "columns": {(c["name"], bool(c["nullable"])) for c in inspector.get_columns(table)},
            "pk": inspector.get_pk_constraint(table)["name"],
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


def test_0043_freezes_the_models_vocabularies() -> None:
    migration = _migration()
    for name in (
        "DATA_ROOM_FOLDER_KEYS",
        "DOCUMENT_TYPES",
        "SCAN_STATUSES",
        "SCANNERS",
        "DOCUMENT_EVENT_ACTIONS",
        "REFUSAL_REASONS",
        "FOLDER_NAME_MAX_CHARS",
        "FILE_NAME_MAX_CHARS",
    ):
        assert getattr(migration, name) == getattr(models, name), name
    assert set(migration.TABLES) == TABLES
    assert migration.down_revision == "0042"


def test_one_head_and_0043_on_it() -> None:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "0043" in {rev.revision for rev in script.iterate_revisions(heads[0], "base")}


def test_0043_round_trip_matches_the_models_and_refuses_to_strand_documents(tmp_path: pathlib.Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migration_0043.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    from_models = _snapshot(engine)
    assert from_models["private_project_document"]["uniques"] == {
        ("uq_private_project_document_object_key", ("object_key",))
    }
    cfg = _config(url)
    command.stamp(cfg, "head")
    command.downgrade(cfg, "0042")
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    command.upgrade(cfg, "head")
    assert _snapshot(engine) == from_models

    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(sa.text("INSERT INTO private_project (id, public_id, title) VALUES ('p', 'prj_1', 't')"))
        folder = sa.text(
            "INSERT INTO private_project_folder (id, project_id, key, name, name_key) "
            "VALUES (:id, 'p', :key, 'F', :id)"
        )
        conn.execute(folder, {"id": "f", "key": "permits"})
        # One default of each kind per item; a key outside the vocabulary is refused.
        for row in ({"id": "g", "key": "permits"}, {"id": "h", "key": "misc"}):
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(folder, row)
        # One folder name per item in any case: the case-folded `name_key` is unique (row "f" has "f").
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO private_project_folder (id, project_id, key, name, name_key) "
                    "VALUES ('i', 'p', NULL, 'F', 'f')"
                )
            )
        insert = sa.text(
            "INSERT INTO private_project_document (id, project_id, folder_id, file_name, doc_type, "
            "size_bytes, sha256, object_key, key_fingerprint, wrapped_key, scan_status, scanner) VALUES "
            "(:id, 'p', 'f', 'a.pdf', :type, 10, :sha, :key, 'fp', x'00', :scan, :scanner)"
        )
        base = {"sha": "0" * 64, "type": "pdf", "scan": "not_scanned", "scanner": None}
        conn.execute(insert, base | {"id": "d1", "key": "data-rooms/" + "a" * 32})
        for bad in (
            {"type": "exe"},
            {"scan": "infected"},
            {"scan": "clean", "scanner": None},
            {"scan": "not_scanned", "scanner": "clamd"},
        ):
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(insert, base | bad | {"id": "d2", "key": "data-rooms/" + "b" * 32})
        event = sa.text(
            "INSERT INTO private_project_document_event (id, project_id, action, reason) "
            "VALUES (:id, 'p', :a, :r)"
        )
        conn.execute(event, {"id": "e1", "a": "upload_refused", "r": "archive"})
        for action, reason in (("uploaded", "archive"), ("upload_refused", None), ("printed", None)):
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(event, {"id": "e2", "a": action, "r": reason})

    with pytest.raises(RuntimeError, match="1 stored documents"):
        command.downgrade(cfg, "0042")
    assert TABLES <= set(sa.inspect(engine).get_table_names())
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM private_project_document"))
    command.downgrade(cfg, "0042")
    command.upgrade(cfg, "head")
    assert _snapshot(engine) == from_models
    engine.dispose()


def test_the_tables_reference_only_private_tables_and_users() -> None:
    allowed = {*models.PRIVATE_TABLES, *models.DATA_ROOM_TABLES, "user"}
    for name in models.DATA_ROOM_TABLES:
        for fk in Base.metadata.tables[name].foreign_keys:
            assert fk.column.table.name in allowed, (name, fk.target_fullname)


def test_every_name_fits_postgres_identifiers() -> None:
    for name in models.DATA_ROOM_TABLES:
        table = Base.metadata.tables[name]
        names = [table.name, *(c.name for c in table.columns)]
        names += [str(c.name) for c in table.constraints] + [str(i.name) for i in table.indexes]
        for identifier in names:
            assert len(identifier.encode()) <= 63, identifier


def test_no_column_holds_a_score_or_a_verification() -> None:
    for name in models.DATA_ROOM_TABLES:
        columns = {c.name for c in Base.metadata.tables[name].columns}
        assert not {c for c in columns if "score" in c or "certif" in c or "verified" in c}, name
