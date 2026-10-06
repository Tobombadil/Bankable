"""Reviewed suppressions: records a source selects that are verified not to be what they claim.

The first list is `data/vendored/data_centres/icis_air_not_data_centres.yaml`: EPA ICIS-Air
facilities the data-centre selection rule keeps (NAICS 518210) that are corporate offices
(docs/25 §3.7; audit 2026-09-30, data scientist F8). The file's header says what goes in.

`apply_suppressions` runs in the resolution step (`infra/scheduler/jobs.py::default_resolve`),
after the loads, so it acts on whatever the loader has written, including a store rebuilt from
scratch. For each listed registry id it finds the active source links of that source whose raw
payload carries the id, follows each to its live proposal (through `merged_into_id`), and:

- if every active link of that proposal is a suppressed one, sets `publish_state = unpublished`
  and writes one `unpublished` event (actor `pipeline`, `reason` from the file, before/after
  publish state, never published itself). The event's idempotency key is fixed per proposal and
  registry id, so the step is idempotent, and a record an admin re-publishes afterwards stays
  published: the suppression has already been recorded and is not applied twice;
- if another source also supports the proposal, leaves it published and reports it
  (`kept_other_sources`), because that source's claim is independent evidence;
- a listed id with no active link is reported (`not_found`), so a stale entry is visible.

Nothing is deleted. Reversing a suppression is an admin re-publish (`PATCH` on the record's
publish state), which writes its own `published` event.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Event, Proposal, ProposalSource

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUPPRESSION_FILES: tuple[pathlib.Path, ...] = (
    ROOT / "data" / "vendored" / "data_centres" / "icis_air_not_data_centres.yaml",
)
#: Raw-payload key that carries the registry id, per source.
REGISTRY_KEYS: dict[str, str] = {"us.epa.echo.icis_air": "REGISTRY_ID"}


@dataclass(frozen=True)
class Suppression:
    source_id: str
    registry_id: str
    name: str
    reason: str


@dataclass
class SuppressionReport:
    #: public ids of proposals this run unpublished
    unpublished: list[str] = field(default_factory=list)
    #: public ids left published because another source supports them
    kept_other_sources: list[str] = field(default_factory=list)
    #: proposals already suppressed by an earlier run (or re-published by an admin since)
    already_recorded: int = 0
    #: listed registry ids with no active link in the store
    not_found: list[str] = field(default_factory=list)


def load_suppressions(paths: Sequence[pathlib.Path] = SUPPRESSION_FILES) -> list[Suppression]:
    out: list[Suppression] = []
    for path in paths:
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        source_id = str(doc["source_id"])
        if source_id not in REGISTRY_KEYS:
            raise ValueError(f"{path}: no registry key known for {source_id}")
        for entry in doc.get("suppressed") or []:
            registry_id = str(entry["registry_id"]).strip()
            reason = " ".join(str(entry.get("reason") or "").split())
            if not registry_id or not reason:
                raise ValueError(f"{path}: every entry needs a registry_id and a reason")
            out.append(Suppression(source_id, registry_id, str(entry.get("name") or ""), reason))
    return out


def _root(session: Session, proposal_id: _uuid.UUID) -> Proposal | None:
    proposal = session.get(Proposal, proposal_id)
    seen: set[_uuid.UUID] = set()
    while proposal is not None and proposal.merged_into_id is not None and proposal.id not in seen:
        seen.add(proposal.id)
        proposal = session.get(Proposal, proposal.merged_into_id)
    return proposal


def _registry_id(link: ProposalSource) -> str:
    key = REGISTRY_KEYS.get(link.source_id)
    raw = link.raw if isinstance(link.raw, dict) else {}
    return str(raw.get(key) or "").strip() if key else ""


def apply_suppressions(
    session: Session, suppressions: Sequence[Suppression] | None = None, *, now: dt.datetime | None = None
) -> SuppressionReport:
    """Unpublish the proposals the reviewed lists suppress (module docstring)."""
    items = list(load_suppressions() if suppressions is None else suppressions)
    report = SuppressionReport()
    if not items:
        return report
    wanted = {(s.source_id, s.registry_id): s for s in items}
    links = session.scalars(
        select(ProposalSource).where(
            ProposalSource.source_id.in_(sorted({s.source_id for s in items})),
            ProposalSource.active.is_(True),
        )
    ).all()
    hits: dict[_uuid.UUID, list[Suppression]] = {}
    found: set[tuple[str, str]] = set()
    for link in links:
        key = (link.source_id, _registry_id(link))
        if key not in wanted:
            continue
        found.add(key)
        root = _root(session, link.proposal_id)
        if root is not None:
            hits.setdefault(root.id, []).append(wanted[key])
    report.not_found = sorted(r for (_, r) in set(wanted) - found)
    stamp = now or dt.datetime.now(dt.UTC)
    for proposal_id, matched in sorted(hits.items(), key=lambda kv: str(kv[0])):
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            continue
        members = [
            proposal.id,
            *session.scalars(select(Proposal.id).where(Proposal.merged_into_id == proposal.id)).all(),
        ]
        active = session.scalars(
            select(ProposalSource).where(
                ProposalSource.proposal_id.in_(members), ProposalSource.active.is_(True)
            )
        ).all()
        if any((link.source_id, _registry_id(link)) not in wanted for link in active):
            report.kept_other_sources.append(proposal.public_id)
            continue
        first = sorted(matched, key=lambda s: s.registry_id)[0]
        idem = f"suppress:proposal:{proposal.id}:{first.source_id}:{first.registry_id}"
        if session.scalar(select(Event.id).where(Event.idempotency_key == idem)) is not None:
            report.already_recorded += 1
            continue
        before = proposal.publish_state
        proposal.publish_state = "unpublished"
        proposal.last_changed = stamp
        session.add(
            Event(
                subject_type="proposal",
                subject_id=proposal.id,
                event_type="unpublished",
                observed_at=stamp,
                before={"publish_state": before},
                after={
                    "publish_state": "unpublished",
                    "suppression": {"source_id": first.source_id, "registry_id": first.registry_id},
                },
                changed_keys=["publish_state"],
                actor_type="pipeline",
                reason=f"suppressed: {first.reason}",
                idempotency_key=idem,
            )
        )
        session.flush()
        report.unpublished.append(proposal.public_id)
    return report
