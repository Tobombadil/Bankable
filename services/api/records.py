"""Public record surface: `v1/proposals` and `v1/opportunities` (list, geo, detail, events,
sources), split out of `services/api/app.py` (docs/42-backend-review-2026-09-26.md, lane L5).

Also carries the filter stack (`_apply_proposal_filters`, `_apply_placement_filter`, `_apply_slip_filter`,
`_opportunity_query_with_filters`, `_proposal_query_with_filters`, `_opportunity_technologies_filter`,
`_proposal_licence_rows`, `_opportunity_licence_rows`, `PROPOSAL_FILTERS`, `OPPORTUNITY_FILTERS`,
`PROPOSAL_SORT_ALLOWLIST`, `OPPORTUNITY_SORT_ALLOWLIST`) that `services/api/app.py`'s own
organisation-scoped routes (`list_organization_proposals`, `list_organization_opportunities`) and
feeds (`feed_proposals`, `feed_opportunities`) also call -- this module never imports
`services.api.app` (it would cycle with `app.py`'s `include_router` on this module's `router`), so
those names live here and `app.py` imports back the ones its own routes still need.

`LIST_COMMON`, `sort_spec` and `int_param` are generic pagination/sort helpers with a shared home
of their own, `services/api/params.py` (fan-in from across `services.api`, no cycle risk); this
module imports them from there rather than owning a copy.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal, NamedTuple, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import (
    InstrumentedAttribute,
    Session,
    lazyload,
    load_only,
    noload,
    selectinload,
)

from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST
from services.api.deps import get_db
from services.api.errors import validation_error
from services.api.geo import GeoLicence, GeoRow, build_geo_feature_collection
from services.api.merged_redirect import merged_redirect_or_404
from services.api.pagination import clamp_limit, paginate
from services.api.params import LIST_COMMON, check_allowed, csv_param, int_param, sort_spec, wants_csv
from services.api.proposal_members import merge_history, proposal_members
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_from_source_aggregates,
    licence_summary_row,
    record_redactions,
    serialize_event,
    serialize_opportunity,
    serialize_proposal,
)
from services.api.slippage import SLIP_BUCKETS, slip_filter
from services.api.slippage import today as slip_today
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    GatedRecord,
    event_visibility_filter,
    gated_record,
    gated_views,
    hidden_provenance_clause,
    interconnection_point_source_filter,
    location_exact_permitted,
    opportunity_visibility_filter,
    organization_visibility_filter,
    permitted_source_states,
    proposal_visibility_filter,
    source_split,
    visible_source_link_filter,
    visible_source_links,
)
from services.db.models import (
    Event,
    InterconnectionPoint,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)

router = APIRouter()


# ------------------------------------------------------------------------------------- proposals
PROPOSAL_FILTERS = {
    "kind",
    "technology",
    "lifecycle_state",
    "jurisdiction",
    "iso",
    "state",
    "source_id",
    "capacity_mw[gte]",
    "capacity_mw[lte]",
    "slug",
    "county_fips",
    "placement",
    # Schedule slippage (services/api/slippage.py, docs/22 §18): derived at read time from
    # `proposed_online_date` against the current date, never stored.
    "slipped",
    "slip_bucket",
    # 2026-09-27 (lane E15): documented since Sprint 2 and refused until now (services/README.md
    # open decision 7). `sponsor_id` names a *public* organisation; a taken-down one selects
    # nothing, exactly as an id that never existed (`visible_organization_ids`).
    "sponsor_id",
    "storage_mwh[gte]",
    "first_seen[from]",
    "first_seen[to]",
    "last_changed[from]",
    "last_changed[to]",
    # 2026-09-28 (lane G1): proposals connecting at a grid interconnection point, by its public id
    # (docs/21 §3.24). A point whose register the tier may not see selects nothing, exactly as an
    # unknown id (`interconnection_point_source_filter`).
    "interconnection_point_id",
}
#: Filters the list, map and feed apply that a saved search or webhook may **not** carry.
#: `updated_since` is the incremental-sync cursor (docs/23 §7; `services/api/bulk.py`): "rows whose
#: `last_changed` is at or after this instant", identical to `last_changed[from]`. An alert already
#: delivers only what changed after its own watermark, and the record it evaluates has just changed,
#: so a stored `updated_since` would either match every change (a past bound) or none until the
#: clock passes it (a future one) -- a cursor frozen into a filter. `validate_saved_search_query`
#: refuses it with a message pointing at `last_changed[from]` (services/api/pro.py).
SYNC_FILTERS = {"updated_since"}
PROPOSAL_SORT_ALLOWLIST = {"last_changed", "first_seen", "capacity_mw", "name_canonical"}

#: ADR 0008, docs/21 §3.7: the three region-grade precisions a `placement=region` filter expands
#: to, and the `none` grade's own precision value. Mirrors `services/api/geo.py::REGION_PRECISIONS`
#: (not imported from there to avoid this module depending on `geo.py` for a plain tuple it only
#: needs for one `IN` clause; both are asserted equal in `services/api/test_placement.py`).
PLACEMENT_REGION_PRECISIONS = ("county_centroid", "state_centroid", "country_centroid")


def number_filter(name: str, value: str, instance: str) -> float:
    """A range bound such as `capacity_mw[gte]`. Not a finite number is a `400 validation_error`
    naming the parameter (before 2026-09-27 `float()` raised and the route answered 500). The alert
    matcher (`services/alerts/matching.py`) parses stored bounds through this same function."""
    try:
        number = float(value)
    except ValueError as exc:
        raise validation_error(name, f"{name} must be a number", instance) from exc
    if not math.isfinite(number):
        raise validation_error(name, f"{name} must be a finite number", instance)
    return number


def instant_filter(name: str, value: str, instance: str) -> dt.datetime:
    """A date-time bound such as `due_at[from]`, as an aware UTC instant: RFC 3339 with `Z` or an
    offset; a bare date or offset-less time is read as UTC. Normalised to UTC before it is bound
    because SQLite compares the stored wall-clock text and ignores the offset, while Postgres
    compares instants; with the bound in UTC both agree (the store writes UTC). Unparseable is a
    `400 validation_error` (before 2026-09-27, a 500). Shared with the alert matcher."""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise validation_error(name, f"{name} must be an RFC 3339 date-time", instance) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def date_filter(name: str, value: str, instance: str) -> dt.date:
    """A calendar-date bound such as `open_at[from]` (api/openapi.yaml `format: date`), for a `DATE`
    column: `YYYY-MM-DD` only. A date-time is refused rather than truncated, because which day
    `2026-01-01T23:00:00-05:00` names depends on a time zone the column does not carry. Unparseable is
    a `400 validation_error`. Shared with the alert matcher."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise validation_error(name, f"{name} must be a date (YYYY-MM-DD)", instance) from exc


def visible_organization_ids(values: list[str]) -> sa.Select[Any]:
    """The internal ids of the organisations `sponsor_id=`/`issuer_id=` name, restricted to the ones a
    non-admin caller may see (`organization_visibility_filter`, identical on every tier). A taken-down
    organisation's id therefore selects nothing -- the same empty page as an id that never existed,
    so the filter cannot be used to confirm a hidden organisation exists or which records it is
    linked to (docs/21 §8 item 3; the 2026-09-27 takedown decision in docs/00-PLAN.md). The match is
    on the exact organisation, not its ownership tree (`/v1/organizations/{id}/proposals?scope=`
    widens) and not a merge survivor (the detail route does not follow merges either)."""
    return select(Organization.id).where(
        Organization.public_id.in_(values), *organization_visibility_filter()
    )


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def currency_values(raw: str, instance: str) -> list[str]:
    """`?budget_currency=EUR,GBP` as ISO 4217 codes (upper case, three letters, as stored); anything
    else is a `400 validation_error`. Shared with the alert matcher."""
    values = csv_param(raw)
    for value in values:
        if not _CURRENCY_RE.fullmatch(value):
            raise validation_error(
                "budget_currency", "budget_currency values are ISO 4217 codes such as USD or EUR", instance
            )
    return values


def budget_bound(raw_amount: str | None, raw_currency: str | None, instance: str) -> tuple[float, str] | None:
    """`(amount, currency)` for `budget_amount[gte]`, or `None` when it is absent.

    Budgets are stored in the notice's own currency and never converted (api/openapi.yaml
    `BudgetAmountGte`); the 2026-09-27 dev store holds nine (EUR, USD, PLN, CZK, RON, SEK, NOK, HUF,
    DKK). Comparing `budget_amount >= 1000000` across them would rank a CZK 1,000,000 grant (about
    EUR 40,000) with a EUR 1,000,000 one. So the bound is only defined within one stated currency:
    `budget_amount[gte]` requires `budget_currency` naming exactly one code, and selects rows in
    that currency with an amount at or above the bound. Without it, or with several, it is a
    `400 validation_error` naming `budget_amount[gte]` -- never a silent cross-currency compare."""
    if not raw_amount:
        return None
    amount = number_filter("budget_amount[gte]", raw_amount, instance)
    currencies = currency_values(raw_currency, instance) if raw_currency else []
    if len(currencies) != 1:
        raise validation_error(
            "budget_amount[gte]",
            "budget_amount[gte] compares amounts in one currency and never converts: pass exactly one "
            "budget_currency (an ISO 4217 code such as EUR)",
            instance,
        )
    return amount, currencies[0]


def check_budget_sort(raw_sort: str | None, raw_currency: str | None, instance: str) -> None:
    """`sort=budget_amount` / `-budget_amount` is defined only beside exactly one `budget_currency`
    (2026-09-27, lane E16): the same rule as `budget_bound`, for the same reason. Budgets are stored in
    the notice's own currency and never converted, so ordering the dev store's nine currencies by the
    bare number put HUF 3,449,282,316 (about EUR 8.9 million) above EUR 294,000,000. With one currency,
    `budget_currency` also narrows the rows to it, so the page is ordered within that currency. A
    `budget_amount` token anywhere in the comma list counts, not only the first. Otherwise a
    `400 validation_error` naming `sort`. Called by every surface that takes an opportunity sort: the
    list (before its `Accept: text/csv` branch), `/v1/organizations/{id}/opportunities`, an export's
    stored query (`resource_queries.validate_export_query`) and the export's own ordering."""
    if not raw_sort:
        return
    fields = {token.strip().lstrip("-") for token in raw_sort.split(",")}
    if "budget_amount" not in fields:
        return
    currencies = currency_values(raw_currency, instance) if raw_currency else []
    if len(currencies) != 1:
        raise validation_error(
            "sort",
            "sort=budget_amount orders amounts in one currency and never converts: pass exactly one "
            "budget_currency (an ISO 4217 code such as EUR)",
            instance,
        )


PLACEMENT_GRADES = ("exact", "region", "none")


def placement_grades(raw: str | None, default: list[str] | None, instance: str) -> list[str] | None:
    """The grades `?placement=` asks for, `default` when it names none, `None` for no filter. An
    unknown grade is a `400 validation_error`. Shared with the alert matcher
    (`services/alerts/matching.py`) so a saved search and the list read one value the same way.

    `?placement=,` parses to no grades and means the caller's default, as an empty value does for
    every sibling filter: before 2026-09-27 it reached `sa.or_()` with no clauses, which selected
    every *located* proposal and silently dropped the unlocated ones."""
    grades = (csv_param(raw) if raw else None) or default
    if grades is None:
        return None
    unknown = [g for g in grades if g not in PLACEMENT_GRADES]
    if unknown:
        raise validation_error("placement", f"unknown placement value(s): {', '.join(unknown)}", instance)
    return grades


def _apply_placement_filter(
    stmt: sa.Select[Any], request: Request, *, default: list[str] | None
) -> sa.Select[Any]:
    """Applied separately from `_apply_proposal_filters` (docstring there) because it must *not*
    narrow the totals `_proposal_geo_features` computes: `placement` controls which grades are
    drawn as map features, never `meta.unplaced_count`/`totals.records`/the lifecycle and
    technology counts, which always cover every visible record matching every other filter
    (docs/23 §3.1, ADR 0008).
    `default` is `None` on `GET /v1/proposals` (every grade returned unless the caller asks
    otherwise) and `["exact", "region"]` on `GET /v1/proposals/geo` (docs/23 §3.1's stated
    default for that endpoint)."""
    clause = _placement_clause(request, default=default)
    return stmt if clause is None else stmt.where(clause)


def _placement_clause(request: Request, *, default: list[str] | None) -> sa.ColumnElement[bool] | None:
    """The `?placement=` predicate over `Proposal.location_id` (`_apply_placement_filter`), or
    `None` for no filter. Also selected as a column by `_proposal_geo_rows`, which needs the rows
    it excludes for its totals, so the filter and the column are one expression."""
    grades = placement_grades(request.query_params.get("placement"), default, request.url.path)
    if grades is None:
        return None
    # Placement is judged on the grade a row is *served* at, not the one it is stored at
    # (restricted-precision rule, docs/04 D-9): an `exact` row whose licence forbids raw
    # publication is a `region` row on every non-admin surface (`services/api/geo.py::
    # effective_placement`), so `placement=exact` must not select it and `placement=region`
    # must -- `location_exact_permitted` is the SQL twin of that rule (services/api/visibility.py).
    clauses: list[sa.ColumnElement[bool]] = []
    if "exact" in grades:
        clauses.append(sa.and_(Location.precision == "exact", location_exact_permitted()))
    if "region" in grades:
        clauses.append(Location.precision.in_(PLACEMENT_REGION_PRECISIONS))
        clauses.append(sa.and_(Location.precision == "exact", sa.not_(location_exact_permitted())))
    if "none" in grades:
        clauses.append(Location.precision == "unknown")
    loc_subquery = select(Location.id).where(sa.or_(*clauses))
    return Proposal.location_id.in_(loc_subquery)


def _apply_slip_filter(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    """`?slipped=true|false` and `?slip_bucket=under_1y,1_to_3y,over_3y`.

    Both, rather than one or the other, because the distribution makes them answer different
    questions. Measured on the 2026-09-21 load: 129 of 5,826 dated active proposals are slipped
    (2.2 %) -- rare enough that a boolean is a usable way to find them at all -- but within those
    129 the split is 67 / 48 / 14, and the buckets do not mean the same thing. A NESO "Consents
    Approved" row eight months past its date is a live project with a late connection; the 14 rows
    more than three years past are a different prospect entirely. With only a boolean, a caller
    who wants the 14 has to pull all 129 and re-derive the threshold client-side, which is exactly
    the kind of rule that then disagrees with ours.

    `slipped=false` and a bucket list is the one contradictory pairing, and it is a 400 naming the
    conflict rather than a 200 with an empty page: an empty page reads as "no such records",
    which is a different and wrong answer (docs/04 API-3's rule that a request we cannot honour is
    an error, never a silent no-op).
    """
    parsed = slip_params(
        request.query_params.get("slipped"), request.query_params.get("slip_bucket"), request.url.path
    )
    if parsed is None:
        return stmt
    slipped, buckets = parsed
    return stmt.where(slip_filter(slipped=slipped, buckets=buckets, on=slip_today()))


def slip_params(
    raw_slipped: str | None, raw_buckets: str | None, instance: str
) -> tuple[bool | None, list[str] | None] | None:
    """`(slipped, buckets)` for `?slipped=`/`?slip_bucket=`, or `None` for no filter; the 400s
    `_apply_slip_filter` documents. Shared with the alert matcher (`services/alerts/matching.py`)."""
    # An empty value means "no filter" for both, as it does for every sibling filter here (the
    # `if v := qp.get(name)` idiom below); a form that submits an unset control must not 400.
    raw_slipped = raw_slipped or None
    if raw_slipped is None and not raw_buckets:
        return None
    slipped: bool | None = None
    if raw_slipped is not None:
        if raw_slipped not in ("true", "false"):
            raise validation_error(
                "slipped", f"unknown slipped value {raw_slipped!r}; expected true or false", instance
            )
        slipped = raw_slipped == "true"
    buckets = csv_param(raw_buckets) if raw_buckets else None
    if buckets:
        unknown = [b for b in buckets if b not in SLIP_BUCKETS]
        if unknown:
            raise validation_error(
                "slip_bucket",
                f"unknown slip_bucket value(s): {', '.join(unknown)}; "
                f"expected one of {', '.join(SLIP_BUCKETS)}",
                instance,
            )
        if slipped is False:
            raise validation_error(
                "slip_bucket",
                "slip_bucket selects slipped proposals and cannot be combined with slipped=false",
                instance,
            )
    if not buckets and slipped is None:
        # `?slip_bucket=,,` parses to no tokens. Without this, it would fall through to
        # `slip_filter(slipped=None, ...)` and silently mean `slipped=false` -- a filter the
        # caller never asked for. An empty value means "no filter", as everywhere else here.
        return None
    return slipped, buckets


#: The record-level time windows proposals and opportunities share: `(parameter, column attribute,
#: comparison)`. Inclusive bounds, as `due_at[from|to]`; a NULL column matches no bound (both columns
#: are NOT NULL today). `updated_since` is `last_changed[from]` under its sync name (`SYNC_FILTERS`).
RECORD_TIME_BOUNDS: tuple[tuple[str, str, str], ...] = (
    ("first_seen[from]", "first_seen", "gte"),
    ("first_seen[to]", "first_seen", "lte"),
    ("last_changed[from]", "last_changed", "gte"),
    ("last_changed[to]", "last_changed", "lte"),
    ("updated_since", "last_changed", "gte"),
)


def _apply_record_time_filters(
    stmt: sa.Select[Any],
    request: Request,
    first_seen: InstrumentedAttribute[dt.datetime],
    last_changed: InstrumentedAttribute[dt.datetime],
) -> sa.Select[Any]:
    """`first_seen[from|to]`, `last_changed[from|to]` and `updated_since` over the record's own
    columns, each bound parsed by `instant_filter` (a malformed one is a `400 validation_error`)."""
    columns = {"first_seen": first_seen, "last_changed": last_changed}
    for name, column, op in RECORD_TIME_BOUNDS:
        if v := request.query_params.get(name):
            bound = instant_filter(name, v, request.url.path)
            stmt = stmt.where(columns[column] >= bound if op == "gte" else columns[column] <= bound)
    return stmt


def _apply_proposal_filters(
    stmt: sa.Select[Any], request: Request, entitlement: str = "public"
) -> sa.Select[Any]:
    """`entitlement` is the caller's tier: `source_id=` and the `q=` source-record-id arm match only
    through links that tier may see (`visible_source_link_filter`; docs/21 §8 item 3)."""
    qp = request.query_params
    if v := qp.get("kind"):
        stmt = stmt.where(Proposal.kind.in_(csv_param(v)))
    if v := qp.get("technology"):
        stmt = stmt.where(Proposal.technology.in_(csv_param(v)))
    if v := qp.get("lifecycle_state"):
        stmt = stmt.where(Proposal.lifecycle_state.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Proposal.jurisdiction.in_(csv_param(v)))
    if v := qp.get("iso"):
        stmt = stmt.where(Proposal.iso.in_(csv_param(v)))
    if v := qp.get("source_id"):
        # A subquery, not a join: a proposal linked to two of the named sources would otherwise be
        # returned twice (`?source_id=a,b` answered 120 rows for 96 proposals on the parity store,
        # `tests/test_saved_search_parity.py`), and `include=count` counted both.
        linked = select(ProposalSource.proposal_id).where(
            ProposalSource.source_id.in_(csv_param(v)),
            *visible_source_link_filter(ProposalSource, entitlement),
        )
        stmt = stmt.where(Proposal.id.in_(linked))
    if v := qp.get("capacity_mw[gte]"):
        stmt = stmt.where(Proposal.capacity_mw >= number_filter("capacity_mw[gte]", v, request.url.path))
    if v := qp.get("capacity_mw[lte]"):
        stmt = stmt.where(Proposal.capacity_mw <= number_filter("capacity_mw[lte]", v, request.url.path))
    if v := qp.get("storage_mwh[gte]"):
        stmt = stmt.where(Proposal.storage_mwh >= number_filter("storage_mwh[gte]", v, request.url.path))
    if v := qp.get("sponsor_id"):
        stmt = stmt.where(Proposal.sponsor_org_id.in_(visible_organization_ids(csv_param(v))))
    if v := qp.get("interconnection_point_id"):
        # A subquery on the point's own register clauses: an id named by a gated register selects
        # nothing, the same empty page a made-up id gets, so the filter is no oracle for it. The
        # "has a visible proposal" half of the point predicate is the list's own row predicate.
        points = select(InterconnectionPoint.id).where(
            InterconnectionPoint.public_id.in_(csv_param(v)),
            *interconnection_point_source_filter(entitlement),
        )
        stmt = stmt.where(Proposal.interconnection_point_id.in_(points))
    stmt = _apply_record_time_filters(stmt, request, Proposal.first_seen, Proposal.last_changed)
    if v := qp.get("slug"):
        stmt = stmt.where(Proposal.slug == v)
    if v := qp.get("county_fips"):
        # A subquery on `Proposal.location_id`, not a `.join(Location, ...)`, because this
        # function runs both before and after `Location` is already joined at some call sites
        # (`_proposal_geo_rows` outer-joins it)
        # -- a second join to the same table there would be invalid SQL, and a subquery is correct
        # regardless of what the caller already joined.
        loc_subquery = select(Location.id).where(Location.county_fips.in_(csv_param(v)))
        stmt = stmt.where(Proposal.location_id.in_(loc_subquery))
    if v := qp.get("state"):
        # `location.state_code` (ISO 3166-2, api/openapi.yaml `State`), a subquery for the same reason
        # as `county_fips` above. A proposal with no location, or a location with no state code,
        # matches no state. Until 2026-09-27 this key was allowlisted and never applied: `?state=US-TX`
        # answered all 10,409 proposals on the dev store.
        loc_subquery = select(Location.id).where(Location.state_code.in_(csv_param(v)))
        stmt = stmt.where(Proposal.location_id.in_(loc_subquery))
    stmt = _apply_slip_filter(stmt, request)
    if v := qp.get("q"):
        # Substring match over the three things a user actually types (web/templates/base.html
        # promises "name, sponsor, queue ID"): the canonical name, the sponsor organisation's
        # canonical name, and any active source record id (queue position, docket, plant-generator
        # id). Subqueries rather than joins so a proposal with several sources is not repeated.
        like = f"%{v.lower()}%"
        # A sponsor the public tier may not see does not match either: `?q=<its name>` returning
        # its proposals would confirm the name behind a `sponsor: null` (organisation arm,
        # services/api/visibility.py).
        sponsor_ids = select(Organization.id).where(
            func.lower(Organization.name_canonical).like(like), *organization_visibility_filter()
        )
        record_hits = select(ProposalSource.proposal_id).where(
            func.lower(ProposalSource.source_record_id).like(like),
            *visible_source_link_filter(ProposalSource, entitlement),
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Proposal.name_canonical).like(like),
                Proposal.sponsor_org_id.in_(sponsor_ids),
                Proposal.id.in_(record_hits),
            )
        )
    return stmt


def _proposal_query_with_filters(request: Request, entitlement: str = "public") -> sa.Select[tuple[Proposal]]:
    # `Proposal.sources` is a `viewonly` relationship with no eager default (unlike `sponsor`/
    # `location`, both `lazy="joined"`), so a caller that reads `.sources` over a whole result set
    # (`_proposal_licence_rows`) would otherwise issue one query per proposal -- fine at this list
    # endpoint's page size (<=200), unlike the geo endpoint's unpaginated full-viewport set, which
    # uses `_proposal_geo_rows` below instead (services/README.md "Sprint 2 fixes").
    #
    # `entitlement` (Pro tier and alerts, task item 2): "public" reads `public_at` as before;
    # "pro"/"api" read `published_at` instead (services/api/visibility.py
    # `proposal_visibility_filter`) — the same list/detail code path serves every tier, only the
    # predicate changes, per docs/21 §5.4's "one predicate" design.
    stmt = (
        select(Proposal)
        .where(*proposal_visibility_filter(entitlement))
        .options(selectinload(Proposal.sources))
    )
    stmt = _apply_proposal_filters(stmt, request, entitlement)
    # No default (ADR 0008): every placement grade is returned unless the caller filters
    # explicitly — a `none`-grade proposal (unknown location) belongs in list/search by design
    # (docs/21 §3.7), unlike the map, which defaults to `exact,region`.
    return _apply_placement_filter(stmt, request, default=None)


#: The `Proposal` columns `services/api/geo.py::_record_feature` reads for an individual marker,
#: plus identity/join keys: `_load_geo_records` loads only these (and the source links), for at most
#: `SPLIT_THRESHOLD` rows. Any column `_record_feature` starts reading later needs adding here too --
#: SQLAlchemy's `load_only` turns a column left out into a silent per-row deferred load on first
#: access, not an error, so a gap here would quietly reintroduce an N+1.
_GEO_PROPOSAL_COLUMNS = (
    Proposal.id,
    Proposal.public_id,
    Proposal.slug,
    Proposal.name_canonical,
    Proposal.kind,
    Proposal.technology,
    Proposal.lifecycle_state,
    Proposal.capacity_mw,
    Proposal.last_changed,
    Proposal.location_id,
)
#: The proposal fields the map prints or sums (`services/api/geo.py`): a row whose provenance for
#: one of them names a hidden source is drawn from its served view.
_GEO_GATED_FIELDS = ("name_canonical", "kind", "technology", "lifecycle_state", "capacity_mw")
#: The fields `totals` counts: a row whose provenance for one of them names a hidden source is
#: counted from its served view.
_GEO_TOTALS_GATED_FIELDS = ("lifecycle_state", "technology")
#: The printed-only rest of `_GEO_GATED_FIELDS`: the map gate is `totals OR rest`, so the two
#: clauses `_proposal_geo_rows` evaluates never parse the same field's provenance twice.
_GEO_PRINTED_ONLY_GATED_FIELDS = tuple(f for f in _GEO_GATED_FIELDS if f not in _GEO_TOTALS_GATED_FIELDS)


def _proposal_geo_rows(
    request: Request, entitlement: str = "public", *, hidden_sources: frozenset[str] = frozenset()
) -> sa.Select[Any]:
    """Every visible proposal matching `request`'s filters, as plain columns, for `GET
    /v1/proposals/geo`: one query that both the totals (every matching row, placed or not) and the
    drawn features (the placeable subset) are computed from, so the visibility predicate is
    evaluated once rather than once per purpose (docs/CHANGELOG.md, 2026-10-07). Columns, not ORM
    entities: hydrating ~9,000 `Proposal`/`Location`/`Licence` objects was most of a national map
    call. Row order is the visibility index's, as it was for the two queries this replaces (no
    `ORDER BY` either way; Postgres gives no order, `tests/test_postgres.py` compares as sets).

    What decides whether a row is drawn is selected as columns, each the same SQL expression the
    separate plottable query used as a filter, so it is exactly as strict:
    - `drawable`: the `placement` predicate (`_placement_clause`, default `exact,region`, docs/23
      §3.1), judged on the grade a row is *served* at (`location_exact_permitted`);
    - `geom` not null, and the placement's own `source_id` not one the tier may not read
      (`hidden_sources`, `visibility.source_split`; GatedRecord's rule);
    - `totals_gate`/`printed_gate`: the row's provenance for a counted field (lifecycle,
      technology), or for one only printed or summed (name, kind, capacity), names a hidden source
      (`hidden_provenance_clause`); the map's gate over `_GEO_GATED_FIELDS` is their OR. Such a row
      is read through its `GatedRecord` view.
    The placement predicate is not a filter here because `placement` never narrows the totals
    (`_apply_placement_filter`'s docstring; ADR 0008)."""
    if hidden_sources:
        totals_gate = hidden_provenance_clause(Proposal, _GEO_TOTALS_GATED_FIELDS, hidden_sources)
        printed_gate = hidden_provenance_clause(Proposal, _GEO_PRINTED_ONLY_GATED_FIELDS, hidden_sources)
    else:
        totals_gate = printed_gate = sa.false()
    drawable = _placement_clause(request, default=["exact", "region"])
    stmt = (
        select(
            Proposal.id,
            Proposal.lifecycle_state,
            Proposal.technology,
            Proposal.capacity_mw,
            totals_gate.label("totals_gate"),
            printed_gate.label("printed_gate"),
            (drawable if drawable is not None else sa.true()).label("drawable"),
            Location.geom,
            Location.precision,
            Location.county_fips,
            Location.county_name,
            Location.state_code,
            Location.country,
            Location.source_id.label("location_source_id"),
            Licence.allows_raw_publication,
        )
        .select_from(Proposal)
        .outerjoin(Location, Location.id == Proposal.location_id)
        .outerjoin(Licence, Licence.id == Location.licence_id)
        .where(*proposal_visibility_filter(entitlement))
    )
    return _apply_proposal_filters(stmt, request, entitlement)


def _load_geo_records(db: Session, ids: list[Any]) -> dict[Any, Proposal]:
    """The full records behind the individual markers (at most `SPLIT_THRESHOLD`), with their
    source links, in one query per 500 plus one for the links -- where a lazy `.sources` read
    per marker issued one query each."""
    out: dict[Any, Proposal] = {}
    for start in range(0, len(ids), 500):
        stmt = (
            select(Proposal)
            .where(Proposal.id.in_(ids[start : start + 500]))
            .options(
                load_only(*_GEO_PROPOSAL_COLUMNS),
                noload(Proposal.sponsor),
                lazyload(Proposal.location),
                selectinload(Proposal.sources),
            )
        )
        out.update((p.id, p) for p in db.scalars(stmt))
    return out


class _GeoFeatures(NamedTuple):
    #: The drawable rows in row order: a `GeoRow`, or the row's `GatedRecord` view when a printed
    #: field came from a source the tier may not read.
    plottable: list[GeoRow | Proposal]
    #: Every visible proposal matching the filters, placed or not (D-8; ADR 0008).
    records_total: int
    lifecycle_state_counts: dict[str, int]
    technology_counts: dict[str, int]
    unplaced_count: int
    #: The rows the licence summary credits: every filter plus the placement default.
    credited_ids: list[Any]


def _proposal_geo_features(db: Session, request: Request, entitlement: str = "public") -> _GeoFeatures:
    """Totals, drawable rows and credited ids for `GET /v1/proposals/geo`, all from the one
    `_proposal_geo_rows` query."""
    hidden_sources = source_split(db, entitlement)[1]
    result = db.connection().execute(_proposal_geo_rows(request, entitlement, hidden_sources=hidden_sources))
    # Executed on the session's connection, not through `Session.execute`: the ORM path fetches every
    # row before the first is returned, and ~10,000 `Row`s held for the whole call were a measurable
    # share of it, mostly as the full garbage collections they triggered. Here rows are streamed
    # from the cursor and unpacked positionally (`_proposal_geo_rows`'s select order).
    rows: Iterable[Any] = result
    views: dict[Any, Any] = {}
    if hidden_sources:
        # Read through the served view only the rows whose stored value for a counted or printed
        # field came from a hidden source; their links are loaded once for the request. This needs
        # the rows twice, so they are held on this path.
        rows = held = result.all()
        gated = set()
        for pid, _ls, _t, _c, totals_gate, printed_gate, drawable, geom, *_loc, loc_source, _raw in held:
            if totals_gate or (
                printed_gate and drawable and geom is not None and loc_source not in hidden_sources
            ):
                gated.add(pid)
        views = gated_views(db, Proposal, gated, entitlement)
    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    unplaced = 0
    licences: dict[Any, GeoLicence] = {}
    plottable: list[GeoRow | Proposal] = []
    credited: list[Any] = []
    records_total = 0
    for row in rows:
        records_total += 1
        (
            pid,
            lifecycle_state,
            technology,
            capacity_mw,
            totals_gate,
            printed_gate,
            drawable,
            geom,
            precision,
            county_fips,
            county_name,
            state_code,
            country,
            loc_source,
            allows_raw,
        ) = row
        if totals_gate and (view := views.get(pid)) is not None:
            # Counted as served (GatedRecord), never as stored; such a row is never a `GeoRow`.
            lifecycle_counts[view.lifecycle_state] += 1
            if view.technology:
                technology_counts[view.technology] += 1
        else:
            lifecycle_counts[lifecycle_state] += 1
            if technology:
                technology_counts[technology] += 1
        if drawable:
            credited.append(pid)
        if geom is None or loc_source in hidden_sources:
            unplaced += 1
            continue
        if not drawable:
            continue
        if totals_gate or printed_gate:
            # Never the stored values: a gated row with no view is not drawn at all.
            if (view := views.get(pid)) is not None:
                plottable.append(view)
            continue
        if (licence := licences.get(allows_raw)) is None:
            licence = licences[allows_raw] = GeoLicence(bool(allows_raw))
        plottable.append(
            GeoRow(
                pid,
                lifecycle_state,
                technology,
                capacity_mw,
                geom,
                precision,
                county_fips,
                county_name,
                state_code,
                country,
                licence,
            )
        )
    return _GeoFeatures(
        plottable, records_total, dict(lifecycle_counts), dict(technology_counts), unplaced, credited
    )


def _proposal_licence_rows(proposals: list[Proposal], entitlement: str = "public") -> list[dict[str, Any]]:
    """`licence_summary` credits only the links the tier may see: a gated source named in the
    summary is the same disclosure as one listed in the Sources panel (docs/21 §8 items 3-4)."""
    rows = []
    for p in proposals:
        for s in visible_source_links(p.sources, entitlement):
            rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


def _csv_list_response(request: Request, db: Session, ctx: AuthContext, resource: str) -> Any:
    """Deferred import: `services.api.exports` reaches this module through
    `services.api.resource_queries`, so importing it at module level would cycle."""
    from services.api.exports import csv_list_response

    return csv_list_response(request, db, ctx, cast("Any", resource))


@router.get("/v1/proposals")
def list_proposals(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, LIST_COMMON | PROPOSAL_FILTERS | SYNC_FILTERS)
    if wants_csv(request):
        return _csv_list_response(request, db, ctx, "proposal")
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, PROPOSAL_SORT_ALLOWLIST, "-last_changed")
    stmt = _proposal_query_with_filters(request, ctx.entitlement)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Proposal, field),
        id_column=Proposal.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_proposal(p, entitlement=ctx.entitlement) for p in rows]
    meta = build_meta("proposal", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(
                _proposal_query_with_filters(request, ctx.entitlement).subquery()
            )
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows, ctx.entitlement)),
        page=build_page(next_cursor, None, has_more),
    )
    return env


#: Ids bound per `_source_licence_aggregate` statement: under SQLite's default limit of 32,766
#: host parameters (3.32+) and psycopg's 65,535, so a national map stays one statement.
_AGGREGATE_ID_CHUNK = 10_000


def _source_licence_aggregate(
    db: Session,
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk_column: InstrumentedAttribute[Any],
    record_ids: Sequence[Any],
    entitlement: str = "public",
) -> dict[str, Any]:
    """`licence_summary` for a whole (unpaginated) result set via a `GROUP BY source_id`
    aggregate query, instead of materialising every visible `proposal_source`/`opportunity_source`
    ORM row just to fold them in Python (`_proposal_licence_rows` — fine at a list page's size, the
    dominant remaining cost at the geo endpoints' full-viewport scale after the visibility-index
    fix, services/README.md "Sprint 2 fixes").

    `record_ids` are the records the response credits, as the caller already selected them (the
    map's drawable rows, `_proposal_geo_features`): binding them, rather than re-running the whole
    visibility, filter and placement predicate as an `IN (subquery)`, was about 80 ms of a national
    map call on the full dev store (docs/CHANGELOG.md, 2026-10-07). The
    link-level clauses below are unchanged. Chunks are merged exactly (sum of counts, max of
    maxima); sources come out ordered by `(source_id, licence_id)`, which is the order SQLite's
    `GROUP BY` produced and makes Postgres's order deterministic too."""
    agg_stmt = (
        select(
            Source.id,
            Source.name,
            Source.operator,
            Licence.id,
            Licence.name,
            Licence.url,
            Licence.reuse_class,
            func.coalesce(Source.attribution_text, Licence.attribution_text),
            Licence.requires_link_back,
            func.max(link_model.retrieved_at),
            func.count(),
        )
        .select_from(link_model)
        .join(Source, Source.id == link_model.source_id)
        .join(Licence, Licence.id == Source.licence_id)
        .where(
            # Bound at execution, not in the statement: SQLAlchemy's compiled cache keeps the
            # first statement it compiles, and with it any values written into it.
            fk_column.in_(sa.bindparam("record_ids", expanding=True)),
            link_model.active.is_(True),
            # The same two clauses `visible_source_links` applies row by row (docs/21 §8 item 4).
            Source.publish_state.in_(permitted_source_states(entitlement)),
            Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        )
        .group_by(Source.id, Licence.id)
    )
    merged: dict[tuple[Any, Any], list[Any]] = {}
    for start in range(0, len(record_ids), _AGGREGATE_ID_CHUNK):
        chunk = list(record_ids[start : start + _AGGREGATE_ID_CHUNK])
        for row in db.execute(agg_stmt, {"record_ids": chunk}).all():
            key = (row[0], row[3])
            if (seen := merged.get(key)) is None:
                merged[key] = list(row)
                continue
            seen[10] += row[10]
            if row[9] is not None and (seen[9] is None or row[9] > seen[9]):
                seen[9] = row[9]
    rows = [tuple(merged[key]) for key in sorted(merged)]
    return licence_summary_from_source_aggregates(rows)  # type: ignore[arg-type]


@router.get("/v1/proposals/geo")
def get_proposals_geo(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | PROPOSAL_FILTERS | SYNC_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc
    geo = _proposal_geo_features(db, request, ctx.entitlement)
    fc = build_geo_feature_collection(
        geo.plottable,
        bbox=bbox,
        zoom=zoom,
        records_total=geo.records_total,
        lifecycle_state_counts=geo.lifecycle_state_counts,
        technology_counts=geo.technology_counts,
        entitlement=ctx.entitlement,
        load_records=lambda ids: _load_geo_records(db, ids),
    )
    meta = build_meta("proposal", tier=ctx.entitlement, extra={"unplaced_count": geo.unplaced_count})
    # The rows matching every filter and the placement default, as the drawn features are, so the
    # licence summary credits exactly the sources behind what is actually drawn.
    licence_summary = _source_licence_aggregate(
        db, ProposalSource, ProposalSource.proposal_id, geo.credited_ids, ctx.entitlement
    )
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)


@router.get("/v1/proposals/{public_id}")
def get_proposal(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        return merged_redirect_or_404(
            db, Proposal, public_id, proposal_visibility_filter, ctx.entitlement, request
        )
    data = serialize_proposal(prop, entitlement=ctx.entitlement)
    # Where the project connects (docs/21 §3.24), with its tier's queue totals; detail only, so no
    # list or map query pays for it. Deferred import: that module imports this one.
    from services.api.interconnection_points import proposal_point_embed

    data["interconnection_point"] = proposal_point_embed(db, prop, ctx.entitlement)
    # What the record is made of and which source supplied each served field (docs/22 §23.4):
    # detail only, from the links the served view admits (the same view `serialize_proposal` built).
    view = gated_record(prop, ctx.entitlement)
    readable = cast("list[ProposalSource]", list(view.sources))
    data["field_sources"] = view.field_sources() if isinstance(view, GatedRecord) else {}
    data["members"] = proposal_members(readable)
    data["merge_history"] = merge_history(db, prop, readable)
    meta = build_meta("proposal", tier=ctx.entitlement)
    return build_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows([prop], ctx.entitlement)),
        redactions=record_redactions(prop, ctx.entitlement),
    )


@router.get("/v1/proposals/{public_id}/sources")
def list_proposal_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        return merged_redirect_or_404(
            db, Proposal, public_id, proposal_visibility_filter, ctx.entitlement, request
        )
    from services.api.serialize import provenance_row

    # docs/21 §8 item 3: a link to a source this tier may not read is omitted, not greyed.
    links = visible_source_links(prop.sources, ctx.entitlement)
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("proposal", tier=ctx.entitlement)
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ----------------------------------------------------------------------------------- opportunities
OPPORTUNITY_FILTERS = {
    "kind",
    "status",
    "technologies",
    "jurisdiction",
    "source_id",
    "due_at[from]",
    "due_at[to]",
    "slug",
    # 2026-09-27 (lane E15), refused until now (services/README.md open decision 7). `issuer_id`
    # follows `sponsor_id`'s no-oracle rule; `budget_amount[gte]` needs one `budget_currency`
    # (`budget_bound`), which is also a facet on its own.
    "issuer_id",
    "open_at[from]",
    "open_at[to]",
    "capacity_sought_mw[gte]",
    "budget_currency",
    "budget_amount[gte]",
    "first_seen[from]",
    "first_seen[to]",
    "last_changed[from]",
    "last_changed[to]",
}
OPPORTUNITY_SORT_ALLOWLIST = {"due_at", "open_at", "last_changed", "budget_amount"}


def _opportunity_technologies_filter(db: Session, values: list[str]) -> ColumnElement[bool]:
    """Any-of over `Opportunity.technologies[]`; an all-source opportunity (empty array) matches
    every value (api/openapi.yaml `Technologies` parameter). `technologies` is a real Postgres
    `ARRAY(Text)` in the canonical migration but a JSON-encoded `TEXT` column on SQLite
    (`services/db/types.py TextArray`, this sprint's test target — services/README.md), so the
    membership test is dialect-specific: Postgres uses the native `&&` overlap operator; SQLite
    matches the JSON-quoted token as a substring, which is exact for the closed technology
    vocabulary (docs/21 §7 — no value contains a quote or backslash). The Postgres branch is
    written to spec but not exercised here, same caveat as every other dialect-specific path in
    this service.
    """
    empty = Opportunity.technologies == []
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    overlap: ColumnElement[bool]
    if dialect == "postgresql":
        overlap = Opportunity.technologies.op("&&")(list(values))
    else:
        # `.like()` on a `TextArray` column would otherwise bind the pattern *through the
        # column's own type* (`services/db/types.py` JSON-encodes it, turning `%"wind"%` into a
        # JSON array of one character per list element) rather than as a plain string -- casting
        # to `Text` first makes the right-hand literal an ordinary string bind again.
        technologies_text = sa.cast(Opportunity.technologies, sa.Text)
        overlap = sa.or_(*(technologies_text.like(f'%"{t}"%') for t in values))
    return sa.or_(empty, overlap)


def _opportunity_query_with_filters(
    request: Request, db: Session, entitlement: str = "public"
) -> sa.Select[tuple[Opportunity]]:
    # Same N+1 fix as `_proposal_query_with_filters` above, for `Opportunity.sources`; same
    # entitlement wiring too (services/api/visibility.py `opportunity_visibility_filter`).
    stmt = (
        select(Opportunity)
        .where(*opportunity_visibility_filter(entitlement))
        .options(selectinload(Opportunity.sources))
    )
    qp = request.query_params
    status = csv_param(qp.get("status")) or ["open"]
    stmt = stmt.where(Opportunity.status.in_(status))
    if v := qp.get("kind"):
        stmt = stmt.where(Opportunity.kind.in_(csv_param(v)))
    if v := qp.get("technologies"):
        stmt = stmt.where(_opportunity_technologies_filter(db, csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Opportunity.jurisdiction.in_(csv_param(v)))
    if v := qp.get("source_id"):
        # A subquery, not a join, for the same reason as the proposal arm: no duplicate rows.
        linked = select(OpportunitySource.opportunity_id).where(
            OpportunitySource.source_id.in_(csv_param(v)),
            *visible_source_link_filter(OpportunitySource, entitlement),
        )
        stmt = stmt.where(Opportunity.id.in_(linked))
    if v := qp.get("due_at[from]"):
        stmt = stmt.where(Opportunity.due_at >= instant_filter("due_at[from]", v, request.url.path))
    if v := qp.get("due_at[to]"):
        stmt = stmt.where(Opportunity.due_at <= instant_filter("due_at[to]", v, request.url.path))
    if v := qp.get("issuer_id"):
        stmt = stmt.where(Opportunity.issuer_org_id.in_(visible_organization_ids(csv_param(v))))
    if v := qp.get("open_at[from]"):
        stmt = stmt.where(Opportunity.open_at >= date_filter("open_at[from]", v, request.url.path))
    if v := qp.get("open_at[to]"):
        stmt = stmt.where(Opportunity.open_at <= date_filter("open_at[to]", v, request.url.path))
    if v := qp.get("capacity_sought_mw[gte]"):
        bound = number_filter("capacity_sought_mw[gte]", v, request.url.path)
        stmt = stmt.where(Opportunity.capacity_sought_mw >= bound)
    if v := qp.get("budget_currency"):
        stmt = stmt.where(Opportunity.budget_currency.in_(currency_values(v, request.url.path)))
    budget = budget_bound(qp.get("budget_amount[gte]"), qp.get("budget_currency"), request.url.path)
    if budget is not None:
        amount, currency = budget
        stmt = stmt.where(Opportunity.budget_currency == currency, Opportunity.budget_amount >= amount)
    stmt = _apply_record_time_filters(stmt, request, Opportunity.first_seen, Opportunity.last_changed)
    if v := qp.get("slug"):
        stmt = stmt.where(Opportunity.slug == v)
    if v := qp.get("q"):
        # Same contract as proposals: title, issuer organisation name, or an active source record id.
        like = f"%{v.lower()}%"
        issuer_ids = select(Organization.id).where(
            func.lower(Organization.name_canonical).like(like), *organization_visibility_filter()
        )
        record_hits = select(OpportunitySource.opportunity_id).where(
            func.lower(OpportunitySource.source_record_id).like(like),
            *visible_source_link_filter(OpportunitySource, entitlement),
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Opportunity.title).like(like),
                Opportunity.issuer_org_id.in_(issuer_ids),
                Opportunity.id.in_(record_hits),
            )
        )
    return stmt


def _opportunity_licence_rows(items: list[Opportunity], entitlement: str = "public") -> list[dict[str, Any]]:
    """As `_proposal_licence_rows`: only the links the tier may see."""
    rows = []
    for o in items:
        for s in visible_source_links(o.sources, entitlement):
            rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@router.get("/v1/opportunities")
def list_opportunities(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, LIST_COMMON | OPPORTUNITY_FILTERS | SYNC_FILTERS)
    # Before the CSV branch, so `Accept: text/csv` refuses a cross-currency budget sort with the
    # same 400 instead of a failed export.
    check_budget_sort(
        request.query_params.get("sort"), request.query_params.get("budget_currency"), request.url.path
    )
    if wants_csv(request):
        return _csv_list_response(request, db, ctx, "opportunity")
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Opportunity, field),
        id_column=Opportunity.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_opportunity(o, entitlement=ctx.entitlement) for o in rows]
    meta = build_meta("opportunity", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(
                _opportunity_query_with_filters(request, db, ctx.entitlement).subquery()
            )
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(rows, ctx.entitlement)),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/v1/opportunities/geo")
def get_opportunities_geo(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | OPPORTUNITY_FILTERS | SYNC_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
    items = list(db.scalars(stmt).all())
    # `services/ingest/loader.py` never geocodes opportunities (no state/county columns in
    # `pipeline.connectors.base.OPPORTUNITY_COLUMNS` to geocode from -- unlike proposals) so
    # `location` is always null here; every visible opportunity is unplaced by construction, never
    # dropped (docs/04 D-8). This endpoint predates that gap being understood and previously
    # crashed on first access to `Proposal`-only attributes (`lifecycle_state`, `technology`) that
    # `Opportunity` doesn't have, the moment any opportunity matched the filters -- untested and
    # unnoticed because nothing had populated `Opportunity.location_id` yet. Fixed here to the
    # extent this sprint's scope covers (proposals' geo endpoint, task priority): a correct,
    # honestly-empty response rather than a 500. Full parity with the proposal side (a real
    # `status_counts`/`kind_counts` aggregate, `technologies[]` clustering) is a follow-up once
    # opportunities have geometry to place at all.
    technology_counts: dict[str, int] = defaultdict(int)
    for o in items:
        for t in o.technologies or []:
            technology_counts[t] += 1
    fc = build_geo_feature_collection(
        [],
        bbox=bbox,
        zoom=zoom,
        records_total=len(items),
        lifecycle_state_counts={},
        technology_counts=dict(technology_counts),
    )
    unplaced_count = len(items)
    meta = build_meta("opportunity", tier=ctx.entitlement, extra={"unplaced_count": unplaced_count})
    return build_envelope(
        fc,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(items, ctx.entitlement)),
    )


@router.get("/v1/opportunities/{public_id}")
def get_opportunity(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        return merged_redirect_or_404(
            db, Opportunity, public_id, opportunity_visibility_filter, ctx.entitlement, request
        )
    data = serialize_opportunity(opp, entitlement=ctx.entitlement)
    meta = build_meta("opportunity", tier=ctx.entitlement)
    return build_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows([opp], ctx.entitlement)),
        redactions=record_redactions(opp, ctx.entitlement),
    )


@router.get("/v1/opportunities/{public_id}/sources")
def list_opportunity_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        return merged_redirect_or_404(
            db, Opportunity, public_id, opportunity_visibility_filter, ctx.entitlement, request
        )
    from services.api.serialize import provenance_row

    links = visible_source_links(opp.sources, ctx.entitlement)  # docs/21 §8 item 3
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("opportunity", tier=ctx.entitlement)
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ------------------------------------------------------------------------------ subject events
# `list_proposal_events` and `list_opportunity_events` were 15-statement mirrors of each other
# (docs/42-backend-review-2026-09-26.md §2 item 4); `_SubjectEventsConfig` carries exactly what
# differs between the two (the model, its visibility predicate, the event `subject_type` string,
# the URL segment and how to read a display name off the row) so `_list_subject_events` has one
# body and two call sites -- the two thin routes below.
class _SubjectEventsConfig(NamedTuple):
    subject_type: Literal["proposal", "opportunity"]
    model: type[Proposal] | type[Opportunity]
    visibility_filter: Callable[[str], Any]
    url_segment: str
    name_of: Callable[[Any], str]


_PROPOSAL_EVENTS_CONFIG = _SubjectEventsConfig(
    subject_type="proposal",
    model=Proposal,
    visibility_filter=proposal_visibility_filter,
    url_segment="proposals",
    name_of=lambda p: p.name_canonical,
)
_OPPORTUNITY_EVENTS_CONFIG = _SubjectEventsConfig(
    subject_type="opportunity",
    model=Opportunity,
    visibility_filter=opportunity_visibility_filter,
    url_segment="opportunities",
    name_of=lambda o: o.title,
)


def _list_subject_events(
    config: _SubjectEventsConfig,
    public_id: str,
    request: Request,
    db: Session,
    ctx: AuthContext,
) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    subject_row = db.scalar(
        select(config.model).where(
            config.model.public_id == public_id, *config.visibility_filter(ctx.entitlement)
        )
    )
    if subject_row is None:
        return merged_redirect_or_404(
            db, config.model, public_id, config.visibility_filter, ctx.entitlement, request
        )
    # `config.model` is a `type[Proposal] | type[Opportunity]` union, so SQLAlchemy's overloads
    # cannot narrow `db.scalar(select(config.model)...)` past the declarative base -- both members
    # share every attribute this function reads (`id`, `public_id`, `slug`), so this is a type-only
    # cast, not a runtime check.
    subject = cast("Proposal | Opportunity", subject_row)
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == config.subject_type,
        Event.subject_id == subject.id,
        *event_visibility_filter(ctx.entitlement),
    )
    if v := request.query_params.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Event, field),
        id_column=Event.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [
        serialize_event(
            e,
            subject_public_id=subject.public_id,
            subject_name=config.name_of(gated_record(subject, ctx.entitlement)),
            subject_url=f"{WEB_HOST}/{config.url_segment}/{subject.slug}",
        )
        for e in rows
    ]
    meta = build_meta(config.subject_type, tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/v1/proposals/{public_id}/events")
def list_proposal_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    return _list_subject_events(_PROPOSAL_EVENTS_CONFIG, public_id, request, db, ctx)


@router.get("/v1/opportunities/{public_id}/events")
def list_opportunity_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    return _list_subject_events(_OPPORTUNITY_EVENTS_CONFIG, public_id, request, db, ctx)


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)
