"""One UTC day's metrics, computed from the store exactly as the public tier sees it (`docs/24-trends.md`).

**Convention.** The document for UTC day D (`as_of`) holds the store as an anonymous visitor would
have been served it at `generated_at` on D (the scheduler measures at 05:58 UTC, after the day's
loads), and the activity of the previous UTC day, D-1, the last complete one. `compute_snapshot`
takes only the moment it measures at and derives both from it, so no document can carry one day's
state under another day's name. That moment must be timezone-aware: a naive one is refused, since
its day depends on the zone it was meant in.

**Visibility.** Every row counted passes the predicate the public routes apply
(`services/api/visibility.py`: `proposal_visibility_filter`, `opportunity_visibility_filter`,
`event_visibility_filter` at `public`), so no restricted, unpublished, not-yet-public or merged-away
row is ever counted, and nothing else is read: no private project, data room, account, alert or
person (`services/trends/test_snapshot.py` pins the tables the computation touches). A counted
record whose stored value for a counted field came from a source the public tier may not read is
counted as served (`visibility.GatedRecord`), never as stored, as the map's totals and the company
pipeline are (`records._proposal_geo_features`, `org_pipeline._rows`): a figure derived from a
withheld source is left out exactly as the public tier leaves it out.

**Every figure is the total of a list query.** `queries` names the `GET /v1/proposals`,
`/v1/opportunities` and `/v1/events` query behind each block and `bucket_filters` the parameter
behind each breakdown, so `?<query>&<filter>=<key>&include=count` answers each figure. Two
exceptions, both stated in the document: the `_unstated` bucket (a null technology, operator or
state), which no filter can ask for; and a record whose counted value a withheld source supplied,
which the list filters by its stored value and this counts by its served one (only possible while a
withheld source is linked to a public record; the normal store has none). How many records that
was, and for which fields, is recorded in `visibility.served_not_stored`: there, and only there,
a figure can differ from its list query's total.

- **Proposals**: `active` is announced through under construction and still listed by a register
  the tier may read, the map's and the list's default view (`web/unlisted.py`: the map hides the
  rest); `active_not_listed` is the active states no register lists any more; `withdrawn`
  (withdrawn or cancelled), `built` and `unknown` hide nothing, as their views do not. Together the
  five partition `visible`. Capacity is summed only where `capacity_mw` is generating or storage
  capacity (`org_pipeline.CAPACITY_NOT_SUMMED_KINDS`); a load's MW is demand and is summed apart
  (`demand_mw`); a line's, a pipeline's or a CO2 well's is never summed (`not_summed_records`).
  `mw_missing` counts the summed kinds that state no MW. Country is the jurisdiction's country part
  (`US`, `GB`, else `other`); a US state is the jurisdiction (`US-TX`, the `/texas` landing's
  filter); a grid operator is `grid_operators.canonical_iso` (`ERCO` reads `ERCOT`, as `iso=`
  matches). Every block is broken down by US state, and both active blocks by state and kind
  (`by_state_kind`), so any state's landing figures can be rebuilt from a document: the `/texas`
  landing's planned large loads are `kind=load` in `US-TX` across both active blocks, since that
  figure does not hide the records no register lists.
- **Opportunities**: `open` and `announced` notices, every one and the energy-relevant ones
  (`relevance=energy`, the RFPs page's default view and the homepage's figure), by kind, country
  and US state.
- **Activity**: the change events of the previous UTC day that are genuine changes at a source
  (`activity_rule` below), by event type and subject type, each with the number of distinct
  subjects (projects or notices) those events changed, with what was left out and why.

Each document also lists the sources that contributed, with their licences, and the visibility
posture the figures were computed under.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from services.api import geo_cache
from services.api.common import iso
from services.api.grid_operators import canonical_iso
from services.api.listing import listed_clause
from services.api.opportunity_relevance import is_relevant_row
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES, CAPACITY_NOT_SUMMED_KINDS
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    event_visibility_filter,
    gated_views,
    hidden_provenance_clause,
    opportunity_visibility_filter,
    permitted_source_states,
    proposal_visibility_filter,
    source_split,
)
from services.db.models import (
    OPPORTUNITY_STATUSES,
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
    SourceRun,
)
from services.match.relevance import RELEVANCE_VERSION

#: Bumped when a key's meaning changes; a new key alone does not bump it.
#:
#: 1. 2026-10-23, the first documents.
#: 2. 2026-10-23: a `bucket_filters` value may be a list (a two-level breakdown's filters, outer
#:    first: `by_state_kind`); the activity rule is `genuine-change-v2` (a backfill-labelled run
#:    that is not a source's first is counted, so `left_out` has no `backfill`). New keys: `by_state`
#:    on every proposal and opportunity block, `by_state_kind` on both active blocks,
#:    `visibility.served_not_stored`, the activity's distinct-subject counts and `by_run_trigger`.
#:    A version-1 document lacks them: a reader treats a missing key as not recorded, never as 0.
SCHEMA_VERSION = 2
#: The versions `services.trends.store` reads back: every version ever written.
READABLE_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)
DOCUMENT_KIND = "inframation.trends.daily"
#: The tier every figure is computed at: an anonymous visitor's.
TIER = "public"
#: The bucket of a null technology, operator, kind or state: no list filter can ask for it.
NULL_KEY = "_unstated"
COUNTRIES: tuple[str, ...] = ("US", "GB")
OTHER_COUNTRY = "other"

ACTIVE_STATES: tuple[str, ...] = ACTIVE_LIFECYCLE_STATES
WITHDRAWN_STATES: tuple[str, ...] = ("withdrawn", "cancelled")
BUILT_STATES: tuple[str, ...] = ("built",)
UNKNOWN_STATES: tuple[str, ...] = ("unknown",)
#: Kinds whose `capacity_mw` is demand, summed apart as `demand_mw`.
DEMAND_KINDS: tuple[str, ...] = ("load",)
#: Kinds whose MW is never summed: transfer capability, throughput or nothing (module docstring).
NOT_SUMMED_KINDS: tuple[str, ...] = tuple(k for k in CAPACITY_NOT_SUMMED_KINDS if k not in DEMAND_KINDS)
OPPORTUNITY_STATUSES_COUNTED: tuple[str, ...] = ("open", "announced")

#: The proposal fields the figures read: a row whose stored value for one came from a withheld
#: source is read through its served view.
PROPOSAL_FIELDS: tuple[str, ...] = (
    "kind",
    "technology",
    "lifecycle_state",
    "capacity_mw",
    "storage_mwh",
    "jurisdiction",
    "iso",
)
#: The opportunity fields the figures and the relevance verdict read.
OPPORTUNITY_FIELDS: tuple[str, ...] = (
    "kind",
    "status",
    "jurisdiction",
    "title",
    "summary",
    "technologies",
    "identifiers",
)

#: The activity rule's name, recorded in each document; a new rule gets a new name.
#: `genuine-change-v1` (schema 1) also left out every run labelled `backfill`.
ACTIVITY_RULE = "genuine-change-v2"
#: Why a visible event in the window was not counted (`activity_rule`), in the order checked.
LEFT_OUT_REASONS: tuple[str, ...] = ("no_source_run", "released_hold", "first_load")

#: The list filter each proposal and opportunity breakdown's key is a value of (`None`: no filter
#: yet; the planned `country` filter). A two-level breakdown names one filter per level, outer
#: first: `by_state_kind["US-TX"]["load"]` is `jurisdiction=US-TX&kind=load`. The activity's
#: breakdowns have none: the counted events are a subset of what `/v1/events` answers for the window
#: (`activity.visible_events`).
BUCKET_FILTERS: dict[str, str | tuple[str, ...] | None] = {
    "by_kind": "kind",
    "by_technology": "technology",
    "by_lifecycle_state": "lifecycle_state",
    "by_iso": "iso",
    "by_state": "jurisdiction",
    "by_state_kind": ("jurisdiction", "kind"),
    "by_country": None,
}
#: The two-level breakdowns: the outer level's dimension, then the inner one's.
NESTED_DIMENSIONS: dict[str, tuple[str, str]] = {"by_state_kind": ("by_state", "by_kind")}

#: The proposal blocks, the lifecycle states each holds, whether a register must still list the
#: record, and the breakdowns it carries. Every block has `by_state`, so a state's lifecycle
#: notice (active, withdrawn, built, unknown) rebuilds from a document; both active blocks have
#: `by_state_kind`, since a landing's large-load figure counts the active states listed or not.
_PROPOSAL_BLOCKS: tuple[tuple[str, tuple[str, ...], bool | None, tuple[str, ...]], ...] = (
    (
        "active",
        ACTIVE_STATES,
        True,
        (
            "by_kind",
            "by_technology",
            "by_lifecycle_state",
            "by_country",
            "by_state",
            "by_iso",
            "by_state_kind",
        ),
    ),
    (
        "active_not_listed",
        ACTIVE_STATES,
        False,
        ("by_kind", "by_country", "by_state", "by_iso", "by_state_kind"),
    ),
    (
        "withdrawn",
        WITHDRAWN_STATES,
        None,
        ("by_kind", "by_technology", "by_lifecycle_state", "by_country", "by_state", "by_iso"),
    ),
    ("built", BUILT_STATES, None, ("by_kind", "by_technology", "by_country", "by_state", "by_iso")),
    ("unknown", UNKNOWN_STATES, None, ("by_kind", "by_country", "by_state")),
)


def activity_rule() -> str:
    """What `activity.counted` counts, as each document states it."""
    return (
        "A change event counts on the UTC day its source showed it (observed_at, the fetch that saw it) "
        "when the public tier serves it (event_visibility_filter: the subject is public, the source and "
        "licence are publishable, never removed_from_source) and it came from a source run that compared "
        "against a snapshot this store already held: not a source's first run here (first_load: the "
        "source's earliest run that promoted output, a seed or a first fetch, whose rows are all new to "
        "the store), not a run released from a data-quality hold (released_hold: the row-drift and churn "
        "gates hold re-keys and scope changes), and not an event no source run wrote (no_source_run: "
        "matches, merges and edits are not changes at a source). A run's trigger label (schedule, manual, "
        "backfill, retry) changes nothing the connector fetches or compares against, so every label "
        "counts alike; counted.by_run_trigger says how many came from each."
    )


def _utc(now: dt.datetime) -> dt.datetime:
    """`now` in UTC. A naive datetime is refused: which UTC day it falls on depends on the zone it
    was meant in, and reading it as the host's local time (`astimezone`) would file a day's figures
    under the wrong date on any host not set to UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now must be timezone-aware, got the naive {now.isoformat()}")
    return now.astimezone(dt.UTC)


# ---------------------------------------------------------------------------------- proposals
@dataclass(frozen=True)
class ServedProposal:
    """One visible proposal's counted fields, as the public tier serves them."""

    kind: str | None
    technology: str | None
    lifecycle_state: str
    capacity_mw: float | None
    storage_mwh: float | None
    jurisdiction: str | None
    iso: str | None
    listed: bool


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def country_of(jurisdiction: str | None) -> str:
    """`US`, `GB` or `other`: the jurisdiction's country part (`US-TX` is `US`)."""
    code = (jurisdiction or "").split("-", 1)[0].strip().upper()
    return code if code in COUNTRIES else OTHER_COUNTRY


def us_state_of(jurisdiction: str | None) -> str | None:
    """A US record's state (`US-TX`), `_unstated` for a US record known only to the country, and
    `None` (not in the breakdown) for any other country."""
    if country_of(jurisdiction) != "US":
        return None
    code = (jurisdiction or "").strip().upper()
    return code if "-" in code and code.split("-", 1)[1] else NULL_KEY


_PROPOSAL_KEYS: dict[str, Callable[[ServedProposal], str | None]] = {
    "by_kind": lambda r: r.kind or NULL_KEY,
    "by_technology": lambda r: r.technology or NULL_KEY,
    "by_lifecycle_state": lambda r: r.lifecycle_state,
    "by_country": lambda r: country_of(r.jurisdiction),
    "by_state": lambda r: us_state_of(r.jurisdiction),
    "by_iso": lambda r: canonical_iso(r.iso) or NULL_KEY,
}


def _thousandths(value: float) -> int:
    """MW (or MWh) as an integer count of thousandths, the column's own precision (`Numeric(12, 3)`),
    so a sum is exact and the same in whatever order the rows arrive."""
    return round(value * 1000)


def _sum(thousandths: int, records: int) -> float | None:
    return thousandths / 1000 if records else None


@dataclass
class Tally:
    """One bucket of proposals: how many, and their MW and MWh (module docstring). `records` is
    always `capacity_records + demand_records + mw_missing + not_summed_records`. Sums are kept in
    thousandths (`_thousandths`)."""

    records: int = 0
    capacity: int = 0
    capacity_records: int = 0
    demand: int = 0
    demand_records: int = 0
    mw_missing: int = 0
    not_summed_records: int = 0
    storage: int = 0
    storage_mwh_records: int = 0

    def add(self, row: ServedProposal) -> None:
        self.records += 1
        if row.kind in NOT_SUMMED_KINDS:
            self.not_summed_records += 1
        elif row.capacity_mw is None:
            self.mw_missing += 1
        elif row.kind in DEMAND_KINDS:
            self.demand += _thousandths(row.capacity_mw)
            self.demand_records += 1
        else:
            self.capacity += _thousandths(row.capacity_mw)
            self.capacity_records += 1
        if row.storage_mwh is not None:
            self.storage += _thousandths(row.storage_mwh)
            self.storage_mwh_records += 1

    def as_dict(self) -> dict[str, Any]:
        """A sum over no stated value is `null`, never `0`."""
        return {
            "records": self.records,
            "capacity_mw": _sum(self.capacity, self.capacity_records),
            "capacity_records": self.capacity_records,
            "demand_mw": _sum(self.demand, self.demand_records),
            "demand_records": self.demand_records,
            "mw_missing": self.mw_missing,
            "not_summed_records": self.not_summed_records,
            "storage_mwh": _sum(self.storage, self.storage_mwh_records),
            "storage_mwh_records": self.storage_mwh_records,
        }


@dataclass
class ProposalBlock:
    """A total and its breakdowns, each a `Tally` per key, or per outer then inner key for a
    two-level breakdown (`NESTED_DIMENSIONS`)."""

    dimensions: tuple[str, ...]
    total: Tally = field(default_factory=Tally)
    buckets: dict[str, dict[str, Tally]] = field(default_factory=dict)
    nested: dict[str, dict[str, dict[str, Tally]]] = field(default_factory=dict)

    def add(self, row: ServedProposal) -> None:
        self.total.add(row)
        for dimension in self.dimensions:
            if dimension in NESTED_DIMENSIONS:
                outer_dim, inner_dim = NESTED_DIMENSIONS[dimension]
                outer, inner = _PROPOSAL_KEYS[outer_dim](row), _PROPOSAL_KEYS[inner_dim](row)
                if outer is not None and inner is not None:
                    level = self.nested.setdefault(dimension, {}).setdefault(outer, {})
                    level.setdefault(inner, Tally()).add(row)
                continue
            key = _PROPOSAL_KEYS[dimension](row)
            if key is not None:
                self.buckets.setdefault(dimension, {}).setdefault(key, Tally()).add(row)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"total": self.total.as_dict()}
        for dimension in self.dimensions:
            if dimension in NESTED_DIMENSIONS:
                levels = self.nested.get(dimension, {})
                out[dimension] = {
                    outer: {inner: levels[outer][inner].as_dict() for inner in sorted(levels[outer])}
                    for outer in sorted(levels)
                }
                continue
            tallies = self.buckets.get(dimension, {})
            out[dimension] = {key: tallies[key].as_dict() for key in sorted(tallies)}
        return out


@dataclass
class ServedNotStored:
    """Visible records whose served value for a counted field is not the stored one, because a
    source the tier may not read supplied the stored value (`visibility.GatedRecord`): the records
    for which a figure can differ from its list query's total, which filters by the stored value.
    Counted by record and by field."""

    records: int = 0
    by_field: dict[str, int] = field(default_factory=dict)

    def add(self, served: Mapping[str, Any], stored: Mapping[str, Any]) -> None:
        changed = [name for name in served if served[name] != stored[name]]
        if changed:
            self.records += 1
            for name in changed:
                self.by_field[name] = self.by_field.get(name, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"records": self.records, "by_field": dict(sorted(self.by_field.items()))}


def _in_use(session: Session, model: Any, link_model: Any, hidden: frozenset[str]) -> frozenset[str]:
    """The withheld sources a record of `model` could have a value from: linked to one, or named in
    one's `field_provenance`. Any other withheld source changes nothing served, so the per-row JSON
    clauses need not name it (the proposal side is `geo_cache.unused_hidden_sources`)."""
    used: set[str] = set()
    for sid in sorted(hidden):
        named = session.execute(
            select(
                sa.or_(
                    sa.exists().where(link_model.source_id == sid),
                    sa.exists().where(
                        sa.cast(model.field_provenance, sa.Text).contains(f'"{sid}"', autoescape=True)
                    ),
                )
            )
        ).scalar()
        if named:
            used.add(sid)
    return frozenset(used)


def _served_proposal(values: Any, listed: bool) -> ServedProposal:
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


def _proposal_counted(row: ServedProposal) -> dict[str, Any]:
    """The values the figures key on and sum, as `ServedNotStored` compares them: MW in the
    thousandths they are summed in, the grid operator as its bucket names it."""
    return {
        "kind": row.kind,
        "technology": row.technology,
        "lifecycle_state": row.lifecycle_state,
        "capacity_mw": None if row.capacity_mw is None else _thousandths(row.capacity_mw),
        "storage_mwh": None if row.storage_mwh is None else _thousandths(row.storage_mwh),
        "jurisdiction": row.jurisdiction,
        "iso": canonical_iso(row.iso),
    }


def served_proposals(
    session: Session, now: dt.datetime, hidden: frozenset[str]
) -> tuple[list[ServedProposal], list[Any], ServedNotStored]:
    """Every proposal the public tier serves at `now`, with its counted fields as served, the ids
    (for the sources), and the records whose served values are not the stored ones."""
    hidden = hidden - geo_cache.unused_hidden_sources(session, hidden)
    gate = hidden_provenance_clause(Proposal, PROPOSAL_FIELDS, hidden) if hidden else sa.false()
    stmt = select(
        Proposal.id,
        Proposal.kind,
        Proposal.technology,
        Proposal.lifecycle_state,
        Proposal.capacity_mw,
        Proposal.storage_mwh,
        Proposal.jurisdiction,
        Proposal.iso,
        listed_clause(TIER).label("listed"),
        gate.label("gated"),
    ).where(*proposal_visibility_filter(TIER, now))
    raw = session.execute(stmt).all()
    views = gated_views(session, Proposal, [r.id for r in raw if r.gated], TIER) if hidden else {}
    rows: list[ServedProposal] = []
    differs = ServedNotStored()
    for r in raw:
        stored = _served_proposal(r, bool(r.listed))
        view = views.get(r.id) if r.gated else None
        if view is None:
            rows.append(stored)
            continue
        served = _served_proposal(view, bool(r.listed))
        differs.add(_proposal_counted(served), _proposal_counted(stored))
        rows.append(served)
    return rows, [r.id for r in raw], differs


def proposal_metrics(rows: Iterable[ServedProposal]) -> dict[str, Any]:
    """The proposal blocks (module docstring), plus `visible`, which they partition."""
    visible = ProposalBlock(("by_country",))
    blocks = {name: ProposalBlock(dims) for name, _states, _listed, dims in _PROPOSAL_BLOCKS}
    for row in rows:
        visible.add(row)
        for name, states, listed, _dims in _PROPOSAL_BLOCKS:
            if row.lifecycle_state in states and (listed is None or row.listed is listed):
                blocks[name].add(row)
                break
    return {"visible": visible.as_dict(), **{name: block.as_dict() for name, block in blocks.items()}}


# ------------------------------------------------------------------------------ opportunities
@dataclass(frozen=True)
class ServedOpportunity:
    """One visible opportunity's counted fields as served, and the relevance verdict over them."""

    kind: str | None
    status: str
    jurisdiction: str | None
    energy: bool


def _served_opportunity(values: Any) -> ServedOpportunity:
    return ServedOpportunity(
        kind=values.kind or None,
        status=str(values.status or "unknown"),
        jurisdiction=values.jurisdiction or None,
        energy=is_relevant_row(values.title, values.summary, values.technologies, values.identifiers),
    )


def _opportunity_counted(row: ServedOpportunity) -> dict[str, Any]:
    """As `_proposal_counted`; `relevance` is the energy verdict `relevance=energy` filters by."""
    return {"kind": row.kind, "status": row.status, "jurisdiction": row.jurisdiction, "relevance": row.energy}


def served_opportunities(
    session: Session, now: dt.datetime, hidden: frozenset[str]
) -> tuple[list[ServedOpportunity], list[Any], ServedNotStored]:
    """Every opportunity the public tier serves at `now`, as served, the ids of those counted
    (`open` or `announced`), and the records whose served values are not the stored ones."""
    hidden = _in_use(session, Opportunity, OpportunitySource, hidden)
    gate = hidden_provenance_clause(Opportunity, OPPORTUNITY_FIELDS, hidden) if hidden else sa.false()
    stmt = select(
        Opportunity.id,
        Opportunity.kind,
        Opportunity.status,
        Opportunity.jurisdiction,
        Opportunity.title,
        Opportunity.summary,
        Opportunity.technologies,
        Opportunity.identifiers,
        gate.label("gated"),
    ).where(*opportunity_visibility_filter(TIER, now))
    raw = session.execute(stmt).all()
    views = gated_views(session, Opportunity, [r.id for r in raw if r.gated], TIER) if hidden else {}
    rows: list[ServedOpportunity] = []
    counted: list[Any] = []
    differs = ServedNotStored()
    for r in raw:
        row = stored = _served_opportunity(r)
        view = views.get(r.id) if r.gated else None
        if view is not None:
            row = _served_opportunity(view)
            differs.add(_opportunity_counted(row), _opportunity_counted(stored))
        rows.append(row)
        if row.status in OPPORTUNITY_STATUSES_COUNTED:
            counted.append(r.id)
    return rows, counted, differs


def _count_block(rows: Sequence[ServedOpportunity]) -> dict[str, Any]:
    by_kind: dict[str, int] = defaultdict(int)
    by_country: dict[str, int] = defaultdict(int)
    by_state: dict[str, int] = defaultdict(int)
    for row in rows:
        by_kind[row.kind or NULL_KEY] += 1
        by_country[country_of(row.jurisdiction)] += 1
        state = us_state_of(row.jurisdiction)
        if state is not None:
            by_state[state] += 1
    return {
        "records": len(rows),
        "by_kind": dict(sorted(by_kind.items())),
        "by_country": dict(sorted(by_country.items())),
        "by_state": dict(sorted(by_state.items())),
    }


def opportunity_metrics(rows: Sequence[ServedOpportunity]) -> dict[str, Any]:
    out: dict[str, Any] = {"visible": {"records": len(rows)}}
    for status in OPPORTUNITY_STATUSES_COUNTED:
        chosen = [r for r in rows if r.status == status]
        out[status] = {"all": _count_block(chosen), "energy": _count_block([r for r in chosen if r.energy])}
    return out


# ----------------------------------------------------------------------------------- activity
def activity_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The previous complete UTC day before `now`: `[start, end)`. A naive `now` is refused
    (`_utc`)."""
    end = dt.datetime.combine(_utc(now).date(), dt.time(0), tzinfo=dt.UTC)
    return end - dt.timedelta(days=1), end


@dataclass(frozen=True)
class _EventRow:
    subject_type: str
    subject_id: Any
    event_type: str
    source_id: str | None
    trigger: str | None
    reason: str | None


def _left_out_reason(run_id: Any, released: bool, baseline: bool) -> str | None:
    """`None` for a genuine change, else the first reason in `LEFT_OUT_REASONS` that applies. The
    run's trigger is not a reason: the label changes nothing the connector fetches or diffs
    against (`activity_rule`; docs/24-trends.md §6)."""
    if run_id is None:
        return "no_source_run"
    if released:
        return "released_hold"
    if baseline:
        return "first_load"
    return None


def window_events(session: Session, now: dt.datetime) -> list[_EventRow]:
    """Every event the public tier serves at `now` that was observed in the activity window, with
    the reason it is left out of the count, if any (`activity_rule`). A source's first run is its
    earliest `source_run` that promoted output (`status = ok`), whatever its trigger label; a source
    with none has no baseline to compare against, so all its events are first-load events."""
    start, end = activity_window(now)
    first_ok = (
        select(SourceRun.source_id.label("source_id"), func.min(SourceRun.started_at).label("first_started"))
        .where(SourceRun.status == "ok")
        .group_by(SourceRun.source_id)
        .subquery("first_ok")
    )
    run = aliased(SourceRun, name="event_run")
    baseline = sa.or_(first_ok.c.first_started.is_(None), run.started_at <= first_ok.c.first_started)
    stmt = (
        select(
            Event.subject_type,
            Event.subject_id,
            Event.event_type,
            Event.source_id,
            run.id.label("run_id"),
            run.trigger,
            run.released_at.is_not(None).label("released"),
            baseline.label("baseline"),
        )
        .select_from(Event)
        .outerjoin(run, run.id == Event.run_id)
        .outerjoin(first_ok, first_ok.c.source_id == run.source_id)
        .where(*event_visibility_filter(TIER, now), Event.observed_at >= start, Event.observed_at < end)
    )
    return [
        _EventRow(
            r.subject_type,
            r.subject_id,
            r.event_type,
            r.source_id,
            r.trigger if r.run_id is not None else None,
            _left_out_reason(r.run_id, bool(r.released), bool(r.baseline)),
        )
        for r in session.execute(stmt).all()
    ]


@dataclass
class _Changes:
    """Counted events and the distinct subjects they changed, in total and by event type: ten
    events on one project are ten events and one subject."""

    events: int = 0
    subjects: set[Any] = field(default_factory=set)
    by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    subjects_by_type: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))

    def add(self, ev: _EventRow) -> None:
        subject = (ev.subject_type, ev.subject_id)
        self.events += 1
        self.subjects.add(subject)
        self.by_type[ev.event_type] += 1
        self.subjects_by_type[ev.event_type].add(subject)

    def as_dict(self) -> dict[str, Any]:
        """Counts only: the subjects' ids never leave this object."""
        return {
            "events": self.events,
            "subjects": len(self.subjects),
            "by_event_type": dict(sorted(self.by_type.items())),
            "subjects_by_event_type": {k: len(v) for k, v in sorted(self.subjects_by_type.items())},
        }


def activity_metrics(events: Sequence[_EventRow], now: dt.datetime) -> dict[str, Any]:
    start, end = activity_window(now)
    total = _Changes()
    by_subject: dict[str, _Changes] = defaultdict(_Changes)
    by_trigger: dict[str, int] = defaultdict(int)
    left_out = dict.fromkeys(LEFT_OUT_REASONS, 0)
    for ev in events:
        if ev.reason is not None:
            left_out[ev.reason] += 1
            continue
        total.add(ev)
        by_subject[ev.subject_type].add(ev)
        by_trigger[ev.trigger or NULL_KEY] += 1
    return {
        "rule": ACTIVITY_RULE,
        "window": {"from": iso(start), "to_exclusive": iso(end)},
        "visible_events": len(events),
        "counted": {
            **total.as_dict(),
            "by_subject_type": {
                subject: changes.as_dict() for subject, changes in sorted(by_subject.items())
            },
            "by_run_trigger": dict(sorted(by_trigger.items())),
        },
        "left_out": left_out,
    }


# ------------------------------------------------------------------------------------ sources
_ID_CHUNK = 10_000


def _link_counts(
    session: Session, link_model: Any, fk: Any, ids: Sequence[Any]
) -> dict[str, tuple[int, dt.datetime | None]]:
    """Per source the public tier may read: how many of `ids`' active links name it, and its latest
    retrieval among them (the clauses `visibility.visible_source_link_filter` applies)."""
    stmt = (
        select(link_model.source_id, func.count(), func.max(link_model.retrieved_at))
        .join(Source, Source.id == link_model.source_id)
        .join(Licence, Licence.id == Source.licence_id)
        .where(
            fk.in_(sa.bindparam("ids", expanding=True)),
            link_model.active.is_(True),
            Source.publish_state.in_(permitted_source_states(TIER)),
            Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        )
        .group_by(link_model.source_id)
    )
    out: dict[str, tuple[int, dt.datetime | None]] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        for source_id, count, latest in session.execute(stmt, {"ids": list(ids[start : start + _ID_CHUNK])}):
            seen = out.get(source_id)
            if seen is None:
                out[source_id] = (int(count), latest)
                continue
            newest = max((t for t in (seen[1], latest) if t is not None), default=None)
            out[source_id] = (seen[0] + int(count), newest)
    return out


def contributing_sources(
    session: Session,
    proposal_ids: Sequence[Any],
    opportunity_ids: Sequence[Any],
    events: Sequence[_EventRow],
) -> list[dict[str, Any]]:
    """Every source behind a counted figure, with its licence (the credit the public pages print)."""
    proposals = _link_counts(session, ProposalSource, ProposalSource.proposal_id, proposal_ids)
    opportunities = _link_counts(
        session, OpportunitySource, OpportunitySource.opportunity_id, opportunity_ids
    )
    counted_events: dict[str, int] = defaultdict(int)
    for ev in events:
        if ev.reason is None and ev.source_id is not None:
            counted_events[ev.source_id] += 1
    ids = sorted(set(proposals) | set(opportunities) | set(counted_events))
    if not ids:
        return []
    rows = session.execute(
        select(Source, Licence).join(Licence, Licence.id == Source.licence_id).where(Source.id.in_(ids))
    ).all()
    out = []
    for source, licence in sorted(rows, key=lambda pair: pair[0].id):
        times = [
            t
            for t in (proposals.get(source.id, (0, None))[1], opportunities.get(source.id, (0, None))[1])
            if t
        ]
        out.append(
            {
                "source_id": source.id,
                "name": source.name,
                "operator": source.operator,
                "licence_id": licence.id,
                "licence_name": licence.name,
                "licence_url": licence.url,
                "reuse_class": licence.reuse_class,
                "attribution_text": source.attribution_text or licence.attribution_text,
                "requires_link_back": bool(licence.requires_link_back),
                "proposal_links": proposals.get(source.id, (0, None))[0],
                "opportunity_links": opportunities.get(source.id, (0, None))[0],
                "counted_events": counted_events.get(source.id, 0),
                "retrieved_at_max": iso(max(times)) if times else None,
            }
        )
    return out


# ----------------------------------------------------------------------------------- document
def _queries(now: dt.datetime) -> dict[str, str]:
    """The public list query behind each block (module docstring)."""
    start, end = activity_window(now)
    active = ",".join(ACTIVE_STATES)
    out = {
        "proposals.visible": "/v1/proposals",
        "proposals.active": f"/v1/proposals?lifecycle_state={active}&listed=true",
        "proposals.active_not_listed": f"/v1/proposals?lifecycle_state={active}&listed=false",
        "proposals.withdrawn": f"/v1/proposals?lifecycle_state={','.join(WITHDRAWN_STATES)}",
        "proposals.built": f"/v1/proposals?lifecycle_state={','.join(BUILT_STATES)}",
        "proposals.unknown": f"/v1/proposals?lifecycle_state={','.join(UNKNOWN_STATES)}",
        "opportunities.visible": f"/v1/opportunities?status={','.join(OPPORTUNITY_STATUSES)}",
        "activity.visible_events": (
            f"/v1/events?observed_at[from]={iso(start)}"
            f"&observed_at[to]={iso(end - dt.timedelta(microseconds=1))}"
        ),
    }
    for status in OPPORTUNITY_STATUSES_COUNTED:
        out[f"opportunities.{status}.all"] = f"/v1/opportunities?status={status}"
        out[f"opportunities.{status}.energy"] = f"/v1/opportunities?status={status}&relevance=energy"
    return dict(sorted(out.items()))


def posture_of(classes: Iterable[str]) -> str:
    """The platform posture a set of publishable classes is (`services/posture.py`)."""
    return "noncommercial" if "noncommercial" in set(classes) else "commercial"


def visibility_posture(
    withheld_sources: int,
    proposals_differ: ServedNotStored | None = None,
    opportunities_differ: ServedNotStored | None = None,
) -> dict[str, Any]:
    """The posture the figures were computed under: the tier, the classes the predicate publishes
    (read from the predicate itself, so it is what was applied, not what the environment says now),
    how many registered sources the tier may not read, and how many visible records were counted
    by a served value that is not the stored one (`ServedNotStored`)."""
    return {
        "tier": TIER,
        "platform_posture": posture_of(PUBLISHABLE_REUSE_CLASSES),
        "publishable_reuse_classes": sorted(PUBLISHABLE_REUSE_CLASSES),
        "withheld_sources": withheld_sources,
        "served_not_stored": {
            "proposals": (proposals_differ or ServedNotStored()).as_dict(),
            "opportunities": (opportunities_differ or ServedNotStored()).as_dict(),
        },
        "predicate": "services/api/visibility.py at the public tier; values as served (GatedRecord)",
        "active_rule": "announced through under_construction, listed by a register the tier may read",
        "opportunity_relevance": f"relevance=energy, rules {RELEVANCE_VERSION} (services/match/relevance.py)",
    }


def _begin_consistent_read(session: Session) -> None:
    """On Postgres, read every statement from one snapshot (REPEATABLE READ, read only), so a load
    committing mid-computation cannot leave one block counting rows another does not. SQLite reads
    one database file under one transaction already. A session already in a transaction is used as
    it is."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql" and not session.in_transaction():
        session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )


def _bucket_filters() -> dict[str, str | list[str] | None]:
    """`BUCKET_FILTERS` as the document holds it (JSON has no tuple)."""
    return {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in sorted(BUCKET_FILTERS.items())
    }


def compute_snapshot(session: Session, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """The document for `now`'s UTC day (module docstring, "Convention"). Reads only. `now` must be
    timezone-aware (`_utc`); omitted, it is the current time."""
    at = dt.datetime.now(dt.UTC) if now is None else _utc(now)
    _begin_consistent_read(session)
    hidden = source_split(session, TIER)[1]
    proposals, proposal_ids, proposals_differ = served_proposals(session, at, hidden)
    opportunities, opportunity_ids, opportunities_differ = served_opportunities(session, at, hidden)
    events = window_events(session, at)
    sources = contributing_sources(session, proposal_ids, opportunity_ids, events)
    names = [s["name"] for s in sources]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "as_of": at.date().isoformat(),
        "generated_at": iso(at),
        "convention": (
            "State as the public tier served it at generated_at on as_of; activity over the previous "
            "UTC day (activity.window)."
        ),
        "visibility": visibility_posture(len(hidden), proposals_differ, opportunities_differ),
        "queries": _queries(at),
        "bucket_filters": _bucket_filters(),
        "activity_rule": activity_rule(),
        "metrics": {
            "proposals": proposal_metrics(proposals),
            "opportunities": opportunity_metrics(opportunities),
            "activity": activity_metrics(events, at),
        },
        "sources": sources,
        "attribution_line": f"Sources: {'; '.join(names)}." if names else "Sources: none.",
    }


#: `figure`'s default: raise when the path is absent.
_REQUIRED: Any = object()


def figure(document: Mapping[str, Any], path: str, default: Any = _REQUIRED) -> Any:
    """A value of `document["metrics"]` by dotted path (`proposals.active.total.records`), for the
    reader and the tests. A path the document does not hold raises `KeyError`, or answers `default`
    when one is given: a version-1 document has no `by_state` on the withdrawn block, and that is
    a figure not recorded, never a zero."""
    node: Any = document["metrics"]
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            if default is _REQUIRED:
                raise KeyError(path)
            return default
        node = node[part]
    return node


__all__ = [
    "ACTIVITY_RULE",
    "DOCUMENT_KIND",
    "NULL_KEY",
    "READABLE_SCHEMA_VERSIONS",
    "SCHEMA_VERSION",
    "ServedNotStored",
    "ServedOpportunity",
    "ServedProposal",
    "Tally",
    "activity_window",
    "compute_snapshot",
    "country_of",
    "figure",
    "us_state_of",
]
