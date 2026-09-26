"""`noncommercial` joins the reuse-class vocabulary; the class and `allows_commercial_use` must agree.

Owner decision, 2026-09-25 (`docs/00-PLAN.md` decisions log; `docs/26-platform-posture.md`),
verbatim: "Let's move forward as a noncommercial platform for now for maximum and best data
access. Then decide how to proceed once we're done."

Why a vocabulary value and not a flag
-------------------------------------
The posture will change back. Every row ingested under a noncommercial grant is a liability at
that switch unless it is tagged now, and the tag has to be the thing the gates already read:
`licence.reuse_class` and the `min_reuse_class` the records carry. So `noncommercial` becomes a
fifth value of the vocabulary that `services/db/models.py::REUSE_CLASSES` names and three CHECK
constraints enforce (docs/21 §1: enums are `text` + `CHECK`, never a native enum, precisely so
that adding a value is a constraint swap rather than a lock-taking type change). Whether the
class is *publishable* is not this migration's business: that is `PLATFORM_POSTURE`
(`services/posture.py`), read by every gate at process start, and under its default
(`commercial`) a `noncommercial` row is gated exactly like `restricted`/`unknown`. Nothing in the
store changes behaviour because this migration ran.

What it does
------------
1. Recreates `ck_licence_reuse_class_vocab`, `ck_proposal_min_reuse_class_vocab` and
   `ck_opportunity_min_reuse_class_vocab` with the five-value list. Inside `batch_alter_table`,
   because SQLite cannot ALTER a CHECK in place and recreates the table there; on PostgreSQL it
   is a plain `DROP CONSTRAINT` / `ADD CONSTRAINT` pair.
2. Adds `ck_licence_noncommercial_no_commercial_use`:
   `reuse_class <> 'noncommercial' OR NOT allows_commercial_use`. A noncommercial grant is by
   definition a licence that does not permit commercial use; the boolean already on `licence`
   says so, and a row where the two disagree is refused rather than served. The converse is
   deliberately not constrained: the loader records `allows_commercial_use = false` for every
   `attribution` licence because the manifest's `attribution` class spans both `open-attribution`
   and `attribution-restricted` register rows (docs/13 §0), so `false` there means "not verified".

Both steps are guarded on what is actually present rather than on the revision number, because
a database built by `Base.metadata.create_all` and then stamped — how the migration tests
exercise this chain on SQLite — already carries the five-value CHECK and the consistency CHECK
from the ORM model, and neither may be added twice.

Downgrade refuses if any row carries the class
----------------------------------------------
`downgrade()` restores the four-value CHECKs and drops the consistency CHECK **only if no row in
`licence`, `proposal` or `opportunity` carries `noncommercial`**. If any does, it raises before
touching the schema and names the count per table: a downgrade that succeeded would leave rows
the old CHECK forbids (on PostgreSQL the `ADD CONSTRAINT` would fail anyway, halfway through; on
SQLite the table recreate would silently carry them), and — the real point — those rows are the
liability the class exists to track. What to do with them is the purge-or-relicence decision per
source that `docs/26` §5 describes and `scripts/posture_report.py` lists; it is an owner
decision, not something a schema rollback may take by dropping the tag.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CLASS = "noncommercial"
#: Frozen here rather than imported from `services.db.models`, as 0001 did: a migration states
#: the vocabulary it wrote, and the model may move on after it.
OLD_REUSE_CLASSES = ("open", "attribution", "restricted", "unknown")
NEW_REUSE_CLASSES = ("open", "attribution", "noncommercial", "restricted", "unknown")

#: (table, column, constraint suffix) for every CHECK that names the vocabulary.
VOCAB_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("licence", "reuse_class", "reuse_class_vocab"),
    ("proposal", "min_reuse_class", "min_reuse_class_vocab"),
    ("opportunity", "min_reuse_class", "min_reuse_class_vocab"),
)
CONSISTENCY_CHECK = "noncommercial_no_commercial_use"
CONSISTENCY_SQL = f"reuse_class <> '{NEW_CLASS}' OR NOT allows_commercial_use"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _check_constraint_name(table: str, suffix: str) -> str | None:
    """The reflected name of a CHECK on `table`, or `None`. Matched by suffix because the
    metadata's naming convention prefixes it (`ck_<table>_...`) and a database built by the ORM
    and one built by 0001 need not spell it identically."""
    inspector = sa.inspect(op.get_bind())
    try:
        names = [c["name"] for c in inspector.get_check_constraints(table) if c.get("name")]
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for name in names:
        if name == suffix or name.endswith(f"_{suffix}"):
            return str(name)
    return None


def _swap_vocab_checks(classes: tuple[str, ...]) -> None:
    for table, column, suffix in VOCAB_CHECKS:
        if not _has_table(table):
            continue
        existing = _check_constraint_name(table, suffix)
        with op.batch_alter_table(table) as batch:
            if existing is not None:
                batch.drop_constraint(existing, type_="check")
            # Named in full: `env.py` passes no metadata to `upgrade`/`downgrade` (its docstring
            # says why), so the naming convention does not apply here and a bare suffix would
            # come out as `reuse_class_vocab` on PostgreSQL where 0001 wrote
            # `ck_licence_reuse_class_vocab`.
            batch.create_check_constraint(f"ck_{table}_{suffix}", f"{column} IN {classes!r}")


def rows_carrying_the_class() -> dict[str, int]:
    """`{table: count}` for every table whose vocabulary column holds `noncommercial`. Exposed
    for the round-trip test; `downgrade()` refuses on any non-zero count."""
    bind = op.get_bind()
    counts: dict[str, int] = {}
    for table, column, _suffix in VOCAB_CHECKS:
        if not _has_table(table):
            continue
        n = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} = :c"),  # noqa: S608 -- fixed names
            {"c": NEW_CLASS},
        ).scalar()
        counts[table] = int(n or 0)
    return counts


def _licence_has_the_flag() -> bool:
    """`services/db/test_migrations.py` stamps a partial 0008 baseline whose `licence` holds only
    `id`, `name`, `reuse_class`; the consistency CHECK names `allows_commercial_use`, so it is
    created only where that column exists. A real store always has it (0001)."""
    if not _has_table("licence"):
        return False
    return "allows_commercial_use" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("licence")}


def upgrade() -> None:
    _swap_vocab_checks(NEW_REUSE_CLASSES)
    if _licence_has_the_flag() and _check_constraint_name("licence", CONSISTENCY_CHECK) is None:
        with op.batch_alter_table("licence") as batch:
            batch.create_check_constraint(f"ck_licence_{CONSISTENCY_CHECK}", CONSISTENCY_SQL)


def downgrade() -> None:
    carrying = {table: n for table, n in rows_carrying_the_class().items() if n}
    if carrying:
        detail = ", ".join(f"{table}={n}" for table, n in sorted(carrying.items()))
        raise RuntimeError(
            f"0020 downgrade refused: rows still carry reuse class {NEW_CLASS!r} ({detail}). "
            "Purge or relicence them per source first (docs/26-platform-posture.md §5; "
            "scripts/posture_report.py lists them); a schema rollback may not drop the tag."
        )
    if _has_table("licence"):
        existing = _check_constraint_name("licence", CONSISTENCY_CHECK)
        if existing is not None:
            with op.batch_alter_table("licence") as batch:
                batch.drop_constraint(existing, type_="check")
    _swap_vocab_checks(OLD_REUSE_CLASSES)
