"""The highest `event.seq` a watermark reader may pass: every event at or below it is committed or
will never be (backend audit 2026-09-30 F5; architect A5, second half; docs/21 §3.10 "Reading `seq`
in order").

On Postgres `event.seq` comes from the identity sequence (migration 0027), so seq order is the
order values were *taken*, not the order transactions *committed*. A reader that moves a watermark
to the highest committed seq can pass a lower seq still held by an open transaction (a load holds
one for up to 30 minutes) and never see that event. `stable_event_seq` returns a bound no open or
future transaction can still commit an event at or below.

How (Postgres). Migration 0032 adds a statement-level `BEFORE INSERT` trigger on `event`. Before
a transaction's first event insert takes a value from the sequence, the trigger reads the
sequence's last value `L` and takes a shared transaction-level advisory lock whose key encodes
`L` (`LOCK_NAMESPACE`). Every seq that transaction takes is greater than `L`, and the lock is held
until it commits or rolls back. A reader then:

1. reads the sequence's last value `S` (every seq taken so far is at most `S`);
2. reads the smallest `L` among the advertised locks still held (`pg_locks`);
3. returns `min(S, smallest L)`.

Why that is safe: a seq at or below the bound was taken before step 1, by a transaction that had
already advertised. If that transaction is still open at step 2, its `L` is below its seqs, so the
bound is below them too. If it has finished, its rows are visible to any statement the reader runs
after step 2 (Postgres releases a transaction's locks after its commit becomes visible). A seq
taken after step 1 is greater than `S`. The reader must therefore compute the bound *before* the
statement that reads events, under READ COMMITTED (the application's isolation level), which is
how every caller below uses it.

How (SQLite). Writers are serialised by the database lock and `seq` is `MAX(seq)+1` inside the
writing transaction (`services/db/models.py::_assign_event_seq`), so a lower seq can never commit
after a higher one; the bound is `MAX(seq)`.

Cost: two single-row reads per call on Postgres; `pg_locks` is read with a filter on the advisory
namespace. Readers: saved-search alerts (`services/alerts/evaluate.py`), webhook enqueue
(`services/alerts/webhooks.py`), social drafts (`services/social/worker.py`), and the `/v1/events`,
`/v1/bulk/events` and export statement (`services/api/resource_queries.py`), plus the starting
watermark of a new saved search or webhook (`services/api/pro.py`).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

#: Advisory-lock keys `LOCK_NAMESPACE << SEQ_BITS | L` belong to the event-seq horizon. 0x455651 is
#: "EVQ"; no other code takes an advisory lock in this range (`infra/scheduler/queue_schema.py`'s
#: key is 0x1F0E0A5C4E0E0001). A collision would only hold the bound lower for a while.
LOCK_NAMESPACE = 0x455651
#: `L` must stay below 2**40 (about 1.1e12 events).
SEQ_BITS = 40
#: The identity sequence behind `event.seq` (migration 0001; asserted in migration 0032).
SEQUENCE = "event_seq_seq"

# NULL before the sequence's first value (nothing taken since it was set): 0 is then a safe bound.
_PG_SEQUENCE_LAST = sa.text(f"SELECT COALESCE(pg_sequence_last_value('{SEQUENCE}'::regclass), 0)")
# `pg_locks` shows an int8 advisory key as classid (high 32 bits) and objid (low 32), objsubid 1.
_PG_OLDEST_ADVERTISED = sa.text(
    "SELECT min(((classid::bigint << 32) | objid::bigint) - (CAST(:ns AS bigint) << :bits)) "
    "FROM pg_locks "
    "WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid::bigint >= (CAST(:ns AS bigint) << (:bits - 32)) "
    "AND classid::bigint < ((CAST(:ns AS bigint) + 1) << (:bits - 32))"
)


def stable_event_seq(db: Session) -> int:
    """The highest seq a watermark may advance to now (module docstring). 0 when no event exists."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return int(db.scalar(sa.select(sa.func.max(sa.column("seq"))).select_from(sa.table("event"))) or 0)
    taken = int(db.scalar(_PG_SEQUENCE_LAST) or 0)
    oldest_open = db.scalar(_PG_OLDEST_ADVERTISED, {"ns": LOCK_NAMESPACE, "bits": SEQ_BITS})
    return max(0, min(taken, int(oldest_open)) if oldest_open is not None else taken)


__all__ = ["LOCK_NAMESPACE", "SEQUENCE", "SEQ_BITS", "stable_event_seq"]
