"""`proposal.lifecycle_state`: admit `postponed` (owner decision 2026-10-24).

The owner's answer to `docs/28-filter-and-tag-audit.md` §11 item 2 (`docs/00-PLAN.md`, row "Owner decisions
on the weekend list", item 3): EIA-860M's "canceled or indefinitely postponed" units get their own lifecycle
state, counted neither as active nor as withdrawn or cancelled. EIA's sheet does not say which of the two a
unit is, so the state says what EIA says: the unit is not being built on any schedule EIA holds. 1,745 units
at 1,072 plants, 185,356.8 MW nameplate, in the September 2026 workbook. This revision widens
`ck_proposal_lifecycle_state_vocab` by that one value; `services/db/models.py::LIFECYCLE_STATES` carries it,
and the switch that loads the sheet is the `"Canceled or Postponed": postponed` key in
`pipeline/status_map.yaml`. No column changes and no row is written: docs/21 §1 keeps enums as `text` +
`CHECK` precisely so that adding a value is a constraint swap.

**Safe on a large table (PostgreSQL).** A plain `DROP CONSTRAINT` / `ADD CONSTRAINT` pair (0020's form) holds
an ACCESS EXCLUSIVE lock on `proposal` while the new CHECK scans every row, which blocks every read and write
of the table for the length of the scan. This revision instead:

1. sets `lock_timeout` (`LOCK_TIMEOUT`) for its own statements, so a long-running reader holding the table
   makes the migration fail fast and retryable rather than queue every later query behind its lock request;
2. drops the old CHECK and adds the widened one `NOT VALID` in **one** `ALTER TABLE`: no scan, so the ACCESS
   EXCLUSIVE lock lasts as long as a catalogue update, and no moment exists with no CHECK on the column (a
   `NOT VALID` CHECK is enforced on every insert and update from the moment it exists);
3. commits, then runs `VALIDATE CONSTRAINT` in its own transaction (`autocommit_block`). Validation scans the
   table under SHARE UPDATE EXCLUSIVE, which lets reads and writes go on: only other DDL and VACUUM wait.

Every existing row already satisfies the widened list (the old one is a subset of it), so validation cannot
fail on upgrade. A run interrupted between steps 2 and 3 leaves a widened, unvalidated CHECK; the next upgrade
sees it, validates it and returns (`_validated`). Measured on PostgreSQL 16 with 2,000,000 rows: see
`docs/22` §3.3.

**SQLite** cannot alter a CHECK in place, so the change goes through `batch_alter_table` (a table copy), as
0020 and 0029 do. The copy rebuilds the indexes from reflection, which loses a column's `DESC`, so every index
whose definition changed is rebuilt from the SQL SQLite held before the copy (`_sqlite_indexes`). The SQLite
round-trip tests build today's `Base.metadata`, which already carries the widened CHECK, and stamp an earlier
revision, so each step reads what is present: the CHECK is matched by suffix (`lifecycle_state_vocab`), and a
CHECK that already admits `postponed` is left alone.

**Downgrade** refuses while any proposal is `postponed`, naming the count, rather than rewriting projects into
another state to make the narrower CHECK hold (0020's and 0029's precedent). To roll back after the EIA-860M
sheet has loaded, take a backup and decide what those records become first. Removing the status-map key does
not do it: a unit that stops being loaded is a removal, and a removal never changes a record's state (docs/21
§7.3). Otherwise the downgrade restores the ten-value CHECK by the same NOT VALID and VALIDATE steps, and on
PostgreSQL counts again under the lock its swap takes, so a `postponed` row written after the first count
refuses the downgrade and rolls the swap back (`_narrow_postgresql`).

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proposal"
CONSTRAINT_SUFFIX = "lifecycle_state_vocab"
#: The name 0001 wrote and the models' naming convention gives (`services/db/base.py`), passed through
#: `op.f` so no convention is applied to it a second time.
CONSTRAINT = f"ck_{TABLE}_{CONSTRAINT_SUFFIX}"
NEW_STATE = "postponed"
#: Frozen here rather than imported from `services.db.models` (0020-0045's convention): a migration
#: states the vocabulary it wrote, and the model may move on after it.
STATES_BEFORE: tuple[str, ...] = (
    "unknown",
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
)
STATES_AFTER: tuple[str, ...] = (*STATES_BEFORE, NEW_STATE)
#: How long a PostgreSQL statement here waits for a lock before failing (module docstring, step 1).
LOCK_TIMEOUT = "5s"
#: The longest a PostgreSQL downgrade statement may run while the swap holds ACCESS EXCLUSIVE: the
#: count under the lock (`_narrow_postgresql`). Reset at the commit that ends that transaction.
LOCKED_COUNT_TIMEOUT = "5s"
#: SQLite rebuilds a table in batch mode by reflecting it; the keys are copied as declared, unresolved
#: (0042's reason: an old baseline need not have every referenced table). PostgreSQL alters in place.
_NO_FK_RESOLVE: dict[str, bool] = {"resolve_fks": False}


def _in_list(states: tuple[str, ...]) -> str:
    return f"lifecycle_state IN ({', '.join(repr(s) for s in states)})"


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(TABLE)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _check_constraint() -> tuple[str, str] | None:
    """`(name, sqltext)` of the lifecycle-vocabulary CHECK, or `None` when absent or unreadable."""
    try:
        checks = sa.inspect(op.get_bind()).get_check_constraints(TABLE)
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for check in checks:
        name = check.get("name") or ""
        if name == CONSTRAINT_SUFFIX or name.endswith(f"_{CONSTRAINT_SUFFIX}"):
            return str(name), str(check.get("sqltext") or "")
    return None


def _admits(sqltext: str, state: str) -> bool:
    """Whether a reflected CHECK names `state`: SQLite returns the text as written, PostgreSQL its
    `= ANY (ARRAY['unknown'::text, ...])` form; both quote each value."""
    return f"'{state}'" in sqltext


def _validated(name: str) -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conrelid = CAST(:table AS regclass) AND conname = :name"
        ),
        {"table": TABLE, "name": name},
    )
    value = row.scalar()
    return bool(value) if value is not None else True


def _validate_postgresql(name: str) -> None:
    """Step 3: in its own transaction, under SHARE UPDATE EXCLUSIVE, with the same lock timeout."""
    with op.get_context().autocommit_block():
        op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        try:
            op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {name}")
        finally:
            op.execute("RESET lock_timeout")


def _replace(present: str | None, states: tuple[str, ...]) -> None:
    if _is_postgresql():
        op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        drop = f"DROP CONSTRAINT {present}, " if present else ""
        op.execute(
            f"ALTER TABLE {TABLE} {drop}ADD CONSTRAINT {CONSTRAINT} CHECK ({_in_list(states)}) NOT VALID"
        )
        _validate_postgresql(CONSTRAINT)
        return
    indexes = _sqlite_indexes()
    with op.batch_alter_table(TABLE, reflect_kwargs=_NO_FK_RESOLVE) as batch:
        if present is not None:
            batch.drop_constraint(present, type_="check")
        batch.create_check_constraint(op.f(CONSTRAINT), _in_list(states))
    # The copy rebuilds each index from reflection, which drops a column's DESC (`capacity_mw DESC, id DESC`
    # came back as `capacity_mw, id`). Each index whose definition changed is rebuilt from the SQL SQLite
    # stored before the copy, so the table leaves this revision exactly as it entered.
    rebuilt = _sqlite_indexes()
    for name, sql in indexes.items():
        if rebuilt.get(name) != sql:
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
            op.execute(sql)


def _sqlite_indexes() -> dict[str, str]:
    """`{name: CREATE INDEX ...}` of the table's declared indexes, as SQLite stores them."""
    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"
        ),
        {"t": TABLE},
    )
    return {str(name): str(sql) for name, sql in rows}


def rows_in_the_new_state() -> int:
    """How many proposals are `postponed`: the rows the ten-value CHECK would refuse. Exposed for the
    round-trip test; `downgrade()` refuses on any."""
    if not _has_table():
        return 0
    count = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE lifecycle_state = :state"),  # noqa: S608 -- fixed names
        {"state": NEW_STATE},
    )
    return int(count.scalar() or 0)


def upgrade() -> None:
    if not _has_table():
        return
    present = _check_constraint()
    if present is not None and _admits(present[1], NEW_STATE):
        if _is_postgresql() and not _validated(present[0]):
            _validate_postgresql(present[0])
        return
    _replace(present[0] if present else None, STATES_AFTER)


def _refuse_if_any_waiting() -> None:
    waiting = rows_in_the_new_state()
    if waiting:
        raise RuntimeError(
            f"0046 downgrade refused: {waiting} proposal(s) are '{NEW_STATE}', which the ten-value CHECK "
            "refuses. A schema rollback must not rewrite a project's state: back up the store and decide "
            "what those records become first (module docstring)."
        )


def _narrow_postgresql(present: str | None) -> None:
    """The downgrade's swap on PostgreSQL, with no window for a `postponed` write.

    The narrowed CHECK is added NOT VALID first, which takes ACCESS EXCLUSIVE until this transaction
    ends; the count then runs under that lock, so no row can be written between the count and the swap.
    A row found refuses the downgrade and the transaction rolls back to the widened CHECK. With none,
    the transaction commits: from then on the narrowed CHECK refuses every new `postponed` write, so
    the VALIDATE in its own transaction cannot fail. (Counting first and swapping after, as the first
    version of this revision did, let a write land in between: the narrowed NOT VALID CHECK was then
    committed, VALIDATE failed, and the store sat at 0046 with a CHECK refusing its own state.) The
    count holds the lock for one scan of `proposal` (117 ms at 2,000,000 rows, docs/22 §3.3), and
    `LOCKED_COUNT_TIMEOUT` bounds it: on a store so large that the scan runs longer, the downgrade
    rolls back rather than block every reader. The lock-free count in `downgrade()` refuses the usual
    case before any lock is taken."""
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"SET LOCAL statement_timeout = '{LOCKED_COUNT_TIMEOUT}'")
    drop = f"DROP CONSTRAINT {present}, " if present else ""
    op.execute(
        f"ALTER TABLE {TABLE} {drop}ADD CONSTRAINT {CONSTRAINT} CHECK ({_in_list(STATES_BEFORE)}) NOT VALID"
    )
    _refuse_if_any_waiting()
    _validate_postgresql(CONSTRAINT)


def downgrade() -> None:
    if not _has_table():
        return
    # Lock-free: refuses the usual case (the EIA-860M sheet has loaded) without touching the table.
    _refuse_if_any_waiting()
    present = _check_constraint()
    if present is not None and not _admits(present[1], NEW_STATE):
        return
    if _is_postgresql():
        _narrow_postgresql(present[0] if present else None)
        return
    _replace(present[0] if present else None, STATES_BEFORE)
