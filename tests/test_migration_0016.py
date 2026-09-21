"""Round-trip for migration 0016 (recompute `public_at` for the paywall-by-shape decision).

0016 is a data migration with no DDL, so what has to be proved is the arithmetic and that it is
reversible: a store carrying the old blanket lag (records and events at `published_at + 14` for
proposals, `+ 7` for opportunities) must come out of `upgrade` with every record at
`public_at == published_at`, ISO change events at `published_at + 14`, every other event live, and
`source.lag_days` seeded for the ISO queue registers; `downgrade` must put the blanket lag back.

Run here on SQLite; the PostGIS half (`upgrade head / downgrade -1 / upgrade head` against a real
PostGIS instance) is exercised by the `migrations` CI job and was run by hand on 2026-09-20 before
this landed.

**Amended 2026-09-21.** Migration `0019` drops `source.lag_days` and `source.lag_overrides` — the
owner dropped the ISO change-event delay and its knob — so the ORM no longer declares the two
columns and `Base.metadata.create_all` no longer creates them. A store standing at 0015, which is
what this test needs, *did* have them, so `_add_pre_0019_lag_columns` puts them back with raw DDL
before seeding. 0016 itself is unchanged and still means what it meant: it is history, and history
has to keep running. What 0019 then does to these same rows is
`tests/test_migration_0019.py`.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid as _uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from services.db.base import Base
from services.db.models import (
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Proposal,
    Source,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 20, 6, 0, tzinfo=UTC)

ISO_SOURCE = "us.iso.caiso.gen_queue"
LIVE_SOURCE = "gb.neso.tec_register"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0016.db'}"


def _add_pre_0019_lag_columns(engine: sa.Engine) -> None:
    """Re-create the two `source` columns as the schema carried them from 0001 until 0019.

    `Base.metadata.create_all` builds the *current* ORM, which has neither, so without this the
    0016 statements that read and seed them would be skipped by their own `_present` guards and
    this test would assert on a migration that did nothing.
    """
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE source ADD COLUMN lag_days INTEGER"))
        conn.execute(sa.text("ALTER TABLE source ADD COLUMN lag_overrides TEXT DEFAULT '{}'"))


def _seed_pre_0016(engine: sa.Engine) -> None:
    """A store as the pre-2026-09-20 loader left it: the blanket lag on every row."""
    from sqlalchemy.orm import Session

    _add_pre_0019_lag_columns(engine)
    with Session(engine) as s:
        lic = Licence(id="lic", name="Terms", reuse_class="attribution")
        s.add(lic)
        s.flush()
        for source_id in (ISO_SOURCE, LIVE_SOURCE):
            s.add(
                Source(
                    id=source_id,
                    name=source_id,
                    category="generation_queue",
                    url="https://example.org/q",
                    access="bulk_file",
                    cadence="weekly",
                    licence_id=lic.id,
                    publish_state="public",
                )
            )
        s.flush()

        prop = Proposal(
            public_id="prop_1",
            slug="p-1",
            kind="storage",
            name_canonical="P",
            jurisdiction="US-TX",
            lifecycle_state="filed",
            publish_state="public",
            published_at=NOW,
            public_at=NOW + dt.timedelta(days=14),
            min_reuse_class="attribution",
            source_count=1,
        )
        opp = Opportunity(
            public_id="opp_1",
            slug="o-1",
            kind="rfp",
            title="O",
            jurisdiction="US-TX",
            status="open",
            publish_state="public",
            published_at=NOW,
            public_at=NOW + dt.timedelta(days=7),
            min_reuse_class="attribution",
            source_count=1,
        )
        s.add_all([prop, opp])
        s.flush()
        s.add(
            OpportunitySource(
                opportunity_id=opp.id,
                source_id=LIVE_SOURCE,
                source_record_id="O1",
                source_url="https://example.org/o1",
                retrieved_at=NOW,
                licence_id=lic.id,
                raw={},
            )
        )
        for source_id, subject_type, subject_id, lag in (
            (ISO_SOURCE, "proposal", prop.id, 14),
            (LIVE_SOURCE, "proposal", prop.id, 14),
            (LIVE_SOURCE, "opportunity", opp.id, 7),
        ):
            s.add(
                Event(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    event_type="status_change",
                    observed_at=NOW,
                    published_at=NOW,
                    public_at=NOW + dt.timedelta(days=lag),
                    source_id=source_id,
                    source_url="https://example.org/q",
                    retrieved_at=NOW,
                    licence_id=lic.id,
                    changed_keys=["lifecycle_state"],
                    idempotency_key=f"{source_id}:{subject_type}:{_uuid.uuid4()}",
                )
            )
            s.flush()
        s.commit()


def _state(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:

        def gap(table: str, where: str = "1=1") -> list[float]:
            rows = conn.execute(
                sa.text(
                    f"SELECT julianday(public_at) - julianday(published_at) AS d "  # noqa: S608
                    f"FROM {table} WHERE {where} ORDER BY d"
                )
            ).all()
            return [round(r.d, 3) for r in rows]

        return {
            "proposal": gap("proposal"),
            "opportunity": gap("opportunity"),
            "iso_events": gap("event", f"source_id = '{ISO_SOURCE}'"),
            "live_proposal_events": gap(
                "event", f"source_id = '{LIVE_SOURCE}' AND subject_type = 'proposal'"
            ),
            "live_opportunity_events": gap(
                "event", f"source_id = '{LIVE_SOURCE}' AND subject_type = 'opportunity'"
            ),
            "iso_lag_days": conn.scalar(
                sa.text(f"SELECT lag_days FROM source WHERE id = '{ISO_SOURCE}'")  # noqa: S608
            ),
            "live_lag_days": conn.scalar(
                sa.text(f"SELECT lag_days FROM source WHERE id = '{LIVE_SOURCE}'")  # noqa: S608
            ),
        }


def test_0016_upgrade_downgrade_upgrade(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    _seed_pre_0016(engine)

    before = _state(engine)
    assert before["proposal"] == [14.0]
    assert before["opportunity"] == [7.0]
    assert before["iso_lag_days"] is None

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0015")

    command.upgrade(cfg, "0016")
    after = _state(engine)
    assert after["proposal"] == [0.0], "every record is public at publication"
    assert after["opportunity"] == [0.0]
    assert after["iso_events"] == [14.0], "the one surviving delay"
    assert after["live_proposal_events"] == [0.0]
    assert after["live_opportunity_events"] == [0.0]
    assert after["iso_lag_days"] == 14
    assert after["live_lag_days"] is None

    command.downgrade(cfg, "-1")
    assert _state(engine) == before, "downgrade restores the blanket lag exactly"

    command.upgrade(cfg, "0016")
    assert _state(engine) == after

    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0016"


def test_0016_respects_a_per_event_type_override(sqlite_url: str) -> None:
    """`source.lag_overrides` wins over the source lag, the same rule the loader applies."""
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    _seed_pre_0016(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE source SET lag_days = 14, lag_overrides = :o WHERE id = :i"),
            {"o": '{"status_change": 0}', "i": ISO_SOURCE},
        )

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0015")
    command.upgrade(cfg, "0016")

    assert _state(engine)["iso_events"] == [0.0]
