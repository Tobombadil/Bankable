"""What this register does and does not contain, measured rather than asserted.

The problem
-----------
Three findings in one week said the same thing in three places: a company with no recorded parent
looks independent; a missing large-load category looks like no data centres exist; a
licence-withheld PJM looks like thin coverage. In each case an absence read as a fact about the
world when it was a fact about our coverage. Global Energy Monitor publishes capacity thresholds
and a scope statement so that a reader can tell the two apart; this module is that statement,
built so it cannot rot.

How it is kept true
-------------------
Every number here is a query or a read of `data/sources.yaml` at render time:

* Sources registered, and how many have rows flowing, come from the registry and the store.
* Withheld sources come from the registry's own `reuse`/`publication` classes — the same fields
  the ingestion gate reads — so a source that clears its licence leaves this list by being
  loaded, not by someone remembering to edit a page.
* Absent technologies are the vocabulary `pipeline/normalize.py::TECH_RULES` can emit minus the
  ones with rows. `load` is on that list today; nothing had to know in advance that it would be.
* Absent states are the code vocabularies minus the ones with rows.
* Ownership depth is a count of `organization.parent_org_id`.

Only the *why* is prose, and it lives in `data/vocabulary/coverage_notes.yaml` with the date it
was written, which the surfaces print. A note whose derived fact has stopped being true is
dropped here rather than rendered beside a measurement that contradicts it.

What this module deliberately does not do
-----------------------------------------
It does not touch visibility. Every count is taken over the whole store, because the question
"what is missing from this register" is not tier-dependent and answering it differently per tier
would be its own kind of dishonesty. It reads no gated row's content — a withheld source
contributes its registry entry and nothing else, which is exactly what the gate allows.
"""

from __future__ import annotations

import functools
import pathlib
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.normalize import TECH_RULES
from services.api.lifecycle import PRE_CONSTRUCTION
from services.db.models import (
    LIFECYCLE_STATES,
    OPPORTUNITY_STATUSES,
    Asset,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.ingest.vintage import UNDETERMINED, label_for

_ROOT = pathlib.Path(__file__).resolve().parents[2]
NOTES_PATH = _ROOT / "data" / "vocabulary" / "coverage_notes.yaml"

#: Reuse classes whose rows the ingestion gate refuses (`pipeline.connectors.registry.GATED_REUSE`
#: and `services/ingest/loader.py::_assert_not_gated`). Named again here rather than imported so
#: that a change to the gate is a deliberate change to this statement too.
WITHHELD_REUSE = ("restricted", "unknown")


@functools.lru_cache(maxsize=1)
def _notes() -> list[dict[str, Any]]:
    document = yaml.safe_load(NOTES_PATH.read_text(encoding="utf-8")) or {}
    return list(document.get("notes") or [])


@functools.lru_cache(maxsize=1)
def technology_vocabulary() -> tuple[str, ...]:
    """Every technology token the normaliser can produce, in rule order, plus the two fallbacks
    it emits when no rule matches. This is the denominator "which technologies have no rows" is
    measured against, so a technology nobody thought to look for still shows up as absent."""
    seen: dict[str, None] = {}
    for _pattern, technology, _kind in TECH_RULES:
        seen.setdefault(technology, None)
    seen.setdefault("other", None)
    seen.setdefault("unknown", None)
    return tuple(seen)


def _counts(db: Session, column: Any) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).group_by(column)).all()
    return {str(value): int(count) for value, count in rows if value is not None}


def _scalar(db: Session, statement: Any) -> int:
    return int(db.scalar(statement) or 0)


def _fetched_at(db: Session) -> dict[str, str]:
    """Newest `retrieved_at` per source, across all three row-bearing tables.

    `source.last_success_at` is written by the connector runner and is NULL for anything loaded
    through the file/dev path, so the release column would have had nothing to sit beside on
    exactly the surface whose point is the comparison. The link rows always carry the fetch, so
    this is the honest fallback -- and it is still *our* date, never promoted to a release."""
    out: dict[str, str] = {}
    for column, table in (
        (ProposalSource.source_id, ProposalSource.retrieved_at),
        (OpportunitySource.source_id, OpportunitySource.retrieved_at),
        (Asset.source_id, Asset.retrieved_at),
    ):
        for source_id, newest in db.execute(select(column, func.max(table)).group_by(column)).all():
            if source_id is None or newest is None:
                continue
            text = newest.isoformat() if hasattr(newest, "isoformat") else str(newest)
            if text > out.get(str(source_id), ""):
                out[str(source_id)] = text
    return out


def source_vintages(db: Session, *, with_fetched: bool = False) -> dict[str, Any]:
    """Per-source release, and the bound it puts on the data's age.

    `oldest` is the oldest release among sources that state one — the honest answer to "how old
    could what I am looking at be?" — and is `None` when no loaded source states a release at all.
    `not_stated` is not a gap to be filled with `retrieved_at`: it is the finding that the source
    publishes no release label, and the surfaces say so in those words.
    """
    rows: list[dict[str, Any]] = []
    # `with_fetched` groups the link tables to recover the fetch date; `/v1/health` calls this on
    # every probe and passes it off.
    fetched = _fetched_at(db) if with_fetched else {}
    # `vintage_basis IS NOT NULL` is precisely "a load has examined this source", which is both
    # the right population for this statement and one indexed table read -- `/v1/health` calls
    # this on every probe and must not scan the link tables to do it.
    for source in db.scalars(select(Source).where(Source.vintage_basis.is_not(None)).order_by(Source.id)):
        rows.append(
            {
                "source_id": source.id,
                "name": source.name,
                "vintage": source.vintage,
                "vintage_label": label_for(source.vintage),
                # NULL basis means no load has resolved it; the vocabulary has no word for that
                # state because it is the absence of one (`services/ingest/vintage.py`).
                "vintage_basis": source.vintage_basis or UNDETERMINED,
                # Ours, and labelled as ours wherever it renders.
                "fetched_at": (
                    source.last_success_at.isoformat() if source.last_success_at else fetched.get(source.id)
                ),
            }
        )
    stated = [r for r in rows if r["vintage"]]
    oldest = min(stated, key=lambda r: str(r["vintage"])) if stated else None
    return {
        "sources": rows,
        "oldest": (
            {
                "source_id": oldest["source_id"],
                "name": oldest["name"],
                "vintage": oldest["vintage"],
                "vintage_label": oldest["vintage_label"],
            }
            if oldest
            else None
        ),
        "sources_stating_a_release": len(stated),
        "sources_stating_none": sum(1 for r in rows if r["vintage_basis"] == "not_stated"),
        # Rows in the store whose release no load has resolved -- loaders that write through a
        # path with no artefact URL (the curated and GLEIF organisation files). Counted, not
        # hidden: "we have not looked" is a different answer from "the source states none".
        "sources_undetermined": _scalar(
            db, select(func.count()).select_from(Source).where(Source.vintage_basis.is_(None))
        ),
    }


def _loaded_source_ids(db: Session) -> set[str]:
    """Source ids with at least one stored row, across all three row-bearing tables."""
    out: set[str] = set()
    for column in (ProposalSource.source_id, OpportunitySource.source_id, Asset.source_id):
        out.update(str(v) for v in db.scalars(select(column).distinct()) if v)
    return out


#: Registry categories that were never candidates for publication as rows, so listing them as
#: "withheld" would be padding: the social channels are our own outbound accounts, and news is a
#: lookup trigger only and never content (docs/13, CLAUDE.md).
NON_RECORD_CATEGORIES = ("social_channel", "news", "aggregator")

#: Categories that would have produced proposals — the absences a reader of a supply map feels.
SUPPLY_CATEGORIES = ("generation_queue", "load_queue")


def _withheld(registry: Registry) -> list[dict[str, Any]]:
    """Registered sources whose rows the licence gate refuses, with the reason in the registry's
    own words. Nothing here reads a gated row; a withheld source contributes its manifest entry,
    which is what the terms allow.

    Supply registers sort first: a reader looking at a map of proposals and seeing nothing in the
    PJM footprint needs that row before it needs a withheld tender feed."""
    out: list[dict[str, Any]] = []
    for source_id in registry.ids():
        entry = registry.get(source_id)
        if entry.reuse not in WITHHELD_REUSE and entry.publication != "none":
            continue
        if entry.category in NON_RECORD_CATEGORIES or entry.never_ingest:
            # Private aggregators are refused on principle, not pending a licence; listing them as
            # "withheld" would imply a licence could change it (CLAUDE.md guardrail).
            continue
        out.append(
            {
                "source_id": entry.id,
                "name": entry.name,
                "operator": entry.operator or None,
                "jurisdiction": entry.jurisdiction or None,
                "category": entry.category,
                "reuse": entry.reuse,
                "publication": entry.publication,
                "url": entry.url,
                "supply": entry.category in SUPPLY_CATEGORIES,
                # The registry's two distinct reasons, kept apart: terms that forbid republication
                # without a licence, and terms nobody has been able to retrieve at all.
                "reason": (
                    "terms require a licence or consent we do not hold"
                    if entry.reuse == "restricted"
                    else "terms not retrievable, so nothing is assumed"
                ),
            }
        )
    return sorted(out, key=lambda r: (not r["supply"], str(r["source_id"])))


def coverage(db: Session, registry: Registry | None = None) -> dict[str, Any]:
    """The whole statement. One call, because every surface that renders part of it should be
    rendering the same numbers."""
    registry = registry or Registry()
    registered_ids = registry.ids()
    loaded = _loaded_source_ids(db)

    proposal_kinds = _counts(db, Proposal.kind)
    opportunity_kinds = _counts(db, Opportunity.kind)
    technologies = _counts(db, Proposal.technology)
    lifecycle = _counts(db, Proposal.lifecycle_state)
    statuses = _counts(db, Opportunity.status)
    jurisdictions = _counts(db, Proposal.jurisdiction)

    absent_technologies = [t for t in technology_vocabulary() if technologies.get(t, 0) == 0]
    withheld = _withheld(registry)

    organizations = _scalar(db, select(func.count()).select_from(Organization))
    with_parent = _scalar(
        db, select(func.count()).select_from(Organization).where(Organization.parent_org_id.is_not(None))
    )
    parent_sources = sorted(
        {str(v) for v in db.scalars(select(Organization.parent_source_id).distinct()) if v is not None}
    )

    facts: dict[str, Any] = {
        "sources": {
            "registered": len(registered_ids),
            "with_rows": len(loaded),
            "loaded_source_ids": sorted(loaded),
            "withheld": withheld,
        },
        "vintage": source_vintages(db, with_fetched=True),
        "records": {
            "proposals": _scalar(db, select(func.count()).select_from(Proposal)),
            "opportunities": _scalar(db, select(func.count()).select_from(Opportunity)),
            "assets": _scalar(db, select(func.count()).select_from(Asset)),
            "proposal_kinds": proposal_kinds,
            "opportunity_kinds": opportunity_kinds,
            "jurisdictions": jurisdictions,
        },
        "technologies": {
            "present": technologies,
            # Derived, not listed: the normaliser's own vocabulary minus what has rows.
            "absent": absent_technologies,
            "vocabulary": list(technology_vocabulary()),
        },
        "states": {
            "lifecycle_counts": lifecycle,
            "lifecycle_absent": [s for s in LIFECYCLE_STATES if lifecycle.get(s, 0) == 0],
            "opportunity_status_counts": statuses,
            "opportunity_status_absent": [s for s in OPPORTUNITY_STATUSES if statuses.get(s, 0) == 0],
            "pre_construction_states": list(PRE_CONSTRUCTION),
            "pre_construction_count": sum(lifecycle.get(s, 0) for s in PRE_CONSTRUCTION),
        },
        "ownership": {
            "organizations": organizations,
            "with_recorded_parent": with_parent,
            "without_recorded_parent": organizations - with_parent,
            "parent_source_ids": parent_sources,
        },
    }
    facts["notes"] = _applicable_notes(facts)
    return facts


def _applicable_notes(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """The written notes whose derived fact still holds, each with the date it was written.

    A note is dropped when its fact stops being true — a technology that gains rows, a licence
    that clears — so the prose can never contradict the measurement printed beside it."""
    out: list[dict[str, Any]] = []
    for note in _notes():
        applies = note.get("applies_to") or {}
        if "absent_technology" in applies:
            if applies["absent_technology"] not in facts["technologies"]["absent"]:
                continue
        if applies.get("withheld_sources") and not facts["sources"]["withheld"]:
            continue
        if applies.get("ownership") and facts["ownership"]["without_recorded_parent"] == 0:
            continue
        out.append(
            {
                "id": note.get("id"),
                "headline": (note.get("headline") or "").strip(),
                "body": (note.get("body") or "").strip(),
                "written": str(note.get("written") or ""),
            }
        )
    return out
