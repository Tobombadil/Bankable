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

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import aliased

from services.db.models import (
    REUSE_CLASSES,
    Asset,
    Event,
    Licence,
    Location,
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
# Drift guard against `services.db.models.REUSE_CLASSES`. Both arcs are covered by
# `tests/test_visibility_predicate.py` (ordinary import; a re-import under a patched vocabulary),
# so this module carries no coverage exclusion and docs/04 E-7's 100% gate measures all of it.
if not set(PUBLISHABLE_REUSE_CLASSES) <= set(REUSE_CLASSES):
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
    """An event is visible only when its *subject* is (2026-09-18 audit, docs/50 §3.1 "the events
    feed ... bypass[es] the record-level visibility gate"): the event's own licence, source and
    timing clauses were already here, but nothing joined the subject, so a `status_change` on a
    hidden, restricted-licence or not-yet-public proposal still leaked the proposal's name, slug
    and before/after state through `GET /v1/events` and `/feeds/events.*` (docs/21 §8 item 2:
    "no event ... including in the global feed, RSS and webhooks"). The subject clause is the
    same `proposal_visibility_filter`/`opportunity_visibility_filter` the record endpoints use,
    correlated on `Event.subject_id`, so the two can never disagree. An event on any other
    subject type (`user` audit events from `services/api/admin_people.py`, or a subject type
    added later) is invisible on every non-admin surface -- fail closed, not "Unknown"."""
    now = now or dt.datetime.now(dt.UTC)
    timing = (
        [Event.public_at.is_not(None), Event.public_at <= now]
        if entitlement == "public"
        else [Event.published_at.is_not(None), Event.published_at <= now]
    )
    subject_visible = or_(
        and_(
            Event.subject_type == "proposal",
            exists(
                select(Proposal.id).where(
                    Proposal.id == Event.subject_id, *proposal_visibility_filter(entitlement, now)
                )
            ),
        ),
        and_(
            Event.subject_type == "opportunity",
            exists(
                select(Opportunity.id).where(
                    Opportunity.id == Event.subject_id, *opportunity_visibility_filter(entitlement, now)
                )
            ),
        ),
    )
    return [
        *timing,
        subject_visible,
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


def asset_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    """`visible_asset_predicate` (2026-09-18 audit, docs/50 §3.1 "the asset endpoint had no licence
    gate"): assets carry no lag and no record-level `publish_state` (ADR 0008), so the predicate is
    the two clauses of docs/21 §5.4 that do apply -- `licence_permits` (the asset's own
    `licence_id` resolves to an `open`/`attribution` licence, `PUBLISHABLE_REUSE_CLASSES`,
    unchanged across tiers) and `source_permits` (the asset's `source_id` is in a `publish_state`
    the entitlement may read, the same `_PERMITTED_SOURCE_STATES` table proposals use). Every
    asset read path (`services/api/assets.py`: list, detail, geo index and its cache key, nearby,
    organisation assets, the plants alias) filters through this; the geo index cache is keyed on
    the visible set so a licence or source flip invalidates it. `now` is accepted for signature
    parity with the other filters and unused: nothing on `asset` is time-gated."""
    del now
    # Aliased so the EXISTS keeps its own FROM even when the caller's outer query already joins
    # `licence`/`source` (the geo licence aggregate does), which SQLAlchemy's auto-correlation
    # would otherwise strip.
    lic = aliased(Licence)
    src = aliased(Source)
    return [
        exists(
            select(lic.id).where(lic.id == Asset.licence_id, lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES))
        ),
        exists(
            select(src.id).where(
                src.id == Asset.source_id,
                src.publish_state.in_(_PERMITTED_SOURCE_STATES.get(entitlement, ("public",))),
            )
        ),
    ]


#: The name the 2026-09-18 audit item uses; same object as `asset_visibility_filter`.
visible_asset_predicate = asset_visibility_filter


def location_exact_permitted() -> ColumnElement[bool]:
    """SQL half of the restricted-precision rule (docs/04 D-9; docs/21 §8 `precise_geo`; 2026-09-18
    audit "exact coordinates publish under licences that forbid raw"): a `location` row may be
    *served* as an exact point only when its own licence's `allows_raw_publication` is true. The
    loader never promotes a derived-only source's row to `exact` in the first place, but a licence
    reclassified after load, or a row loaded before the override existed, still carries the
    coordinate -- so the API boundary, not the loader, is where the rule has to hold. Used by
    `placement=` filtering (services/api/app.py) and `nearby-proposals` (services/api/assets.py);
    the Python twin that rewrites an already-loaded row to its region grade is
    `services/api/geo.py::effective_placement`."""
    lic = aliased(Licence)
    return exists(select(lic.id).where(lic.id == Location.licence_id, lic.allows_raw_publication.is_(True)))


def asset_geometry_permitted() -> ColumnElement[bool]:
    """The asset twin of `location_exact_permitted` (2026-09-19 line layer): an asset's stored
    coordinates -- its representative point *and* its line geometry -- are served only when the
    asset's own licence has `allows_raw_publication = true`. Every source in scope today is public
    domain (EIA), so nothing is withheld yet; the clause exists so a registry whose terms allow
    derived data but not raw coordinates (the docs/21 §8 `precise_geo` field class) is gated by
    the same rule proposals already obey, without a per-type decision. Used by the geo indexes
    and both nearby-proposals endpoints in `services/api/assets.py`; the Python twin for an
    already-loaded row is `asset_geometry_visible`."""
    lic = aliased(Licence)
    return exists(select(lic.id).where(lic.id == Asset.licence_id, lic.allows_raw_publication.is_(True)))


def asset_geometry_visible(asset: Asset) -> bool:
    """Python twin of `asset_geometry_permitted` for a loaded `Asset` (detail responses)."""
    return bool(asset.licence.allows_raw_publication)
