"""Matches a stored `saved_search.query`/`webhook_endpoint.query` dict (docs/21 §3.15's "filter
definition, not results") against a `Proposal`, `Opportunity` or `Event` row.

**Decision** (services/README.md "Pro tier and alerts"): this does not reuse
`services/api/records.py`'s `_apply_proposal_filters` et al. directly -- those build a SQL `WHERE`
clause, and the alert/webhook evaluator needs a predicate over one already-loaded row ("given new
change events, match them against saved searches"). This module re-implements the list endpoints'
filter grammar (docs/23 §7) as a predicate per row instead.

**The whole grammar, pinned by a parity test** (2026-09-27). Until then it covered a named subset
(kind, technology, jurisdiction, lifecycle_state, iso, source_id, capacity, `q` on the name
only), so a saved search for "storage over 500 MW in US-TX" alerted on every storage proposal
anywhere. It now implements every key `GET /v1/proposals`, `GET /v1/opportunities` and
`GET /v1/events` accept, with the SQL's semantics exactly: comma-separated OR within a facet, AND
across facets, inclusive range bounds, a NULL column matching no value and no bound, a proposal
with no location matching no location filter (`state`, `county_fips`, `placement`), `placement`
judged on the grade the row is *served* at, `slipped`/`slip_bucket` through the same
`services/api/slippage.py` functions, an opportunity query with no `status` meaning `status=open`
(the list's default), `q` over the name, the visible sponsor/issuer and the visible source
record ids with SQL `LIKE` wildcards, and on events `changed_key` (any-of over `changed_keys`)
and inclusive `observed_at[from|to]` bounds (2026-09-27, lane E14); on proposals and opportunities
`sponsor_id`/`issuer_id` (a hidden organisation matches no id), `storage_mwh[gte]`,
`capacity_sought_mw[gte]`, `open_at[from|to]`, `budget_currency` and `budget_amount[gte]` (one currency,
never across), and the `first_seen`/`last_changed` windows (2026-09-27, lane E15); on proposals
`interconnection_point_id` (a point whose register the owner's tier may not see matches no id; 2026-09-28,
lane G1). `GET /v1/events` refuses
`q`, so an event query never carries one that means anything. Value parsing and validation go
through the functions the list endpoints call (`number_filter`, `instant_filter`, `date_filter`,
`currency_values`, `budget_bound`, `placement_grades`, `slip_params`, `changed_key_values`).
`tests/test_saved_search_parity.py` asserts, over a fixture store and a generated query set, at the
public and Pro tiers, that the ids the list returns equal the ids this module accepts, and that
every filter key the list accepts is exercised -- so a filter added to the list without a matching
arm here fails CI.

Stored queries are **not** re-validated on read (saved-search/webhook creation validates keys since
2026-09-27; older rows may carry anything): an unknown key is ignored, as it always was, and a
value the list endpoint would answer with a 400 makes the query match nothing (fail closed -- an
alert on every record is the failure this module exists to prevent).

**`source_id` matches only through links the owner's tier may see** (2026-09-26; docs/21 §8
item 3): a saved search or webhook on a gated source's id must not match a record that is visible
through another source, or the alert itself confirms the gated link exists. `entitlement` is the
owner's tier (a Pro owner still matches an `api_only` link) and defaults to `public`, the strictest,
so a caller that forgets it fails closed. The `q` source-record-id arm and the sponsor/issuer arm
are gated the same way.

**Dialect notes.** Where SQLite and Postgres disagree the Postgres reading is implemented: `q`
lower-cases non-ASCII text (SQLite's `lower()` is ASCII-only), and date-time bounds compare
instants (the list side normalises its bound to UTC for the same reason, `instant_filter`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from typing import Any

from services.api import slippage
from services.api.errors import ProblemError
from services.api.params import csv_param
from services.api.records import (
    PLACEMENT_REGION_PRECISIONS,
    RECORD_TIME_BOUNDS,
    budget_bound,
    currency_values,
    date_filter,
    instant_filter,
    number_filter,
    placement_grades,
    slip_params,
)
from services.api.resource_queries import changed_key_values
from services.api.visibility import interconnection_point_visible, organization_visible, visible_source_links
from services.db.models import Event, Location, Opportunity, OpportunitySource, Proposal, ProposalSource
from services.ids import parse_public_id, public_id

#: The `instance` a stored query's parse error would carry; never shown (the matcher fails closed).
_INSTANCE = "saved-search"


def query_params(query: Mapping[str, Any]) -> dict[str, str]:
    """The stored dict as the query string the list endpoint would receive -- the same rendering
    as `services/api/resource_queries.py::synthetic_request` (lists join with commas, `None` and
    empty list items drop, booleans render `true`/`false`, everything else `str()`), so a value
    reads the same on both sides."""
    out: dict[str, str] = {}
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            joined = ",".join(str(v) for v in value if v is not None and str(v) != "")
            if joined:
                out[key] = joined
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def _like(raw: str) -> re.Pattern[str]:
    """`lower(column) LIKE '%<lower(raw)>%'` (records.py `q`): `%` is any run, `_` any one
    character, no escape character."""
    body = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in raw.lower())
    return re.compile(f".*{body}.*", re.DOTALL)


def _like_matches(pattern: re.Pattern[str], text: str | None) -> bool:
    return text is not None and pattern.fullmatch(text.lower()) is not None


def _text_matches(
    raw: str,
    name: str,
    org: Any,
    links: Iterable[ProposalSource | OpportunitySource],
    entitlement: str,
) -> bool:
    pattern = _like(raw)
    if _like_matches(pattern, name):
        return True
    if org is not None and organization_visible(org) and _like_matches(pattern, org.name_canonical):
        return True
    return any(
        _like_matches(pattern, link.source_record_id) for link in visible_source_links(links, entitlement)
    )


def _in(value: Any, raw: str) -> bool:
    """`column IN (a, b, ...)`: a NULL column is in no list, and an empty list holds nothing."""
    return value is not None and value in csv_param(raw)


def _source_matches(links: Iterable[ProposalSource | OpportunitySource], raw: str, entitlement: str) -> bool:
    wanted = set(csv_param(raw))
    return any(link.source_id in wanted for link in visible_source_links(links, entitlement))


def _as_utc(value: dt.datetime) -> dt.datetime:
    """A stored instant as aware UTC: SQLite hands `DateTime(timezone=True)` back naive, and the
    store writes UTC."""
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _at_least(value: Any, bound: float) -> bool:
    """`column >= bound` for a nullable numeric column: NULL satisfies no bound."""
    return value is not None and float(value) >= bound


def _org_matches(org: Any, raw: str) -> bool:
    """`sponsor_id=`/`issuer_id=` (records.py `visible_organization_ids`): the linked organisation is
    one of the named public ids *and* visible; a taken-down organisation matches no id, as in SQL."""
    return org is not None and organization_visible(org) and org.public_id in csv_param(raw)


def _point_matches(point: Any, raw: str, entitlement: str) -> bool:
    """`interconnection_point_id=` (records.py): the proposal connects at one of the named points
    *and* that point's register is visible at the owner's tier; a point named by a gated register
    matches no id, as in SQL."""
    return (
        point is not None
        and interconnection_point_visible(point, entitlement)
        and point.public_id in csv_param(raw)
    )


def _time_bounds_match(record: Proposal | Opportunity, qp: Mapping[str, str]) -> bool:
    """`first_seen[from|to]`, `last_changed[from|to]`, `updated_since`: records.py
    `RECORD_TIME_BOUNDS`, inclusive, compared as UTC instants. A saved search cannot store
    `updated_since` (services/api/pro.py refuses it); a row stored before that rule reads it as the
    list does."""
    for name, column, op in RECORD_TIME_BOUNDS:
        if v := qp.get(name):
            bound = instant_filter(name, v, _INSTANCE)
            stored = getattr(record, column)
            if stored is None:
                return False
            value = _as_utc(stored)
            if (op == "gte" and value < bound) or (op == "lte" and value > bound):
                return False
    return True


def _served_grade(loc: Location) -> str | None:
    """The placement grade a location is served at: `records.py::_apply_placement_filter`'s three
    SQL arms, row by row (an `exact` point whose licence forbids raw publication serves as
    `region`, docs/04 D-9)."""
    if loc.precision == "exact":
        permitted = loc.licence is not None and loc.licence.allows_raw_publication is True
        return "exact" if permitted else "region"
    if loc.precision in PLACEMENT_REGION_PRECISIONS:
        return "region"
    if loc.precision == "unknown":
        return "none"
    return None


# ------------------------------------------------------------------------------------ proposals
def _proposal_matches(proposal: Proposal, qp: Mapping[str, str], entitlement: str) -> bool:
    for key, value in (
        ("kind", proposal.kind),
        ("technology", proposal.technology),
        ("lifecycle_state", proposal.lifecycle_state),
        ("jurisdiction", proposal.jurisdiction),
        ("iso", proposal.iso),
    ):
        if (v := qp.get(key)) and not _in(value, v):
            return False
    if (v := qp.get("source_id")) and not _source_matches(proposal.sources, v, entitlement):
        return False
    capacity = float(proposal.capacity_mw) if proposal.capacity_mw is not None else None
    if v := qp.get("capacity_mw[gte]"):
        bound = number_filter("capacity_mw[gte]", v, _INSTANCE)
        if capacity is None or not capacity >= bound:
            return False
    if v := qp.get("capacity_mw[lte]"):
        bound = number_filter("capacity_mw[lte]", v, _INSTANCE)
        if capacity is None or not capacity <= bound:
            return False
    if (v := qp.get("storage_mwh[gte]")) and not _at_least(
        proposal.storage_mwh, number_filter("storage_mwh[gte]", v, _INSTANCE)
    ):
        return False
    if (v := qp.get("sponsor_id")) and not _org_matches(proposal.sponsor, v):
        return False
    if (v := qp.get("interconnection_point_id")) and not _point_matches(
        proposal.interconnection_point, v, entitlement
    ):
        return False
    if not _time_bounds_match(proposal, qp):
        return False
    if (v := qp.get("slug")) and proposal.slug != v:
        return False
    loc = proposal.location
    if (v := qp.get("county_fips")) and (loc is None or not _in(loc.county_fips, v)):
        return False
    if (v := qp.get("state")) and (loc is None or not _in(loc.state_code, v)):
        return False
    slip = slip_params(qp.get("slipped"), qp.get("slip_bucket"), _INSTANCE)
    if slip is not None:
        slipped, buckets = slip
        days = slippage.slip_days(
            proposal.lifecycle_state, proposal.proposed_online_date, on=slippage.today()
        )
        if buckets:
            if days is None or slippage.slip_bucket(days) not in buckets:
                return False
        elif (days is not None) != slipped:
            return False
    grades = placement_grades(qp.get("placement"), None, _INSTANCE)
    if grades is not None and (loc is None or _served_grade(loc) not in grades):
        return False
    if (v := qp.get("q")) and not _text_matches(
        v, proposal.name_canonical, proposal.sponsor, proposal.sources, entitlement
    ):
        return False
    return True


def proposal_matches_query(proposal: Proposal, query: dict[str, Any], entitlement: str = "public") -> bool:
    try:
        return _proposal_matches(proposal, query_params(query), entitlement)
    except (ProblemError, ValueError):
        return False


# -------------------------------------------------------------------------------- opportunities
def _opportunity_matches(opportunity: Opportunity, qp: Mapping[str, str], entitlement: str) -> bool:
    # The list's default: no `status` (or one that parses to nothing) means `status=open`.
    if opportunity.status not in (csv_param(qp.get("status")) or ["open"]):
        return False
    for key, value in (("kind", opportunity.kind), ("jurisdiction", opportunity.jurisdiction)):
        if (v := qp.get(key)) and not _in(value, v):
            return False
    if v := qp.get("technologies"):
        # Any-of, and an all-source opportunity (empty array) matches every value.
        have = list(opportunity.technologies or [])
        if have and not set(have) & set(csv_param(v)):
            return False
    if (v := qp.get("source_id")) and not _source_matches(opportunity.sources, v, entitlement):
        return False
    due = _as_utc(opportunity.due_at) if opportunity.due_at is not None else None
    if v := qp.get("due_at[from]"):
        bound = instant_filter("due_at[from]", v, _INSTANCE)
        if due is None or due < bound:
            return False
    if v := qp.get("due_at[to]"):
        bound = instant_filter("due_at[to]", v, _INSTANCE)
        if due is None or due > bound:
            return False
    if (v := qp.get("issuer_id")) and not _org_matches(opportunity.issuer, v):
        return False
    for name, op in (("open_at[from]", "gte"), ("open_at[to]", "lte")):
        if v := qp.get(name):
            day = date_filter(name, v, _INSTANCE)
            opened = opportunity.open_at
            if opened is None or (op == "gte" and opened < day) or (op == "lte" and opened > day):
                return False
    if (v := qp.get("capacity_sought_mw[gte]")) and not _at_least(
        opportunity.capacity_sought_mw, number_filter("capacity_sought_mw[gte]", v, _INSTANCE)
    ):
        return False
    if (v := qp.get("budget_currency")) and opportunity.budget_currency not in currency_values(v, _INSTANCE):
        return False
    budget = budget_bound(qp.get("budget_amount[gte]"), qp.get("budget_currency"), _INSTANCE)
    if budget is not None:
        amount, currency = budget
        if opportunity.budget_currency != currency or not _at_least(opportunity.budget_amount, amount):
            return False
    if not _time_bounds_match(opportunity, qp):
        return False
    if (v := qp.get("slug")) and opportunity.slug != v:
        return False
    if (v := qp.get("q")) and not _text_matches(
        v, opportunity.title, opportunity.issuer, opportunity.sources, entitlement
    ):
        return False
    return True


def opportunity_matches_query(
    opportunity: Opportunity, query: dict[str, Any], entitlement: str = "public"
) -> bool:
    try:
        return _opportunity_matches(opportunity, query_params(query), entitlement)
    except (ProblemError, ValueError):
        return False


# ---------------------------------------------------------------------------------------- events
_SUBJECT_PREFIXES = {"prop": "proposal", "opp": "opportunity"}


def _subject_matches(event: Event, raw: str) -> bool:
    """`services/api/resource_queries.py::resolve_subject`: a `prop_`/`opp_` public id names that
    record, anything else names nothing. Decoded rather than looked up (public ids are the
    Crockford form of the internal uuid, `services/ids.py`); re-encoding rejects a non-canonical
    spelling the SQL equality would not find."""
    prefix = raw.split("_", 1)[0]
    subject_type = _SUBJECT_PREFIXES.get(prefix)
    subject_uuid = parse_public_id(prefix, raw) if subject_type else None
    return (
        subject_uuid is not None
        and public_id(prefix, subject_uuid) == raw
        and event.subject_type == subject_type
        and event.subject_id == subject_uuid
    )


def _event_matches(event: Event, qp: Mapping[str, str]) -> bool:
    for key, value in (
        ("subject_type", event.subject_type),
        ("event_type", event.event_type),
        ("source_id", event.source_id),
    ):
        if (v := qp.get(key)) and not _in(value, v):
            return False
    if (v := qp.get("subject_id")) and not _subject_matches(event, v):
        return False
    if v := qp.get("since"):
        if v.isdigit():
            if not event.seq > int(v):
                return False
        elif not _as_utc(event.observed_at) > instant_filter("since", v, _INSTANCE):
            return False
    if (v := qp.get("changed_key")) and not set(event.changed_keys or []) & set(
        changed_key_values(v, _INSTANCE)
    ):
        return False
    if (v := qp.get("observed_at[from]")) and _as_utc(event.observed_at) < instant_filter(
        "observed_at[from]", v, _INSTANCE
    ):
        return False
    if (v := qp.get("observed_at[to]")) and _as_utc(event.observed_at) > instant_filter(
        "observed_at[to]", v, _INSTANCE
    ):
        return False
    return True


def event_matches_query(event: Event, query: dict[str, Any]) -> bool:
    try:
        return _event_matches(event, query_params(query))
    except (ProblemError, ValueError):
        return False


def matches_query(
    entity: str, row: Proposal | Opportunity | Event, query: dict[str, Any], entitlement: str = "public"
) -> bool:
    if entity == "proposal" and isinstance(row, Proposal):
        return proposal_matches_query(row, query, entitlement)
    if entity == "opportunity" and isinstance(row, Opportunity):
        return opportunity_matches_query(row, query, entitlement)
    if entity == "event" and isinstance(row, Event):
        return event_matches_query(row, query)
    return False
