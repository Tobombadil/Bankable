"""Admin panel tables: document, model_call, extraction, post, channel_config, task, and the
resolution_decision table that services/resolve/models.py has used since Sprint 2 without a
migration (found while preparing Sprint 3 item 3; recorded in docs/00-PLAN.md).

Canonical Postgres DDL for `services/db/models.py` "admin panel tables" and
`services/resolve/models.py`. Not run against a live database in this environment (Postgres is not
installed here; services/README.md's caveat on 0001 applies); checked for internal consistency only.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

DOCUMENT_SUBJECT_TYPES = ("proposal", "opportunity", "organization", "none")
DOCUMENT_TYPES = (
    "filing",
    "order",
    "notice",
    "rfp_document",
    "news_article",
    "press_release",
    "report",
    "other",
)
DOCUMENT_STORAGE_POLICIES = ("stored", "link_only", "headline_only")
EXTRACTION_PURPOSES = ("extract", "adjudicate")
EXTRACTION_STATUSES = ("proposed", "accepted", "rejected", "superseded")
MODEL_ALIASES = ("fast", "careful", "batch")
POST_CHANNELS = ("bluesky", "linkedin", "x")
POST_STATES = ("draft", "approved", "scheduled", "published", "rejected", "withdrawn", "failed")
TASK_TYPES = ("report", "intake_proposal", "intake_opportunity", "deletion_request", "resolution_dispute")
TASK_STATUSES = ("open", "in_progress", "done", "rejected")
DECISION_STATUSES = ("proposed", "confirmed", "rejected")


def _uuid_pk() -> sa.Column[object]:
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "document",
        _uuid_pk(),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("subject_type", sa.Text, nullable=False, server_default="none"),
        sa.Column("subject_id", pg.UUID(as_uuid=True)),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("snapshot.id")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False, server_default="other"),
        sa.Column("published_date", sa.Date),
        sa.Column("identifiers", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("storage_policy", sa.Text, nullable=False, server_default="link_only"),
        sa.Column("object_key", sa.Text),
        sa.Column("content_type", sa.Text),
        sa.Column("byte_size", sa.BigInteger),
        sa.Column("sha256", sa.String(64)),
        sa.Column("page_count", sa.Integer),
        sa.Column("text_extracted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("personal_data_flag", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("robots_opt_out", sa.Boolean, nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.CheckConstraint(f"subject_type IN {DOCUMENT_SUBJECT_TYPES!r}", name="subject_type_vocab"),
        sa.CheckConstraint(f"doc_type IN {DOCUMENT_TYPES!r}", name="doc_type_vocab"),
        sa.CheckConstraint(f"storage_policy IN {DOCUMENT_STORAGE_POLICIES!r}", name="storage_policy_vocab"),
    )
    op.create_index("ix_document_subject", "document", ["subject_type", "subject_id"])

    op.create_table(
        "model_call",
        _uuid_pk(),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("subject_type", sa.Text),
        sa.Column("subject_id", pg.UUID(as_uuid=True)),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id")),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("prompt_template_id", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("cache_hit", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text),
        sa.CheckConstraint(f"alias IN {MODEL_ALIASES!r}", name="alias_vocab"),
    )
    op.create_index("ix_model_call_source_created", "model_call", ["source_id", "created_at"])

    op.create_table(
        "extraction",
        _uuid_pk(),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("document_id", pg.UUID(as_uuid=True), sa.ForeignKey("document.id")),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text, nullable=False, server_default="extract"),
        sa.Column("field_path", sa.Text),
        sa.Column("payload", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("citations", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("model_alias", sa.Text),
        sa.Column("prompt_template_id", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("model_call_id", pg.UUID(as_uuid=True), sa.ForeignKey("model_call.id")),
        sa.Column("status", sa.Text, nullable=False, server_default="proposed"),
        sa.Column("accepted_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id")),
        sa.Column("applied_event_id", pg.UUID(as_uuid=True), sa.ForeignKey("event.id")),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(f"purpose IN {EXTRACTION_PURPOSES!r}", name="purpose_vocab"),
        sa.CheckConstraint(f"status IN {EXTRACTION_STATUSES!r}", name="status_vocab"),
    )
    op.create_index("ix_extraction_status_created", "extraction", ["status", "created_at"])
    op.create_index("ix_extraction_subject", "extraction", ["subject_type", "subject_id"])

    op.create_table(
        "post",
        _uuid_pk(),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("event_id", pg.UUID(as_uuid=True), sa.ForeignKey("event.id"), nullable=False),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", sa.Text, nullable=False),
        sa.Column("template_version", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("link_url", sa.Text, nullable=False),
        sa.Column("credit_line", sa.Text, nullable=False),
        sa.Column("disclosure_label", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="draft"),
        sa.Column("gate_checked_at", TIMESTAMPTZ, nullable=False),
        sa.Column("approved_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id")),
        sa.Column("auto_published", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("scheduled_for", TIMESTAMPTZ),
        sa.Column("published_at", TIMESTAMPTZ),
        sa.Column("external_post_id", sa.Text),
        sa.Column("reject_reason", sa.Text),
        sa.Column("metrics", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint(f"channel IN {POST_CHANNELS!r}", name="channel_vocab"),
        sa.CheckConstraint(f"state IN {POST_STATES!r}", name="state_vocab"),
    )
    op.create_index("ix_post_queue", "post", ["state", "channel", "created_at"])

    op.create_table(
        "channel_config",
        sa.Column("channel", sa.Text, primary_key=True),
        sa.Column("auto_publish", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("disclosure_label", sa.Text),
        sa.Column("daily_cap", sa.Integer),
        sa.Column("updated_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id")),
        *_timestamps(),
        sa.CheckConstraint(f"channel IN {POST_CHANNELS!r}", name="channel_vocab"),
    )

    op.create_table(
        "task",
        _uuid_pk(),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("subject_type", sa.Text),
        sa.Column("subject_id", pg.UUID(as_uuid=True)),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("assignee_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id")),
        sa.Column("notes", sa.Text),
        sa.Column("issue_type", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("contact", pg.JSONB),
        sa.Column("pending_record", pg.JSONB),
        sa.Column("resolver_suggestions", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("due_at", TIMESTAMPTZ),
        sa.Column("public_opt_in", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("audit_event_ids", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("completed_at", TIMESTAMPTZ),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("user.id")),
        sa.Column("idempotency_key", sa.Text, unique=True),
        *_timestamps(),
        sa.CheckConstraint(f"type IN {TASK_TYPES!r}", name="type_vocab"),
        sa.CheckConstraint(f"status IN {TASK_STATUSES!r}", name="status_vocab"),
    )
    op.create_index("ix_task_queue", "task", ["status", "type", "created_at"])
    op.create_index("ix_task_subject", "task", ["subject_type", "subject_id"])

    op.create_table(
        "resolution_decision",
        _uuid_pk(),
        sa.Column("left_proposal_id", pg.UUID(as_uuid=True), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("right_proposal_id", pg.UUID(as_uuid=True), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("cluster_key", sa.Text, nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("gate_reason", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="proposed"),
        sa.Column("created_by", sa.Text, nullable=False, server_default="pipeline"),
        sa.Column("decided_by_user_id", pg.UUID(as_uuid=True)),
        sa.Column("decided_at", TIMESTAMPTZ),
        sa.Column("extra", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.CheckConstraint(f"status IN {DECISION_STATUSES!r}", name="status_vocab"),
        sa.UniqueConstraint("left_proposal_id", "right_proposal_id", name="one_decision_per_pair"),
    )


def downgrade() -> None:
    op.drop_table("resolution_decision")
    op.drop_index("ix_task_subject", table_name="task")
    op.drop_index("ix_task_queue", table_name="task")
    op.drop_table("task")
    op.drop_table("channel_config")
    op.drop_index("ix_post_queue", table_name="post")
    op.drop_table("post")
    op.drop_index("ix_extraction_subject", table_name="extraction")
    op.drop_index("ix_extraction_status_created", table_name="extraction")
    op.drop_table("extraction")
    op.drop_index("ix_model_call_source_created", table_name="model_call")
    op.drop_table("model_call")
    op.drop_index("ix_document_subject", table_name="document")
    op.drop_table("document")
