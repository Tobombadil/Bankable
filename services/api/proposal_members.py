"""What a merged proposal is made of, as the caller's tier may see it (docs/22 §23.4; designer
audit 2026-09-30 D-5: "the proposal page hides the record's history and what was merged into it").

Three detail-only parts of `GET /v1/proposals/{id}`:

* `field_sources`: per served field, the readable sources that supplied it and the survivorship
  rule that chose it (`GatedRecord.field_sources`), so a page names the register a status or a
  figure came from rather than the first provenance row (audit F2: the Darden page credited CAISO
  for EIA's "(T) Regulatory approvals received").
* `members`: one row per readable active source link, with that source's own figures (name,
  technology, MW, MWh, lifecycle, COD) from the link's `normalised` row and the identifiers it
  contributes. Raw-class values (`status_raw`, `technology_raw`) and the source's own record id and
  identifiers appear only where the licence permits them, the same rule as `provenance_row`.
* `merge_history`: one item per `merged` event on the record that has not been reversed, naming
  only the links it brought that the caller may read. A merge whose links are all hidden is left
  out: the event's own payload (the absorbed row's full snapshot) is never served.

Nothing here reads a hidden source: every value comes from a link `GatedRecord` already admits.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.common import iso
from services.db.models import Event, Proposal, ProposalSource
from services.resolve import survivorship

#: Figures a member row prints from its link's own `normalised` row.
_MEMBER_FIELDS: tuple[str, ...] = (
    "name_canonical",
    "kind",
    "technology",
    "capacity_mw",
    "storage_mwh",
    "lifecycle_state",
    "proposed_online_date",
)
_RAW_FIELDS: tuple[str, ...] = ("status_raw", "technology_raw")


def _identifying_ok(link: ProposalSource) -> bool:
    licence = link.source.licence
    return bool(licence.allows_raw_publication or licence.reuse_class == "open")


def member_row(link: ProposalSource) -> dict[str, Any]:
    normalised = link.normalised or {}
    member = survivorship.member_from_link(link)
    row: dict[str, Any] = {
        "source_id": link.source_id,
        "source_name": link.source.name,
        "role": member.role,
        "retrieved_at": iso(link.retrieved_at),
        "first_seen": iso(link.first_seen),
        "gone_at": iso(link.gone_at),
    }
    for name in _MEMBER_FIELDS:
        row[name] = normalised.get(name)
    raw_ok = link.source.licence.allows_raw_publication
    for name in _RAW_FIELDS:
        row[name] = normalised.get(name) if raw_ok else None
    identifying = _identifying_ok(link)
    row["source_record_id"] = link.source_record_id if identifying else None
    row["identifiers"] = dict(member.identifiers) if identifying else {}
    return row


def proposal_members(links: list[ProposalSource]) -> list[dict[str, Any]]:
    """`members` for the readable active links, requests first, then the inventory, then the rest;
    within a role the largest MW first."""
    order = {"request": 0, "inventory": 1, "other": 2}
    rows = [member_row(link) for link in links if link.active]
    return sorted(
        rows,
        key=lambda r: (
            order.get(r["role"], 3),
            -(r["capacity_mw"] or 0.0),
            r["source_id"],
            r["retrieved_at"] or "",
        ),
    )


def merge_events(db: Session, proposal_ids: list[_uuid.UUID]) -> dict[_uuid.UUID, list[Event]]:
    """The unreversed `merged` events of each of `proposal_ids`, newest first: two queries for a
    whole page (the bulk stream) or one record (the detail route)."""
    if not proposal_ids:
        return {}
    merges = db.scalars(
        select(Event)
        .where(
            Event.subject_type == "proposal",
            Event.subject_id.in_(proposal_ids),
            Event.event_type == "merged",
        )
        .order_by(Event.observed_at.desc())
    ).all()
    if not merges:
        return {}
    reversed_ids = set(
        db.scalars(
            select(Event.reverses_event_id).where(
                Event.event_type == "unmerged",
                Event.reverses_event_id.in_([e.id for e in merges]),
            )
        ).all()
    )
    out: dict[_uuid.UUID, list[Event]] = {}
    for event in merges:
        if event.id not in reversed_ids:
            out.setdefault(event.subject_id, []).append(event)
    return out


def merge_history_from(events: list[Event], links: list[ProposalSource]) -> list[dict[str, Any]]:
    """`merge_history` from a record's unreversed merge events and the links the caller may read
    (module docstring): a merge that brought none of them is left out."""
    readable = {link.id: link for link in links if link.active}
    out: list[dict[str, Any]] = []
    for event in events:
        moved = ((event.before or {}).get("absorbed") or {}).get("proposal_source_ids") or []
        brought = [readable[lid] for lid in (_as_uuid(x) for x in moved) if lid in readable]
        if not brought:
            continue
        out.append(
            {
                "merged_at": iso(event.observed_at),
                "actor_type": event.actor_type,
                "confidence": float(event.confidence) if event.confidence is not None else None,
                "members": [
                    {
                        "source_id": link.source_id,
                        "source_name": link.source.name,
                        "source_record_id": link.source_record_id if _identifying_ok(link) else None,
                        "name_canonical": (link.normalised or {}).get("name_canonical"),
                        "capacity_mw": (link.normalised or {}).get("capacity_mw"),
                    }
                    for link in brought
                ],
            }
        )
    return out


def merge_history(db: Session, proposal: Proposal, links: list[ProposalSource]) -> list[dict[str, Any]]:
    """`merge_history` for one record, newest first."""
    return merge_history_from(merge_events(db, [proposal.id]).get(proposal.id, []), links)


def _as_uuid(value: Any) -> _uuid.UUID | None:
    try:
        return _uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
