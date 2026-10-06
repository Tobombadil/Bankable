"""`asset.status` gains `retiring`; `asset.retirement_year` (lane R1, owner request 2026-10-06).

Retired and planned-retirement power plants are a powered-land signal: the site keeps its grid
connection after the plant closes. EIA-860M states both (its Retired sheet, and the planned
retirement columns of its Operating sheet), and they extend the existing `power_plant` assets
rather than forming a new type, so one EIA plant id stays one record whether it is operating,
retiring or retired (`docs/27` section R1).

What it does
------------
1. Recreates the asset status CHECK with the five-value list
   `('operating', 'standby', 'retiring', 'retired', 'unknown')`. Inside `batch_alter_table`,
   because SQLite cannot ALTER a CHECK in place; on PostgreSQL a `DROP` / `ADD` pair.
2. Adds `asset.retirement_year integer NULL` and `ix_asset_retirement_year`: the year a retired
   plant finished retiring, else the year its next unit is scheduled to, so `GET /v1/assets` can
   filter on it. The detail lives in `asset.attributes["retirement"]`; no other column is added.

Both steps check what is present first (0020/0026's convention), because the SQLite round-trip tests
build the current `Base.metadata`, which already has both, and stamp an earlier revision.

Downgrade refuses while any asset is `retiring`
-----------------------------------------------
The four-value CHECK would reject those rows (on PostgreSQL the `ADD CONSTRAINT` fails halfway; on
SQLite the table recreate would carry them silently). Reload the plants from a pre-R1 frame, or
set them back to `operating`, first. The column and its index are dropped without refusal: every
value is re-derivable from the workbook by `pipeline/context/eia_plants.py`.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "asset"
COLUMN = "retirement_year"
INDEX = "ix_asset_retirement_year"
CHECK_SUFFIX = "status_vocab"
#: Frozen here rather than imported from `services.db.models` (0020-0026's convention).
OLD_STATUSES = ("operating", "standby", "retired", "unknown")
NEW_STATUSES = ("operating", "standby", "retiring", "retired", "unknown")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _indexes() -> set[str]:
    return {str(i["name"]) for i in sa.inspect(op.get_bind()).get_indexes(TABLE) if i.get("name")}


def _check(table: str, suffix: str) -> tuple[str, str] | None:
    """`(name, sqltext)` of the CHECK on `table` whose name ends with `suffix`, or None."""
    try:
        checks = sa.inspect(op.get_bind()).get_check_constraints(table)
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for c in checks:
        name = c.get("name")
        if name and (name == suffix or str(name).endswith(f"_{suffix}")):
            return str(name), str(c.get("sqltext") or "")
    return None


def _swap_status_check(statuses: tuple[str, ...]) -> None:
    existing = _check(TABLE, CHECK_SUFFIX)
    if (
        existing is not None
        and all(f"'{s}'" in existing[1] for s in statuses)
        and (existing[1].count("'") == 2 * len(statuses))
    ):
        return  # already exactly this vocabulary (a store the ORM built)
    # The name is kept as found: 0009 created it as `status_vocab` and its own downgrade drops it
    # by that name, while a store the ORM built spells it `ck_asset_status_vocab`.
    name = existing[0] if existing is not None else f"ck_{TABLE}_{CHECK_SUFFIX}"
    with op.batch_alter_table(TABLE) as batch:
        if existing is not None:
            batch.drop_constraint(existing[0], type_="check")
        batch.create_check_constraint(name, f"status IN {statuses!r}")


def retiring_rows() -> int:
    """How many assets carry `retiring`; `downgrade()` refuses on any. Exposed for the test."""
    if not _has_table(TABLE):
        return 0
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM asset WHERE status = 'retiring'")).scalar()
    return int(n or 0)


def upgrade() -> None:
    if not _has_table(TABLE):
        return  # the reduced baselines of services/db/test_migrations.py may carry no `asset`
    _swap_status_check(NEW_STATUSES)
    if COLUMN not in _columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Integer, nullable=True))
    if INDEX not in _indexes():
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    n = retiring_rows()
    if n:
        raise RuntimeError(
            f"0030 downgrade refused: {n} asset rows are 'retiring', which the previous CHECK rejects. "
            "Reload the plants from a pre-R1 frame or set them to 'operating' first."
        )
    if INDEX in _indexes():
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
    _swap_status_check(OLD_STATUSES)
