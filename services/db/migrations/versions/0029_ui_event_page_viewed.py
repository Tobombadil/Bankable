"""`ui_event`: admit `page.viewed` in the name vocabulary (owner decision 2026-09-30).

The alert-activation rate the owner's day-30 posture decision reads is alerts created per view of a
proposal, company, asset or grid-point page (docs/00-PLAN.md 2026-09-18 decision 8, 2026-09-30).
`alert.created` has been counted since 0008; nothing counted the views. This revision widens
`ck_ui_event_name_vocab` by one name. No column is added: a view is a row like every other counter
-- a name, `{"page_type": ...}` and a time, never a user, session, IP, user agent or referrer
(docs/21 §3.21) -- and `services/api/ui_events.py` accepts it only from the site's own server.

**Numbering.** Lane P2 took 0027 (event seq) and 0028 (organisation FK indexes) in parallel, so
this file is 0029 and revises 0028, which is not in this lane's worktree; the coordinator rebases
it onto P2 at integration (verified here by running the round trip with P2's two files beside it).
Nothing in the body depends on 0027 or 0028.

Guards follow 0025: the SQLite round-trip tests build the current `Base.metadata` (which already
carries the widened CHECK) and stamp an earlier revision, so each step reads what is present. The
CHECK is matched by suffix (`name_vocab`).

Downgrade refuses while any `page.viewed` row exists, naming the count, rather than deleting
measurements to make the narrower CHECK hold; delete them deliberately first if the rollback is
meant.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ui_event"
CONSTRAINT_SUFFIX = "name_vocab"
#: The name the model's naming convention gives the CHECK (`services/db/base.py`), passed through
#: `op.f` so no convention is applied to it a second time.
CONSTRAINT = f"ck_{TABLE}_{CONSTRAINT_SUFFIX}"
#: Frozen here rather than imported from `services.db.models` (0020-0026's convention).
NAMES_BEFORE = (
    "map.layer_toggled",
    "map.region_jumped",
    "map.basemap_failed",
    "auth.registered",
    "alert.created",
)
NAMES_AFTER = (*NAMES_BEFORE, "page.viewed")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _check_constraint() -> tuple[str, str] | None:
    """`(name, sqltext)` of the name-vocabulary CHECK, or `None` when absent or unreadable."""
    inspector = sa.inspect(op.get_bind())
    try:
        checks = inspector.get_check_constraints(TABLE)
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for check in checks:
        name = check.get("name") or ""
        if name == CONSTRAINT_SUFFIX or name.endswith(f"_{CONSTRAINT_SUFFIX}"):
            return str(name), str(check.get("sqltext") or "")
    return None


def _replace_check(names: tuple[str, ...]) -> None:
    present = _check_constraint()
    with op.batch_alter_table(TABLE) as batch:
        if present is not None:
            batch.drop_constraint(present[0], type_="check")
        batch.create_check_constraint(op.f(CONSTRAINT), f"name IN {names!r}")


def upgrade() -> None:
    if not _has_table(TABLE):
        return
    present = _check_constraint()
    if present is not None and "page.viewed" in present[1]:
        return
    _replace_check(NAMES_AFTER)


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM ui_event WHERE name = 'page.viewed'")).scalar()
    )
    if count:
        raise RuntimeError(
            f"refusing to downgrade 0029: {count} ui_event row(s) named 'page.viewed' exist; the "
            "narrower CHECK would reject them. Delete them deliberately first if the rollback is meant."
        )
    present = _check_constraint()
    if present is not None and "page.viewed" not in present[1]:
        return
    _replace_check(NAMES_BEFORE)
