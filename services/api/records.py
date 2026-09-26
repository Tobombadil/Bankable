"""Public record surface: `v1/proposals` and `v1/opportunities` (list, geo, detail, events,
sources), split out of `services/api/app.py` (docs/42-backend-review-2026-09-26.md, lane L5).

Also carries the filter stack (`_apply_proposal_filters`, `_apply_placement_filter`, `_apply_slip_filter`,
`_opportunity_query_with_filters`, `_proposal_query_with_filters`, `_opportunity_technologies_filter`,
`_proposal_licence_rows`, `_opportunity_licence_rows`, `PROPOSAL_FILTERS`, `OPPORTUNITY_FILTERS`,
`PROPOSAL_SORT_ALLOWLIST`, `OPPORTUNITY_SORT_ALLOWLIST`) that `services/api/app.py`'s own
organisation-scoped routes (`list_organization_proposals`, `list_organization_opportunities`) and
feeds (`feed_proposals`, `feed_opportunities`) also call -- this module never imports
`services.api.app` (it would cycle with `app.py`'s `include_router` on this module's `router`), so
those names live here and `app.py` imports back the ones its own routes still need.

`LIST_COMMON`, `sort_spec` and `int_param` are generic pagination/sort helpers with a shared home
of their own, `services/api/params.py` (fan-in from across `services.api`, no cycle risk); this
module imports them from there rather than owning a copy.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Literal, NamedTuple, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import (
    InstrumentedAttribute,
    Session,
    contains_eager,
    load_only,
    noload,
    selectinload,
)

from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST
from services.api.deps import get_db
from services.api.errors import not_found, validation_error
from services.api.geo import build_geo_feature_collection
from services.api.pagination import clamp_limit, paginate
from services.api.params import LIST_COMMON, check_allowed, csv_param, int_param, sort_spec
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_from_source_aggregates,
    licence_summary_row,
    location_redactions,
    serialize_event,
    serialize_opportunity,
    serialize_proposal,
)
from services.api.slippage import SLIP_BUCKETS, slip_filter
from services.api.slippage import today as slip_today
from services.api.visibility import (
    event_visibility_filter,
    location_exact_permitted,
    opportunity_visibility_filter,
    proposal_visibility_filter,
)
from services.db.models import (
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)

router = APIRouter()


# ------------------------------------------------------------------------------------- proposals
PROPOSAL_FILTERS = {
    "kind",
    "technology",
    "lifecycle_state",
    "jurisdiction",
    "iso",
    "state",
    "source_id",
    "capacity_mw[gte]",
    "capacity_mw[lte]",
    "slug",
    "county_fips",
    "placement",
    # Schedule slippage (services/api/slippage.py, docs/22 §18): derived at read time from
    # `proposed_online_date` against the current date, never stored.
    "slipped",
    "slip_bucket",
}
PROPOSAL_SORT_ALLOWLIST = {"last_changed", "first_seen", "capacity_mw", "name_canonical"}

#: ADR 0008, docs/21 §3.7: the three region-grade precisions a `placement=region` filter expands
#: to, and the `none` grade's own precision value. Mirrors `services/api/geo.py::REGION_PRECISIONS`
#: (not imported from there to avoid this module depending on `geo.py` for a plain tuple it only
#: needs for one `IN` clause; both are asserted equal in `services/api/test_placement.py`).
PLACEMENT_REGION_PRECISIONS = ("county_centroid", "state_centroid", "country_centroid")


def _apply_placement_filter(
    stmt: sa.Select[Any], request: Request, *, default: list[str] | None
) -> sa.Select[Any]:
    """Applied separately from `_apply_proposal_filters` (docstring there) because it must *not*
    run inside `_proposal_geo_totals`: `placement` controls which grades are drawn as map
    features, never `meta.unplaced_count`/`totals.records`/the lifecycle and technology counts,
    which always cover every visible record matching every other filter (docs/23 §3.1, ADR 0008).
    `default` is `None` on `GET /v1/proposals` (every grade returned unless the caller asks
    otherwise) and `["exact", "region"]` on `GET /v1/proposals/geo` (docs/23 §3.1's stated
    default for that endpoint)."""
    v = request.query_params.get("placement")
    grades = csv_param(v) if v else default
    if grades is None:
        return stmt
    unknown = [g for g in grades if g not in ("exact", "region", "none")]
    if unknown:
        raise validation_error(
            "placement", f"unknown placement value(s): {', '.join(unknown)}", request.url.path
        )
    # Placement is judged on the grade a row is *served* at, not the one it is stored at
    # (restricted-precision rule, docs/04 D-9): an `exact` row whose licence forbids raw
    # publication is a `region` row on every non-admin surface (`services/api/geo.py::
    # effective_placement`), so `placement=exact` must not select it and `placement=region`
    # must -- `location_exact_permitted` is the SQL twin of that rule (services/api/visibility.py).
    clauses: list[sa.ColumnElement[bool]] = []
    if "exact" in grades:
        clauses.append(sa.and_(Location.precision == "exact", location_exact_permitted()))
    if "region" in grades:
        clauses.append(Location.precision.in_(PLACEMENT_REGION_PRECISIONS))
        clauses.append(sa.and_(Location.precision == "exact", sa.not_(location_exact_permitted())))
    if "none" in grades:
        clauses.append(Location.precision == "unknown")
    loc_subquery = select(Location.id).where(sa.or_(*clauses))
    return stmt.where(Proposal.location_id.in_(loc_subquery))


def _apply_slip_filter(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    """`?slipped=true|false` and `?slip_bucket=under_1y,1_to_3y,over_3y`.

    Both, rather than one or the other, because the distribution makes them answer different
    questions. Measured on the 2026-09-21 load: 129 of 5,826 dated active proposals are slipped
    (2.2 %) -- rare enough that a boolean is a usable way to find them at all -- but within those
    129 the split is 67 / 48 / 14, and the buckets do not mean the same thing. A NESO "Consents
    Approved" row eight months past its date is a live project with a late connection; the 14 rows
    more than three years past are a different prospect entirely. With only a boolean, a caller
    who wants the 14 has to pull all 129 and re-derive the threshold client-side, which is exactly
    the kind of rule that then disagrees with ours.

    `slipped=false` and a bucket list is the one contradictory pairing, and it is a 400 naming the
    conflict rather than a 200 with an empty page: an empty page reads as "no such records",
    which is a different and wrong answer (docs/04 API-3's rule that a request we cannot honour is
    an error, never a silent no-op).
    """
    qp = request.query_params
    # An empty value means "no filter" for both, as it does for every sibling filter here (the
    # `if v := qp.get(name)` idiom below); a form that submits an unset control must not 400.
    raw_slipped = qp.get("slipped") or None
    raw_buckets = qp.get("slip_bucket")
    if raw_slipped is None and not raw_buckets:
        return stmt
    slipped: bool | None = None
    if raw_slipped is not None:
        if raw_slipped not in ("true", "false"):
            raise validation_error(
                "slipped",
                f"unknown slipped value {raw_slipped!r}; expected true or false",
                request.url.path,
            )
        slipped = raw_slipped == "true"
    buckets = csv_param(raw_buckets) if raw_buckets else None
    if buckets:
        unknown = [b for b in buckets if b not in SLIP_BUCKETS]
        if unknown:
            raise validation_error(
                "slip_bucket",
                f"unknown slip_bucket value(s): {', '.join(unknown)}; "
                f"expected one of {', '.join(SLIP_BUCKETS)}",
                request.url.path,
            )
        if slipped is False:
            raise validation_error(
                "slip_bucket",
                "slip_bucket selects slipped proposals and cannot be combined with slipped=false",
                request.url.path,
            )
    if not buckets and slipped is None:
        # `?slip_bucket=,,` parses to no tokens. Without this, it would fall through to
        # `slip_filter(slipped=None, ...)` and silently mean `slipped=false` -- a filter the
        # caller never asked for. An empty value means "no filter", as everywhere else here.
        return stmt
    return stmt.where(slip_filter(slipped=slipped, buckets=buckets, on=slip_today()))


def _apply_proposal_filters(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    qp = request.query_params
    if v := qp.get("kind"):
        stmt = stmt.where(Proposal.kind.in_(csv_param(v)))
    if v := qp.get("technology"):
        stmt = stmt.where(Proposal.technology.in_(csv_param(v)))
    if v := qp.get("lifecycle_state"):
        stmt = stmt.where(Proposal.lifecycle_state.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Proposal.jurisdiction.in_(csv_param(v)))
    if v := qp.get("iso"):
        stmt = stmt.where(Proposal.iso.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.join(ProposalSource, ProposalSource.proposal_id == Proposal.id).where(
            ProposalSource.source_id.in_(csv_param(v)), ProposalSource.active.is_(True)
        )
    if v := qp.get("capacity_mw[gte]"):
        stmt = stmt.where(Proposal.capacity_mw >= float(v))
    if v := qp.get("capacity_mw[lte]"):
        stmt = stmt.where(Proposal.capacity_mw <= float(v))
    if v := qp.get("slug"):
        stmt = stmt.where(Proposal.slug == v)
    if v := qp.get("county_fips"):
        # A subquery on `Proposal.location_id`, not a `.join(Location, ...)`, because this
        # function runs both before and after `Location` is already joined at some call sites
        # (`_proposal_geo_plottable_query` inner-joins it, `_proposal_geo_totals` outer-joins it)
        # -- a second join to the same table there would be invalid SQL, and a subquery is correct
        # regardless of what the caller already joined.
        loc_subquery = select(Location.id).where(Location.county_fips.in_(csv_param(v)))
        stmt = stmt.where(Proposal.location_id.in_(loc_subquery))
    stmt = _apply_slip_filter(stmt, request)
    if v := qp.get("q"):
        # Substring match over the three things a user actually types (web/templates/base.html
        # promises "name, sponsor, queue ID"): the canonical name, the sponsor organisation's
        # canonical name, and any active source record id (queue position, docket, plant-generator
        # id). Subqueries rather than joins so a proposal with several sources is not repeated.
        like = f"%{v.lower()}%"
        sponsor_ids = select(Organization.id).where(func.lower(Organization.name_canonical).like(like))
        record_hits = select(ProposalSource.proposal_id).where(
            ProposalSource.active.is_(True), func.lower(ProposalSource.source_record_id).like(like)
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Proposal.name_canonical).like(like),
                Proposal.sponsor_org_id.in_(sponsor_ids),
                Proposal.id.in_(record_hits),
            )
        )
    return stmt


def _proposal_query_with_filters(request: Request, entitlement: str = "public") -> sa.Select[tuple[Proposal]]:
    # `Proposal.sources` is a `viewonly` relationship with no eager default (unlike `sponsor`/
    # `location`, both `lazy="joined"`), so a caller that reads `.sources` over a whole result set
    # (`_proposal_licence_rows`) would otherwise issue one query per proposal -- fine at this list
    # endpoint's page size (<=200), unlike the geo endpoint's unpaginated full-viewport set, which
    # uses `_proposal_geo_plottable_query` below instead (services/README.md "Sprint 2 fixes").
    #
    # `entitlement` (Pro tier and alerts, task item 2): "public" reads `public_at` as before;
    # "pro"/"api" read `published_at` instead (services/api/visibility.py
    # `proposal_visibility_filter`) — the same list/detail code path serves every tier, only the
    # predicate changes, per docs/21 §5.4's "one predicate" design.
    stmt = (
        select(Proposal)
        .where(*proposal_visibility_filter(entitlement))
        .options(selectinload(Proposal.sources))
    )
    stmt = _apply_proposal_filters(stmt, request)
    # No default (ADR 0008): every placement grade is returned unless the caller filters
    # explicitly — a `none`-grade proposal (unknown location) belongs in list/search by design
    # (docs/21 §3.7), unlike the map, which defaults to `exact,region`.
    return _apply_placement_filter(stmt, request, default=None)


#: Exactly the `Proposal`/`Location` columns `services/api/geo.py` reads for a placed feature
#: (individual marker or cluster member) plus its identity/join keys. Kept as an explicit list
#: (rather than loading every column) because unused-column hydration -- not the join itself --
#: turned out to be most of the residual cost at the real ~8,200-row placed set: even a bare,
#: no-relationship `select(Proposal)` over every column took ~0.44s of pure ORM row construction;
#: narrowing to these columns cuts that to ~0.12-0.25s (services/README.md "Sprint 2 fixes" has the
#: full before/after). Any column `services/api/geo.py` starts reading later needs adding here too
#: -- SQLAlchemy's default `load_only` behaviour for a column left out is a silent per-row
#: deferred-load query on first access, not an error, so a gap here would quietly reintroduce an
#: N+1 rather than fail loudly; nothing but the endpoint's own timing budget would catch it.
_GEO_PROPOSAL_COLUMNS = (
    Proposal.id,
    Proposal.public_id,
    Proposal.slug,
    Proposal.name_canonical,
    Proposal.kind,
    Proposal.technology,
    Proposal.lifecycle_state,
    Proposal.capacity_mw,
    Proposal.last_changed,
    Proposal.location_id,
)
_GEO_LOCATION_COLUMNS = (
    Location.id,
    Location.geom,
    Location.precision,
    Location.county_name,
    Location.county_fips,
    Location.state_code,
    Location.country,
    Location.licence_id,
)


def _proposal_geo_plottable_query(
    request: Request, entitlement: str = "public"
) -> sa.Select[tuple[Proposal]]:
    """Same filters as `_proposal_query_with_filters`, restricted to proposals that can actually
    be placed on the map (`location` present with a resolved `geom`) and loaded for `GET
    /v1/proposals/geo`'s access pattern: every visible *placeable* proposal (not one page of ~50),
    over only the columns `services/api/geo.py` reads (`_GEO_PROPOSAL_COLUMNS`/
    `_GEO_LOCATION_COLUMNS` above).

    Restricting to placeable rows in SQL (an inner join, `Location.geom.is_not(None)`) rather than
    loading every visible proposal and filtering the ~2,200 unplaced ones out in Python matters at
    the real ~10,400-row proposal set — `_proposal_geo_totals` below still counts every visible
    proposal (placeable or not) for `records`/`lifecycle_state_counts`/`unplaced_count`, just via a
    separate, much cheaper column-only query instead of full ORM hydration.

    `sponsor` (the list endpoint's default, `lazy="joined"` on the model, never read here) is
    dropped entirely; `location`'s own `lazy="joined"` `source` falls back to a per-record lazy
    load instead of an eager join on every row, while `location.licence` *is* joined (two columns
    of a table with one row per source) because `services/api/geo.py::effective_placement` reads
    `allows_raw_publication` for every plotted row to decide whether an `exact` point may be
    served at all (restricted-precision rule; a lazy load there would be an N+1 over the whole
    placed set); `.sources` is not loaded here at all -- `_source_licence_aggregate`
    below computes the licence summary with one SQL aggregate instead, and the individual-feature
    path lazy-loads `.sources` per record (bounded by `SPLIT_THRESHOLD`, so at most a few hundred
    small queries). Together with the visibility indexes (services/db/models.py), this is the
    services/README.md "Sprint 2 fixes" geo timing.
    """
    loc_load = contains_eager(Proposal.location)
    stmt = (
        select(Proposal)
        .join(Location, Location.id == Proposal.location_id)
        .join(Licence, Licence.id == Location.licence_id)
        .where(*proposal_visibility_filter(entitlement), Location.geom.is_not(None))
        .options(
            load_only(*_GEO_PROPOSAL_COLUMNS),
            noload(Proposal.sponsor),
            loc_load.load_only(*_GEO_LOCATION_COLUMNS),
            loc_load.lazyload(Location.source),
            loc_load.contains_eager(Location.licence).load_only(Licence.id, Licence.allows_raw_publication),
        )
    )
    stmt = _apply_proposal_filters(stmt, request)
    # Default `exact,region` (docs/23 §3.1): a caller who never passes `placement` sees exactly
    # the pre-ADR-0008 shape (points/clusters) plus the new region features, never bare `none`
    # rows -- those have no `geom` anyway and are excluded by this query's own join already.
    return _apply_placement_filter(stmt, request, default=["exact", "region"])


def _proposal_geo_totals(
    db: Session, request: Request, entitlement: str = "public"
) -> tuple[int, dict[str, int], dict[str, int], int]:
    """`(records_total, lifecycle_state_counts, technology_counts, unplaced_count)` over every
    visible proposal matching `request`'s filters (not just the placeable subset
    `_proposal_geo_plottable_query` loads) — one column-only query (three plain columns, not full
    `Proposal`/`Location` ORM entities) rather than four separate `COUNT`/`GROUP BY` round trips
    each re-evaluating the visibility predicate, or hydrating every row as an ORM object just to
    tally two of its columns in Python (services/README.md "Sprint 2 fixes").
    """
    stmt = _apply_proposal_filters(
        select(Proposal.lifecycle_state, Proposal.technology, Location.geom)
        .select_from(Proposal)
        .outerjoin(Location, Location.id == Proposal.location_id)
        .where(*proposal_visibility_filter(entitlement)),
        request,
    )
    rows = db.execute(stmt).all()
    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    unplaced = 0
    for lifecycle_state, technology, geom in rows:
        lifecycle_counts[lifecycle_state] += 1
        if technology:
            technology_counts[technology] += 1
        if geom is None:
            unplaced += 1
    return len(rows), dict(lifecycle_counts), dict(technology_counts), unplaced


def _proposal_licence_rows(proposals: list[Proposal]) -> list[dict[str, Any]]:
    rows = []
    for p in proposals:
        for s in p.sources:
            if s.active:
                rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@router.get("/v1/proposals")
def list_proposals(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, LIST_COMMON | PROPOSAL_FILTERS)
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, PROPOSAL_SORT_ALLOWLIST, "-last_changed")
    stmt = _proposal_query_with_filters(request, ctx.entitlement)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Proposal, field),
        id_column=Proposal.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_proposal(p) for p in rows]
    meta = build_meta("proposal", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(
                _proposal_query_with_filters(request, ctx.entitlement).subquery()
            )
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )
    return env


def _source_licence_aggregate(
    db: Session,
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk_column: InstrumentedAttribute[Any],
    id_subquery: sa.Select[Any],
) -> dict[str, Any]:
    """`licence_summary` for a whole (unpaginated) result set via one `GROUP BY source_id`
    aggregate query, instead of materialising every visible `proposal_source`/`opportunity_source`
    ORM row just to fold them in Python (`_proposal_licence_rows` — fine at a list page's size, the
    dominant remaining cost at the geo endpoints' full-viewport scale after the visibility-index
    fix, services/README.md "Sprint 2 fixes")."""
    agg_stmt = (
        select(
            Source.id,
            Source.name,
            Source.operator,
            Licence.id,
            Licence.name,
            Licence.url,
            Licence.reuse_class,
            func.coalesce(Source.attribution_text, Licence.attribution_text),
            Licence.requires_link_back,
            func.max(link_model.retrieved_at),
            func.count(),
        )
        .select_from(link_model)
        .join(Source, Source.id == link_model.source_id)
        .join(Licence, Licence.id == Source.licence_id)
        .where(fk_column.in_(id_subquery), link_model.active.is_(True))
        .group_by(Source.id, Licence.id)
    )
    rows = [tuple(row) for row in db.execute(agg_stmt).all()]
    return licence_summary_from_source_aggregates(rows)  # type: ignore[arg-type]


@router.get("/v1/proposals/geo")
def get_proposals_geo(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | PROPOSAL_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc
    stmt = _proposal_geo_plottable_query(request, ctx.entitlement)
    plottable = list(db.scalars(stmt).all())
    records_total, lifecycle_counts, technology_counts, unplaced_count = _proposal_geo_totals(
        db, request, ctx.entitlement
    )
    fc = build_geo_feature_collection(
        plottable,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        lifecycle_state_counts=lifecycle_counts,
        technology_counts=technology_counts,
    )
    meta = build_meta("proposal", tier=ctx.entitlement, extra={"unplaced_count": unplaced_count})
    id_subquery = _apply_proposal_filters(
        select(Proposal.id).where(*proposal_visibility_filter(ctx.entitlement)), request
    )
    # Same default as the plottable query, so the licence summary credits exactly the sources
    # behind what is actually drawn.
    id_subquery = _apply_placement_filter(id_subquery, request, default=["exact", "region"])
    licence_summary = _source_licence_aggregate(db, ProposalSource, ProposalSource.proposal_id, id_subquery)
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)


@router.get("/v1/proposals/{public_id}")
def get_proposal(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        raise not_found(request.url.path)
    data = serialize_proposal(prop)
    meta = build_meta("proposal", tier=ctx.entitlement)
    return build_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows([prop])),
        redactions=location_redactions(prop.public_id, prop.location),
    )


@router.get("/v1/proposals/{public_id}/sources")
def list_proposal_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in prop.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("proposal", tier=ctx.entitlement)
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ----------------------------------------------------------------------------------- opportunities
OPPORTUNITY_FILTERS = {
    "kind",
    "status",
    "technologies",
    "jurisdiction",
    "source_id",
    "due_at[from]",
    "due_at[to]",
    "slug",
}
OPPORTUNITY_SORT_ALLOWLIST = {"due_at", "open_at", "last_changed", "budget_amount"}


def _opportunity_technologies_filter(db: Session, values: list[str]) -> ColumnElement[bool]:
    """Any-of over `Opportunity.technologies[]`; an all-source opportunity (empty array) matches
    every value (api/openapi.yaml `Technologies` parameter). `technologies` is a real Postgres
    `ARRAY(Text)` in the canonical migration but a JSON-encoded `TEXT` column on SQLite
    (`services/db/types.py TextArray`, this sprint's test target — services/README.md), so the
    membership test is dialect-specific: Postgres uses the native `&&` overlap operator; SQLite
    matches the JSON-quoted token as a substring, which is exact for the closed technology
    vocabulary (docs/21 §7 — no value contains a quote or backslash). The Postgres branch is
    written to spec but not exercised here, same caveat as every other dialect-specific path in
    this service.
    """
    empty = Opportunity.technologies == []
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    overlap: ColumnElement[bool]
    if dialect == "postgresql":
        overlap = Opportunity.technologies.op("&&")(list(values))
    else:
        # `.like()` on a `TextArray` column would otherwise bind the pattern *through the
        # column's own type* (`services/db/types.py` JSON-encodes it, turning `%"wind"%` into a
        # JSON array of one character per list element) rather than as a plain string -- casting
        # to `Text` first makes the right-hand literal an ordinary string bind again.
        technologies_text = sa.cast(Opportunity.technologies, sa.Text)
        overlap = sa.or_(*(technologies_text.like(f'%"{t}"%') for t in values))
    return sa.or_(empty, overlap)


def _opportunity_query_with_filters(
    request: Request, db: Session, entitlement: str = "public"
) -> sa.Select[tuple[Opportunity]]:
    # Same N+1 fix as `_proposal_query_with_filters` above, for `Opportunity.sources`; same
    # entitlement wiring too (services/api/visibility.py `opportunity_visibility_filter`).
    stmt = (
        select(Opportunity)
        .where(*opportunity_visibility_filter(entitlement))
        .options(selectinload(Opportunity.sources))
    )
    qp = request.query_params
    status = csv_param(qp.get("status")) or ["open"]
    stmt = stmt.where(Opportunity.status.in_(status))
    if v := qp.get("kind"):
        stmt = stmt.where(Opportunity.kind.in_(csv_param(v)))
    if v := qp.get("technologies"):
        stmt = stmt.where(_opportunity_technologies_filter(db, csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Opportunity.jurisdiction.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.join(OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id).where(
            OpportunitySource.source_id.in_(csv_param(v)), OpportunitySource.active.is_(True)
        )
    if v := qp.get("due_at[from]"):
        stmt = stmt.where(Opportunity.due_at >= dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    if v := qp.get("due_at[to]"):
        stmt = stmt.where(Opportunity.due_at <= dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    if v := qp.get("slug"):
        stmt = stmt.where(Opportunity.slug == v)
    if v := qp.get("q"):
        # Same contract as proposals: title, issuer organisation name, or an active source record id.
        like = f"%{v.lower()}%"
        issuer_ids = select(Organization.id).where(func.lower(Organization.name_canonical).like(like))
        record_hits = select(OpportunitySource.opportunity_id).where(
            OpportunitySource.active.is_(True), func.lower(OpportunitySource.source_record_id).like(like)
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Opportunity.title).like(like),
                Opportunity.issuer_org_id.in_(issuer_ids),
                Opportunity.id.in_(record_hits),
            )
        )
    return stmt


def _opportunity_licence_rows(items: list[Opportunity]) -> list[dict[str, Any]]:
    rows = []
    for o in items:
        for s in o.sources:
            if s.active:
                rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@router.get("/v1/opportunities")
def list_opportunities(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, LIST_COMMON | OPPORTUNITY_FILTERS)
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Opportunity, field),
        id_column=Opportunity.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_opportunity(o) for o in rows]
    meta = build_meta("opportunity", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(
                _opportunity_query_with_filters(request, db, ctx.entitlement).subquery()
            )
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/v1/opportunities/geo")
def get_opportunities_geo(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | OPPORTUNITY_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
    items = list(db.scalars(stmt).all())
    # `services/ingest/loader.py` never geocodes opportunities (no state/county columns in
    # `pipeline.connectors.base.OPPORTUNITY_COLUMNS` to geocode from -- unlike proposals) so
    # `location` is always null here; every visible opportunity is unplaced by construction, never
    # dropped (docs/04 D-8). This endpoint predates that gap being understood and previously
    # crashed on first access to `Proposal`-only attributes (`lifecycle_state`, `technology`) that
    # `Opportunity` doesn't have, the moment any opportunity matched the filters -- untested and
    # unnoticed because nothing had populated `Opportunity.location_id` yet. Fixed here to the
    # extent this sprint's scope covers (proposals' geo endpoint, task priority): a correct,
    # honestly-empty response rather than a 500. Full parity with the proposal side (a real
    # `status_counts`/`kind_counts` aggregate, `technologies[]` clustering) is a follow-up once
    # opportunities have geometry to place at all.
    technology_counts: dict[str, int] = defaultdict(int)
    for o in items:
        for t in o.technologies or []:
            technology_counts[t] += 1
    fc = build_geo_feature_collection(
        [],
        bbox=bbox,
        zoom=zoom,
        records_total=len(items),
        lifecycle_state_counts={},
        technology_counts=dict(technology_counts),
    )
    unplaced_count = len(items)
    meta = build_meta("opportunity", tier=ctx.entitlement, extra={"unplaced_count": unplaced_count})
    return build_envelope(
        fc, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows(items))
    )


@router.get("/v1/opportunities/{public_id}")
def get_opportunity(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        raise not_found(request.url.path)
    data = serialize_opportunity(opp)
    meta = build_meta("opportunity", tier=ctx.entitlement)
    return build_envelope(
        data, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows([opp]))
    )


@router.get("/v1/opportunities/{public_id}/sources")
def list_opportunity_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in opp.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("opportunity", tier=ctx.entitlement)
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ------------------------------------------------------------------------------ subject events
# `list_proposal_events` and `list_opportunity_events` were 15-statement mirrors of each other
# (docs/42-backend-review-2026-09-26.md §2 item 4); `_SubjectEventsConfig` carries exactly what
# differs between the two (the model, its visibility predicate, the event `subject_type` string,
# the URL segment and how to read a display name off the row) so `_list_subject_events` has one
# body and two call sites -- the two thin routes below.
class _SubjectEventsConfig(NamedTuple):
    subject_type: Literal["proposal", "opportunity"]
    model: type[Proposal] | type[Opportunity]
    visibility_filter: Callable[[str], Any]
    url_segment: str
    name_of: Callable[[Any], str]


_PROPOSAL_EVENTS_CONFIG = _SubjectEventsConfig(
    subject_type="proposal",
    model=Proposal,
    visibility_filter=proposal_visibility_filter,
    url_segment="proposals",
    name_of=lambda p: p.name_canonical,
)
_OPPORTUNITY_EVENTS_CONFIG = _SubjectEventsConfig(
    subject_type="opportunity",
    model=Opportunity,
    visibility_filter=opportunity_visibility_filter,
    url_segment="opportunities",
    name_of=lambda o: o.title,
)


def _list_subject_events(
    config: _SubjectEventsConfig,
    public_id: str,
    request: Request,
    db: Session,
    ctx: AuthContext,
) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    subject_row = db.scalar(
        select(config.model).where(
            config.model.public_id == public_id, *config.visibility_filter(ctx.entitlement)
        )
    )
    if subject_row is None:
        raise not_found(request.url.path)
    # `config.model` is a `type[Proposal] | type[Opportunity]` union, so SQLAlchemy's overloads
    # cannot narrow `db.scalar(select(config.model)...)` past the declarative base -- both members
    # share every attribute this function reads (`id`, `public_id`, `slug`), so this is a type-only
    # cast, not a runtime check.
    subject = cast("Proposal | Opportunity", subject_row)
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == config.subject_type,
        Event.subject_id == subject.id,
        *event_visibility_filter(ctx.entitlement),
    )
    if v := request.query_params.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Event, field),
        id_column=Event.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [
        serialize_event(
            e,
            subject_public_id=subject.public_id,
            subject_name=config.name_of(subject),
            subject_url=f"{WEB_HOST}/{config.url_segment}/{subject.slug}",
        )
        for e in rows
    ]
    meta = build_meta(config.subject_type, tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/v1/proposals/{public_id}/events")
def list_proposal_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    return _list_subject_events(_PROPOSAL_EVENTS_CONFIG, public_id, request, db, ctx)


@router.get("/v1/opportunities/{public_id}/events")
def list_opportunity_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    return _list_subject_events(_OPPORTUNITY_EVENTS_CONFIG, public_id, request, db, ctx)


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)
