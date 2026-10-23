"""The tracker's item types: projects, bids and investments on one list (decision row 2026-10-23, "My
pipeline is one central tracker"; docs/19 as amended; docs/30 §11.4).

`private_project` gains:

- `kind` text NOT NULL, default `project` (so every existing row is a project), CHECK `project`,
  `bid` or `investment`;
- a stage vocabulary per kind, one CHECK (`stage_for_kind`) in place of 0040's `stage_vocab`:
  project prospecting to operating (unchanged); bid considering, preparing, submitted, shortlisted,
  won, lost, withdrawn; investment screening, diligence, term sheet, committed, closed, passed;
- `parent_project_id` uuid NULL, a foreign key to `private_project` ON DELETE SET NULL: the project a
  bid is for. Only a bid may have one (`parent_bid_only`), never itself (`parent_not_self`); that it is
  one of the user's own projects they can edit is checked in `services.projects.authz`;
- `submitted_on` date NULL, bids only (`submitted_bid_only`).

`private_project_member` gains the member's own view of the item: `last_viewed_at` timestamptz NULL
(what "changed since your last visit" counts from) and `email_changes` boolean NOT NULL default
false (the explicit opt-in per item; stored only, no email is sent).

Guards follow 0040/0041: a SQLite store built by `create_all` from today's models already has the
columns and is stamped, so each step checks what is present. On SQLite the constraint changes go
through `batch_alter_table` (a table copy), as 0020 does.

**Downgrade** drops the new columns and constraints and restores 0040's project-only `stage_vocab`.
It refuses while any bid or investment exists, naming the counts, because the 0041 schema cannot
hold one and a schema rollback must not delete a user's items or turn a bid into a project
(0020's precedent). Before launch (`MY_PIPELINE` off) there are none and it runs clean; after,
take a backup and decide what to do with those items first.

Revision ID: 0042
Revises: 0039 (the site finder; 0039 revises 0041)
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0042"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.projects.kinds` (0020-0041's convention);
#: `services/projects/test_migration.py` pins them equal.
ITEM_KINDS = ("project", "bid", "investment")
PROJECT_STAGES = ("prospecting", "site_control", "development", "ready_to_build", "construction", "operating")
KIND_STAGES = {
    "project": PROJECT_STAGES,
    "bid": ("considering", "preparing", "submitted", "shortlisted", "won", "lost", "withdrawn"),
    "investment": ("screening", "diligence", "term_sheet", "committed", "closed", "passed"),
}

PROJECT = "private_project"
MEMBER = "private_project_member"
PARENT_FK = "fk_private_project_parent_project_id_private_project"
PARENT_INDEX = "ix_private_project_parent_project_id"


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


#: (constraint suffix, condition) for every CHECK this revision adds to `private_project`.
NEW_CHECKS: tuple[tuple[str, str], ...] = (
    ("kind_vocab", _in("kind", ITEM_KINDS)),
    (
        "stage_for_kind",
        " OR ".join(f"(kind = {kind!r} AND {_in('stage', KIND_STAGES[kind])})" for kind in ITEM_KINDS),
    ),
    ("parent_bid_only", "parent_project_id IS NULL OR kind = 'bid'"),
    ("parent_not_self", "parent_project_id IS NULL OR parent_project_id <> id"),
    ("submitted_bid_only", "submitted_on IS NULL OR kind = 'bid'"),
)
OLD_STAGE_CHECK = ("stage_vocab", _in("stage", PROJECT_STAGES))


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _check_constraint_name(table: str, suffix: str) -> str | None:
    """The reflected name of a CHECK on `table` ending in `suffix` (0020's helper): a store built by
    the ORM and one built by 0040 need not spell it identically."""
    try:
        names = [c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table) if c.get("name")]
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for name in names:
        if name == suffix or name.endswith(f"_{suffix}"):
            return str(name)
    return None


def _has_index(table: str, name: str) -> bool:
    return any(index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def _has_foreign_key(table: str, column: str) -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if fk["constrained_columns"] == [column]:
            return fk.get("name") or None
    return None


def items_that_are_not_projects() -> dict[str, int]:
    """`{kind: count}` of the bids and investments a downgrade would have nowhere to put. Exposed for
    the round-trip test; `downgrade()` refuses on any."""
    if not _has_table(PROJECT) or "kind" not in _columns(PROJECT):
        return {}
    rows = op.get_bind().execute(
        sa.text(f"SELECT kind, count(*) FROM {PROJECT} WHERE kind <> 'project' GROUP BY kind")  # noqa: S608 -- fixed names
    )
    return {str(kind): int(count) for kind, count in rows}


#: SQLite rebuilds a table in batch mode by reflecting it, and 0040 created these tables with foreign
#: keys to `user` whether or not `user` exists (SQLite allows it): a store built from an old baseline
#: (services/db/test_migrations.py) has no `user` table, so resolving those keys raises
#: NoSuchTableError. The keys are copied as declared, unresolved. Postgres alters in place.
_NO_FK_RESOLVE: dict[str, bool] = {"resolve_fks": False}


def upgrade() -> None:
    if _has_table(PROJECT) and "kind" not in _columns(PROJECT):
        old_stage = _check_constraint_name(PROJECT, OLD_STAGE_CHECK[0])
        with op.batch_alter_table(PROJECT, reflect_kwargs=_NO_FK_RESOLVE) as batch:
            batch.add_column(sa.Column("kind", sa.Text, nullable=False, server_default="project"))
            batch.add_column(sa.Column("parent_project_id", GUID(), nullable=True))
            batch.add_column(sa.Column("submitted_on", sa.Date, nullable=True))
            if old_stage is not None:
                batch.drop_constraint(old_stage, type_="check")
            # Named in full: `env.py` passes no metadata here, so the naming convention does not apply.
            for suffix, condition in NEW_CHECKS:
                batch.create_check_constraint(f"ck_{PROJECT}_{suffix}", condition)
            batch.create_foreign_key(PARENT_FK, PROJECT, ["parent_project_id"], ["id"], ondelete="SET NULL")
    if (
        _has_table(PROJECT)
        and "parent_project_id" in _columns(PROJECT)
        and not _has_index(PROJECT, PARENT_INDEX)
    ):
        op.create_index(PARENT_INDEX, PROJECT, ["parent_project_id"])
    if _has_table(MEMBER) and "last_viewed_at" not in _columns(MEMBER):
        with op.batch_alter_table(MEMBER, reflect_kwargs=_NO_FK_RESOLVE) as batch:
            batch.add_column(sa.Column("last_viewed_at", sa.TIMESTAMP(timezone=True), nullable=True))
            batch.add_column(
                sa.Column("email_changes", sa.Boolean, nullable=False, server_default=sa.false())
            )


def downgrade() -> None:
    waiting = items_that_are_not_projects()
    if waiting:
        detail = ", ".join(f"{kind}={n}" for kind, n in sorted(waiting.items()))
        raise RuntimeError(
            f"0042 downgrade refused: the tracker holds items that are not projects ({detail}). "
            "The 0041 schema has no place for them, and a schema rollback must not delete them or "
            "turn them into projects: back up the store and decide what happens to them first."
        )
    if _has_table(MEMBER) and "last_viewed_at" in _columns(MEMBER):
        with op.batch_alter_table(MEMBER, reflect_kwargs=_NO_FK_RESOLVE) as batch:
            batch.drop_column("email_changes")
            batch.drop_column("last_viewed_at")
    if not _has_table(PROJECT) or "kind" not in _columns(PROJECT):
        return
    if _has_index(PROJECT, PARENT_INDEX):
        op.drop_index(PARENT_INDEX, table_name=PROJECT)
    present = [name for suffix, _condition in NEW_CHECKS if (name := _check_constraint_name(PROJECT, suffix))]
    parent_fk = _has_foreign_key(PROJECT, "parent_project_id")
    with op.batch_alter_table(PROJECT, reflect_kwargs=_NO_FK_RESOLVE) as batch:
        if parent_fk is not None:
            batch.drop_constraint(parent_fk, type_="foreignkey")
        for name in present:
            batch.drop_constraint(name, type_="check")
        batch.drop_column("submitted_on")
        batch.drop_column("parent_project_id")
        batch.drop_column("kind")
        batch.create_check_constraint(f"ck_{PROJECT}_{OLD_STAGE_CHECK[0]}", OLD_STAGE_CHECK[1])
