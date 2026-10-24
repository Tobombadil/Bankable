"""`country=`: records by the country part of their jurisdiction (owner, 2026-10-23: "our primary
focus should be the USA", with Great Britain, EU and World Bank coverage kept and reachable).

**What a record's country is.** Every proposal and opportunity stores one `jurisdiction`
(docs/21 §3.1, §3.3, NOT NULL): a US record is `US` or `US-<state>` (`services/ingest/loader.py::
_jurisdiction` builds `US-TX` from a row's state, or keeps the source's own jurisdiction, such as
`US` or `US-OCS`); a Great Britain record is `GB`; a TED notice is its buyer's ISO 3166-1 alpha-2
country (`DE`, `FR`, ...) or `EU` when TED names none; a World Bank notice is its project country
or `global` (`pipeline/connectors/eu_ted_api`, `mdb_worldbank_procnotices`). The country is the
part before the first `-`, upper-cased, when it is two letters: `US-TX` is `US`, `GB` is `GB`,
`EU` is `EU` (an exceptionally reserved ISO 3166-1 code, kept so TED's own fallback is reachable),
and `global` or a compound such as `US+CA` has none, so no `country=` value selects it.

**One rule, two forms.** `country_clause` is the SQL the list, map, count and event filters apply;
`country_of` is its Python twin for the figures that bucket served rows (`services/api/front.py`).
Both compare upper-cased text, so SQLite (whose `LIKE` ignores ASCII case) and Postgres (whose
`LIKE` does not) select the same rows; `services/api/test_country_filter.py` holds the two to the
same answer on every spelling in the store and a set of awkward ones.

**A view filter.** The alert matcher (`services/alerts/matching.py`) does not evaluate it, so a
saved search or webhook may not carry it (`400 unknown_parameter`), as `listed` and `relevance`;
exports and the CSV twins run the same filter functions and take it.
"""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ColumnElement, func, select

from services.api.errors import validation_error
from services.api.params import csv_param
from services.db.models import Event, Opportunity, Proposal

COUNTRY_PARAM = "country"
#: The list filters this module adds, kept out of `records.PROPOSAL_FILTERS` and
#: `OPPORTUNITY_FILTERS` (which a saved search may carry) and out of `PROPOSAL_VIEW_FILTERS` (which
#: the site's passthrough is pinned to, `web/test_list_filter_passthrough.py`).
COUNTRY_FILTERS: frozenset[str] = frozenset({COUNTRY_PARAM})
_CODE = re.compile(r"^[A-Za-z]{2}$")


def country_values(raw: str | None, instance: str) -> list[str] | None:
    """`?country=US,GB` as upper-case ISO 3166-1 alpha-2 codes, `None` for no filter (absent or
    empty, as every sibling filter reads an empty value). Anything else is a `400
    validation_error` naming `country`; codes are letters only, so none can act as a `LIKE`
    wildcard."""
    values = csv_param(raw) if raw else None
    if not values:
        return None
    bad = [v for v in values if not _CODE.fullmatch(v)]
    if bad:
        raise validation_error(
            COUNTRY_PARAM,
            f"country takes ISO 3166-1 alpha-2 codes such as US or GB (got {', '.join(bad)})",
            instance,
        )
    return sorted({v.upper() for v in values})


def country_clause(column: Any, codes: list[str]) -> ColumnElement[bool]:
    """True where `column` (a jurisdiction) names one of `codes`: the code itself or `<code>-...`."""
    upper = func.upper(column)
    return sa.or_(*(sa.or_(upper == code, upper.like(f"{code}-%")) for code in codes))


def country_of(jurisdiction: str | None) -> str | None:
    """The country `country_clause` reads in `jurisdiction`, or `None` when no code selects it."""
    if not jurisdiction:
        return None
    # Split before upper-casing and accept ASCII letters only: `str.upper` maps some non-ASCII
    # letters to ASCII ones (`ı` to `I`), which SQLite's `upper` does not.
    head = jurisdiction.split("-", 1)[0]
    return head.upper() if len(head) == 2 and head.isascii() and head.isalpha() else None


def event_subject_country_clause(codes: list[str]) -> ColumnElement[bool]:
    """`country=` on `GET /v1/events`: an event has no jurisdiction of its own, so the filter reads
    its subject's, as `resource_queries.event_subject_jurisdiction_filter` does for the events feed.
    Two uncorrelated `IN` subqueries, one per subject type; the list's own `event_visibility_filter`
    already requires the subject to be visible, so a hidden subject's country cannot be probed."""
    return sa.or_(
        sa.and_(
            Event.subject_type == "proposal",
            Event.subject_id.in_(select(Proposal.id).where(country_clause(Proposal.jurisdiction, codes))),
        ),
        sa.and_(
            Event.subject_type == "opportunity",
            Event.subject_id.in_(
                select(Opportunity.id).where(country_clause(Opportunity.jurisdiction, codes))
            ),
        ),
    )


__all__ = [
    "COUNTRY_FILTERS",
    "COUNTRY_PARAM",
    "country_clause",
    "country_of",
    "country_values",
    "event_subject_country_clause",
]
