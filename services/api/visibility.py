"""The visibility predicate (docs/21-data-model.md §5.4; docs/04-standards.md S-3).

    visible(r, t, now) :=
          r.publish_state = 'public'
      AND source_permits(r.source_id, t)
      AND licence_permits(r.licence_id, t, field_class)
      AND (t = 'public' ? r.public_at <= now : r.published_at <= now)

This service implements the `public` tier only (Pro/API/admin are out of scope this sprint), so
every function here hard-codes `t = public`: `public_at` is the only visibility column read, never
`published_at` (docs/21 §5.4's "the only column the public predicate reads"), and
`source_permits` requires the record's own source to be marked `publish_state = 'public'`
(not merely `api_only`) — a stricter bar than "ingested", by design (docs/04 API-1..9 tier table).

`licence_permits` is enforced twice, defensively: the ingest loader (`services/ingest/loader.py`)
refuses to write any record whose source is `restricted`/`unknown`, and the clauses below repeat
the check on `min_reuse_class` so that a query built from this module cannot expose a gated row
even if that first gate were ever bypassed (docs/20 §11: "a query without a tier and licence
predicate cannot be constructed from the public API code path").
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, exists, select

from services.db.models import (
    REUSE_CLASSES,
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
)

PUBLISHABLE_REUSE_CLASSES = ("open", "attribution")
# Drift guard, not a real code path: pragma'd out of the module's required 100% branch coverage
# (docs/04 E-7) rather than exercised by a test that would need to corrupt REUSE_CLASSES itself.
if not set(PUBLISHABLE_REUSE_CLASSES) <= set(REUSE_CLASSES):  # pragma: no cover
    raise RuntimeError("PUBLISHABLE_REUSE_CLASSES has drifted from services.db.models.REUSE_CLASSES")


def _has_public_source(
    link_model: type[ProposalSource] | type[OpportunitySource], fk: ColumnElement[bool]
) -> ColumnElement[bool]:
    return exists(
        select(link_model.id)
        .join(Source, Source.id == link_model.source_id)
        .where(
            fk,
            link_model.active.is_(True),
            Source.publish_state == "public",
        )
    )


def proposal_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    return [
        Proposal.publish_state == "public",
        Proposal.public_at.is_not(None),
        Proposal.public_at <= now,
        Proposal.min_reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        _has_public_source(ProposalSource, ProposalSource.proposal_id == Proposal.id),
    ]


def opportunity_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    return [
        Opportunity.publish_state == "public",
        Opportunity.public_at.is_not(None),
        Opportunity.public_at <= now,
        Opportunity.min_reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        _has_public_source(OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id),
    ]


def event_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    return [
        Event.public_at.is_not(None),
        Event.public_at <= now,
        exists(
            select(Licence.id).where(
                Licence.id == Event.licence_id, Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES)
            )
        ),
        exists(select(Source.id).where(Source.id == Event.source_id, Source.publish_state == "public")),
    ]
