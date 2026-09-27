"""`organization.publish_state` — the record-level publish gate for organisations.

Closes docs/40-launch-runbook.md §6 item 2 ("Organisation takedown is a 400"): `PUT
/admin/v1/records/organizations/{public_id}/publish-state` refused every request because the
column it would have written did not exist (`services/api/admin_records.py`, former decision 5).
`proposal` and `opportunity` have carried `publish_state` since 0001 with the vocabulary
`services/db/models.py::RECORD_PUBLISH_STATES` names and a CHECK enforces; this migration gives
`organization` the same column, the same vocabulary and the same CHECK, so one admin route and
one visibility predicate (`services/api/visibility.py::organization_visibility_filter`) cover all
three record types.

Default `public`, and why
-------------------------
Organisations are derived data — a canonical name, a type, a country, the identifiers a register
states — and every public surface (`GET /v1/organizations`, the company page, the sponsor and
issuer embeds, the asset owners table, the sitemap) has always shown every row with no
record-level gate at all. The state every existing organisation is *effectively* in today is
therefore `public`, and `server_default='public'` is the only backfill that changes nothing a
reader can see. `pending_review` (the proposal default) would take every company page off the
site in the same deploy that added the column. The ORM default (`services/db/models.py`) is the
same value for the same reason: a loader that creates an organisation while resolving a sponsor
must not create an invisible one.

No `published_at` / `public_at` pair
------------------------------------
Nothing on an organisation is time-gated (docs/21 §5.4: the paywall is by shape, not by time, and
organisations have never had a lag), so the predicate reads the state alone and the index is a
plain BTREE on `publish_state` (as `source` has), not the `(publish_state, public_at DESC)`
composite `proposal`/`opportunity` carry for their time-ordered public list.

Guards
------
Every step is guarded on what is present, not on the revision number, because a database built by
`Base.metadata.create_all` and then stamped — how the SQLite round-trip tests exercise this chain
— already carries the column, the CHECK and the index from the ORM model, and none may be added
twice. The CHECK is matched by suffix (`publish_state_vocab`) because the metadata's naming
convention prefixes it (`ck_organization_publish_state_vocab`) and a database built by the ORM
and one built by this migration need not spell it identically (0018/0020's convention).

Downgrade refuses while any organisation is not `public`
--------------------------------------------------------
Dropping the column re-publishes every row: an organisation taken down on a rights-holder's
request would be back on every surface the moment the schema rolled back, with no event recording
that it happened. `downgrade()` therefore raises, naming the count, if any row carries a state
other than `public`. Republishing them first is an operator decision made through the audited
admin route, not something a schema rollback may take silently (0020's pattern for the
`noncommercial` tag).

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "organization"
COLUMN = "publish_state"
DEFAULT_STATE = "public"
CONSTRAINT_SUFFIX = "publish_state_vocab"
CONSTRAINT = f"ck_{TABLE}_{CONSTRAINT_SUFFIX}"
INDEX = "ix_organization_publish_state"
#: Frozen here rather than imported from `services.db.models`, as 0001 and 0020 did: a migration
#: states the vocabulary it wrote, and the model may move on after it.
RECORD_PUBLISH_STATES = ("pending_review", "ingest_only", "api_only", "public", "unpublished")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column() -> bool:
    return COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _has_index() -> bool:
    return INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def _check_constraint_name() -> str | None:
    """The reflected name of this revision's CHECK, or `None` (matched by suffix; see docstring)."""
    inspector = sa.inspect(op.get_bind())
    try:
        names = [c["name"] for c in inspector.get_check_constraints(TABLE) if c.get("name")]
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for name in names:
        if name == CONSTRAINT_SUFFIX or name.endswith(f"_{CONSTRAINT_SUFFIX}"):
            return str(name)
    return None


def rows_not_public() -> int:
    """How many organisations carry a state other than `public`. Exposed for the round-trip test;
    `downgrade()` refuses on any non-zero count."""
    if not _has_table(TABLE) or not _has_column():
        return 0
    n = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} <> :s"),  # noqa: S608 -- fixed names
            {"s": DEFAULT_STATE},
        )
        .scalar()
    )
    return int(n or 0)


def upgrade() -> None:
    if not _has_table(TABLE):
        return
    if not _has_column():
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.Text(), nullable=False, server_default=DEFAULT_STATE),
        )
    if _check_constraint_name() is None:
        with op.batch_alter_table(TABLE) as batch:
            batch.create_check_constraint(CONSTRAINT, f"{COLUMN} IN {RECORD_PUBLISH_STATES!r}")
    if not _has_index():
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    carrying = rows_not_public()
    if carrying:
        raise RuntimeError(
            f"0022 downgrade refused: {carrying} organization row(s) carry a publish_state other "
            f"than {DEFAULT_STATE!r}; dropping the column would republish them. Republish or "
            "keep them through PUT /admin/v1/records/organizations/{public_id}/publish-state "
            "first (docs/21 §8); a schema rollback may not undo a takedown."
        )
    if _has_index():
        op.drop_index(INDEX, table_name=TABLE)
    constraint = _check_constraint_name()
    if constraint is None and not _has_column():
        return
    # One batch, so SQLite's table recreate happens once: dropping the constraint first is what
    # lets the column go (SQLite refuses `DROP COLUMN` on a column named in a CHECK).
    with op.batch_alter_table(TABLE) as batch:
        if constraint is not None:
            batch.drop_constraint(constraint, type_="check")
        if _has_column():
            batch.drop_column(COLUMN)
