"""The site pass over the store (docs/21 §3.25): read every live proposal, group, choose leads,
label, and write `site` / `site_member` so a re-run over unchanged data writes nothing.

Run at the end of each resolve tick (`infra/scheduler/jobs.py::default_resolve`, after merges,
suppressions and the personal-data pass, so it sees the merged store) and by
`python -m services.sites` (`--dry-run` builds in memory and writes nothing).

What it writes, and what it never touches:

* **Proposals are never changed.** No merge, no unmerge, no field, no event: a site is a parent
  over live records, and the resolver's merges stand (owner, 2026-10-10: beta ids must not move).
* **A site of one is not stored.** A proposal with no grouping evidence has no `site_member` row
  and no site; there is no public page for a singleton, and 7,971 one-member rows (2026-10-10)
  would mean nothing. A site that falls to one member is retired.
* **Ids are stable** (`rules.inherit`): the rebuilt cluster that shares the most members with an
  existing site keeps its id (ties: the oldest site); a site no cluster takes is retired with
  `retired_at` and, when most of its former members now sit in one site, `successor_site_id`.
  A retired site is never reused.
* **No public events.** Membership is derived data, rebuilt each run (a restatement, like field
  survivorship); a proposal's own news is unchanged. What a rebuild did to each site (created,
  members gained or lost, lead changed, split, merged, retired) goes to `site_audit`, which no
  public surface reads.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import (
    Asset,
    InterconnectionPoint,
    Location,
    Organization,
    Proposal,
    ProposalSource,
    Site,
    SiteAudit,
    SiteMember,
    new_uuid,
)
from services.ids import public_id, unique_slug
from services.sites import evidence, rules
from services.sites.evidence import Candidate
from services.sites.rules import Basis, Label

logger = logging.getLogger(__name__)

SITE_PREFIX = "site"
#: The asset rows a site's EIA plant ids anchor to: EIA-860M power plants keyed by plant id.
ASSET_TYPE = "power_plant"


# --------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class ProposalRow:
    id: Any
    public_id: str
    name: str
    technology: str | None
    capacity_mw: float | None
    lifecycle_state: str
    sponsor_org_id: Any
    poi_id: Any
    precision: str | None
    lon: float | None
    lat: float | None


@dataclass(frozen=True)
class LinkRow:
    proposal_id: Any
    source_id: str
    source_record_id: str
    name: str | None
    queue_date: str | None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class OrgRow:
    id: Any
    merged_into_id: Any
    name: str | None
    parent_org_id: Any


def _follow(start: Any, step: Mapping[Any, Any], limit: int = 50) -> Any:
    """Follow `step` from `start` until it ends (cycle- and depth-safe)."""
    seen = {start}
    node = start
    for _ in range(limit):
        nxt = step.get(node)
        if nxt is None or nxt in seen:
            return node
        seen.add(nxt)
        node = nxt
    return node


def make_candidates(
    proposals: Iterable[ProposalRow],
    links: Iterable[LinkRow],
    orgs: Iterable[OrgRow],
    poi_names: Mapping[Any, str],
) -> list[Candidate]:
    """One `Candidate` per live proposal, in `public_id` order (so every later step is
    deterministic whatever order the store returns rows in)."""
    org_list = list(orgs)
    merged = {o.id: o.merged_into_id for o in org_list if o.merged_into_id is not None}
    parent = {o.id: o.parent_org_id for o in org_list if o.parent_org_id is not None}
    names = {o.id: o.name for o in org_list}
    by_proposal: dict[Any, list[LinkRow]] = defaultdict(list)
    for link in links:
        by_proposal[link.proposal_id].append(link)
    out: list[Candidate] = []
    for p in sorted(proposals, key=lambda r: r.public_id):
        mine = sorted(by_proposal.get(p.id, ()), key=lambda r: (r.source_id, r.source_record_id))
        sponsor = _follow(p.sponsor_org_id, merged) if p.sponsor_org_id is not None else None
        family = _follow(sponsor, parent) if sponsor is not None else None
        dates = sorted(d for d in (str(r.queue_date)[:10] for r in mine if r.queue_date) if d)
        out.append(
            Candidate(
                proposal_id=p.id,
                public_id=p.public_id,
                name=p.name,
                technology=p.technology,
                capacity_mw=float(p.capacity_mw) if p.capacity_mw is not None else None,
                lifecycle_state=p.lifecycle_state,
                sponsor_key=str(sponsor) if sponsor is not None else None,
                sponsor_name=names.get(sponsor) if sponsor is not None else None,
                sponsor_family=str(family) if family is not None else None,
                poi_key=str(p.poi_id) if p.poi_id is not None else None,
                poi_name=poi_names.get(p.poi_id) if p.poi_id is not None else None,
                precision=p.precision,
                lon=p.lon,
                lat=p.lat,
                plant_ids=frozenset(
                    pid for r in mine if (pid := evidence.eia_plant_id(r.source_id, r.source_record_id))
                ),
                names=tuple(dict.fromkeys(r.name for r in mine if r.name)),
                filing_date=dates[-1] if dates else None,
                mw_increase=any(
                    evidence.neso_mw_increase(dict(r.raw or {}))
                    for r in mine
                    if r.source_id == evidence.NESO_SOURCE_ID
                ),
            )
        )
    return out


def read_inputs(session: Session) -> tuple[list[ProposalRow], list[LinkRow], list[OrgRow], dict[Any, str]]:
    """Every live proposal (not merged away, any publish state: visibility is applied when a
    site is read), its active links, the organisations and the interconnection point names."""
    proposals = [
        ProposalRow(
            id=r.id,
            public_id=r.public_id,
            name=r.name_canonical,
            technology=r.technology,
            capacity_mw=float(r.capacity_mw) if r.capacity_mw is not None else None,
            lifecycle_state=r.lifecycle_state,
            sponsor_org_id=r.sponsor_org_id,
            poi_id=r.interconnection_point_id,
            precision=r.precision,
            lon=r.geom[0] if r.geom is not None else None,
            lat=r.geom[1] if r.geom is not None else None,
        )
        for r in session.execute(
            select(
                Proposal.id,
                Proposal.public_id,
                Proposal.name_canonical,
                Proposal.technology,
                Proposal.capacity_mw,
                Proposal.lifecycle_state,
                Proposal.sponsor_org_id,
                Proposal.interconnection_point_id,
                Location.precision,
                Location.geom,
            )
            .outerjoin(Location, Location.id == Proposal.location_id)
            .where(Proposal.merged_into_id.is_(None))
        )
    ]
    live = select(Proposal.id).where(Proposal.merged_into_id.is_(None))
    links: list[LinkRow] = []
    for r in session.execute(
        select(
            ProposalSource.proposal_id,
            ProposalSource.source_id,
            ProposalSource.source_record_id,
            ProposalSource.normalised,
            ProposalSource.raw,
        ).where(ProposalSource.active.is_(True), ProposalSource.proposal_id.in_(live))
    ):
        normalised = r.normalised or {}
        links.append(
            LinkRow(
                proposal_id=r.proposal_id,
                source_id=r.source_id,
                source_record_id=r.source_record_id,
                name=normalised.get("name_canonical"),
                queue_date=normalised.get("queue_date"),
                # Only the two TEC register columns the expansion rule reads leave the query.
                raw=(
                    {k: (r.raw or {}).get(k) for k in ("MW Connected", "MW Increase / Decrease")}
                    if r.source_id == evidence.NESO_SOURCE_ID
                    else None
                ),
            )
        )
    orgs = [
        OrgRow(r.id, r.merged_into_id, r.name_canonical, r.parent_org_id)
        for r in session.execute(
            select(
                Organization.id,
                Organization.merged_into_id,
                Organization.name_canonical,
                Organization.parent_org_id,
            )
        )
    ]
    poi_names = {
        r.id: r.name_display
        for r in session.execute(select(InterconnectionPoint.id, InterconnectionPoint.name_display))
    }
    return proposals, links, orgs, poi_names


# ---------------------------------------------------------------------------------- the plan
@dataclass
class PlannedMember:
    candidate: Candidate
    basis: Basis
    label: Label
    rank: int
    grouping_rule: str
    evidence: dict[str, Any]
    #: `eia:<plant ids>` or `proposal:<public_id>` (`rules.plant_groups`).
    group_key: str = ""
    #: The proposal id of the member's plant-group head when the member is one of its units.
    parent: Any = None


@dataclass
class PlannedSite:
    #: In lead order: `members[0]` is the lead.
    members: list[PlannedMember]
    review: bool

    @property
    def lead(self) -> PlannedMember:
        return self.members[0]

    @property
    def proposal_ids(self) -> frozenset[Any]:
        return frozenset(m.candidate.proposal_id for m in self.members)

    def eia_plant_ids(self) -> list[str]:
        return sorted({p for m in self.members for p in m.candidate.plant_ids}, key=lambda s: (len(s), s))

    def poi_keys(self) -> list[str]:
        return sorted({m.candidate.poi_key for m in self.members if m.candidate.poi_key is not None})


def plan(candidates: Sequence[Candidate], edges: Sequence[rules.Edge] | None = None) -> list[PlannedSite]:
    """Group, rank and label `candidates` (no store access). Sites are ordered by their lead's
    `public_id`."""
    edges = evidence.grouping_edges(candidates) if edges is None else edges
    out: list[PlannedSite] = []
    for comp in rules.components(len(candidates), edges):
        by_public = {candidates[i].public_id: i for i in comp.members}
        bases = [candidates[i].basis() for i in comp.members]
        members = []
        for placed in rules.label_site(bases):
            i = by_public[placed.basis.public_id]
            parent = candidates[by_public[placed.parent]].proposal_id if placed.parent else None
            members.append(
                PlannedMember(
                    candidates[i],
                    placed.basis,
                    placed.label,
                    placed.rank,
                    comp.rule_of[i],
                    comp.evidence_of[i],
                    placed.group_key,
                    parent,
                )
            )
        review = rules.needs_review([m.candidate.plant_ids for m in members])
        out.append(PlannedSite(members, review))
    out.sort(key=lambda s: s.lead.candidate.public_id)
    return out


# ------------------------------------------------------------------------------------ report
@dataclass
class SiteReport:
    sites: int = 0
    members: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    retired: int = 0
    members_written: int = 0
    members_removed: int = 0
    flagged: list[str] = field(default_factory=list)
    #: `site_audit` rows written, by kind.
    audit: Counter[str] = field(default_factory=Counter)
    by_rule: Counter[str] = field(default_factory=Counter)
    by_relation: Counter[str] = field(default_factory=Counter)
    by_confidence: Counter[str] = field(default_factory=Counter)
    sizes: Counter[int] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "members": self.members,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "retired": self.retired,
            "members_written": self.members_written,
            "members_removed": self.members_removed,
            "flagged": list(self.flagged),
            "audit": dict(sorted(self.audit.items())),
            "by_rule": dict(sorted(self.by_rule.items())),
            "by_relation": dict(sorted(self.by_relation.items())),
            "by_confidence": dict(sorted(self.by_confidence.items())),
            "sizes": {str(k): v for k, v in sorted(self.sizes.items())},
        }


def summarise(planned: Sequence[PlannedSite], report: SiteReport | None = None) -> SiteReport:
    report = report or SiteReport()
    report.sites = len(planned)
    report.members = sum(len(s.members) for s in planned)
    for site in planned:
        report.sizes[len(site.members)] += 1
        for m in site.members:
            report.by_rule[m.grouping_rule] += 1
            report.by_relation[m.label.relation] += 1
            report.by_confidence[m.label.confidence] += 1
    return report


# ------------------------------------------------------------------------------- persistence
def _anchors(site: PlannedSite, assets: Mapping[str, Any]) -> dict[str, Any]:
    plants = site.eia_plant_ids()
    return {
        "eia_plant_ids": plants,
        "interconnection_point_ids": site.poi_keys(),
        "assets": {p: str(assets[p]) for p in plants if p in assets},
    }


def _asset_ids_by_plant(session: Session, plants: Iterable[str]) -> dict[str, Any]:
    wanted = sorted(set(plants))
    out: dict[str, Any] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        for asset_id, plant in session.execute(
            select(Asset.id, Asset.source_asset_id).where(
                Asset.asset_type == ASSET_TYPE,
                Asset.source_id == evidence.EIA_SOURCE_ID,
                Asset.source_asset_id.in_(chunk),
            )
        ):
            out[str(plant)] = asset_id
    return out


def _set(row: Any, values: Mapping[str, Any]) -> bool:
    """Assign only the attributes whose value differs; True when any did."""
    changed = False
    for key, value in values.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


def _new_site(session: Session, name: str, taken: set[str], now: dt.datetime) -> Site:
    site_id = new_uuid()
    pid = public_id(SITE_PREFIX, site_id)
    slug = unique_slug(name, pid, taken)
    taken.add(slug)
    site = Site(id=site_id, public_id=pid, slug=slug, name_display=name, rule_version=rules.RULE_VERSION)
    site.created_at = now
    site.updated_at = now
    site.built_at = now
    session.add(site)
    return site


def rebuild_sites(
    session: Session, *, now: dt.datetime | None = None, candidates: Sequence[Candidate] | None = None
) -> SiteReport:
    """The whole pass (module docstring). Flushes; the caller commits."""
    now = now or dt.datetime.now(dt.UTC)
    if candidates is None:
        candidates = make_candidates(*read_inputs(session))
    planned = plan(candidates)
    report = summarise(planned)

    prior_sites = list(session.scalars(select(Site).where(Site.retired_at.is_(None))))
    current_rows = list(session.scalars(select(SiteMember)))
    members_of: dict[Any, set[Any]] = defaultdict(set)
    for row in current_rows:
        members_of[row.site_id].add(row.proposal_id)
    prior = [
        rules.PriorSite(s.id, _aware(s.created_at), frozenset(members_of.get(s.id, ())))
        for s in sorted(prior_sites, key=lambda s: (_aware(s.created_at), str(s.id)))
    ]
    inheritance = rules.inherit([s.proposal_ids for s in planned], prior)
    sites_by_id = {s.id: s for s in prior_sites}
    taken = set(session.scalars(select(Site.slug)))
    assets = _asset_ids_by_plant(session, (p for s in planned for p in s.eia_plant_ids()))

    previous_lead = {s.id: s.lead_proposal_id for s in prior_sites}
    built: list[Site] = []
    changed: list[bool] = []
    for site_plan, kept in zip(planned, inheritance.assigned, strict=True):
        lead = site_plan.lead.candidate
        if kept is None:
            site = _new_site(session, lead.name, taken, now)
            report.created += 1
        else:
            site = sites_by_id[kept.site_id]
        values = {
            "name_display": lead.name,
            "lead_proposal_id": lead.proposal_id,
            "member_count": len(site_plan.members),
            "rule_version": rules.RULE_VERSION,
            "review_flag": "oversize" if site_plan.review else None,
            "anchors": _anchors(site_plan, assets),
        }
        changed.append(_set(site, values) and kept is not None)
        if site_plan.review:
            report.flagged.append(site.public_id)
            logger.warning(
                "site %s groups %d records (%d distinct things): flagged for review, not served",
                site.public_id,
                len(site_plan.members),
                rules.effective_size([m.candidate.plant_ids for m in site_plan.members]),
                extra={"site": site.public_id},
            )
        built.append(site)
    session.flush()

    rows_by_proposal = {row.proposal_id: row for row in current_rows}
    wanted: set[Any] = set()
    for k, (site, site_plan) in enumerate(zip(built, planned, strict=True)):
        for m in site_plan.members:
            pid = m.candidate.proposal_id
            wanted.add(pid)
            values = {
                "site_id": site.id,
                "is_lead": m.rank == 0,
                "lead_rank": m.rank,
                "grouping_rule": m.grouping_rule,
                "grouping_evidence": m.evidence,
                "group_key": m.group_key,
                "parent_proposal_id": m.parent,
                "relation": m.label.relation,
                "relation_rule": m.label.rule,
                "confidence": m.label.confidence,
                "basis": m.basis.as_json(),
            }
            existing = rows_by_proposal.get(pid)
            if existing is None:
                session.add(
                    SiteMember(id=new_uuid(), proposal_id=pid, created_at=now, updated_at=now, **values)
                )
                report.members_written += 1
                changed[k] = True
            elif _set(existing, values):
                report.members_written += 1
                changed[k] = True
    for row in current_rows:
        if row.proposal_id not in wanted:
            session.delete(row)
            report.members_removed += 1
    for site, kept, was_changed in zip(built, inheritance.assigned, changed, strict=True):
        if kept is None:
            continue
        if was_changed:
            site.built_at = now
            report.updated += 1
        else:
            report.unchanged += 1
    session.flush()

    for old, successor in inheritance.retired:
        site = sites_by_id[old.site_id]
        site.retired_at = now
        site.lead_proposal_id = None
        site.member_count = 0
        site.review_flag = None
        site.successor_site_id = built[successor].id if successor is not None else None
        report.retired += 1
    session.flush()
    _audit(session, planned, built, inheritance, prior, previous_lead, sites_by_id, report, now)
    session.flush()
    return report


def _audit(
    session: Session,
    planned: Sequence[PlannedSite],
    built: Sequence[Site],
    inheritance: rules.Inheritance[Any],
    prior: Sequence[rules.PriorSite[Any]],
    previous_lead: Mapping[Any, Any],
    sites_by_id: Mapping[Any, Site],
    report: SiteReport,
    now: dt.datetime,
) -> None:
    """One `site_audit` row per thing this rebuild did to a site (`SiteAudit`): created, members
    gained or lost, lead changed, split (a prior site's members now in two or more sites), merged
    (a site now holding members of two or more prior sites), retired. Proposals and sites are named
    by `public_id`; nothing is written when nothing changed."""
    public_of = {m.candidate.proposal_id: m.candidate.public_id for s in planned for m in s.members}
    missing = {pid for p in prior for pid in p.members if pid not in public_of}
    if missing:
        rows = session.execute(select(Proposal.id, Proposal.public_id).where(Proposal.id.in_(missing))).all()
        public_of.update({row[0]: row[1] for row in rows})

    def names(ids: Iterable[Any]) -> list[str]:
        return sorted(str(public_of.get(i, i)) for i in ids)

    def write(site: Site, kind: str, detail: dict[str, Any]) -> None:
        session.add(
            SiteAudit(
                site_id=site.id, kind=kind, detail=detail, rule_version=rules.RULE_VERSION, recorded_at=now
            )
        )
        report.audit[kind] += 1

    cluster_of = {pid: k for k, site_plan in enumerate(planned) for pid in site_plan.proposal_ids}
    prior_of = {pid: p for p in prior for pid in p.members}
    for site_plan, kept, site in zip(planned, inheritance.assigned, built, strict=True):
        now_members = site_plan.proposal_ids
        if kept is None:
            write(
                site, "created", {"members": names(now_members), "lead": site_plan.lead.candidate.public_id}
            )
        else:
            gained, lost = now_members - kept.members, kept.members - now_members
            if gained:
                write(site, "members_gained", {"members": names(gained)})
            if lost:
                write(site, "members_lost", {"members": names(lost)})
            before = previous_lead.get(kept.site_id)
            if before != site_plan.lead.candidate.proposal_id:
                write(
                    site,
                    "lead_changed",
                    {
                        "from": names([before])[0] if before else None,
                        "to": site_plan.lead.candidate.public_id,
                    },
                )
        sources = {prior_of[pid].site_id for pid in now_members if pid in prior_of}
        if len(sources) > 1:
            write(site, "merged", {"from": sorted(sites_by_id[sid].public_id for sid in sources)})
    for p in prior:
        into = sorted({cluster_of[pid] for pid in p.members if pid in cluster_of})
        if len(into) > 1:
            write(
                sites_by_id[p.site_id],
                "split",
                {
                    "into": {
                        built[k].public_id: names(pid for pid in p.members if cluster_of.get(pid) == k)
                        for k in into
                    }
                },
            )
    for old, successor in inheritance.retired:
        write(
            sites_by_id[old.site_id],
            "retired",
            {
                "members": names(old.members),
                "successor": built[successor].public_id if successor is not None else None,
            },
        )


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def run(session_factory: Any) -> dict[str, Any]:
    """The resolve tick's site pass (`infra/scheduler/jobs.py::default_resolve`): `rebuild_sites`
    in a transaction of its own, reported as the counts only (the flagged ids are logged by
    `rebuild_sites` and listed by `python -m services.sites`)."""
    from services.db.session import session_scope

    with session_scope(session_factory) as session:
        report = rebuild_sites(session)
    keys = (
        "sites",
        "members",
        "created",
        "updated",
        "unchanged",
        "retired",
        "members_written",
        "members_removed",
    )
    return {**{k: getattr(report, k) for k in keys}, "flagged": len(report.flagged)}
