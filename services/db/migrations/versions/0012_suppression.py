"""`suppression` and `privacy_request` (docs/50-audit-2026-09-18.md §3.1 "erasure and suppression";
US-910).

Two new tables, no change to an existing one, so no batch mode is needed on either dialect:

- `suppression`: a salted hash of an email address plus why it must never be written to again
  (`erasure | unsubscribe | bounce | complaint`). Written on erasure (`services/api/admin_people.py`)
  and on unsubscribe (`services/api/unsubscribe_routes.py`), read by the alerts sender
  (`services/alerts/suppression.py`) before any address is used.
- `privacy_request`: the filer-deletion route the privacy notice promised and the code did not
  have — `POST /v1/privacy/requests` stores `{kind, record public_id, contact email, message}`;
  operators read it under `/admin/v1/privacy-requests`.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUPPRESSION_REASONS = ("erasure", "unsubscribe", "bounce", "complaint")
PRIVACY_REQUEST_KINDS = ("erasure", "correction")
PRIVACY_REQUEST_STATUSES = ("open", "in_progress", "done", "rejected")


def upgrade() -> None:
    op.create_table(
        "suppression",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("email_hash", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"reason IN {SUPPRESSION_REASONS!r}", name="reason_vocab"),
        sa.UniqueConstraint("email_hash", "reason", name="uq_suppression_email_hash_reason"),
    )
    op.create_index("ix_suppression_email_hash", "suppression", ["email_hash"])

    op.create_table(
        "privacy_request",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("record_public_id", sa.Text, nullable=False),
        sa.Column("contact_email", sa.Text, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN {PRIVACY_REQUEST_KINDS!r}", name="kind_vocab"),
        sa.CheckConstraint(f"status IN {PRIVACY_REQUEST_STATUSES!r}", name="status_vocab"),
    )
    op.create_index("ix_privacy_request_status_created", "privacy_request", ["status", "created_at"])
    op.create_index("ix_privacy_request_record", "privacy_request", ["record_public_id"])


def downgrade() -> None:
    op.drop_index("ix_privacy_request_record", table_name="privacy_request")
    op.drop_index("ix_privacy_request_status_created", table_name="privacy_request")
    op.drop_table("privacy_request")
    op.drop_index("ix_suppression_email_hash", table_name="suppression")
    op.drop_table("suppression")
