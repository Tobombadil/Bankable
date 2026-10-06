"""`301` from a merged record's old id or slug to its visible survivor (US-201 AC3; docs/21 §6.3;
api/openapi.yaml `getProposal` and the `ProposalPublicId` parameter; QA audit 2026-09-30 QA-8).

A merge leaves the absorbed row in place with `merged_into_id` set and its own `public_id` and
`slug` unchanged, so the old id and the old slug both still name it. That row is the slug history
docs/21 §6.3 describes for merges ("`slug_history` gains B's slug pointing at A"), and unmerge
restores it: no separate table is needed for this case. A rename does not regenerate a slug today,
so no other history exists to keep.

The chain is followed to the record that is not merged itself (a survivor can be absorbed later),
and the redirect is given only when that record is visible at the caller's tier. Otherwise the
answer is the same `404` an unknown id gets, so the survivor's id never leaks (docs/21 §8 item 3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from services.api.errors import not_found
from services.db.models import Opportunity, Proposal

#: A chain longer than this is treated as broken rather than followed (merges are rarely chained).
MAX_MERGE_HOPS = 10

_COLLECTION = {Proposal: "proposals", Opportunity: "opportunities"}


def merged_survivor(
    db: Session,
    model: type[Proposal] | type[Opportunity],
    segment: str,
    visibility_filter: Callable[..., list[ColumnElement[bool]]],
    entitlement: str,
) -> Proposal | Opportunity | None:
    """The visible record a merged id or slug now lives on, or None."""
    row: Any = db.scalar(
        select(model).where(
            or_(model.public_id == segment, model.slug == segment), model.merged_into_id.is_not(None)
        )
    )
    for _ in range(MAX_MERGE_HOPS):
        if row is None or row.merged_into_id is None:
            break
        row = db.get(model, row.merged_into_id)
    if row is None or row.merged_into_id is not None:
        return None
    visible = db.scalar(select(model.id).where(model.id == row.id, *visibility_filter(entitlement)))
    return row if visible is not None else None


def merged_redirect_or_404(
    db: Session,
    model: type[Proposal] | type[Opportunity],
    segment: str,
    visibility_filter: Callable[..., list[ColumnElement[bool]]],
    entitlement: str,
    request: Request,
) -> RedirectResponse:
    """For a route that found no visible record under `segment`: `301` to the same path on the
    survivor (query string kept), or raises the route's `404`."""
    survivor = merged_survivor(db, model, segment, visibility_filter, entitlement)
    if survivor is None:
        raise not_found(request.url.path)
    prefix = f"/v1/{_COLLECTION[model]}/"
    path = request.url.path
    rest = path[len(prefix) + len(segment) :] if path.startswith(prefix + segment) else ""
    location = request.url.replace(path=f"{prefix}{survivor.public_id}{rest}")
    return RedirectResponse(str(location), status_code=301)
