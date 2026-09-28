"""NDJSON bulk streams (US-703; docs/23 §3.2 `/v1/bulk/*`, §5 scope `read:bulk`, §6 "20 bulk
requests / hour", §7 "1,000 on `/bulk/*`"; api/openapi.yaml `bulkProposals`, `bulkOpportunities`,
`bulkEvents`).

One page per request, `application/x-ndjson`: the first line is the page's `meta` record
(`BulkMetaLine`: `record_type: "meta"`, `page`, `meta`, `licence_summary`, `redactions`), so a
consumer that stops reading early still holds the credit lines; every following line is one record
in exactly the shape the detail endpoint serialises (`serialize_proposal`, `serialize_opportunity`,
`serialize_event`), provenance included. Paging is the list endpoints' keyset cursor over a sort
that suits a sync: `last_changed` ascending for proposals and opportunities (so `updated_since`
plus `cursor` walks every change once, oldest first) and `seq` ascending for events (`since` is the
resume watermark).

Who may call: an API key carrying `read:bulk` (`require_scope`; a session never carries a key
scope) whose plan resolves to a tier with a bulk allowance in
`services/api/ratelimit.py::PLAN_QUOTAS` -- the API plan today. Each request spends one unit of the
key's own `bulk` bucket (20 an hour) and nothing from its `read` bucket; the headers name the
`bulk` policy.

Licence shape (docs/21 §8's per-shape table, api/openapi.yaml `bulkProposals`): a record is present
only if one of its active source links carries a licence with `allows_api_redistribution`
(`services/api/resource_queries.py`); only such links appear in its `provenance[]` and in the
page's `licence_summary`; a link whose licence has `allows_bulk_export = false` contributes derived
fields only, so its `source_record_id` is nulled and a `licence` redaction says so. A location
whose own licence forbids API redistribution does not travel either. Tier visibility is
`services/api/visibility.py`'s predicate, reached through the same filter functions the list
endpoints use.

The page is read and serialised inside the request, then streamed from memory: the database
session is closed before the body is written, whatever order the framework tears dependencies down
in, and a 1,000-record page is a few megabytes at most.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_scope
from services.api.deps import get_db
from services.api.errors import ProblemError
from services.api.interconnection_points import proposal_point_embeds
from services.api.pagination import paginate
from services.api.params import check_allowed, int_param
from services.api.ratelimit import default_limiter, plan_quota, policy_header
from services.api.resource_queries import (
    Resource,
    lean_load_options,
    resource_model,
    resource_statement,
    subject_infos,
)
from services.api.serialize import (
    build_licence_summary,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_row,
    location_redactions,
    serialize_event,
    serialize_opportunity,
    serialize_proposal,
)
from services.db.models import Event, Opportunity, OpportunitySource, Proposal, ProposalSource

router = APIRouter()
_Link = TypeVar("_Link", ProposalSource, OpportunitySource)

BULK_DEFAULT_LIMIT = 1000
BULK_MAX_LIMIT = 1000
NDJSON = "application/x-ndjson"

#: The query parameters each stream accepts, exactly as api/openapi.yaml lists them; anything else
#: is a `400 unknown_parameter` (docs/04 API-3), as on the list endpoints.
BULK_PARAMS: dict[Resource, set[str]] = {
    "proposal": {"limit", "cursor", "updated_since", "kind", "jurisdiction", "source_id"},
    "opportunity": {"limit", "cursor", "updated_since", "kind", "status", "jurisdiction", "source_id"},
    "event": {"limit", "cursor", "since", "subject_type", "event_type", "source_id"},
}
#: The ascending sort column per stream: oldest change first, so a resumed sync never skips.
BULK_SORT: dict[Resource, str] = {"proposal": "last_changed", "opportunity": "last_changed", "event": "seq"}


def bulk_limit(request: Request) -> int:
    """`BulkLimit`: default 1,000, clamped to 1..1,000 (the list endpoints' `clamp_limit` caps at
    200, which is the wrong ceiling here)."""
    value = int_param(request, "limit")
    if value is None:
        return BULK_DEFAULT_LIMIT
    return max(1, min(value, BULK_MAX_LIMIT))


def _bulk_rate_limit(ctx: AuthContext, instance: str) -> dict[str, str]:
    quota = plan_quota(ctx.entitlement)
    if quota.bulk_requests_per_hour is None or ctx.api_key is None:
        raise ProblemError(
            "forbidden_tier",
            "Bulk streams need the API plan",
            detail="This operation needs an API-plan key with the 'read:bulk' scope.",
            instance=instance,
        )
    result = default_limiter.check(f"bulk:{ctx.api_key.public_id}", limit=quota.bulk_requests_per_hour)
    headers = {
        "RateLimit-Limit": str(result.limit),
        "RateLimit-Remaining": str(result.remaining),
        "RateLimit-Reset": str(result.reset_seconds),
        "RateLimit-Policy": policy_header(ctx.entitlement, result, bucket="bulk"),
    }
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Bulk rate limit exceeded",
            detail=f"More than {result.limit} bulk requests in the current window.",
            instance=instance,
            headers={**headers, "Retry-After": str(result.reset_seconds)},
        )
    return headers


def _redistributable(links: Iterable[_Link]) -> list[_Link]:
    return [s for s in links if s.active and s.source.licence.allows_api_redistribution]


def _record_line(
    record: Proposal | Opportunity, redactions: list[dict[str, Any]], licence_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    links: list[ProposalSource | OpportunitySource] = []
    if isinstance(record, Proposal):
        proposal_links = _redistributable(record.sources)
        data = serialize_proposal(record, sources=proposal_links)
        links.extend(proposal_links)
    else:
        opportunity_links = _redistributable(record.sources)
        data = serialize_opportunity(record, sources=opportunity_links)
        links.extend(opportunity_links)
    for i, link in enumerate(links):
        licence = link.source.licence
        licence_rows.append(licence_summary_row(link.source, licence, link.retrieved_at))
        if not licence.allows_bulk_export and data["provenance"][i]["source_record_id"] is not None:
            data["provenance"][i]["source_record_id"] = None
            redactions.append(
                {
                    "public_id": record.public_id,
                    "field": f"provenance[{i}].source_record_id",
                    "reason": "licence",
                    "source_id": link.source_id,
                    "note": "the source licence does not permit bulk export; derived fields only",
                }
            )
    location = record.location
    if location is not None and not location.licence.allows_api_redistribution:
        data["location"] = None
        redactions.append(
            {
                "public_id": record.public_id,
                "field": "location",
                "reason": "licence",
                "source_id": location.source_id,
                "note": "the location's source licence does not permit API redistribution",
            }
        )
    else:
        redactions.extend(location_redactions(record.public_id, location))
    return data


def _stream(meta_line: dict[str, Any], lines: list[dict[str, Any]]) -> Iterator[bytes]:
    yield (json.dumps(meta_line, separators=(",", ":"), default=str) + "\n").encode()
    for line in lines:
        yield (json.dumps(line, separators=(",", ":"), default=str) + "\n").encode()


def bulk_response(request: Request, db: Session, ctx: AuthContext, resource: Resource) -> StreamingResponse:
    instance = request.url.path
    check_allowed(request, BULK_PARAMS[resource])
    headers = _bulk_rate_limit(ctx, instance)
    limit = bulk_limit(request)
    params = {k: v for k, v in request.query_params.items() if k not in ("limit", "cursor")}
    stmt = resource_statement(
        resource,
        params,
        db=db,
        entitlement=ctx.entitlement,
        redistribution="allows_api_redistribution",
        instance=instance,
        all_opportunity_statuses=True,
    ).options(*lean_load_options(resource, identifiers=True))
    model = resource_model(resource)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(model, BULK_SORT[resource]),
        id_column=model.id,
        ascending=True,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=instance,
    )
    redactions: list[dict[str, Any]] = []
    licence_rows: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    subjects = subject_infos(db, [r for r in rows if isinstance(r, Event)])
    # The detail shape includes the proposal's grid interconnection point (docs/21 §3.24), batched
    # for the page; its register's licence must allow API redistribution, like every other field.
    points = proposal_point_embeds(
        db, [r for r in rows if isinstance(r, Proposal)], ctx.entitlement, redistribution=True
    )
    for row in rows:
        if isinstance(row, Event):
            lines.append(serialize_event(row, **subjects[row.subject_id]))
            if (lic := event_licence_row(row)) is not None:
                licence_rows.append(lic)
        else:
            line = _record_line(row, redactions, licence_rows)
            if isinstance(row, Proposal):
                line["interconnection_point"] = points[row.id]
            lines.append(line)
    meta_line = {
        "record_type": "meta",
        "page": build_page(next_cursor, None, has_more),
        "meta": build_meta(lag_days=0, tier=ctx.entitlement),
        "licence_summary": build_licence_summary(licence_rows),
        "redactions": redactions,
    }
    return StreamingResponse(
        _stream(meta_line, lines),
        media_type=NDJSON,
        headers={**headers, "Cache-Control": "private, no-store"},
    )


@router.get("/v1/bulk/proposals")
def bulk_proposals(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_scope("read:bulk"))],
) -> StreamingResponse:
    return bulk_response(request, db, ctx, "proposal")


@router.get("/v1/bulk/opportunities")
def bulk_opportunities(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_scope("read:bulk"))],
) -> StreamingResponse:
    return bulk_response(request, db, ctx, "opportunity")


@router.get("/v1/bulk/events")
def bulk_events(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_scope("read:bulk"))],
) -> StreamingResponse:
    return bulk_response(request, db, ctx, "event")


__all__ = ["BULK_MAX_LIMIT", "BULK_PARAMS", "bulk_limit", "bulk_response", "router"]
