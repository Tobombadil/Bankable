"""One place that turns a filter definition into the visible, filtered statement for each of the
three list resources (`proposal`, `opportunity`, `event`) -- what `services/api/exports.py`,
`services/api/bulk.py` and the `Accept: text/csv` path share (US-603 "any filtered list", US-703).

The list endpoints' own filter stack already lives in `services/api/records.py`
(`_proposal_query_with_filters`, `_opportunity_query_with_filters`) and reads a Starlette
`Request`; rather than re-implementing the grammar for a stored `query` dict (the duplication
docs/42 §2 counted), this module builds a *synthetic request* carrying the same query string and
feeds it to the same functions, so an export and the list page it was taken from cannot disagree
on a filter. The events filter block (five `if`s that lived inline in `services/api/app.py::
list_events`) moves here as `event_query_with_filters` and `app.py` calls it back, for the same
reason.

One clause the list endpoints do **not** apply is added here, because bulk and CSV are
redistribution surfaces the tier predicate does not distinguish (docs/21 §8's per-shape table;
`services/api/visibility.py` is untouched):

- `allows_bulk_export` (exports) / `allows_api_redistribution` (bulk) on the licence: a record is
  present only if at least one of its active source links carries a licence that permits the
  shape, and its provenance columns come from such a link (`redistributable_link`). Today the
  loader writes both flags `false` only for the `noncommercial` class (`services/ingest/loader.py`
  `upsert_licence_and_source`), which docs/21 §8 says contributes "nothing" to bulk export or the
  API even while the posture admits it on the web -- so under `PLATFORM_POSTURE=noncommercial`
  those rows are on the site and absent here, by design.

`updated_since` (docs/23 §7; api/openapi.yaml `UpdatedSince`), the incremental-sync parameter, was
a second clause added here until 2026-09-27; the list endpoints now take it themselves
(`records.SYNC_FILTERS`), with the same meaning, so it runs in the shared filter stack.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid as _uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.orm import Session, defer, joinedload, lazyload, selectinload
from sqlalchemy.orm.interfaces import LoaderOption

from services.api.common import WEB_HOST
from services.api.errors import validation_error
from services.api.params import check_allowed, csv_param, since_seq
from services.api.records import (
    OPPORTUNITY_FILTERS,
    OPPORTUNITY_SORT_ALLOWLIST,
    PROPOSAL_FILTERS,
    PROPOSAL_SORT_ALLOWLIST,
    SYNC_FILTERS,
    _opportunity_query_with_filters,
    _proposal_query_with_filters,
    check_budget_sort,
    instant_filter,
)
from services.api.visibility import event_visibility_filter, gated_record, source_visible
from services.db.event_horizon import stable_event_seq
from services.db.models import (
    OPPORTUNITY_STATUSES,
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
)

Resource = Literal["proposal", "opportunity", "event"]
RedistributionFlag = Literal["allows_bulk_export", "allows_api_redistribution"]

#: Accepted spellings for a resource, normalised to the singular the contract uses
#: (`SavedSearchEntity`): the task brief and the URL paths say `proposals`, the schema says
#: `proposal`; both are honoured on input, one is stored.
RESOURCE_ALIASES: dict[str, Resource] = {
    "proposal": "proposal",
    "proposals": "proposal",
    "opportunity": "opportunity",
    "opportunities": "opportunity",
    "event": "event",
    "events": "event",
}

EVENT_FILTERS = {
    "subject_type",
    "subject_id",
    "event_type",
    "source_id",
    "since",
    "changed_key",
    "observed_at[from]",
    "observed_at[to]",
}
EVENT_SORT_ALLOWLIST = {"seq", "observed_at"}
#: What `GET /v1/events` accepts: its filters plus the page parameters. **Not `q`**, which the
#: other lists share through `LIST_COMMON` (2026-09-27, lane E14): an event has no text of its own
#: to search (its subject's name lives on the subject list, reachable here by `subject_id`), the
#: spec never documented `q` on this operation, and until this change it was accepted and applied
#: nothing -- a filter that looks like it works and does not (docs/04 API-3). It is refused with
#: `400 unknown_parameter`, as on saved searches, webhooks and exports for `entity=event`.
EVENT_LIST_PARAMS = {"limit", "cursor", "include", "sort"} | EVENT_FILTERS

#: `changed_key` values: canonical field names as `event.changed_keys` stores them (`lifecycle_state`,
#: `capacity_mw`, `merged_into_id`; dotted for a nested key). Anything else is a `400
#: validation_error`, which is what keeps the SQLite arm below exact: it matches the JSON-quoted
#: token inside the encoded list, and a value that could hold a quote or a comma could match across
#: two elements.
_CHANGED_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

#: What an export's `query` may name, per resource: the list endpoint's filters plus `q` and
#: `sort` (both of which shape "the list I am looking at"); never `limit`/`cursor`/`include`,
#: which describe a page, not a result set.
EXPORT_QUERY_KEYS: dict[Resource, set[str]] = {
    "proposal": PROPOSAL_FILTERS | SYNC_FILTERS | {"q", "sort"},
    "opportunity": OPPORTUNITY_FILTERS | SYNC_FILTERS | {"q", "sort"},
    "event": EVENT_FILTERS | {"sort"},
}
SORT_ALLOWLISTS: dict[Resource, set[str]] = {
    "proposal": PROPOSAL_SORT_ALLOWLIST,
    "opportunity": OPPORTUNITY_SORT_ALLOWLIST,
    "event": EVENT_SORT_ALLOWLIST,
}
DEFAULT_SORTS: dict[Resource, str] = {"proposal": "-last_changed", "opportunity": "due_at", "event": "-seq"}


def normalise_resource(value: Any) -> Resource | None:
    return RESOURCE_ALIASES.get(value) if isinstance(value, str) else None


def synthetic_request(params: Mapping[str, Any], *, path: str) -> Request:
    """A Starlette `Request` whose query string is `params`, so the list endpoints' filter
    functions (which read `request.query_params`) run unchanged over a stored `query` dict. List
    values join with commas (the grammar's OR within a facet); scalars render as `str`. Only
    `query_params` and `url.path` are ever read from it."""
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            joined = ",".join(str(v) for v in value if v is not None and str(v) != "")
            if joined:
                items.append((key, joined))
        elif isinstance(value, bool):
            items.append((key, "true" if value else "false"))
        else:
            items.append((key, str(value)))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": urlencode(items).encode(),
        "headers": [],
    }
    return Request(scope)


def validate_export_query(resource: Resource, query: Any, *, instance: str) -> dict[str, Any]:
    """The `query` half of `ExportCreate`: a dict of allowed keys with scalar or list values. Unknown
    keys are a `400 unknown_parameter`, exactly as on the list endpoint (docs/04 API-3: a
    silently dropped filter on a licence-sensitive surface is a leak), and page parameters are a
    `400 validation_error` naming the field."""
    if not isinstance(query, dict):
        raise validation_error("query", "query must be an object in the x-filter-grammar shape", instance)
    for key in ("limit", "cursor", "include"):
        if key in query:
            raise validation_error(
                f"query.{key}", f"{key} describes a page, not a result set; an export has neither", instance
            )
    check_query_values(query, instance)
    request = synthetic_request(query, path=instance)
    check_allowed(request, EXPORT_QUERY_KEYS[resource])
    if resource == "opportunity":
        # A cross-currency budget sort is refused here, as on the list, rather than becoming a
        # failed export row (records.check_budget_sort; 2026-09-27, lane E16).
        check_budget_sort(
            request.query_params.get("sort"), request.query_params.get("budget_currency"), instance
        )
    return dict(query)


def check_query_values(query: Mapping[str, Any], instance: str) -> None:
    """A stored filter's values are scalars or lists of scalars (`SavedSearchQuery`, `ExportCreate`):
    anything else is a `400 validation_error` naming `query.<key>`."""
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            if not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value):
                raise validation_error(f"query.{key}", "list values must be strings or numbers", instance)
        elif not isinstance(value, (str, int, float, bool)):
            raise validation_error(f"query.{key}", "values must be scalars or lists of scalars", instance)


def resolve_subject(db: Session, public_id_value: str) -> Proposal | Opportunity | None:
    """`subject_id` on the events filters (`GET /v1/events`, `/v1/bulk/events`, an event export)
    is a public id; resolve it to the row, or `None` when it names nothing."""
    if public_id_value.startswith("prop_"):
        return db.scalar(select(Proposal).where(Proposal.public_id == public_id_value))
    if public_id_value.startswith("opp_"):
        return db.scalar(select(Opportunity).where(Opportunity.public_id == public_id_value))
    return None


def subject_info(db: Session, event: Event, entitlement: str = "public") -> dict[str, str]:
    """The denormalised `subject` block `serialize_event` takes (public id, name, web URL), moved
    here from `services/api/app.py` so `/v1/events`, `/v1/bulk/events` and an event export render
    the same subject for the same event. The name is the subject's served name at `entitlement`
    (`services/api/visibility.py::GatedRecord`), never a hidden source's spelling."""
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p:
            return {
                "subject_public_id": p.public_id,
                "subject_name": gated_record(p, entitlement).name_canonical,
                "subject_url": f"{WEB_HOST}/proposals/{p.slug}",
            }
    if event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o:
            return {
                "subject_public_id": o.public_id,
                "subject_name": gated_record(o, entitlement).title,
                "subject_url": f"{WEB_HOST}/opportunities/{o.slug}",
            }
    return {"subject_public_id": str(event.subject_id), "subject_name": "Unknown", "subject_url": WEB_HOST}


_UNKNOWN_SUBJECT_NAME = "Unknown"
#: Chunk size for the `IN (...)` lists below: under SQLite's historical 999-variable limit.
_IN_CHUNK = 500


def subject_infos(
    db: Session, events: Sequence[Event], entitlement: str = "public"
) -> dict[_uuid.UUID, dict[str, str]]:
    """`subject_info` for a whole page or export at once: one column-only query per 500 subjects
    per type instead of one `db.get` per event. Measured on 12,000 synthetic events over the real
    proposal load: the per-event `get` was ~15 of a 10,000-row event export's ~19 seconds under the
    profiler (the session's identity map is weak, so rows already streamed past are reloaded).

    The name is the served one at `entitlement`, as `subject_info`: a row whose name's
    `field_provenance` source the tier may read keeps the column (the common case, no extra
    query); any other row is loaded and read through `GatedRecord`."""
    out: dict[_uuid.UUID, dict[str, str]] = {}
    for subject_type, model, name_col, path in (
        ("proposal", Proposal, Proposal.name_canonical, "proposals"),
        ("opportunity", Opportunity, Opportunity.title, "opportunities"),
    ):
        name_key = name_col.key
        ids = sorted({e.subject_id for e in events if e.subject_type == subject_type}, key=str)
        for start in range(0, len(ids), _IN_CHUNK):
            rows = db.execute(
                select(model.id, model.public_id, name_col, model.slug, model.field_provenance).where(
                    model.id.in_(ids[start : start + _IN_CHUNK])
                )
            ).all()
            readable = _readable_source_ids(
                db, {str((fp or {}).get(name_key, {}).get("source_id")) for *_, fp in rows}, entitlement
            )
            # Rows whose name source this tier may not read (or that record none) are read through
            # `GatedRecord`, loaded in one query for the chunk rather than one `get` each.
            gated_ids = [
                row_id
                for row_id, *_, fp in rows
                if str((fp or {}).get(name_key, {}).get("source_id")) not in readable
            ]
            loaded = (
                db.scalars(select(model).where(model.id.in_(gated_ids)).options(selectinload(model.sources)))
                if gated_ids
                else None
            )
            gated_rows = {
                r.id: r for r in cast("list[Proposal | Opportunity]", list(loaded.all() if loaded else []))
            }
            for row_id, row_public_id, name, slug, _fp in rows:
                if row_id in gated_rows:
                    record = gated_rows[row_id]
                    name = getattr(gated_record(record, entitlement), name_key)
                out[row_id] = {
                    "subject_public_id": row_public_id,
                    "subject_name": name,
                    "subject_url": f"{WEB_HOST}/{path}/{slug}",
                }
    for e in events:
        out.setdefault(
            e.subject_id,
            {
                "subject_public_id": str(e.subject_id),
                "subject_name": _UNKNOWN_SUBJECT_NAME,
                "subject_url": WEB_HOST,
            },
        )
    return out


def _readable_source_ids(db: Session, source_ids: set[str], entitlement: str) -> set[str]:
    """Those of `source_ids` that `entitlement` may read (`visibility.source_visible`)."""
    wanted = sorted(source_ids - {"None"})
    if not wanted:
        return set()
    return {
        source.id
        for source in db.scalars(select(Source).where(Source.id.in_(wanted))).all()
        if source_visible(source, entitlement)
    }


def event_query_with_filters(request: Request, db: Session, entitlement: str) -> sa.Select[tuple[Event]]:
    """`GET /v1/events`'s filters (docs/23 §7: `subject_type`, `subject_id`, `event_type`,
    `source_id`, `since` as a seq or a timestamp; `changed_key` and `observed_at[from|to]` since
    2026-09-27, lane E14) over `event_visibility_filter`. Moved here from
    `services/api/app.py::list_events` so bulk and exports run the same block. A `since` that is
    neither an integer nor an RFC 3339 instant is a `400 validation_error` (the inline block let
    `fromisoformat` raise, a 500)."""
    # Never past a seq an open transaction may still commit: a client resuming with `since=<the
    # last seq it saw>` would otherwise skip an event that commits late with a lower seq (backend
    # audit 2026-09-30 F5; `services/db/event_horizon.py`). On SQLite this is `MAX(seq)`.
    stmt = select(Event).where(Event.seq <= stable_event_seq(db), *event_visibility_filter(entitlement))
    qp = request.query_params
    if v := qp.get("subject_type"):
        stmt = stmt.where(Event.subject_type.in_(csv_param(v)))
    if v := qp.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.where(Event.source_id.in_(csv_param(v)))
    if v := qp.get("subject_id"):
        subj = resolve_subject(db, v)
        if subj is None:
            stmt = stmt.where(Event.subject_id == _uuid.UUID(int=0))
        else:
            stmt = stmt.where(Event.subject_id == subj.id)
    if v := qp.get("since"):
        if (seq := since_seq(v, request.url.path)) is not None:
            stmt = stmt.where(Event.seq > seq)
        else:
            stmt = stmt.where(Event.observed_at > _parse_instant(v, "since", request.url.path))
    if v := qp.get("changed_key"):
        stmt = stmt.where(_changed_key_filter(db, changed_key_values(v, request.url.path)))
    # Inclusive bounds, as `due_at[from|to]` on opportunities; `since` stays strictly-after.
    if v := qp.get("observed_at[from]"):
        stmt = stmt.where(Event.observed_at >= _parse_instant(v, "observed_at[from]", request.url.path))
    if v := qp.get("observed_at[to]"):
        stmt = stmt.where(Event.observed_at <= _parse_instant(v, "observed_at[to]", request.url.path))
    return stmt


def event_subject_jurisdiction_filter(values: list[str]) -> ColumnElement[bool]:
    """`jurisdiction=` on `/feeds/events.{format}` (lane E15, 2026-09-27): an event has no
    jurisdiction of its own, so the filter reads its *subject's* (`proposal.jurisdiction` or
    `opportunity.jurisdiction`, both NOT NULL), exact match, OR within the facet, as on the record
    lists and feeds. Two uncorrelated `IN` subqueries on the subject id, one per subject type, so
    no join and no duplicate items. No visibility clause here: the feed's own
    `event_visibility_filter` already requires the subject to be visible, so a hidden subject's
    event is absent with or without this filter and its jurisdiction cannot be probed. Any other
    subject type matches no jurisdiction."""
    return sa.or_(
        sa.and_(
            Event.subject_type == "proposal",
            Event.subject_id.in_(select(Proposal.id).where(Proposal.jurisdiction.in_(values))),
        ),
        sa.and_(
            Event.subject_type == "opportunity",
            Event.subject_id.in_(select(Opportunity.id).where(Opportunity.jurisdiction.in_(values))),
        ),
    )


def changed_key_values(raw: str, instance: str) -> list[str]:
    """`?changed_key=a,b` as the list of canonical field names it names (OR within the facet), each
    checked against `_CHANGED_KEY_RE`. Shared with the alert matcher
    (`services/alerts/matching.py`) so a saved search and the list read one value the same way."""
    values = csv_param(raw)
    for value in values:
        if not _CHANGED_KEY_RE.fullmatch(value):
            raise validation_error(
                "changed_key", "changed_key values are canonical field names ([A-Za-z0-9_.-])", instance
            )
    return values


def _changed_key_filter(db: Session, values: list[str]) -> ColumnElement[bool]:
    """Any-of over `event.changed_keys`: an event matches when it changed at least one named key; an
    empty list matches nothing (`IN ()`'s reading, and the matcher's). Postgres: the native `&&`
    overlap on `text[]`. SQLite stores the array as JSON text (`services/db/types.py TextArray`), so
    the test is a substring match on the JSON-quoted token, with `autoescape` so the `_` in
    `lifecycle_state` is a literal, not a LIKE wildcard; `_CHANGED_KEY_RE` keeps the token free of
    quotes and commas, so it can only match one whole element."""
    if not values:
        return sa.false()
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        return Event.changed_keys.op("&&")(list(values))
    as_text = sa.cast(Event.changed_keys, sa.Text)
    return sa.or_(*(as_text.contains(json.dumps(v), autoescape=True) for v in values))


def _parse_instant(value: str, field: str, instance: str) -> dt.datetime:
    """`services/api/records.py::instant_filter`: one parser for every date-time bound, normalised
    to UTC so SQLite (wall-clock text) and Postgres (instants) agree on an offset bound."""
    return instant_filter(field, value, instance)


# ------------------------------------------------------------------------- redistribution clauses
def _link_permits(flag: RedistributionFlag) -> Any:
    return getattr(Licence, flag).is_(True)


def proposal_redistribution_clause(flag: RedistributionFlag) -> ColumnElement[bool]:
    return exists(
        select(ProposalSource.id)
        .join(Licence, Licence.id == ProposalSource.licence_id)
        .where(
            ProposalSource.proposal_id == Proposal.id, ProposalSource.active.is_(True), _link_permits(flag)
        )
    )


def opportunity_redistribution_clause(flag: RedistributionFlag) -> ColumnElement[bool]:
    return exists(
        select(OpportunitySource.id)
        .join(Licence, Licence.id == OpportunitySource.licence_id)
        .where(
            OpportunitySource.opportunity_id == Opportunity.id,
            OpportunitySource.active.is_(True),
            _link_permits(flag),
        )
    )


def event_redistribution_clause(flag: RedistributionFlag) -> ColumnElement[bool]:
    return exists(select(Licence.id).where(Licence.id == Event.licence_id, _link_permits(flag)))


def redistributable_link(
    links: list[ProposalSource] | list[OpportunitySource], flag: RedistributionFlag
) -> ProposalSource | OpportunitySource | None:
    """The link whose provenance a CSV row prints: the first active one whose licence permits
    the shape (the same test the SQL clause applied, so it always exists for a selected record)."""
    for link in links:
        if link.active and bool(getattr(link.source.licence, flag)):
            return link
    return None


# ------------------------------------------------------------------------------ the statement
def resource_statement(
    resource: Resource,
    params: Mapping[str, Any],
    *,
    db: Session,
    entitlement: str,
    redistribution: RedistributionFlag,
    instance: str,
    all_opportunity_statuses: bool = False,
) -> sa.Select[Any]:
    """The visible, filtered, redistribution-gated statement for `resource` under `params`, not yet
    ordered or limited (the caller pages or caps it). `all_opportunity_statuses=True` (bulk) lifts
    the opportunity list's `status=open` default so an incremental sync sees every status unless
    the caller filters one; an export keeps the list's default, since it exports the list."""
    request = synthetic_request(params, path=instance)
    if resource == "proposal":
        # `updated_since` is applied by the list's own filter stack since 2026-09-27 (lane E15;
        # `records.RECORD_TIME_BOUNDS`), with the meaning it always had here: `last_changed` at or
        # after the instant.
        return _proposal_query_with_filters(request, entitlement).where(
            proposal_redistribution_clause(redistribution)
        )
    if resource == "opportunity":
        if all_opportunity_statuses and "status" not in params:
            request = synthetic_request({**params, "status": list(OPPORTUNITY_STATUSES)}, path=instance)
        return _opportunity_query_with_filters(request, db, entitlement).where(
            opportunity_redistribution_clause(redistribution)
        )
    return event_query_with_filters(request, db, entitlement).where(
        event_redistribution_clause(redistribution)
    )


def lean_load_options(resource: Resource, *, identifiers: bool) -> list[LoaderOption]:
    """Loader options for a whole-result-set read (an export, a bulk page), measured on the real
    `data/normalized` load (services/README.md "Exports, bulk and documents"):

    - defer the JSON columns neither shape prints -- every source link's `raw`/`normalised`
      (`raw` is never served off the admin tier at all; `normalised` is read only for the rare
      field whose provenance source is hidden, `services/api/visibility.py::GatedRecord`);
      `identifiers=True` keeps `identifiers`, which the bulk line (the detail shape) prints. The
      record's `field_provenance` and `overrides` are loaded: the served view reads both for
      every field it prints (2026-10-06);
    - load the many-to-one `source`/`licence` hops lazily instead of by the models' default
      `joined` strategy: there are a few dozen sources and licences against thousands of rows, so
      after the first row each hop is an identity-map hit with no SQL, where the join re-read and
      re-hydrated them on every row."""
    if resource == "proposal":
        options: list[LoaderOption] = [
            joinedload(Proposal.location).lazyload(Location.source),
            joinedload(Proposal.location).lazyload(Location.licence),
            selectinload(Proposal.sources).defer(ProposalSource.raw).defer(ProposalSource.normalised),
            selectinload(Proposal.sources).lazyload(ProposalSource.source),
        ]
        return options if identifiers else [*options, defer(Proposal.identifiers)]
    if resource == "opportunity":
        options = [
            joinedload(Opportunity.location).lazyload(Location.source),
            joinedload(Opportunity.location).lazyload(Location.licence),
            selectinload(Opportunity.sources)
            .defer(OpportunitySource.raw)
            .defer(OpportunitySource.normalised),
            selectinload(Opportunity.sources).lazyload(OpportunitySource.source),
        ]
        return options if identifiers else [*options, defer(Opportunity.identifiers)]
    return [lazyload(Event.source), lazyload(Event.licence)]


def resource_model(resource: Resource) -> type[Proposal] | type[Opportunity] | type[Event]:
    return cast(
        "type[Proposal] | type[Opportunity] | type[Event]",
        {"proposal": Proposal, "opportunity": Opportunity, "event": Event}[resource],
    )


__all__ = [
    "DEFAULT_SORTS",
    "EVENT_FILTERS",
    "EVENT_LIST_PARAMS",
    "EVENT_SORT_ALLOWLIST",
    "EXPORT_QUERY_KEYS",
    "RESOURCE_ALIASES",
    "SORT_ALLOWLISTS",
    "RedistributionFlag",
    "Resource",
    "changed_key_values",
    "check_query_values",
    "event_query_with_filters",
    "event_subject_jurisdiction_filter",
    "lean_load_options",
    "normalise_resource",
    "redistributable_link",
    "resolve_subject",
    "resource_model",
    "resource_statement",
    "subject_info",
    "subject_infos",
    "synthetic_request",
    "validate_export_query",
]
