"""`GET /v1/interconnection-points` and `GET /v1/interconnection-points/{public_id}` (owner
decision 2026-09-28; docs/21 §3.24; docs/25 §1), plus the `interconnection_point` embed on the
proposal detail. The detail's `recent_changes[]` (lane H2, 2026-09-29) is the newest change events
of the point's visible proposals (`recent_point_changes`).

A point is where a project connects to the grid, as one register names it
(`services/ingest/interconnection.py` has the parse and the grouping key). What a reader wants from
it is the queue behind it: how many megawatts are waiting to connect there, in what technologies,
and how much has already withdrawn or been built. Those numbers are **aggregates over proposals**,
and so they obey the proposal predicate:

* **Computed per request, at the caller's tier, over visible proposals only**
  (`proposal_visibility_filter`), never stored. A stored total would carry the capacity of a
  proposal the tier may not see -- unpublished, gated, not yet public, merged away -- and docs/21
  §8 item 4 forbids exactly that ("no aggregate, count ... that includes it").
* **A point with no visible proposal does not exist** on that tier: the list's inner join to the
  aggregate drops it, and the detail answers the same 404 an unknown id gets
  (`interconnection_point_visibility_filter`). Its name alone would say the register holds a
  project there.
* **The point's own register must be visible** (`interconnection_point_source_filter`): the name is
  that register's text, so a PJM-named point stays dark even beside a visible proposal.

Lifecycle buckets are the public list's: *active* is the web list's default view (announced through
under construction, `ACTIVE_LIFECYCLE_STATES`, pinned equal to `web/viewmodels.py` by
`web/test_interconnection_points.py`), *withdrawn* is withdrawn or cancelled, *built* is built,
and *other* is `unknown`. Megawatts are the sum of `capacity_mw` where stated; a proposal with no
capacity counts in its bucket's count and adds nothing to its MW.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import Session, selectinload

from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import invalid_cursor, not_found, validation_error
from services.api.pagination import clamp_limit, coerce_cursor_value, decode_cursor, encode_cursor
from services.api.params import check_allowed, csv_param, int_param
from services.api.records import _proposal_licence_rows, number_filter
from services.api.resource_queries import subject_infos
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_row,
    provenance_quartet,
    serialize_event,
)
from services.api.visibility import (
    event_visibility_filter,
    gated_record,
    gated_views,
    hidden_provenance_clause,
    interconnection_point_source_filter,
    interconnection_point_visibility_filter,
    interconnection_point_visible,
    proposal_visibility_filter,
    provenance_visible,
    source_split,
)
from services.db.models import INTERCONNECTION_POINT_KINDS, Event, InterconnectionPoint, Proposal
from services.ingest.interconnection import withhold_coordinates

router = APIRouter()

#: The public proposal list's default view (`web/viewmodels.py::ACTIVE_PROPOSAL_STATES`, product
#: defect A): announced through under construction. `built` and `unknown` are outside it.
ACTIVE_LIFECYCLE_STATES: tuple[str, ...] = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
)
WITHDRAWN_LIFECYCLE_STATES: tuple[str, ...] = ("withdrawn", "cancelled")
BUILT_LIFECYCLE_STATES: tuple[str, ...] = ("built",)
BUCKETS: tuple[str, ...] = ("active", "withdrawn", "built", "other")

LIST_PARAMS = {"limit", "cursor", "include", "sort", "q", "iso", "jurisdiction", "kind", "min_active_mw"}
SORTS = {"active_mw", "name", "voltage_kv"}
DEFAULT_SORT = "-active_mw"
#: Proposals listed on a point's detail; the totals always cover all of them.
DETAIL_PROPOSAL_CAP = 500
#: "Recent changes at this point" (`recent_point_changes`): the change events of the point's
#: visible proposals that say something about the queue there -- a project entering it (`created`)
#: and the proposal-lifecycle group of docs/21 §7.3 (the loader emits `status_change` and
#: `withdrawn` today; the named lifecycle types are listed so a connector that emits them is shown
#: without a code change). Field-level edits, matching and publication events are not queue news.
RECENT_CHANGE_EVENT_TYPES: tuple[str, ...] = (
    "created",
    "status_change",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "built",
    "withdrawn",
    "cancelled",
)
#: Rows in `recent_changes`, newest observation first.
RECENT_CHANGES_LIMIT = 10


def bucket_of(lifecycle_state: str) -> str:
    if lifecycle_state in ACTIVE_LIFECYCLE_STATES:
        return "active"
    if lifecycle_state in WITHDRAWN_LIFECYCLE_STATES:
        return "withdrawn"
    if lifecycle_state in BUILT_LIFECYCLE_STATES:
        return "built"
    return "other"


@dataclass
class PointTotals:
    """One point's aggregates over the proposals the caller may see."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))
    mw: dict[str, float] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0.0))
    #: technology (or `unknown`) -> `{count, active_count, active_mw}`
    technologies: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, lifecycle_state: str, technology: str | None, n: int, mw: float) -> None:
        bucket = bucket_of(lifecycle_state)
        self.counts[bucket] += n
        self.mw[bucket] += mw
        row = self.technologies.setdefault(
            technology or "unknown", {"count": 0, "active_count": 0, "active_mw": 0.0}
        )
        row["count"] += n
        if bucket == "active":
            row["active_count"] += n
            row["active_mw"] += mw

    def as_dict(self) -> dict[str, Any]:
        by_technology = [
            {
                "technology": tech,
                "count": int(row["count"]),
                "active_count": int(row["active_count"]),
                "active_mw": round(row["active_mw"], 3),
            }
            for tech, row in sorted(
                self.technologies.items(), key=lambda kv: (-kv[1]["active_mw"], -kv[1]["count"], kv[0])
            )
        ]
        return {
            "proposal_count": sum(self.counts.values()),
            "active_count": self.counts["active"],
            "active_mw": round(self.mw["active"], 3),
            "withdrawn_count": self.counts["withdrawn"],
            "withdrawn_mw": round(self.mw["withdrawn"], 3),
            "built_count": self.counts["built"],
            "built_mw": round(self.mw["built"], 3),
            "other_count": self.counts["other"],
            "other_mw": round(self.mw["other"], 3),
            "by_technology": by_technology,
        }


def point_proposal_filter(entitlement: str, now: dt.datetime | None = None) -> list[ColumnElement[bool]]:
    """The proposals every number and list on a point is built from: linked to a point and visible
    at `entitlement` (`proposal_visibility_filter`). The list's aggregate, `point_totals`, the
    detail's `proposals[]` and `recent_changes`, and the proposal embeds all read through this one
    clause list, so a total, a listed row and a change event can never disagree about which
    proposals exist at the tier -- and the M-11 audit (`services/visibility_audit/run.py`) renders
    the same clauses to check them."""
    return [Proposal.interconnection_point_id.is_not(None), *proposal_visibility_filter(entitlement, now)]


def point_totals(
    db: Session, point_ids: list[_uuid.UUID], entitlement: str, now: dt.datetime | None = None
) -> dict[_uuid.UUID, PointTotals]:
    """Totals for each point in `point_ids`, over proposals visible at `entitlement`, summed over
    each proposal's *served* lifecycle, technology and capacity (`visibility.GatedRecord`; docs/21
    §8 item 4: no aggregate that includes a hidden source's value). One column query; a row whose
    stored value for one of those fields came from a source the tier may not read is re-read
    through its served view (rare: only after a source is unpublished). A point with no visible
    proposal is absent from the result."""
    if not point_ids:
        return {}
    hidden = source_split(db, entitlement)[1]
    out: dict[_uuid.UUID, PointTotals] = {}
    visible_at_points = [
        Proposal.interconnection_point_id.in_(point_ids),
        *point_proposal_filter(entitlement, now),
    ]
    if not hidden:
        # Every source readable at this tier: no stored value can be a hidden one, so one grouped
        # query over the stored columns is the served sum.
        grouped_stmt = (
            select(
                Proposal.interconnection_point_id,
                Proposal.lifecycle_state,
                Proposal.technology,
                func.count(),
                func.coalesce(func.sum(Proposal.capacity_mw), 0),
            )
            .where(*visible_at_points)
            .group_by(Proposal.interconnection_point_id, Proposal.lifecycle_state, Proposal.technology)
        )
        for point_id, lifecycle_state, technology, n, mw in db.execute(grouped_stmt).all():
            out.setdefault(point_id, PointTotals()).add(lifecycle_state, technology, int(n), float(mw or 0))
        return out
    stmt = select(
        Proposal.id,
        Proposal.interconnection_point_id,
        Proposal.lifecycle_state,
        Proposal.technology,
        Proposal.capacity_mw,
        hidden_provenance_clause(Proposal, _TOTAL_FIELDS, hidden).label("gate"),
    ).where(*visible_at_points)
    grouped: dict[tuple[_uuid.UUID, str, str | None], list[float]] = {}
    rows = db.execute(stmt).all()
    views = gated_views(db, Proposal, (r[0] for r in rows if r[5]), entitlement)
    for row_id, point_id, lifecycle_state, technology, mw, gated in rows:
        if gated and (view := views.get(row_id)) is not None:
            lifecycle_state, technology, mw = view.lifecycle_state, view.technology, view.capacity_mw
        grouped.setdefault((point_id, lifecycle_state, technology), []).append(float(mw or 0))
    for (point_id, lifecycle_state, technology), mws in grouped.items():
        out.setdefault(point_id, PointTotals()).add(lifecycle_state, technology, len(mws), sum(mws))
    return out


#: The proposal fields a point's totals read (`point_totals`).
_TOTAL_FIELDS = ("lifecycle_state", "technology", "capacity_mw")


def _active_mw_aggregate(entitlement: str) -> sa.Subquery:
    """`(point_id, active_mw)` over visible proposals: the list's sort key and its
    `min_active_mw` filter, and -- through the inner join -- its "at least one visible proposal"
    clause."""
    active = Proposal.lifecycle_state.in_(ACTIVE_LIFECYCLE_STATES)
    return (
        select(
            Proposal.interconnection_point_id.label("point_id"),
            func.round(func.coalesce(func.sum(case((active, Proposal.capacity_mw), else_=0)), 0), 3).label(
                "active_mw"
            ),
        )
        .where(*point_proposal_filter(entitlement))
        .group_by(Proposal.interconnection_point_id)
        .subquery("point_agg")
    )


def listed_points(entitlement: str) -> tuple[sa.Select[Any], sa.Subquery]:
    """The list's row set before its filters, sort and paging: every point whose own register is
    visible (`interconnection_point_source_filter`) joined to its visible-proposal aggregate, whose
    inner join is the "at least one visible proposal" clause. Returns the statement (selecting the
    point and its `active_mw`) and the aggregate. `GET /v1/interconnection-points` narrows it; the
    sitemap walks it through that route; the M-11 audit reads it whole."""
    agg = _active_mw_aggregate(entitlement)
    stmt = (
        select(InterconnectionPoint, agg.c.active_mw)
        .join(agg, agg.c.point_id == InterconnectionPoint.id)
        .where(*interconnection_point_source_filter(entitlement))
    )
    return stmt, agg


def point_url(point: InterconnectionPoint) -> str:
    return f"{WEB_HOST}/interconnection-points/{point.public_id}"


def _substation_asset(point: InterconnectionPoint, entitlement: str) -> dict[str, Any] | None:
    """The substation asset lane G2's crosswalk links, when it has and the asset is visible."""
    asset = point.substation_asset
    if asset is None or not provenance_visible(asset.source, asset.licence, entitlement):
        return None
    return {
        "public_id": asset.public_id,
        "slug": asset.slug,
        "url": f"{WEB_HOST}/assets/{asset.slug}",
        "name": asset.name,
    }


def point_name(point: InterconnectionPoint) -> str:
    """The point's served name: the register's spelling, with any coordinate typed into it
    withheld when the point's licence forbids raw publication (docs/21 §8 `precise_geo`; the
    loader already strips it at link time, and this covers rows linked before that rule)."""
    if point.licence.allows_raw_publication:
        return point.name_display
    return withhold_coordinates(point.name_display)


def serialize_point(point: InterconnectionPoint, totals: PointTotals, entitlement: str) -> dict[str, Any]:
    return {
        "public_id": point.public_id,
        "url": point_url(point),
        "name": point_name(point),
        "iso": point.operator,
        "kind": point.kind,
        "voltage_kv": float(point.voltage_kv) if point.voltage_kv is not None else None,
        "bus_number": point.bus_number,
        "jurisdiction": point.jurisdiction,
        "substation_asset": _substation_asset(point, entitlement),
        "totals": totals.as_dict(),
        "provenance": [
            provenance_quartet(
                point.source, point.licence, source_url=point.source_url, retrieved_at=point.retrieved_at
            )
        ],
    }


def _point_licence_row(point: InterconnectionPoint) -> dict[str, Any]:
    return licence_summary_row(point.source, point.licence, point.retrieved_at)


# ------------------------------------------------------------------------------------------ list
def _sort(request: Request) -> tuple[str, bool]:
    raw = request.query_params.get("sort", DEFAULT_SORT)
    token = raw.split(",")[0]
    ascending = not token.startswith("-")
    name = token[1:] if not ascending else token
    if name not in SORTS:
        raise validation_error(
            "sort", f"sort field {name!r} is not allowlisted for this resource", request.url.path
        )
    return name, ascending


def _keyset(
    sort_expr: ColumnElement[Any], ascending: bool, value: Any, tiebreaker: str
) -> ColumnElement[bool]:
    """`services/api/pagination.py::paginate`'s keyset clause over an expression rather than a
    mapped column (the sort key here is an aggregate). NULLs sort last in both directions."""
    point_id = _uuid.UUID(tiebreaker)
    after_id = InterconnectionPoint.id > point_id if ascending else InterconnectionPoint.id < point_id
    if value is None:
        return sa.and_(sort_expr.is_(None), after_id)
    beyond = sort_expr > value if ascending else sort_expr < value
    return sa.or_(beyond, sort_expr.is_(None), sa.and_(sort_expr == value, after_id))


def _list_statement(request: Request, entitlement: str) -> tuple[sa.Select[Any], ColumnElement[Any]]:
    qp = request.query_params
    stmt, agg = listed_points(entitlement)
    if v := qp.get("iso"):
        stmt = stmt.where(InterconnectionPoint.operator.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(InterconnectionPoint.jurisdiction.in_(csv_param(v)))
    if v := qp.get("kind"):
        kinds = csv_param(v)
        unknown = [k for k in kinds if k not in INTERCONNECTION_POINT_KINDS]
        if unknown:
            raise validation_error(
                "kind", f"kind must be one of {', '.join(INTERCONNECTION_POINT_KINDS)}", request.url.path
            )
        stmt = stmt.where(InterconnectionPoint.kind.in_(kinds))
    if v := qp.get("min_active_mw"):
        stmt = stmt.where(agg.c.active_mw >= number_filter("min_active_mw", v, request.url.path))
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        stmt = stmt.where(
            sa.or_(
                func.lower(InterconnectionPoint.name_display).like(like), InterconnectionPoint.bus_number == v
            )
        )
    field_name, _asc = _sort(request)
    sort_columns: dict[str, ColumnElement[Any]] = {
        "active_mw": agg.c.active_mw,
        "name": InterconnectionPoint.name_display.expression,
        "voltage_kv": InterconnectionPoint.voltage_kv.expression,
    }
    sort_expr = sort_columns[field_name]
    return stmt, sort_expr


def _cursor_value(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return float(value)


@router.get("/v1/interconnection-points")
def list_interconnection_points(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, LIST_PARAMS)
    limit = clamp_limit(int_param(request, "limit"))
    _field, ascending = _sort(request)
    stmt, sort_expr = _list_statement(request, ctx.entitlement)
    include = (request.query_params.get("include") or "").split(",")
    if any(part and part != "count" for part in include):
        raise validation_error("include", "include takes only `count` on this resource", request.url.path)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) if "count" in include else None

    paged = stmt
    if cursor := request.query_params.get("cursor"):
        decoded = decode_cursor(cursor, request.url.path)
        # Typed like `paginate`'s cursors (backend audit 2026-09-30 F9): a string where the sort is
        # a number would be a Postgres type error, not an empty page.
        # `active_mw` is an aggregate with no declared type; it is a number.
        sort_type = sa.Float() if isinstance(sort_expr.type, sa.types.NullType) else sort_expr.type
        value = coerce_cursor_value(decoded.sort_value, sort_type, request.url.path)
        try:
            clause = _keyset(sort_expr, ascending, value, decoded.tiebreaker_id)
        except ValueError as exc:
            raise invalid_cursor(request.url.path) from exc
        paged = paged.where(clause)
    order = (sort_expr.asc() if ascending else sort_expr.desc()).nulls_last()
    tiebreak = InterconnectionPoint.id.asc() if ascending else InterconnectionPoint.id.desc()
    rows = db.execute(
        paged.add_columns(sort_expr.label("sort_value")).order_by(order, tiebreak).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(_cursor_value(last.sort_value), str(last[0].id))

    points = [row[0] for row in rows]
    totals = point_totals(db, [p.id for p in points], ctx.entitlement)
    data = [serialize_point(p, totals.get(p.id, PointTotals()), ctx.entitlement) for p in points]
    meta = build_meta(tier=ctx.entitlement)
    if total is not None:
        meta["total"] = total
        meta["total_is_estimate"] = False
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary([_point_licence_row(p) for p in points]),
        page=build_page(next_cursor, None, has_more),
    )


# ---------------------------------------------------------------------------------------- detail
def _proposal_row(p: Proposal) -> dict[str, Any]:
    return {
        "public_id": p.public_id,
        "slug": p.slug,
        "url": f"{WEB_HOST}/proposals/{p.slug}",
        "name_canonical": p.name_canonical,
        "technology": p.technology,
        "capacity_mw": float(p.capacity_mw) if p.capacity_mw is not None else None,
        "lifecycle_state": p.lifecycle_state,
        "lifecycle_bucket": bucket_of(p.lifecycle_state),
        "jurisdiction": p.jurisdiction,
        "proposed_online_date": iso(p.proposed_online_date),
    }


def recent_point_changes(
    db: Session,
    point_ids: list[_uuid.UUID],
    entitlement: str,
    *,
    limit: int | None = None,
    now: dt.datetime | None = None,
) -> dict[_uuid.UUID, list[Event]]:
    """The "Recent changes at this point" rows: for each of `point_ids`, the newest `limit` (default
    `RECENT_CHANGES_LIMIT`) change events (`RECENT_CHANGE_EVENT_TYPES`) whose subject is a proposal
    at the point that is visible at `entitlement`, newest observation first (`seq` breaks ties).

    No new event type and no new table: these are the proposal's own `event` rows, filtered by the
    same `event_visibility_filter` `GET /v1/events` and `GET /v1/proposals/{id}/events` use (the
    event's own timing, licence and source, and its subject's visibility), and by
    `point_proposal_filter` for the point link -- so an event of an unpublished, gated or
    not-yet-public proposal is never here, and a change event can never name a proposal the point's
    `proposals[]` withholds. A point with no such event maps to `[]`. One query for any number of
    points; one point (the detail route) is capped in SQL."""
    if not point_ids:
        return {}
    limit = RECENT_CHANGES_LIMIT if limit is None else limit
    at_points = select(Proposal.id).where(
        Proposal.interconnection_point_id.in_(point_ids), *point_proposal_filter(entitlement, now)
    )
    stmt = (
        select(Event)
        .where(
            Event.subject_type == "proposal",
            Event.subject_id.in_(at_points),
            Event.event_type.in_(RECENT_CHANGE_EVENT_TYPES),
            *event_visibility_filter(entitlement, now),
        )
        .order_by(Event.observed_at.desc(), Event.seq.desc())
    )
    if len(point_ids) == 1:
        stmt = stmt.limit(limit)
    events = list(db.scalars(stmt).all())
    subject_points: dict[_uuid.UUID, _uuid.UUID] = {}
    if events:
        linked = select(Proposal.id, Proposal.interconnection_point_id).where(
            Proposal.id.in_({e.subject_id for e in events})
        )
        subject_points = {pid: point_id for pid, point_id in db.execute(linked).all() if point_id is not None}
    out: dict[_uuid.UUID, list[Event]] = {pid: [] for pid in point_ids}
    for event in events:
        point_id = subject_points.get(event.subject_id)
        rows = out.get(point_id) if point_id is not None else None
        if rows is not None and len(rows) < limit:
            rows.append(event)
    return out


def serialize_recent_changes(
    db: Session, events: list[Event], entitlement: str = "public"
) -> list[dict[str, Any]]:
    """`recent_changes[]`: each event in the `Event` shape the events endpoints serve (its
    `subject` names and links the proposal, by its served name at `entitlement`; its `provenance`
    is the event's own quartet)."""
    subjects = subject_infos(db, events, entitlement)
    return [serialize_event(e, **subjects[e.subject_id]) for e in events]


def visible_point(db: Session, public_id: str, entitlement: str) -> InterconnectionPoint | None:
    return db.scalar(
        select(InterconnectionPoint).where(
            InterconnectionPoint.public_id == public_id, *interconnection_point_visibility_filter(entitlement)
        )
    )


@router.get("/v1/interconnection-points/{public_id}")
def get_interconnection_point(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, set())
    point = visible_point(db, public_id, ctx.entitlement)
    if point is None:
        raise not_found(request.url.path)
    totals = point_totals(db, [point.id], ctx.entitlement).get(point.id, PointTotals())
    order_bucket = case(
        (Proposal.lifecycle_state.in_(ACTIVE_LIFECYCLE_STATES), 0),
        (Proposal.lifecycle_state.in_(BUILT_LIFECYCLE_STATES), 1),
        (Proposal.lifecycle_state.in_(WITHDRAWN_LIFECYCLE_STATES), 3),
        else_=2,
    )
    proposals = list(
        db.scalars(
            select(Proposal)
            .where(Proposal.interconnection_point_id == point.id, *point_proposal_filter(ctx.entitlement))
            .options(selectinload(Proposal.sources))
            .order_by(order_bucket, Proposal.capacity_mw.desc().nulls_last(), Proposal.public_id)
            .limit(DETAIL_PROPOSAL_CAP + 1)
        ).all()
    )
    truncated = len(proposals) > DETAIL_PROPOSAL_CAP
    proposals = proposals[:DETAIL_PROPOSAL_CAP]
    data = serialize_point(point, totals, ctx.entitlement)
    # Each row is the proposal's served view: no value from a source the tier may not read.
    data["proposals"] = [_proposal_row(gated_record(p, ctx.entitlement)) for p in proposals]
    data["proposals_truncated"] = truncated
    changes = recent_point_changes(db, [point.id], ctx.entitlement)[point.id]
    data["recent_changes"] = serialize_recent_changes(db, changes, ctx.entitlement)
    rows = [
        _point_licence_row(point),
        *_proposal_licence_rows(proposals, ctx.entitlement),
        *[r for e in changes if (r := event_licence_row(e)) is not None],
    ]
    return build_envelope(
        data, meta=build_meta(tier=ctx.entitlement), licence_summary=build_licence_summary(rows)
    )


# ------------------------------------------------------------------------ proposal detail embed
def proposal_point_embeds(
    db: Session, proposals: list[Proposal], entitlement: str, *, redistribution: bool = False
) -> dict[_uuid.UUID, dict[str, Any] | None]:
    """`interconnection_point` for each of `proposals` (keyed by proposal id): the point it
    connects at with its tier's totals, or `None` when it has none or its register is not visible
    at the tier (each proposal is itself visible, which satisfies the point's other clause). Two
    queries for any number of proposals -- the points, then their totals -- so the bulk stream
    carries the detail shape without a query per record. `redistribution` (the bulk stream) also
    drops a point whose own licence does not allow API redistribution, the rule every other field
    of a bulk record follows (`services/api/bulk.py`)."""
    point_ids = {p.interconnection_point_id for p in proposals if p.interconnection_point_id is not None}
    points = (
        {
            pt.id: pt
            for pt in db.scalars(select(InterconnectionPoint).where(InterconnectionPoint.id.in_(point_ids)))
        }
        if point_ids
        else {}
    )
    shown = {
        pid: pt
        for pid, pt in points.items()
        if interconnection_point_visible(pt, entitlement)
        and (not redistribution or pt.licence.allows_api_redistribution)
    }
    totals = point_totals(db, list(shown), entitlement)
    out: dict[_uuid.UUID, dict[str, Any] | None] = {}
    for proposal in proposals:
        point = shown.get(proposal.interconnection_point_id) if proposal.interconnection_point_id else None
        if point is None:
            out[proposal.id] = None
            continue
        t = totals.get(point.id, PointTotals()).as_dict()
        out[proposal.id] = {
            "public_id": point.public_id,
            "url": point_url(point),
            "name": point_name(point),
            "kind": point.kind,
            "voltage_kv": float(point.voltage_kv) if point.voltage_kv is not None else None,
            "active_mw": t["active_mw"],
            "active_count": t["active_count"],
            "proposal_count": t["proposal_count"],
        }
    return out


def proposal_point_embed(db: Session, proposal: Proposal, entitlement: str) -> dict[str, Any] | None:
    """`interconnection_point` on `GET /v1/proposals/{public_id}` (`proposal_point_embeds` for one)."""
    return proposal_point_embeds(db, [proposal], entitlement)[proposal.id]
