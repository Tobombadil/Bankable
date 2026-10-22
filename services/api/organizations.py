"""`GET /v1/organizations`: the companies index, searchable, filterable and sortable by size
(2026-10-22, lane org-stats; moved here from `services/api/app.py`).

The owner asked for a companies page with search, filters and sorting: "Allow me to sort by
companies with the most assets or GWs ... sort data by simple clicking on the title of it, to go
from largest to smallest". Each row now carries its figures (`stats`), read from
`organization_stats` (migration 0038; `services/api/org_stats.py` says what each figure counts and
by whose rules), so the list sorts and filters on them in SQL and the page needs no detail call
per row (`web/organizations.py::_org_holdings` made 25).

**Parameters.** Unchanged: `limit`, `cursor`, `include=count`, `slug`, `type`, `country`,
`jurisdiction`, `is_curated_issuer`, `updated_since`. Changed: `q` and `sort`. New: `scope`
(`self`, the default, or `group`: whose figures each row carries and the stats filters and sorts
read), `state`, `iso`, `technology` (each csv, any-of, over the figures' sets), `role` (csv of
`operator`, `owner`, `sponsor`: holds a current asset in that role, or sponsors a visible
proposal), `has_assets`, `has_pipeline` (`true`/`false`) and `min_operating_mw`.

**`q`** matches the canonical name or any alias a readable register spelled, ignoring case,
spacing and punctuation (`AES Corp.` finds "AES Corporation", `nextera` finds "NextEra Energy,
Inc."), anywhere in the name, so a prefix finds it. With `q` and no `sort`, the best matches come
first: names that start with the query, then aliases that do, then the rest, each in name order.
An alias from a register the tier may not read is not searched (it would confirm the spelling).

**`sort`**: `name` (the default; `name_canonical` is the same), `assets`, `operating_mw`,
`owned_mw`, `pipeline_count`, `pipeline_mw`, `rfps_issued`; `-` for largest first. A missing figure
(no MW stated, no stats row yet, or stats withheld, below) sorts last in both directions. Pagination
is the same opaque keyset cursor as every list (`services/api/pagination.py`'s encoding).

**Withheld figures.** A row's figures are served only while they were computed under the current
gate key (`org_stats.gate_key`): after a source is unpublished, a licence reclassified, the posture
flipped or a record taken down, every row reads `stats: null`, and the stats filters match nothing,
until the next refresh. `meta.organization_stats.state` says which: `current`, `stale` (withheld)
or `missing` (the table has never been filled). A company listed after the last refresh has
`stats: null` too. Organisation visibility itself is read live, as before.

**Every figure names the list query that reproduces it** (`stats.list_queries`): the portfolio's
asset list (`GET /v1/organizations/{id}/assets`, whose distinct `public_id`s are the count), the
proposal list (`GET /v1/proposals`, `sponsor_id` with `sponsor_scope` for a group) and the
opportunity list (`GET /v1/opportunities`, `issuer_id` over the group's issuers). `stats.sources`
names the registers behind the figures, credited in the envelope's `licence_summary`.

**Rate limits.** As every list: a `GET` spends a read unit and a daily-cap unit; a non-empty `q`
spends a search unit as well (`services/api/ratelimit.py`).
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
from collections.abc import Sequence
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from services.api import geo_cache
from services.api.auth import AuthContext, get_auth_context
from services.api.common import iso
from services.api.deps import get_db
from services.api.errors import validation_error
from services.api.grid_operators import canonical_iso
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES, list_query_for
from services.api.org_portfolio import CURRENT_STATUSES, scope_query
from services.api.org_stats import GROUP_TREE_SCOPE, STATS_SCOPES, current_gate_key, tier_for, token_list
from services.api.pagination import coerce_cursor_value, coerce_tiebreaker, decode_cursor, encode_cursor
from services.api.params import LIST_COMMON, check_allowed, csv_param, include_values, page_limit, sort_spec
from services.api.records import LIST_INCLUDES, exact_total, instant_filter, number_filter
from services.api.serialize import (
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_from_source_aggregates,
    serialize_organization,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    organization_visibility_filter,
    permitted_source_states,
)
from services.db.models import (
    OPPORTUNITY_STATUSES,
    Licence,
    Organization,
    OrganizationAlias,
    OrganizationStats,
    Source,
)

router = APIRouter()

ORGANIZATION_LIST_FILTERS: frozenset[str] = frozenset(
    {
        "type",
        "country",
        "jurisdiction",
        "is_curated_issuer",
        "slug",
        "updated_since",
        "scope",
        "state",
        "iso",
        "technology",
        "role",
        "has_assets",
        "has_pipeline",
        "min_operating_mw",
    }
)
#: `role=` tokens: an asset role, or `sponsor` (a visible sponsored proposal).
ROLES: tuple[str, ...] = ("operator", "owner", "sponsor")
_S = OrganizationStats
#: `sort` fields. `name` and `name_canonical` are one field.
SORT_COLUMNS: dict[str, Any] = {
    "name": Organization.name_canonical,
    "name_canonical": Organization.name_canonical,
    "assets": _S.assets,
    "operating_mw": _S.operating_mw,
    "owned_mw": _S.owned_mw,
    "pipeline_count": _S.pipeline_count,
    "pipeline_mw": _S.pipeline_mw,
    "rfps_issued": _S.rfps_issued,
}
#: The figure sorts: what `scope` and a withheld state affect.
STATS_SORTS: frozenset[str] = frozenset(SORT_COLUMNS) - {"name", "name_canonical"}
#: Characters `q` ignores, on both sides of the match (module docstring). `%`, `_` and `\` are here
#: too, so no LIKE wildcard or escape survives into the pattern.
SEARCH_IGNORED = " .,-'&/()\"!?;:+_*#@%`\\’"
#: A `state`, `iso` or `technology` token: what the stored sets hold, and nothing a LIKE reads.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ----------------------------------------------------------------------------- availability
_table_lock = threading.Lock()
_table_present: dict[int, bool] = {}


def _stats_table(db: Session) -> bool:
    """Whether the store has the table (a SQLite dev store built before migration 0038 does not:
    `web.dev_up --skip-load` reuses one). Asked once per database."""
    token = geo_cache.engine_token(db)
    with _table_lock:
        known = _table_present.get(token)
    if known is None:
        known = sa.inspect(db.get_bind()).has_table(_S.__tablename__)
        with _table_lock:
            _table_present[token] = known
    return known


def stats_state(db: Session, scope: str, tier: str) -> tuple[str, str | None]:
    """(`current` | `stale` | `missing`, the gate key rows must carry to be served)."""
    if not _stats_table(db):
        return "missing", None
    key = current_gate_key(db)
    rows = select(_S.organization_id).where(_S.scope == scope, _S.tier == tier)
    if db.scalar(select(rows.where(_S.gate_key == key).exists())):
        return "current", key
    return ("stale" if db.scalar(select(rows.exists())) else "missing"), key


# ------------------------------------------------------------------------------------ search
#: The SQLite function `squash_sql` calls: `squash` itself, registered on the request's connection
#: (`register_sqlite_squash`). Twenty-odd nested `replace()` calls overflow SQLite's parser inside
#: the alias subquery, and SQLite has no `translate()`.
SQLITE_SQUASH = "inframation_squash"


def squash(value: str) -> str:
    """`value` lower-cased with `SEARCH_IGNORED` removed: the Python side of `squash_sql`."""
    return value.lower().translate(_SQUASH_TABLE)


_SQUASH_TABLE = str.maketrans("", "", SEARCH_IGNORED)


def register_sqlite_squash(db: Session) -> None:
    """Make `SQLITE_SQUASH` callable on the session's SQLite connection (a no-op elsewhere).
    Registering again replaces it, so this is safe on a pooled connection."""
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        raw = connection.connection.dbapi_connection
        raw.create_function(SQLITE_SQUASH, 1, lambda v: None if v is None else squash(v), deterministic=True)  # type: ignore[union-attr]


def squash_sql(column: Any, dialect: str) -> ColumnElement[str]:
    """`squash` in SQL: Postgres `translate(lower(x), <ignored>, '')`, which deletes every ignored
    character in one call; on SQLite, `squash` itself (`register_sqlite_squash`)."""
    if dialect == "sqlite":
        return getattr(func, SQLITE_SQUASH)(column)  # type: ignore[no-any-return]
    return func.translate(func.lower(column), SEARCH_IGNORED, "")


def _alias_match(pattern: str, dialect: str) -> ColumnElement[bool]:
    """An alias of the organisation, from a register the public tier may read, squashes to
    `pattern` (the organisation arm reads aliases at the public tier on every route)."""
    src = aliased(Source)
    lic = aliased(Licence)
    return exists(
        select(OrganizationAlias.id)
        .join(src, src.id == OrganizationAlias.source_id)
        .join(lic, lic.id == src.licence_id)
        .where(
            OrganizationAlias.organization_id == Organization.id,
            src.publish_state.in_(permitted_source_states("public")),
            lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
            squash_sql(OrganizationAlias.alias, dialect).like(pattern),
        )
    )


def search_clause(q: str, dialect: str) -> tuple[ColumnElement[bool], ColumnElement[str]]:
    """The `q` match and its relevance key (`0:`, `1:` or `2:` and the name, so one string sorts
    by rank, then name)."""
    key = squash(q)
    if not key:  # nothing but punctuation: that text, literally (`%` is a character, not a pattern)
        match = func.lower(Organization.name_canonical).contains(q.lower(), autoescape=True)
        return match, sa.literal("2:").concat(Organization.name_canonical)
    name = squash_sql(Organization.name_canonical, dialect)
    match = or_(name.like(f"%{key}%"), _alias_match(f"%{key}%", dialect))
    rank = sa.case((name.like(f"{key}%"), "0:"), (_alias_match(f"{key}%", dialect), "1:"), else_="2:")
    return match, rank.concat(Organization.name_canonical)


# ----------------------------------------------------------------------------------- filters
def _bool_param(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if not raw:
        return None
    if raw not in ("true", "false"):
        raise validation_error(name, f"{name} must be true or false", request.url.path)
    return raw == "true"


def _tokens(request: Request, name: str) -> list[str] | None:
    values = csv_param(request.query_params.get(name))
    if not values:
        return None
    bad = [v for v in values if not _TOKEN_RE.match(v)]
    if bad:
        raise validation_error(name, f"unknown {name} value(s): {', '.join(bad)}", request.url.path)
    return values


def _in_set(column: Any, values: Sequence[str]) -> ColumnElement[bool]:
    """Any of `values` in a stored `|a|b|` set (`OrganizationStats` docstring). Escaped, because a
    technology token holds `_`, which LIKE would read as any character."""
    return or_(*(column.contains(f"|{v}|", autoescape=True) for v in values))


def _stats_filters(request: Request) -> list[ColumnElement[bool]]:
    instance = request.url.path
    clauses: list[ColumnElement[bool]] = []
    if states := _tokens(request, "state"):
        clauses.append(_in_set(_S.states, [s.upper() for s in states]))
    if isos := _tokens(request, "iso"):
        clauses.append(_in_set(_S.isos, sorted({canonical_iso(v.upper()) or v for v in isos})))
    if technologies := _tokens(request, "technology"):
        clauses.append(_in_set(_S.technologies, [t.lower() for t in technologies]))
    if roles := csv_param(request.query_params.get("role")):
        unknown = [r for r in roles if r not in ROLES]
        if unknown:
            raise validation_error("role", f"role must be among {', '.join(ROLES)}", instance)
        by_role = {
            "operator": _S.operated_assets > 0,
            "owner": _S.owned_assets > 0,
            "sponsor": _S.proposals > 0,
        }
        clauses.append(or_(*(by_role[r] for r in roles)))
    if (has_assets := _bool_param(request, "has_assets")) is not None:
        clauses.append(_S.assets > 0 if has_assets else _S.assets == 0)
    if (has_pipeline := _bool_param(request, "has_pipeline")) is not None:
        clauses.append(_S.pipeline_count > 0 if has_pipeline else _S.pipeline_count == 0)
    if v := request.query_params.get("min_operating_mw"):
        bound = number_filter("min_operating_mw", v, instance)
        if bound < 0:
            raise validation_error("min_operating_mw", "min_operating_mw must be 0 or more", instance)
        clauses.append(_S.operating_mw >= bound)
    return clauses


def _organization_filters(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    """The filters the list has always taken, unchanged."""
    qp = request.query_params
    if v := qp.get("slug"):
        stmt = stmt.where(Organization.slug == v)
    if v := qp.get("type"):
        stmt = stmt.where(Organization.type.in_(csv_param(v)))
    if v := qp.get("country"):
        stmt = stmt.where(Organization.country.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        # The organisation's own sub-national jurisdiction (ISO 3166-2), where a source states one;
        # NULL matches nothing, as on the record lists. Country-level filtering is `country`.
        stmt = stmt.where(Organization.jurisdiction.in_(csv_param(v)))
    if v := qp.get("updated_since"):
        # api/openapi.yaml `UpdatedSince`: for organisations the sync column is `updated_at`, which
        # moves on every write to the row (a rename, a merge, a new parent link) where `last_changed`
        # is set by the loaders; it is never earlier than `last_changed`, so a client that pages
        # with the largest `last_changed` it has seen as its next cursor misses nothing.
        stmt = stmt.where(Organization.updated_at >= instant_filter("updated_since", v, request.url.path))
    if v := qp.get("is_curated_issuer"):
        stmt = stmt.where(Organization.is_curated_issuer.is_(v.lower() == "true"))
    return stmt


def _scope(request: Request) -> str:
    raw = (request.query_params.get("scope") or "self").strip().lower()
    if raw not in STATS_SCOPES:
        raise validation_error("scope", f"scope must be one of {', '.join(STATS_SCOPES)}", request.url.path)
    return raw


# -------------------------------------------------------------------------------- pagination
def _page(
    db: Session,
    stmt: sa.Select[Any],
    *,
    sort: ColumnElement[Any],
    sort_type: Any,
    ascending: bool,
    cursor: str | None,
    limit: int,
    instance: str,
) -> tuple[list[Any], str | None, bool]:
    """`services/api/pagination.py::paginate`'s keyset over a sort expression that is not a column
    of the selected entity (a joined figure, the relevance key): the same cursor encoding, the same
    NULLs-last rule in both directions, `Organization.id` as the tiebreaker."""
    id_column = Organization.id
    if cursor:
        decoded = decode_cursor(cursor, instance)
        value = coerce_cursor_value(decoded.sort_value, sort_type, instance)
        tiebreaker = coerce_tiebreaker(decoded.tiebreaker_id, id_column.type, instance)
        after_id = id_column > tiebreaker if ascending else id_column < tiebreaker
        if value is None:
            stmt = stmt.where(sort.is_(None), after_id)
        else:
            beyond = sort > value if ascending else sort < value
            stmt = stmt.where(or_(beyond, sort.is_(None), and_(sort == value, after_id)))
    order = (sort.asc() if ascending else sort.desc()).nulls_last()
    stmt = stmt.add_columns(sort.label("sort_value")).order_by(
        order, id_column.asc() if ascending else id_column.desc()
    )
    rows = list(db.execute(stmt.limit(limit + 1)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        value = last.sort_value
        if isinstance(value, decimal.Decimal | float):
            value = str(value)
        next_cursor = encode_cursor(value, str(last[0].id))
    return rows, next_cursor, has_more


# ----------------------------------------------------------------------------- serialization
def _mw(value: Any) -> float | None:
    return None if value is None else round(float(value), 3)


def list_queries(public_id: str, scope: str, row: OrganizationStats) -> dict[str, dict[str, Any]]:
    """The list query each figure is the total of (module docstring)."""
    tree_scope = GROUP_TREE_SCOPE if scope == "group" else "self"
    assets_path = f"/v1/organizations/{public_id}/assets"
    held: dict[str, str] = {
        **scope_query(tree_scope, row.organizations),
        "status": ",".join(CURRENT_STATUSES),
    }
    assets = {"path": assets_path, "query": held}
    operated = {"path": assets_path, "query": {**held, "role": "operator"}}
    owned = {"path": assets_path, "query": {**held, "role": "owner"}}
    sponsor = list_query_for(public_id, tree_scope, row.organizations)
    active = {
        "path": "/v1/proposals",
        "query": {**sponsor, "lifecycle_state": ",".join(ACTIVE_LIFECYCLE_STATES), "listed": "true"},
    }
    issuers = ",".join(token_list(row.rfp_issuers) or [public_id])
    rfps = {
        "path": "/v1/opportunities",
        "query": {"issuer_id": issuers, "status": ",".join(OPPORTUNITY_STATUSES)},
    }
    return {
        "assets": assets,
        "operated_assets": operated,
        "owned_assets": owned,
        "operating_mw": assets,
        "owned_mw": owned,
        "pipeline_count": active,
        "pipeline_mw": active,
        "withdrawn_count": {"path": "/v1/proposals", "query": {**sponsor, "lifecycle_state": "withdrawn"}},
        "proposals": {"path": "/v1/proposals", "query": dict(sponsor)},
        "rfps_issued": rfps,
        "rfps_open": {"path": "/v1/opportunities", "query": {"issuer_id": issuers, "status": "open"}},
    }


def serialize_stats(public_id: str, scope: str, row: OrganizationStats | None) -> dict[str, Any] | None:
    """A row's `stats`, or `None` when it has none to serve (module docstring)."""
    if row is None:
        return None
    return {
        "scope": scope,
        "organizations": row.organizations,
        "assets": row.assets,
        "operated_assets": row.operated_assets,
        "owned_assets": row.owned_assets,
        "operating_mw": _mw(row.operating_mw),
        "operating_mw_assets": row.operating_mw_assets,
        "owned_mw": _mw(row.owned_mw),
        "owned_mw_assets": row.owned_mw_assets,
        "pipeline_count": row.pipeline_count,
        "pipeline_mw": _mw(row.pipeline_mw),
        "pipeline_mw_records": row.pipeline_mw_records,
        "withdrawn_count": row.withdrawn_count,
        "proposals": row.proposals,
        "rfps_issued": row.rfps_issued,
        "rfps_open": row.rfps_open,
        "states": token_list(row.states),
        "isos": token_list(row.isos),
        "technologies": token_list(row.technologies),
        "list_queries": list_queries(public_id, scope, row),
        "sources": list(row.sources or []),
        "computed_at": iso(row.computed_at),
    }


def _parse_instant(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def licence_summary(
    db: Session, rows: Sequence[OrganizationStats | None], entitlement: str
) -> dict[str, Any]:
    """One credit per register behind the page's figures: `record_count` is the organisations on the
    page whose figures it backs, `retrieved_at_max` its latest retrieval among them."""
    counts: dict[str, int] = {}
    latest: dict[str, dt.datetime | None] = {}
    for row in rows:
        for entry in (row.sources or []) if row is not None else []:
            sid = entry["source_id"]
            counts[sid] = counts.get(sid, 0) + 1
            seen = _parse_instant(entry.get("retrieved_at_max"))
            if seen is not None and (latest.get(sid) is None or seen > latest[sid]):  # type: ignore[operator]
                latest[sid] = seen
    if not counts:
        return licence_summary_from_source_aggregates([])
    stmt = (
        select(Source, Licence)
        .join(Licence, Licence.id == Source.licence_id)
        .where(
            Source.id.in_(sorted(counts)),
            Source.publish_state.in_(permitted_source_states(entitlement)),
            Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        )
        .order_by(Source.id)
    )
    aggregates = [
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
            latest.get(source.id),
            counts[source.id],
        )
        for source, licence in db.execute(stmt).tuples()
    ]
    return licence_summary_from_source_aggregates(aggregates)


# ------------------------------------------------------------------------------------- route
@router.get("/v1/organizations")
def list_organizations(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, LIST_COMMON | ORGANIZATION_LIST_FILTERS)
    instance = request.url.path
    limit = page_limit(request)
    include = include_values(request, LIST_INCLUDES)
    qp = request.query_params
    q = qp.get("q") or None
    explicit_sort = bool(qp.get("sort"))
    field, ascending = sort_spec(request, set(SORT_COLUMNS), "name_canonical")
    scope = _scope(request)
    tier = tier_for(ctx.entitlement)
    state, key = stats_state(db, scope, tier)
    stats_filters = _stats_filters(request)  # validated whatever the state

    base = [Organization.merged_into_id.is_(None), *organization_visibility_filter()]
    stmt: sa.Select[Any]
    if key is not None:
        joined = and_(
            _S.organization_id == Organization.id, _S.scope == scope, _S.tier == tier, _S.gate_key == key
        )
        stmt = select(Organization, _S).outerjoin(_S, joined).where(*base)
        stmt = stmt.where(*stats_filters)
    else:
        stmt = select(Organization, sa.null().label("stats")).where(*base)
        if stats_filters:
            stmt = stmt.where(sa.false())
    stmt = _organization_filters(stmt, request)

    sort: ColumnElement[Any] = SORT_COLUMNS[field]
    sort_type: Any = sort.type
    if q is not None:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            register_sqlite_squash(db)
        match, relevance = search_clause(q, dialect)
        stmt = stmt.where(match)
        if not explicit_sort:
            sort, sort_type, ascending = relevance, sa.Text(), True
    if field in STATS_SORTS and key is None:
        sort = sa.null()  # no figures to sort by: every row is a missing figure, in id order

    rows, next_cursor, has_more = _page(
        db,
        stmt,
        sort=sort,
        sort_type=sort_type,
        ascending=ascending,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=instance,
    )
    stats_rows: list[OrganizationStats | None] = [r[1] if key is not None else None for r in rows]
    data = []
    for row, stats in zip(rows, stats_rows, strict=True):
        org: Organization = row[0]
        item = serialize_organization(org)
        item["stats"] = serialize_stats(org.public_id, scope, stats)
        data.append(item)
    meta = build_meta(
        lag_days=0,
        tier=ctx.entitlement,
        extra={"organization_stats": {"scope": scope, "tier": tier, "state": state}},
    )
    if "count" in include:  # documented (docs/23 §7) and accepted, but ignored until 2026-10-07
        meta.update(exact_total(db, stmt))
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=licence_summary(db, stats_rows, ctx.entitlement),
        page=build_page(next_cursor, None, has_more),
    )
