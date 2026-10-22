"""Organisation statistics: what every company and its group hold and pursue, counted once for the
whole store, so `GET /v1/organizations` can search, filter and sort by size (2026-10-22, lane
org-stats; owner: "Allow me to sort by companies with the most assets or GWs").

**What is counted, and by whose rules.** Each figure is one the company page already prints, from
the endpoint that prints it, through that endpoint's own code:

- `assets`, `operated_assets`, `owned_assets`, `operating_mw`, `owned_mw`: the headline of
  `GET /v1/organizations/{id}/portfolio` (`services/api/org_portfolio.py`). Distinct operating,
  standby and retiring assets, counted once however many companies in the scope or registers name
  them; nameplate MW summed over those that state one (`null`, never 0, when none does); owned MW
  the largest per-register sum of the scope's stated shares, capped at 100 %, never summed across
  registers; a natural person's share never used. The edges and their fold are that module's
  (`edge_statement`, `fold_edges`, `portfolio_summary`), not a re-derivation.
- `pipeline_count`, `pipeline_mw`, `withdrawn_count`, `proposals`: `active`, `by_lifecycle_state`
  and `totals` of `GET /v1/organizations/{id}/pipeline` (`services/api/org_pipeline.py`,
  `pipeline_summary`). Active is announced through under construction and still listed by a
  register the tier may read, which leaves out withdrawn, built and cancelled projects (and
  `unknown`-state and delisted ones, as the company page does). Values are counted as served
  (`GatedRecord`), and the MW of loads, lines and other non-generating kinds is never summed.
- `rfps_issued`, `rfps_open`: visible opportunities whose issuer is in the scope, and those served
  `open`.
- `states`, `isos`, `technologies`: the states, grid operators and technologies of the counted
  current assets, active pipeline and issued opportunities, for the list's filters. A state is an
  ISO 3166-2 subdivision (`US-TX`); a country-level jurisdiction is not one.

**Scopes.** `self` is the organisation alone; `group` is it and every organisation below it in the
ownership tree: `services/api/orgtree.py::org_scope(..., "all")`'s walk, made in memory over the
listed organisations (`group_scopes`; a taken-down subsidiary and everything below it are left
out, a cycle is visited once, the same caps apply).

**Tiers.** Assets carry no tier gating, so their figures are the same on both rows. Proposals and
opportunities are counted under `proposal_visibility_filter`/`opportunity_visibility_filter` at
`public` and at `pro` (which `api` shares: the predicate treats them identically).

**The gate key.** Figures are computed by a job, not per request, so something that hides a counted
row between two refreshes (an operator unpublishing a source, a licence reclassified, the posture
flipped, a record or organisation taken down, an organisation flagged as a natural person) must
not leave its count on the page. `gate_key` fingerprints exactly those inputs; a row is served only
while its stored key equals the current one (`services/api/organizations.py`), so such a change
withdraws every figure within `GATE_KEY_TTL_S` (the admin takedown promises 60 s) until the next
refresh writes figures under the new key. Ordinary loads do not move it: new rows enter the counts
at the next refresh, which every context load and resolve pass runs.

**Cost.** A fixed number of whole-store statements however many organisations, trees and records
the store holds (the gate key's six, the listed organisations, every visible edge, and per tier the
visible proposals and opportunities with their links), then per-organisation folds in Python.
Measured in `services/api/org_stats_refresh.py`'s docstring.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api import geo_cache
from services.api.common import iso
from services.api.grid_operators import canonical_iso
from services.api.listing import listed_clause
from services.api.org_pipeline import COUNTED_FIELDS, _Row, pipeline_summary
from services.api.org_portfolio import CURRENT_STATUSES, edge_statement, fold_edges, portfolio_summary
from services.api.orgtree import MAX_DEPTH, MAX_SCOPE_ORGS
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    gated_views,
    hidden_provenance_clause,
    opportunity_visibility_filter,
    organization_visibility_filter,
    proposal_visibility_filter,
    source_split,
    visible_source_link_filter,
)
from services.db.models import (
    ORGANIZATION_STATS_SCOPES,
    ORGANIZATION_STATS_TIERS,
    RECORD_PUBLISH_STATES,
    Licence,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)

STATS_SCOPES: tuple[str, ...] = ORGANIZATION_STATS_SCOPES
TIERS: tuple[str, ...] = ORGANIZATION_STATS_TIERS
#: The ownership-tree scope a `group` row counts (`services/api/orgtree.py`).
GROUP_TREE_SCOPE = "all"
#: How long a process reuses the gate key it read (module docstring). A write this process makes to
#: a table the key reads drops it at once (`geo_cache.generation`); this bounds a write from another
#: process, well inside the 60 s an admin takedown promises (`effective_within_seconds`).
GATE_KEY_TTL_S = 10.0
#: An ISO 3166-2 subdivision, the form the list's `state` filter takes.
_SUBDIVISION = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")
#: Every record state but `public`, as an `IN` list the `(publish_state, public_at)` indexes serve.
_NOT_PUBLIC: tuple[str, ...] = tuple(s for s in RECORD_PUBLISH_STATES if s != "public")
#: Fields an opportunity's served view answers for the figures (status for `rfps_open`).
_OPPORTUNITY_FIELDS: tuple[str, ...] = ("status", "jurisdiction", "technologies")


def tier_for(entitlement: str) -> str:
    """The stats tier an entitlement reads: `pro` for `pro` and `api`, `public` for anything else
    (`admin` included, which the visibility predicate also treats as public)."""
    return "pro" if entitlement in ("pro", "api") else "public"


def token_set(values: Iterable[str | None]) -> str:
    """`|a|b|` over the distinct non-empty values, sorted; `''` for none (the stored set form)."""
    tokens = sorted({v for v in values if v})
    return f"|{'|'.join(tokens)}|" if tokens else ""


def token_list(stored: str | None) -> list[str]:
    """The stored set form back as a list."""
    return [t for t in (stored or "").split("|") if t]


# ----------------------------------------------------------------------------------- gate key
def gate_key(db: Session) -> str:
    """The fingerprint of everything that can hide a counted row (module docstring): the posture's
    publishable classes; every source's publish state and licence; every licence's class; the
    organisations not public, and those flagged as natural persons; the proposals and opportunities
    taken down after publication (not public, `published_at` set: a takedown, suppression or merge
    keeps the date, while a row a load stores unpublished never had one, so loads do not move it)."""

    def ids(stmt: Any) -> list[str]:
        return sorted(str(i) for i in db.scalars(stmt))

    parts = {
        "reuse": sorted(PUBLISHABLE_REUSE_CLASSES),
        "sources": [
            list(r)
            for r in db.execute(
                select(Source.id, Source.publish_state, Source.licence_id).order_by(Source.id)
            )
        ],
        "licences": [
            list(r) for r in db.execute(select(Licence.id, Licence.reuse_class).order_by(Licence.id))
        ],
        "organizations": ids(select(Organization.id).where(Organization.publish_state.in_(_NOT_PUBLIC))),
        "persons": ids(select(Organization.id).where(Organization.personal_data.is_(True))),
        "proposals": ids(
            select(Proposal.id).where(
                Proposal.publish_state.in_(_NOT_PUBLIC), Proposal.published_at.is_not(None)
            )
        ),
        "opportunities": ids(
            select(Opportunity.id).where(
                Opportunity.publish_state.in_(_NOT_PUBLIC), Opportunity.published_at.is_not(None)
            )
        ),
    }
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


_gate_lock = threading.Lock()
_gate_cache: dict[tuple[int, int], tuple[float, str]] = {}


def current_gate_key(db: Session) -> str:
    """`gate_key(db)`, reused for `GATE_KEY_TTL_S` per database and write generation."""
    cache_key = (geo_cache.engine_token(db), geo_cache.generation())
    now = time.monotonic()
    with _gate_lock:
        hit = _gate_cache.get(cache_key)
        if hit is not None and now - hit[0] < GATE_KEY_TTL_S:
            return hit[1]
    key = gate_key(db)
    with _gate_lock:
        _gate_cache.clear()  # one live entry: an older generation's key is never read again
        _gate_cache[cache_key] = (now, key)
    return key


def forget_gate_key() -> None:
    """Drop the cached key (tests; a refresh in this process calls it after committing)."""
    with _gate_lock:
        _gate_cache.clear()


# ------------------------------------------------------------------------------------ figures
@dataclass
class _Sources:
    """Per register, the counted things it backs and its latest retrieval among them."""

    records: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))
    latest: dict[str, dt.datetime] = field(default_factory=dict)

    def add(self, source_id: str, subject: Any, retrieved_at: dt.datetime | None) -> None:
        self.records[source_id].add(subject)
        self._seen(source_id, retrieved_at)

    def update(self, other: _Sources) -> None:
        for sid, subjects in other.records.items():
            self.records[sid] |= subjects
            self._seen(sid, other.latest.get(sid))

    def _seen(self, source_id: str, retrieved_at: dt.datetime | None) -> None:
        if retrieved_at is not None and (
            source_id not in self.latest or retrieved_at > self.latest[source_id]
        ):
            self.latest[source_id] = retrieved_at

    def as_list(self) -> list[dict[str, Any]]:
        rows = [
            {"source_id": sid, "records": len(subjects), "retrieved_at_max": iso(self.latest.get(sid))}
            for sid, subjects in self.records.items()
        ]
        return sorted(rows, key=lambda r: (-r["records"], r["source_id"]))


@dataclass
class OrgStats:
    """One `organization_stats` row's figures (`services/db/models.py::OrganizationStats`)."""

    organization_id: Any
    scope: str
    tier: str
    organizations: int = 1
    assets: int = 0
    operated_assets: int = 0
    owned_assets: int = 0
    operating_mw: float | None = None
    operating_mw_assets: int = 0
    owned_mw: float | None = None
    owned_mw_assets: int = 0
    pipeline_count: int = 0
    pipeline_mw: float | None = None
    pipeline_mw_records: int = 0
    withdrawn_count: int = 0
    proposals: int = 0
    rfps_issued: int = 0
    rfps_open: int = 0
    rfp_issuers: str = ""
    states: str = ""
    isos: str = ""
    technologies: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (str(self.organization_id), self.scope, self.tier)

    def columns(self) -> dict[str, Any]:
        """Every stored column but `gate_key` and `computed_at`."""
        return {
            "organization_id": self.organization_id,
            "scope": self.scope,
            "tier": self.tier,
            "organizations": self.organizations,
            "assets": self.assets,
            "operated_assets": self.operated_assets,
            "owned_assets": self.owned_assets,
            "operating_mw": self.operating_mw,
            "operating_mw_assets": self.operating_mw_assets,
            "owned_mw": self.owned_mw,
            "owned_mw_assets": self.owned_mw_assets,
            "pipeline_count": self.pipeline_count,
            "pipeline_mw": self.pipeline_mw,
            "pipeline_mw_records": self.pipeline_mw_records,
            "withdrawn_count": self.withdrawn_count,
            "proposals": self.proposals,
            "rfps_issued": self.rfps_issued,
            "rfps_open": self.rfps_open,
            "rfp_issuers": self.rfp_issuers,
            "states": self.states,
            "isos": self.isos,
            "technologies": self.technologies,
            "sources": self.sources,
        }


@dataclass
class _AssetFigures:
    assets: int = 0
    operated_assets: int = 0
    owned_assets: int = 0
    operating_mw: float | None = None
    operating_mw_assets: int = 0
    owned_mw: float | None = None
    owned_mw_assets: int = 0
    states: set[str] = field(default_factory=set)
    technologies: set[str] = field(default_factory=set)
    sources: _Sources = field(default_factory=_Sources)


@dataclass(frozen=True)
class _ProposalRow:
    """One counted proposal as served: the pipeline's `_Row`, plus its sponsor (stored, as the
    list's `sponsor_id` filter reads it) and its served jurisdiction (for `states`)."""

    sponsor_id: Any
    row: _Row
    jurisdiction: str | None


@dataclass(frozen=True)
class _OpportunityRow:
    id: Any
    issuer_id: Any
    status: str
    jurisdiction: str | None
    technologies: tuple[str, ...]


def _asset_figures(edges: Sequence[Any]) -> _AssetFigures:
    """The portfolio's headline over `edges` (`org_portfolio.fold_edges` + `portfolio_summary`)."""
    if not edges:
        return _AssetFigures()
    held, asset_registers, ownership_registers = fold_edges(edges)
    summary = portfolio_summary(held.values(), CURRENT_STATUSES, {})
    totals = summary["totals"]
    roles = {entry["role"]: entry["assets"] for entry in summary["by_role"]}
    sources = _Sources()
    for registers in (asset_registers, ownership_registers):
        for sid, register in registers.items():
            for asset_id in register.assets:
                sources.add(sid, ("asset", asset_id), register.retrieved_at_max)
    return _AssetFigures(
        assets=totals["assets"],
        operated_assets=roles.get("operator", 0),
        owned_assets=roles.get("owner", 0),
        operating_mw=totals["capacity_mw"],
        operating_mw_assets=totals["capacity_assets"],
        owned_mw=totals["owned_mw"],
        owned_mw_assets=totals["owned_mw_assets"],
        states={e["state_code"] for e in summary["by_state"] if e["state_code"]},
        technologies={e["technology"] for e in summary["by_technology"] if e["technology"]},
        sources=sources,
    )


def _proposal_rows(db: Session, tier: str) -> dict[Any, list[_ProposalRow]]:
    """Every visible sponsored proposal at `tier`, as served, by sponsor: `org_pipeline._rows` over
    the whole store in one statement (same predicate, same `listed` clause, same field gate, with
    `jurisdiction` read through it too)."""
    hidden = source_split(db, tier)[1]
    hidden = hidden - geo_cache.unused_hidden_sources(db, hidden)
    fields = (*COUNTED_FIELDS, "jurisdiction")
    gate = hidden_provenance_clause(Proposal, fields, hidden) if hidden else sa.false()
    stmt = select(
        Proposal.id,
        Proposal.sponsor_org_id,
        Proposal.lifecycle_state,
        Proposal.technology,
        Proposal.iso,
        Proposal.kind,
        Proposal.capacity_mw,
        Proposal.jurisdiction,
        listed_clause(tier).label("listed"),
        gate.label("gated"),
    ).where(*proposal_visibility_filter(tier), Proposal.sponsor_org_id.is_not(None))
    raw = db.execute(stmt).all()
    views = gated_views(db, Proposal, [r.id for r in raw if r.gated], tier) if hidden else {}
    out: dict[Any, list[_ProposalRow]] = defaultdict(list)
    for r in raw:
        view = views.get(r.id) if r.gated else None
        served: Any = view if view is not None else r
        row = _Row(
            id=r.id,
            lifecycle_state=str(served.lifecycle_state or "unknown"),
            technology=served.technology or None,
            iso=canonical_iso(served.iso) or None,
            kind=served.kind,
            capacity_mw=served.capacity_mw,
            listed=bool(r.listed),
        )
        out[r.sponsor_org_id].append(_ProposalRow(r.sponsor_org_id, row, served.jurisdiction or None))
    return out


def _opportunity_rows(db: Session, tier: str) -> dict[Any, list[_OpportunityRow]]:
    """Every visible opportunity with an issuer at `tier`, as served, by issuer."""
    hidden = source_split(db, tier)[1]
    gate = hidden_provenance_clause(Opportunity, _OPPORTUNITY_FIELDS, hidden) if hidden else sa.false()
    stmt = select(
        Opportunity.id,
        Opportunity.issuer_org_id,
        Opportunity.status,
        Opportunity.jurisdiction,
        Opportunity.technologies,
        gate.label("gated"),
    ).where(*opportunity_visibility_filter(tier), Opportunity.issuer_org_id.is_not(None))
    raw = db.execute(stmt).all()
    views = gated_views(db, Opportunity, [r.id for r in raw if r.gated], tier) if hidden else {}
    out: dict[Any, list[_OpportunityRow]] = defaultdict(list)
    for r in raw:
        view = views.get(r.id) if r.gated else None
        served: Any = view if view is not None else r
        out[r.issuer_org_id].append(
            _OpportunityRow(
                id=r.id,
                issuer_id=r.issuer_org_id,
                status=str(served.status or "unknown"),
                jurisdiction=served.jurisdiction or None,
                technologies=tuple(served.technologies or ()),
            )
        )
    return out


def _link_sources(
    db: Session, link_model: type[ProposalSource] | type[OpportunitySource], subject: Any, tier: str
) -> dict[Any, list[tuple[str, dt.datetime]]]:
    """Per record, the registers the tier may read that list it (`visible_source_link_filter`, the
    pipeline's `sources` clauses), with each link's retrieval."""
    out: dict[Any, list[tuple[str, dt.datetime]]] = defaultdict(list)
    stmt = select(subject, link_model.source_id, link_model.retrieved_at).where(
        *visible_source_link_filter(link_model, tier)
    )
    for record_id, source_id, retrieved_at in db.execute(stmt):
        out[record_id].append((source_id, retrieved_at))
    return out


@dataclass
class _TierIndex:
    """One tier's proposals and opportunities by organisation, and the registers that list them."""

    proposals: dict[Any, list[_ProposalRow]]
    opportunities: dict[Any, list[_OpportunityRow]]
    proposal_links: dict[Any, list[tuple[str, dt.datetime]]]
    opportunity_links: dict[Any, list[tuple[str, dt.datetime]]]

    @classmethod
    def load(cls, db: Session, tier: str) -> _TierIndex:
        return cls(
            proposals=_proposal_rows(db, tier),
            opportunities=_opportunity_rows(db, tier),
            proposal_links=_link_sources(db, ProposalSource, ProposalSource.proposal_id, tier),
            opportunity_links=_link_sources(db, OpportunitySource, OpportunitySource.opportunity_id, tier),
        )


def _record_figures(
    stats: OrgStats, index: _TierIndex, ids: Sequence[Any], public_ids: Mapping[Any, str]
) -> tuple[set[str], set[str], set[str], _Sources]:
    """Fill `stats`' pipeline and opportunity figures over the organisations `ids`; return the
    states, grid operators and technologies they add, and the registers behind them."""
    states: set[str] = set()
    isos: set[str] = set()
    technologies: set[str] = set()
    sources = _Sources()
    proposals = [p for oid in ids for p in index.proposals.get(oid, ())]
    if proposals:
        summary = pipeline_summary(p.row for p in proposals)
        active, totals = summary["active"], summary["totals"]
        stats.pipeline_count = active["records"]
        stats.pipeline_mw = active["capacity_mw"]
        stats.pipeline_mw_records = active["capacity_records"]
        stats.proposals = totals["records"]
        stats.withdrawn_count = sum(
            e["listed"]["records"] + e["not_listed"]["records"]
            for e in summary["by_lifecycle_state"]
            if e["lifecycle_state"] == "withdrawn"
        )
        isos = {e["iso"] for e in summary["by_iso"] if e["iso"]}
        technologies = {e["technology"] for e in summary["by_technology"] if e["technology"]}
        active_states = set(summary["active_states"])
        for p in proposals:
            if p.row.lifecycle_state in active_states and p.row.listed and p.jurisdiction:
                if _SUBDIVISION.match(p.jurisdiction):
                    states.add(p.jurisdiction)
            for sid, retrieved_at in index.proposal_links.get(p.row.id, ()):
                sources.add(sid, ("proposal", p.row.id), retrieved_at)
    issuers: list[str] = []
    for oid in ids:
        issued = index.opportunities.get(oid, ())
        if not issued:
            continue
        issuers.append(public_ids[oid])
        for o in issued:
            stats.rfps_issued += 1
            stats.rfps_open += o.status == "open"
            if o.jurisdiction and _SUBDIVISION.match(o.jurisdiction):
                states.add(o.jurisdiction)
            technologies.update(t for t in o.technologies if t)
            for sid, retrieved_at in index.opportunity_links.get(o.id, ()):
                sources.add(sid, ("opportunity", o.id), retrieved_at)
    stats.rfp_issuers = token_set(issuers)
    return states, isos, technologies, sources


@dataclass(frozen=True)
class _Listed:
    """A listed organisation: the columns the computation reads."""

    id: Any
    public_id: str
    parent_org_id: Any


def listed_organizations(db: Session) -> list[_Listed]:
    """The organisations `GET /v1/organizations` lists: unmerged, through the organisation arm."""
    stmt = select(Organization.id, Organization.public_id, Organization.parent_org_id).where(
        Organization.merged_into_id.is_(None), *organization_visibility_filter()
    )
    return [_Listed(*row) for row in db.execute(stmt)]


def group_scopes(organizations: Sequence[_Listed]) -> dict[Any, list[Any]]:
    """`orgtree.org_scope(org, "all").ids` for every listed organisation with a listed subsidiary,
    walked in memory over the listed organisations' parent links: the same walk, by level, with the
    same rules (only unmerged, visible children, which are exactly the listed ones; a node already
    visited is not re-entered; at most `MAX_DEPTH` levels and `MAX_SCOPE_ORGS` organisations).
    `org_scope` itself costs two or more statements per organisation, which is 20 s over a store
    with 1,200 parents; `services/api/test_org_stats.py` asserts the two give the same ids. Every
    other organisation's group is itself alone."""
    children: dict[Any, list[Any]] = defaultdict(list)
    for org in organizations:
        if org.parent_org_id is not None:
            children[org.parent_org_id].append(org.id)
    listed = {o.id for o in organizations}
    return {root: _descent(root, children) for root in children if root in listed}


def _descent(root: Any, children: Mapping[Any, list[Any]]) -> list[Any]:
    ids, seen, frontier, depth = [root], {root}, [root], 0
    while frontier and depth < MAX_DEPTH:
        level = [c for parent in frontier for c in children.get(parent, ())]
        found: list[Any] = []
        for child in level:
            if child not in seen:
                seen.add(child)
                found.append(child)
        if not found:
            break
        depth += 1
        room = MAX_SCOPE_ORGS - len(ids)
        if len(found) > room:
            ids.extend(found[:room])
            break
        ids.extend(found)
        frontier = found
    return ids


def _row(
    org: _Listed,
    scope: str,
    tier: str,
    ids: Sequence[Any],
    assets: _AssetFigures,
    index: _TierIndex,
    public_ids: Mapping[Any, str],
    asset_sources: list[dict[str, Any]],
) -> OrgStats:
    stats = OrgStats(
        organization_id=org.id,
        scope=scope,
        tier=tier,
        organizations=len(ids),
        assets=assets.assets,
        operated_assets=assets.operated_assets,
        owned_assets=assets.owned_assets,
        operating_mw=assets.operating_mw,
        operating_mw_assets=assets.operating_mw_assets,
        owned_mw=assets.owned_mw,
        owned_mw_assets=assets.owned_mw_assets,
    )
    states, isos, technologies, sources = _record_figures(stats, index, ids, public_ids)
    stats.states = token_set(assets.states | states)
    stats.isos = token_set(isos)
    stats.technologies = token_set(assets.technologies | technologies)
    if sources.records:
        sources.update(assets.sources)
        stats.sources = sources.as_list()
    else:
        stats.sources = asset_sources
    return stats


def compute(db: Session) -> tuple[str, list[OrgStats]]:
    """The gate key and every row the table should hold: one per listed organisation, scope and
    tier. The key is read first, so a gate that moves while the figures are read leaves the rows
    under the older key, which the API then refuses until the next refresh (never the reverse)."""
    key = gate_key(db)
    organizations = listed_organizations(db)
    public_ids = {o.id: o.public_id for o in organizations}
    groups = group_scopes(organizations)
    edges: dict[Any, list[Any]] = defaultdict(list)
    for edge in db.execute(edge_statement(None)):
        edges[edge.holder_id].append(edge)
    tiers = {tier: _TierIndex.load(db, tier) for tier in TIERS}

    rows: list[OrgStats] = []
    for org in organizations:
        own = _asset_figures(edges.get(org.id, []))
        own_sources = own.sources.as_list()
        selves = {
            tier: _row(org, "self", tier, [org.id], own, index, public_ids, own_sources)
            for tier, index in tiers.items()
        }
        rows.extend(selves.values())
        ids = groups.get(org.id)
        if ids is None or len(ids) == 1:
            # No listed subsidiary: the group is the organisation, and so are its figures.
            rows.extend(replace(row, scope="group") for row in selves.values())
            continue
        group = _asset_figures([e for oid in ids for e in edges.get(oid, ())])
        group_sources = group.sources.as_list()
        rows.extend(
            _row(org, "group", tier, ids, group, index, public_ids, group_sources)
            for tier, index in tiers.items()
        )
    return key, rows
