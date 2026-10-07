"""`organization.personal_data`; `privacy_request.due_at`; API-key licence acceptance nullable
(2026-09-30 legal audit L-3, L-5, L-6; docs/13 §5.4, §5.5).

What it does
------------
1. Adds `organization.personal_data boolean NOT NULL DEFAULT false` and
   `organization.personal_data_basis text`, then classifies every existing row with
   `services/personal_names.py` (the same rule the ORM default applies to new rows). A flagged row
   stays public; the public surfaces leave it out of the sitemap, serve it `noindex` and show no
   ownership share (docs/13 §5.5).
2. Adds `privacy_request.due_at timestamptz NOT NULL`, backfilled as `created_at + 30 days`
   (docs/13 §5.4 rule 6), with an index on `(status, due_at)` for the overdue view.
3. Makes `api_key.licence_accepted_version` and `licence_accepted_at` nullable: no API licence is
   published yet, so new keys record no acceptance (docs/13-legal-customer-terms.md). Existing rows
   keep the value they were written with; it records what the code asserted at the time, and the
   draft notes that it is not evidence of acceptance of any published document.

Each step checks what is present first (0020/0026/0031's convention), because the SQLite round-trip
tests build the current `Base.metadata`, which already has the columns, and stamp an earlier revision.

The classification in step 1 imports the classifier rather than copying it (0018 imports
`services.ingest.vintage` the same way). Later changes to the rule or to the curated file reach the
store through `python -m services.ingest.personal_data classify`, not by re-running this revision.

Downgrade drops the two organisation columns and `due_at`. It refuses while any API key has a NULL
acceptance: restoring NOT NULL would need a value, and inventing one is exactly what L-3 removed.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG = "organization"
PRIVACY = "privacy_request"
KEY = "api_key"
DUE_INDEX = "ix_privacy_request_status_due"
#: Kept literal (not imported) so the revision's backfill stays what it was when written.
RESPONSE_DAYS = 30


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> dict[str, dict[str, object]]:
    return {str(c["name"]): dict(c) for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {str(i["name"]) for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _classify_organizations() -> None:
    from services.personal_names import classify

    bind = op.get_bind()
    org = sa.table(
        ORG,
        sa.column("id"),
        sa.column("name_canonical", sa.Text),
        sa.column("personal_data", sa.Boolean),
        sa.column("personal_data_basis", sa.Text),
    )
    rows = bind.execute(sa.select(org.c.id, org.c.name_canonical)).all()
    for row_id, name in rows:
        result = classify(name)
        bind.execute(
            org.update()
            .where(org.c.id == row_id)
            .values(personal_data=result.personal, personal_data_basis=result.basis)
        )


def upgrade() -> None:
    if _has_table(ORG):
        cols = _columns(ORG)
        added = False
        with op.batch_alter_table(ORG) as batch:
            if "personal_data" not in cols:
                batch.add_column(
                    sa.Column("personal_data", sa.Boolean, nullable=False, server_default=sa.false())
                )
                added = True
            if "personal_data_basis" not in cols:
                batch.add_column(sa.Column("personal_data_basis", sa.Text, nullable=True))
        if added:
            _classify_organizations()

    if _has_table(PRIVACY):
        if "due_at" not in _columns(PRIVACY):
            with op.batch_alter_table(PRIVACY) as batch:
                batch.add_column(sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=True))
            bind = op.get_bind()
            if bind.dialect.name == "postgresql":
                op.execute(f"UPDATE {PRIVACY} SET due_at = created_at + interval '{RESPONSE_DAYS} days'")  # noqa: S608
            else:
                op.execute(f"UPDATE {PRIVACY} SET due_at = datetime(created_at, '+{RESPONSE_DAYS} days')")  # noqa: S608
            with op.batch_alter_table(PRIVACY) as batch:
                batch.alter_column("due_at", existing_type=sa.TIMESTAMP(timezone=True), nullable=False)
        if DUE_INDEX not in _indexes(PRIVACY):
            op.create_index(DUE_INDEX, PRIVACY, ["status", "due_at"])

    if _has_table(KEY):
        cols = _columns(KEY)
        if not cols.get("licence_accepted_version", {}).get("nullable", True) or not cols.get(
            "licence_accepted_at", {}
        ).get("nullable", True):
            with op.batch_alter_table(KEY) as batch:
                batch.alter_column("licence_accepted_version", existing_type=sa.Text, nullable=True)
                batch.alter_column(
                    "licence_accepted_at", existing_type=sa.TIMESTAMP(timezone=True), nullable=True
                )


def downgrade() -> None:
    if _has_table(KEY):
        bind = op.get_bind()
        unaccepted = bind.execute(
            sa.text(f"SELECT count(*) FROM {KEY} WHERE licence_accepted_version IS NULL")  # noqa: S608
        ).scalar()
        if unaccepted:
            raise RuntimeError(
                f"{unaccepted} api_key row(s) record no licence acceptance; downgrading would need an "
                "invented acceptance to restore NOT NULL (2026-09-30 legal audit L-3). Revoke or "
                "delete those keys first."
            )
        with op.batch_alter_table(KEY) as batch:
            batch.alter_column("licence_accepted_version", existing_type=sa.Text, nullable=False)
            batch.alter_column(
                "licence_accepted_at",
                existing_type=sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
    if _has_table(PRIVACY):
        if DUE_INDEX in _indexes(PRIVACY):
            op.drop_index(DUE_INDEX, table_name=PRIVACY)
        if "due_at" in _columns(PRIVACY):
            with op.batch_alter_table(PRIVACY) as batch:
                batch.drop_column("due_at")
    if _has_table(ORG):
        cols = _columns(ORG)
        with op.batch_alter_table(ORG) as batch:
            if "personal_data_basis" in cols:
                batch.drop_column("personal_data_basis")
            if "personal_data" in cols:
                batch.drop_column("personal_data")
