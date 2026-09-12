"""SQLAlchemy 2.0 models for the public-tier entities of docs/21-data-model.md.

Scope (Sprint 2 backend brief): the thirteen entities the public tier needs — `licence`, `source`,
`source_run`, `snapshot`, `organization`, `organization_alias`, `location`, `proposal`,
`proposal_source`, `opportunity`, `opportunity_source`, `event`, `match`. `document` and every
Pro/API/admin-only table (docs/21 §3.12-§3.18, §4.4) are out of scope this sprint.

Every table that originates outside the platform carries the provenance quartet
(`source_id, source_url, retrieved_at, licence_id` — CLAUDE.md, docs/04 DA-2) as NOT NULL columns.
Enums are `text` + `CHECK`, never native enum types (docs/21 §1), so that adding a vocabulary
value never needs a lock-taking migration.

Two simplifications versus the full docs/21 model, both because their upstream producers
(enrichment, resolution, billing) are not built yet, are recorded as open decisions in
services/README.md rather than silently dropped:
  - `search_tsv` generated columns (docs/21 §5.2) are omitted; Postgres FTS is a follow-up
    migration once the search story is prioritised.
  - `event` is not yet range-partitioned monthly (docs/21 §5.3); the canonical migration notes
    this as a known gap to close before volume makes the rewrite expensive.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from services.db.base import Base
from services.db.types import GeographyPoint, GUID, JSONVariant, TextArray, uuid7

# ---------------------------------------------------------------------------------------------
# Vocabularies enforced as CHECK constraints (docs/21 §1, §7). Kept here so the DB layer, the API
# layer (services/api/schemas.py) and services/CHANGELOG entries have one place to cross-check.
# ---------------------------------------------------------------------------------------------
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


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_uuid() -> _uuid.UUID:
    return uuid7()


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ================================================================================ licence (§3.19)
class Licence(Base, TimestampMixin):
    """The gate. One row per distinct licence/terms document (docs/21 §3.19, invariant L1/L2)."""

    __tablename__ = "licence"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    url: Mapped[str | None] = mapped_column(sa.Text)
    reuse_class: Mapped[str] = mapped_column(sa.Text, nullable=False)
    attribution_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    attribution_text: Mapped[str | None] = mapped_column(sa.Text)
    requires_link_back: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    allows_derived_publication: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    allows_raw_publication: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    allows_api_redistribution: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    allows_bulk_export: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    allows_commercial_use: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    share_alike: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    gate_flag: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    gate_name: Mapped[str | None] = mapped_column(sa.Text)
    gate_cleared_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    gate_cleared_by: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    evidence_url: Mapped[str | None] = mapped_column(sa.Text)
    evidence_retrieved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    evidence_object_key: Mapped[str | None] = mapped_column(sa.Text)
    classified_by: Mapped[str | None] = mapped_column(sa.Text)
    contract_ref: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (sa.CheckConstraint(f"reuse_class IN {REUSE_CLASSES!r}", name="reuse_class_vocab"),)

    @property
    def is_publishable_class(self) -> bool:
        """Invariant L1's reuse-class half: `open`/`attribution` may leave the building at all."""
        return self.reuse_class in ("open", "attribution")

    @property
    def gate_clear(self) -> bool:
        """Invariant L1 in full (docs/21 §3.19): publishable class, gate cleared, evidence on file."""
        return (
            self.is_publishable_class
            and not self.gate_flag
            and self.evidence_url is not None
            and self.evidence_retrieved_at is not None
            and self.classified_by is not None
        )


# ================================================================================= source (§4.1)
class Source(Base, TimestampMixin):
    """Runtime mirror of one `data/sources.yaml` entry (docs/21 §4.1)."""

    __tablename__ = "source"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)  # data/sources.yaml id
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(sa.Text)
    category: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operator: Mapped[str | None] = mapped_column(sa.Text)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    access: Mapped[str] = mapped_column(sa.Text, nullable=False)
    format: Mapped[str | None] = mapped_column(sa.Text)
    cadence: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tier: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3)
    effort: Mapped[str | None] = mapped_column(sa.Text)
    egress: Mapped[str] = mapped_column(sa.Text, nullable=False, default="plain")
    connector: Mapped[str | None] = mapped_column(sa.Text)
    implemented: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)

    publish_state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ingest_only")
    lag_days: Mapped[int | None] = mapped_column(sa.Integer)
    lag_overrides: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    schedule_cron: Mapped[str | None] = mapped_column(sa.Text)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    paused: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    health: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ok")
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    host: Mapped[str | None] = mapped_column(sa.Text)
    max_rps: Mapped[float] = mapped_column(sa.Numeric(6, 3), nullable=False, default=1.0)
    max_concurrency: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    enrichment_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    model_budget_usd_daily: Mapped[float | None] = mapped_column(sa.Numeric(10, 2))
    cost_per_changed_record_30d: Mapped[float | None] = mapped_column(sa.Numeric(10, 4))
    attribution_text: Mapped[str | None] = mapped_column(sa.Text)
    manifest_version: Mapped[str | None] = mapped_column(sa.Text)
    manifest_hash: Mapped[str | None] = mapped_column(sa.Text)

    licence: Mapped[Licence] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"publish_state IN {SOURCE_PUBLISH_STATES!r}", name="publish_state_vocab"),
    )

    @property
    def gated(self) -> bool:
        """Mirrors `pipeline.connectors.registry.SourceEntry.gated` (both gates must hold)."""
        return self.licence.reuse_class in ("restricted", "unknown")


# ============================================================================= source_run (§4.2)
class SourceRun(Base):
    __tablename__ = "source_run"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(sa.Text, nullable=False, default="manual")
    started_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="running")
    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    egress_class: Mapped[str] = mapped_column(sa.Text, nullable=False, default="plain")
    rows_seen: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    rows_new: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    rows_changed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    rows_gone: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    events_emitted: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Numeric(10, 4), nullable=False, default=0)
    worker_seconds: Mapped[float] = mapped_column(sa.Numeric(10, 2), nullable=False, default=0)
    dq_status: Mapped[str | None] = mapped_column(sa.Text)
    dq: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    error: Mapped[str | None] = mapped_column(sa.Text)
    error_class: Mapped[str | None] = mapped_column(sa.Text)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    dead_lettered: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        sa.CheckConstraint(f"status IN {SOURCE_RUN_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_source_run_source_started", "source_id", sa.text("started_at DESC")),
    )


# =============================================================================== snapshot (§4.3)
class Snapshot(Base):
    __tablename__ = "snapshot"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_run_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("source_run.id"), nullable=False)
    object_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    fetched_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    http_status: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(sa.Text)
    record_count: Mapped[int | None] = mapped_column(sa.Integer)
    previous_snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    retention_class: Mapped[str] = mapped_column(sa.Text, nullable=False, default="full")
    expires_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.UniqueConstraint("source_id", "sha256", name="one_object_per_content"),)


# ============================================================================ organization (§3.5)
class Organization(Base, TimestampMixin):
    __tablename__ = "organization"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name_canonical: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name_normalised: Mapped[str] = mapped_column(sa.Text, nullable=False)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    country: Mapped[str] = mapped_column(sa.String(2), nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(sa.Text)
    ids: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    website: Mapped[str | None] = mapped_column(sa.Text)
    is_curated_issuer: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    first_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_changed: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    merged_into_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("organization.id"))


# ====================================================================== organization_alias (§3.6)
class OrganizationAlias(Base):
    __tablename__ = "organization_alias"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    organization_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("organization.id"), nullable=False)
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alias_normalised: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")

    __table_args__ = (
        sa.UniqueConstraint("organization_id", "alias_normalised", name="one_alias_per_org"),
    )


# ================================================================================= location (§3.7)
class Location(Base):
    __tablename__ = "location"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geom: Mapped[Any | None] = mapped_column(GeographyPoint())
    precision: Mapped[str] = mapped_column(sa.Text, nullable=False)
    precision_reason: Mapped[str | None] = mapped_column(sa.Text)
    county_fips: Mapped[str | None] = mapped_column(sa.String(5))
    county_name: Mapped[str | None] = mapped_column(sa.Text)
    state_code: Mapped[str | None] = mapped_column(sa.Text)
    country: Mapped[str] = mapped_column(sa.String(2), nullable=False)
    raw_place: Mapped[str | None] = mapped_column(sa.Text)
    geocoder: Mapped[str | None] = mapped_column(sa.Text)

    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)

    __table_args__ = (
        sa.CheckConstraint(f"kind IN {LOCATION_KINDS!r}", name="kind_vocab"),
        sa.CheckConstraint(f"precision IN {LOCATION_PRECISIONS!r}", name="precision_vocab"),
    )


# ================================================================================= proposal (§3.1)
class Proposal(Base, TimestampMixin):
    __tablename__ = "proposal"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name_canonical: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sponsor_org_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("organization.id"))
    technology: Mapped[str | None] = mapped_column(sa.Text)
    technology_raw: Mapped[str | None] = mapped_column(sa.Text)
    capacity_mw: Mapped[float | None] = mapped_column(sa.Numeric(12, 3))
    storage_mwh: Mapped[float | None] = mapped_column(sa.Numeric(12, 3))
    jurisdiction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    iso: Mapped[str | None] = mapped_column(sa.Text)
    location_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("location.id"))
    lifecycle_state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unknown")
    status_raw: Mapped[str | None] = mapped_column(sa.Text)
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    proposed_online_date: Mapped[dt.date | None] = mapped_column(sa.Date)
    first_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_changed: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    publish_state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending_review")
    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    public_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    min_reuse_class: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unknown")
    field_provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    source_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")
    resolution_confidence: Mapped[float | None] = mapped_column(sa.Numeric(4, 3))
    merged_into_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("proposal.id"))

    sponsor: Mapped[Organization | None] = relationship(lazy="joined")
    location: Mapped[Location | None] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"lifecycle_state IN {LIFECYCLE_STATES!r}", name="lifecycle_state_vocab"),
        sa.CheckConstraint(f"publish_state IN {RECORD_PUBLISH_STATES!r}", name="publish_state_vocab"),
        sa.CheckConstraint(f"min_reuse_class IN {REUSE_CLASSES!r}", name="min_reuse_class_vocab"),
        sa.CheckConstraint(f"created_by IN {CREATED_BY_VALUES!r}", name="created_by_vocab"),
        sa.Index("ix_proposal_publish_public_at", "publish_state", sa.text("public_at DESC")),
    )


# ========================================================================== proposal_source (§3.2)
class ProposalSource(Base):
    __tablename__ = "proposal_source"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    proposal_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("proposal.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_record_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    normalised: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    status_raw: Mapped[str | None] = mapped_column(sa.Text)
    first_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    gone_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    link_method: Mapped[str] = mapped_column(sa.Text, nullable=False, default="deterministic_key")
    link_confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    link_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (
        sa.CheckConstraint(f"link_method IN {LINK_METHODS!r}", name="link_method_vocab"),
        sa.Index(
            "uq_proposal_source_active",
            "source_id",
            "source_record_id",
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active"),
        ),
    )


# ================================================================================ opportunity (§3.3)
class Opportunity(Base, TimestampMixin):
    __tablename__ = "opportunity"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    issuer_org_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("organization.id"))
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    jurisdiction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    technologies: Mapped[list[str]] = mapped_column(TextArray(), nullable=False, default=list)
    capacity_sought_mw: Mapped[float | None] = mapped_column(sa.Numeric(12, 3))
    budget_amount: Mapped[float | None] = mapped_column(sa.Numeric(18, 2))
    budget_currency: Mapped[str | None] = mapped_column(sa.String(3))
    open_at: Mapped[dt.date | None] = mapped_column(sa.Date)
    due_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unknown")
    status_raw: Mapped[str | None] = mapped_column(sa.Text)
    location_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("location.id"))
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    first_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_changed: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    publish_state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending_review")
    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    public_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    min_reuse_class: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unknown")
    field_provenance: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    source_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")
    merged_into_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("opportunity.id"))

    issuer: Mapped[Organization | None] = relationship(lazy="joined")
    location: Mapped[Location | None] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"status IN {OPPORTUNITY_STATUSES!r}", name="status_vocab"),
        sa.CheckConstraint(f"publish_state IN {RECORD_PUBLISH_STATES!r}", name="publish_state_vocab"),
        sa.CheckConstraint(f"min_reuse_class IN {REUSE_CLASSES!r}", name="min_reuse_class_vocab"),
        sa.Index("ix_opportunity_publish_public_at", "publish_state", sa.text("public_at DESC")),
    )


# ======================================================================== opportunity_source (§3.4)
class OpportunitySource(Base):
    __tablename__ = "opportunity_source"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    opportunity_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("opportunity.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_record_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    normalised: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    status_raw: Mapped[str | None] = mapped_column(sa.Text)
    first_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    gone_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    link_method: Mapped[str] = mapped_column(sa.Text, nullable=False, default="deterministic_key")
    link_confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    link_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (
        sa.CheckConstraint(f"link_method IN {LINK_METHODS!r}", name="link_method_vocab"),
        sa.Index(
            "uq_opportunity_source_active",
            "source_id",
            "source_record_id",
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active"),
        ),
    )


# ===================================================================================== event (§3.10)
class Event(Base):
    """The append-only log (docs/21 §3.10, §6.1). No role but `migration` may UPDATE/DELETE it;
    application code never issues those statements against this table (docs/04 DA-3)."""

    __tablename__ = "event"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    seq: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=False), nullable=False, unique=True)
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_id: Mapped[_uuid.UUID] = mapped_column(GUID(), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    public_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source_id: Mapped[str | None] = mapped_column(sa.ForeignKey("source.id"))
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    retrieved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    licence_id: Mapped[str | None] = mapped_column(sa.ForeignKey("licence.id"))
    snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    changed_keys: Mapped[list[str]] = mapped_column(TextArray(), nullable=False, default=list)
    actor_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")
    actor_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    reason: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Numeric(4, 3))
    reverses_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    run_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("source_run.id"))
    job_id: Mapped[str | None] = mapped_column(sa.Text)
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)

    __table_args__ = (
        sa.CheckConstraint(f"actor_type IN {ACTOR_TYPES!r}", name="actor_type_vocab"),
        sa.Index("ix_event_subject_observed", "subject_type", "subject_id", sa.text("observed_at DESC")),
        sa.Index("ix_event_public_at", "public_at"),
    )


# ===================================================================================== match (§3.11)
class Match(Base):
    __tablename__ = "match"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    proposal_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("proposal.id"), nullable=False)
    opportunity_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("opportunity.id"), nullable=False)
    score: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False)
    rationale: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    rationale_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rule_set_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="rule")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    first_matched_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_evaluated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    removed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    crm_lead_ref: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(f"status IN {MATCH_STATUSES!r}", name="status_vocab"),
        sa.Index(
            "uq_match_active_pair",
            "proposal_id",
            "opportunity_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
    )
