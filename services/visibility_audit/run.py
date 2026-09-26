"""The nightly M-11 visibility audit: "publish-gate breaches: gated or restricted records visible
on any non-admin surface" (docs/10-prd-mvp.md §5, source of truth "US-906 integration test +
nightly audit query", target 0, any breach blocks release). docs/04-standards.md R-4 signs off on
"M-11 = 0 in the nightly audit", S-9 makes a breach an S1 incident, and docs/40-launch-runbook.md
§4 row 11 recorded that nothing computed it. This module is that job.

Two passes, deliberately independent of each other:

1. **Store pass** (`audit_store`). The public (anonymous) set of every surface is recomputed from
   the store through the one predicate the API uses (`services/api/visibility.py`, called, never
   re-stated), and every row in it is then checked against the *publication invariants* written
   here without the predicate: the record is `publish_state = public` and past `public_at`; its
   `min_reuse_class` is publishable under the posture in force (`services/posture.py`); every
   active source link points at a source whose licence class is publishable, whose own
   `publish_state` is `public`, and which the register (`data/sources.yaml`) does not mark
   `publication: none` / a gated `reuse` — the PJM/MISO/SPP/ISO-NE rows `CLAUDE.md` names; an
   event's own source and licence pass the same tests and its subject record is public; an asset's
   own source and licence pass; an organisation is not on the surface *only* because of gated
   evidence. A row that passes the predicate and fails an invariant is a breach. This catches the
   two ways M-11 goes wrong in practice: a predicate regression, and a store that drifted under a
   correct predicate (a licence reclassified after load while the source stayed `public`, a source
   that should never have been flipped). It also checks the **source links** the record pages
   serve (`/v1/proposals/{id}/sources`, `provenance` on every detail): docs/21 §8 item 3 says a
   gated source's link row is omitted, not greyed, so a shown record with an active link to a
   gated source is a breach on the `source_links` surface even when the record itself is rightly
   public on other evidence.
2. **Served pass** (`served_pass`). Belt and braces: real requests through the FastAPI
   `TestClient` against the real app with no credentials, for a sample of the store pass's
   breaches (does the API actually serve what the store says it would?) and for a sample of rows
   that must be hidden whatever the predicate says (taken-down and pending records, records and
   assets whose only evidence is gated). A `200` on a must-be-hidden row is a breach in its own
   right (`served_hidden`), so a serving path that bypasses the predicate is caught even when the
   predicate is right.

`m11` is the total number of breaches from both passes. The result is persisted **without a new
table**: one append-only `event` row (docs/21 §3.10; subject type `source`, the closest existing
vocabulary entry for a platform-wide publication check; `event_type = visibility_audit`;
`actor_type = system`; the result dict in `after`). `services.api.audit.record_audit_event` was
the intended writer but requires a human `actor: User` and hard-codes `actor_type = "user"` —
the nightly job has no operator, and `docs/21` §3.10 gives `system` for exactly this case — so
`persist_result` writes the same row shape directly with the system actor; `GET
/admin/v1/visibility-audits` (`services/api/admin_audit_routes.py`) reads it back. The row is
never public: `published_at`/`public_at` are null and `event_visibility_filter` admits only
`proposal`/`opportunity` subjects.

Run by hand (exit 1 when `m11 > 0`, so a cron or a CI step can gate on it):

    python -m services.visibility_audit.run --posture auto [--no-persist] [--sample 25] [--json]

Scheduled: `visibility_audit_tick` in `infra/scheduler/app.py`, nightly after the daily fetch
bucket (docs/60-deployment.md §6.1), body `infra/scheduler/jobs.py::visibility_audit_tick_job`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased, sessionmaker

from services.api import visibility
from services.db.models import (
    Asset,
    AssetOwner,
    AssetSource,
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.db.session import get_engine, get_sessionmaker, session_scope
from services.ids import public_id
from services.posture import (
    PLATFORM_POSTURES,
    normalise_posture,
    platform_posture,
    publishable_reuse_classes,
)

logger = logging.getLogger("services.visibility_audit")

ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = ROOT / "data" / "sources.yaml"

#: The audit row in the append-only log (module docstring): `event.subject_type` from docs/21
#: §3.10's vocabulary, a deterministic subject id so every run shares one subject (and the
#: `ix_event_subject_observed` index groups them), and an event type outside the §7.3 vocabulary,
#: named by the task and read back by the admin route. `event.event_type` carries no CHECK.
EVENT_TYPE = "visibility_audit"
SUBJECT_TYPE = "source"
SUBJECT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "bankable:source:__visibility_audit__")
AUDIT_REASON = "nightly M-11 visibility audit (docs/04 R-4; a breach is an S1 incident, docs/04 S-9)"

#: The breach list persisted and returned is capped; `breach_total`/`m11` are never capped.
BREACH_CAP = 200
#: Requests issued by the served pass per candidate group (breaches, must-be-hidden rows). Kept
#: under the public tier's per-minute budget (`services/api/ratelimit.py`) with room to spare.
SERVED_SAMPLE = 25

SURFACES: tuple[str, ...] = (
    "proposals",
    "opportunities",
    "events",
    "organizations",
    "assets",
    "source_links",
)
#: Subject types an event may be served for (`event_visibility_filter`'s subject join).
_EVENT_SUBJECTS = ("proposal", "opportunity")
#: Record `publish_state` values that mean "must not be served on any non-admin surface".
HIDDEN_RECORD_STATES = ("unpublished", "pending_review")

Predicate = Callable[..., list[ColumnElement[bool]]]
#: The predicate, called by surface. Held in a dict so a test can stand in a regressed predicate
#: for one surface and prove the invariant checks catch it; production never rebinds these.
PREDICATES: dict[str, Predicate] = {
    "proposals": visibility.proposal_visibility_filter,
    "opportunities": visibility.opportunity_visibility_filter,
    "events": visibility.event_visibility_filter,
    "assets": visibility.asset_visibility_filter,
}


@dataclass
class Breach:
    surface: str
    public_id: str
    source_id: str | None
    reason: str
    #: Filled by the served pass for the sampled breaches: the HTTP status an anonymous request
    #: for the row got, and whether the response carried the offending row/link.
    served_status: int | None = None
    served_leak: bool | None = None


@dataclass(frozen=True)
class _Candidate:
    """A row that must be hidden on the public surface whatever the predicate says."""

    surface: str
    public_id: str
    why: str


# ================================================================================ the register
def register_gated_sources(posture: str, path: Path | None = None) -> dict[str, str]:
    """Source ids `data/sources.yaml` says may not be published under `posture`: `publication:
    none` or a `reuse` class outside the posture's publishable set. The register is the owner's
    statement of terms (`CLAUDE.md`; docs/21 §8 "declared, not inferred"), so a store row on one
    of these sources is a breach even when the store's own licence row disagrees. A missing or
    unreadable register yields an empty set and is reported, never raised — the store checks
    still run."""
    target = path if path is not None else SOURCES_YAML
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("register unreadable: %s", exc)
        return {}
    publishable = publishable_reuse_classes(posture)
    gated: dict[str, str] = {}
    for entry in doc.get("sources") or []:
        if not isinstance(entry, Mapping) or "id" not in entry:
            continue
        source_id = str(entry["id"])
        publication = str(entry.get("publication") or "")
        reuse = str(entry.get("reuse") or "unknown")
        if publication == "none":
            gated[source_id] = "register:publication=none"
        elif reuse not in publishable:
            gated[source_id] = f"register:reuse={reuse}"
    return gated


def gated_sources(db: Session, posture: str, register: Mapping[str, str]) -> dict[str, str]:
    """Every `source` row that may not stand behind a public row under `posture`, with the reason
    (the first that applies; all are joined with `;`): licence class not publishable, the source's
    own `publish_state` not `public`, or the register's verdict."""
    publishable = publishable_reuse_classes(posture)
    out: dict[str, str] = {}
    for source in db.scalars(select(Source)).all():
        reasons: list[str] = []
        if source.licence.reuse_class not in publishable:
            reasons.append(f"source_class_gated:{source.licence.reuse_class}")
        if source.publish_state != "public":
            reasons.append(f"source_not_public:{source.publish_state}")
        if source.id in register:
            reasons.append(register[source.id])
        if reasons:
            out[source.id] = ";".join(reasons)
    return out


def gated_licences(db: Session, posture: str) -> dict[str, str]:
    publishable = publishable_reuse_classes(posture)
    rows = db.execute(select(Licence.id, Licence.reuse_class).where(Licence.reuse_class.not_in(publishable)))
    return {lic_id: f"licence_class_gated:{cls}" for lic_id, cls in rows.all()}


# ================================================================================ store pass
def _count(db: Session, model: type[Any], where: list[ColumnElement[bool]]) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _audit_records(
    db: Session,
    *,
    surface: str,
    model: type[Proposal] | type[Opportunity],
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk: InstrumentedAttribute[uuid.UUID],
    gated_src: Mapping[str, str],
    publishable: tuple[str, ...],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES[surface]("public", now)
    count = _count(db, model, shown)
    breaches: list[Breach] = []

    # Record-level invariants, on the shown set. Vacuous while the predicate is right — which is
    # the point: they are the independent restatement a regression cannot share.
    rows = db.execute(
        select(model.public_id, model.publish_state, model.min_reuse_class, model.public_at).where(
            *shown,
            or_(
                model.publish_state != "public",
                model.min_reuse_class.not_in(publishable),
                model.public_at.is_(None),
                model.public_at > now,
            ),
        )
    ).all()
    for pid, state, cls, public_at in rows:
        if state != "public":
            breaches.append(Breach(surface, pid, None, f"record_not_public:{state}"))
        if cls not in publishable:
            breaches.append(Breach(surface, pid, None, f"record_class_gated:{cls}"))
        if public_at is None or _aware(public_at) > now:
            breaches.append(Breach(surface, pid, None, "record_not_yet_public"))

    # Link-level invariants: a shown record's active links to gated sources. If the record has
    # no clean link at all it is on the surface only through gated evidence (a record breach);
    # otherwise the record is legitimately public and the gated link row is the breach
    # (docs/21 §8 item 3, the mixed-provenance case).
    if gated_src:
        gated_ids = list(gated_src)
        link_rows = db.execute(
            select(model.id, model.public_id, link_model.source_id)
            .join(link_model, fk == model.id)
            .where(*shown, link_model.active.is_(True), link_model.source_id.in_(gated_ids))
        ).all()
        affected = {rid for rid, _, _ in link_rows}
        clean: set[uuid.UUID] = set()
        if affected:
            clean = set(
                db.scalars(
                    select(fk).where(
                        link_model.active.is_(True),
                        link_model.source_id.not_in(gated_ids),
                        fk.in_(list(affected)),
                    )
                ).all()
            )
        for rid, pid, sid in link_rows:
            reason = gated_src[sid]
            if rid in clean:
                breaches.append(Breach("source_links", pid, sid, f"source_link_gated:{reason}"))
            else:
                breaches.append(Breach(surface, pid, sid, f"only_gated_evidence:{reason}"))
    return count, breaches


def _audit_events(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES["events"]("public", now)
    count = _count(db, Event, shown)
    src_clause = Event.source_id.in_(list(gated_src)) if gated_src else sa.false()
    lic_clause = Event.licence_id.in_(list(gated_lic)) if gated_lic else sa.false()
    subject_hidden = or_(
        Event.subject_type.not_in(_EVENT_SUBJECTS),
        exists(
            select(Proposal.id).where(
                Event.subject_type == "proposal",
                Proposal.id == Event.subject_id,
                Proposal.publish_state != "public",
            )
        ),
        exists(
            select(Opportunity.id).where(
                Event.subject_type == "opportunity",
                Opportunity.id == Event.subject_id,
                Opportunity.publish_state != "public",
            )
        ),
    )
    rows = db.execute(
        select(
            Event.id, Event.subject_type, Event.subject_id, Event.source_id, Event.licence_id, Event.public_at
        ).where(
            *shown,
            or_(src_clause, lic_clause, subject_hidden, Event.public_at.is_(None), Event.public_at > now),
        )
    ).all()
    breaches: list[Breach] = []
    for eid, subject_type, subject_id, source_id, licence_id, public_at in rows:
        pid = public_id("evt", eid)
        if source_id in gated_src:
            breaches.append(Breach("events", pid, source_id, f"event_source_gated:{gated_src[source_id]}"))
        if licence_id in gated_lic:
            breaches.append(Breach("events", pid, source_id, f"event_{gated_lic[licence_id]}"))
        if subject_type not in _EVENT_SUBJECTS:
            breaches.append(Breach("events", pid, source_id, f"event_subject_type:{subject_type}"))
        else:
            state = _subject_publish_state(db, subject_type, subject_id)
            if state is not None and state != "public":
                breaches.append(Breach("events", pid, source_id, f"event_subject_not_public:{state}"))
        if public_at is None or _aware(public_at) > now:
            breaches.append(Breach("events", pid, source_id, "event_not_yet_public"))
    return count, breaches


def _subject_publish_state(db: Session, subject_type: str, subject_id: uuid.UUID) -> str | None:
    if subject_type == "proposal":
        proposal = db.get(Proposal, subject_id)
        return proposal.publish_state if proposal is not None else None
    opportunity = db.get(Opportunity, subject_id)
    return opportunity.publish_state if opportunity is not None else None


def _audit_assets(
    db: Session,
    *,
    gated_src: Mapping[str, str],
    gated_lic: Mapping[str, str],
    now: dt.datetime,
) -> tuple[int, list[Breach]]:
    shown = PREDICATES["assets"]("public", now)
    count = _count(db, Asset, shown)
    breaches: list[Breach] = []
    src_clause = Asset.source_id.in_(list(gated_src)) if gated_src else sa.false()
    lic_clause = Asset.licence_id.in_(list(gated_lic)) if gated_lic else sa.false()
    rows = db.execute(
        select(Asset.public_id, Asset.source_id, Asset.licence_id).where(*shown, or_(src_clause, lic_clause))
    ).all()
    for pid, source_id, licence_id in rows:
        if source_id in gated_src:
            breaches.append(Breach("assets", pid, source_id, f"asset_source_gated:{gated_src[source_id]}"))
        if licence_id in gated_lic:
            breaches.append(Breach("assets", pid, source_id, f"asset_{gated_lic[licence_id]}"))
    if gated_src:
        link_rows = db.execute(
            select(Asset.public_id, Asset.source_id, AssetSource.source_id)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .where(*shown, AssetSource.source_id.in_(list(gated_src)))
        ).all()
        for pid, own_source, link_source in link_rows:
            if link_source == own_source:
                continue  # already a breach on the asset itself above
            breaches.append(
                Breach("source_links", pid, link_source, f"source_link_gated:{gated_src[link_source]}")
            )
    return count, breaches


def _audit_organizations(
    db: Session, *, gated_src: Mapping[str, str], now: dt.datetime
) -> tuple[int, list[Breach]]:
    """`GET /v1/organizations` serves every unmerged organisation (it has no licence of its own,
    docs/21 §3.5), so the surface check is existence-based: an organisation with no visible
    proposal, opportunity or asset edge, but with at least one edge to a gated source, is on the
    surface only because of gated evidence — the existence disclosure docs/21 §8 item 3 forbids.
    "Visible" here means shown by the predicate *and* resting on at least one clean link, so an
    organisation whose only shown record is itself a gated-evidence breach is counted too."""
    served: list[ColumnElement[bool]] = [Organization.merged_into_id.is_(None)]
    count = _count(db, Organization, served)
    if not gated_src:
        return count, []
    gated_ids = list(gated_src)
    clean_p = aliased(ProposalSource)
    clean_o = aliased(OpportunitySource)
    visible = or_(
        exists(
            select(Proposal.id).where(
                Proposal.sponsor_org_id == Organization.id,
                *PREDICATES["proposals"]("public", now),
                exists(
                    select(clean_p.id).where(
                        clean_p.proposal_id == Proposal.id,
                        clean_p.active.is_(True),
                        clean_p.source_id.not_in(gated_ids),
                    )
                ),
            )
        ),
        exists(
            select(Opportunity.id).where(
                Opportunity.issuer_org_id == Organization.id,
                *PREDICATES["opportunities"]("public", now),
                exists(
                    select(clean_o.id).where(
                        clean_o.opportunity_id == Opportunity.id,
                        clean_o.active.is_(True),
                        clean_o.source_id.not_in(gated_ids),
                    )
                ),
            )
        ),
        exists(
            select(AssetOwner.id)
            .join(Asset, Asset.id == AssetOwner.asset_id)
            .where(
                AssetOwner.organization_id == Organization.id,
                AssetOwner.source_id.not_in(gated_ids),
                Asset.source_id.not_in(gated_ids),
                *PREDICATES["assets"]("public", now),
            )
        ),
    )
    gated_edge = or_(
        exists(
            select(ProposalSource.id)
            .join(Proposal, Proposal.id == ProposalSource.proposal_id)
            .where(Proposal.sponsor_org_id == Organization.id, ProposalSource.source_id.in_(gated_ids))
        ),
        exists(
            select(OpportunitySource.id)
            .join(Opportunity, Opportunity.id == OpportunitySource.opportunity_id)
            .where(Opportunity.issuer_org_id == Organization.id, OpportunitySource.source_id.in_(gated_ids))
        ),
        exists(
            select(AssetOwner.id).where(
                AssetOwner.organization_id == Organization.id, AssetOwner.source_id.in_(gated_ids)
            )
        ),
    )
    rows = db.execute(
        select(Organization.id, Organization.public_id).where(*served, gated_edge, ~visible)
    ).all()
    breaches: list[Breach] = []
    for oid, pid in rows:
        sid = _first_gated_org_source(db, oid, gated_ids)
        reason = gated_src.get(sid or "", "gated")
        breaches.append(Breach("organizations", pid, sid, f"organization_gated_evidence_only:{reason}"))
    return count, breaches


def _first_gated_org_source(db: Session, org_id: uuid.UUID, gated_ids: list[str]) -> str | None:
    for stmt in (
        select(ProposalSource.source_id)
        .join(Proposal, Proposal.id == ProposalSource.proposal_id)
        .where(Proposal.sponsor_org_id == org_id, ProposalSource.source_id.in_(gated_ids)),
        select(OpportunitySource.source_id)
        .join(Opportunity, Opportunity.id == OpportunitySource.opportunity_id)
        .where(Opportunity.issuer_org_id == org_id, OpportunitySource.source_id.in_(gated_ids)),
        select(AssetOwner.source_id).where(
            AssetOwner.organization_id == org_id, AssetOwner.source_id.in_(gated_ids)
        ),
    ):
        found = db.scalar(stmt.limit(1))
        if found is not None:
            return str(found)
    return None


def _hidden_candidates(db: Session, gated_src: Mapping[str, str], *, limit: int) -> list[_Candidate]:
    """Rows that must be hidden whatever the predicate says, for the served pass: taken-down and
    pending records, records whose *only* active evidence is gated (a mixed-provenance record is
    rightly public on its clean evidence, docs/21 §8, so it is not a candidate here; its gated
    link is the store pass's `source_links` check), assets on gated sources."""
    out: list[_Candidate] = []
    for surface, model in (("proposals", Proposal), ("opportunities", Opportunity)):
        rows = db.execute(
            select(model.public_id, model.publish_state)
            .where(model.publish_state.in_(HIDDEN_RECORD_STATES))
            .order_by(model.public_id)
            .limit(limit)
        ).all()
        out.extend(_Candidate(surface, pid, f"publish_state={state}") for pid, state in rows)
    if gated_src:
        gated_ids = list(gated_src)
        clean_p = aliased(ProposalSource)
        clean_o = aliased(OpportunitySource)
        for surface, model, link_model, fk, clean, clean_fk in (
            ("proposals", Proposal, ProposalSource, ProposalSource.proposal_id, clean_p, clean_p.proposal_id),
            (
                "opportunities",
                Opportunity,
                OpportunitySource,
                OpportunitySource.opportunity_id,
                clean_o,
                clean_o.opportunity_id,
            ),
        ):
            has_clean = exists(
                select(clean.id).where(
                    clean_fk == model.id,
                    clean.active.is_(True),
                    clean.source_id.not_in(gated_ids),
                )
            )
            rows = db.execute(
                select(model.public_id, func.min(link_model.source_id))
                .join(link_model, fk == model.id)
                .where(link_model.active.is_(True), link_model.source_id.in_(gated_ids), ~has_clean)
                .group_by(model.public_id)
                .order_by(model.public_id)
                .limit(limit)
            ).all()
            out.extend(_Candidate(surface, pid, f"only_gated_source={sid}") for pid, sid in rows)
        rows = db.execute(
            select(Asset.public_id, Asset.source_id)
            .where(Asset.source_id.in_(gated_ids))
            .order_by(Asset.public_id)
            .limit(limit)
        ).all()
        out.extend(_Candidate("assets", pid, f"source={sid}") for pid, sid in rows)
    return out


def audit_store(
    db: Session,
    *,
    posture: str,
    now: dt.datetime | None = None,
    register_path: Path | None = None,
) -> dict[str, Any]:
    """The store pass (module docstring, 1). Returns the result dict minus the served pass."""
    now = now or dt.datetime.now(dt.UTC)
    publishable = publishable_reuse_classes(posture)
    register = register_gated_sources(posture, register_path)
    gated_src = gated_sources(db, posture, register)
    gated_lic = gated_licences(db, posture)

    counts: dict[str, dict[str, int]] = {s: {"shown": 0, "breaches": 0} for s in SURFACES}
    breaches: list[Breach] = []
    for surface, model, link_model, fk in (
        ("proposals", Proposal, ProposalSource, ProposalSource.proposal_id),
        ("opportunities", Opportunity, OpportunitySource, OpportunitySource.opportunity_id),
    ):
        shown, found = _audit_records(
            db,
            surface=surface,
            model=model,
            link_model=link_model,
            fk=fk,
            gated_src=gated_src,
            publishable=publishable,
            now=now,
        )
        counts[surface]["shown"] = shown
        breaches.extend(found)
    shown, found = _audit_events(db, gated_src=gated_src, gated_lic=gated_lic, now=now)
    counts["events"]["shown"] = shown
    breaches.extend(found)
    shown, found = _audit_organizations(db, gated_src=gated_src, now=now)
    counts["organizations"]["shown"] = shown
    breaches.extend(found)
    shown, found = _audit_assets(db, gated_src=gated_src, gated_lic=gated_lic, now=now)
    counts["assets"]["shown"] = shown
    breaches.extend(found)
    # The links surface has no independent row count of its own: what it "shows" is the active
    # link rows of the shown proposals, opportunities and assets.
    counts["source_links"]["shown"] = _shown_link_count(db, now)
    for breach in breaches:
        counts[breach.surface]["breaches"] += 1

    return {
        "run_id": str(uuid.uuid4()),
        "run_at": now.isoformat(),
        "posture": posture,
        "posture_source": "platform" if posture == platform_posture() else "override",
        "publishable_reuse_classes": list(publishable),
        "register_path": str(register_path or SOURCES_YAML),
        "register_gated_sources": len(register),
        "gated_sources": dict(sorted(gated_src.items())),
        "counts": counts,
        "breaches": [asdict(b) for b in breaches[:BREACH_CAP]],
        "breach_total": len(breaches),
        "breach_cap": BREACH_CAP,
        "breaches_truncated": len(breaches) > BREACH_CAP,
        "served": {"checked": 0, "leaks": 0, "inconclusive": 0, "checks": []},
        "m11": len(breaches),
        "_breaches": breaches,  # in-memory only; stripped before persisting/returning
    }


def _shown_link_count(db: Session, now: dt.datetime) -> int:
    total = 0
    for model, link_model, fk, surface in (
        (Proposal, ProposalSource, ProposalSource.proposal_id, "proposals"),
        (Opportunity, OpportunitySource, OpportunitySource.opportunity_id, "opportunities"),
    ):
        total += int(
            db.scalar(
                select(func.count())
                .select_from(link_model)
                .join(model, fk == model.id)
                .where(link_model.active.is_(True), *PREDICATES[surface]("public", now))
            )
            or 0
        )
    total += int(
        db.scalar(
            select(func.count())
            .select_from(AssetSource)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(*PREDICATES["assets"]("public", now))
        )
        or 0
    )
    return total


# ================================================================================ served pass
def _detail_path(surface: str, pid: str) -> str | None:
    if surface == "proposals":
        return f"/v1/proposals/{pid}"
    if surface == "opportunities":
        return f"/v1/opportunities/{pid}"
    if surface == "events":
        return f"/v1/events/{pid}"
    if surface == "organizations":
        return f"/v1/organizations/{pid}"
    if surface == "assets":
        return f"/v1/assets/{pid}"
    if surface == "source_links":
        if pid.startswith("prop_"):
            return f"/v1/proposals/{pid}/sources"
        if pid.startswith("opp_"):
            return f"/v1/opportunities/{pid}/sources"
        if pid.startswith("asset_"):
            return f"/v1/assets/{pid}"
    return None


def _mentions_source(node: Any, source_id: str) -> bool:
    if isinstance(node, dict):
        if node.get("source_id") == source_id:
            return True
        return any(_mentions_source(v, source_id) for v in node.values())
    if isinstance(node, list):
        return any(_mentions_source(v, source_id) for v in node)
    return False


@contextmanager
def anonymous_client(session_factory: sessionmaker[Session]) -> Iterator[Any]:
    """The real app, no credentials, reading the audited store. `get_db` is overridden for the
    duration and the previous override (a test's, if any) is put back."""
    from fastapi.testclient import TestClient

    from services.api.app import app
    from services.api.deps import get_db

    def _override() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def served_pass(
    session_factory: sessionmaker[Session],
    result: dict[str, Any],
    candidates: list[_Candidate],
    *,
    sample: int = SERVED_SAMPLE,
) -> None:
    """The served pass (module docstring, 2). Mutates `result` in place: annotates the sampled
    breaches, appends `served_hidden` breaches, fills `result["served"]` and recomputes `m11`."""
    breaches: list[Breach] = result["_breaches"]
    already = {(b.surface, b.public_id) for b in breaches}
    checks: list[dict[str, Any]] = []
    leaks = 0
    with anonymous_client(session_factory) as client:
        for breach in breaches[:sample]:
            path = _detail_path(breach.surface, breach.public_id)
            if path is None:
                continue
            response = client.get(path)
            breach.served_status = response.status_code
            leak = response.status_code == 200
            if leak and breach.surface == "source_links" and breach.source_id:
                leak = _mentions_source(response.json(), breach.source_id)
            breach.served_leak = leak
            leaks += int(leak)
            checks.append(
                {
                    "kind": "breach",
                    "surface": breach.surface,
                    "public_id": breach.public_id,
                    "path": path,
                    "status": response.status_code,
                    "leak": leak,
                }
            )
        # A candidate the store pass already counted is not requested again: its breach is in
        # the list once, and the sampled-breach loop above is where its served status lands.
        fresh = [c for c in candidates if (c.surface, c.public_id) not in already]
        for candidate in fresh[:sample]:
            path = _detail_path(candidate.surface, candidate.public_id)
            if path is None:
                continue
            response = client.get(path)
            leak = response.status_code == 200
            leaks += int(leak)
            checks.append(
                {
                    "kind": "must_be_hidden",
                    "surface": candidate.surface,
                    "public_id": candidate.public_id,
                    "path": path,
                    "status": response.status_code,
                    "leak": leak,
                    "why": candidate.why,
                }
            )
            if leak:
                breach = Breach(
                    candidate.surface, candidate.public_id, None, f"served_hidden:{candidate.why}", 200, True
                )
                breaches.append(breach)
                result["counts"][candidate.surface]["breaches"] += 1
    # A status that is neither "served" (200) nor "hidden" (404) — a 429 from the public tier's
    # hourly budget, a 5xx — proves nothing either way; it is counted so a run whose served pass
    # was starved cannot read as a clean one.
    inconclusive = sum(1 for c in checks if c["status"] not in (200, 404))
    result["served"] = {
        "checked": len(checks),
        "leaks": leaks,
        "inconclusive": inconclusive,
        "checks": checks,
    }
    result["breaches"] = [asdict(b) for b in breaches[:BREACH_CAP]]
    result["breach_total"] = len(breaches)
    result["breaches_truncated"] = len(breaches) > BREACH_CAP
    result["m11"] = len(breaches)


# ================================================================================ persistence
def persist_result(db: Session, result: Mapping[str, Any]) -> Event:
    """One `event` row per run (module docstring). `after` holds the whole result; the row is
    system-actored, unpublished on every tier, and idempotent per `run_id`."""
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    run_at = dt.datetime.fromisoformat(str(payload["run_at"]))
    event = Event(
        subject_type=SUBJECT_TYPE,
        subject_id=SUBJECT_ID,
        event_type=EVENT_TYPE,
        observed_at=run_at,
        published_at=None,
        public_at=None,
        before=None,
        after=payload,
        changed_keys=sorted(payload),
        actor_type="system",
        reason=AUDIT_REASON,
        job_id=f"{EVENT_TYPE}:{payload['run_id']}",
        idempotency_key=f"audit:{SUBJECT_TYPE}:{SUBJECT_ID}:{EVENT_TYPE}:{payload['run_id']}",
    )
    db.add(event)
    db.flush()
    return event


def latest_results(db: Session, *, limit: int = 1) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.event_type == EVENT_TYPE, Event.subject_type == SUBJECT_TYPE)
        .order_by(Event.seq.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


# ================================================================================ entry points
def resolve_posture(value: str | None) -> str:
    """`auto` (or empty) is the platform's posture; anything else is normalised the way
    `PLATFORM_POSTURE` itself is (an unrecognised value fails closed to `commercial`)."""
    if value is None or value.strip().lower() in ("", "auto"):
        return platform_posture()
    return normalise_posture(value)


def run_audit(
    session_factory: sessionmaker[Session],
    *,
    posture: str | None = "auto",
    now: dt.datetime | None = None,
    persist: bool = True,
    served_sample: int = SERVED_SAMPLE,
    register_path: Path | None = None,
) -> dict[str, Any]:
    """Both passes, then persistence. Three sessions, not one: the store pass reads, the served
    pass runs request-scoped sessions of its own (a 404 rolls one back, and in the SQLite test
    target every session shares one connection), and the persist step writes."""
    effective = resolve_posture(posture)
    with session_scope(session_factory) as db:
        result = audit_store(db, posture=effective, now=now, register_path=register_path)
        candidates = _hidden_candidates(db, result["gated_sources"], limit=served_sample)
    served_pass(session_factory, result, candidates, sample=served_sample)
    if persist:
        with session_scope(session_factory) as db:
            event = persist_result(db, result)
            result["event_id"] = public_id("evt", event.id)
    result.pop("_breaches", None)
    if result["m11"] > 0:
        logger.error(
            "M-11 breach: %s gated or restricted rows reachable on a non-admin surface (S1, docs/04 S-9)",
            result["m11"],
            extra={"m11": result["m11"], "run_id": result["run_id"]},
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.visibility_audit.run", description="Nightly M-11 visibility audit."
    )
    parser.add_argument(
        "--posture",
        default="auto",
        help=f"auto (PLATFORM_POSTURE, the default) or one of {', '.join(PLATFORM_POSTURES)}",
    )
    parser.add_argument("--no-persist", action="store_true", help="do not write the audit event")
    parser.add_argument("--sample", type=int, default=SERVED_SAMPLE, help="served-pass sample size")
    parser.add_argument(
        "--json", action="store_true", help="print the full result as JSON (default: summary)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    factory = get_sessionmaker(get_engine(os.environ.get("DATABASE_URL")))
    result = run_audit(factory, posture=args.posture, persist=not args.no_persist, served_sample=args.sample)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(json.dumps(summarise(result), indent=2, default=str) + "\n")
    return 1 if result["m11"] > 0 else 0


def summarise(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result without its row lists — what the job logs and the admin list returns."""
    return {
        key: value
        for key, value in result.items()
        if key not in ("breaches", "gated_sources", "served") and not key.startswith("_")
    } | {"served": {k: v for k, v in result["served"].items() if k != "checks"}}


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


if __name__ == "__main__":  # pragma: no cover - exercised through `main()` in tests
    sys.exit(main())
