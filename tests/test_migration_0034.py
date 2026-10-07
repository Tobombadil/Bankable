"""Round-trip for migration 0034 (`organization.personal_data`; `privacy_request.due_at`;
API-key licence acceptance nullable). Written as 0033 and renumbered after the social lane took 0033.

0031's shape: the store starts at the schema `Base.metadata` produces, is stamped `0034`, walked
down and up again. Going up from a store without the columns classifies the existing organisations
and backfills `due_at`. The Postgres half is CI's `migrations` job. Names are invented.
"""

from __future__ import annotations

import datetime as dt
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
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0034.db'}"


def _cols(engine: sa.Engine, table: str) -> dict[str, dict[str, object]]:
    return {str(c["name"]): dict(c) for c in sa.inspect(engine).get_columns(table)}


def _seed(engine: sa.Engine) -> None:
    from sqlalchemy.orm import Session

    from services.db.models import Organization, PrivacyRequest

    with Session(engine) as db:
        for i, name in enumerate(("Imaginary Wind Partners LLC", "Wilhelmina Q. Placeholder")):
            db.add(
                Organization(
                    public_id=f"org_{i}",
                    slug=f"org-{i}",
                    name_canonical=name,
                    name_normalised=name.lower(),
                    type="other",
                    country="US",
                )
            )
        db.add(
            PrivacyRequest(
                public_id="prq_1",
                kind="erasure",
                record_public_id="org_0123456789AB",
                contact_email="someone@example.com",
                created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            )
        )
        db.commit()


def test_0034_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0034")
    _seed(engine)

    command.downgrade(cfg, "-1")
    assert "personal_data" not in _cols(engine, "organization")
    assert "due_at" not in _cols(engine, "privacy_request")
    assert _cols(engine, "api_key")["licence_accepted_version"]["nullable"] is False

    command.upgrade(cfg, "0034")
    org_cols = _cols(engine, "organization")
    assert org_cols["personal_data"]["nullable"] is False
    with engine.connect() as conn:
        flagged = dict(
            conn.execute(sa.text("SELECT name_canonical, personal_data FROM organization")).all()  # type: ignore[arg-type]
        )
        basis = dict(
            conn.execute(sa.text("SELECT name_canonical, personal_data_basis FROM organization")).all()  # type: ignore[arg-type]
        )
        due = conn.execute(sa.text("SELECT due_at FROM privacy_request")).scalar()
    assert bool(flagged["Wilhelmina Q. Placeholder"]) is True
    assert bool(flagged["Imaginary Wind Partners LLC"]) is False
    assert basis["Wilhelmina Q. Placeholder"] == "heuristic"
    assert str(due).startswith("2026-10-01"), due
    assert _cols(engine, "privacy_request")["due_at"]["nullable"] is False
    assert _cols(engine, "api_key")["licence_accepted_version"]["nullable"] is True
    assert "ix_privacy_request_status_due" in {
        i["name"] for i in sa.inspect(engine).get_indexes("privacy_request")
    }


def test_0034_downgrade_refuses_while_a_key_records_no_acceptance(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0034")
    from sqlalchemy.orm import Session

    from services.db.models import ApiKey
    from tests.conftest import make_account, make_user

    with Session(engine) as db:
        account = make_account(db, entitlement="api")
        user = make_user(db, account)
        db.add(
            ApiKey(
                public_id="key_x",
                account_id=account.id,
                created_by_user_id=user.id,
                name="k",
                prefix="bk_test",
                last4="abcd",
                key_hash="h",
                scopes=["read:public"],
                tier="public",
            )
        )
        db.commit()
    with pytest.raises(RuntimeError, match="no licence acceptance"):
        command.downgrade(cfg, "-1")


def test_0034_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0033")
    before = {t: _cols(engine, t) for t in ("organization", "privacy_request", "api_key")}
    command.upgrade(cfg, "0034")
    after = {t: _cols(engine, t) for t in ("organization", "privacy_request", "api_key")}
    assert before.keys() == after.keys()
    for table in before:
        assert set(before[table]) == set(after[table])
