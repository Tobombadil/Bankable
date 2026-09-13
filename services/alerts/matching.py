"""Matches a stored `saved_search.query`/`webhook_endpoint.query` dict (docs/21 §3.15's "filter
definition, not results") against a `Proposal`, `Opportunity` or `Event` row.

**Decision** (services/README.md "Pro tier and alerts"): this does not reuse
`services/api/app.py`'s `_apply_proposal_filters` et al. directly — those build a SQL `WHERE`
clause against a live `Request`, and generalising them to also drive in-Python matching against a
single already-loaded row (what the alert/webhook evaluator needs — "given new change events,
match them against saved searches") would have meant restructuring code the Sprint 2 public API
already ships and tests. This module re-implements the same filter grammar (docs/23 §7) as a
predicate function over one row instead, covering the same named subset of parameters
`services/api/app.py` allows (`services/README.md` open decision 7) plus the `q`/text filters —
duplicated field lists, not duplicated query-building logic. A follow-up that unifies both under
one grammar module is a reasonable next step, not done here to keep this sprint's change surface
to the paths the task names.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from services.db.models import Event, Opportunity, Proposal


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _as_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def proposal_matches_query(proposal: Proposal, query: dict[str, Any]) -> bool:
    if v := query.get("kind"):
        if proposal.kind not in _csv(v):
            return False
    if v := query.get("technology"):
        if proposal.technology not in _csv(v):
            return False
    if v := query.get("lifecycle_state"):
        if proposal.lifecycle_state not in _csv(v):
            return False
    if v := query.get("jurisdiction"):
        if proposal.jurisdiction not in _csv(v):
            return False
    if v := query.get("iso"):
        if proposal.iso not in _csv(v):
            return False
    if v := query.get("source_id"):
        active_sources = {s.source_id for s in proposal.sources if s.active}
        if not active_sources & set(_csv(v)):
            return False
    if (v := query.get("capacity_mw[gte]")) is not None:
        if proposal.capacity_mw is None or float(proposal.capacity_mw) < float(v):
            return False
    if (v := query.get("capacity_mw[lte]")) is not None:
        if proposal.capacity_mw is None or float(proposal.capacity_mw) > float(v):
            return False
    if v := query.get("q"):
        if str(v).lower() not in proposal.name_canonical.lower():
            return False
    return True


def opportunity_matches_query(opportunity: Opportunity, query: dict[str, Any]) -> bool:
    if v := query.get("kind"):
        if opportunity.kind not in _csv(v):
            return False
    if v := query.get("status"):
        if opportunity.status not in _csv(v):
            return False
    if v := query.get("technologies"):
        wanted = set(_csv(v))
        have = set(opportunity.technologies or [])
        if have and not (have & wanted):
            return False
    if v := query.get("jurisdiction"):
        if opportunity.jurisdiction not in _csv(v):
            return False
    if v := query.get("source_id"):
        active_sources = {s.source_id for s in opportunity.sources if s.active}
        if not active_sources & set(_csv(v)):
            return False
    if v := query.get("due_at[from]"):
        due = opportunity.due_at
        threshold = _as_datetime(v)
        if due is None or threshold is None or due < threshold:
            return False
    if v := query.get("due_at[to]"):
        due = opportunity.due_at
        threshold = _as_datetime(v)
        if due is None or threshold is None or due > threshold:
            return False
    if v := query.get("q"):
        if str(v).lower() not in opportunity.title.lower():
            return False
    return True


def event_matches_query(event: Event, query: dict[str, Any]) -> bool:
    if v := query.get("subject_type"):
        if event.subject_type not in _csv(v):
            return False
    if v := query.get("event_type"):
        if event.event_type not in _csv(v):
            return False
    if v := query.get("source_id"):
        if event.source_id not in _csv(v):
            return False
    return True


def matches_query(entity: str, row: Proposal | Opportunity | Event, query: dict[str, Any]) -> bool:
    if entity == "proposal" and isinstance(row, Proposal):
        return proposal_matches_query(row, query)
    if entity == "opportunity" and isinstance(row, Opportunity):
        return opportunity_matches_query(row, query)
    if entity == "event" and isinstance(row, Event):
        return event_matches_query(row, query)
    return False
