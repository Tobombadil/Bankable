"""Apply the resolver's clusters to the store, reversibly (docs/21-data-model.md §6.3, §6.4;
docs/22-entity-resolution-and-change-detection.md §4, §6, §7, §13).

Scope: this module mutates an already-populated `services.db` store. It does not fetch, parse,
or ingest anything -- `services/resolve/report.py` is the driver that loads real data (via the
existing `services.ingest.loader`, unmodified), calls `pipeline.resolve.run()` (unmodified), and
then calls the functions here.

Three things happen at the confidence gate (task step 2), and only these three:

1. A cluster whose minimum pairwise score is >= `MERGE_SCORE_THRESHOLD` (docs/22 §6's chosen
   threshold, 75) *and* that does not trip `id_reuse_conflict` is merged: `merge_proposal` is
   called once per absorbed member, each call writing one `merged` event and never deleting a row
   (docs/21 §6.3 invariant M1).
2. Anything below the gate is filed as a `resolution_decision` row per candidate pair
   (`status="proposed"`), for human review -- never auto-applied (task step 2's "match row ...
   for human review", adapted to the right table; see `services/resolve/models.py`'s docstring
   for why `resolution_decision` and not `docs/21` §3.11 `match`, which is proposal<->opportunity
   only).
3. Nothing is ever deleted. `unmerge` reverses a `merged` event exactly, from that event's own
   `before` payload alone (docs/21 invariant M1), without reading any other row.

## The id-reuse guard, and why it is not the pair's literal wording

The task instructs a guard against "two records from the same source with different queue ids
... the ISO-NE id-reuse failure case." Read literally (same source, *differing* queue ids inside
one cluster), that rule refuses 73 of the 281 loadable multi-member clusters measured on the
2026-09-12 data (`services/resolve/README.md`) -- nearly all of them the *legitimate*
multi-queue-position complexes `docs/22` §7.4 already documents (one EIA plant, several distinct,
real ERCOT/CAISO interconnection requests for co-located units). The actual bug measured in
`docs/22` §5/§7.1 ("within-source duplicate pairs": 85 ISO-NE, 2 NYISO; the `isone:84` /
`isone:84#5` false positive) is the opposite shape: the **same** queue id, from the **same**
source, shared by two records the resolver should have kept apart. `id_reuse_conflict` below
implements that second reading -- it is the one that actually catches the measured bug (fires on
exactly the 2 NYISO clusters that exhibit it) without gutting recall on legitimate complexes. This
is a deliberate, documented departure from the pair's literal wording, made from the measured
counts, not a misreading left uncorrected (`docs/22` §13 records both numbers).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from services.db.models import Event, Organization, OrganizationAlias, Proposal, ProposalSource
from services.resolve.models import ResolutionDecision

#: docs/22 §6's chosen threshold (precision 0.927 / recall 0.950 sample; 0.915/0.946 weighted; n=85).
MERGE_SCORE_THRESHOLD = 75.0

_UUID_COLUMNS = frozenset({"id", "sponsor_org_id", "location_id", "merged_into_id", "issuer_org_id"})
_DATETIME_COLUMNS = frozenset(
    {"first_seen", "last_changed", "published_at", "public_at", "created_at", "updated_at"}
)
_DATE_COLUMNS = frozenset({"proposed_online_date"})


# ------------------------------------------------------------------------------- (de)serialisation
def _json_safe(value: Any) -> Any:
    """Make a mapped-column value safe to store in a `jsonb`/`JSON` event payload."""
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def serialize_row(obj: Proposal | Organization) -> dict[str, Any]:
    """Every mapped column of `obj`, JSON-safe. Used to snapshot a full row into an event's
    `before` payload so `unmerge` can restore it exactly without reading any other row
    (docs/21 §6.3 invariant M1)."""
    mapper = type(obj).__mapper__
    return {c.key: _json_safe(getattr(obj, c.key)) for c in mapper.columns}


def restore_row(obj: Proposal | Organization, data: dict[str, Any]) -> None:
    """Inverse of `serialize_row`: write every field back except the primary key."""
    for key, value in data.items():
        if key == "id":
            continue
        if value is not None and key in _UUID_COLUMNS:
            value = _uuid.UUID(value)
        elif value is not None and key in _DATETIME_COLUMNS:
            value = dt.datetime.fromisoformat(value)
        elif value is not None and key in _DATE_COLUMNS:
            value = dt.date.fromisoformat(value)
        setattr(obj, key, value)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------------------------- the cluster
@dataclass(frozen=True)
class ClusterMember:
    """One resolver-cluster member already loaded into the store, with just the fields the gate
    and the canonical-choice rule need."""

    proposal_id: _uuid.UUID
    source_id: str  # `source.id` (the store/registry id, e.g. "us.iso.caiso.gen_queue")
    queue_id: str | None
    has_eia_id: bool
    retrieved_at: dt.datetime


@dataclass(frozen=True)
class ClusterEdge:
    left_proposal_id: _uuid.UUID
    right_proposal_id: _uuid.UUID
    score: float
    rationale: str


Action = Literal["merged", "proposed", "skipped_singleton"]


@dataclass
class ClusterApplication:
    cluster_key: str
    action: Action
    members_merged: int
    merge_events: list[Event] = field(default_factory=list)
    decisions: list[ResolutionDecision] = field(default_factory=list)
    gate_reason: str = ""


# ---------------------------------------------------------------------------------- the confidence gate
def id_reuse_conflict(members: Sequence[ClusterMember]) -> tuple[bool, str | None]:
    """True when one source's queue id is shared, inside this cluster, by more than one distinct
    store record -- the measured ISO-NE/NYISO id-reuse signature (see the module docstring)."""
    by_key: dict[tuple[str, str], set[_uuid.UUID]] = {}
    for m in members:
        if not m.queue_id:
            continue
        by_key.setdefault((m.source_id, m.queue_id), set()).add(m.proposal_id)
    for (source_id, queue_id), proposal_ids in by_key.items():
        if len(proposal_ids) > 1:
            return True, f"{source_id}: queue id {queue_id!r} shared by {len(proposal_ids)} distinct records"
    return False, None


def gate_cluster(
    members: Sequence[ClusterMember], min_score: float, *, threshold: float = MERGE_SCORE_THRESHOLD
) -> tuple[bool, str]:
    """The confidence gate (task step 2): both conditions must hold for a cluster to be merged."""
    if len(members) < 2:
        return False, "singleton after filtering to loaded records"
    if min_score < threshold:
        return False, f"min pairwise score {min_score:.1f} below threshold {threshold:.0f}"
    conflict, detail = id_reuse_conflict(members)
    if conflict:
        return False, f"id-reuse guard: {detail}"
    return True, "min pairwise score >= threshold and no id-reuse conflict"


def choose_canonical(members: Sequence[ClusterMember]) -> ClusterMember:
    """Canonical-record rule (task step 1, docs/22 §13): prefer a member carrying an EIA id; else
    the most recently retrieved; else the lowest source id, then lowest... there is no further
    tiebreak field on `ClusterMember`, so ties beyond this are broken by `proposal_id` for a
    total, deterministic order (uuid7 is time-ordered, so this also prefers the earlier-created
    row among true ties)."""
    if not members:
        raise ValueError("cannot choose a canonical record from an empty cluster")
    return min(
        members,
        key=lambda m: (
            0 if m.has_eia_id else 1,
            -m.retrieved_at.timestamp(),
            m.source_id,
            str(m.proposal_id),
        ),
    )


# --------------------------------------------------------------------------------------- proposal merge
def merge_proposal(
    session: Session,
    *,
    canonical: Proposal,
    absorbed: Proposal,
    score: float,
    rationale: str,
    actor_type: str = "model",
) -> Event:
    """Merge `absorbed` into `canonical` (docs/21 §6.3). Idempotent: calling this twice for the
    same pair returns the existing `merged` event rather than writing a second one."""
    if absorbed.id == canonical.id:
        raise ValueError("cannot merge a proposal into itself")
    idem = f"merge:proposal:{canonical.id}:{absorbed.id}"
    existing = session.scalar(select(Event).where(Event.idempotency_key == idem))
    if existing is not None:
        return existing
    if absorbed.merged_into_id is not None and absorbed.merged_into_id != canonical.id:
        raise ValueError(
            f"{absorbed.id} is already merged into {absorbed.merged_into_id}, not {canonical.id}"
        )

    absorbed_sources = session.scalars(
        select(ProposalSource).where(
            ProposalSource.proposal_id == absorbed.id, ProposalSource.active.is_(True)
        )
    ).all()

    before_payload = {
        "surviving": {
            "source_count": canonical.source_count,
            "resolution_confidence": canonical.resolution_confidence,
            "last_changed": _json_safe(canonical.last_changed),
        },
        "absorbed": {
            "id": str(absorbed.id),
            "public_id": absorbed.public_id,
            "slug": absorbed.slug,
            "entity": serialize_row(absorbed),
            "proposal_source_ids": [str(s.id) for s in absorbed_sources],
            "match_ids": [],
            "document_ids": [],
        },
    }

    for s in absorbed_sources:
        s.proposal_id = canonical.id

    canonical.source_count = canonical.source_count + len(absorbed_sources)
    confidence = round(score / 100.0, 3)
    if canonical.resolution_confidence is None or confidence < canonical.resolution_confidence:
        canonical.resolution_confidence = confidence
    canonical.last_changed = utcnow()

    absorbed.merged_into_id = canonical.id
    absorbed.publish_state = "unpublished"

    after_payload = {
        "surviving": {
            "source_count": canonical.source_count,
            "resolution_confidence": canonical.resolution_confidence,
            "last_changed": _json_safe(canonical.last_changed),
        }
    }

    event = Event(
        subject_type="proposal",
        subject_id=canonical.id,
        event_type="merged",
        observed_at=utcnow(),
        before=before_payload,
        after=after_payload,
        changed_keys=["source_count", "resolution_confidence"],
        actor_type=actor_type,
        confidence=confidence,
        reason=rationale,
        idempotency_key=idem,
    )
    session.add(event)
    session.flush()
    return event


def unmerge_proposal(session: Session, merge_event_id: _uuid.UUID, *, reason: str = "unmerge") -> Event:
    """Reverse a `merged` proposal event exactly, from its own `before` payload alone (docs/21
    §6.3 invariant M1: "every merged event must contain enough state to execute this without
    reading any other row"). Idempotent: a second call for the same merge event returns the
    existing `unmerged` event."""
    merge_event = session.get(Event, merge_event_id)
    if merge_event is None or merge_event.event_type != "merged" or merge_event.subject_type != "proposal":
        raise ValueError(f"{merge_event_id} is not a proposal `merged` event")

    already = session.scalar(select(Event).where(Event.reverses_event_id == merge_event.id))
    if already is not None:
        return already

    before = merge_event.before
    if before is None:
        raise ValueError(f"merge event {merge_event.id} carries no `before` payload; cannot unmerge")

    canonical = session.get(Proposal, merge_event.subject_id)
    absorbed_id = _uuid.UUID(before["absorbed"]["id"])
    absorbed = session.get(Proposal, absorbed_id)
    if canonical is None or absorbed is None:
        raise ValueError(
            f"cannot unmerge {merge_event.id}: canonical ({merge_event.subject_id}) or absorbed "
            f"({absorbed_id}) proposal row is missing"
        )

    restore_row(absorbed, before["absorbed"]["entity"])

    for source_id_str in before["absorbed"]["proposal_source_ids"]:
        ps = session.get(ProposalSource, _uuid.UUID(source_id_str))
        if ps is not None:
            ps.proposal_id = absorbed.id

    surviving = before["surviving"]
    canonical.source_count = surviving["source_count"]
    canonical.resolution_confidence = surviving["resolution_confidence"]
    canonical.last_changed = dt.datetime.fromisoformat(surviving["last_changed"])

    event = Event(
        subject_type="proposal",
        subject_id=canonical.id,
        event_type="unmerged",
        observed_at=utcnow(),
        before=None,
        after={"restored_proposal_id": str(absorbed.id)},
        changed_keys=["merged_into_id"],
        actor_type="user",
        reason=reason,
        reverses_event_id=merge_event.id,
        idempotency_key=f"unmerge:{merge_event.id}",
    )
    session.add(event)
    session.flush()
    return event


# -------------------------------------------------------------------------------------- apply a cluster
def apply_cluster(
    session: Session,
    members: Sequence[ClusterMember],
    edges: Sequence[ClusterEdge],
    *,
    cluster_key: str,
    threshold: float = MERGE_SCORE_THRESHOLD,
) -> ClusterApplication:
    """Apply the confidence gate to one resolver cluster (task steps 1-2): merge if it passes,
    else file every pairwise edge as a `resolution_decision` for human review."""
    if len(members) < 2:
        return ClusterApplication(cluster_key, "skipped_singleton", 0, gate_reason="singleton")

    if not edges:
        # The resolver's union-find connected these members only transitively, through a node
        # this store never loaded (an EIA plant-rollup synthetic record, or a gated SPP/ISO-NE
        # record) -- there is no direct, measured similarity between any two of them. Refuse
        # rather than trust the externally computed cluster_id (the same "both must hold"
        # independent-recheck pattern services/ingest/loader.py already uses for the licence
        # gate): a shared bridge is not evidence the bridged records are the same project.
        reason = "no direct edge among loaded members (linked only via an excluded/rollup node)"
        return ClusterApplication(cluster_key, "proposed", 0, gate_reason=reason)

    min_score = min(e.score for e in edges)
    allowed, reason = gate_cluster(members, min_score, threshold=threshold)

    if not allowed:
        decisions = [_propose_decision(session, e, cluster_key=cluster_key, reason=reason) for e in edges]
        return ClusterApplication(cluster_key, "proposed", 0, decisions=decisions, gate_reason=reason)

    canonical_member = choose_canonical(members)
    canonical_row = session.get(Proposal, canonical_member.proposal_id)
    assert canonical_row is not None  # noqa: S101 -- loaded by the caller; a store bug otherwise

    events: list[Event] = []
    for m in members:
        if m.proposal_id == canonical_member.proposal_id:
            continue
        absorbed_row = session.get(Proposal, m.proposal_id)
        assert absorbed_row is not None  # noqa: S101
        if absorbed_row.merged_into_id is not None:
            continue  # already absorbed in an earlier cluster or a previous run of this report
        rationale = (
            f"resolver cluster {cluster_key}: min pairwise score {min_score:.1f}/100 "
            f"(threshold {threshold:.0f})"
        )
        ev = merge_proposal(
            session, canonical=canonical_row, absorbed=absorbed_row, score=min_score, rationale=rationale
        )
        events.append(ev)
    return ClusterApplication(cluster_key, "merged", len(events), merge_events=events, gate_reason=reason)


def _propose_decision(
    session: Session, edge: ClusterEdge, *, cluster_key: str, reason: str
) -> ResolutionDecision:
    left, right = sorted((edge.left_proposal_id, edge.right_proposal_id), key=str)
    existing = session.scalar(
        select(ResolutionDecision).where(
            ResolutionDecision.left_proposal_id == left, ResolutionDecision.right_proposal_id == right
        )
    )
    if existing is not None:
        return existing
    decision = ResolutionDecision(
        left_proposal_id=left,
        right_proposal_id=right,
        cluster_key=cluster_key,
        score=round(edge.score, 2),
        rationale=edge.rationale,
        gate_reason=reason,
        status="proposed",
    )
    session.add(decision)
    session.flush()
    return decision


# ----------------------------------------------------------------------------------- organization merge
def merge_organization(
    session: Session,
    *,
    canonical: Organization,
    absorbed: Organization,
    rationale: str,
    actor_type: str = "pipeline",
) -> Event:
    """Merge `absorbed` organization into `canonical`, writing an `alias_added` row for the
    absorbed spelling and one `merged` event (mirrors `merge_proposal`; docs/21 §6.3, applied to
    `organization` per its own `merged_into_id` column, docs/21 §3.5)."""
    if absorbed.id == canonical.id:
        raise ValueError("cannot merge an organization into itself")
    idem = f"merge:organization:{canonical.id}:{absorbed.id}"
    existing = session.scalar(select(Event).where(Event.idempotency_key == idem))
    if existing is not None:
        return existing
    if absorbed.merged_into_id is not None and absorbed.merged_into_id != canonical.id:
        raise ValueError(
            f"{absorbed.id} is already merged into {absorbed.merged_into_id}, not {canonical.id}"
        )

    sponsored = session.scalars(select(Proposal).where(Proposal.sponsor_org_id == absorbed.id)).all()
    absorbed_source_id: str | None = None
    absorbed_source_url = ""
    absorbed_retrieved_at = utcnow()
    absorbed_licence_id: str | None = None
    if sponsored:
        link = session.scalar(
            select(ProposalSource).where(
                ProposalSource.proposal_id == sponsored[0].id, ProposalSource.active.is_(True)
            )
        )
        if link is not None:
            absorbed_source_id = link.source_id
            absorbed_source_url = link.source_url
            absorbed_retrieved_at = link.retrieved_at
            absorbed_licence_id = link.licence_id

    before_payload = {
        "surviving": {"last_changed": _json_safe(canonical.last_changed)},
        "absorbed": {
            "id": str(absorbed.id),
            "public_id": absorbed.public_id,
            "slug": absorbed.slug,
            "entity": serialize_row(absorbed),
            "sponsored_proposal_ids": [str(p.id) for p in sponsored],
        },
    }

    for p in sponsored:
        p.sponsor_org_id = canonical.id

    if absorbed_source_id and absorbed_licence_id:
        alias = OrganizationAlias(
            organization_id=canonical.id,
            alias=absorbed.name_canonical,
            alias_normalised=absorbed.name_normalised,
            kind="filing_spelling",
            source_id=absorbed_source_id,
            source_url=absorbed_source_url,
            retrieved_at=absorbed_retrieved_at,
            licence_id=absorbed_licence_id,
            confidence=1.0,
            created_by="pipeline",
        )
        session.add(alias)

    canonical.last_changed = utcnow()
    absorbed.merged_into_id = canonical.id

    event = Event(
        subject_type="organization",
        subject_id=canonical.id,
        event_type="merged",
        observed_at=utcnow(),
        before=before_payload,
        after={"surviving": {"last_changed": _json_safe(canonical.last_changed)}},
        changed_keys=["last_changed"],
        actor_type=actor_type,
        confidence=1.0,
        reason=rationale,
        idempotency_key=idem,
    )
    session.add(event)
    session.flush()
    return event


def unmerge_organization(session: Session, merge_event_id: _uuid.UUID, *, reason: str = "unmerge") -> Event:
    """Organization counterpart of `unmerge_proposal`, same exact-restore contract."""
    merge_event = session.get(Event, merge_event_id)
    if (
        merge_event is None
        or merge_event.event_type != "merged"
        or merge_event.subject_type != "organization"
    ):
        raise ValueError(f"{merge_event_id} is not an organization `merged` event")

    already = session.scalar(select(Event).where(Event.reverses_event_id == merge_event.id))
    if already is not None:
        return already

    before = merge_event.before
    if before is None:
        raise ValueError(f"merge event {merge_event.id} carries no `before` payload; cannot unmerge")

    canonical = session.get(Organization, merge_event.subject_id)
    absorbed_id = _uuid.UUID(before["absorbed"]["id"])
    absorbed = session.get(Organization, absorbed_id)
    if canonical is None or absorbed is None:
        raise ValueError(
            f"cannot unmerge {merge_event.id}: canonical or absorbed organization row is missing"
        )

    restore_row(absorbed, before["absorbed"]["entity"])

    for proposal_id_str in before["absorbed"]["sponsored_proposal_ids"]:
        p = session.get(Proposal, _uuid.UUID(proposal_id_str))
        if p is not None:
            p.sponsor_org_id = absorbed.id

    canonical.last_changed = dt.datetime.fromisoformat(before["surviving"]["last_changed"])

    event = Event(
        subject_type="organization",
        subject_id=canonical.id,
        event_type="unmerged",
        observed_at=utcnow(),
        before=None,
        after={"restored_organization_id": str(absorbed.id)},
        changed_keys=["merged_into_id"],
        actor_type="user",
        reason=reason,
        reverses_event_id=merge_event.id,
        idempotency_key=f"unmerge:{merge_event.id}",
    )
    session.add(event)
    session.flush()
    return event


@dataclass
class OrganizationResolutionReport:
    groups_considered: int = 0
    groups_merged: int = 0
    organizations_absorbed: int = 0
    merge_events: list[Event] = field(default_factory=list)


def resolve_organizations(session: Session, norm_org_fn: Any = None) -> OrganizationResolutionReport:
    """Conservative organization resolution (task step 3): group live organizations by
    `pipeline.normalize.org_key(name_canonical)` -- the one organisation key every consumer uses
    (the ownership loader, the midstream operator edges, the proposal loader) -- and merge every
    group of size > 1. This is a deterministic-key match, not a fuzzy score, and is deliberately
    not the same "score >= 75" gate the proposal clusters use: `docs/22 §13` explains why a fuzzy
    pass over the ~3,000 sponsor organizations in this dataset is deferred rather than run without
    a labelled sample to measure it against. The rule fires with confidence 1.0, the same
    convention `pipeline/resolve.py`'s deterministic (D-prefixed) passes use.

    Since 2026-09-19 that key strips legal forms only. It previously stripped industry and
    geography words as well, which merged organizations that are not one legal entity: measured
    over the 7,642 organisation strings in the dev store and EIA-860 Schedule 4, 258 of the 557
    pairs it merged were a different company or a parent/affiliate, and this function applied
    those merges to the store as `merged` events (reversible, but wrong). docs/22 §16 has the
    census; same-company spellings the narrower key cannot reach belong in `organization_alias`
    (`data/vendored/organizations/aliases.yaml`), not in a looser key.

    `norm_org_fn` stays accepted so an existing caller or test can inject a key; omit it and the
    canonical `org_key` is used."""
    key_fn = norm_org_fn if norm_org_fn is not None else org_key
    orgs = session.scalars(select(Organization).where(Organization.merged_into_id.is_(None))).all()
    groups: dict[str, list[Organization]] = {}
    for org in orgs:
        key = key_fn(org.name_canonical) or org.name_normalised
        groups.setdefault(key, []).append(org)

    report = OrganizationResolutionReport()
    for key, members in groups.items():
        if len(members) < 2:
            continue
        report.groups_considered += 1
        canonical = _choose_canonical_organization(session, members)
        for org in members:
            if org.id == canonical.id:
                continue
            ev = merge_organization(
                session,
                canonical=canonical,
                absorbed=org,
                rationale=f"normalised name match {key!r} (deterministic, confidence 1.0)",
            )
            report.merge_events.append(ev)
            report.organizations_absorbed += 1
        report.groups_merged += 1
    return report


def _choose_canonical_organization(session: Session, members: Sequence[Organization]) -> Organization:
    """Canonical org = most proposals sponsored (the most-attested spelling); ties broken by
    earliest `first_seen`, then shortest `name_canonical`, then `id` for a total order."""

    def sponsor_count(org: Organization) -> int:
        return (
            session.scalar(
                select(sa.func.count()).select_from(Proposal).where(Proposal.sponsor_org_id == org.id)
            )
            or 0
        )

    return min(
        members,
        key=lambda o: (
            -sponsor_count(o),
            o.first_seen,
            len(o.name_canonical),
            str(o.id),
        ),
    )
