"""Pro tier and alerts: auth, entitlement mirror, saved searches/alerts, webhooks.

Canonical Postgres DDL for the tables `services/db/models.py` adds this sprint (Sprint 2 backend
brief "Pro tier and alerts"): `account`, `user`, `session`, `api_key`, `saved_search`, `alert`,
`webhook_endpoint`, `webhook_delivery` — docs/21 §3.12-§3.17 plus the `session` supporting table
of §4.4. Not run against a live database in this environment (Postgres is not installed here —
services/README.md's caveat on `0001_initial_schema.py` applies here too); checked for internal
consistency (`alembic history` resolves the revision graph) only.

Two deliberate deviations from a literal reading of docs/21, both recorded in
services/README.md "Pro tier and alerts" rather than silently assumed:
  - `user.auth_provider` gains `password` alongside docs/20 §7's `magic_link | google`, because
    this task's brief asks for argon2 password-hash session auth.
  - `alert.event_seqs` (docs/21 §3.16 `bigint[]`) is stored as `jsonb` (`JSONVariant`) rather than
    a native array, matching this sprint's one other integer-array need with no new column type.

`user.role` includes `legal` per docs/21 §3.12's 2026-09-12 note.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

USER_ROLES = ("viewer", "member", "operator", "legal", "owner")
USER_STATUSES = ("active", "disabled", "anonymised")
AUTH_PROVIDERS = ("magic_link", "google", "password")
ACCOUNT_KINDS = ("personal", "organization")
ACCOUNT_ENTITLEMENTS = ("public", "pro", "api", "admin")
ACCOUNT_ENTITLEMENT_SOURCES = ("sor", "manual_grant", "trial")
ACCOUNT_STATUSES = ("active", "suspended", "closed")
API_KEY_SCOPES_PREFIXES = ("bk_live", "bk_test")
API_KEY_TIERS = ("public", "pro", "api")
SAVED_SEARCH_ENTITIES = ("proposal", "opportunity", "event", "match")
SAVED_SEARCH_DELIVERY_MODES = ("immediate", "daily", "weekly", "none")
SAVED_SEARCH_STATUSES = ("active", "paused")
ALERT_CHANNELS = ("email", "webhook", "rss")
ALERT_MODES = ("immediate", "daily", "weekly")
ALERT_STATUSES = ("queued", "sent", "bounced", "failed", "suppressed")
WEBHOOK_TYPES = (
    "event.published",
    "match.added",
    "match.removed",
    "record.unpublished",
    "webhook.test",
)
WEBHOOK_ENDPOINT_STATUSES = ("active", "paused", "disabled")
WEBHOOK_DELIVERY_STATUSES = ("pending", "delivered", "retrying", "failed")


def _in_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=name)


def upgrade() -> None:
    # ------------------------------------------------------------------------------- account
    op.create_table(
        "account",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default="personal"),
        sa.Column("organization_id", pg.UUID(as_uuid=True), sa.ForeignKey("organization.id")),
        sa.Column("entitlement", sa.Text, nullable=False, server_default="public"),
        sa.Column("entitlement_source", sa.Text, nullable=False, server_default="manual_grant"),
        sa.Column("entitlement_checked_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("entitlement_stale", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("seats", sa.Integer, nullable=False, server_default="1"),
        sa.Column("seats_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sor_kind", sa.Text),
        sa.Column("sor_ref", sa.Text),
        sa.Column("billing_ref", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("kind", ACCOUNT_KINDS, "ck_account_kind_vocab"),
        _in_check("entitlement", ACCOUNT_ENTITLEMENTS, "ck_account_entitlement_vocab"),
        _in_check("entitlement_source", ACCOUNT_ENTITLEMENT_SOURCES, "ck_account_entitlement_source_vocab"),
        _in_check("status", ACCOUNT_STATUSES, "ck_account_status_vocab"),
    )

    # ---------------------------------------------------------------------------------- user
    op.create_table(
        "user",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("email", sa.Text),
        sa.Column("password_hash", sa.Text),
        sa.Column("email_verified_at", TIMESTAMPTZ),
        sa.Column("name", sa.Text),
        sa.Column("role", sa.Text, nullable=False, server_default="member"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("auth_provider", sa.Text, nullable=False, server_default="password"),
        sa.Column("mfa_enforced", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("marketing_consent", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("consent_version", sa.Text),
        sa.Column("tos_version", sa.Text),
        sa.Column("last_login_at", TIMESTAMPTZ),
        sa.Column("anonymised_at", TIMESTAMPTZ),
        sa.Column("sor_kind", sa.Text),
        sa.Column("sor_ref", sa.Text),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("role", USER_ROLES, "ck_user_role_vocab"),
        _in_check("status", USER_STATUSES, "ck_user_status_vocab"),
        _in_check("auth_provider", AUTH_PROVIDERS, "ck_user_auth_provider_vocab"),
    )
    op.create_index(
        "uq_user_email_active",
        "user",
        ["email"],
        unique=True,
        postgresql_where=sa.text("status <> 'anonymised'"),
    )

    # ------------------------------------------------------------------------------- session
    op.create_table(
        "session",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("revoked_at", TIMESTAMPTZ),
        sa.Column("ip_prefix", sa.Text),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])

    # ------------------------------------------------------------------------------- api_key
    op.create_table(
        "api_key",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("prefix", sa.Text, nullable=False, server_default="bk_live"),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("tier", sa.Text, nullable=False, server_default="public"),
        sa.Column("rate_limit_per_hour", sa.Integer, nullable=False, server_default="6000"),
        sa.Column("daily_quota", sa.Integer),
        sa.Column("expires_at", TIMESTAMPTZ),
        sa.Column("last_used_at", TIMESTAMPTZ),
        sa.Column("last_used_ip", sa.Text),
        sa.Column("revoked_at", TIMESTAMPTZ),
        sa.Column("licence_accepted_version", sa.Text, nullable=False),
        sa.Column("licence_accepted_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("prefix", API_KEY_SCOPES_PREFIXES, "ck_api_key_prefix_vocab"),
        _in_check("tier", API_KEY_TIERS, "ck_api_key_tier_vocab"),
    )
    op.create_index("ix_api_key_account_id", "api_key", ["account_id"])

    # -------------------------------------------------------------------------- saved_search
    op.create_table(
        "saved_search",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("entity", sa.Text, nullable=False, server_default="proposal"),
        sa.Column("query", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("delivery_mode", sa.Text, nullable=False, server_default="daily"),
        sa.Column("channels", pg.ARRAY(sa.Text), nullable=False, server_default="{email}"),
        sa.Column("rss_token", sa.Text, unique=True),
        sa.Column("last_run_at", TIMESTAMPTZ),
        sa.Column("watermark_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_match_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("entity", SAVED_SEARCH_ENTITIES, "ck_saved_search_entity_vocab"),
        _in_check("delivery_mode", SAVED_SEARCH_DELIVERY_MODES, "ck_saved_search_delivery_mode_vocab"),
        _in_check("status", SAVED_SEARCH_STATUSES, "ck_saved_search_status_vocab"),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_search_user_id_name"),
    )
    op.create_index("ix_saved_search_status_delivery", "saved_search", ["status", "delivery_mode"])

    # ------------------------------------------------------------------------------- alert
    op.create_table(
        "alert",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("saved_search_id", pg.UUID(as_uuid=True), sa.ForeignKey("saved_search.id"), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("channel", sa.Text, nullable=False, server_default="email"),
        sa.Column("mode", sa.Text, nullable=False, server_default="daily"),
        sa.Column("window_start", TIMESTAMPTZ, nullable=False),
        sa.Column("window_end", TIMESTAMPTZ, nullable=False),
        sa.Column("event_seqs", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("recipient", sa.Text),
        sa.Column("subject", sa.Text),
        sa.Column("provider_message_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("sent_at", TIMESTAMPTZ),
        sa.Column("unsubscribe_token", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("channel", ALERT_CHANNELS, "ck_alert_channel_vocab"),
        _in_check("mode", ALERT_MODES, "ck_alert_mode_vocab"),
        _in_check("status", ALERT_STATUSES, "ck_alert_status_vocab"),
    )
    op.create_index("ix_alert_saved_search_created", "alert", ["saved_search_id", "created_at"])

    # ---------------------------------------------------------------------- webhook_endpoint
    op.create_table(
        "webhook_endpoint",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("types", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("entity", sa.Text, nullable=False, server_default="event"),
        sa.Column("query", pg.JSONB, nullable=False, server_default="{}"),
        # Stored in the clear — see `WebhookEndpoint.secret`'s docstring in services/db/models.py:
        # signing an outbound delivery needs the actual secret, not a one-way hash of it.
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_delivery_at", TIMESTAMPTZ),
        sa.Column("last_success_at", TIMESTAMPTZ),
        sa.Column("secret_rotated_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("entity", SAVED_SEARCH_ENTITIES, "ck_webhook_endpoint_entity_vocab"),
        _in_check("status", WEBHOOK_ENDPOINT_STATUSES, "ck_webhook_endpoint_status_vocab"),
    )
    op.create_index("ix_webhook_endpoint_account_id", "webhook_endpoint", ["account_id"])

    # ---------------------------------------------------------------------- webhook_delivery
    op.create_table(
        "webhook_delivery",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column(
            "webhook_endpoint_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoint.id"),
            nullable=False,
        ),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("event_id", pg.UUID(as_uuid=True), sa.ForeignKey("event.id")),
        sa.Column("event_seq", sa.BigInteger),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body_excerpt", sa.Text),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("error_class", sa.Text),
        sa.Column("next_attempt_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("type", WEBHOOK_TYPES, "ck_webhook_delivery_type_vocab"),
        _in_check("status", WEBHOOK_DELIVERY_STATUSES, "ck_webhook_delivery_status_vocab"),
    )
    op.create_index(
        "ix_webhook_delivery_endpoint_created", "webhook_delivery", ["webhook_endpoint_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("webhook_delivery")
    op.drop_table("webhook_endpoint")
    op.drop_table("alert")
    op.drop_table("saved_search")
    op.drop_table("api_key")
    op.drop_table("session")
    op.drop_index("uq_user_email_active", table_name="user")
    op.drop_table("user")
    op.drop_table("account")
