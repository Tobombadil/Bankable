"""`GET /v1/organizations/{public_id}/portfolio`: a company's fleet of existing assets in one read
(2026-10-21, lane W1; docs/16 §5 and §4.1).

The owner asked for a company's current assets beside its pipeline (docs/16 §0 item 3). The company
page read `GET /v1/organizations/{id}/assets` one row per edge and counted in the template, which
printed four different asset counts on one page and summed no MW (docs/16 §4.1 item 7). This is
the fleet's twin of `services/api/org_pipeline.py`: one query over the scope's `asset_owner` edges
joined to their assets, grouped in Python, answering how many assets, how many MW and how much of
that the group owns, by status, role, technology, state and asset type, with the registers behind
the numbers and their latest retrieval.

**What is counted.** An asset is counted when an edge to it is held by an organisation in the scope
(`services/api/orgtree.py::org_scope`, which leaves out a taken-down subsidiary and everything below
it), the asset passes the asset visibility predicate, and that edge's own source and licence do
(`services/api/assets.py::organization_edge_filters`; the publishable classes are the posture's,
`services/posture.py`). An edge only a hidden register states lends nothing: not the asset, not a
role, not a share.

**Distinct assets.** One asset held by two companies in the scope, named by two registers, or both
owned and operated counts once in each bucket it falls in. `by_role` counts it under each role the
scope holds it in, so the roles can add up to more than `totals.assets`.

**MW.** `capacity_mw` is nameplate MW summed over the counted assets that state one: power plants and
landfill-gas electricity projects do (and one RNG row, on the 2026-10-09 load); pipelines, storage
fields, processing plants and the other types state their capacity in their own units and are
counted, not summed. `capacity_assets` says how many were summed. `capacity_mw` is `null`, never
`0`, when no counted asset states one.

**Owned MW.** Where a register states ownership shares (EIA-860 Schedule 4 names the subsidiary that
holds a stake, EPA GHGRP often its parent), an asset's owned fraction is: per register, the sum of
the shares the scope's holders have in it; then the largest of those per-register sums, never their
total, because two registers stating one stake at two levels of a group is one stake; capped at 100
(17 per-register sums on the 2026-10-09 load exceed it). `owned_mw` is that fraction of each
asset's MW, summed over the assets that have both a stated share and MW; `owned_mw_assets` says how
many. An operator edge states no share. A natural person's share is withheld on every surface
(`services/api/serialize.py::personal_share`) and is not used here either.

**Status.** The headline (`totals`, `by_role`, `by_technology`, `by_state`, `by_asset_type`) counts
the statuses in `statuses`: `operating`, `standby` and `retiring` unless `status=` names others.
`by_status` counts every status in the scope whatever `status=` says, so a page prints the retired
fleet beside the headline without adding it in. No edge reaches an `unknown`-status asset on the
2026-10-09 load (those are 13,084 transmission lines with no holder); if one appears it is reported
under `unknown` and left out of the default headline, as `retired` is.

**Every count is the total of a list query.** A bucket's `list_query` is the
`GET /v1/organizations/{public_id}/assets` filters that select exactly its assets: `scope` (named only
when the scope spans more than the organisation), `status`, and the bucket's `role`, `technology`,
`state` or `asset_type`. That list is one row per edge, so the count is the number of distinct assets
(`public_id`) it returns, which is what a page grouping the rows per asset shows. A `null`
technology or state is a bucket of its own, which no filter selects (`list_query: null`). The
registers carry counts and no query: the list has no source filter.

**Registers.** `asset_registers` are the registers the counted assets' rows come from (the asset's own
source); `ownership_registers` the registers of the counted edges. Each says how many counted assets
it backs, over every status, and its latest retrieval; its licence is the one its rows carry.

**Cost.** Three statements beside the organisation lookup and the scope walk, whatever the scope
spans: the edges (one row per visible edge), the registers and their licences.

Assets carry no tier gating (`services/api/assets.py`'s module docstring), so every caller sees the
same response. A `GET` without `q` spends one unit of the caller's read window and daily cap
(`services/api/ratelimit.py`), like any other read.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.assets import _status_filter_values, organization_edge_filters
from services.api.common import iso
from services.api.deps import get_db
from services.api.orgtree import org_scope, scope_from_request
from services.api.params import check_allowed
from services.api.serialize import (
    build_envelope,
    build_meta,
    licence_summary_from_source_aggregates,
    serialize_organization_summary,
)
from services.api.visibility import visible_organization_or_404
from services.db.models import (
    ASSET_OWNER_ROLES,
    ASSET_STATUSES,
    Asset,
    AssetOwner,
    Licence,
    Organization,
    Source,
)

router = APIRouter()

#: The headline's statuses when `status=` is absent: what a company runs today. `retired` (and
#: `unknown`) are counted in `by_status` beside it, never in it by default.
CURRENT_STATUSES: tuple[str, ...] = ("operating", "standby", "retiring")
#: The most of an asset a group can own, whatever its registers' shares add up to.
SHARE_CAP_PCT = 100.0


@dataclass
class _Held:
    """One counted asset: the fields the list filters on, and what the scope's edges say of it."""

    asset_type: str
    status: str
    technology: str | None
    state_code: str | None
    capacity_mw: float | None
    roles: set[str] = field(default_factory=set)
    #: Per ownership register, the sum of the shares the scope's holders have in the asset.
    shares: dict[str, float] = field(default_factory=dict)

    def owned_fraction(self) -> float | None:
        """The largest per-register sum, capped (module docstring); `None` when no share is stated."""
        if not self.shares:
            return None
        return min(max(self.shares.values()), SHARE_CAP_PCT) / 100.0


@dataclass
class Tally:
    """One bucket: how many assets, their summed MW, and the owned part of it."""

    assets: int = 0
    capacity_mw: float = 0.0
    capacity_assets: int = 0
    owned_mw: float = 0.0
    owned_mw_assets: int = 0

    def add(self, held: _Held) -> None:
        self.assets += 1
        if held.capacity_mw is None:
            return
        self.capacity_mw += held.capacity_mw
        self.capacity_assets += 1
        fraction = held.owned_fraction()
        if fraction is not None:
            self.owned_mw += held.capacity_mw * fraction
            self.owned_mw_assets += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "assets": self.assets,
            "capacity_mw": round(self.capacity_mw, 3) if self.capacity_assets else None,
            "capacity_assets": self.capacity_assets,
            "owned_mw": round(self.owned_mw, 3) if self.owned_mw_assets else None,
            "owned_mw_assets": self.owned_mw_assets,
        }


@dataclass
class _Register:
    """A register behind the counted assets: which assets it backs, its latest row and that row's
    licence."""

    assets: set[Any] = field(default_factory=set)
    retrieved_at_max: dt.datetime | None = None
    licence_id: str | None = None

    def add(self, asset_id: Any, retrieved_at: dt.datetime, licence_id: str) -> None:
        self.assets.add(asset_id)
        if self.retrieved_at_max is None or retrieved_at > self.retrieved_at_max:
            self.retrieved_at_max = retrieved_at
            self.licence_id = licence_id


def scope_query(scope: str, organizations: int) -> dict[str, str]:
    """`scope` for the list query, named only when the scope spans more than the organisation: the
    rows are the same either way, and the shorter query is the one a reader shares."""
    return {"scope": scope} if scope != "self" and organizations > 1 else {}


def headline_statuses(request: Request) -> tuple[str, ...]:
    """`status=` (csv of the asset statuses; an unknown one is a 400) in vocabulary order, or
    `CURRENT_STATUSES` when absent or empty."""
    asked = _status_filter_values(request)
    if not asked:
        return CURRENT_STATUSES
    return tuple(s for s in ASSET_STATUSES if s in set(asked))


def _held_assets(
    db: Session, holder_ids: list[Any]
) -> tuple[dict[Any, _Held], dict[str, _Register], dict[str, _Register]]:
    """Every counted asset, and the asset and ownership registers behind them, from one query."""
    stmt = (
        select(
            AssetOwner.asset_id,
            AssetOwner.role,
            AssetOwner.share_pct,
            AssetOwner.source_id.label("edge_source_id"),
            AssetOwner.retrieved_at.label("edge_retrieved_at"),
            AssetOwner.licence_id.label("edge_licence_id"),
            Organization.personal_data,
            Asset.asset_type,
            Asset.status,
            Asset.technology,
            Asset.state_code,
            Asset.capacity_mw,
            Asset.source_id,
            Asset.retrieved_at,
            Asset.licence_id,
        )
        .join(Asset, Asset.id == AssetOwner.asset_id)
        .join(Organization, Organization.id == AssetOwner.organization_id)
        .where(*organization_edge_filters(holder_ids))
    )
    held: dict[Any, _Held] = {}
    asset_registers: dict[str, _Register] = {}
    ownership_registers: dict[str, _Register] = {}
    for r in db.execute(stmt):
        asset = held.get(r.asset_id)
        if asset is None:
            asset = held[r.asset_id] = _Held(
                asset_type=r.asset_type,
                status=r.status,
                technology=r.technology or None,
                state_code=r.state_code or None,
                capacity_mw=float(r.capacity_mw) if r.capacity_mw is not None else None,
            )
            asset_registers.setdefault(r.source_id, _Register()).add(r.asset_id, r.retrieved_at, r.licence_id)
        asset.roles.add(r.role)
        if r.role == "owner" and r.share_pct is not None and not r.personal_data:
            asset.shares[r.edge_source_id] = asset.shares.get(r.edge_source_id, 0.0) + float(r.share_pct)
        ownership_registers.setdefault(r.edge_source_id, _Register()).add(
            r.asset_id, r.edge_retrieved_at, r.edge_licence_id
        )
    return held, asset_registers, ownership_registers


def _ranked(
    tallies: Mapping[str | None, Tally], key: str, list_query: Callable[[str], dict[str, str]]
) -> list[dict[str, Any]]:
    """Largest MW first, then most assets, then the token; the `null` bucket last, with no query."""
    ordered = sorted(
        tallies.items(),
        key=lambda item: (
            item[0] is None,
            -(item[1].capacity_mw if item[1].capacity_assets else -1.0),
            -item[1].assets,
            item[0] or "",
        ),
    )
    return [
        {key: token, **tally.as_dict(), "list_query": list_query(token) if token is not None else None}
        for token, tally in ordered
    ]


def portfolio_summary(
    held: Iterable[_Held], statuses: tuple[str, ...], base: Mapping[str, str]
) -> dict[str, Any]:
    """The counted buckets over `held` (module docstring), each with its list query, without the
    envelope. `base` is `scope_query(...)`."""
    status_csv = ",".join(statuses)
    headline = {**base, "status": status_csv}
    totals = Tally()
    by_status: dict[str, Tally] = {}
    by_role: dict[str, Tally] = {}
    by_technology: dict[str | None, Tally] = {}
    by_state: dict[str | None, Tally] = {}
    by_asset_type: dict[str | None, Tally] = {}
    for asset in held:
        by_status.setdefault(asset.status, Tally()).add(asset)
        if asset.status not in statuses:
            continue
        totals.add(asset)
        for role in asset.roles:
            by_role.setdefault(role, Tally()).add(asset)
        by_technology.setdefault(asset.technology, Tally()).add(asset)
        by_state.setdefault(asset.state_code, Tally()).add(asset)
        by_asset_type.setdefault(asset.asset_type, Tally()).add(asset)
    status_order = {s: i for i, s in enumerate(ASSET_STATUSES)}
    role_order = {r: i for i, r in enumerate(ASSET_OWNER_ROLES)}
    return {
        "statuses": list(statuses),
        "list_query": headline,
        "totals": totals.as_dict(),
        "by_status": [
            {"status": s, **t.as_dict(), "list_query": {**base, "status": s}}
            for s, t in sorted(
                by_status.items(), key=lambda item: status_order.get(item[0], len(status_order))
            )
        ],
        "by_role": [
            {"role": r, **t.as_dict(), "list_query": {**headline, "role": r}}
            for r, t in sorted(by_role.items(), key=lambda item: role_order.get(item[0], len(role_order)))
        ],
        "by_technology": _ranked(by_technology, "technology", lambda t: {**headline, "technology": t}),
        "by_state": _ranked(by_state, "state_code", lambda s: {**headline, "state": s}),
        "by_asset_type": _ranked(by_asset_type, "asset_type", lambda t: {**headline, "asset_type": t}),
    }


def _registers(
    db: Session, asset_registers: Mapping[str, _Register], ownership_registers: Mapping[str, _Register]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """`asset_registers`, `ownership_registers` and the envelope's `licence_summary` (one row per
    register, crediting the distinct assets it backs in either role)."""
    registers = {**asset_registers, **ownership_registers}
    if not registers:
        return [], [], licence_summary_from_source_aggregates([])
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_(sorted(registers))))}
    licence_ids = {
        r.licence_id for r in (*asset_registers.values(), *ownership_registers.values()) if r.licence_id
    }
    licences = {lic.id: lic for lic in db.scalars(select(Licence).where(Licence.id.in_(sorted(licence_ids))))}

    def rows(by_source: Mapping[str, _Register]) -> list[dict[str, Any]]:
        out = []
        for sid, reg in by_source.items():
            source, licence = sources[sid], licences[str(reg.licence_id)]
            out.append(
                {
                    "source_id": sid,
                    "name": source.name,
                    "licence_id": licence.id,
                    "reuse_class": licence.reuse_class,
                    "attribution_text": source.attribution_text or licence.attribution_text,
                    "assets": len(reg.assets),
                    "retrieved_at_max": iso(reg.retrieved_at_max),
                }
            )
        return sorted(out, key=lambda s: (-s["assets"], s["name"], s["source_id"]))

    summary_rows = []
    for sid in sorted(registers):
        both = [r for r in (asset_registers.get(sid), ownership_registers.get(sid)) if r is not None]
        latest = max(both, key=lambda r: (r.retrieved_at_max is not None, r.retrieved_at_max))
        source, licence = sources[sid], licences[str(latest.licence_id)]
        summary_rows.append(
            (
                sid,
                source.name,
                source.operator,
                licence.id,
                licence.name,
                licence.url,
                licence.reuse_class,
                source.attribution_text or licence.attribution_text,
                licence.requires_link_back,
                latest.retrieved_at_max,
                len(set().union(*(r.assets for r in both))),
            )
        )
    return (
        rows(asset_registers),
        rows(ownership_registers),
        licence_summary_from_source_aggregates(summary_rows),
    )


@router.get("/v1/organizations/{public_id}/portfolio")
def get_organization_portfolio(
    public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
) -> Any:
    check_allowed(request, {"scope", "status"})
    org = visible_organization_or_404(db, public_id, request.url.path)
    scope = scope_from_request(request)
    statuses = headline_statuses(request)
    scope_result = org_scope(db, org, scope)
    held, asset_registers, ownership_registers = _held_assets(db, scope_result.ids)
    asset_rows, ownership_rows, licence_summary = _registers(db, asset_registers, ownership_registers)
    data = {
        "organization": serialize_organization_summary(org),
        "scope": scope_result.as_meta(),
        **portfolio_summary(held.values(), statuses, scope_query(scope, scope_result.organizations)),
        "asset_registers": asset_rows,
        "ownership_registers": ownership_rows,
    }
    return build_envelope(data, meta=build_meta(lag_days=0, tier="public"), licence_summary=licence_summary)
