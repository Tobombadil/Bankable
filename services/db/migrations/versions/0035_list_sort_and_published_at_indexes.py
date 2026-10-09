"""List-sort, paid-tier timing and data-version indexes (backend audit 2026-10-07, PERF-3, PERF-2).

1. **Paid-tier timing** (PERF-3). The visibility predicate reads `public_at` for the public tier and
   `published_at` for Pro and API (`services/api/visibility.py`). Only `public_at` was indexed, so
   every keyed read of `/v1/proposals` scanned the table. Twins of the public-tier indexes:
   `ix_proposal_publish_published_at`, `ix_opportunity_publish_published_at`, `ix_event_published_at`.
   Measured on SQLite (full dev store, 10,636 visible proposals, `limit=200` page SQL, median of 5):
   API key 141 -> 68 ms; the public tier is unchanged at ~70 ms.
2. **The four proposal list sorts** (PERF-3), each exactly as `services/api/pagination.py::paginate`
   orders it, as partial indexes over `publish_state = 'public'` (the only rows any non-admin list
   reads): `last_changed DESC, id DESC` (the default sort, the proposals feed, and the bulk sync
   walked backwards), `capacity_mw DESC NULLS LAST, id DESC`, `first_seen DESC, id DESC` and
   `name_canonical, id`. On Postgres this lets a page be read as a top-N index walk instead of a
   sort of the whole visible set (**inferred**: the audit store could not be loaded into Postgres
   in the measuring sandbox). SQLite has no `NULLS LAST` in `CREATE INDEX`; `DESC` there already
   puts NULLs last, so the SQLite index is the same order. SQLite's planner keeps choosing
   `ix_proposal_publish_public_at` for these queries whether or not the sort indexes exist
   (measured: plans and timings unchanged), so on SQLite they cost writes and buy nothing; they
   are created there anyway so a dev or test store has the production schema (PERF-12).
3. **`ix_proposal_updated_at`** (PERF-2). The proposal map's in-process cache is keyed on a data
   version that includes `max(proposal.updated_at)` (`services/api/geo_cache.py`); without an
   index that aggregate scanned the table on every map request (15 ms on the dev store).

The model declares every index here (`services/db/models.py`), so `Base.metadata.create_all` stores
(tests, the dev store) carry them too. Guards follow 0028: the SQLite round-trip tests build today's
`Base.metadata`, which already has these indexes, and stamp an earlier revision, so each step checks
what is present. Downgrade drops them.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLIC_ROWS = "publish_state = 'public'"

#: `(name, table, Postgres columns, SQLite columns, partial predicate or None)`. Kept literal (not
#: imported from the models) so the revision stays what it was when written.
INDEXES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str | None], ...] = (
    (
        "ix_proposal_publish_published_at",
        "proposal",
        ("publish_state", "published_at DESC"),
        ("publish_state", "published_at DESC"),
        None,
    ),
    (
        "ix_opportunity_publish_published_at",
        "opportunity",
        ("publish_state", "published_at DESC"),
        ("publish_state", "published_at DESC"),
        None,
    ),
    ("ix_event_published_at", "event", ("published_at",), ("published_at",), None),
    (
        "ix_proposal_public_last_changed",
        "proposal",
        ("last_changed DESC", "id DESC"),
        ("last_changed DESC", "id DESC"),
        PUBLIC_ROWS,
    ),
    (
        "ix_proposal_public_capacity_mw",
        "proposal",
        ("capacity_mw DESC NULLS LAST", "id DESC"),
        ("capacity_mw DESC", "id DESC"),
        PUBLIC_ROWS,
    ),
    (
        "ix_proposal_public_first_seen",
        "proposal",
        ("first_seen DESC", "id DESC"),
        ("first_seen DESC", "id DESC"),
        PUBLIC_ROWS,
    ),
    (
        "ix_proposal_public_name_canonical",
        "proposal",
        ("name_canonical", "id"),
        ("name_canonical", "id"),
        PUBLIC_ROWS,
    ),
    ("ix_proposal_updated_at", "proposal", ("updated_at",), ("updated_at",), None),
)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table):
        return set()
    return {str(i["name"]) for i in sa.inspect(bind).get_indexes(table) if i.get("name")}


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, table, pg_columns, sqlite_columns, where in INDEXES:
        if not _has_table(table) or name in _indexes(table):
            continue
        columns = pg_columns if postgres else sqlite_columns
        kwargs = {"postgresql_where": sa.text(where), "sqlite_where": sa.text(where)} if where else {}
        op.create_index(name, table, [sa.text(c) for c in columns], **kwargs)


def downgrade() -> None:
    for name, table, *_rest in INDEXES:
        if name in _indexes(table):
            op.drop_index(name, table_name=table)
