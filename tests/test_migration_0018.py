"""Round-trip for migration 0018 (`source.vintage` / `source.vintage_basis`) and its backfill.

Three things have to hold and each is a way the change could be quietly useless:

* the columns arrive, survive a downgrade and come back on a second upgrade;
* the backfill reads the evidence already in the store — the artefact URL on the link rows and
  the Atlas vintage token on `asset.attributes` — so an existing load renders a release without a
  re-ingest (which needs live network, and is not available in CI or in a restore drill);
* a source that publishes no release is written as `not_stated` rather than left NULL, because
  "examined, states none" and "never examined" are different answers and the whole point of the
  column is that the second must never be dressed up as the first — nor either of them as the
  fetch date.

Run on SQLite; the PostGIS half is exercised by the `migrations` CI job.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from services.db.base import Base
from services.db.models import Licence, Source, new_uuid
from services.db.types import GUID, JSONVariant

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 13, 20, 25, tzinfo=UTC)

#: The real workbook loaded on 2026-09-13 — EIA's July report, fetched in September.
EIA_URL = "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx"
#: A queue register that replaces a file at a stable URL and names no release.
QUEUE_URL = "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0018.db'}"


def _pre_0018_schema(engine: sa.Engine) -> sa.MetaData:
    """The pre-0018 shape of exactly the four tables the migration reads or writes, built with the
    dialect-neutral column types the models use.

    Written out rather than taken from `Base.metadata.create_all`, for the reason
    `services/db/test_migrations.py` gives for the same approach: `create_all` builds today's
    schema, which already has the columns this migration adds, so it cannot be a starting point
    for adding them. Four tables, because the backfill reads the link rows' artefact URLs and the
    asset rows' vintage token and writes `source` -- nothing else."""
    meta = sa.MetaData()
    sa.Table(
        "licence",
        meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("reuse_class", sa.Text, nullable=False),
    )
    sa.Table(
        "source",
        meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
    )
    sa.Table(
        "proposal_source",
        meta,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_record_id", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        "asset",
        meta,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_asset_id", sa.Text, nullable=False),
        sa.Column("attributes", JSONVariant()),
    )
    meta.create_all(engine)
    return meta


def _seed(engine: sa.Engine, meta: sa.MetaData) -> None:
    with engine.begin() as conn:
        conn.execute(meta.tables["licence"].insert(), {"id": "lic", "name": "Terms", "reuse_class": "open"})
        conn.execute(
            meta.tables["source"].insert(),
            [
                {"id": sid, "name": sid, "url": "https://example.org/", "licence_id": "lic"}
                for sid in (
                    "us.eia.860m",
                    "us.iso.caiso.gen_queue",
                    "us.eia.atlas.gas_storage",
                    # A source with no rows at all: nothing to read, so nothing is claimed.
                    "us.never.loaded",
                )
            ],
        )
        conn.execute(
            meta.tables["proposal_source"].insert(),
            [
                {
                    "id": new_uuid(),
                    "source_id": "us.eia.860m",
                    "source_record_id": "R0",
                    "source_url": EIA_URL,
                    "retrieved_at": NOW,
                },
                {
                    "id": new_uuid(),
                    "source_id": "us.iso.caiso.gen_queue",
                    "source_record_id": "R1",
                    "source_url": QUEUE_URL,
                    "retrieved_at": NOW,
                },
            ],
        )
        conn.execute(
            meta.tables["asset"].insert(),
            {
                "id": new_uuid(),
                "source_id": "us.eia.atlas.gas_storage",
                "source_asset_id": "A1",
                "attributes": {"source_vintage": "202012"},
            },
        )


def _vintages(engine: sa.Engine) -> dict[str, tuple[str | None, str | None]]:
    with engine.connect() as conn:
        return {
            r.id: (r.vintage, r.vintage_basis)
            for r in conn.execute(sa.text("SELECT id, vintage, vintage_basis FROM source")).all()
        }


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("source")}


def test_0018_upgrade_downgrade_upgrade_and_backfill(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    meta = _pre_0018_schema(engine)
    _seed(engine, meta)
    assert "vintage" not in _columns(engine)

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0017")
    command.upgrade(cfg, "0018")

    assert {"vintage", "vintage_basis"} <= _columns(engine)
    after = _vintages(engine)
    assert after["us.eia.860m"] == ("2026-07", "artefact_filename"), (
        "the July workbook's own name states the release; the September fetch date does not"
    )
    assert after["us.eia.atlas.gas_storage"] == ("2020-12", "shapefile_member")
    assert after["us.iso.caiso.gen_queue"] == (None, "not_stated"), (
        "examined and found to publish no release label — not left NULL, and not the fetch date"
    )
    assert after["us.never.loaded"] == (None, None), "no rows, so nothing is claimed"

    command.downgrade(cfg, "0017")
    assert "vintage" not in _columns(engine)
    assert "vintage_basis" not in _columns(engine)

    command.upgrade(cfg, "0018")
    assert _vintages(engine) == after, "a second upgrade reproduces the same backfill exactly"
    # The guard arrives on a migration-built database too, and the constraint the table already
    # had is still there: SQLite's recreate is driven by reflection, which carries both.
    checks = {c["name"] for c in sa.inspect(engine).get_check_constraints("source")}
    assert any(n == "vintage_basis_vocab" or n.endswith("_vintage_basis_vocab") for n in checks)


def test_vintage_basis_vocabulary_is_enforced_by_the_database(sqlite_url: str) -> None:
    """The CHECK constraint is what makes "derived from the fetch date" unrepresentable: a future
    loader cannot invent a fourth basis without a migration and a reviewer."""
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Licence(id="lic", name="Terms", reuse_class="open"))
        s.flush()
        s.add(
            Source(
                id="us.bad",
                name="Bad",
                category="registry",
                url="https://example.org/",
                access="api",
                cadence="daily",
                licence_id="lic",
                vintage="2026-07",
                vintage_basis="retrieved_at",
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            s.commit()
