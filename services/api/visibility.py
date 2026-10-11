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
- `licence_permits` **is posture-dependent** (owner, 2026-09-25; `docs/26-platform-posture.md`;
  `services/posture.py`): `PUBLISHABLE_REUSE_CLASSES` is computed once, at import, from
  `PLATFORM_POSTURE` — `("open", "attribution")` under `commercial` (the default, and what every
  deployment gets when the variable is unset or unrecognised) and additionally `"noncommercial"`
  under `noncommercial`. It is the same tuple on every tier, so the clause is still byte-identical
  across `public`/`pro`/`api` under either posture. Flipping the posture back to `commercial` and
  restarting makes every `noncommercial` row invisible on every non-admin surface at once, with no
  other change; `tests/test_visibility_predicate.py` executes this module under both values.
- `r.publish_state == 'public'` at the record level is unconditional on tier, matching the
  pseudocode above exactly (not "unless admin", which the module does not implement).

**Field-level provenance (2026-10-06, QA-1).** The record predicate decides whether a record is
served; `GatedRecord` (end of module) decides what each served field says: a value only from a
source the tier may read, raw fields withheld under a derived-only licence, `source_count` and
`min_reuse_class` over the readable links, a placement only from a readable source. Every
serialising surface builds it; admin views do not.

Backward-compatible names `proposal_public_filter`/`opportunity_public_filter`/
`event_public_filter` are kept as the `entitlement="public"` case — `services/api/app.py`'s public
routes are unchanged by this sprint.

**The organisation arm (2026-09-26, migration 0022; docs/40 §6 item 2).** `organization` now
carries `publish_state` with the record vocabulary, and `organization_visibility_filter` is the
predicate's first clause, `r.publish_state = 'public'`, plus (2026-10-06, QA-1 of the 2026-09-30
audit) an evidence clause: the organisation is curated, or has no alias row, or at least one of its
`organization_alias` rows came from a source the tier may read -- so unpublishing a source takes
down an organisation only that source named. An organisation has no source link rows, no
`min_reuse_class` and no `published_at`/`public_at` pair (nothing on it is time-gated); the
evidence clause reads source states per tier as the record arms do, and `now` is accepted for
signature parity only. Every public read of organisations
goes through it or its Python twin `organization_visible`: `GET /v1/organizations` and the
detail/sub-list routes (`visible_organization_or_404`, which answers the same `not_found`
problem an unknown id gets, so existence does not leak), the sponsor/issuer embeds
(`services/api/serialize.py::visible_organization_summary`), the asset owners table, the
`organization=`/`q=` filters that match through an organisation, and the ownership tree in both
directions (`services/api/orgtree.py`). The web pages and sitemaps read only through those routes.

**Edges to a taken-down organisation are dropped, not rendered with the name withheld.** docs/21
§8 item 3 settles the shape for the analogous case (a source row the tier may not see): "the
Sources panel omits the row entirely rather than showing a greyed placeholder, because the
existence of the row is itself a disclosure". A withheld-name edge on an asset's owners table or a
proposal's `sponsor` would say "there is an organisation here you may not see", which is the leak
a takedown exists to close; so `sponsor`/`issuer` become `null`, the `asset_owner` edge is
omitted, the parent link is `null` and the ancestor chain stops below the hidden organisation, and
a hidden subsidiary is absent from `subsidiaries`, the counts and every `scope=` walk. Admin reads
bypass all of this (`GET /admin/v1/organizations/{public_id}`), as docs/21 §5.4's fourth row says.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, event, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, object_session

from services.api.errors import not_found
from services.db.models import (
    NON_PUBLIC_EVENT_TYPES,
    REUSE_CLASSES,
    Asset,
    Event,
    InterconnectionPoint,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Source,
)
from services.posture import platform_posture, publishable_reuse_classes
from services.resolve import survivorship

#: The three non-admin entitlements this predicate distinguishes. Typed as plain `str` at every
#: function boundary below (not a `Literal["public","pro","api"]`) because the real caller-facing
#: type, `services.api.auth.AuthContext.entitlement`, is also `str` — it can legitimately hold
#: `"admin"` too (an operator's own account entitlement), which this module was never meant to
#: special-case (admin reads bypass this predicate entirely, per the module docstring). Any value
#: other than `"pro"`/`"api"` is treated as `"public"` by `_PERMITTED_SOURCE_STATES.get` below, so
#: an unrecognised entitlement never accidentally widens visibility.
Entitlement = str

#: One read of the posture, at import, through the helper `pipeline/connectors/registry.py` also
#: uses (`services/posture.py`); no filter below branches on it. `tests/test_platform_posture.py`
#: pins that this tuple and the registry's `PUBLISHABLE_REUSE` are the same set.
PUBLISHABLE_REUSE_CLASSES = publishable_reuse_classes(platform_posture())
# Drift guard against `services.db.models.REUSE_CLASSES`. Both arcs are covered by
# `tests/test_visibility_predicate.py` (ordinary import; a re-import under a patched vocabulary),
# so this module carries no coverage exclusion and docs/04 E-7's 100% gate measures all of it.
if not set(PUBLISHABLE_REUSE_CLASSES) <= set(REUSE_CLASSES):
    raise RuntimeError("PUBLISHABLE_REUSE_CLASSES has drifted from services.db.models.REUSE_CLASSES")

#: `services.db.models.NON_PUBLIC_EVENT_TYPES` as the ordered tuple an `IN` list binds.
_NON_PUBLIC_EVENT_TYPES: tuple[str, ...] = tuple(sorted(NON_PUBLIC_EVENT_TYPES))

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
            Source.publish_state.in_(permitted_source_states(entitlement)),
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
    added later) is invisible on every non-admin surface -- fail closed, not "Unknown".

    An event whose type is in `NON_PUBLIC_EVENT_TYPES` (`removed_from_source`: a row that left its
    source's file, which is not a withdrawal; docs/51 §2.7 item 1) is invisible on every tier by
    name, beside the NULL `public_at`/`published_at` it is written with, so neither a later backfill
    of those columns nor a takedown reversal can surface it. Every list, detail, feed, alert and
    webhook read goes through this function, so the one clause covers them all."""
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
        Event.event_type.not_in(_NON_PUBLIC_EVENT_TYPES),
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


def asset_event_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    """The event half of what an asset page may print from `event` (lane R1): the event's own
    timing, licence and source clauses, exactly as `event_visibility_filter` states them. The
    subject half is the caller's: the one asset whose page is being built, already resolved through
    `asset_visibility_filter`. `event_visibility_filter` itself stays closed to asset subjects, so
    these rows never reach the global feed, alerts or webhooks."""
    now = now or dt.datetime.now(dt.UTC)
    timing = (
        [Event.public_at.is_not(None), Event.public_at <= now]
        if entitlement == "public"
        else [Event.published_at.is_not(None), Event.published_at <= now]
    )
    return [
        *timing,
        Event.subject_type == "asset",
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


# ------------------------------------------------------------------ interconnection points (0026)
def interconnection_point_source_filter(entitlement: Entitlement = "public") -> list[ColumnElement[bool]]:
    """The point's own provenance clauses (docs/21 §3.24): its source passes `source_permits` at
    `entitlement` and the licence its quartet prints passes `licence_permits` -- the two clauses
    `asset_visibility_filter` applies, because the point's name is that register's text. A point
    named by a gated register (PJM, or a source an operator has not put on the surface) is
    invisible on every non-admin surface even when a proposal at it is visible through another
    source. Aliased so the EXISTS keeps its own FROM whatever the caller joined."""
    lic = aliased(Licence)
    src = aliased(Source)
    return [
        exists(
            select(lic.id).where(
                lic.id == InterconnectionPoint.licence_id, lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES)
            )
        ),
        exists(
            select(src.id).where(
                src.id == InterconnectionPoint.source_id,
                src.publish_state.in_(permitted_source_states(entitlement)),
            )
        ),
    ]


def interconnection_point_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    """A point is visible at `entitlement` when its own source and licence are
    (`interconnection_point_source_filter`) **and** at least one proposal at it is visible at the
    same tier (`proposal_visibility_filter`, correlated on `proposal.interconnection_point_id`).
    The second clause is what keeps a point whose only proposals are gated, unpublished, merged
    away or not yet public off every public surface: its name alone would disclose that the
    register holds a project there (docs/21 §8 item 3: the existence of the row is itself a
    disclosure). Its aggregates are computed over the same visible set, never stored
    (`services/api/interconnection_points.py`)."""
    return [
        *interconnection_point_source_filter(entitlement),
        exists(
            select(Proposal.id).where(
                Proposal.interconnection_point_id == InterconnectionPoint.id,
                *proposal_visibility_filter(entitlement, now),
            )
        ),
    ]


def interconnection_point_visible(point: InterconnectionPoint, entitlement: Entitlement = "public") -> bool:
    """Python twin of `interconnection_point_source_filter` for a loaded point -- the proposal
    detail's `interconnection_point` embed, whose own proposal already satisfies the
    visible-proposal clause. `source_visible` reads the source's licence, and the point's own
    `licence` must be publishable too, since that is the licence its quartet prints."""
    return (
        source_visible(point.source, entitlement) and point.licence.reuse_class in PUBLISHABLE_REUSE_CLASSES
    )


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


def organization_visibility_filter(
    entitlement: Entitlement = "public", now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    """The organisation arm (module docstring): `publish_state = 'public'` **and** evidence the
    tier may see (2026-10-06, QA-1 of the 2026-09-30 audit). An organisation has no source link
    rows of its own; what a source says about it is its `organization_alias` rows, one per
    spelling a register used, each with the `source_id` it came from. Unpublishing a source must
    take down an organisation that source alone named (docs/21 §8 item 3: its existence is itself
    a disclosure of the register's contents), so the row is visible only when one of these holds:

    - it is a curated issuer (`is_curated_issuer`: Infraque's own statement, not a register's);
    - it has no alias row at all (nothing recorded to gate on: hand-made and legacy rows);
    - at least one alias came from a source `entitlement` may read (`source_permits` and
      `licence_permits`, the two clauses `visible_source_link_filter` applies).

    Conservative by construction: a second source that spelled the name exactly as the first
    adds no alias (the `one_alias_per_org` constraint), so an organisation first named by a source
    that is later unpublished is hidden even where another register names it too. No timing
    clause: nothing on an organisation is time-gated; `now` is accepted for signature parity."""
    del now
    return [Organization.publish_state == "public", _organization_evidence(entitlement)]


def _organization_evidence(entitlement: Entitlement) -> ColumnElement[bool]:
    """The organisation arm's evidence clause alone (`organization_visibility_filter`)."""
    alias_src = aliased(Source)
    alias_lic = aliased(Licence)
    any_alias = exists(
        select(OrganizationAlias.id).where(OrganizationAlias.organization_id == Organization.id)
    )
    visible_alias = exists(
        select(OrganizationAlias.id)
        .join(alias_src, alias_src.id == OrganizationAlias.source_id)
        .join(alias_lic, alias_lic.id == alias_src.licence_id)
        .where(
            OrganizationAlias.organization_id == Organization.id,
            alias_src.publish_state.in_(permitted_source_states(entitlement)),
            alias_lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        )
    )
    return or_(Organization.is_curated_issuer.is_(True), visible_alias, ~any_alias)


#: `Session.info` key of the evidence answers `organization_visible` has looked up, per tier.
_ORG_EVIDENCE_CACHE = "visibility.organization_evidence"
_IN_CHUNK = 500


@event.listens_for(Session, "after_flush")
@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _forget_organization_evidence(session: Session, *_args: Any) -> None:
    """Any write, commit or rollback in the session may change the evidence (a source's state, an
    alias): the cached answers go with it. Sessions are per request, so a change another session
    commits is seen by the next request."""
    session.info.pop(_ORG_EVIDENCE_CACHE, None)


def organization_visible(org: Organization, entitlement: Entitlement = "public") -> bool:
    """Python twin of `organization_visibility_filter` for an already-loaded row (the sponsor and
    issuer embeds, the asset owners table, the parent link). The state clause is read off the row;
    the evidence clause is the same SQL, asked of the row's own session, because an
    organisation's aliases are not loaded with it. A row with no session (built in memory and
    never added) has no stored evidence to gate on, so the state clause alone decides.

    One query answers every organisation the session holds at that moment (a list page's sponsors
    arrive together, joined-loaded), and the answers are kept on the session until its next flush,
    commit or rollback: a 200-row page costs one evidence query, not 200."""
    if org.publish_state != "public":
        return False
    session = object_session(org)
    if session is None or org.id is None:
        return True
    cache: dict[Any, bool] = session.info.setdefault(_ORG_EVIDENCE_CACHE, {}).setdefault(entitlement, {})
    if org.id not in cache:
        # Identity keys, not the objects: reading `.id` off an expired row would refresh it.
        held = {key[1][0] for key in session.identity_map.keys() if key[0] is Organization}
        pending = sorted((held - set(cache)) | {org.id}, key=str)
        for start in range(0, len(pending), _IN_CHUNK):
            chunk = pending[start : start + _IN_CHUNK]
            shown = set(
                session.scalars(
                    select(Organization.id).where(
                        Organization.id.in_(chunk), _organization_evidence(entitlement)
                    )
                )
            )
            cache.update({oid: oid in shown for oid in chunk})
    return cache[org.id]


def visible_organization_or_404(db: Session, public_id: str, instance: str) -> Organization:
    """Load the organisation a public route was asked for, or raise the same `not_found` problem
    an unknown id raises. One helper for the detail route and every `/v1/organizations/{id}/...`
    sub-list so a taken-down organisation cannot be told apart from one that never existed
    (docs/21 §8 item 3; `services/api/errors.py::not_found`'s own docstring)."""
    org = db.scalar(
        select(Organization).where(Organization.public_id == public_id, *organization_visibility_filter())
    )
    if org is None:
        raise not_found(instance)
    return org


# ------------------------------------------------------------------------- source link rows
# docs/21 §8 item 3 (2026-09-26, M-11 audit): a record visible through one publishable source
# still carries its link rows to every other source, and the record predicate above only asks that
# *some* link be permitted. The Sources panel (`/v1/{proposals,opportunities}/{id}/sources`), the
# `provenance` array, the `licence_summary` built from those links, the feed item's credited source
# and an asset's `asset_source`/`asset_owner` rows each list links one by one, so each must drop a
# link whose source the tier may not read -- "the Sources panel omits the row entirely rather than
# showing a greyed placeholder, because the existence of the row is itself a disclosure". The two
# clauses are the ones `_has_permitted_source` and `asset_visibility_filter` already apply:
# `source_permits` (the source's `publish_state` against `_PERMITTED_SOURCE_STATES`) and
# `licence_permits` (the licence's class in `PUBLISHABLE_REUSE_CLASSES`, posture-dependent, the
# same on every tier). Admin reads bypass all of it, as everywhere else in this module.
def permitted_source_states(entitlement: Entitlement = "public") -> tuple[str, ...]:
    """The `source.publish_state` values `entitlement` may read (`source_permits`), for a caller
    composing its own SQL over `Source` -- `services/api/records.py`'s licence aggregate."""
    return _PERMITTED_SOURCE_STATES.get(entitlement, ("public",))


def source_visible(source: Source, entitlement: Entitlement = "public") -> bool:
    """Python twin of `source_permits AND licence_permits` for one loaded source."""
    return (
        source.publish_state in permitted_source_states(entitlement)
        and source.licence.reuse_class in PUBLISHABLE_REUSE_CLASSES
    )


def provenance_visible(source: Source, licence: Licence, entitlement: Entitlement = "public") -> bool:
    """A row that carries its own licence beside its source (`asset_source`, `asset_owner`): the
    source must be visible and the row's own licence publishable, since that licence is the one
    the row's provenance quartet prints."""
    return source_visible(source, entitlement) and licence.reuse_class in PUBLISHABLE_REUSE_CLASSES


def visible_source_links(
    links: Iterable[ProposalSource | OpportunitySource], entitlement: Entitlement = "public"
) -> list[Any]:
    """The active `proposal_source`/`opportunity_source` rows `entitlement` may see, in order."""
    return [link for link in links if link.active and source_visible(link.source, entitlement)]


def visible_source_link_filter(
    link_model: type[ProposalSource] | type[OpportunitySource], entitlement: Entitlement = "public"
) -> list[ColumnElement[bool]]:
    """SQL twin of `visible_source_links` over a `proposal_source`/`opportunity_source` row: active,
    and its source passes `source_permits` and `licence_permits` at `entitlement`. For the list
    filters that match *through* a link (`source_id=`, the `q=` source-record-id arm in
    `services/api/records.py`): matching a record through a link the tier may not see would
    confirm the link exists and search its identifying field, the disclosure docs/21 §8 item 3
    forbids. Aliased so the EXISTS keeps its own FROM whatever the caller already joined."""
    src = aliased(Source)
    lic = aliased(Licence)
    return [
        link_model.active.is_(True),
        exists(
            select(src.id)
            .join(lic, lic.id == src.licence_id)
            .where(
                src.id == link_model.source_id,
                src.publish_state.in_(permitted_source_states(entitlement)),
                lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
            )
        ),
    ]


# ------------------------------------------------------------------ field-level provenance gate
# docs/21 §8, the mixed-provenance case (2026-10-06, QA-1 of the 2026-09-30 audit): a record visible
# through one source is published "with ... every field whose `field_provenance` points only at
# [a hidden source] removed from the response". The record predicate above decides whether a
# record is served at all; `GatedRecord` decides what each served field says. One object, built
# at every serialising surface (record list and detail, bulk, CSV export, map features, feeds,
# alert payloads, event subjects, interconnection-point rows), so a field cannot be filtered on
# one surface and leak on another.
#
# Per field `f` of `PROPOSAL_SOURCED_FIELDS` / `OPPORTUNITY_SOURCED_FIELDS`:
#   1. an admin override (`overrides[f]`) is a human decision (docs/21 §6.4) and is served as stored;
#   2. else, when every source `field_provenance[f]` names (`source_ids`, else `source_id`) is one
#      the caller may read (and, for bulk and export, one whose licence permits the shape), the
#      stored value is served;
#   3. else the value is re-derived from the readable links' own `normalised` rows: for a proposal
#      by the same field survivorship rule that chose the stored value from all links
#      (`services/resolve/survivorship.py`, docs/22 §23), over the readable links only; for an
#      opportunity, most recently retrieved first. Either way the value printed is one the
#      credited readable sources actually state;
#   4. else it is withheld (`None`; a required field takes a neutral placeholder).
# A field with no recorded provenance is served as stored only while every active link is
# readable; otherwise it is re-derived as in 3. `field_sources()` names, per served field, the
# sources that supplied it, so a page credits the right register (docs/22 §23.4).
#
# Raw field class (docs/21 §8 "no raw, no `status_raw`" for a derived-only licence; L-4 of the
# legal audit): `status_raw`/`technology_raw` are withheld when the source that supplies the
# served value has `allows_raw_publication = false`. Same rule, same place, every surface.
PROPOSAL_SOURCED_FIELDS: tuple[str, ...] = (
    "kind",
    "name_canonical",
    "technology",
    "technology_raw",
    "capacity_mw",
    "storage_mwh",
    "jurisdiction",
    "iso",
    "lifecycle_state",
    "status_raw",
    "identifiers",
    "proposed_online_date",
)
OPPORTUNITY_SOURCED_FIELDS: tuple[str, ...] = (
    "kind",
    "title",
    "summary",
    "jurisdiction",
    "technologies",
    "capacity_sought_mw",
    "budget_amount",
    "budget_currency",
    "open_at",
    "due_at",
    "status",
    "status_raw",
    "identifiers",
)
#: docs/21 §8 field class **raw** as it appears on a record row.
RAW_RECORD_FIELDS: frozenset[str] = frozenset({"status_raw", "technology_raw"})
_DATE_FIELDS = frozenset({"proposed_online_date", "open_at"})
_DATETIME_FIELDS = frozenset({"due_at"})
#: `identifiers` key whose value is `{source_id: basis}` (`services/ingest/loader.py`).
_SELECT_BASIS_KEY = "select_basis"

LinkOk = Callable[[Source], bool]


def _coerce(field: str, value: Any) -> Any:
    """A non-null `normalised` value back to the column's type: the loader stores dates as ISO
    text."""
    if field in _DATE_FIELDS and isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if field in _DATETIME_FIELDS and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    return value


def _strictest_class(classes: Iterable[str]) -> str | None:
    ranked = [c for c in classes if c in REUSE_CLASSES]
    return max(ranked, key=REUSE_CLASSES.index) if ranked else None


class GatedRecord:
    """A served view of one `Proposal`/`Opportunity` (see the section comment above). Attribute
    access answers the gated value for the sourced fields and for `sources` (the readable active
    links), `source_count` (readable links only, docs/21 §8 item 4), `min_reuse_class` (over the
    readable links), `location` (withheld when its own source is not readable) and `sponsor`/
    `issuer` (withheld when `organization_visible` says so); everything else is the record's own
    attribute. Read-only: nothing writes through it.

    `link_ok` narrows the links further for one shape: bulk passes `allows_api_redistribution`,
    an export `allows_bulk_export`, so a field is printed only from a link whose licence permits
    that shape. `source_count` stays the tier's count."""

    def __init__(
        self, record: Proposal | Opportunity, entitlement: Entitlement, link_ok: LinkOk | None
    ) -> None:
        self._record = record
        self._entitlement = entitlement
        self._link_ok = link_ok
        self._fields = PROPOSAL_SOURCED_FIELDS if isinstance(record, Proposal) else OPPORTUNITY_SOURCED_FIELDS
        active = [link for link in record.sources if link.active]
        tier = [link for link in active if source_visible(link.source, entitlement)]
        allowed = [link for link in tier if link_ok is None or link_ok(link.source)]
        self._active = active
        self._tier_links = tier
        self._allowed_links = allowed
        self._tier_hidden = len(tier) < len(active)
        self._any_hidden = len(allowed) < len(active)
        self._allowed_sources: dict[str, Source] = {link.source_id: link.source for link in allowed}
        self._values: dict[str, Any] = {}
        #: field -> source id whose licence withheld its raw value (for `redactions[]`).
        self._raw_withheld: dict[str, str] = {}
        #: field -> (source ids that supplied the served value, rule name or None).
        self._supplied: dict[str, tuple[list[str], str | None]] = {}
        self._survived: dict[str, survivorship.Pick] | None = None

    # -- plumbing
    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found on the instance or the class (the `_`-prefixed state
        # above never is), so every gated name is answered here and everything else falls through.
        if name in self._fields:
            if name not in self._values:
                self._values[name] = self._field(name)
            return self._values[name]
        if name == "sources":
            return list(self._allowed_links)
        if name == "source_count":
            return len(self._tier_links) if self._tier_hidden else self._record.source_count
        if name == "min_reuse_class":
            if not self._tier_hidden:
                return self._record.min_reuse_class
            return _strictest_class(link.source.licence.reuse_class for link in self._tier_links) or (
                self._record.min_reuse_class
            )
        if name == "location":
            return self._location()
        if name in ("sponsor", "issuer"):
            if name not in self._values:  # one visibility query per view, however often it is read
                org = getattr(self._record, name)
                visible = org is not None and organization_visible(org, self._entitlement)
                self._values[name] = org if visible else None
            return self._values[name]
        return getattr(self._record, name)

    def field_sources(self) -> dict[str, dict[str, Any]]:
        """`{field: {"source_ids": [...], "rule": ...}}` for each sourced field, naming the readable
        sources that supplied the served value (an override reads `{"source_ids": [], "rule":
        "override"}`). A field served with no supplier is left out."""
        out: dict[str, dict[str, Any]] = {}
        for name in self._fields:
            getattr(self, name)
            ids, rule = self._supplied.get(name, ([], None))
            if ids or rule == "override":
                out[name] = {"source_ids": ids, "rule": rule}
        return out

    def raw_withheld(self) -> dict[str, str]:
        """`{field: source_id}` for each raw field withheld under its supplier's licence, after
        every raw field has been read (so a caller can list them in `redactions[]`)."""
        for name in RAW_RECORD_FIELDS & set(self._fields):
            getattr(self, name)
        return dict(self._raw_withheld)

    # -- per-field rule
    def _source_allowed(self, source_id: str) -> Source | None:
        if source_id in self._allowed_sources:
            return self._allowed_sources[source_id]
        session = object_session(self._record)
        source = session.get(Source, source_id) if session is not None else None
        if source is None or not source_visible(source, self._entitlement):
            return None
        if self._link_ok is not None and not self._link_ok(source):
            return None
        return source

    def _field(self, name: str) -> Any:
        stored = getattr(self._record, name)
        if name in (self._record.overrides or {}):
            self._supplied[name] = ([], "override")
            return stored  # rule 1: a human decision is served as made
        prov = (self._record.field_provenance or {}).get(name)
        source_ids = survivorship.provenance_source_ids(prov)
        allowed = [self._source_allowed(sid) for sid in source_ids]
        suppliers: list[Source]
        rule: str | None = None
        if source_ids and all(a is not None for a in allowed):
            value, suppliers = stored, [a for a in allowed if a is not None]  # rule 2
            rule = prov.get("rule") if isinstance(prov, dict) else None
        elif not source_ids and not self._any_hidden:
            value, suppliers = stored, [link.source for link in self._allowed_links]
        else:
            value, suppliers, rule = self._fallback(name)  # rules 3 and 4
        self._supplied[name] = (sorted({s.id for s in suppliers}), rule)
        if name == "identifiers":
            return self._visible_identifiers(value)
        if name in RAW_RECORD_FIELDS and value is not None:
            # No supplier at all (a record with no readable link) cannot show its raw value is
            # allowed, so it is withheld too -- fail closed.
            barred = next((s for s in suppliers if not s.licence.allows_raw_publication), None)
            if barred is not None:
                self._raw_withheld[name] = barred.id
            if barred is not None or not suppliers:
                return None
        return value

    def _fallback(self, name: str) -> tuple[Any, list[Source], str | None]:
        if isinstance(self._record, Proposal):
            return self._survivorship_fallback(name)
        if name != "identifiers":
            ordered = sorted(self._allowed_links, key=lambda link: link.retrieved_at, reverse=True)
            for link in ordered:
                value = (link.normalised or {}).get(name)
                if value is not None:
                    return _coerce(name, value), [link.source], None
        return self._placeholder(name), [], None

    def _survivorship_fallback(self, name: str) -> tuple[Any, list[Source], str | None]:
        """Rule 3 for a proposal: the survivorship rule over the readable links only, so the
        served value is the one the stored rule would choose had the hidden sources never been
        linked (docs/22 §23.3)."""
        if self._survived is None:
            members = [
                survivorship.member_from_link(link)
                for link in self._allowed_links
                if isinstance(link, ProposalSource)
            ]
            current = {f: getattr(self._record, f) for f in survivorship.SURVIVING_FIELDS}
            self._survived = survivorship.survive(members, current)
        pick = self._survived.get(name)
        if pick is None:
            return self._placeholder(name), [], None
        suppliers = [self._allowed_sources[sid] for sid in pick.source_ids if sid in self._allowed_sources]
        if name == "identifiers":
            stored = dict(self._record.identifiers or {})
            kept = {_SELECT_BASIS_KEY: stored[_SELECT_BASIS_KEY]} if _SELECT_BASIS_KEY in stored else {}
            return {**kept, **pick.value}, suppliers, pick.rule
        return survivorship.coerce(name, pick.value), suppliers, pick.rule

    def _placeholder(self, name: str) -> Any:
        """Rule 4: a field no readable source states. Nullable fields are `None`; the record's
        required fields take the neutral value its own loader writes for a row that states
        nothing (`services/ingest/loader.py::_proposal_fields_from_row`)."""
        if name in ("identifiers",):
            return {}
        if name == "technologies":
            return []
        if name in ("name_canonical", "title"):
            return "Untitled"
        if name == "kind":
            return "other" if isinstance(self._record, Proposal) else "program"
        if name in ("lifecycle_state", "status"):
            return "unknown"
        if name == "jurisdiction":
            # The country part only: which country a project is in is not the hidden source's
            # contribution alone (every record carries one), the subdivision may be.
            return str(self._record.jurisdiction or "US").split("-", 1)[0]
        return None

    def _visible_identifiers(self, value: Any) -> dict[str, Any]:
        """`identifiers` minus any `select_basis` entry keyed by a source the caller may not read."""
        ids = dict(value or {})
        basis = ids.get(_SELECT_BASIS_KEY)
        if isinstance(basis, dict):
            kept = {sid: b for sid, b in basis.items() if self._source_allowed(str(sid)) is not None}
            if kept:
                ids[_SELECT_BASIS_KEY] = kept
            else:
                ids.pop(_SELECT_BASIS_KEY, None)
        return ids

    def _location(self) -> Location | None:
        loc = self._record.location
        if loc is None:
            return None
        if not source_visible(loc.source, self._entitlement):
            return None
        if self._link_ok is not None and not self._link_ok(loc.source):
            return None
        return loc


def gated_record(
    record: Proposal | Opportunity, entitlement: Entitlement = "public", link_ok: LinkOk | None = None
) -> Any:
    """The served view of `record` at `entitlement` (`GatedRecord`). Typed `Any` so the
    serialisers keep reading it as the model they were written against; idempotent on a view."""
    if isinstance(record, GatedRecord):
        return record
    return GatedRecord(record, entitlement, link_ok)


def tier_links(record: Any, entitlement: Entitlement = "public") -> list[Any]:
    """The active links of `record` (a model row or its served view) that `entitlement` may read,
    before any shape narrowing (`link_ok`): what `source_count` counts. `services/api/listing.py`
    reads `listed` from these, so bulk lines and list rows agree with the `listed` filter."""
    if isinstance(record, GatedRecord):
        return list(record._tier_links)
    return visible_source_links(record.sources, entitlement)


def gated_proposal(
    record: Proposal, entitlement: Entitlement = "public", link_ok: LinkOk | None = None
) -> Proposal:
    return cast(Proposal, gated_record(record, entitlement, link_ok))


def gated_opportunity(
    record: Opportunity, entitlement: Entitlement = "public", link_ok: LinkOk | None = None
) -> Opportunity:
    return cast(Opportunity, gated_record(record, entitlement, link_ok))


def source_split(db: Session, entitlement: Entitlement = "public") -> tuple[frozenset[str], frozenset[str]]:
    """`(readable, hidden)` source ids at `entitlement`. An empty `hidden` -- every stored source
    on the tier's surface, the normal state -- lets a whole-set surface skip the field gate's
    per-row work: no row can then carry a hidden source's value."""
    readable: set[str] = set()
    hidden: set[str] = set()
    for source in db.scalars(select(Source)).all():
        (readable if source_visible(source, entitlement) else hidden).add(source.id)
    return frozenset(readable), frozenset(hidden)


def hidden_provenance_clause(
    model: type[Proposal] | type[Opportunity], fields: Iterable[str], hidden: Iterable[str]
) -> ColumnElement[bool]:
    """SQL twin of `provenance_outside` for a whole-set surface: true for a row whose
    `field_provenance` names one of `hidden` for any of `fields` -- the only rows whose served value
    can differ from the stored one, which such a surface then reads through `GatedRecord`. JSON
    path access renders as `json_extract` on SQLite and `->` on Postgres."""
    ids = sorted(hidden)
    clauses: list[ColumnElement[bool]] = []
    for f in fields:
        clauses.append(func.coalesce(model.field_provenance[(f, "source_id")].as_string(), "").in_(ids))
        # A value several sources supplied (a summed capacity, a union of identifiers; field
        # survivorship, docs/22 §23) lists them all under `source_ids`, read here as the array's JSON
        # text on both dialects; a quoted id cannot match inside another id.
        listed = func.coalesce(model.field_provenance[(f, "source_ids")].as_string(), "")
        clauses.extend(listed.contains(f'"{sid}"', autoescape=True) for sid in ids)
    return or_(*clauses)


def gated_views(
    db: Session,
    model: type[Proposal] | type[Opportunity],
    ids: Iterable[Any],
    entitlement: Entitlement = "public",
) -> dict[Any, Any]:
    """`{id: GatedRecord}` for `ids`, loaded in one query per 500 with their links, for a whole-set
    surface that must re-read a few hundred rows through their served view (`hidden_provenance_clause`
    picks them) without one query per row."""
    from sqlalchemy.orm import selectinload

    wanted = sorted(set(ids), key=str)
    out: dict[Any, Any] = {}
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        stmt = select(model).where(model.id.in_(chunk)).options(selectinload(model.sources))
        rows = cast("list[Proposal | Opportunity]", list(db.scalars(stmt)))
        out.update({row.id: gated_record(row, entitlement) for row in rows})
    return out
