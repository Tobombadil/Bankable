"""The provenance quartet on every event the resolver writes (docs/22 §13.7; CLAUDE.md: "Every stored
record carries `source_id`, `source_url`, `retrieved_at`, `licence`").

A resolver event is derived: a merge joins records from one or several sources, an unmerge undoes
one, a suppression unpublishes a record on a reviewed list. None of them has a source document of
its own, so before 2026-10-07 they carried no quartet at all (`docs/21` §3.10 allows that only for
`actor_type = user` events). The rule here, recorded as A-22-27 in docs/22 §13.7:

1. **Evidence.** The records the event is about, each with the quartet it was stored with:
   - proposal merge: the active `proposal_source` links of the survivor and of the absorbed record;
   - organisation merge: for each of the two organisations, the links of the proposals it sponsors
     (a sponsored proposal merged away is followed to its own links through its merge event), its
     `asset_owner` edges and its `organization_alias` rows;
   - suppression: the active links of the suppressed record.
2. **One member's whole quartet, never a mix.** The event carries the quartet of exactly one
   evidence row, so its `source_id`, `source_url`, `retrieved_at` and `licence_id` always describe
   one real stored record. A mixed quartet (one source's URL under another source's licence) would
   render a credit line that no source states (`services/api/serialize.py::serialize_event`).
3. **The most restrictive licence wins** (`licence_rank`: reuse class, then an uncleared gate, then
   each `allows_*` permission withheld). So an event touching a `restricted` or `unknown` member
   carries that member's licence, and `services/api/visibility.py::event_visibility_filter` keeps
   it off every non-admin surface (docs/21 §8 checklist item 2). Its `source_url` is therefore
   never on a public event: a restricted URL can only sit on an event no public reader is served.
4. **Ties** go to the evidence that triggered the event (the absorbed side of a merge, the listed
   registry id of a suppression), then the most recent retrieval, then source id and record key,
   for a total order.

An unmerge carries the quartet of the merge it reverses (docs/21 §6.2: a reversal restores
provenance as well as value); a merge written before this rule carries none, and its unmerge then
derives one from the same evidence. An explicit quartet passed by a caller (the curated merge file,
`services/ingest/organizations.py::load_merges`, docs/22 §20.9) is the evidence and is used as is.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import (
    REUSE_CLASSES,
    AssetOwner,
    Event,
    Licence,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
)


@dataclass(frozen=True)
class Quartet:
    source_id: str
    source_url: str
    retrieved_at: dt.datetime
    licence_id: str

    def columns(self) -> dict[str, Any]:
        """Keyword arguments for `Event(...)`."""
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "licence_id": self.licence_id,
        }


@dataclass(frozen=True)
class Evidence:
    """One stored row the event is about, with the quartet it carries."""

    quartet: Quartet
    #: True for the row whose evidence triggered the event (tie-break only, rule 4).
    triggering: bool
    #: A stable per-row key for the last tie-break (a record id or a row id).
    record_key: str


def is_complete(source_id: Any, source_url: Any, retrieved_at: Any, licence_id: Any) -> bool:
    return all(v is not None and v != "" for v in (source_id, source_url, retrieved_at, licence_id))


def of_event(event: Event) -> Quartet | None:
    """The event's own quartet when all four are set, else `None`."""
    if not is_complete(event.source_id, event.source_url, event.retrieved_at, event.licence_id):
        return None
    assert event.source_id is not None and event.source_url is not None  # noqa: S101 -- checked above
    assert event.retrieved_at is not None and event.licence_id is not None  # noqa: S101
    return Quartet(event.source_id, event.source_url, event.retrieved_at, event.licence_id)


def licence_rank(licence: Licence | None) -> tuple[int, ...]:
    """Larger is more restrictive. A licence id with no row ranks above every known class."""
    if licence is None:
        return (len(REUSE_CLASSES), 1, 1, 1, 1, 1)
    cls = (
        REUSE_CLASSES.index(licence.reuse_class)
        if licence.reuse_class in REUSE_CLASSES
        else len(REUSE_CLASSES)
    )
    return (
        cls,
        int(bool(licence.gate_flag)),
        int(not licence.allows_derived_publication),
        int(not licence.allows_raw_publication),
        int(not licence.allows_api_redistribution),
        int(not licence.allows_bulk_export),
    )


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def choose(session: Session, evidence: Iterable[Evidence]) -> Quartet | None:
    """The quartet of the most restrictive evidence row (module docstring rules 2-4)."""
    rows = list(evidence)
    if not rows:
        return None
    ranks = {lid: licence_rank(session.get(Licence, lid)) for lid in {e.quartet.licence_id for e in rows}}

    def key(e: Evidence) -> tuple[Any, ...]:
        return (
            tuple(-x for x in ranks[e.quartet.licence_id]),
            not e.triggering,
            -_aware(e.quartet.retrieved_at).timestamp(),
            e.quartet.source_id,
            e.record_key,
        )

    return min(rows, key=key).quartet


# --------------------------------------------------------------------------------------- proposals
def link_evidence(links: Iterable[ProposalSource], *, triggering: bool) -> list[Evidence]:
    return [
        Evidence(
            Quartet(link.source_id, link.source_url, link.retrieved_at, link.licence_id),
            triggering,
            f"{link.source_record_id}:{link.id}",
        )
        for link in links
    ]


def _links_of(session: Session, proposal_id: _uuid.UUID, *, active_only: bool) -> list[ProposalSource]:
    stmt = select(ProposalSource).where(ProposalSource.proposal_id == proposal_id)
    if active_only:
        stmt = stmt.where(ProposalSource.active.is_(True))
    return list(session.scalars(stmt.order_by(ProposalSource.id)).all())


def for_proposal_merge(
    session: Session, canonical_id: _uuid.UUID, absorbed_links: Sequence[ProposalSource]
) -> Quartet | None:
    """Merge or unmerge of a proposal: the survivor's active links and the absorbed record's links
    (`absorbed_links`, the triggering side). Call before the links move. When neither side has an
    active link, their inactive links are the evidence (a record that left its register is still
    the record its source published)."""
    absorbed_ids = {link.id for link in absorbed_links}
    canonical_links = [
        link for link in _links_of(session, canonical_id, active_only=True) if link.id not in absorbed_ids
    ]
    evidence = link_evidence(absorbed_links, triggering=True) + link_evidence(
        canonical_links, triggering=False
    )
    if not evidence:
        evidence = link_evidence(_links_of(session, canonical_id, active_only=False), triggering=False)
    return choose(session, evidence)


def for_suppression(links: Sequence[ProposalSource], triggering_ids: set[_uuid.UUID]) -> list[Evidence]:
    """A suppressed record's active links; the ones carrying the listed registry id trigger."""
    out: list[Evidence] = []
    for link in links:
        out.extend(link_evidence([link], triggering=link.id in triggering_ids))
    return out


# ---------------------------------------------------------------------------------- organisations
def _quartet_rows(rows: Iterable[Any], *, triggering: bool, prefix: str) -> list[Evidence]:
    return [
        Evidence(
            Quartet(r.source_id, r.source_url, r.retrieved_at, r.licence_id), triggering, f"{prefix}:{r.id}"
        )
        for r in rows
    ]


def organization_evidence(session: Session, org: Organization, *, triggering: bool) -> list[Evidence]:
    """Every stored row that names `org` (module docstring rule 1)."""
    sponsored = session.execute(
        select(Proposal.id, Proposal.merged_into_id).where(Proposal.sponsor_org_id == org.id)
    ).all()
    live_ids = [pid for pid, into in sponsored if into is None]
    links: list[ProposalSource] = []
    if live_ids:
        links.extend(
            session.scalars(
                select(ProposalSource).where(
                    ProposalSource.proposal_id.in_(live_ids), ProposalSource.active.is_(True)
                )
            ).all()
        )
    # A sponsored proposal merged into another record took its links with it; its merge event lists
    # them (`before.absorbed.proposal_source_ids`, docs/21 §6.3), whichever record holds them now.
    merge_keys = [f"merge:proposal:{into}:{pid}" for pid, into in sponsored if into is not None]
    if merge_keys:
        moved: list[_uuid.UUID] = []
        for before in session.scalars(select(Event.before).where(Event.idempotency_key.in_(merge_keys))):
            for raw in ((before or {}).get("absorbed") or {}).get("proposal_source_ids") or []:
                moved.append(_uuid.UUID(str(raw)))
        if moved:
            links.extend(
                session.scalars(
                    select(ProposalSource).where(
                        ProposalSource.id.in_(moved), ProposalSource.active.is_(True)
                    )
                ).all()
            )
    edges = session.scalars(select(AssetOwner).where(AssetOwner.organization_id == org.id)).all()
    aliases = session.scalars(
        select(OrganizationAlias).where(OrganizationAlias.organization_id == org.id)
    ).all()
    unique_links = list({link.id: link for link in links}.values())
    return (
        link_evidence(unique_links, triggering=triggering)
        + _quartet_rows(edges, triggering=triggering, prefix="asset_owner")
        + _quartet_rows(aliases, triggering=triggering, prefix="alias")
    )


def for_organization_merge(
    session: Session, canonical: Organization, absorbed: Organization
) -> Quartet | None:
    """Merge or unmerge of an organisation: everything naming either row, the absorbed one
    triggering. Call before rows move, so each side's evidence is still its own."""
    return choose(
        session,
        organization_evidence(session, absorbed, triggering=True)
        + organization_evidence(session, canonical, triggering=False),
    )


__all__ = [
    "Evidence",
    "Quartet",
    "choose",
    "for_organization_merge",
    "for_proposal_merge",
    "for_suppression",
    "is_complete",
    "licence_rank",
    "link_evidence",
    "of_event",
    "organization_evidence",
]
