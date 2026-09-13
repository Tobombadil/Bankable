"""The visibility predicate (docs/21-data-model.md §5.4; docs/04-standards.md S-3).

    visible(r, t, now) :=
          r.publish_state = 'public'
      AND source_permits(r.source_id, t)
      AND licence_permits(r.licence_id, t, field_class)
      AND (t = 'public' ? r.public_at <= now : r.published_at <= now)

Sprint 2 implemented the `public` tier only; this sprint (Pro tier and alerts) adds the
`entitlement` parameter the module's own docstring used to say was out of scope. Every `*_filter`
function below now takes an `entitlement` (`public | pro | api`; `admin` is out of scope —
admin reads bypass this predicate entirely per docs/21 §5.4's fourth row) and dispatches on it:

- `public_at` is read only for `entitlement == "public"`; `pro`/`api` read `published_at`
  (docs/21 §5.4 "the only column the public predicate reads" — true of *the public branch*, not
  of the predicate as a whole).
- `source_permits` for `public` still requires the source's own `publish_state == "public"`
  (open decision 5 in services/README.md, unchanged); `pro`/`api` additionally accept
  `publish_state == "api_only"` — a source an operator has cleared for ingestion and the licence
  gate but not yet flipped to the public surface specifically is exactly what Pro/API tiers exist
  to see sooner (docs/20 §5's tier table).
- `licence_permits` (`PUBLISHABLE_REUSE_CLASSES`) is **unchanged across tiers**: `restricted`/
  `unknown` sources stay invisible on every non-admin surface, Pro included — the standing,
  stricter reading of docs/21 D-2 and `CLAUDE.md`, not loosened by this sprint. Licence gating is
  orthogonal to tier and stricter by design (docs/20 §5): tier answers "how old", licence answers
  "at all".
- `r.publish_state == 'public'` at the record level is unconditional on tier, matching the
  pseudocode above exactly (not "unless admin", which the module does not implement).

Backward-compatible names `proposal_public_filter`/`opportunity_public_filter`/
`event_public_filter` are kept as the `entitlement="public"` case — `services/api/app.py`'s public
routes are unchanged by this sprint.
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

#: The three non-admin entitlements this predicate distinguishes. Typed as plain `str` at every
#: function boundary below (not a `Literal["public","pro","api"]`) because the real caller-facing
#: type, `services.api.auth.AuthContext.entitlement`, is also `str` — it can legitimately hold
#: `"admin"` too (an operator's own account entitlement), which this module was never meant to
#: special-case (admin reads bypass this predicate entirely, per the module docstring). Any value
#: other than `"pro"`/`"api"` is treated as `"public"` by `_PERMITTED_SOURCE_STATES.get` below, so
#: an unrecognised entitlement never accidentally widens visibility.
Entitlement = str

PUBLISHABLE_REUSE_CLASSES = ("open", "attribution")
# Drift guard, not a real code path: pragma'd out of the module's required 100% branch coverage
# (docs/04 E-7) rather than exercised by a test that would need to corrupt REUSE_CLASSES itself.
if not set(PUBLISHABLE_REUSE_CLASSES) <= set(REUSE_CLASSES):  # pragma: no cover
    raise RuntimeError("PUBLISHABLE_REUSE_CLASSES has drifted from services.db.models.REUSE_CLASSES")

#: `source_permits(source_id, t)` (docs/21 §5.4): the `source.publish_state` values each
#: entitlement may read from. `public` stays exactly the source's own public surface flag (open
#: decision 5); `pro`/`api` also see a source cleared for ingestion-and-licence but not yet the
#: public surface (`api_only`).
_PERMITTED_SOURCE_STATES: dict[str, tuple[str, ...]] = {
    "public": ("public",),
    "pro": ("public", "api_only"),
    "api": ("public", "api_only"),
}


def _has_permitted_source(
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk: ColumnElement[bool],
    entitlement: Entitlement,
) -> ColumnElement[bool]:
    return exists(
        select(link_model.id)
        .join(Source, Source.id == link_model.source_id)
        .where(
            fk,
            link_model.active.is_(True),
            Source.publish_state.in_(_PERMITTED_SOURCE_STATES.get(entitlement, ("public",))),
        )
    )


def proposal_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    timing = (
        [Proposal.public_at.is_not(None), Proposal.public_at <= now]
        if entitlement == "public"
        else [Proposal.published_at.is_not(None), Proposal.published_at <= now]
    )
    return [
        Proposal.publish_state == "public",
        *timing,
        Proposal.min_reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        _has_permitted_source(ProposalSource, ProposalSource.proposal_id == Proposal.id, entitlement),
    ]


def opportunity_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    timing = (
        [Opportunity.public_at.is_not(None), Opportunity.public_at <= now]
        if entitlement == "public"
        else [Opportunity.published_at.is_not(None), Opportunity.published_at <= now]
    )
    return [
        Opportunity.publish_state == "public",
        *timing,
        Opportunity.min_reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        _has_permitted_source(
            OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id, entitlement
        ),
    ]


def event_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    timing = (
        [Event.public_at.is_not(None), Event.public_at <= now]
        if entitlement == "public"
        else [Event.published_at.is_not(None), Event.published_at <= now]
    )
    return [
        *timing,
        exists(
            select(Licence.id).where(
                Licence.id == Event.licence_id, Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES)
            )
        ),
        exists(
            select(Source.id).where(
                Source.id == Event.source_id,
                Source.publish_state.in_(_PERMITTED_SOURCE_STATES.get(entitlement, ("public",))),
            )
        ),
    ]


def proposal_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    return proposal_visibility_filter("public", now)


def opportunity_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    return opportunity_visibility_filter("public", now)


def event_public_filter(now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    return event_visibility_filter("public", now)
