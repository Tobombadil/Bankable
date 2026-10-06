"""Event-seq horizon trigger; `source.last_loaded_ts`; `subscription.last_event_at` (backend audit
2026-09-30 F4, F5; architect A5 second half, A6).

1. **Postgres only: the event-seq horizon trigger** (F5, A5). A statement-level `BEFORE INSERT`
   trigger on `event` that, once per transaction, reads the identity sequence's last value `L` and
   takes a shared transaction-level advisory lock keyed `0x455651 << 40 | L`. Every seq the
   transaction then takes is above `L`, and the lock lasts until it ends, so a watermark reader can
   bound itself below every open writer (`services/db/event_horizon.py`, docs/21 §3.10). A
   statement-level `BEFORE` trigger fires before the statement evaluates any column default, so the
   advertisement always precedes the `nextval` it covers, for ORM, Core, `INSERT ... SELECT` and
   `COPY` alike. The once-per-transaction flag is a transaction-local setting
   (`set_config(..., true)`): a rolled-back savepoint drops the flag and the lock together, so the
   next insert advertises again. The sequence must not cache values (`CACHE 1`, the default),
   otherwise a session could hand out a value below another session's `L`; this revision refuses to
   run if it does.
2. **`source.last_loaded_ts`** (A6). The snapshot token (`YYYYMMDDTHHMMSSZ`) of the last run whose
   load committed. The scheduler's `load_source` replays every promoted run between it and the run
   it was asked to load, oldest first, so a failed load's change events arrive with the next load
   instead of being lost (`infra/scheduler/jobs.py::load_source_job`). NULL until a load commits.
3. **`subscription.last_event_at`** (F4). The `created` time of the billing event last applied to
   the subscription. An event older than it is ignored, so a late-delivered `active` cannot undo a
   newer `canceled` (`services/billing/entitlement.py`). NULL for rows written before this.

Downgrade drops the trigger, its function and both columns.

SQLite: only the two columns; it has no sequences or advisory locks, and its single-writer lock
already makes seq order commit order.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept literal here (not imported) so the revision stays what it was when written;
#: `services/db/event_horizon.py` holds the same values and `tests/test_postgres.py` compares them.
LOCK_NAMESPACE = 0x455651
SEQ_BITS = 40
EXPECTED_SEQUENCE = "public.event_seq_seq"
FUNCTION = "event_seq_horizon_advertise"
TRIGGER = "event_seq_horizon"
FLAG = "infraque.event_seq_advertised"

SOURCE_COLUMN = "last_loaded_ts"
SUBSCRIPTION_COLUMN = "last_event_at"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _create_trigger() -> None:
    bind = op.get_bind()
    sequence = bind.execute(sa.text("SELECT pg_get_serial_sequence('event', 'seq')")).scalar()
    if sequence is None:
        raise RuntimeError("event.seq has no identity sequence; the horizon trigger cannot be installed")
    if sequence != EXPECTED_SEQUENCE:
        raise RuntimeError(
            f"event.seq uses {sequence}, not {EXPECTED_SEQUENCE}; update services/db/event_horizon.py"
        )
    cache = bind.execute(
        sa.text("SELECT seqcache FROM pg_sequence WHERE seqrelid = CAST(:s AS regclass)"), {"s": sequence}
    ).scalar()
    if cache != 1:
        raise RuntimeError(f"{sequence} caches {cache} values; the event-seq horizon needs CACHE 1")
    base = LOCK_NAMESPACE << SEQ_BITS
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            taken bigint;
        BEGIN
            IF current_setting('{FLAG}', true) = '1' THEN
                RETURN NULL;
            END IF;
            -- NULL before the sequence's first value: nothing taken yet, so 0 is a safe lower bound.
            taken := COALESCE(pg_sequence_last_value('{sequence}'::regclass), 0);
            PERFORM pg_advisory_xact_lock_shared({base}::bigint + taken);
            PERFORM set_config('{FLAG}', '1', true);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON event")
    op.execute(
        f"CREATE TRIGGER {TRIGGER} BEFORE INSERT ON event FOR EACH STATEMENT EXECUTE FUNCTION {FUNCTION}()"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql" and _has_table("event"):
        _create_trigger()
    if _has_table("source") and SOURCE_COLUMN not in _columns("source"):
        with op.batch_alter_table("source") as batch:
            batch.add_column(sa.Column(SOURCE_COLUMN, sa.Text, nullable=True))
    if _has_table("subscription") and SUBSCRIPTION_COLUMN not in _columns("subscription"):
        with op.batch_alter_table("subscription") as batch:
            batch.add_column(sa.Column(SUBSCRIPTION_COLUMN, sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table("subscription") and SUBSCRIPTION_COLUMN in _columns("subscription"):
        with op.batch_alter_table("subscription") as batch:
            batch.drop_column(SUBSCRIPTION_COLUMN)
    if _has_table("source") and SOURCE_COLUMN in _columns("source"):
        with op.batch_alter_table("source") as batch:
            batch.drop_column(SOURCE_COLUMN)
    if bind.dialect.name == "postgresql":
        if _has_table("event"):
            op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON event")
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
