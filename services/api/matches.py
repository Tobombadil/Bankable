"""Proposal <-> opportunity matches on the API (docs/10 US-401, US-402; docs/23 §3.1, §3.2;
`api/openapi.yaml` `listMatches`, `getMatch`, `dismissMatch`, `undismissMatch`,
`listProposalMatches`, `listOpportunityMatches`). The rows are written by `services/match/run.py`.

**Visibility.** A match is visible to a caller only when *both* sides are: every query here joins
`proposal` and `opportunity` and applies `services/api/visibility.py`'s
`proposal_visibility_filter` and `opportunity_visibility_filter` for the caller's entitlement,
composed, never re-implemented. A match whose other side is hidden (pending review, a gated or
restricted licence, unpublished) is absent -- on the detail routes as well as the Pro list --
and a match id that is not visible is the same `404` an unknown one is (docs/23 §8).

**Tiers.** The two record-scoped lists are public (a detail page shows its matches); the
cross-entity list, the match detail and the dismissal routes are Pro (`require_entitlement("pro")`,
docs/23 §3.2), metered per caller like every other Pro route (`services/api/pro.py`).

**Dismissals** are per user and never global (US-402 AC2; docs/21 §3.11): a `match_dismissal`
row keyed on `(user_id, match_id)`, the `match` row untouched. An API key acts for the user who
created it (`api_key.created_by_user_id`, as `GET /v1/me` does). `GET /v1/matches` hides the
caller's dismissed matches unless `include_dismissed=true`; every Pro+ response carries
`dismissed_by_me`. The record-scoped lists do not hide them (a detail page greys them out), they
only flag them. `api/openapi.yaml` documented `exclude_dismissed` (default `false`) here until
2026-09-26; see services/README.md "Matches" for why the default was flipped.

**Every response** carries the §10 envelope; each side of a match carries its own `provenance`
(one quartet per active source link) and the envelope's `licence_summary` lists the sources of
both sides, so attribution renders for a match exactly as for the records it joins.
`crm_lead_ref` is returned to operator sessions only (the schema's "Admin only").

**Publish gate** (coordinator decision, 2026-09-26): while the rule set's `publish` flag is false
(`data/match_rules.yaml`; `match-rules@v1` measured precision 0.25 against docs/10 US-401 AC3's
0.7), every list route answers every caller but an operator with an empty `data` and a top-level
`matches_withheld` object, and the by-id routes (detail, dismiss, undismiss) answer `404`, the same
as a hidden match. Parameters are validated either way. Operator sessions see everything, and the
lead hand-off (`POST /admin/v1/leads`, services/crm/router.py) never consults the gate.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.orm import Session, selectinload

from services.api.auth import AuthContext, get_auth_context, require_entitlement
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import invalid_cursor, not_found, validation_error
from services.api.pagination import clamp_limit, decode_cursor, paginate
from services.api.params import check_allowed, csv_param, int_param, sort_spec
from services.api.pro import _rate_limit_headers
from services.api.records import _opportunity_technologies_filter
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_row,
    provenance_quartet,
)
from services.api.visibility import (
    gated_opportunity,
    gated_proposal,
    opportunity_visibility_filter,
    proposal_visibility_filter,
)
from services.db.models import (
    MATCH_STATUSES,
    Match,
    MatchDismissal,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
)
from services.ids import parse_public_id, public_id
from services.match.rules import load_rules

router = APIRouter()

MATCH_SORT_ALLOWLIST = {"score", "first_matched_at"}
LIST_MATCHES_PARAMS = {
    "limit",
    "cursor",
    "include",
    "sort",
    "proposal_id",
    "opportunity_id",
    "status",
    "score[gte]",
    "technology",
    "jurisdiction",
    "include_dismissed",
    "updated_since",
}
RECORD_MATCHES_PARAMS = {"limit", "cursor", "status"}
_ADMIN_ROLES = ("operator", "owner")
_PRO_ENTITLEMENTS = ("pro", "api", "admin")


# ------------------------------------------------------------------------------------ caller facts
def caller_user_id(ctx: AuthContext) -> _uuid.UUID | None:
    """The user a dismissal belongs to: the session's user, or the user who created the key."""
    if ctx.user is not None:
        return ctx.user.id
    if ctx.api_key is not None:
        return ctx.api_key.created_by_user_id
    return None


def _is_pro(ctx: AuthContext) -> bool:
    return ctx.is_authenticated and ctx.entitlement in _PRO_ENTITLEMENTS


def _is_operator(ctx: AuthContext) -> bool:
    return ctx.user is not None and ctx.user.role in _ADMIN_ROLES


# -------------------------------------------------------------------------------- publish gate
def matches_published() -> bool:
    """The rule set's own `publish` flag (`data/match_rules.yaml`). A module-level function so the
    tests can flip it without editing the committed file."""
    return load_rules().publish


def _withheld(ctx: AuthContext) -> bool:
    return not matches_published() and not _is_operator(ctx)


def withheld_notice() -> dict[str, Any]:
    """The top-level `matches_withheld` object on a list response while the gate is closed."""
    return {
        "reason": "pending_evaluation",
        "rule_set_version": load_rules().version,
        "detail": (
            "Matches from this rule set are computed but not shown: its measured precision is below "
            "the bar set for publishing them (docs/10 US-401 AC3)."
        ),
    }


# ---------------------------------------------------------------------------------------- queries
def visible_matches(entitlement: str, now: dt.datetime | None = None) -> sa.Select[tuple[Match]]:
    """Every match whose two sides are both visible to `entitlement` (module docstring)."""
    now = now or dt.datetime.now(dt.UTC)
    return (
        select(Match)
        .join(Proposal, Proposal.id == Match.proposal_id)
        .join(Opportunity, Opportunity.id == Match.opportunity_id)
        .where(
            *proposal_visibility_filter(entitlement, now),
            *opportunity_visibility_filter(entitlement, now),
        )
    )


def _dismissed_by(user_id: _uuid.UUID) -> ColumnElement[bool]:
    return exists(
        select(MatchDismissal.id).where(
            MatchDismissal.match_id == Match.id, MatchDismissal.user_id == user_id
        )
    )


def _status_filter(request: Request) -> list[str]:
    values = csv_param(request.query_params.get("status")) or ["active"]
    for value in values:
        if value not in MATCH_STATUSES:
            raise validation_error(
                "status", f"status must be one of {', '.join(MATCH_STATUSES)}", request.url.path
            )
    return values


def _bool_param(request: Request, name: str) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return False
    if raw.lower() in ("true", "1"):
        return True
    if raw.lower() in ("false", "0"):
        return False
    raise validation_error(name, f"{name} must be true or false", request.url.path)


def _float_param(request: Request, name: str, low: float, high: float) -> float | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise validation_error(name, f"{name} must be a number", request.url.path) from exc
    if not low <= value <= high:
        raise validation_error(name, f"{name} must be between {low:g} and {high:g}", request.url.path)
    return value


def _datetime_param(request: Request, name: str) -> dt.datetime | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise validation_error(name, f"{name} must be an ISO 8601 date-time", request.url.path) from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _check_cursor(request: Request) -> str | None:
    """`pagination.paginate` decodes the cursor but binds its tiebreaker straight into a `GUID`
    comparison, where a well-formed base64 cursor with a non-uuid id raises inside the driver.
    Validated here first so a tampered cursor is the documented `400 invalid_cursor`."""
    cursor = request.query_params.get("cursor")
    if cursor:
        decoded = decode_cursor(cursor, request.url.path)
        try:
            _uuid.UUID(decoded.tiebreaker_id)
            if decoded.sort_value is not None:
                if isinstance(decoded.sort_value, str):
                    try:
                        float(decoded.sort_value)
                    except ValueError:
                        dt.datetime.fromisoformat(decoded.sort_value)
        except (ValueError, TypeError) as exc:
            raise invalid_cursor(request.url.path) from exc
    return cursor


def _find_visible_match(db: Session, match_id: str, ctx: AuthContext, instance: str) -> Match:
    internal = parse_public_id("mat", match_id)
    if internal is None or _withheld(ctx):
        raise not_found(instance)
    match = db.scalar(visible_matches(ctx.entitlement).where(Match.id == internal))
    if match is None:
        raise not_found(instance)
    return match


# ------------------------------------------------------------------------------------ serializing
def _quartets(links: list[ProposalSource] | list[OpportunitySource]) -> list[dict[str, Any]]:
    return [
        provenance_quartet(
            link.source, link.source.licence, source_url=link.source_url, retrieved_at=link.retrieved_at
        )
        for link in links
        if link.active
    ]


def serialize_match(
    match: Match,
    proposal: Proposal,
    opportunity: Opportunity,
    *,
    dismissed_by_me: bool | None = None,
    include_lead_ref: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "match_id": public_id("mat", match.id),
        "proposal": {
            "public_id": proposal.public_id,
            "name_canonical": proposal.name_canonical,
            "url": f"{WEB_HOST}/proposals/{proposal.slug}",
            "technology": proposal.technology,
            "jurisdiction": proposal.jurisdiction,
            "capacity_mw": float(proposal.capacity_mw) if proposal.capacity_mw is not None else None,
            "lifecycle_state": proposal.lifecycle_state,
            "provenance": _quartets(proposal.sources),
        },
        "opportunity": {
            "public_id": opportunity.public_id,
            "title": opportunity.title,
            "due_at": iso(opportunity.due_at),
            "url": f"{WEB_HOST}/opportunities/{opportunity.slug}",
            "kind": opportunity.kind,
            "jurisdiction": opportunity.jurisdiction,
            "status": opportunity.status,
            "provenance": _quartets(opportunity.sources),
        },
        "score": float(match.score),
        "rationale": match.rationale or {"rules_passed": [], "rules_failed": []},
        "rationale_text": match.rationale_text,
        "rule_set_version": match.rule_set_version,
        "created_by": match.created_by,
        "status": match.status,
        "first_matched_at": iso(match.first_matched_at),
        "last_evaluated_at": iso(match.last_evaluated_at),
        "removed_at": iso(match.removed_at),
    }
    if dismissed_by_me is not None:
        out["dismissed_by_me"] = dismissed_by_me
    if include_lead_ref:
        out["crm_lead_ref"] = match.crm_lead_ref
    return out


def _licence_rows(records: list[Proposal] | list[Opportunity]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        for link in record.sources:
            if link.active:
                rows.append(licence_summary_row(link.source, link.source.licence, link.retrieved_at))
    return rows


def _render(
    db: Session, matches: list[Match], ctx: AuthContext
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Serialized rows plus licence-summary rows for one page: both sides loaded in two queries
    (sources eagerly), dismissals in one."""
    # Each side is its served view at the caller's tier (`visibility.GatedRecord`): no field and no
    # provenance or licence row from a source that tier may not read (2026-10-06, QA-1).
    proposals = {
        p.id: gated_proposal(p, ctx.entitlement)
        for p in db.scalars(
            select(Proposal)
            .where(Proposal.id.in_([m.proposal_id for m in matches]))
            .options(selectinload(Proposal.sources).selectinload(ProposalSource.source))
        ).unique()
    }
    opportunities = {
        o.id: gated_opportunity(o, ctx.entitlement)
        for o in db.scalars(
            select(Opportunity)
            .where(Opportunity.id.in_([m.opportunity_id for m in matches]))
            .options(selectinload(Opportunity.sources).selectinload(OpportunitySource.source))
        ).unique()
    }
    dismissed: set[_uuid.UUID] | None = None
    user_id = caller_user_id(ctx)
    if _is_pro(ctx) and user_id is not None:
        dismissed = set(
            db.scalars(
                select(MatchDismissal.match_id).where(
                    MatchDismissal.user_id == user_id,
                    MatchDismissal.match_id.in_([m.id for m in matches]),
                )
            )
        )
    lead_ref = _is_operator(ctx)
    data = [
        serialize_match(
            m,
            proposals[m.proposal_id],
            opportunities[m.opportunity_id],
            dismissed_by_me=(m.id in dismissed) if dismissed is not None else None,
            include_lead_ref=lead_ref,
        )
        for m in matches
    ]
    licence_rows = _licence_rows(list(proposals.values())) + _licence_rows(list(opportunities.values()))
    return data, licence_rows


def _page(
    db: Session, request: Request, ctx: AuthContext, stmt: sa.Select[tuple[Match]], *, count: bool = False
) -> dict[str, Any]:
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, MATCH_SORT_ALLOWLIST, "-score")
    cursor = _check_cursor(request)
    if _withheld(ctx):
        # Parameters are still validated above, so a bad request is a 400 whether or not the gate
        # is open; the answer itself is empty and says why.
        meta = build_meta(lag_days=0, tier=ctx.entitlement)
        if count:
            meta["total"], meta["total_is_estimate"] = 0, False
        envelope = build_list_envelope(
            [], meta=meta, licence_summary=build_licence_summary([]), page=build_page(None, None, False)
        )
        envelope["matches_withheld"] = withheld_notice()
        return envelope
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Match, field),
        id_column=Match.id,
        ascending=ascending,
        cursor=cursor,
        limit=limit,
        instance=request.url.path,
    )
    data, licence_rows = _render(db, rows, ctx)
    meta = build_meta(lag_days=0, tier=ctx.entitlement)
    if count:
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        meta["total"] = total
        meta["total_is_estimate"] = False
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


# ------------------------------------------------------------------------------ GET /v1/matches
@router.get("/v1/matches")
def list_matches(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    check_allowed(request, LIST_MATCHES_PARAMS)
    for name, value in _rate_limit_headers(request, ctx).items():
        response.headers[name] = value
    qp = request.query_params
    stmt = visible_matches(ctx.entitlement).where(Match.status.in_(_status_filter(request)))
    if v := qp.get("proposal_id"):
        stmt = stmt.where(Proposal.public_id.in_(csv_param(v)))
    if v := qp.get("opportunity_id"):
        stmt = stmt.where(Opportunity.public_id.in_(csv_param(v)))
    if (score_gte := _float_param(request, "score[gte]", 0.0, 1.0)) is not None:
        stmt = stmt.where(Match.score >= score_gte)
    if v := qp.get("technology"):
        values = csv_param(v)
        stmt = stmt.where(
            sa.or_(
                Proposal.technology.in_(values),
                sa.and_(Opportunity.technologies != [], _opportunity_technologies_filter(db, values)),
            )
        )
    if v := qp.get("jurisdiction"):
        values = csv_param(v)
        stmt = stmt.where(sa.or_(Proposal.jurisdiction.in_(values), Opportunity.jurisdiction.in_(values)))
    if (since := _datetime_param(request, "updated_since")) is not None:
        stmt = stmt.where(Match.last_evaluated_at >= since)
    user_id = caller_user_id(ctx)
    if not _bool_param(request, "include_dismissed") and user_id is not None:
        stmt = stmt.where(~_dismissed_by(user_id))
    count = "count" in (csv_param(qp.get("include")) or [])
    return _page(db, request, ctx, stmt, count=count)


# ---------------------------------------------------------------------- GET /v1/matches/{match_id}
@router.get("/v1/matches/{match_id}")
def get_match(
    match_id: str,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    check_allowed(request, set())
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    match = _find_visible_match(db, match_id, ctx, request.url.path)
    return _detail(db, match, ctx)


def _detail(db: Session, match: Match, ctx: AuthContext) -> dict[str, Any]:
    data, licence_rows = _render(db, [match], ctx)
    return build_envelope(
        data[0],
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary(licence_rows),
    )


# ------------------------------------------------------- POST / DELETE /v1/matches/{match_id}/dismiss
def _require_user(ctx: AuthContext, request: Request) -> _uuid.UUID:
    user_id = caller_user_id(ctx)
    if user_id is None:  # unreachable: require_entitlement("pro") refused an anonymous caller
        raise not_found(request.url.path, "No user context for this credential.")
    return user_id


@router.post("/v1/matches/{match_id}/dismiss")
def dismiss_match(
    match_id: str,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    """Idempotent (`api/openapi.yaml`): a second call leaves the one row and its first
    `dismissed_at`. The global `match` row is never written (US-402 AC2)."""
    check_allowed(request, set())
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    user_id = _require_user(ctx, request)
    match = _find_visible_match(db, match_id, ctx, request.url.path)
    existing = db.scalar(
        select(MatchDismissal).where(MatchDismissal.user_id == user_id, MatchDismissal.match_id == match.id)
    )
    if existing is None:
        db.add(MatchDismissal(user_id=user_id, match_id=match.id))
        db.flush()
    return _detail(db, match, ctx)


@router.delete("/v1/matches/{match_id}/dismiss", status_code=204)
def undismiss_match(
    match_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Response:
    """Idempotent: restoring a match that is not dismissed is still `204`."""
    check_allowed(request, set())
    headers = _rate_limit_headers(request, ctx)
    user_id = _require_user(ctx, request)
    match = _find_visible_match(db, match_id, ctx, request.url.path)
    db.execute(
        sa.delete(MatchDismissal).where(
            MatchDismissal.user_id == user_id, MatchDismissal.match_id == match.id
        )
    )
    db.flush()
    return Response(status_code=204, headers=headers)


# ----------------------------------------------- GET /v1/{proposals,opportunities}/{public_id}/matches
@router.get("/v1/proposals/{public_id}/matches")
def list_proposal_matches(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, RECORD_MATCHES_PARAMS)
    proposal_id = db.scalar(
        select(Proposal.id).where(
            Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement)
        )
    )
    if proposal_id is None:
        raise not_found(request.url.path)
    stmt = visible_matches(ctx.entitlement).where(
        Match.proposal_id == proposal_id, Match.status.in_(_status_filter(request))
    )
    return _page(db, request, ctx, stmt)


@router.get("/v1/opportunities/{public_id}/matches")
def list_opportunity_matches(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, RECORD_MATCHES_PARAMS)
    opportunity_id = db.scalar(
        select(Opportunity.id).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opportunity_id is None:
        raise not_found(request.url.path)
    stmt = visible_matches(ctx.entitlement).where(
        Match.opportunity_id == opportunity_id, Match.status.in_(_status_filter(request))
    )
    return _page(db, request, ctx, stmt)


__all__ = [
    "caller_user_id",
    "matches_published",
    "router",
    "serialize_match",
    "visible_matches",
    "withheld_notice",
]
