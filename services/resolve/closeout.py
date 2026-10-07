"""Close out queue requests against operating EIA plants (docs/22 §23.8; interconnection analyst
review 2026-10-07, finding "queue records that are already operating are shown as overdue").

The store holds EIA-860M's operating inventory as `power_plant` assets, but nothing tied a queue
request to the plant it became. A request whose register never moved it to "completed" therefore
read as overdue for years: CAISO "MONTEZUMA II" was "Contracted, overdue by 14.7 years" beside the
78.2 MW Montezuma Wind II plant, operating since 2012; "DAGGETT SOLAR 3" was "Contracted, overdue
3.2 years" beside Daggett 3 (449 MW, 2023).

What this does, on each resolve tick (`services/resolve/report.py::apply_all_clusters`):

1. **Candidates.** A live record (not merged away) of a power kind whose stored state is pre-built
   (announced .. under_construction), with at least one live interconnection request and *no*
   EIA-860M Planned member: a Planned row is EIA's own statement that the unit is not yet
   operating, and it is the fresher register for that project. A record already linked to an
   operating plant is skipped.
2. **Blocking.** Operating, standby or retiring plants (`asset.status`) in the record's state and
   county.
3. **Rule** (`match`; every condition must hold):
   - names: the resolver's `mean` scorer over `norm_name` (legal forms, noise words and phase
     markers removed) at least `NAME_MIN`;
   - phase numbers equal (`pipeline.resolve.phase_key`: "MONTEZUMA II" and "Montezuma Wind II" are
     both phase 2; "FPL Energy Montezuma Winds" is not);
   - technology: the plant holds a generator of a family the request names (EIA's per-generator
     technologies on the asset): a storage request beside an operating solar plant of the same
     name is the plant's add-on, not the plant ("Long Point Storage" and Long Point Solar);
   - agreement: the request has an executed or filed interconnection agreement (or is under
     construction), or its own proposed date has passed;
   - capacity: the plant within `CAP_RATIO` of the request either way, when both state one;
     without one of them, the normalised names must be equal;
   - timing: the plant entered service no earlier than the request's queue year, and within
     `COD_YEARS` of the request's own proposed date. A repower or an expansion request at an
     operating plant fails this (its proposed date is years after the plant's first year).
   - one plant per record and one record per plant: an ambiguous pair links nothing.
4. **Link.** The plant joins the record as a `proposal_source` row of `us.eia.860m` keyed
   `plant:<EIA plant id>` (no Planned generator id has that shape), `link_method = "rule"`,
   `link_confidence` the name score, its `normalised` row stating `built`, the plant's name, MW,
   technology, `eia_plant_id` and its first operating year as `actual_cod` (EIA's plant inventory
   carries the year, not the month). A `source_linked` event records it with the provenance
   quartet of the most restrictive member (A-22-27) and is unpublished, as every resolver event
   is. `link_event_id` names that event (docs/21 §3.2).
5. **Survivorship** then serves the record as built from the plant (`operating_plant_outranks_queue`,
   `services/resolve/survivorship.py`), keeps the request's MW as the grid-connection figure, and
   gives the actual COD as the record's `actual_cod` milestone.

**Reversal.** `unlink_operating_plant` deactivates the link, writes a `source_unlinked` event that
reverses the `source_linked` one, and restates the record. A pair once unlinked is never linked
again by this rule (the unlink event's idempotency key is checked first).
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import classify_tech, norm_county, norm_name
from pipeline.resolve import _ratio, phase_key, tech_families
from services.db.models import Asset, Event, Location, Proposal, ProposalSource, Source
from services.resolve import provenance, survivorship

#: The register the operating plants come from (the `power_plant` assets' source).
PLANT_SOURCE_ID = "us.eia.860m"
#: `proposal_source.source_record_id` prefix of an operating-plant link.
RECORD_PREFIX = "plant:"
#: Plant statuses that mean the plant is (or was until a scheduled date) in service.
IN_SERVICE = ("operating", "standby", "retiring")
#: Record states a close-out may move to built.
PRE_BUILT = frozenset({"announced", "filed", "studied", "permitted", "contracted", "under_construction"})
#: Proposal kinds of the power class (`pipeline.resolve.KIND_CLASSES`).
POWER_KINDS = frozenset({"generation", "storage", "nuclear"})
#: Request states that carry an executed or filed interconnection agreement (or later). A request
#: short of one closes out only when its own proposed date has passed.
AGREEMENT_STATES = frozenset({"permitted", "contracted", "under_construction"})
#: The resolver's `mean` name score a pair must reach.
NAME_MIN = 90.0
#: Plant MW within this factor of the request's, either way.
CAP_RATIO = 2.0
#: The plant's first operating year within this many years of the request's proposed date.
COD_YEARS = 3
OPERATING_STATUS_TEXT = "Operating (EIA-860M operating generator inventory)"
#: Event payload key for the record's survivorship fields (as `services/resolve/merge.py`).
SURVIVORSHIP_KEY = "survivorship"


def link_key(proposal_id: _uuid.UUID, plant_id: str) -> str:
    return f"closeout:link:{proposal_id}:{plant_id}"


def unlink_key(proposal_id: _uuid.UUID, plant_id: str) -> str:
    return f"closeout:unlink:{proposal_id}:{plant_id}"


@dataclass(frozen=True)
class Plant:
    asset_id: _uuid.UUID
    plant_id: str
    name: str
    name_norm: str | None
    technology: str | None
    families: frozenset[str]
    capacity_mw: float | None
    first_year: int
    state_code: str
    county: str | None


@dataclass(frozen=True)
class Request:
    proposal_id: _uuid.UUID
    name: str
    name_norm: str | None
    technology: str | None
    capacity_mw: float | None
    queue_year: int | None
    cod_year: int | None
    state_code: str | None
    county: str | None
    state: str


@dataclass
class CloseoutReport:
    candidates: int = 0
    pairs_passing: int = 0
    ambiguous: int = 0
    previously_unlinked: int = 0
    linked: int = 0
    records_built: int = 0
    links: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "pairs_passing": self.pairs_passing,
            "ambiguous": self.ambiguous,
            "previously_unlinked": self.previously_unlinked,
            "linked": self.linked,
            "records_built": self.records_built,
        }


def today_year() -> int:
    return dt.datetime.now(dt.UTC).year


def plant_families(technology: str | None, technologies: Any) -> frozenset[str]:
    """The technology families a plant holds: every generator technology EIA lists for it
    (`asset.technologies`, raw text -> MW), else its one class."""
    out: set[str] = set()
    if isinstance(technologies, dict):
        for raw, mw in technologies.items():
            if _float(mw) is not None:
                out |= tech_families(classify_tech(raw)[0]) or set()
    if not out:
        out |= tech_families(technology) or set()
    return frozenset(out)


def _year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value)
    head = text[:4]
    return int(head) if head.isdigit() else None


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out > 0 else None


def match(request: Request, plant: Plant) -> tuple[bool, float, str]:
    """Whether `plant` is the operating plant `request` became (module docstring, rule 3), with the
    name score and a one-line rationale."""
    score = _ratio(request.name_norm, plant.name_norm, "mean")
    if score is None or score < NAME_MIN:
        return False, score or 0.0, "name"
    if phase_key(request.name) != phase_key(plant.name):
        return False, score, "phase"
    wanted = tech_families(request.technology)
    if wanted is not None and not wanted & plant.families:
        return False, score, "technology"
    if request.state not in AGREEMENT_STATES and (
        request.cod_year is None or request.cod_year >= today_year()
    ):
        return False, score, "no_agreement_and_not_overdue"
    if request.capacity_mw is not None and plant.capacity_mw is not None:
        ratio = plant.capacity_mw / request.capacity_mw
        if not 1 / CAP_RATIO <= ratio <= CAP_RATIO:
            return False, score, "capacity"
    elif request.name_norm != plant.name_norm:
        return False, score, "capacity_unknown"
    if request.queue_year is not None and plant.first_year < request.queue_year:
        return False, score, "plant_predates_request"
    if request.cod_year is not None and abs(request.cod_year - plant.first_year) > COD_YEARS:
        return False, score, "cod"
    rationale = (
        f"operating close-out: name {score:.0f}, phase {sorted(map(str, phase_key(request.name))) or '-'}, "
        f"MW {request.capacity_mw} vs {plant.capacity_mw}, queue {request.queue_year}, "
        f"proposed {request.cod_year}, in service {plant.first_year}"
    )
    return True, score, rationale


def _plants(session: Session) -> dict[tuple[str, str], list[Plant]]:
    rows = session.execute(
        select(
            Asset.id,
            Asset.source_asset_id,
            Asset.name,
            Asset.technology,
            Asset.technologies,
            Asset.capacity_mw,
            Asset.commissioned_year,
            Asset.state_code,
            Asset.county_name,
        ).where(
            Asset.asset_type == "power_plant",
            Asset.source_id == PLANT_SOURCE_ID,
            Asset.status.in_(IN_SERVICE),
            Asset.commissioned_year.is_not(None),
        )
    ).all()
    out: dict[tuple[str, str], list[Plant]] = defaultdict(list)
    for asset_id, plant_id, name, tech, techs, mw, year, state, county in rows:
        county_norm = norm_county(county)
        if not state or not county_norm:
            continue
        out[(str(state), county_norm)].append(
            Plant(
                asset_id=asset_id,
                plant_id=str(plant_id),
                name=str(name),
                name_norm=norm_name(name),
                technology=tech,
                families=plant_families(tech, techs),
                capacity_mw=_float(mw),
                first_year=int(year),
                state_code=str(state),
                county=county_norm,
            )
        )
    return out


def _candidate_requests(session: Session) -> list[Request]:
    """Module docstring, rule 1: one `Request` per live request link of each candidate record."""
    proposals = {
        p.id: p
        for p in session.scalars(
            select(Proposal).where(
                Proposal.merged_into_id.is_(None),
                Proposal.lifecycle_state.in_(sorted(PRE_BUILT)),
                Proposal.kind.in_(sorted(POWER_KINDS)),
            )
        )
    }
    if not proposals:
        return []
    categories = dict(session.execute(select(Source.id, Source.category)).tuples().all())
    links: dict[_uuid.UUID, list[ProposalSource]] = defaultdict(list)
    ids = list(proposals)
    for start in range(0, len(ids), 500):
        for link in session.scalars(
            select(ProposalSource).where(
                ProposalSource.proposal_id.in_(ids[start : start + 500]), ProposalSource.active.is_(True)
            )
        ):
            links[link.proposal_id].append(link)
    locations = dict(
        session.execute(
            select(Location.id, Location.county_name).where(
                Location.id.in_({p.location_id for p in proposals.values() if p.location_id is not None})
            )
        )
        .tuples()
        .all()
    )
    out: list[Request] = []
    for pid, proposal in proposals.items():
        members = links.get(pid, [])
        roles = [categories.get(m.source_id, "") in survivorship.REQUEST_CATEGORIES for m in members]
        if not any(roles):
            continue
        if any(categories.get(m.source_id) in survivorship.INVENTORY_CATEGORIES for m in members):
            continue  # an EIA-860M Planned member (or an operating link already) speaks for it
        county = norm_county(locations.get(proposal.location_id)) if proposal.location_id else None
        for link, is_request in zip(members, roles, strict=True):
            if not is_request:
                continue
            values = survivorship.link_values(link)
            if values.get("lifecycle_state") not in PRE_BUILT:
                continue
            name = str(values.get("name_canonical") or proposal.name_canonical)
            out.append(
                Request(
                    proposal_id=pid,
                    name=name,
                    name_norm=norm_name(name),
                    technology=values.get("technology") or proposal.technology,
                    capacity_mw=_float(values.get("capacity_mw")),
                    queue_year=_year(values.get("queue_date")),
                    cod_year=_year(values.get("proposed_online_date")),
                    state_code=proposal.jurisdiction if "-" in (proposal.jurisdiction or "") else None,
                    county=county,
                    state=str(values.get("lifecycle_state")),
                )
            )
    return out


def find_pairs(
    session: Session, report: CloseoutReport | None = None
) -> list[tuple[Request, Plant, float, str]]:
    """Every (request, plant) the rule accepts, with ambiguous ones dropped (rule 3, last line)."""
    report = report if report is not None else CloseoutReport()
    plants = _plants(session)
    requests = _candidate_requests(session)
    report.candidates = len({r.proposal_id for r in requests})
    passing: list[tuple[Request, Plant, float, str]] = []
    for request in requests:
        if not request.state_code or not request.county:
            continue
        for plant in plants.get((request.state_code, request.county), []):
            ok, score, why = match(request, plant)
            if ok:
                passing.append((request, plant, score, why))
    report.pairs_passing = len(passing)
    by_record: dict[_uuid.UUID, set[str]] = defaultdict(set)
    by_plant: dict[str, set[_uuid.UUID]] = defaultdict(set)
    for request, plant, _, _ in passing:
        by_record[request.proposal_id].add(plant.plant_id)
        by_plant[plant.plant_id].add(request.proposal_id)
    out: dict[tuple[_uuid.UUID, str], tuple[Request, Plant, float, str]] = {}
    for request, plant, score, why in passing:
        if len(by_record[request.proposal_id]) > 1 or len(by_plant[plant.plant_id]) > 1:
            report.ambiguous += 1
            continue
        key = (request.proposal_id, plant.plant_id)
        if key not in out or score > out[key][2]:
            out[key] = (request, plant, score, why)
    return list(out.values())


def _plant_normalised(asset: Asset) -> dict[str, Any]:
    return {
        "kind": "storage" if asset.technology in ("storage", "pumped_storage") else "generation",
        "name_canonical": asset.name,
        "technology": asset.technology,
        "technology_raw": asset.technology_raw,
        "capacity_mw": float(asset.capacity_mw) if asset.capacity_mw is not None else None,
        "jurisdiction": asset.state_code,
        "lifecycle_state": "built",
        "status_raw": OPERATING_STATUS_TEXT,
        "actual_cod": str(asset.commissioned_year) if asset.commissioned_year else None,
        survivorship.OPERATING_PLANT_FLAG: True,
        "identifiers": {"eia_plant_id": str(asset.source_asset_id)},
    }


def link_operating_plant(
    session: Session, proposal: Proposal, asset: Asset, *, score: float, rationale: str
) -> ProposalSource | None:
    """Attach `asset` to `proposal` as an operating-plant member (module docstring, rule 4) and
    restate the record. None when the pair was unlinked before or the plant is already linked."""
    plant_id = str(asset.source_asset_id)
    if session.scalar(select(Event.id).where(Event.idempotency_key == unlink_key(proposal.id, plant_id))):
        return None
    record_id = f"{RECORD_PREFIX}{plant_id}"
    taken = session.scalar(
        select(ProposalSource).where(
            ProposalSource.source_id == PLANT_SOURCE_ID,
            ProposalSource.source_record_id == record_id,
            ProposalSource.active.is_(True),
        )
    )
    if taken is not None:
        return None
    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=PLANT_SOURCE_ID,
        source_record_id=record_id,
        source_url=asset.source_url,
        retrieved_at=asset.retrieved_at,
        licence_id=asset.licence_id,
        raw={},
        normalised=_plant_normalised(asset),
        status_raw=OPERATING_STATUS_TEXT,
        first_seen=asset.retrieved_at,
        last_seen=asset.retrieved_at,
        link_method="rule",
        link_confidence=round(min(score, 100.0) / 100.0, 3),
        active=True,
    )
    quartet = provenance.for_proposal_merge(session, proposal.id, [link])
    before = survivorship.snapshot(proposal)
    session.add(link)
    proposal.source_count = (proposal.source_count or 0) + 1
    session.flush()
    result = survivorship.apply_survivorship(session, proposal)
    event = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type="source_linked",
        observed_at=dt.datetime.now(dt.UTC),
        **(quartet.columns() if quartet is not None else {}),
        before={"surviving": {SURVIVORSHIP_KEY: before}},
        after={
            "link": {
                "proposal_source_id": str(link.id),
                "source_id": PLANT_SOURCE_ID,
                "source_record_id": record_id,
            },
            "asset_id": str(asset.id),
            "surviving": {SURVIVORSHIP_KEY: {c.field: c.after for c in result.changes}},
        },
        changed_keys=["source_count", *[c.field for c in result.changes]],
        actor_type="pipeline",
        confidence=round(min(score, 100.0) / 100.0, 3),
        reason=rationale,
        idempotency_key=link_key(proposal.id, plant_id),
    )
    session.add(event)
    session.flush()
    link.link_event_id = event.id
    session.flush()
    return link


def unlink_operating_plant(session: Session, link: ProposalSource, *, reason: str = "unlink") -> Event:
    """Reverse `link_operating_plant` (module docstring, "Reversal")."""
    if not link.source_record_id.startswith(RECORD_PREFIX) or link.link_method != "rule":
        raise ValueError(f"{link.id} is not an operating-plant link")
    plant_id = link.source_record_id[len(RECORD_PREFIX) :]
    proposal = session.get(Proposal, link.proposal_id)
    if proposal is None:
        raise ValueError(f"{link.id} points at a missing proposal")
    linked = session.get(Event, link.link_event_id) if link.link_event_id is not None else None
    link.active = False
    proposal.source_count = max(0, (proposal.source_count or 1) - 1)
    session.flush()
    remaining = survivorship.proposal_members(session, proposal.id)
    snapshot = ((linked.before or {}).get("surviving") or {}).get(SURVIVORSHIP_KEY) if linked else None
    if snapshot and len(remaining) <= 1:
        survivorship.restore_snapshot(proposal, snapshot)
    result = survivorship.apply_survivorship(session, proposal) if len(remaining) > 1 else None
    event = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type="source_unlinked",
        observed_at=dt.datetime.now(dt.UTC),
        source_id=linked.source_id if linked else link.source_id,
        source_url=linked.source_url if linked else link.source_url,
        retrieved_at=linked.retrieved_at if linked else link.retrieved_at,
        licence_id=linked.licence_id if linked else link.licence_id,
        before={"link": {"proposal_source_id": str(link.id), "source_record_id": link.source_record_id}},
        after={
            "surviving": {SURVIVORSHIP_KEY: {c.field: c.after for c in (result.changes if result else [])}}
        },
        changed_keys=["source_count"],
        actor_type="pipeline",
        reason=reason,
        reverses_event_id=linked.id if linked else None,
        idempotency_key=unlink_key(proposal.id, plant_id),
    )
    session.add(event)
    session.flush()
    return event


def close_out(session: Session, *, dry_run: bool = False) -> CloseoutReport:
    """One pass of the rule over the store (module docstring). Idempotent: a linked pair is not a
    candidate again, and an unlinked pair is never relinked."""
    report = CloseoutReport()
    pairs = find_pairs(session, report)
    assets = (
        {
            a.id: a
            for a in session.scalars(select(Asset).where(Asset.id.in_({p.asset_id for _, p, _, _ in pairs})))
        }
        if pairs
        else {}
    )
    for request, plant, score, why in sorted(pairs, key=lambda t: (str(t[0].proposal_id), t[1].plant_id)):
        proposal = session.get(Proposal, request.proposal_id)
        asset = assets.get(plant.asset_id)
        if proposal is None or asset is None:
            continue
        entry = {
            "proposal_id": str(proposal.id),
            "record": proposal.name_canonical,
            "plant_id": plant.plant_id,
            "plant": plant.name,
            "score": round(score, 1),
            "rationale": why,
            "state_before": proposal.lifecycle_state,
        }
        if dry_run:
            report.links.append(entry)
            continue
        link = link_operating_plant(session, proposal, asset, score=score, rationale=why)
        if link is None:
            report.previously_unlinked += 1
            continue
        report.linked += 1
        entry["state_after"] = proposal.lifecycle_state
        if proposal.lifecycle_state == "built":
            report.records_built += 1
        report.links.append(entry)
    session.flush()
    return report


def operating_links(
    session: Session, proposal_ids: Iterable[_uuid.UUID] | None = None
) -> Sequence[ProposalSource]:
    """Active operating-plant links (all, or of `proposal_ids`)."""
    stmt = select(ProposalSource).where(
        ProposalSource.source_id == PLANT_SOURCE_ID,
        ProposalSource.source_record_id.like(f"{RECORD_PREFIX}%"),
        ProposalSource.active.is_(True),
    )
    if proposal_ids is not None:
        stmt = stmt.where(ProposalSource.proposal_id.in_(list(proposal_ids)))
    return session.scalars(stmt).all()
