"""The energy-relevant view of `GET /v1/opportunities` (docs/16 U-7; decision 2026-10-22).

`relevance=energy` keeps the notices `services/match/relevance.py` calls energy-relevant: the same
verdict, over the same fields, that the matcher's eligibility gate uses, so the RFPs & funding list
and the matcher can never disagree about which notices are about energy. `relevance=all`, or no
parameter, is every notice; the API's default is unchanged, and the web page's default view asks
for `energy` itself (`web/app.py::opportunities_list`).

The verdict is a pure function of text and codes, not a SQL expression, so it is applied as a
clause over ids: the notices that pass every other filter are read (five columns each), judged,
and the statement is narrowed to the relevant ids. Paging and `include=count` then work as for any
other filter. Verdicts are cached on the fields they read, so a repeat request judges nothing anew.
At 1,349 notices (the 2026-10-20 store) the read is a few milliseconds. Up to `BOUND_IDS_MAX` ids
are bound as one expanding parameter; beyond that they are written into the statement as UUID
literals (`ids_in`), so no number of notices can pass a driver's bind-parameter limit (SQLite's
compiled one, 32,766 by default; psycopg's 65,535) (review of lane front-api, 2026-10-23; the events
list's `relevance=energy` uses the same helper).

A view filter like `services/api/records.py::PROPOSAL_VIEW_FILTERS`: the alert matcher does not
evaluate it, so a saved search may not carry it (`OPPORTUNITY_FILTERS` does not list it).
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from services.api.errors import validation_error
from services.db.models import Opportunity
from services.match.relevance import is_energy_relevant, programme_codes

#: The parameter and its values.
RELEVANCE_PARAM = "relevance"
RELEVANCE_VALUES: tuple[str, ...] = ("energy", "all")
#: Filters the opportunity list applies that a saved search or webhook may not carry.
OPPORTUNITY_VIEW_FILTERS = frozenset({RELEVANCE_PARAM})


def relevance_param(request: Request) -> str | None:
    """`energy`, `all` or `None` (absent); anything else is a `400 validation_error`."""
    raw = request.query_params.get(RELEVANCE_PARAM)
    if raw is None or raw == "":
        return None
    value = raw.strip().lower()
    if value not in RELEVANCE_VALUES:
        raise validation_error(
            RELEVANCE_PARAM, f"relevance must be one of {', '.join(RELEVANCE_VALUES)}", request.url.path
        )
    return value


@functools.lru_cache(maxsize=65_536)
def _relevant(
    title: str | None, summary: str | None, technologies: tuple[str, ...], codes: tuple[str, ...]
) -> bool:
    return is_energy_relevant(title=title, summary=summary, technologies=technologies, programme_codes=codes)


def is_relevant_row(title: str | None, summary: str | None, technologies: Any, identifiers: Any) -> bool:
    """The verdict for one notice's stored fields (`services.match.relevance.opportunity_relevance`
    over the five columns the clause reads, so no ORM row is built)."""
    techs = tuple(str(t) for t in (technologies or []) if t)
    codes = programme_codes(identifiers if isinstance(identifiers, dict) else None)
    return _relevant(title, summary, techs, codes)


#: The most ids `ids_in` binds as parameters (module docstring).
BOUND_IDS_MAX = 2_000


def ids_in(column: Any, ids: Sequence[Any]) -> ColumnElement[bool]:
    """`column IN (ids)` for a `GUID` column. Up to `BOUND_IDS_MAX` ids are one expanding parameter;
    more are written as literals, each parsed as a UUID first, so a literal can only ever be a
    hyphenated UUID: the text `GUID` stores on SQLite and a value Postgres casts to `uuid`. (SQLAlchemy's
    own literal rendering writes a `GUID` value without hyphens on SQLite, which matches nothing.)"""
    if len(ids) <= BOUND_IDS_MAX:
        clause: ColumnElement[bool] = column.in_(list(ids))
        return clause
    literals: list[ColumnElement[Any]] = [sa.literal_column(f"'{uuid.UUID(str(value))}'") for value in ids]
    clause = column.in_(literals)
    return clause


def energy_relevant_clause(db: Session, stmt: sa.Select[Any]) -> ColumnElement[bool]:
    """`Opportunity.id IN (...)` over the notices `stmt` selects that are energy-relevant."""
    candidates = stmt.with_only_columns(
        Opportunity.id,
        Opportunity.title,
        Opportunity.summary,
        Opportunity.technologies,
        Opportunity.identifiers,
    ).order_by(None)
    ids = [
        row.id
        for row in db.execute(candidates).all()
        if is_relevant_row(row.title, row.summary, row.technologies, row.identifiers)
    ]
    return ids_in(Opportunity.id, ids)


__all__ = [
    "BOUND_IDS_MAX",
    "OPPORTUNITY_VIEW_FILTERS",
    "RELEVANCE_PARAM",
    "RELEVANCE_VALUES",
    "energy_relevant_clause",
    "ids_in",
    "is_relevant_row",
    "relevance_param",
]
