"""Recompute `public_at` for the paywall-by-shape decision (owner, 2026-09-19).

`docs/00-PLAN.md` decisions log, 2026-09-18 row item 2, answered 2026-09-19: "alerts, exports, API
and watchlists are paid; free users see every record; the delay is kept only on ISO change events.
Supersedes the time-delay model in docs/10 and docs/41".

`public_at` is a **stored** column, not a computed one: `services/api/visibility.py` reads it
directly for `entitlement == "public"`. Changing `services/ingest/lag.py` therefore only fixes rows
loaded *after* the change — every row already in the store still carries `published_at + 14 days`
(proposals, and proposal events) or `+ 7 days` (opportunities and their events) from the blanket
lag. Without this migration a free reader would keep waiting a fortnight for records the owner
decided they should see now. That is what makes a data migration necessary here; there is no schema
change in either direction.

Three statements, in this order:

1. **Seed `source.lag_days`** with the change-event delay for the eight `us.iso.*` interconnection
   queue registers. Nothing ever wrote this column before today (`services/ingest/loader.py` did
   not set it and the only other writer is the operator's audited
   `PATCH /admin/v1/sources/{id}`), so it is `NULL` on every row of every existing store; the
   `WHERE lag_days IS NULL` guard means an operator's hand-set value is never overwritten. From
   0016 on, `source.lag_days` means "days a *change event* from this source waits before the
   public tier sees it" — it no longer delays the record.
2. **Records go live**: `proposal.public_at = proposal.published_at`, same for `opportunity`. A
   row with a NULL `published_at` (taken down, or never published) keeps a NULL `public_at`,
   which is what `services/api/admin_records.py`'s takedown path already writes.
3. **Events are recomputed** from `published_at` plus the lag their own source declares, applying
   `source.lag_overrides[event_type]` first where an operator has set one and the source-level
   lag otherwise. Sources with no declared lag — every non-ISO source — publish their change
   events live, so their events collapse to `public_at = published_at` too.

The ISO id list and the 14-day figure are written out here rather than imported from
`services/ingest/lag.py`: a migration has to keep meaning the same thing when that constant is
next revised.

`downgrade` restores the blanket lag exactly as it stood before this revision — records and events
at `published_at + 14 days` for proposals and `+ 7 days` for opportunities, and `source.lag_days`
back to NULL on the eight ISO rows this migration seeded (only where it still holds the value 14,
so an operator's later edit survives the round trip). It is an exact inverse for any store this
migration ran forward on, and approximate only for a store where an operator had already hand-set a
per-source lag through the admin API before 0016 — in which case that per-source number, not the
class default, was what the loader applied and the class default is the best available restoration.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from services.db.types import JSONVariant

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The interconnection-queue registers whose change events keep the delay (owner, 2026-09-19:
#: "the delay is kept only on ISO change events"). Frozen at the 2026-09-20 manifest; the live
#: list is `data/sources.yaml change_event_lag_days` and is pinned by
#: `tests/test_iso_change_event_lag.py`.
ISO_QUEUE_SOURCE_IDS: tuple[str, ...] = (
    "us.iso.caiso.gen_queue",
    "us.iso.ercot.gen_queue",
    "us.iso.ercot.large_load_queue",
    "us.iso.spp.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.iso.isone.gen_queue",
    "us.iso.pjm.gen_queue",
    "us.iso.miso.gen_queue",
)
ISO_CHANGE_EVENT_LAG_DAYS = 14

#: The blanket per-kind lag this revision removes, kept only so `downgrade` can put it back.
LEGACY_LAG_DAYS_BY_KIND = {"proposal": 14, "opportunity": 7}

_source = sa.table(
    "source",
    sa.column("id", sa.Text),
    sa.column("lag_days", sa.Integer),
    sa.column("lag_overrides", JSONVariant()),
)
_event = sa.table(
    "event",
    sa.column("source_id", sa.Text),
    sa.column("subject_type", sa.Text),
    sa.column("event_type", sa.Text),
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("public_at", sa.DateTime(timezone=True)),
)
_RECORD_TABLES = {
    "proposal": sa.table(
        "proposal",
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("public_at", sa.DateTime(timezone=True)),
    ),
    "opportunity": sa.table(
        "opportunity",
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("public_at", sa.DateTime(timezone=True)),
    ),
}


def _shift(column: sa.ColumnElement[Any], days: int) -> sa.ColumnElement[Any]:
    """`published_at + days`, in each dialect's own arithmetic.

    There is no portable spelling: adding a Python `timedelta` to a column renders as numeric
    addition on SQLite and produces a value `julianday()` cannot read back, which is exactly the
    kind of silent corruption a `public_at` migration must not risk. PostgreSQL gets a real
    `INTERVAL`; SQLite gets `datetime(col, '+N days')`, which is second-precision — acceptable
    because SQLite is the test dialect only (migrations 0001-0008 cannot run on it at all, see
    `services/db/migrations/env.py`) and because a visibility threshold does not turn on
    microseconds. `days` is always an `int` from this module's own constants or from an
    operator-set integer override, never from free text.
    """
    if days == 0:
        return column
    if op.get_bind().dialect.name == "postgresql":
        return column + sa.literal_column(f"INTERVAL '{int(days)} days'")
    return sa.func.datetime(column, f"+{int(days)} days")


def _present(table: str, *columns: str) -> bool:
    """True when `table` exists with all of `columns`.

    Every statement below is guarded by this, following 0013-0015's "check the live schema first"
    pattern. The reason is not defensiveness for its own sake: `services/db/test_migrations.py`
    stamps a *partial* 0008 baseline — only the tables 0009-0011 touch, with only the columns they
    need — and runs `upgrade head` against it, so an unguarded `UPDATE event ...` here would fail
    a test about a different migration. A real store has every table and the guards are no-ops.
    """
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    present = {c["name"] for c in inspector.get_columns(table)}
    return set(columns) <= present


def _source_lags() -> list[tuple[str, int | None, dict[str, Any]]]:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_source.c.id, _source.c.lag_days, _source.c.lag_overrides)).all()
    return [(r[0], r[1], dict(r[2] or {})) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    has_source = _present("source", "lag_days", "lag_overrides")
    has_event = _present("event", "public_at", "published_at", "source_id", "event_type")

    # 1. Seed the change-event lag on the ISO queue sources that are actually present in this
    #    store (a store that has never ingested PJM simply has no row to seed).
    if has_source:
        bind.execute(
            sa.update(_source)
            .where(_source.c.id.in_(ISO_QUEUE_SOURCE_IDS), _source.c.lag_days.is_(None))
            .values(lag_days=ISO_CHANGE_EVENT_LAG_DAYS)
        )

    # 2. Records: visible at publication, no delay, on every tier.
    for name, table in _RECORD_TABLES.items():
        if _present(name, "public_at", "published_at"):
            bind.execute(sa.update(table).values(public_at=table.c.published_at))

    # 3. Events: the source's declared change-event lag, per-event-type overrides first.
    if has_source and has_event:
        for source_id, lag_days, overrides in _source_lags():
            for event_type, override_days in overrides.items():
                bind.execute(
                    sa.update(_event)
                    .where(_event.c.source_id == source_id, _event.c.event_type == str(event_type))
                    .values(public_at=_shift(_event.c.published_at, max(0, int(override_days))))
                )
            rest = sa.update(_event).where(_event.c.source_id == source_id)
            if overrides:
                rest = rest.where(_event.c.event_type.notin_([str(k) for k in overrides]))
            bind.execute(rest.values(public_at=_shift(_event.c.published_at, max(0, int(lag_days or 0)))))


def downgrade() -> None:
    bind = op.get_bind()

    for name, table in _RECORD_TABLES.items():
        if _present(name, "public_at", "published_at"):
            bind.execute(
                sa.update(table).values(public_at=_shift(table.c.published_at, LEGACY_LAG_DAYS_BY_KIND[name]))
            )
    if _present("event", "public_at", "published_at", "subject_type"):
        for kind, days in LEGACY_LAG_DAYS_BY_KIND.items():
            bind.execute(
                sa.update(_event)
                .where(_event.c.subject_type == kind)
                .values(public_at=_shift(_event.c.published_at, days))
            )
    if _present("source", "lag_days"):
        bind.execute(
            sa.update(_source)
            .where(
                _source.c.id.in_(ISO_QUEUE_SOURCE_IDS),
                _source.c.lag_days == ISO_CHANGE_EVENT_LAG_DAYS,
            )
            .values(lag_days=None)
        )
