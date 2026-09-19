"""Subscription mirror (Sprint 3, first wave: `services/billing/`).

Canonical Postgres DDL for the `subscription` table `services/db/models.py` adds this sprint
(docs/21 §3.14; `api/openapi.yaml` `Subscription` schema). Not run against a live database in this
environment (Postgres is not installed here — services/README.md's caveat on `0001_initial_
schema.py` applies here too); checked for internal consistency (`alembic history` resolves the
revision graph) only, same as `0003_pro_tier_and_alerts.py`.

`plan_tier`'s CHECK vocabulary is `free | pro | team | api` — `api/openapi.yaml`'s `PlanTier`
schema and docs/21 §3.14, not the wider vendor-neutral `services.sor.ports.PLAN_TIERS`
(`pro | team | api | enterprise`) — see `services/db/models.py`'s `Subscription` docstring and
`services/billing/README.md` decision #3 for why `enterprise` purchases are stored as `plan_tier
= "api"` with the vendor's exact code kept in `plan_code`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

SUBSCRIPTION_SOR_KINDS = ("stripe", "odoo", "erpnext")
SUBSCRIPTION_PLAN_TIERS = ("free", "pro", "team", "api")
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "paused", "canceled")


def _in_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=name)


def upgrade() -> None:
    op.create_table(
        "subscription",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("sor_kind", sa.Text, nullable=False),
        sa.Column("sor_ref", sa.Text, nullable=False),
        sa.Column("plan_code", sa.Text, nullable=False),
        sa.Column("plan_tier", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("seats", sa.Integer, nullable=False, server_default="1"),
        sa.Column("current_period_start", TIMESTAMPTZ, nullable=False),
        sa.Column("current_period_end", TIMESTAMPTZ, nullable=False),
        sa.Column("cancel_at", TIMESTAMPTZ),
        sa.Column("mrr_amount", sa.Numeric(18, 2)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("mirrored_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("drift_flag", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("sor_kind", SUBSCRIPTION_SOR_KINDS, "ck_subscription_sor_kind_vocab"),
        _in_check("plan_tier", SUBSCRIPTION_PLAN_TIERS, "ck_subscription_plan_tier_vocab"),
        _in_check("status", SUBSCRIPTION_STATUSES, "ck_subscription_status_vocab"),
        sa.UniqueConstraint("sor_kind", "sor_ref", name="uq_subscription_sor_kind_sor_ref"),
    )
    op.create_index("ix_subscription_account_id", "subscription", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_subscription_account_id", table_name="subscription")
    op.drop_table("subscription")
