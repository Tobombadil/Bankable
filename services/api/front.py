"""The front page's figures: `GET /v1/stats/capacity` and `GET /v1/pulse` (lane front-api, 2026-10-23).

Owner, 2026-10-23: the site is "the front page of the energy revolution", and its momentum must be
one a reader can verify: every figure lists exactly what its link shows, a source's first load is
never news, and the United States comes first with Great Britain, EU and World Bank coverage kept
and reachable. So every figure here names its lists, each `{"path", "query"}` (the shape
`organization_stats` serves, `services/api/organizations.py::list_queries`): `list_query`, the list
whose `include=count` total it is, or, for a figure that is the sum of several, `list_queries`. The
records and events are selected by those lists' own filter functions over their own visibility
predicates, never by a second definition.

**`GET /v1/stats/capacity`** takes any filter set `GET /v1/proposals` takes (`country`,
`jurisdiction`, `iso`, `kind`, `technology`, `lifecycle_state`, `listed`, ...) and answers counts and
summed MW by technology, by lifecycle state, by both, and by kind. Each bucket is a tally in the
daily metrics recorder's own shape (`services/trends/snapshot.py::Tally`, imported): generating and
storage MW (`capacity_mw`) apart from demand MW (`demand_mw`, a data centre's or other large load's),
MW never summed for lines, pipelines, LNG, CCS and hydrogen (`not_summed_records`), the records of a
summed kind that state no MW (`mw_missing`), and storage MWh where stated. A record is counted by the
values the caller's tier is served (`visibility.GatedRecord`), as the map's totals, the company
pipeline and the recorder count: a value a withheld source supplied is never summed.
`served_not_stored` says how many records were counted by a served value that differs from the
stored one the list filters on; only those can make a figure differ from its list (none on the normal
store).

**Records no register lists any more are never counted in an active state** (`web/unlisted.py`, "The
rule"; owner, 2026-10-11). The site hides them from every view made only of active states, so a figure
for such a view that counted them would show more than its link. So, whenever the caller names no
`listed`, every figure counts, of the records in an active state (`announced` to
`under_construction`), the listed ones only, and its list says `listed=true`; the dropped ones are
tallied apart in `not_listed`, with a `listed=false` list of their own. A record in any other state
(built, withdrawn, cancelled, unknown) is counted whether or not a register lists it, as the site's
view of those states shows it. A figure for one state, or for a selection made only of active or only
of other states, is the total of one list (`list_query`). A figure that spans both kinds (the whole
selection, a technology or a kind when the caller asks for every state or for a mix) is the sum of
two lists, the active states with `listed=true` and the other states with no `listed` (`list_queries`;
`list_query` is then null), because no one list selects that set. When the caller names `listed`
itself, nothing is hidden and every figure is one list carrying it. `selection` is the list of every
record read: its `include=count` total is `total.records + not_listed.records`.

**`GET /v1/pulse`** is what genuinely changed in the last `days` (7 by default, at most 90): new
proposals a source newly listed, status changes per lifecycle state (permitted, contracted, under
construction, built, withdrawn, cancelled, ...; not a move to `unknown`, which is a register that
stopped stating a status, and a withdrawal that names no state counts as one to `withdrawn`),
departures from a register that announces them (`delisted`, reason not stated), and newly open,
energy-relevant opportunities. Countries are the stored jurisdictions', as `country=` reads them;
`served_not_stored` counts the events whose record is served another country. Each is a set of
change events that `GET /v1/events` lists with `genuine=true` (`services/api/change_rule.py`: the
recorder's `genuine-change-v2`, so a source's first run, a run released from a data-quality hold and
anything no source run wrote are never counted) and the window as `observed_at[from|to]`. Counts by
type and by country, each with its list; a page of the events themselves, newest first, each with
its record link, its source and its date.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from services.api import geo_cache
from services.api.auth import AuthContext, get_auth_context
from services.api.capacity_kinds import CAPACITY_NOT_SUMMED_KINDS, DEMAND_KINDS, NEVER_SUMMED_KINDS
from services.api.change_rule import GENUINE_CHANGE_RULE, event_to_state, to_state_expression
from services.api.common import WEB_HOST, iso, utcnow
from services.api.country import COUNTRY_FILTERS, country_of, country_values
from services.api.deps import get_db
from services.api.errors import invalid_cursor, validation_error
from services.api.grid_operators import canonical_iso
from services.api.listing import listed_clause
from services.api.opportunity_relevance import is_relevant_row
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES, STATE_ORDER
from services.api.pagination import decode_cursor, encode_cursor
from services.api.params import check_allowed, csv_param, int_param, page_limit
from services.api.records import (
    PROPOSAL_FILTERS,
    PROPOSAL_VIEW_FILTERS,
    SYNC_FILTERS,
    _apply_placement_filter,
    _apply_proposal_filters,
    _source_licence_aggregate,
)
from services.api.resource_queries import event_query_with_filters, subject_infos, synthetic_request
from services.api.serialize import (
    build_envelope,
    build_meta,
    build_page,
    licence_summary_from_source_aggregates,
    serialize_event,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    gated_record,
    gated_views,
    hidden_provenance_clause,
    proposal_visibility_filter,
    source_split,
)
from services.db.models import (
    LIFECYCLE_STATES,
    Event,
    Licence,
    Opportunity,
    Proposal,
    ProposalSource,
    Source,
)
from services.trends.snapshot import ServedNotStored, ServedProposal, Tally

router = APIRouter()

PROPOSALS_PATH = "/v1/proposals"
EVENTS_PATH = "/v1/events"


def list_ref(path: str, query: Mapping[str, str]) -> dict[str, Any]:
    """A figure's list: the path and the query whose `include=count` total the figure is."""
    return {"path": path, "query": dict(query)}


# ======================================================================== GET /v1/stats/capacity
#: What the endpoint takes: every filter `GET /v1/proposals` takes, and nothing that shapes a page.
CAPACITY_PARAMS: frozenset[str] = frozenset(
    PROPOSAL_FILTERS | PROPOSAL_VIEW_FILTERS | SYNC_FILTERS | COUNTRY_FILTERS | {"q"}
)
#: The fields the figures read: a row whose stored value for one came from a source the tier may not
#: read is counted through its served view (the recorder's `PROPOSAL_FIELDS`).
COUNTED_FIELDS: tuple[str, ...] = (
    "kind",
    "technology",
    "lifecycle_state",
    "capacity_mw",
    "storage_mwh",
    "jurisdiction",
    "iso",
)


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _served(values: Any, listed: bool) -> ServedProposal:
    return ServedProposal(
        kind=values.kind or None,
        technology=values.technology or None,
        lifecycle_state=str(values.lifecycle_state or "unknown"),
        capacity_mw=_number(values.capacity_mw),
        storage_mwh=_number(values.storage_mwh),
        jurisdiction=values.jurisdiction or None,
        iso=values.iso or None,
        listed=listed,
    )


def _counted(row: ServedProposal) -> dict[str, Any]:
    """The values a figure keys on or sums, as `ServedNotStored` compares them (MW in thousandths)."""
    return {
        "kind": row.kind,
        "technology": row.technology,
        "lifecycle_state": row.lifecycle_state,
        "capacity_mw": None if row.capacity_mw is None else round(row.capacity_mw * 1000),
        "storage_mwh": None if row.storage_mwh is None else round(row.storage_mwh * 1000),
        "jurisdiction": row.jurisdiction,
        "iso": canonical_iso(row.iso),
    }


#: The states the site counts listed records only in (`web/unlisted.py`; `org_pipeline`'s `active`).
_ACTIVE: frozenset[str] = frozenset(ACTIVE_LIFECYCLE_STATES)
#: Every other state, in vocabulary order: a selection that names no `lifecycle_state` asks for all.
OTHER_LIFECYCLE_STATES: tuple[str, ...] = tuple(s for s in LIFECYCLE_STATES if s not in _ACTIVE)


@dataclass(frozen=True)
class CapacitySelection:
    """What the caller asked for (`query`, empty values dropped, as the list reads an empty value as no
    filter), split the way the figures' lists are built (module docstring)."""

    query: dict[str, str]
    #: The caller's lifecycle states, or `None` for every state.
    states: tuple[str, ...] | None
    #: The caller's own `listed`, or `None`.
    listed: str | None

    @property
    def hides_not_listed(self) -> bool:
        """Whether active-state figures count listed records only (the caller named no `listed`)."""
        return self.listed is None

    @property
    def active(self) -> tuple[str, ...]:
        """The selection's active states, in the caller's order (every one when it named none)."""
        if self.states is None:
            return ACTIVE_LIFECYCLE_STATES
        return tuple(s for s in self.states if s in _ACTIVE)

    @property
    def other(self) -> tuple[str, ...]:
        """The selection's other states, in the caller's order (every one when it named none)."""
        if self.states is None:
            return OTHER_LIFECYCLE_STATES
        return tuple(s for s in self.states if s not in _ACTIVE)

    def hidden(self, row: ServedProposal) -> bool:
        """Whether `row` is tallied in `not_listed` rather than counted."""
        return self.hides_not_listed and not row.listed and row.lifecycle_state in _ACTIVE

    def lists(self, extra: Mapping[str, str], state: str | None = None) -> list[dict[str, str]]:
        """The list queries whose `include=count` totals add up to a figure: the selection narrowed by
        `extra` (a technology or kind) and, for a state's figure, to `state`."""
        base = {k: v for k, v in self.query.items() if k not in ("lifecycle_state", "listed")} | dict(extra)
        if self.listed is not None:
            states = (state,) if state is not None else self.states
            narrowed = {"lifecycle_state": ",".join(states)} if states else {}
            return [{**base, **narrowed, "listed": self.listed}]
        if state is not None:
            return [{**base, "lifecycle_state": state, **({"listed": "true"} if state in _ACTIVE else {})}]
        out: list[dict[str, str]] = []
        if self.active:
            out.append({**base, "lifecycle_state": ",".join(self.active), "listed": "true"})
        if self.other:
            out.append({**base, "lifecycle_state": ",".join(self.other)})
        return out

    def not_listed_list(self) -> dict[str, str] | None:
        """The list of the records `not_listed` tallies, or `None` when it tallies none by construction."""
        if not self.hides_not_listed or not self.active:
            return None
        base = {k: v for k, v in self.query.items() if k not in ("lifecycle_state", "listed")}
        return {**base, "lifecycle_state": ",".join(self.active), "listed": "false"}


def capacity_selection(request: Request) -> CapacitySelection:
    """The caller's selection (`CapacitySelection`)."""
    query = {k: v for k, v in request.query_params.items() if v.strip()}
    raw_states = csv_param(query.get("lifecycle_state")) or []
    states = tuple(dict.fromkeys(raw_states)) or None
    return CapacitySelection(query=query, states=states, listed=query.get("listed"))


def served_rows(
    db: Session, query: Mapping[str, str], entitlement: str, instance: str
) -> tuple[list[ServedProposal], list[Any], ServedNotStored]:
    """Every proposal `GET /v1/proposals?<query>` selects at `entitlement`, as served; their ids; and
    the records counted by a served value that is not the stored one."""
    request = synthetic_request(query, path=instance)
    hidden = source_split(db, entitlement)[1]
    hidden = hidden - geo_cache.unused_hidden_sources(db, hidden)
    gate = hidden_provenance_clause(Proposal, COUNTED_FIELDS, hidden) if hidden else sa.false()
    stmt = select(
        Proposal.id,
        Proposal.kind,
        Proposal.technology,
        Proposal.lifecycle_state,
        Proposal.capacity_mw,
        Proposal.storage_mwh,
        Proposal.jurisdiction,
        Proposal.iso,
        listed_clause(entitlement).label("listed"),
        gate.label("gated"),
    ).where(*proposal_visibility_filter(entitlement))
    stmt = _apply_proposal_filters(stmt, request, entitlement)
    stmt = _apply_placement_filter(stmt, request, default=None)
    raw = db.execute(stmt).all()
    views = gated_views(db, Proposal, [r.id for r in raw if r.gated], entitlement) if hidden else {}
    rows: list[ServedProposal] = []
    differs = ServedNotStored()
    for r in raw:
        stored = _served(r, bool(r.listed))
        view = views.get(r.id) if r.gated else None
        if view is None:
            rows.append(stored)
            continue
        served = _served(view, bool(r.listed))
        differs.add(_counted(served), _counted(stored))
        rows.append(served)
    return rows, [r.id for r in raw], differs


def _rank_key(item: tuple[Any, Tally]) -> tuple[Any, ...]:
    """Largest generating and storage MW first, then demand MW, then most records, then the token;
    the unstated bucket last."""
    token, tally = item
    return (token is None, -tally.capacity, -tally.demand, -tally.records, str(token or ""))


def _state_key(state: str) -> tuple[int, str]:
    order = {s: i for i, s in enumerate(STATE_ORDER)}
    return (order.get(state, len(order)), state)


def _lists(queries: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """A figure's lists: `list_queries`, whose totals add up to it, and `list_query`, the one list
    when one suffices, else null (module docstring)."""
    refs = [list_ref(PROPOSALS_PATH, q) for q in queries]
    return {"list_query": refs[0] if len(refs) == 1 else None, "list_queries": refs}


def _entry(
    name: str, token: str | None, tally: Tally, queries: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    return {name: token, **tally.as_dict(), **_lists(queries)}


def capacity_summary(rows: Iterable[ServedProposal], selection: CapacitySelection) -> dict[str, Any]:
    """The figures over `rows` (module docstring), each with the lists it is the total of. A bucket of
    no stated technology or kind has none: no list filter selects a null."""
    total, not_listed = Tally(), Tally()
    by_technology: dict[str | None, Tally] = {}
    by_state: dict[str, Tally] = {}
    by_kind: dict[str | None, Tally] = {}
    cross: dict[tuple[str | None, str], Tally] = {}
    for row in rows:
        if selection.hidden(row):
            not_listed.add(row)
            continue
        total.add(row)
        by_technology.setdefault(row.technology, Tally()).add(row)
        by_state.setdefault(row.lifecycle_state, Tally()).add(row)
        by_kind.setdefault(row.kind, Tally()).add(row)
        cross.setdefault((row.technology, row.lifecycle_state), Tally()).add(row)
    technologies = sorted(by_technology.items(), key=_rank_key)
    rank = {token: i for i, (token, _t) in enumerate(technologies)}
    hidden_list = selection.not_listed_list()
    out: dict[str, Any] = {
        "selection": list_ref(PROPOSALS_PATH, selection.query),
        "hides_not_listed": selection.hides_not_listed,
        "rule": {
            "capacity": "generating and storage MW: every kind not named below",
            "demand_kinds": list(DEMAND_KINDS),
            "not_summed_kinds": list(NEVER_SUMMED_KINDS),
            "capacity_not_summed_kinds": list(CAPACITY_NOT_SUMMED_KINDS),
            "listed_only_states": list(ACTIVE_LIFECYCLE_STATES) if selection.hides_not_listed else [],
        },
        "total": {**total.as_dict(), **_lists(selection.lists({}))},
        "not_listed": (
            {**not_listed.as_dict(), **_lists([] if hidden_list is None else [hidden_list])}
            if selection.hides_not_listed
            else None
        ),
        "by_technology": [
            _entry("technology", t, tally, [] if t is None else selection.lists({"technology": t}))
            for t, tally in technologies
        ],
        "by_lifecycle_state": [
            _entry("lifecycle_state", s, by_state[s], selection.lists({}, state=s))
            for s in sorted(by_state, key=_state_key)
        ],
        "by_technology_and_lifecycle_state": [
            {
                "technology": t,
                **_entry(
                    "lifecycle_state",
                    s,
                    tally,
                    [] if t is None else selection.lists({"technology": t}, state=s),
                ),
            }
            for (t, s), tally in sorted(cross.items(), key=lambda i: (rank[i[0][0]], _state_key(i[0][1])))
        ],
        "by_kind": [
            _entry("kind", k, tally, [] if k is None else selection.lists({"kind": k}))
            for k, tally in sorted(by_kind.items(), key=_rank_key)
        ],
    }
    return out


def _gw(mw: float | None) -> float | None:
    return None if mw is None else round(mw / 1000, 3)


@router.get("/v1/stats/capacity")
def get_capacity_stats(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, set(CAPACITY_PARAMS))
    instance = request.url.path
    selection = capacity_selection(request)
    # Every record of the caller's selection, the ones no register lists too, so `not_listed` can
    # tally them; `capacity_summary` splits the two.
    rows, ids, differs = served_rows(db, selection.query, ctx.entitlement, instance)
    data = capacity_summary(rows, selection)
    data["served_not_stored"] = differs.as_dict()
    totals = data["total"]
    meta = build_meta(
        "proposal",
        tier=ctx.entitlement,
        extra={
            "capacity_gw": _gw(totals["capacity_mw"]),
            "demand_gw": _gw(totals["demand_mw"]),
            "storage_gwh": _gw(totals["storage_mwh"]),
        },
    )
    # Every record read is behind a figure (`total` or `not_listed`), so every one is credited.
    credited = ids
    return build_envelope(
        data,
        meta=meta,
        licence_summary=_source_licence_aggregate(
            db, ProposalSource, ProposalSource.proposal_id, credited, ctx.entitlement
        ),
    )


# ====================================================================================== GET /v1/pulse
PULSE_DAYS_DEFAULT = 7
PULSE_DAYS_MAX = 90
PULSE_PARAMS: frozenset[str] = frozenset({"days", "limit", "cursor"} | COUNTRY_FILTERS)
#: The pulse's change types, in the order they are listed.
NEW_PROPOSAL = "new_proposal"
STATUS_CHANGE = "status_change"
DELISTED = "delisted"
NEW_OPEN_OPPORTUNITY = "new_open_opportunity"
#: The event types a proposal's move to another lifecycle state is written as (`pipeline/diff.py`:
#: a move to `withdrawn` or `cancelled` is a `withdrawn` event, any other a `status_change`; a
#: `withdrawn` event that names no state moved it to `withdrawn`, `change_rule.event_to_state`).
STATUS_EVENT_TYPES: tuple[str, ...] = ("status_change", "withdrawn")
#: The states a status change is reported for: every lifecycle state but `unknown`. A move to
#: `unknown` is a register that stopped stating a status, not news about the project.
PULSE_STATES: tuple[str, ...] = tuple(s for s in LIFECYCLE_STATES if s != "unknown")
#: A notice that newly opens: one first listed open, or one whose status moved to open.
OPENING_EVENT_TYPES: tuple[str, ...] = ("created", "status_change")


@dataclass(frozen=True)
class PulseSpec:
    """One change type (and for a status change, the state moved to) and its events-list filters."""

    type: str
    to_state: str | None
    filters: dict[str, str]

    def keys(self) -> list[tuple[str, str, str | None]]:
        """`(subject_type, event_type, to_state)` for each event type the spec's list names; the
        state is `None` when the list does not filter by it."""
        state = self.filters.get("to_state")
        return [(self.filters["subject_type"], t, state) for t in self.filters["event_type"].split(",")]


def pulse_specs() -> list[PulseSpec]:
    """Every change type the pulse counts, with the filters `GET /v1/events` selects it by."""
    specs = [PulseSpec(NEW_PROPOSAL, None, {"subject_type": "proposal", "event_type": "created"})]
    specs += [
        PulseSpec(
            STATUS_CHANGE,
            state,
            {"subject_type": "proposal", "event_type": ",".join(STATUS_EVENT_TYPES), "to_state": state},
        )
        for state in PULSE_STATES
    ]
    specs.append(PulseSpec(DELISTED, None, {"subject_type": "proposal", "event_type": "delisted"}))
    specs.append(
        PulseSpec(
            NEW_OPEN_OPPORTUNITY,
            "open",
            {
                "subject_type": "opportunity",
                "event_type": ",".join(OPENING_EVENT_TYPES),
                "to_state": "open",
                "relevance": "energy",
            },
        )
    )
    return specs


def spec_index(specs: Sequence[PulseSpec]) -> dict[tuple[str, str, str | None], PulseSpec]:
    """Each `(subject_type, event_type, to_state)` the change types name, to its type. The types are
    disjoint, so a row finds at most one: by its own state first, then by a type that takes any."""
    index: dict[tuple[str, str, str | None], PulseSpec] = {}
    for spec in specs:
        for key in spec.keys():
            if key in index:  # pragma: no cover - the types are disjoint by construction
                raise ValueError(f"two change types claim {key}")
            index[key] = spec
    return index


def classify(
    row: _PulseRow, index: Mapping[tuple[str, str, str | None], PulseSpec], relevant: frozenset[Any]
) -> PulseSpec | None:
    """The change type `row` counts under, by the same tests its list's filters make in SQL over the
    rows the shared filters (window, rule, country) already selected, or `None`; `relevant` is the
    energy-relevant notices (`relevance=energy`)."""
    spec = index.get((row.subject_type, row.event_type, row.to_state)) or index.get(
        (row.subject_type, row.event_type, None)
    )
    if spec is None or ("relevance" in spec.filters and row.subject_id not in relevant):
        return None
    return spec


@dataclass(frozen=True)
class _PulseRow:
    id: Any
    subject_type: str
    subject_id: Any
    event_type: str
    to_state: str | None
    country: str | None
    #: The page order, newest first: `(observed_at, id)` descending.
    order: tuple[dt.datetime, str]
    source_id: str
    licence_id: str
    retrieved_at: dt.datetime | None


def _aware(value: dt.datetime) -> dt.datetime:
    """SQLite hands back naive UTC; compare instants either way."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


@dataclass
class _Count:
    events: int = 0
    subjects: set[Any] = field(default_factory=set)

    def add(self, row: _PulseRow) -> None:
        self.events += 1
        self.subjects.add((row.subject_type, row.subject_id))

    def as_dict(self) -> dict[str, int]:
        return {"events": self.events, "subjects": len(self.subjects)}


def pulse_window(request: Request, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, int]:
    """`[now - days, now]` to the second, `days` from `?days=` (1 to 90, default 7)."""
    days = int_param(request, "days")
    if days is None:
        days = PULSE_DAYS_DEFAULT
    if not 1 <= days <= PULSE_DAYS_MAX:
        raise validation_error("days", f"days must be between 1 and {PULSE_DAYS_MAX}", request.url.path)
    end = now.astimezone(dt.UTC).replace(microsecond=0)
    return end - dt.timedelta(days=days), end, days


def _shared_filters(start: dt.datetime, end: dt.datetime, countries: list[str] | None) -> dict[str, str]:
    shared = {
        "genuine": "true",
        "observed_at[from]": str(iso(start)),
        "observed_at[to]": str(iso(end)),
    }
    if countries:
        shared["country"] = ",".join(countries)
    return shared


def _candidate_statement(
    db: Session, shared: Mapping[str, str], entitlement: str, instance: str
) -> sa.Select[tuple[Event]]:
    """Every event any change type could count: the events list over the shared filters (window,
    rule, country) and the union of the types' subject and event types. Each type is then a subset
    of these by its own remaining filters."""
    event_types = sorted({t for s in pulse_specs() for t in s.filters["event_type"].split(",")})
    params = {**shared, "subject_type": "proposal,opportunity", "event_type": ",".join(event_types)}
    return event_query_with_filters(synthetic_request(params, path=instance), db, entitlement)


def _relevant_notices(db: Session, ids: Iterable[Any]) -> frozenset[Any]:
    """Of the notices `ids`, the energy-relevant ones (stored fields, as `relevance=energy`)."""
    wanted = sorted(set(ids), key=str)
    out: set[Any] = set()
    for start in range(0, len(wanted), 500):
        for r in db.execute(
            select(
                Opportunity.id,
                Opportunity.title,
                Opportunity.summary,
                Opportunity.technologies,
                Opportunity.identifiers,
            ).where(Opportunity.id.in_(wanted[start : start + 500]))
        ):
            if is_relevant_row(r.title, r.summary, r.technologies, r.identifiers):
                out.add(r.id)
    return frozenset(out)


def _subject_jurisdiction() -> sa.ColumnElement[Any]:
    """The event's subject's stored jurisdiction, as the `country=` filter reads it: one correlated
    primary-key lookup per event, in the candidate read itself."""
    proposal = select(Proposal.jurisdiction).where(Proposal.id == Event.subject_id).scalar_subquery()
    opportunity = select(Opportunity.jurisdiction).where(Opportunity.id == Event.subject_id).scalar_subquery()
    return sa.case((Event.subject_type == "proposal", proposal), else_=opportunity)


def _event_licence_aggregate(db: Session, rows: Iterable[_PulseRow]) -> dict[str, Any]:
    """`licence_summary` over every counted event, by source and licence (the credit every figure
    needs, not only the page's): counted over the rows already read, then named from the few
    sources and licences they cite."""
    counts: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        entry = counts.setdefault((row.source_id, row.licence_id), [0, None])
        entry[0] += 1
        if row.retrieved_at is not None and (entry[1] is None or row.retrieved_at > entry[1]):
            entry[1] = row.retrieved_at
    if not counts:
        return licence_summary_from_source_aggregates([])
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_({k[0] for k in counts})))}
    licences = {
        lic.id: lic
        for lic in db.scalars(
            select(Licence).where(
                Licence.id.in_({k[1] for k in counts}), Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES)
            )
        )
    }
    aggregates = []
    for (source_id, licence_id), (count, latest) in sorted(counts.items()):
        source, licence = sources.get(source_id), licences.get(licence_id)
        if source is None or licence is None:
            continue
        aggregates.append(
            (
                source.id,
                source.name,
                source.operator,
                licence.id,
                licence.name,
                licence.url,
                licence.reuse_class,
                source.attribution_text or licence.attribution_text,
                licence.requires_link_back,
                latest,
                count,
            )
        )
    return licence_summary_from_source_aggregates(aggregates)  # type: ignore[arg-type]


def _record_block(db: Session, events: Sequence[Event], entitlement: str) -> dict[Any, dict[str, Any]]:
    """The served fields of each page item's record (name and link from `subject_infos`)."""
    infos = subject_infos(db, events, entitlement)
    out: dict[Any, dict[str, Any]] = {}
    for subject_type, model in (("proposal", Proposal), ("opportunity", Opportunity)):
        ids = sorted({e.subject_id for e in events if e.subject_type == subject_type}, key=str)
        if not ids:
            continue
        stmt = select(model).where(model.id.in_(ids)).options(selectinload(model.sources))
        for record in cast("list[Proposal | Opportunity]", list(db.scalars(stmt))):
            view = gated_record(record, entitlement)
            info = infos[record.id]
            block: dict[str, Any] = {
                "subject_type": subject_type,
                "public_id": info["subject_public_id"],
                "name": info["subject_name"],
                "url": info["subject_url"],
                "kind": view.kind,
                "jurisdiction": view.jurisdiction,
                "country": country_of(view.jurisdiction),
            }
            if subject_type == "proposal":
                block.update(
                    {
                        "technology": view.technology,
                        "lifecycle_state": view.lifecycle_state,
                        "capacity_mw": _number(view.capacity_mw),
                        "storage_mwh": _number(view.storage_mwh),
                    }
                )
            else:
                block.update(
                    {
                        "status": view.status,
                        "due_at": iso(view.due_at),
                        "capacity_sought_mw": _number(view.capacity_sought_mw),
                    }
                )
            out[record.id] = block
    return out


def _state(payload: Mapping[str, Any] | None) -> str | None:
    value = (payload or {}).get("lifecycle_state")
    return None if value is None else str(value)


def _item(event: Event, spec: PulseSpec, record: dict[str, Any] | None) -> dict[str, Any]:
    info = {
        "subject_public_id": (record or {}).get("public_id") or str(event.subject_id),
        "subject_name": (record or {}).get("name") or "Unknown",
        "subject_url": (record or {}).get("url") or WEB_HOST,
    }
    served = serialize_event(event, **info)
    return {
        "type": spec.type,
        "to_state": event_to_state(event.subject_type, event.event_type, event.after),
        "from_state": _state(event.before),
        "observed_at": served["observed_at"],
        "event_id": served["id"],
        "headline": served["headline"],
        "reason": served["reason"],
        # As served: it differs from the country the event was counted under (the stored
        # jurisdiction's, as `country=` reads it) only as the pulse's `served_not_stored` says.
        "record": record,
        "provenance": served["provenance"],
    }


def _country_served_not_stored(db: Session, rows: Sequence[_PulseRow], entitlement: str) -> dict[str, int]:
    """Of the counted events, those whose record is served a jurisdiction of another country than
    the stored one they were counted under (as `capacity`'s `served_not_stored`): only a record
    whose stored jurisdiction came from a source the tier may not read can be one. None on the
    normal store, where no such source supplies a counted record's jurisdiction."""
    count = _Count()
    hidden = source_split(db, entitlement)[1]
    if not hidden or not rows:
        return count.as_dict()
    for subject_type, model in (("proposal", Proposal), ("opportunity", Opportunity)):
        of_type = [r for r in rows if r.subject_type == subject_type]
        wanted = sorted({r.subject_id for r in of_type}, key=str)
        gated: set[Any] = set()
        for start in range(0, len(wanted), 500):
            gated.update(
                db.scalars(
                    select(model.id).where(
                        model.id.in_(wanted[start : start + 500]),
                        hidden_provenance_clause(model, ("jurisdiction",), hidden),
                    )
                )
            )
        if not gated:
            continue
        views = gated_views(db, model, gated, entitlement)
        for row in of_type:
            view = views.get(row.subject_id)
            if view is not None and country_of(view.jurisdiction) != row.country:
                count.add(row)
    return count.as_dict()


def _country_entries(
    counts: Mapping[str | None, _Count], filters: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    """By country, most events first; `null` (a jurisdiction no code selects, such as `global`)
    last, with no list query."""
    ordered = sorted(counts.items(), key=lambda i: (i[0] is None, -i[1].events, i[0] or ""))
    return [
        {
            "country": country,
            **count.as_dict(),
            "list_query": (
                None
                if country is None or filters is None
                else list_ref(EVENTS_PATH, {**filters, "country": country})
            ),
        }
        for country, count in ordered
    ]


def _without_country(filters: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in filters.items() if k != "country"}


@router.get("/v1/pulse")
def get_pulse(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, set(PULSE_PARAMS))
    instance = request.url.path
    start, end, days = pulse_window(request, utcnow())
    countries = country_values(request.query_params.get("country"), instance)
    limit = page_limit(request)
    shared = _shared_filters(start, end, countries)
    candidates = _candidate_statement(db, shared, ctx.entitlement, instance)
    raw = db.execute(
        candidates.with_only_columns(
            Event.id,
            Event.subject_type,
            Event.subject_id,
            Event.event_type,
            to_state_expression().label("to_state"),
            Event.observed_at,
            Event.source_id,
            Event.licence_id,
            Event.retrieved_at,
            _subject_jurisdiction().label("jurisdiction"),
        ).order_by(None)
    ).all()
    rows = [
        _PulseRow(
            id=r.id,
            subject_type=r.subject_type,
            subject_id=r.subject_id,
            event_type=r.event_type,
            to_state=r.to_state,
            country=country_of(r.jurisdiction),
            order=(_aware(r.observed_at), str(r.id)),
            source_id=r.source_id,
            licence_id=r.licence_id,
            retrieved_at=r.retrieved_at,
        )
        for r in raw
    ]
    # Only a notice that could count as newly open needs the energy verdict.
    opening = (
        r.subject_id
        for r in rows
        if r.subject_type == "opportunity" and r.event_type in OPENING_EVENT_TYPES and r.to_state == "open"
    )
    relevant = _relevant_notices(db, opening)
    counted, changes, total, by_country = pulse_counts(rows, relevant, shared)
    page, next_cursor, has_more = _page(
        list(counted.values()), request.query_params.get("cursor"), limit, instance
    )
    events = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_([r.id for r in page])))}
    page_events = [events[r.id] for r in page if r.id in events]
    records = _record_block(db, page_events, ctx.entitlement)
    data = {
        "window": {"from": iso(start), "to": iso(end), "days": days},
        "country": countries,
        "rule": {
            "name": GENUINE_CHANGE_RULE,
            "parameter": "genuine=true",
            "statement": (
                "A change event counts when a source run wrote it after that source's first run here, "
                "and the run was not released from a data-quality hold: a source's first load, a "
                "released hold and anything no source run wrote (matches, merges, edits) are left out. "
                "The date is the fetch that saw the change, not the register's own action date."
            ),
        },
        "total": total,
        "changes": changes,
        "by_country": by_country,
        "served_not_stored": _country_served_not_stored(
            db, [r for r, _spec in counted.values()], ctx.entitlement
        ),
        "items": [_item(e, counted[e.id][1], records.get(e.subject_id)) for e in page_events],
    }
    envelope = build_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=_event_licence_aggregate(db, (r for r, _spec in counted.values())),
    )
    envelope["page"] = build_page(next_cursor, None, has_more)
    return envelope


def pulse_counts(
    rows: Sequence[_PulseRow], relevant: frozenset[Any], shared: Mapping[str, str]
) -> tuple[
    dict[Any, tuple[_PulseRow, PulseSpec]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]
]:
    """Each row's change type (the types are disjoint), the counts by type and by country with their
    lists, the total and the totals by country (each the sum of the lists it names)."""
    specs = pulse_specs()
    index = spec_index(specs)
    counted: dict[Any, tuple[_PulseRow, PulseSpec]] = {}
    per_spec: dict[int, tuple[_Count, dict[str | None, _Count]]] = {}
    total = _Count()
    country_totals: dict[str | None, _Count] = defaultdict(_Count)
    for row in rows:
        spec = classify(row, index, relevant)
        if spec is None or row.id in counted:
            continue
        counted[row.id] = (row, spec)
        count, by_country = per_spec.setdefault(id(spec), (_Count(), defaultdict(_Count)))
        count.add(row)
        by_country[row.country].add(row)
        total.add(row)
        country_totals[row.country].add(row)
    contributing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    changes: list[dict[str, Any]] = []
    nonzero: list[dict[str, Any]] = []
    for spec in specs:
        count, spec_countries = per_spec.get(id(spec), (_Count(), {}))
        filters = {**shared, **spec.filters}
        unscoped = _without_country(filters)
        changes.append(
            {
                "type": spec.type,
                "to_state": spec.to_state,
                **count.as_dict(),
                "list_query": list_ref(EVENTS_PATH, filters),
                "by_country": _country_entries(spec_countries, unscoped),
            }
        )
        if count.events:
            nonzero.append(list_ref(EVENTS_PATH, filters))
        for country in spec_countries:
            if country is not None:
                contributing[country].append(list_ref(EVENTS_PATH, {**unscoped, "country": country}))
    ordered = sorted(country_totals.items(), key=lambda i: (i[0] is None, -i[1].events, i[0] or ""))
    by_country_out = [
        {
            "country": country,
            **count.as_dict(),
            "list_queries": contributing.get(country, []) if country is not None else [],
        }
        for country, count in ordered
    ]
    return counted, changes, {**total.as_dict(), "list_queries": nonzero}, by_country_out


def _page(
    counted: Sequence[Any], cursor: str | None, limit: int, instance: str
) -> tuple[list[_PulseRow], str | None, bool]:
    """A page of the counted events, newest first, `limit` long, after `cursor` (the opaque
    `{observed_at, id}` cursor the lists use). Paged here, over the rows already read, rather than by
    a second query binding every counted id."""
    rows = sorted((row for row, _spec in counted), key=lambda r: r.order, reverse=True)
    if cursor:
        decoded = decode_cursor(cursor, instance)
        try:
            after = (_aware(dt.datetime.fromisoformat(str(decoded.sort_value))), str(decoded.tiebreaker_id))
        except ValueError as exc:
            raise invalid_cursor(instance) from exc
        rows = [r for r in rows if r.order < after]
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = (
        encode_cursor(page[-1].order[0].isoformat(), page[-1].order[1]) if has_more and page else None
    )
    return page, next_cursor, has_more


__all__ = [
    "CAPACITY_PARAMS",
    "OTHER_LIFECYCLE_STATES",
    "PULSE_DAYS_MAX",
    "CapacitySelection",
    "PulseSpec",
    "capacity_selection",
    "capacity_summary",
    "list_ref",
    "pulse_counts",
    "pulse_specs",
    "router",
    "served_rows",
]
