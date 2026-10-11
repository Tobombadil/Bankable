"""`GET /v1/organizations/{public_id}/pipeline`: a company's pipeline in one read (2026-10-10, lane P).

The beta's first job (owner, 2026-10-10) is "show me this developer's or owner's pipeline, with a
source for every claim". The company page used to page through `/v1/organizations/{id}/proposals`
(up to 5 x 200 rows) and call its summary partial beyond that; this endpoint answers the whole
summary from the store: counts and MW by lifecycle state, by technology and by grid operator, the
records no register lists any more, and the registers behind them with counts and their latest
retrieval.

**Every count is the total of a list query.** The records counted are selected by the list's own
filter stack (`records._apply_proposal_filters`) over the list's own visibility predicate, from
the query this response returns as `list_query` (`sponsor_id`, plus `sponsor_scope` when the scope
spans subsidiaries). So `GET /v1/proposals?<list_query>&<the bucket's filter>&include=count` answers
each count exactly:

- a `by_lifecycle_state` entry's `listed` / `not_listed`: `lifecycle_state=<state>&listed=true|false`;
- `active`: `lifecycle_state=<active_states>&listed=true`; `not_listed`: the same with `listed=false`;
- a `by_technology` / `by_iso` entry: the `active` query plus `technology=` / `iso=` (the grid
  operator's token: `iso=` matches every stored spelling, `services/api/grid_operators.py`);
- a `sources` entry: `source_id=<id>` over every lifecycle state.

A record not visible to the caller's tier is never counted (`proposal_visibility_filter`). A
visible record whose stored value for a counted field came from a source the tier may not read is
counted as served (`visibility.GatedRecord`), never as stored, as the map's totals are
(`records._proposal_geo_features`): the list filters stored values, so only such a record could
make a count differ from its link. A null technology or operator is a bucket of its own (`null`),
which no list filter can ask for.

**Capacity** is summed only for kinds whose `capacity_mw` is generating or storage capacity: a
data centre's MW is demand, a line's is transfer capability, a CO2 well has none, so those kinds
(`CAPACITY_NOT_SUMMED_KINDS`) are counted in `not_summed_records` and never added. `capacity_mw`
is `null` where no counted record states one, never `0`.

**Rate limits.** A `GET` without `q`: one unit of the caller's tier read window and of its daily
cap (`services/api/ratelimit.py`), like any other read; it spends no search unit and has no window
of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api import geo_cache
from services.api.auth import AuthContext, get_auth_context
from services.api.common import iso
from services.api.deps import get_db
from services.api.grid_operators import canonical_iso
from services.api.listing import listed_clause
from services.api.orgtree import org_scope, scope_from_request
from services.api.params import check_allowed
from services.api.records import _apply_proposal_filters, _source_licence_aggregate
from services.api.resource_queries import synthetic_request
from services.api.serialize import build_envelope, build_meta, serialize_organization_summary
from services.api.slippage import SLIP_ACTIVE_STATES
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    gated_views,
    hidden_provenance_clause,
    permitted_source_states,
    proposal_visibility_filter,
    source_split,
    visible_organization_or_404,
)
from services.db.models import LIFECYCLE_STATES, Licence, Proposal, ProposalSource, Source

router = APIRouter()

#: The active pipeline: announced through under construction, the list's default view (the web's
#: `ACTIVE_PROPOSAL_STATES`, asserted equal to this tuple in `services/api/test_slippage.py`).
ACTIVE_LIFECYCLE_STATES: tuple[str, ...] = SLIP_ACTIVE_STATES
#: `by_lifecycle_state` order: the active states in lifecycle order, then the rest, `unknown` last.
STATE_ORDER: tuple[str, ...] = (
    *ACTIVE_LIFECYCLE_STATES,
    *(s for s in LIFECYCLE_STATES if s not in ACTIVE_LIFECYCLE_STATES and s != "unknown"),
    "unknown",
)
#: Kinds whose `capacity_mw` is not generating or storage capacity, so it is never summed (module
#: docstring). Generation, storage, nuclear and other are summed.
CAPACITY_NOT_SUMMED_KINDS: tuple[str, ...] = ("load", "transmission", "pipeline", "lng", "ccs", "hydrogen")
#: The fields the summary reads: a record whose stored value for one of them came from a hidden
#: source is read through its served view.
COUNTED_FIELDS: tuple[str, ...] = ("lifecycle_state", "technology", "iso", "kind", "capacity_mw")
#: Proposal ids bound per sources statement (under SQLite's 32,766 host parameters).
_ID_CHUNK = 5_000


@dataclass
class Tally:
    """One bucket: how many records, and their summed capacity (module docstring)."""

    records: int = 0
    capacity_mw: float = 0.0
    capacity_records: int = 0
    not_summed_records: int = 0

    def add(self, kind: str | None, capacity_mw: Any) -> None:
        self.records += 1
        if kind in CAPACITY_NOT_SUMMED_KINDS:
            self.not_summed_records += 1
            return
        if capacity_mw is not None:
            self.capacity_mw += float(capacity_mw)
            self.capacity_records += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "capacity_mw": round(self.capacity_mw, 3) if self.capacity_records else None,
            "capacity_records": self.capacity_records,
            "not_summed_records": self.not_summed_records,
        }


@dataclass(frozen=True)
class _Row:
    """One counted record as served: the counted fields plus whether a register still lists it."""

    id: Any
    lifecycle_state: str
    technology: str | None
    iso: str | None
    kind: str | None
    capacity_mw: Any
    listed: bool


def list_query_for(org_public_id: str, scope: str, organizations: int) -> dict[str, str]:
    """The `GET /v1/proposals` filters that select exactly the organisation's records at `scope`.
    `sponsor_scope` is named only when the scope spans more than the organisation itself: the
    records are the same either way, and the shorter query is the one a reader shares."""
    query = {"sponsor_id": org_public_id}
    if scope != "self" and organizations > 1:
        query["sponsor_scope"] = scope
    return query


def _rows(
    db: Session, list_query: Mapping[str, str], entitlement: str, instance: str
) -> tuple[list[_Row], list[Any]]:
    """The counted records as served, and their ids (for the sources and the licence summary)."""
    hidden = source_split(db, entitlement)[1]
    hidden = hidden - geo_cache.unused_hidden_sources(db, hidden)
    gate = hidden_provenance_clause(Proposal, COUNTED_FIELDS, hidden) if hidden else sa.false()
    stmt = select(
        Proposal.id,
        Proposal.lifecycle_state,
        Proposal.technology,
        Proposal.iso,
        Proposal.kind,
        Proposal.capacity_mw,
        listed_clause(entitlement).label("listed"),
        gate.label("gated"),
    ).where(*proposal_visibility_filter(entitlement))
    stmt = _apply_proposal_filters(stmt, synthetic_request(list_query, path=instance), entitlement)
    raw = db.execute(stmt).all()
    views = gated_views(db, Proposal, [r.id for r in raw if r.gated], entitlement) if hidden else {}
    rows: list[_Row] = []
    for r in raw:
        view = views.get(r.id) if r.gated else None
        source: Any = view if view is not None else r
        rows.append(
            _Row(
                id=r.id,
                lifecycle_state=str(source.lifecycle_state or "unknown"),
                technology=source.technology or None,
                iso=canonical_iso(source.iso) or None,
                kind=source.kind,
                capacity_mw=source.capacity_mw,
                listed=bool(r.listed),
            )
        )
    return rows, [r.id for r in raw]


def _ranked(tallies: Mapping[str | None, Tally], key: str) -> list[dict[str, Any]]:
    """Largest capacity first, then most records, then the token; the `null` bucket last."""
    ordered = sorted(
        tallies.items(),
        key=lambda item: (
            item[0] is None,
            -(item[1].capacity_mw if item[1].capacity_records else -1.0),
            -item[1].records,
            item[0] or "",
        ),
    )
    return [{key: token, **tally.as_dict()} for token, tally in ordered]


def _sources(db: Session, ids: Sequence[Any], entitlement: str) -> list[dict[str, Any]]:
    """Per register the tier may read: how many of the counted records it lists or listed (an
    active link, `gone_at` or not, as `source_id=` selects), and its latest retrieval. The clauses
    are `visible_source_link_filter`'s, so a count is the `source_id=` link's total."""
    stmt = (
        select(
            Source.id,
            Source.name,
            Licence.id,
            Licence.reuse_class,
            func.coalesce(Source.attribution_text, Licence.attribution_text),
            func.count(sa.distinct(ProposalSource.proposal_id)),
            func.max(ProposalSource.retrieved_at),
        )
        .select_from(ProposalSource)
        .join(Source, Source.id == ProposalSource.source_id)
        .join(Licence, Licence.id == Source.licence_id)
        .where(
            ProposalSource.proposal_id.in_(sa.bindparam("ids", expanding=True)),
            ProposalSource.active.is_(True),
            Source.publish_state.in_(permitted_source_states(entitlement)),
            Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
        )
        .group_by(Source.id, Source.name, Licence.id, Licence.reuse_class, Source.attribution_text)
        .group_by(Licence.attribution_text)
    )
    merged: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        # Chunks partition the records, so per-chunk distinct counts add up exactly.
        for sid, name, lic_id, reuse, credit, count, latest in db.execute(
            stmt, {"ids": list(ids[start : start + _ID_CHUNK])}
        ):
            entry = merged.setdefault(
                sid,
                {
                    "source_id": sid,
                    "name": name,
                    "licence_id": lic_id,
                    "reuse_class": reuse,
                    "attribution_text": credit,
                    "records": 0,
                    "retrieved_at_max": None,
                },
            )
            entry["records"] += int(count)
            if latest is not None and (
                entry["retrieved_at_max"] is None or latest > entry["retrieved_at_max"]
            ):
                entry["retrieved_at_max"] = latest
    out = sorted(merged.values(), key=lambda s: (-s["records"], s["name"], s["source_id"]))
    for entry in out:
        entry["retrieved_at_max"] = iso(entry["retrieved_at_max"])
    return out


def pipeline_summary(rows: Iterable[_Row]) -> dict[str, Any]:
    """The counted buckets over `rows` (module docstring), without the envelope."""
    totals, active, not_listed = Tally(), Tally(), Tally()
    by_state: dict[str, dict[bool, Tally]] = {}
    by_technology: dict[str | None, Tally] = {}
    by_iso: dict[str | None, Tally] = {}
    not_summed: dict[str, int] = {}
    for row in rows:
        totals.add(row.kind, row.capacity_mw)
        if row.kind in CAPACITY_NOT_SUMMED_KINDS:
            not_summed[str(row.kind)] = not_summed.get(str(row.kind), 0) + 1
        state = by_state.setdefault(row.lifecycle_state, {True: Tally(), False: Tally()})
        state[row.listed].add(row.kind, row.capacity_mw)
        if row.lifecycle_state not in ACTIVE_LIFECYCLE_STATES:
            continue
        if not row.listed:
            not_listed.add(row.kind, row.capacity_mw)
            continue
        active.add(row.kind, row.capacity_mw)
        by_technology.setdefault(row.technology, Tally()).add(row.kind, row.capacity_mw)
        by_iso.setdefault(row.iso, Tally()).add(row.kind, row.capacity_mw)
    order = {s: i for i, s in enumerate(STATE_ORDER)}
    return {
        "active_states": list(ACTIVE_LIFECYCLE_STATES),
        "capacity_not_summed_kinds": list(CAPACITY_NOT_SUMMED_KINDS),
        "totals": totals.as_dict(),
        "active": active.as_dict(),
        "not_listed": not_listed.as_dict(),
        "by_lifecycle_state": [
            {"lifecycle_state": s, "listed": t[True].as_dict(), "not_listed": t[False].as_dict()}
            for s, t in sorted(by_state.items(), key=lambda item: (order.get(item[0], len(order)), item[0]))
        ],
        "by_technology": _ranked(by_technology, "technology"),
        "by_iso": _ranked(by_iso, "iso"),
        "not_summed_by_kind": [{"kind": k, "records": n} for k, n in sorted(not_summed.items())],
    }


@router.get("/v1/organizations/{public_id}/pipeline")
def get_organization_pipeline(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, {"scope"})
    instance = request.url.path
    org = visible_organization_or_404(db, public_id, instance)
    scope = scope_from_request(request)
    scope_result = org_scope(db, org, scope)
    list_query = list_query_for(org.public_id, scope, scope_result.organizations)
    rows, ids = _rows(db, list_query, ctx.entitlement, instance)
    data = {
        "organization": serialize_organization_summary(org),
        "scope": scope_result.as_meta(),
        "list_query": list_query,
        **pipeline_summary(rows),
        "sources": _sources(db, ids, ctx.entitlement),
    }
    return build_envelope(
        data,
        meta=build_meta("proposal", tier=ctx.entitlement),
        licence_summary=_source_licence_aggregate(
            db, ProposalSource, ProposalSource.proposal_id, ids, ctx.entitlement
        ),
    )
