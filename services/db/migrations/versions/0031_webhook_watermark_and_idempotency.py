"""`webhook_endpoint.watermark_seq`; the `idempotency_record` table (backend audit 2026-09-30 F2, F12).

What it does
------------
1. Adds `webhook_endpoint.watermark_seq bigint NOT NULL DEFAULT 0`: the highest `event.seq` the
   alert tick has considered for the endpoint, the `saved_search.watermark_seq` pattern. Before
   it, no production path created a delivery for a real change (`services/alerts/webhooks.py
   ::enqueue_new_deliveries`). Existing endpoints are set to the current head of the event log, so
   the first tick after deploy sends the changes that follow it, not every event ever recorded;
   history is `POST /v1/webhooks/{id}/replay`.
2. Creates `idempotency_record`: one stored response per `(scope, idempotency_key)` for 24 hours,
   so a retried mutating call returns the original response instead of acting twice
   (`services/api/idempotency.py`; api/openapi.yaml `IdempotencyKey`).

Both steps check what is present first (0020/0026's convention), because the SQLite round-trip
tests build the current `Base.metadata`, which already has both, and stamp an earlier revision.

Downgrade drops the table and the column. Nothing in either is source data: stored responses
expire within a day, and a lost watermark is reset to the head by the next upgrade.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENDPOINT = "webhook_endpoint"
WATERMARK = "watermark_seq"
IDEMPOTENCY = "idempotency_record"
IDEMPOTENCY_EXPIRY_INDEX = "ix_idempotency_record_expires_at"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _has_table(ENDPOINT) and WATERMARK not in _columns(ENDPOINT):
        with op.batch_alter_table(ENDPOINT) as batch:
            batch.add_column(sa.Column(WATERMARK, sa.BigInteger, nullable=False, server_default="0"))
        if _has_table("event"):
            op.execute(
                f"UPDATE {ENDPOINT} SET {WATERMARK} = (SELECT COALESCE(MAX(seq), 0) FROM event)"  # noqa: S608
            )
    if not _has_table(IDEMPOTENCY):
        op.create_table(
            IDEMPOTENCY,
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("scope", sa.Text, nullable=False),
            sa.Column("idempotency_key", sa.Text, nullable=False),
            sa.Column("method", sa.Text, nullable=False),
            sa.Column("path", sa.Text, nullable=False),
            sa.Column("request_hash", sa.Text, nullable=False),
            sa.Column("status_code", sa.Integer, nullable=False),
            sa.Column("response_headers", JSONVariant(), nullable=False),
            sa.Column("response_body", sa.LargeBinary, nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_record_scope_key"),
        )
        op.create_index(IDEMPOTENCY_EXPIRY_INDEX, IDEMPOTENCY, ["expires_at"])


def downgrade() -> None:
    if _has_table(IDEMPOTENCY):
        op.drop_index(IDEMPOTENCY_EXPIRY_INDEX, table_name=IDEMPOTENCY)
        op.drop_table(IDEMPOTENCY)
    if _has_table(ENDPOINT) and WATERMARK in _columns(ENDPOINT):
        with op.batch_alter_table(ENDPOINT) as batch:
            batch.drop_column(WATERMARK)
