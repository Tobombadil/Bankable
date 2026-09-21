"""Round-trip for migration 0019 (drop the source lag columns; release the withheld events).

Two things have to be proved, because 0019 does two things:

1. **The events that were still waiting go live.** `public_at` is a stored column, so the rows
   loaded while the ISO change-event delay was in force carry `published_at + 14 days` from 0016
   and would go on waiting it out after the code stopped applying it. A row with a NULL
   `published_at` — taken down, or never published — keeps its NULL `public_at`.
2. **The two columns are gone**, and `downgrade` puts the schema back (nullable, no values: there
   is nothing left anywhere to restore them from, which the migration's docstring states).

Run here on SQLite. The PostGIS half (`upgrade head` / `downgrade -1` / `upgrade head` against a
real instance) belongs to the `migrations` CI job; whether it was run by hand for this revision is
recorded in `docs/CHANGELOG.md` rather than asserted here.
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
from services.db.models import Event, Licence, Proposal, Source

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 21, 6, 0, tzinfo=UTC)

ISO_SOURCE = "us.iso.caiso.gen_queue"
LIVE_SOURCE = "gb.neso.tec_register"
LAG_COLUMNS = ("lag_days", "lag_overrides")


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0019.db'}"


def _seed_pre_0019(engine: sa.Engine) -> None:
    """A store as 0016 left it: records live, ISO change events held 14 days, the lag columns set.

    The ORM no longer declares those columns (that is what 0019 does), so they are added with raw
    DDL exactly as 0001 created them.
    """
    from sqlalchemy.orm import Session

    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE source ADD COLUMN lag_days INTEGER"))
        conn.execute(sa.text("ALTER TABLE source ADD COLUMN lag_overrides TEXT DEFAULT '{}'"))

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
        prop = Proposal(
            public_id="prop_1",
            slug="p-1",
            kind="storage",
            name_canonical="P",
            jurisdiction="US-TX",
            lifecycle_state="filed",
            publish_state="public",
            published_at=NOW,
            public_at=NOW,
            min_reuse_class="attribution",
            source_count=1,
        )
        s.add(prop)
        s.flush()
        for source_id, lag, published in (
            (ISO_SOURCE, 14, NOW),  # withheld by the delay 0019 removes
            (LIVE_SOURCE, 0, NOW),  # already live
            (ISO_SOURCE, 14, None),  # taken down: NULL published_at, NULL public_at
        ):
            s.add(
                Event(
                    subject_type="proposal",
                    subject_id=prop.id,
                    event_type="status_change",
                    observed_at=NOW,
                    published_at=published,
                    public_at=(published + dt.timedelta(days=lag)) if published else None,
                    source_id=source_id,
                    source_url="https://example.org/q",
                    retrieved_at=NOW,
                    licence_id=lic.id,
                    changed_keys=["lifecycle_state"],
                    idempotency_key=f"{source_id}:{_uuid.uuid4()}",
                )
            )
            s.flush()
        s.execute(sa.text("UPDATE source SET lag_days = 14 WHERE id = :i"), {"i": ISO_SOURCE})
        s.commit()


def _source_columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("source")}


def _event_gaps(engine: sa.Engine) -> list[float | None]:
    """`public_at - published_at` per event, smallest first, NULLs last (SQLite sorts them first)."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT julianday(public_at) - julianday(published_at) AS d FROM event")
        ).all()
    gaps = [None if r.d is None else round(r.d, 3) for r in rows]
    return sorted(g for g in gaps if g is not None) + [g for g in gaps if g is None]


def test_0019_releases_withheld_events_and_drops_the_columns(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    _seed_pre_0019(engine)

    assert set(LAG_COLUMNS) <= _source_columns(engine)
    assert _event_gaps(engine) == [0.0, 14.0, None], "one live event, one withheld, one unpublished"

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0018")
    command.upgrade(cfg, "0019")

    assert _event_gaps(engine) == [0.0, 0.0, None], (
        "every published event is public at publication; an unpublished one stays unpublished"
    )
    assert not (set(LAG_COLUMNS) & _source_columns(engine)), "no knob is left to turn"

    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0019"


def test_0019_downgrade_restores_the_schema_but_no_values(sqlite_url: str) -> None:
    """The stated limit of the downgrade, asserted rather than only documented."""
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    _seed_pre_0019(engine)

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0018")
    command.upgrade(cfg, "0019")
    command.downgrade(cfg, "-1")

    assert set(LAG_COLUMNS) <= _source_columns(engine), "the schema comes back"
    with engine.connect() as conn:
        lags = conn.execute(sa.text("SELECT lag_days FROM source")).scalars().all()
    assert set(lags) == {None}, "the values cannot come back, and the migration says so"
    assert _event_gaps(engine) == [0.0, 0.0, None], "nor are the events re-withheld"

    command.upgrade(cfg, "0019")
    assert not (set(LAG_COLUMNS) & _source_columns(engine))
