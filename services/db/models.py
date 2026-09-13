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
from services.db.types import GUID, GeographyPoint, JSONVariant, TextArray, uuid7

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

# --------------------------------------------------------------------------------------------
# Pro tier and alerts (Sprint 2 backend brief: auth, tier enforcement, saved searches/alerts,
# webhooks). docs/21 §3.12-§3.17; `legal` added to USER_ROLES per docs/21 §3.12's 2026-09-12 note
# (US-905 AC1 needs a role distinct from `owner` to clear a licence gate) — api/openapi.yaml's
# `UserRole` enum was stale on this and is corrected alongside this migration (services/README.md
# "Pro tier and alerts").
# --------------------------------------------------------------------------------------------
USER_ROLES = ("viewer", "member", "operator", "legal", "owner")
USER_STATUSES = ("active", "disabled", "anonymised")
#: docs/20 §7 / api/openapi.yaml specify `magic_link | google` (passwordless). This task's brief
#: explicitly asks for "signed cookie, argon2 password hashing" session auth, which `password`
#: accommodates without dropping the other two — recorded as a decision in services/README.md
#: rather than silently overriding docs/20 §7.
AUTH_PROVIDERS = ("magic_link", "google", "password")
ACCOUNT_KINDS = ("personal", "organization")
ACCOUNT_ENTITLEMENTS = ("public", "pro", "api", "admin")
ACCOUNT_ENTITLEMENT_SOURCES = ("sor", "manual_grant", "trial")
ACCOUNT_STATUSES = ("active", "suspended", "closed")
API_KEY_SCOPES = ("read:public", "read:live", "read:bulk", "write:webhooks", "admin:*")
API_KEY_PREFIXES = ("bk_live", "bk_test")
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


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_uuid() -> _uuid.UUID:
    return uuid7()


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )
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
    #: The free-text `license` clause `data/sources.yaml` records for this source (the actual
    #: quoted terms), distinct from `notes` (data-engineer commentary about them). Added so the
    #: public `source`/`licence` resources can carry a real licence quote instead of web/'s
    #: composed paraphrase (web/README.md "Missing from the API" item 3).
    quote_text: Mapped[str | None] = mapped_column(sa.Text)

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
        sa.Index("ix_source_publish_state", "publish_state"),
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
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )

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
    first_seen: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_changed: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    merged_into_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("organization.id"))


# ====================================================================== organization_alias (§3.6)
class OrganizationAlias(Base):
    __tablename__ = "organization_alias"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    organization_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("organization.id"), nullable=False
    )
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alias_normalised: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")

    __table_args__ = (sa.UniqueConstraint("organization_id", "alias_normalised", name="one_alias_per_org"),)


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

    source: Mapped[Source] = relationship(lazy="joined")
    licence: Mapped[Licence] = relationship(lazy="joined")

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
    first_seen: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_changed: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
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
    sources: Mapped[list[ProposalSource]] = relationship(
        primaryjoin="Proposal.id == ProposalSource.proposal_id",
        foreign_keys="ProposalSource.proposal_id",
        viewonly=True,
    )

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
    first_seen: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    gone_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    link_method: Mapped[str] = mapped_column(sa.Text, nullable=False, default="deterministic_key")
    link_confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    link_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    source: Mapped[Source] = relationship(lazy="joined")

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
        # services/api/visibility.py's `_has_public_source` is a correlated `EXISTS` keyed on
        # `proposal_id` for every candidate proposal row; with no index on this column SQLite (and
        # Postgres alike) falls back to a full `proposal_source` scan per outer row. Measured at
        # ~10,400 real proposals: 14-75s per list/geo page without this index, ~0.1s with it
        # (services/README.md "Sprint 2 fixes" has the full before/after) -- this single index is
        # the fix, not a query rewrite; `public_at` was already materialised (docs/21 §5.4).
        sa.Index("ix_proposal_source_proposal_id", "proposal_id"),
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
    first_seen: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_changed: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
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
    sources: Mapped[list[OpportunitySource]] = relationship(
        primaryjoin="Opportunity.id == OpportunitySource.opportunity_id",
        foreign_keys="OpportunitySource.opportunity_id",
        viewonly=True,
    )

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
    opportunity_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("opportunity.id"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_record_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    normalised: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    status_raw: Mapped[str | None] = mapped_column(sa.Text)
    first_seen: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    gone_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    link_method: Mapped[str] = mapped_column(sa.Text, nullable=False, default="deterministic_key")
    link_confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False, default=1.0)
    link_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    source: Mapped[Source] = relationship(lazy="joined")

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
        # Same fix as `ix_proposal_source_proposal_id` above, for the opportunity side of
        # `services/api/visibility.py`'s `_has_public_source`.
        sa.Index("ix_opportunity_source_opportunity_id", "opportunity_id"),
    )


# ===================================================================================== event (§3.10)
class Event(Base):
    """The append-only log (docs/21 §3.10, §6.1). No role but `migration` may UPDATE/DELETE it;
    application code never issues those statements against this table (docs/04 DA-3)."""

    __tablename__ = "event"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    #: `bigint identity` in the canonical Postgres migration (docs/21 §3.10). The ORM leaves this
    #: unmanaged and a `before_insert` listener below assigns the next value transactionally —
    #: portable across Postgres and this sprint's SQLite test target, where a real `IDENTITY`
    #: column is not available. Production inserts against Postgres should prefer the DB
    #: identity default; the loader (services/ingest/loader.py) never sets `seq` itself.
    #: Caveat: the listener reads `MAX(seq)` from rows already *inserted* in the current
    #: transaction, so events must be flushed one at a time (as the loader does) rather than
    #: batched into one multi-row INSERT, or several rows in the same batch would compute the
    #: same MAX() and collide on the `seq` unique constraint.
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, unique=True)
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_id: Mapped[_uuid.UUID] = mapped_column(GUID(), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
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

    source: Mapped[Source | None] = relationship(lazy="joined")
    licence: Mapped[Licence | None] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"actor_type IN {ACTOR_TYPES!r}", name="actor_type_vocab"),
        sa.Index("ix_event_subject_observed", "subject_type", "subject_id", sa.text("observed_at DESC")),
        sa.Index("ix_event_public_at", "public_at"),
    )


@sa.event.listens_for(Event, "before_insert")
def _assign_event_seq(mapper: Any, connection: sa.Connection, target: Event) -> None:
    """Portable stand-in for the canonical Postgres `bigint identity` column (see the `seq`
    docstring above): the next value within the current transaction, so ordering is exact
    even across a batch of events committed together (docs/20 §3.7)."""
    if target.seq is None:
        current_max = connection.execute(sa.select(sa.func.max(Event.__table__.c.seq))).scalar()
        target.seq = (current_max or 0) + 1


# ===================================================================================== match (§3.11)
class Match(Base):
    __tablename__ = "match"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    proposal_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("proposal.id"), nullable=False)
    opportunity_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("opportunity.id"), nullable=False
    )
    score: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False)
    rationale: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    rationale_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rule_set_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="rule")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    first_matched_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
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


# ===================================================================================== account (§3.13)
class Account(Base, TimestampMixin):
    """The entitlement mirror (docs/21 §3.13). Authoritative for nothing once a real CRM/ERP
    adapter exists (`docs/20` §9); this sprint has no billing integration (Sprint 3), so
    `entitlement_source = 'manual_grant'` rows are set directly by an admin endpoint
    (services/README.md "Pro tier and alerts" open decisions)."""

    __tablename__ = "account"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False, default="personal")
    organization_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("organization.id"))
    entitlement: Mapped[str] = mapped_column(sa.Text, nullable=False, default="public")
    entitlement_source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="manual_grant")
    entitlement_checked_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    entitlement_stale: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    seats: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    seats_used: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    sor_kind: Mapped[str | None] = mapped_column(sa.Text)
    sor_ref: Mapped[str | None] = mapped_column(sa.Text)
    billing_ref: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")

    __table_args__ = (
        sa.CheckConstraint(f"kind IN {ACCOUNT_KINDS!r}", name="kind_vocab"),
        sa.CheckConstraint(f"entitlement IN {ACCOUNT_ENTITLEMENTS!r}", name="entitlement_vocab"),
        sa.CheckConstraint(
            f"entitlement_source IN {ACCOUNT_ENTITLEMENT_SOURCES!r}", name="entitlement_source_vocab"
        ),
        sa.CheckConstraint(f"status IN {ACCOUNT_STATUSES!r}", name="status_vocab"),
    )


# ======================================================================================== user (§3.12)
class User(Base, TimestampMixin):
    """docs/21 §3.12. `role` includes `legal` (2026-09-12 note); `password_hash` is this sprint's
    addition (see `AUTH_PROVIDERS` docstring)."""

    __tablename__ = "user"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    account_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("account.id"), nullable=False)
    email: Mapped[str | None] = mapped_column(sa.Text)
    password_hash: Mapped[str | None] = mapped_column(sa.Text)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    name: Mapped[str | None] = mapped_column(sa.Text)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False, default="member")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    auth_provider: Mapped[str] = mapped_column(sa.Text, nullable=False, default="password")
    mfa_enforced: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    marketing_consent: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    consent_version: Mapped[str | None] = mapped_column(sa.Text)
    tos_version: Mapped[str | None] = mapped_column(sa.Text)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    anonymised_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sor_kind: Mapped[str | None] = mapped_column(sa.Text)
    sor_ref: Mapped[str | None] = mapped_column(sa.Text)

    account: Mapped[Account] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"role IN {USER_ROLES!r}", name="role_vocab"),
        sa.CheckConstraint(f"status IN {USER_STATUSES!r}", name="status_vocab"),
        sa.CheckConstraint(f"auth_provider IN {AUTH_PROVIDERS!r}", name="auth_provider_vocab"),
        sa.Index(
            "uq_user_email_active",
            "email",
            unique=True,
            postgresql_where=sa.text("status <> 'anonymised'"),
            sqlite_where=sa.text("status <> 'anonymised'"),
        ),
    )


# =============================================================================== session (§4.4 table)
class UserSession(Base):
    """Revocable server-side session row backing the signed session cookie (docs/20 §7, docs/04
    S-2). Named `UserSession`/`__tablename__ = "session"` rather than a Python class `Session` to
    avoid colliding with `sqlalchemy.orm.Session` imported throughout this codebase."""

    __tablename__ = "session"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    user_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ip_prefix: Mapped[str | None] = mapped_column(sa.Text)


# ====================================================================================== api_key (§3.17)
class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_key"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    account_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("account.id"), nullable=False)
    created_by_user_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prefix: Mapped[str] = mapped_column(sa.Text, nullable=False, default="bk_live")
    last4: Mapped[str] = mapped_column(sa.String(4), nullable=False)
    key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(TextArray(), nullable=False, default=list)
    tier: Mapped[str] = mapped_column(sa.Text, nullable=False, default="public")
    rate_limit_per_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=6000)
    daily_quota: Mapped[int | None] = mapped_column(sa.Integer)
    expires_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(sa.Text)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    licence_accepted_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    licence_accepted_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    account: Mapped[Account] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"prefix IN {API_KEY_PREFIXES!r}", name="prefix_vocab"),
        sa.CheckConstraint(f"tier IN {API_KEY_TIERS!r}", name="tier_vocab"),
        sa.Index("ix_api_key_account_id", "account_id"),
    )


# ================================================================================ saved_search (§3.15)
class SavedSearch(Base, TimestampMixin):
    __tablename__ = "saved_search"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    user_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=False)
    account_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("account.id"), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity: Mapped[str] = mapped_column(sa.Text, nullable=False, default="proposal")
    query: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    query_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    delivery_mode: Mapped[str] = mapped_column(sa.Text, nullable=False, default="daily")
    channels: Mapped[list[str]] = mapped_column(TextArray(), nullable=False, default=lambda: ["email"])
    rss_token: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: `bigint` per docs/21 §3.15; the highest `event.seq` this saved search has already
    #: considered, so the evaluation job is exactly-once (services/alerts/evaluate.py).
    watermark_seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    last_match_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")

    user: Mapped[User] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"entity IN {SAVED_SEARCH_ENTITIES!r}", name="entity_vocab"),
        sa.CheckConstraint(f"delivery_mode IN {SAVED_SEARCH_DELIVERY_MODES!r}", name="delivery_mode_vocab"),
        sa.CheckConstraint(f"status IN {SAVED_SEARCH_STATUSES!r}", name="status_vocab"),
        sa.UniqueConstraint("user_id", "name", name="one_name_per_user"),
        sa.Index("ix_saved_search_status_delivery", "status", "delivery_mode"),
    )


# ======================================================================================== alert (§3.16)
class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    saved_search_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("saved_search.id"), nullable=False
    )
    user_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=False)
    channel: Mapped[str] = mapped_column(sa.Text, nullable=False, default="email")
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False, default="daily")
    window_start: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    window_end: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    #: `bigint[]` per docs/21 §3.16; stored as a JSON list of ints (`JSONVariant`, not
    #: `TextArray`, which this codebase types as text) since this sprint has no other consumer of
    #: an integer array column and adding one just for this is not worth it (services/README.md).
    event_seqs: Mapped[list[int]] = mapped_column(JSONVariant(), nullable=False, default=list)
    recipient: Mapped[str | None] = mapped_column(sa.Text)
    subject: Mapped[str | None] = mapped_column(sa.Text)
    provider_message_id: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="queued")
    sent_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    unsubscribe_token: Mapped[str] = mapped_column(sa.Text, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    saved_search: Mapped[SavedSearch] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"channel IN {ALERT_CHANNELS!r}", name="channel_vocab"),
        sa.CheckConstraint(f"mode IN {ALERT_MODES!r}", name="mode_vocab"),
        sa.CheckConstraint(f"status IN {ALERT_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_alert_saved_search_created", "saved_search_id", sa.text("created_at DESC")),
    )


# ============================================================================= webhook_endpoint (§4.4)
class WebhookEndpoint(Base, TimestampMixin):
    """docs/23 §9.1: "a webhook is literally a saved search with a URL as its channel"."""

    __tablename__ = "webhook_endpoint"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    account_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("account.id"), nullable=False)
    created_by_user_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    types: Mapped[list[str]] = mapped_column(TextArray(), nullable=False, default=list)
    entity: Mapped[str] = mapped_column(sa.Text, nullable=False, default="event")
    query: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    #: Stored in the clear, unlike `ApiKey.key_hash` — the platform is the one *sending* a signed
    #: request here, not verifying a bearer credential presented to it, so a one-way hash cannot
    #: work: HMAC-signing every outbound delivery needs the actual secret, every time (docs/23 §9.1
    #: `X-Platform-Signature`; the customer holds the same value to verify). "Shown once" in
    #: `api/openapi.yaml` describes the client-facing UX, not server-side discard — the same
    #: pattern Stripe and GitHub webhooks use. This sprint has no envelope-encryption/KMS story for
    #: secrets at rest (services/README.md "Pro tier and alerts" flags it as a follow-up, same
    #: gap `docs/04` E-19 already calls out for deploy-time secrets generally).
    secret: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_delivery_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    secret_rotated_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(f"entity IN {SAVED_SEARCH_ENTITIES!r}", name="entity_vocab"),
        sa.CheckConstraint(f"status IN {WEBHOOK_ENDPOINT_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_webhook_endpoint_account_id", "account_id"),
    )


# ============================================================================= webhook_delivery (§4.4)
class WebhookDelivery(Base):
    __tablename__ = "webhook_delivery"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    webhook_endpoint_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("webhook_endpoint.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    event_seq: Mapped[int | None] = mapped_column(sa.BigInteger)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    response_status: Mapped[int | None] = mapped_column(sa.Integer)
    response_body_excerpt: Mapped[str | None] = mapped_column(sa.Text)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error_class: Mapped[str | None] = mapped_column(sa.Text)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    endpoint: Mapped[WebhookEndpoint] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"type IN {WEBHOOK_TYPES!r}", name="type_vocab"),
        sa.CheckConstraint(f"status IN {WEBHOOK_DELIVERY_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_webhook_delivery_endpoint_created", "webhook_endpoint_id", sa.text("created_at DESC")),
    )


# ============================================================================= subscription (§3.14)
# Sprint 3, first wave (docs/00-PLAN.md; docs/34-crm-system-of-record.md §1, §5): the Stripe billing
# adapter and the entitlement-application logic behind it (`services/billing/`). `SUBSCRIPTION_
# STATUSES` mirrors `services.sor.ports.STATUSES_IN_GOOD_STANDING`'s source vocabulary (docs/21
# §3.14) rather than importing it — this module never imports `services.sor` (docs/04 E-2 layering:
# the DB layer stays vendor- and port-free), the same reason `services/db/migrations/versions/
# 0003_pro_tier_and_alerts.py` duplicates `USER_ROLES` etc. instead of importing `services/db/
# models.py`.
#
# `plan_tier` keeps docs/21's `free | pro | team | api` vocabulary — the same four values as
# `api/openapi.yaml`'s `PlanTier` schema — rather than the wider vendor-neutral
# `services.sor.ports.PLAN_TIERS` (`pro | team | api | enterprise`: the commercial names a
# customer can *buy*, one purchasable tier above `api`). Neither the spec's `PlanTier` nor this
# sprint's docs/21 reading has an `enterprise` member, so `services/billing/entitlement.py` stores
# an `enterprise` purchase with `plan_tier = "api"` (the entitlement `enterprise` grants, per
# `services.sor.ports.PLAN_ENTITLEMENT`) while `plan_code` keeps the vendor's exact price/plan
# code — no information is lost, and this CHECK constraint never needs the "extend an existing
# vocab tuple" escape hatch CLAUDE.md's task brief allows (services/billing/README.md decision #3).
SUBSCRIPTION_SOR_KINDS = ("stripe", "odoo", "erpnext")
SUBSCRIPTION_PLAN_TIERS = ("free", "pro", "team", "api")
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "paused", "canceled")


class Subscription(Base, TimestampMixin):
    """docs/21 §3.14: the mirror of the commercial record. Written only by
    `services/billing/entitlement.py:apply_entitlement_change` from a billing-provider webhook —
    never by hand (docs/34 §1: "Stripe writes it; the platform never treats a CRM as the source of
    a commercial fact")."""

    __tablename__ = "subscription"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    account_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("account.id"), nullable=False)
    sor_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sor_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    plan_code: Mapped[str] = mapped_column(sa.Text, nullable=False)
    plan_tier: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    seats: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    current_period_start: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    cancel_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    mrr_amount: Mapped[float | None] = mapped_column(sa.Numeric(18, 2))
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    mirrored_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    drift_flag: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    account: Mapped[Account] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"sor_kind IN {SUBSCRIPTION_SOR_KINDS!r}", name="sor_kind_vocab"),
        sa.CheckConstraint(f"plan_tier IN {SUBSCRIPTION_PLAN_TIERS!r}", name="plan_tier_vocab"),
        sa.CheckConstraint(f"status IN {SUBSCRIPTION_STATUSES!r}", name="status_vocab"),
        sa.UniqueConstraint("sor_kind", "sor_ref", name="one_subscription_per_sor_ref"),
        sa.Index("ix_subscription_account_id", "account_id"),
    )


# ============================================================= admin panel tables (Sprint 3 item 3)
# Coordinator-owned contract for the admin wave (docs/00-PLAN.md "Sprint 3 kickoff" item 3): the
# docs/21 tables the admin surface reads and writes that no earlier sprint had a writer for —
# `document` (§3.8), `extraction` (§3.9), `post` (§3.18), `task` and `model_call` (§4.4) — plus one
# small table docs/21 does not name, `channel_config`, for the per-channel auto-publish switch that
# `PUT /admin/v1/channels/{channel}/auto-publish` sets (docs/20 §8 item 5; owner role only).
# Vocabularies are the spec's enums (api/openapi.yaml `TaskType`, `TaskStatus`, `PostState`,
# `ExtractionStatus`) so the CHECK constraints and the contract cannot disagree.
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


class Document(Base, TimestampMixin):
    """docs/21 §3.8. Provenance quartet is not-null like every stored record (CLAUDE.md); news is
    never `stored` (`storage_policy`), and `robots_opt_out` blocks text extraction."""

    __tablename__ = "document"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="none")
    subject_id: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    snapshot_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("snapshot.id"))
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="other")
    published_date: Mapped[dt.date | None] = mapped_column(sa.Date)
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    storage_policy: Mapped[str] = mapped_column(sa.Text, nullable=False, default="link_only")
    object_key: Mapped[str | None] = mapped_column(sa.Text)
    content_type: Mapped[str | None] = mapped_column(sa.Text)
    byte_size: Mapped[int | None] = mapped_column(sa.BigInteger)
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    text_extracted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    personal_data_flag: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    robots_opt_out: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    __table_args__ = (
        sa.CheckConstraint(f"subject_type IN {DOCUMENT_SUBJECT_TYPES!r}", name="subject_type_vocab"),
        sa.CheckConstraint(f"doc_type IN {DOCUMENT_TYPES!r}", name="doc_type_vocab"),
        sa.CheckConstraint(f"storage_policy IN {DOCUMENT_STORAGE_POLICIES!r}", name="storage_policy_vocab"),
        sa.Index("ix_document_subject", "subject_type", "subject_id"),
    )


class ModelCall(Base):
    """docs/21 §4.4 `model_call`: one row per gateway call (US-909). `alias` is `fast | careful |
    batch`, never a provider model id (CLAUDE.md). No gateway writes here yet; the admin cost report
    reads it so the column set is fixed now."""

    __tablename__ = "model_call"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(sa.Text)
    subject_id: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    source_id: Mapped[str | None] = mapped_column(sa.ForeignKey("source.id"))
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_template_id: Mapped[str | None] = mapped_column(sa.Text)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Numeric(10, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    cache_hit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(f"alias IN {MODEL_ALIASES!r}", name="alias_vocab"),
        sa.Index("ix_model_call_source_created", "source_id", "created_at"),
    )


class Extraction(Base, TimestampMixin):
    """docs/21 §3.9. An extraction without a citation is rejected at the application layer; the
    licence gate on the extracted value inherits `source_id`/`licence_id` from the document."""

    __tablename__ = "extraction"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    document_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("document.id"))
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_id: Mapped[_uuid.UUID] = mapped_column(GUID(), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False, default="extract")
    field_path: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(sa.Numeric(4, 3), nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant(), nullable=False, default=list)
    model_alias: Mapped[str | None] = mapped_column(sa.Text)
    prompt_template_id: Mapped[str | None] = mapped_column(sa.Text)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)
    model_call_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("model_call.id"))
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="proposed")
    accepted_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("user.id"))
    applied_event_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("event.id"))
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)

    __table_args__ = (
        sa.CheckConstraint(f"purpose IN {EXTRACTION_PURPOSES!r}", name="purpose_vocab"),
        sa.CheckConstraint(f"status IN {EXTRACTION_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_extraction_status_created", "status", "created_at"),
        sa.Index("ix_extraction_subject", "subject_type", "subject_id"),
    )


class Post(Base, TimestampMixin):
    """docs/21 §3.18: the social review queue row (US-801 to US-804). `approved_by_user_id` is
    null only when the channel's `auto_publish` was on; `reject_reason` is required on reject."""

    __tablename__ = "post"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    channel: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("event.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_id: Mapped[_uuid.UUID] = mapped_column(GUID(), nullable=False)
    template_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    template_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    link_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    credit_line: Mapped[str] = mapped_column(sa.Text, nullable=False)
    disclosure_label: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="draft")
    gate_checked_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    approved_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("user.id"))
    auto_published: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    external_post_id: Mapped[str | None] = mapped_column(sa.Text)
    reject_reason: Mapped[str | None] = mapped_column(sa.Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    cost_usd: Mapped[float] = mapped_column(sa.Numeric(10, 4), nullable=False, default=0)

    __table_args__ = (
        sa.CheckConstraint(f"channel IN {POST_CHANNELS!r}", name="channel_vocab"),
        sa.CheckConstraint(f"state IN {POST_STATES!r}", name="state_vocab"),
        sa.Index("ix_post_queue", "state", "channel", "created_at"),
    )


class ChannelConfig(Base, TimestampMixin):
    """Per-channel publishing switch (docs/20 §8 item 5; docs/32 §6). `auto_publish` defaults off
    and only an `owner` session may set it; `disclosure_label` is what a published post carries
    where the platform requires an automation label (CLAUDE.md)."""

    __tablename__ = "channel_config"

    channel: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    auto_publish: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    disclosure_label: Mapped[str | None] = mapped_column(sa.Text)
    daily_cap: Mapped[int | None] = mapped_column(sa.Integer)
    updated_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("user.id"))

    __table_args__ = (sa.CheckConstraint(f"channel IN {POST_CHANNELS!r}", name="channel_vocab"),)


class Task(Base, TimestampMixin):
    """docs/21 §4.4 `task`, widened to the spec's `Task` schema: the admin work queue for reported
    problems (US-204), intake submissions (US-1001/1003, `pending_record` holds the submission
    until approved), deletion requests (US-910) and resolution disputes (US-907). `contact` holds
    the minimum personal data the flow needs and is cleared on completion of a deletion request."""

    __tablename__ = "task"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    public_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(sa.Text)
    subject_id: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="open")
    assignee_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("user.id"))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    issue_type: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    contact: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    pending_record: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    resolver_suggestions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONVariant(), nullable=False, default=list
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    public_opt_in: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    audit_event_ids: Mapped[list[str]] = mapped_column(JSONVariant(), nullable=False, default=list)
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID(), sa.ForeignKey("user.id"))
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text, unique=True)

    __table_args__ = (
        sa.CheckConstraint(f"type IN {TASK_TYPES!r}", name="type_vocab"),
        sa.CheckConstraint(f"status IN {TASK_STATUSES!r}", name="status_vocab"),
        sa.Index("ix_task_queue", "status", "type", "created_at"),
        sa.Index("ix_task_subject", "subject_type", "subject_id"),
    )


# ======================================================================== worker watermarks (Sprint 3)
class WorkerWatermark(Base):
    """One row per scheduled worker that scans the event log (`services/social/worker.py` today):
    the highest `event.seq` it has already examined, so a run of events that earn nothing (gated,
    duplicate, ineligible) is never rescanned on every tick and can never starve newer events
    behind a page limit. Written in the worker's own transaction at the end of each tick."""

    __tablename__ = "worker_watermark"

    name: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
