"""`source_run`: who released a data-quality hold, and a CHECK on `trigger`.

Two measured defects, one table:

1. **A DQ hold could only be released with a database edit.** `pipeline/connectors/dq.py` holds a
   run whose output drifts past a threshold (docs/04 DA-6): the runner writes the frame to
   `held/` instead of `normalized/`, ends the run `partial`, and the scheduler never enqueues its
   load. `POST /admin/v1/source-runs/{run_id}/release` (services/api/admin_sources.py) is the
   audited way out; these three nullable columns record who released it, when and why, next to
   the run they apply to. The append-only audit event the route writes carries the same facts, so
   the columns are a convenience for the run views, not the only copy.

2. **`trigger` had no vocabulary in the schema.** docs/21 §4.2 and `api/openapi.yaml` `SourceRun`
   say `schedule | manual | backfill | retry`; the scheduler wrote `scheduled` (off-vocabulary)
   into every row it recorded between 2026-09-18 and this revision, and even that only when the
   runner's own `manual` did not win first. Upgrade rewrites `scheduled` to `schedule`, then adds
   `ck_source_run_trigger_vocab`. Any other off-vocabulary value makes the CHECK creation fail on
   Postgres, loudly, rather than being guessed into a bucket.

Guards follow 0022: every step checks what is present, because the SQLite round-trip tests build
the current `Base.metadata` (which already carries the columns and the CHECK) and stamp an earlier
revision. The CHECK is matched by suffix (`trigger_vocab`), as 0018/0020/0022 match theirs.

Downgrade drops the CHECK and the three columns. It does not refuse: the release facts survive in
the audit `event` rows (append-only, docs/04 DA-3), and `schedule` was already the documented
value before this revision, so nothing is rewritten back to `scheduled`.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "source_run"
RELEASE_COLUMNS = ("released_at", "released_by", "release_reason")
CONSTRAINT_SUFFIX = "trigger_vocab"
CONSTRAINT = f"ck_{TABLE}_{CONSTRAINT_SUFFIX}"
FK_NAME = "fk_source_run_released_by_user"
#: Frozen here rather than imported from `services.db.models` (0020-0024's convention): a
#: migration states the vocabulary it wrote, and the model may move on after it.
SOURCE_RUN_TRIGGERS = ("schedule", "manual", "backfill", "retry")
LEGACY_TRIGGER_ALIASES = {"scheduled": "schedule"}


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _check_constraint_name() -> str | None:
    inspector = sa.inspect(op.get_bind())
    try:
        names = [c["name"] for c in inspector.get_check_constraints(TABLE) if c.get("name")]
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for name in names:
        if name == CONSTRAINT_SUFFIX or name.endswith(f"_{CONSTRAINT_SUFFIX}"):
            return str(name)
    return None


def _released_by_fk_name() -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(TABLE):
        if fk.get("constrained_columns") == ["released_by"]:
            return str(fk["name"]) if fk.get("name") else None
    return None


def upgrade() -> None:
    if not _has_table(TABLE):
        return
    present = _columns()
    missing = [c for c in RELEASE_COLUMNS if c not in present]
    if missing:
        # One batch: SQLite recreates the table once, and the FK is created with a name so the
        # downgrade can drop it on Postgres.
        with op.batch_alter_table(TABLE) as batch:
            if "released_at" in missing:
                batch.add_column(sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=True))
            if "released_by" in missing:
                batch.add_column(sa.Column("released_by", GUID(), nullable=True))
                batch.create_foreign_key(FK_NAME, "user", ["released_by"], ["id"])
            if "release_reason" in missing:
                batch.add_column(sa.Column("release_reason", sa.Text(), nullable=True))
    for legacy, canonical in LEGACY_TRIGGER_ALIASES.items():
        op.get_bind().execute(
            sa.text(f"UPDATE {TABLE} SET trigger = :canonical WHERE trigger = :legacy"),  # noqa: S608
            {"canonical": canonical, "legacy": legacy},
        )
    if _check_constraint_name() is None:
        with op.batch_alter_table(TABLE) as batch:
            batch.create_check_constraint(CONSTRAINT, f"trigger IN {SOURCE_RUN_TRIGGERS!r}")


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    constraint = _check_constraint_name()
    present = _columns()
    dropping = [c for c in RELEASE_COLUMNS if c in present]
    if constraint is None and not dropping:
        return
    fk = _released_by_fk_name() if "released_by" in present else None
    with op.batch_alter_table(TABLE) as batch:
        if constraint is not None:
            batch.drop_constraint(constraint, type_="check")
        if fk is not None:
            batch.drop_constraint(fk, type_="foreignkey")
        for column in dropping:
            batch.drop_column(column)
