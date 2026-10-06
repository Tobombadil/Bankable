"""Round-trip for migration 0031 (`webhook_endpoint.watermark_seq`; `idempotency_record`).

0030's shape: the store starts at the schema `Base.metadata` produces (which already has both), is
stamped `0031`, and is walked down and up again. The upgrade from a store without the column sets
existing endpoints to the head of the event log. The Postgres half is CI's `migrations` job.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from services.db.base import Base

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0031.db'}"


def _endpoint_columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("webhook_endpoint")}


def _seed(engine: sa.Engine) -> None:
    """One account, user and webhook endpoint, and three events, written through the ORM while the
    schema is the current one."""
    from sqlalchemy.orm import Session

    from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
    from services.db.models import WebhookEndpoint
    from tests.conftest import make_account, make_user

    with Session(engine) as db:
        account = make_account(db, entitlement="api")
        user = make_user(db, account)
        source = make_public_source(db, make_open_licence(db))
        prop = make_visible_proposal(db, source)
        for event_type in ("created", "status_change", "field_changed"):
            make_event(db, prop, source, event_type=event_type)
        db.add(
            WebhookEndpoint(
                public_id="whe_w",
                account_id=account.id,
                created_by_user_id=user.id,
                url="https://example.com/hook",
                types=["event.published"],
                secret="s",
            )
        )
        db.commit()


def test_0031_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0031")
    _seed(engine)

    # ---- down to 0030: no watermark, no idempotency table, the endpoint kept
    command.downgrade(cfg, "-1")
    assert "watermark_seq" not in _endpoint_columns(engine)
    assert not sa.inspect(engine).has_table("idempotency_record")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM webhook_endpoint")).scalar() == 1

    # ---- up to 0031: the endpoint starts at the head of the log, not at 0
    command.upgrade(cfg, "0031")
    assert "watermark_seq" in _endpoint_columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT watermark_seq FROM webhook_endpoint")).scalar() == 3
    inspector = sa.inspect(engine)
    assert inspector.has_table("idempotency_record")
    assert "ix_idempotency_record_expires_at" in {
        i["name"] for i in inspector.get_indexes("idempotency_record")
    }
    row = {
        "id": "r1",
        "scope": "account:a",
        "key": "k-12345678",
        "now": "2026-10-06 00:00:00",
    }
    insert = sa.text(
        "INSERT INTO idempotency_record (id, scope, idempotency_key, method, path, request_hash, "
        "status_code, response_headers, response_body, created_at, expires_at) VALUES (:id, :scope, :key, "
        "'POST', '/v1/webhooks', 'h', 201, '{}', x'7b7d', :now, :now)"
    )
    with engine.begin() as conn:
        conn.execute(insert, row)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(insert, {**row, "id": "r2"})


def test_0031_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0030")
    before = _endpoint_columns(engine)
    command.upgrade(cfg, "0031")
    assert _endpoint_columns(engine) == before
