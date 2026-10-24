"""The change-event view filters that let a front-page figure name the list it is the total of:
`genuine`, `to_state` and `relevance` on `GET /v1/events` (with `country`, `services/api/country.py`).

**`genuine=true`: the genuine-change rule.** Owner, 2026-10-23: "a source's first load is not news".
The rule is the daily metrics recorder's, `genuine-change-v2` (`services/trends/snapshot.py`
`ACTIVITY_RULE`, `window_events`, `_left_out_reason`; docs/24-trends.md §6), stated here as one SQL
predicate so a list can filter by it: an event counts when a source run wrote it (`run_id` set; not
a match, merge or edit), that run was not released from a data-quality hold (`released_at` null),
and it is not the source's first run here: its `started_at` is after the earliest `started_at` of
the source's runs that promoted output (`status = ok`), said as "some ok run of the source started
earlier", which is the same set. A source with no such run has no baseline, so none of its events
counts. A run's trigger label (schedule, manual, backfill, retry) changes nothing. The predicate is
correlated on each event's run, so its cost follows the events a list reads, not the size of
`source_run` (`genuine_change_clause`). `genuine=false` is the complement: every event the rule
leaves out. The recorder computes
the same verdict in Python over the same columns; `services/api/test_change_rule.py` holds the two
to the same answer, event by event and day by day, on every case the recorder's own tests name, and
fails if the recorder's rule is renamed (`GENUINE_CHANGE_RULE` must equal `ACTIVITY_RULE`). This
module does not import `services.trends` at load: the recorder imports `services.api`, and the
events filter layer (`resource_queries.py`) imports this module, so the import would cycle.

**`to_state`**: the state an event moved its subject to, `after.lifecycle_state`. The diff writes
the new state there for `created`, `status_change` and `withdrawn` events, for a proposal's
lifecycle state and for an opportunity's status alike (`pipeline/diff.py`, whose opportunity view
aliases `status` as `lifecycle_state`, `pipeline/connectors/runner.py::_DIFF_ALIASES`). One shape
names no state: a proposal's `withdrawn` event the loader writes for a register row that left a
source declaring `removal_meaning = "withdrawn"` (`services/ingest/loader.py::_load_one_event`:
`before` holds the last state, `after` is empty; no connector declares it today). Its `to_state` is
`withdrawn`, which is what the event says. Any other event with no state (a capacity change, a
`delisted` departure) has no `to_state` and no value selects it. `to_state_expression` is the SQL,
`event_to_state` its Python twin.

**`relevance=energy`**: events whose subject is an energy-relevant notice, the verdict
`relevance=energy` applies on `GET /v1/opportunities` (`services/api/opportunity_relevance.py`,
over the notice's stored fields), so an event of a proposal never passes it. The notices' ids are
bound as parameters only while few (`opportunity_relevance.ids_in`), so the list never passes a
driver's bind-parameter limit however many notices accumulate.

All three are view filters: the alert matcher (`services/alerts/matching.py`) evaluates none of
them, so a saved search or webhook refuses them (`400 unknown_parameter`), as it does `country`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, aliased

from services.api.errors import validation_error
from services.api.opportunity_relevance import ids_in, is_relevant_row
from services.api.params import csv_param
from services.db.models import LIFECYCLE_STATES, OPPORTUNITY_STATUSES, Event, Opportunity, SourceRun

#: The rule's name as the recorder states it; a new rule gets a new name there and here.
GENUINE_CHANGE_RULE = "genuine-change-v2"
GENUINE_PARAM = "genuine"
TO_STATE_PARAM = "to_state"
RELEVANCE_PARAM = "relevance"
#: The events list's view filters (module docstring); `country` is `country.COUNTRY_FILTERS`.
EVENT_VIEW_FILTERS: frozenset[str] = frozenset({GENUINE_PARAM, TO_STATE_PARAM, RELEVANCE_PARAM})
#: Every state an event can name in `after.lifecycle_state`: the proposal lifecycle and the
#: opportunity status vocabularies (docs/21 §7.1, §7.2).
TO_STATES: tuple[str, ...] = tuple(dict.fromkeys((*LIFECYCLE_STATES, *OPPORTUNITY_STATUSES)))


def genuine_param(request: Request) -> bool | None:
    """`true`, `false` or `None` (absent or empty); anything else is a `400 validation_error`."""
    raw = (request.query_params.get(GENUINE_PARAM) or "").strip().lower()
    if not raw:
        return None
    if raw not in ("true", "false"):
        raise validation_error(GENUINE_PARAM, "genuine must be true or false", request.url.path)
    return raw == "true"


def genuine_change_clause() -> ColumnElement[bool]:
    """True for an event the genuine-change rule counts (module docstring).

    Stated per event, correlated on `Event.run_id`: the event's own run (a primary-key lookup) is not
    a released hold, and its source has a run that promoted output and started strictly earlier
    (`started_at > min(started_at of the ok runs)` said as an `EXISTS`, which stops at the first such
    run on `ix_source_run_source_started`). The cost is a pair of index probes per event the other
    filters leave, whatever the size of `source_run`: the 15-minute price sources add about 100,000
    runs a year, and an `IN` over every counted run (the first form of this clause) read them all on
    every call."""
    run = aliased(SourceRun, name="genuine_run")
    earlier = aliased(SourceRun, name="genuine_earlier")
    promoted_before = (
        select(earlier.id)
        .where(
            earlier.source_id == run.source_id,
            earlier.status == "ok",
            earlier.started_at < run.started_at,
        )
        .exists()
    )
    return select(run.id).where(run.id == Event.run_id, run.released_at.is_(None), promoted_before).exists()


def genuine_filter(value: bool) -> ColumnElement[bool]:
    """`genuine=true` or `genuine=false`: the rule, or exactly its complement (an event with no run
    matches no run, so `NOT EXISTS` puts it in the complement)."""
    clause = genuine_change_clause()
    return clause if value else sa.not_(clause)


def to_state_values(request: Request) -> list[str] | None:
    """`?to_state=built,withdrawn`, each a lifecycle state or opportunity status; `None` for none."""
    raw = request.query_params.get(TO_STATE_PARAM)
    values = csv_param(raw) if raw else None
    if not values:
        return None
    unknown = [v for v in values if v not in TO_STATES]
    if unknown:
        raise validation_error(
            TO_STATE_PARAM,
            f"unknown to_state value(s): {', '.join(unknown)}; a lifecycle state or an opportunity status",
            request.url.path,
        )
    return values


#: The state a proposal's `withdrawn` event that names none moved it to (module docstring).
WITHDRAWN_STATE = "withdrawn"


def event_to_state(subject_type: str, event_type: str, after: Mapping[str, Any] | None) -> str | None:
    """The state an event moved its subject to, as `to_state_expression` reads it in SQL."""
    value = after.get("lifecycle_state") if isinstance(after, Mapping) else None
    if value is not None:
        return str(value)
    if subject_type == "proposal" and event_type == WITHDRAWN_STATE:
        return WITHDRAWN_STATE
    return None


def to_state_expression() -> ColumnElement[str | None]:
    """The event's `after.lifecycle_state` (JSON path: `json_extract` on SQLite, `->>` on Postgres),
    or `withdrawn` for a proposal's `withdrawn` event that names no state."""
    stated = Event.after["lifecycle_state"].as_string()
    named_by_type = sa.case(
        (
            sa.and_(Event.subject_type == "proposal", Event.event_type == WITHDRAWN_STATE),
            sa.literal(WITHDRAWN_STATE),
        ),
        else_=sa.null(),
    )
    expression: ColumnElement[str | None] = func.coalesce(stated, named_by_type)
    return expression


def to_state_clause(values: list[str]) -> ColumnElement[bool]:
    """The state the event moved its subject to (`to_state_expression`) is one of `values`."""
    return to_state_expression().in_(values)


def event_relevance_clause(db: Session, stmt: sa.Select[Any]) -> ColumnElement[bool]:
    """`relevance=energy` on the events `stmt` selects so far: `Event.subject_id IN (...)` over the
    opportunity subjects whose notice is energy-relevant, judged as `relevance=energy` judges them on
    the opportunity list (`opportunity_relevance.is_relevant_row`, stored fields)."""
    subjects = (
        stmt.with_only_columns(Event.subject_id)
        .where(Event.subject_type == "opportunity")
        .order_by(None)
        .distinct()
    )
    rows = db.execute(
        select(
            Opportunity.id,
            Opportunity.title,
            Opportunity.summary,
            Opportunity.technologies,
            Opportunity.identifiers,
        ).where(Opportunity.id.in_(subjects))
    ).all()
    ids = [r.id for r in rows if is_relevant_row(r.title, r.summary, r.technologies, r.identifiers)]
    # Bound as parameters only while few (`ids_in`): as notices accumulate, one parameter per id
    # would pass SQLite's and psycopg's bind limits on a list with no narrow window.
    return sa.and_(Event.subject_type == "opportunity", ids_in(Event.subject_id, ids))


__all__ = [
    "EVENT_VIEW_FILTERS",
    "GENUINE_CHANGE_RULE",
    "GENUINE_PARAM",
    "RELEVANCE_PARAM",
    "TO_STATES",
    "TO_STATE_PARAM",
    "WITHDRAWN_STATE",
    "event_relevance_clause",
    "event_to_state",
    "genuine_change_clause",
    "genuine_filter",
    "genuine_param",
    "to_state_clause",
    "to_state_expression",
    "to_state_values",
]
