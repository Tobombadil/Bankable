"""Initial schema: the public-tier entities of docs/21-data-model.md.

Canonical DDL for Postgres 16 + PostGIS (ADR 0003, docs/20-architecture.md §4.4). This migration
is the executable twin of docs/21-data-model.md and must not diverge from it silently (docs/04
DA-1). It has not been run against a live database in this environment — Postgres is not
installed here (`which postgres pg_ctl initdb` all fail) — so services/db/test_models.py exercises
the equivalent ORM schema on SQLite instead; see services/README.md for the full caveat.

Scope matches services/db/models.py: `licence`, `source`, `source_run`, `snapshot`,
`organization`, `organization_alias`, `location`, `proposal`, `proposal_source`, `opportunity`,
`opportunity_source`, `event`, `match` — the public tier's thirteen entities. `document` and every
Pro/API/admin-only table are a later migration.

Known gaps versus the full docs/21 model, both because their upstream producers do not exist yet
and recorded as open decisions in services/README.md rather than silently dropped:
  - `search_tsv` generated columns and their GIN indexes (docs/21 §5.2) are deferred to the
    migration that ships full-text search.
  - `event` is not yet range-partitioned monthly on `observed_at` (docs/21 §5.3) — a follow-up
    migration before volume makes that rewrite expensive, per docs/20 §13 step 5.

Revision ID: 0001
Revises:
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)


def _geography_point() -> sa.types.TypeEngine[object]:
    from geoalchemy2 import Geography

    return Geography(geometry_type="POINT", srid=4326)


REUSE_CLASSES = ("open", "attribution", "restricted", "unknown")
SOURCE_PUBLISH_STATES = ("ingest_only", "api_only", "public")
RECORD_PUBLISH_STATES = ("pending_review", "ingest_only", "api_only", "public", "unpublished")
LIFECYCLE_STATES = (
    "unknown",
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
)
OPPORTUNITY_STATUSES = (
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
)
ACTOR_TYPES = ("pipeline", "model", "user", "system")
CREATED_BY_VALUES = ("pipeline", "user", "import")
LINK_METHODS = ("deterministic_key", "rule", "model", "user")
MATCH_STATUSES = ("active", "removed", "superseded")
SOURCE_RUN_STATUSES = ("running", "ok", "unchanged", "partial", "failed", "blocked", "budget")
LOCATION_KINDS = ("point", "county", "state", "region", "service_territory")
LOCATION_PRECISIONS = ("exact", "county_centroid", "state_centroid", "unknown")


def _in_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=name)


def upgrade() -> None:
    # Extensions (docs/20 §4.4): PostGIS for geography, pg_trgm for fuzzy name search,
    # btree_gin for combined indexes, pgcrypto for gen_random_uuid() where useful.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------------------- licence
    op.create_table(
        "licence",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("url", sa.Text),
        sa.Column("reuse_class", sa.Text, nullable=False),
        sa.Column("attribution_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attribution_text", sa.Text),
        sa.Column("requires_link_back", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allows_derived_publication", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allows_raw_publication", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allows_api_redistribution", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allows_bulk_export", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allows_commercial_use", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("share_alike", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gate_flag", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("gate_name", sa.Text),
        sa.Column("gate_cleared_at", TIMESTAMPTZ),
        sa.Column("gate_cleared_by", pg.UUID(as_uuid=True)),
        sa.Column("evidence_url", sa.Text),
        sa.Column("evidence_retrieved_at", TIMESTAMPTZ),
        sa.Column("evidence_object_key", sa.Text),
        sa.Column("classified_by", sa.Text),
        sa.Column("contract_ref", sa.Text),
        sa.Column("expires_at", TIMESTAMPTZ),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("reuse_class", REUSE_CLASSES, "ck_licence_reuse_class_vocab"),
    )

    # -------------------------------------------------------------------------------- source
    op.create_table(
        "source",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("jurisdiction", sa.Text),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("operator", sa.Text),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("access", sa.Text, nullable=False),
        sa.Column("format", sa.Text),
        sa.Column("cadence", sa.Text, nullable=False),
        sa.Column("tier", sa.Integer, nullable=False, server_default="3"),
        sa.Column("effort", sa.Text),
        sa.Column("egress", sa.Text, nullable=False, server_default="plain"),
        sa.Column("connector", sa.Text),
        sa.Column("implemented", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("publish_state", sa.Text, nullable=False, server_default="ingest_only"),
        sa.Column("lag_days", sa.Integer),
        sa.Column("lag_overrides", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("schedule_cron", sa.Text),
        sa.Column("next_run_at", TIMESTAMPTZ),
        sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("health", sa.Text, nullable=False, server_default="ok"),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_success_at", TIMESTAMPTZ),
        sa.Column("last_error", sa.Text),
        sa.Column("last_error_at", TIMESTAMPTZ),
        sa.Column("host", sa.Text),
        sa.Column("max_rps", sa.Numeric(6, 3), nullable=False, server_default="1.0"),
        sa.Column("max_concurrency", sa.Integer, nullable=False, server_default="1"),
        sa.Column("enrichment_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("model_budget_usd_daily", sa.Numeric(10, 2)),
        sa.Column("cost_per_changed_record_30d", sa.Numeric(10, 4)),
        sa.Column("attribution_text", sa.Text),
        sa.Column("manifest_version", sa.Text),
        sa.Column("manifest_hash", sa.Text),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("publish_state", SOURCE_PUBLISH_STATES, "ck_source_publish_state_vocab"),
    )

    # ---------------------------------------------------------------------------- source_run
    op.create_table(
        "source_run",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("trigger", sa.Text, nullable=False, server_default="manual"),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", TIMESTAMPTZ),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("http_status", sa.Integer),
        sa.Column("bytes", sa.BigInteger),
        sa.Column("egress_class", sa.Text, nullable=False, server_default="plain"),
        sa.Column("rows_seen", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_new", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_changed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_gone", sa.Integer, nullable=False, server_default="0"),
        sa.Column("events_emitted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("worker_seconds", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("dq_status", sa.Text),
        sa.Column("dq", pg.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("error_class", sa.Text),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("dead_lettered", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("status", SOURCE_RUN_STATUSES, "ck_source_run_status_vocab"),
    )
    op.create_index("ix_source_run_source_started", "source_run", ["source_id", sa.text("started_at DESC")])

    # ----------------------------------------------------------------------------- snapshot
    op.create_table(
        "snapshot",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_run_id", pg.UUID(as_uuid=True), sa.ForeignKey("source_run.id"), nullable=False),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("fetched_url", sa.Text, nullable=False),
        sa.Column("http_status", sa.Integer, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("parser_version", sa.Text),
        sa.Column("record_count", sa.Integer),
        sa.Column("previous_snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("snapshot.id")),
        sa.Column("retention_class", sa.Text, nullable=False, server_default="full"),
        sa.Column("expires_at", TIMESTAMPTZ),
        sa.UniqueConstraint("source_id", "sha256", name="uq_snapshot_source_sha256"),
    )

    # -------------------------------------------------------------------------- organization
    op.create_table(
        "organization",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name_canonical", sa.Text, nullable=False),
        sa.Column("name_normalised", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("jurisdiction", sa.Text),
        sa.Column("ids", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("website", sa.Text),
        sa.Column("is_curated_issuer", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("first_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_changed", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("merged_into_id", pg.UUID(as_uuid=True), sa.ForeignKey("organization.id")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_organization_name_trgm",
        "organization",
        ["name_normalised"],
        postgresql_using="gin",
        postgresql_ops={"name_normalised": "gin_trgm_ops"},
    )

    # ---------------------------------------------------------------------- organization_alias
    op.create_table(
        "organization_alias",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", pg.UUID(as_uuid=True), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("alias_normalised", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1.0"),
        sa.Column("created_by", sa.Text, nullable=False, server_default="pipeline"),
        sa.UniqueConstraint("organization_id", "alias_normalised", name="uq_organization_alias_org_alias"),
    )
    op.create_index(
        "ix_organization_alias_trgm",
        "organization_alias",
        ["alias_normalised"],
        postgresql_using="gin",
        postgresql_ops={"alias_normalised": "gin_trgm_ops"},
    )

    # ------------------------------------------------------------------------------- location
    op.create_table(
        "location",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("geom", _geography_point()),
        sa.Column("precision", sa.Text, nullable=False),
        sa.Column("precision_reason", sa.Text),
        sa.Column("county_fips", sa.String(5)),
        sa.Column("county_name", sa.Text),
        sa.Column("state_code", sa.Text),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("raw_place", sa.Text),
        sa.Column("geocoder", sa.Text),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        _in_check("kind", LOCATION_KINDS, "ck_location_kind_vocab"),
        _in_check("precision", LOCATION_PRECISIONS, "ck_location_precision_vocab"),
    )
    op.create_index("ix_location_geom", "location", ["geom"], postgresql_using="gist")

    # ------------------------------------------------------------------------------- proposal
    op.create_table(
        "proposal",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name_canonical", sa.Text, nullable=False),
        sa.Column("sponsor_org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organization.id")),
        sa.Column("technology", sa.Text),
        sa.Column("technology_raw", sa.Text),
        sa.Column("capacity_mw", sa.Numeric(12, 3)),
        sa.Column("storage_mwh", sa.Numeric(12, 3)),
        sa.Column("jurisdiction", sa.Text, nullable=False),
        sa.Column("iso", sa.Text),
        sa.Column("location_id", pg.UUID(as_uuid=True), sa.ForeignKey("location.id")),
        sa.Column("lifecycle_state", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("status_raw", sa.Text),
        sa.Column("identifiers", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("proposed_online_date", sa.Date),
        sa.Column("first_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_changed", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("publish_state", sa.Text, nullable=False, server_default="pending_review"),
        sa.Column("published_at", TIMESTAMPTZ),
        sa.Column("public_at", TIMESTAMPTZ),
        sa.Column("min_reuse_class", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("field_provenance", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("overrides", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("source_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text, nullable=False, server_default="pipeline"),
        sa.Column("resolution_confidence", sa.Numeric(4, 3)),
        sa.Column("merged_into_id", pg.UUID(as_uuid=True), sa.ForeignKey("proposal.id")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("lifecycle_state", LIFECYCLE_STATES, "ck_proposal_lifecycle_state_vocab"),
        _in_check("publish_state", RECORD_PUBLISH_STATES, "ck_proposal_publish_state_vocab"),
        _in_check("min_reuse_class", REUSE_CLASSES, "ck_proposal_min_reuse_class_vocab"),
        _in_check("created_by", CREATED_BY_VALUES, "ck_proposal_created_by_vocab"),
    )
    op.create_index("ix_proposal_publish_public_at", "proposal", ["publish_state", sa.text("public_at DESC")])
    op.create_index(
        "ix_proposal_identifiers",
        "proposal",
        ["identifiers"],
        postgresql_using="gin",
        postgresql_ops={"identifiers": "jsonb_path_ops"},
    )

    # ------------------------------------------------------------------------ proposal_source
    op.create_table(
        "proposal_source",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("proposal_id", pg.UUID(as_uuid=True), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_record_id", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("snapshot.id")),
        sa.Column("raw", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("normalised", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("status_raw", sa.Text),
        sa.Column("first_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("gone_at", TIMESTAMPTZ),
        sa.Column("link_method", sa.Text, nullable=False, server_default="deterministic_key"),
        sa.Column("link_confidence", sa.Numeric(4, 3), nullable=False, server_default="1.0"),
        sa.Column("link_event_id", pg.UUID(as_uuid=True)),  # FK to event.id added after event exists
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        _in_check("link_method", LINK_METHODS, "ck_proposal_source_link_method_vocab"),
    )
    op.create_index(
        "uq_proposal_source_active",
        "proposal_source",
        ["source_id", "source_record_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    # ----------------------------------------------------------------------------- opportunity
    op.create_table(
        "opportunity",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("issuer_org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organization.id")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("jurisdiction", sa.Text, nullable=False),
        sa.Column("technologies", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("capacity_sought_mw", sa.Numeric(12, 3)),
        sa.Column("budget_amount", sa.Numeric(18, 2)),
        sa.Column("budget_currency", sa.String(3)),
        sa.Column("open_at", sa.Date),
        sa.Column("due_at", TIMESTAMPTZ),
        sa.Column("status", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("status_raw", sa.Text),
        sa.Column("location_id", pg.UUID(as_uuid=True), sa.ForeignKey("location.id")),
        sa.Column("identifiers", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("first_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_changed", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("publish_state", sa.Text, nullable=False, server_default="pending_review"),
        sa.Column("published_at", TIMESTAMPTZ),
        sa.Column("public_at", TIMESTAMPTZ),
        sa.Column("min_reuse_class", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("field_provenance", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("overrides", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("source_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text, nullable=False, server_default="pipeline"),
        sa.Column("merged_into_id", pg.UUID(as_uuid=True), sa.ForeignKey("opportunity.id")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        _in_check("status", OPPORTUNITY_STATUSES, "ck_opportunity_status_vocab"),
        _in_check("publish_state", RECORD_PUBLISH_STATES, "ck_opportunity_publish_state_vocab"),
        _in_check("min_reuse_class", REUSE_CLASSES, "ck_opportunity_min_reuse_class_vocab"),
    )
    op.create_index(
        "ix_opportunity_publish_public_at", "opportunity", ["publish_state", sa.text("public_at DESC")]
    )
    op.create_index(
        "ix_opportunity_identifiers",
        "opportunity",
        ["identifiers"],
        postgresql_using="gin",
        postgresql_ops={"identifiers": "jsonb_path_ops"},
    )

    # -------------------------------------------------------------------- opportunity_source
    op.create_table(
        "opportunity_source",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("opportunity_id", pg.UUID(as_uuid=True), sa.ForeignKey("opportunity.id"), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_record_id", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", TIMESTAMPTZ, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("snapshot.id")),
        sa.Column("raw", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("normalised", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("status_raw", sa.Text),
        sa.Column("first_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("gone_at", TIMESTAMPTZ),
        sa.Column("link_method", sa.Text, nullable=False, server_default="deterministic_key"),
        sa.Column("link_confidence", sa.Numeric(4, 3), nullable=False, server_default="1.0"),
        sa.Column("link_event_id", pg.UUID(as_uuid=True)),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        _in_check("link_method", LINK_METHODS, "ck_opportunity_source_link_method_vocab"),
    )
    op.create_index(
        "uq_opportunity_source_active",
        "opportunity_source",
        ["source_id", "source_record_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    # ----------------------------------------------------------------------------------- event
    op.create_table(
        "event",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=False), nullable=False, unique=True),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("observed_at", TIMESTAMPTZ, nullable=False),
        sa.Column("recorded_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", TIMESTAMPTZ),
        sa.Column("public_at", TIMESTAMPTZ),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id")),
        sa.Column("source_url", sa.Text),
        sa.Column("retrieved_at", TIMESTAMPTZ),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id")),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("snapshot.id")),
        sa.Column("before", pg.JSONB),
        sa.Column("after", pg.JSONB),
        sa.Column("changed_keys", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("actor_type", sa.Text, nullable=False, server_default="pipeline"),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True)),
        sa.Column("reason", sa.Text),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("reverses_event_id", pg.UUID(as_uuid=True), sa.ForeignKey("event.id")),
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("source_run.id")),
        sa.Column("job_id", sa.Text),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        _in_check("actor_type", ACTOR_TYPES, "ck_event_actor_type_vocab"),
        # docs/21 §5.3: monthly range partitioning on observed_at is deferred (see module
        # docstring) — this migration ships an ordinary table.
    )
    op.create_index(
        "ix_event_subject_observed", "event", ["subject_type", "subject_id", sa.text("observed_at DESC")]
    )
    op.create_index("ix_event_public_at", "event", ["public_at"])
    op.create_index("ix_event_seq", "event", ["seq"])

    # proposal_source.link_event_id / opportunity_source.link_event_id -> event.id, added now
    # that `event` exists (docs/21 §3.2: "the source_linked event that created this row").
    op.create_foreign_key(
        "fk_proposal_source_link_event_id_event", "proposal_source", "event", ["link_event_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_opportunity_source_link_event_id_event", "opportunity_source", "event", ["link_event_id"], ["id"]
    )

    # ----------------------------------------------------------------------------------- match
    op.create_table(
        "match",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("proposal_id", pg.UUID(as_uuid=True), sa.ForeignKey("proposal.id"), nullable=False),
        sa.Column("opportunity_id", pg.UUID(as_uuid=True), sa.ForeignKey("opportunity.id"), nullable=False),
        sa.Column("score", sa.Numeric(4, 3), nullable=False),
        sa.Column("rationale", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("rationale_text", sa.Text, nullable=False),
        sa.Column("rule_set_version", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False, server_default="rule"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("first_matched_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_evaluated_at", TIMESTAMPTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("removed_at", TIMESTAMPTZ),
        sa.Column("crm_lead_ref", sa.Text),
        _in_check("status", MATCH_STATUSES, "ck_match_status_vocab"),
    )
    op.create_index(
        "uq_match_active_pair",
        "match",
        ["proposal_id", "opportunity_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_table("match")
    op.drop_constraint("fk_opportunity_source_link_event_id_event", "opportunity_source", type_="foreignkey")
    op.drop_constraint("fk_proposal_source_link_event_id_event", "proposal_source", type_="foreignkey")
    op.drop_table("event")
    op.drop_table("opportunity_source")
    op.drop_table("opportunity")
    op.drop_table("proposal_source")
    op.drop_table("proposal")
    op.drop_table("location")
    op.drop_table("organization_alias")
    op.drop_table("organization")
    op.drop_table("snapshot")
    op.drop_table("source_run")
    op.drop_table("source")
    op.drop_table("licence")
