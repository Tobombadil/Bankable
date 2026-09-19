"""The alert-side visibility gate (docs/50-audit-2026-09-18.md §3.1: "the events feed and Pro
digest emails bypass the record-level visibility gate and leak names, URLs and transitions of
hidden and restricted records").

`services/api/visibility.py` is the predicate and stays the only place it is defined. What was
missing on this side is composition: an `event` row can pass `event_visibility_filter` (its own
source public, its own licence publishable, its own `published_at` reached) while the *record it
describes* is `unpublished`, `pending_review`, sits under a `restricted`/`unknown` licence, or has
not reached its own `published_at`/`public_at` yet. The digest, the private feed and the webhook
payload all name that record (its `name_canonical`/`title`, its slug URL, its `before`/`after`
transition), so the record-level predicate must hold too — the same conjunction
`services/api/app.py` applies when it lists a record's events.

`event_with_visible_subject_filter(entitlement)` is that conjunction as SQL predicates: the event's
own filter, AND an `EXISTS` on the subject row under the record-level filter for the subject type.
An event whose `subject_type` is neither `proposal` nor `opportunity` (audit rows: `user`,
`account`, `saved_search`, ...) never passes — those were already excluded by the event filter's
source `EXISTS` (they carry no `source_id`), and this makes it explicit rather than incidental.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, and_, exists, or_, select

from services.api.visibility import (
    Entitlement,
    event_visibility_filter,
    opportunity_visibility_filter,
    proposal_visibility_filter,
)
from services.db.models import Event, Opportunity, Proposal


def visible_subject_filter(entitlement: Entitlement, now: dt.datetime | None = None) -> ColumnElement[bool]:
    """`EXISTS (subject row under its record-level filter)`, dispatched on `Event.subject_type`."""
    now = now or dt.datetime.now(dt.UTC)
    proposal_visible = exists(
        select(Proposal.id).where(
            Proposal.id == Event.subject_id, *proposal_visibility_filter(entitlement, now)
        )
    )
    opportunity_visible = exists(
        select(Opportunity.id).where(
            Opportunity.id == Event.subject_id, *opportunity_visibility_filter(entitlement, now)
        )
    )
    return or_(
        and_(Event.subject_type == "proposal", proposal_visible),
        and_(Event.subject_type == "opportunity", opportunity_visible),
    )


def event_with_visible_subject_filter(
    entitlement: Entitlement, now: dt.datetime | None = None
) -> list[ColumnElement[bool]]:
    now = now or dt.datetime.now(dt.UTC)
    return [*event_visibility_filter(entitlement, now), visible_subject_filter(entitlement, now)]


__all__ = ["event_with_visible_subject_filter", "visible_subject_filter"]
