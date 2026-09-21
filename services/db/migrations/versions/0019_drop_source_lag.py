"""Drop `source.lag_days` / `source.lag_overrides`, and un-delay the events already withheld.

Owner decision, 2026-09-21 (`docs/00-PLAN.md` decisions log), answering the open question the
2026-09-20 row raised: the surviving ISO change-event delay goes. The measurement that drove it is
in that log and in `tests/test_free_tier_change_reconstruction.py` — the withheld `(field, before,
after)` was recoverable from two reads of a page that was never delayed, and a full daily sweep of
the register cost 53 requests against the public tier's own 1,440.

Why the columns go rather than being set to zero
------------------------------------------------
A dormant per-source lag that an admin `PATCH /admin/v1/sources/{id}` can flip back on is exactly
the defeatable promise being removed: it would leave a public page one audited write away from
claiming a delay the product does not apply, and it would let a re-ingest silently re-withhold a
register's change events. `services/ingest/lag.py` already made this argument for the record-level
lag ("there is no knob"); events now get the same treatment. Reintroducing either is an owner
decision that lands in code, not a row in `source`.

Two statements, in this order
-----------------------------
1. **Events go live**: `event.public_at = event.published_at` wherever `published_at` is not NULL.
   `public_at` is a *stored* column — `services/api/visibility.py` reads it directly for
   `entitlement == 'public'` — so changing the loader only fixes rows written after the change.
   Every ISO queue change event already in the store carries `published_at + 14 days` from
   migration 0016 and would go on waiting out a fortnight the owner has abolished. A row with a
   NULL `published_at` (never published, or taken down) keeps its NULL `public_at`, which is what
   `services/api/admin_records.py`'s takedown path writes. Records are untouched: 0016 already set
   `public_at = published_at` for `proposal` and `opportunity`, and nothing has delayed one since.
2. **The two columns are dropped** from `source`.

The `WHERE public_at <> published_at` shape is deliberately *not* used: on SQLite the two columns
are text and the comparison is lexical, and an unconditional assignment is idempotent anyway.

`downgrade` re-adds both columns, nullable, with `lag_overrides` defaulting to `{}` as 0001
created it — **and it cannot restore a single value.** The lag figures are gone from the manifest
(`data/sources.yaml`), from the policy module and from the admin surface in the same change, so
there is nothing left in the repository or the database to read them back from; a downgraded store
gets the schema back with `lag_days` NULL on every row. Nor can it re-withhold the change events
step 1 released: the `public_at` values it overwrote are not recoverable from anything the store
still holds (`published_at` plus a lag nobody records any more). A downgrade is therefore a schema
rollback only, and the data stays un-delayed — which is the safe direction: it publishes nothing
that the licence and publish-state gates would not already publish, since those are untouched here.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import JSONVariant

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("lag_days", "lag_overrides")

_event = sa.table(
    "event",
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("public_at", sa.DateTime(timezone=True)),
)


def _columns(table: str) -> set[str]:
    """The live column set, or empty when the table is absent.

    Guarded like 0013-0018: `services/db/test_migrations.py` stamps a *partial* 0008 baseline
    holding only the tables and columns the revisions it exercises need, so an unguarded statement
    against `event` here would fail a test about a different migration. A real store has every
    table and the guards are no-ops.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if {"public_at", "published_at"} <= _columns("event"):
        bind.execute(
            sa.update(_event)
            .where(_event.c.published_at.is_not(None))
            .values(public_at=_event.c.published_at)
        )

    existing = _columns("source")
    present = [c for c in COLUMNS if c in existing]
    if present:
        # One batch, so SQLite recreates the table once rather than per column.
        with op.batch_alter_table("source") as batch:
            for column in present:
                batch.drop_column(column)


def downgrade() -> None:
    existing = _columns("source")
    if not existing:
        return
    with op.batch_alter_table("source") as batch:
        if "lag_days" not in existing:
            batch.add_column(sa.Column("lag_days", sa.Integer(), nullable=True))
        if "lag_overrides" not in existing:
            # Nullable, unlike 0001's NOT NULL DEFAULT '{}': there are no values to backfill and a
            # NOT NULL column would have to invent one for every existing row. The server default
            # is kept so a row written by pre-0019 code still gets `{}`.
            batch.add_column(sa.Column("lag_overrides", JSONVariant(), nullable=True, server_default="{}"))
