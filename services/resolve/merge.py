"""Apply the resolver's clusters to the store, reversibly (docs/21-data-model.md §6.3, §6.4;
docs/22-entity-resolution-and-change-detection.md §4, §6, §7, §13).

Scope: this module mutates an already-populated `services.db` store. It does not fetch, parse,
or ingest anything -- `services/resolve/report.py` is the driver that loads real data (via the
existing `services.ingest.loader`, unmodified), calls `pipeline.resolve.run()` (unmodified), and
then calls the functions here.

Three things happen at the confidence gate (task step 2), and only these three:

1. A cluster whose minimum pairwise score is >= `MERGE_SCORE_THRESHOLD` (docs/22 §6's chosen
   threshold, 75) *and* that trips neither `id_reuse_conflict` nor `coherence_conflict` (docs/22
   §22: one source's requests adding up to far more than the EIA plant) is merged: `merge_proposal` is
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
import logging
import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from pipeline.resolve import tech_families
from services.db.models import (
    AssetOwner,
    Event,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
)
from services.ingest.loader import SELECT_BASIS_KEY
from services.resolve import provenance, survivorship
from services.resolve.models import ResolutionDecision

logger = logging.getLogger(__name__)

#: docs/22 §6's chosen threshold. Re-measured with the docs/22 §22 rules on 2026-09-29: resolver
#: 0.974 / 0.925 sample (0.958 / 0.910 weighted, n=85); through the store 1.000 / 0.892 (77 usable).
MERGE_SCORE_THRESHOLD = 75.0

_UUID_COLUMNS = frozenset(
    {"id", "sponsor_org_id", "location_id", "merged_into_id", "issuer_org_id", "parent_org_id"}
)
_DATETIME_COLUMNS = frozenset(
    {"first_seen", "last_changed", "published_at", "public_at", "created_at", "updated_at"}
)
_DATE_COLUMNS = frozenset({"proposed_online_date", "parent_as_of"})


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


def _all_links(session: Session, proposal_id: _uuid.UUID) -> list[ProposalSource]:
    """Every link of a record, active or not: the evidence of a record whose links are all inactive."""
    return list(
        session.scalars(
            select(ProposalSource)
            .where(ProposalSource.proposal_id == proposal_id)
            .order_by(ProposalSource.id)
        ).all()
    )


def _required(quartet: provenance.Quartet | None, what: str) -> provenance.Quartet:
    """A resolver event must carry a provenance quartet (CLAUDE.md; docs/22 §13.7): an event with no
    stored record behind it is a store defect, refused rather than written unattributed."""
    if quartet is None:
        raise ValueError(f"{what}: no stored record carries a provenance quartet to attribute it to")
    return quartet


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
    # Read by the coherence check only; a member without them is never counted there.
    capacity_mw: float | None = None
    technology: str | None = None
    eia_plant_id: str | None = None


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


#: docs/22 §22 rule K. One source's requests in a cluster may add up to at most this multiple of
#: the cluster's EIA capacity in the same technology family. Measured on the 2026-09-29 dev store:
#: every sound cluster the check judges is at or below 1.36 (Gonzaga); above are 2.97 (three
#: withdrawn Rolling Upland re-filings), 5.61 (Cody Road re-filings) and 10.51 (Riverhead). 2.0
#: sits in that gap. In-sample; docs/22 §22.8 is the out-of-sample check.
COHERENCE_FACTOR = 2.0


def coherence_ratio(members: Sequence[ClusterMember]) -> tuple[float, str | None]:
    """The largest (one source's requests in one technology family) / (the cluster's EIA capacity
    in that family), over families holding two or more of one source's requests; 0.0 when nothing
    can be judged. `coherence_conflict` compares it with `COHERENCE_FACTOR`.

    A family with a single request is not judged here: one request against one plant is rule C's
    job (docs/22 §22.8, "K-v1"). Briggs files one solar and one storage request with ERCOT; its
    336 MW storage request against the plant's 70.5 MW storage generator is one request, compared
    pairwise with the whole 375.5 MW plant, and the cluster merges.

    Counted per family because ERCOT files a hybrid's solar and storage as two requests and EIA
    lists them as two generators (Harryoung: 190 + 190 against 190 + 190), and a family the EIA
    side does not hold is not judged (EIA-860M may list only the storage half of a plant whose
    solar already operates: Duffy). A hybrid request counts once, in whichever of its families
    holds the most EIA capacity (its MW is the point-of-interconnection total, not either half);
    a request of unknown technology counts in the EIA side's largest family. Two rows of one
    source with the same technology and MW count once (NYISO lists a project under its cluster id
    and its queue position: 'KCE NY 30' as C24-008 and 1448). A request without MW is not counted."""
    eia_cap: dict[str, float] = {}
    for m in members:
        if m.eia_plant_id and m.capacity_mw:
            for fam in tech_families(m.technology) or ():
                eia_cap[fam] = eia_cap.get(fam, 0.0) + float(m.capacity_mw)
    if not eia_cap:
        return 0.0, None
    dominant = max(sorted(eia_cap), key=lambda f: eia_cap[f])

    by_source: dict[str, dict[tuple[str | None, float], ClusterMember]] = {}
    for m in members:
        if m.eia_plant_id or not m.capacity_mw:
            continue
        by_source.setdefault(m.source_id, {})[(m.technology, round(float(m.capacity_mw), 1))] = m
    worst, detail = 0.0, None
    for source_id in sorted(by_source):
        requests = by_source[source_id]
        if len(requests) < 2:
            continue
        per_family: dict[str, float] = {}
        count: dict[str, int] = {}
        for m in requests.values():
            fams = tech_families(m.technology)
            present = [dominant] if fams is None else sorted(f for f in fams if f in eia_cap)
            if not present:
                continue
            target = max(present, key=lambda f: eia_cap[f])
            per_family[target] = per_family.get(target, 0.0) + float(m.capacity_mw or 0.0)
            count[target] = count.get(target, 0) + 1
        for fam in sorted(per_family):
            if count[fam] < 2:
                continue
            ratio = per_family[fam] / eia_cap[fam]
            if ratio > worst:
                worst = ratio
                detail = (
                    f"{source_id}: {count[fam]} requests total {per_family[fam]:.1f} MW {fam} "
                    f"against {eia_cap[fam]:.1f} MW in the EIA plant"
                )
    return worst, detail


def coherence_conflict(
    members: Sequence[ClusterMember], *, factor: float = COHERENCE_FACTOR
) -> tuple[bool, str | None]:
    """Whole-cluster check (docs/22 §22 rule K). Union-find accepts a cluster pair by pair, so a
    town name can chain several requests onto one small plant (Riverhead). A cluster that holds
    an EIA plant and two or more requests from one other source is refused when those requests,
    in one technology family, add up to more than `factor` times the cluster's EIA capacity in
    that family (`coherence_ratio`). It goes to review, not to merge."""
    ratio, detail = coherence_ratio(members)
    if ratio > factor:
        return True, f"{detail} (x{ratio:.2f}, limit x{factor:g})"
    return False, None


def gate_cluster(
    members: Sequence[ClusterMember], min_score: float, *, threshold: float = MERGE_SCORE_THRESHOLD
) -> tuple[bool, str]:
    """The confidence gate (task step 2): every condition must hold for a cluster to be merged."""
    if len(members) < 2:
        return False, "singleton after filtering to loaded records"
    if min_score < threshold:
        return False, f"min pairwise score {min_score:.1f} below threshold {threshold:.0f}"
    conflict, detail = id_reuse_conflict(members)
    if conflict:
        return False, f"id-reuse guard: {detail}"
    incoherent, detail = coherence_conflict(members)
    if incoherent:
        return False, f"coherence check: {detail}"
    return True, "min pairwise score >= threshold, no id-reuse conflict, coherent capacity"


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
#: Key under the merge event's `after.surviving` for the `select_basis` entries the merge copied onto
#: the survivor, `{source_id: basis}`. Unmerge removes exactly these source ids (lane I3).
SELECT_BASIS_ADDED_KEY = f"{SELECT_BASIS_KEY}_added"


def _select_basis(proposal: Proposal) -> dict[str, Any]:
    return dict((proposal.identifiers or {}).get(SELECT_BASIS_KEY) or {})


def _carry_select_basis(canonical: Proposal, absorbed: Proposal) -> dict[str, Any]:
    """Copy the absorbed record's `identifiers.select_basis` entries onto the survivor, a union by
    source id in which the survivor's own entry wins, and return the entries added.

    `select_basis` is `{source_id: basis}`, written per source by the loader (lane H6; docs/25 §3.3,
    §3.7): why a selecting connector counts the site as a data centre. Each entry belongs to one
    source's link, and a merge moves the absorbed record's links to the survivor, so their reasons
    move with them. Without this the survivor showed only its own reason until the absorbed
    record's source loaded again. After the merge, the loader's `_keep_other_sources_basis` keeps
    other sources' entries on every load and replaces the loading source's own, so a carried entry
    is then maintained by its source like any other. Only `select_basis` is carried: the absorbed
    row's other identifiers stay on its own (unpublished) row."""
    mine = _select_basis(canonical)
    added = {sid: basis for sid, basis in _select_basis(absorbed).items() if sid not in mine}
    if added:
        # Reassigned, not mutated, so the JSON column is marked dirty.
        canonical.identifiers = {**(canonical.identifiers or {}), SELECT_BASIS_KEY: {**mine, **added}}
    return added


def _drop_select_basis(canonical: Proposal, source_ids: Sequence[str]) -> bool:
    """Remove `source_ids` from the survivor's `select_basis` (the key itself when it empties).
    Returns whether anything changed."""
    current = _select_basis(canonical)
    kept = {sid: basis for sid, basis in current.items() if sid not in set(source_ids)}
    if kept == current:
        return False
    identifiers = dict(canonical.identifiers or {})
    if kept:
        identifiers[SELECT_BASIS_KEY] = kept
    else:
        identifiers.pop(SELECT_BASIS_KEY, None)
    canonical.identifiers = identifiers
    return True


#: Key under the merge event's `before.surviving` for the survivor's surviving fields and their
#: provenance before the merge (`survivorship.snapshot`), and under `after.surviving` for the fields
#: the merge's survivorship pass changed, `{field: new value}` (docs/22 §23).
SURVIVORSHIP_KEY = "survivorship"

#: Keys under the merge event's `before.surviving` / `after.surviving` for the admin overrides the
#: merge carried from the absorbed record onto the survivor (2026-10-06, L-1). `before` holds the
#: survivor's own value of each such column, so unmerge restores it exactly.
OVERRIDES_CARRIED_KEY = "overrides_carried"
#: An override key that names something other than a same-named column (`admin_records.py`).
_OVERRIDE_COLUMNS = {"location": "location_id"}


def _override_column(key: str) -> str | None:
    column = _OVERRIDE_COLUMNS.get(key, key)
    return column if column in Proposal.__mapper__.columns else None


def _carry_overrides(canonical: Proposal, absorbed: Proposal) -> dict[str, Any]:
    """Pin the absorbed record's admin overrides onto the survivor where the survivor has none of
    its own for that field, and return `{key: survivor's previous column value}` (JSON-safe).

    An override is a human decision about the real-world project both rows describe (docs/21
    §6.4); a correction or a privacy redaction made on the row that loses the merge must not be
    undone by the merge (L-1 of the 2026-09-30 legal audit: "a deletion that a later crawl silently
    undoes is not a deletion", docs/13 §5.4 rule 6). The survivor's own overrides win. The column
    takes the absorbed row's value, which is the override's value."""
    mine = dict(canonical.overrides or {})
    carried: dict[str, Any] = {}
    for key, entry in (absorbed.overrides or {}).items():
        column = _override_column(key)
        if key in mine or column is None:
            continue
        carried[key] = _json_safe(getattr(canonical, column))
        setattr(canonical, column, getattr(absorbed, column))
        mine[key] = entry
    if carried:
        canonical.overrides = mine  # reassigned so the JSON column is marked dirty
    return carried


def _uncarry_overrides(canonical: Proposal, carried: dict[str, Any]) -> bool:
    """Undo `_carry_overrides` from the merge event's own payload: drop each carried key from the
    survivor's overrides and restore its column."""
    if not carried:
        return False
    overrides = dict(canonical.overrides or {})
    for key, before in carried.items():
        column = _override_column(key)
        overrides.pop(key, None)
        if column is not None:
            restore_row(canonical, {column: before})
    canonical.overrides = overrides
    return True


#: Key under the merge event's `before.absorbed`: `{proposal_source id: its link_event_id before
#: this merge stamped it}` (str or null) for every link the merge re-pointed (docs/21 §6.3). Its
#: presence marks a merge written since 2026-10-07, which stamps `link_event_id`; unmerge restores
#: each value. A merge written before carries none, and its links were never stamped.
LINK_EVENT_PRIORS_KEY = "proposal_source_link_event_ids"


def _optional_uuid(value: Any) -> _uuid.UUID | None:
    return None if value is None else _uuid.UUID(str(value))


def _attribution_chain(session: Session, link: ProposalSource) -> list[_uuid.UUID | None]:
    """`link.link_event_id`, then the value each stamping merge recorded it replaced, back to the
    first value no merge recorded: `[latest, ..., base]`.

    A merge carries the links it re-points onward, including links an earlier merge had brought to
    the absorbed record (C into B, then B into A: C's links carry the B-into-A event, which recorded
    the C-into-B event as their previous value). The chain is how an unmerge finds a link it moved
    that a later merge has since carried, and what value to give back to a link moved by a merge
    written before stamping existed."""
    chain: list[_uuid.UUID | None] = [link.link_event_id]
    seen: set[_uuid.UUID] = set()
    current = link.link_event_id
    while current is not None and current not in seen:
        seen.add(current)
        event = session.get(Event, current)
        if event is None or event.event_type != "merged" or event.subject_type != "proposal":
            break
        priors = ((event.before or {}).get("absorbed") or {}).get(LINK_EVENT_PRIORS_KEY)
        if not isinstance(priors, dict) or str(link.id) not in priors:
            break
        current = _optional_uuid(priors[str(link.id)])
        chain.append(current)
    return chain


def links_moved_by(session: Session, merge_event: Event) -> list[tuple[ProposalSource, _uuid.UUID | None]]:
    """The links a proposal `merged` event moved that are still its to move back, each with the
    `link_event_id` it had before that merge (docs/21 §6.3).

    A merge written since 2026-10-07 stamped every link it moved with its own id: those links are
    exactly the ones that still carry it, plus any a later merge has carried onward (the chain
    passes through this merge). A listed link whose chain no longer reaches this merge has already
    been taken back by another unmerge and is left where it is. A merge written before stamping
    existed is identified, as it always was, by its listed `proposal_source_ids`; each goes back
    with the value it had when that merge ran (the end of its chain: such a merge changed nothing)."""
    absorbed = (merge_event.before or {}).get("absorbed") or {}
    listed = [
        link
        for link in (
            session.get(ProposalSource, _uuid.UUID(str(x))) for x in absorbed.get("proposal_source_ids") or []
        )
        if link is not None
    ]
    priors = absorbed.get(LINK_EVENT_PRIORS_KEY)
    if not isinstance(priors, dict):
        return [(link, _attribution_chain(session, link)[-1]) for link in listed]

    # A link this merge stamped sits on its survivor until a later merge carries it on (and restamps
    # it) or an unmerge returns it; the survivor filter keeps the lookup on `proposal_id`'s index.
    moved: dict[_uuid.UUID, ProposalSource] = {
        link.id: link
        for link in session.scalars(
            select(ProposalSource)
            .where(
                ProposalSource.proposal_id == merge_event.subject_id,
                ProposalSource.link_event_id == merge_event.id,
            )
            .order_by(ProposalSource.id)
        )
    }
    for link in listed:
        if link.id not in moved and merge_event.id in _attribution_chain(session, link):
            moved[link.id] = link
    return [(link, _optional_uuid(priors.get(str(link.id)))) for link in moved.values()]


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
    # The provenance quartet: the most restrictive member link's, read before the links move
    # (docs/22 §13.7, `services/resolve/provenance.py`).
    quartet = _required(
        provenance.for_proposal_merge(
            session,
            canonical.id,
            absorbed_sources or _all_links(session, absorbed.id),
        ),
        f"merge of proposal {absorbed.id} into {canonical.id}",
    )

    before_payload = {
        "surviving": {
            "source_count": canonical.source_count,
            "resolution_confidence": canonical.resolution_confidence,
            "last_changed": _json_safe(canonical.last_changed),
            # The survivor's fields as they were, for an exact unmerge (docs/22 §23).
            SURVIVORSHIP_KEY: survivorship.snapshot(canonical),
        },
        "absorbed": {
            "id": str(absorbed.id),
            "public_id": absorbed.public_id,
            "slug": absorbed.slug,
            "entity": serialize_row(absorbed),
            "proposal_source_ids": [str(s.id) for s in absorbed_sources],
            LINK_EVENT_PRIORS_KEY: {str(s.id): _json_safe(s.link_event_id) for s in absorbed_sources},
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
    # An admin override of `identifiers` on the survivor pins the whole value (docs/21 §6.4).
    basis_added = (
        {} if "identifiers" in (canonical.overrides or {}) else _carry_select_basis(canonical, absorbed)
    )
    overrides_carried = _carry_overrides(canonical, absorbed)
    if overrides_carried:
        before_payload["surviving"][OVERRIDES_CARRIED_KEY] = overrides_carried
    # Field-level survivorship over every link the survivor now holds (docs/22 §23); overridden
    # fields, including the ones just carried, are skipped.
    session.flush()
    survived = survivorship.apply_survivorship(session, canonical, touch=False)

    after_payload: dict[str, Any] = {
        "surviving": {
            "source_count": canonical.source_count,
            "resolution_confidence": canonical.resolution_confidence,
            "last_changed": _json_safe(canonical.last_changed),
        }
    }
    changed_keys = ["source_count", "resolution_confidence"]
    if basis_added:
        after_payload["surviving"][SELECT_BASIS_ADDED_KEY] = basis_added
        changed_keys.append("identifiers")
    if overrides_carried:
        after_payload["surviving"][OVERRIDES_CARRIED_KEY] = sorted(overrides_carried)
        changed_keys.extend(k for k in sorted(overrides_carried) if k not in changed_keys)
    if survived.changes:
        after_payload["surviving"][SURVIVORSHIP_KEY] = {c.field: c.after for c in survived.changes}
        changed_keys.extend(c.field for c in survived.changes if c.field not in changed_keys)

    event = Event(
        subject_type="proposal",
        subject_id=canonical.id,
        event_type="merged",
        observed_at=utcnow(),
        **quartet.columns(),
        before=before_payload,
        after=after_payload,
        changed_keys=changed_keys,
        actor_type=actor_type,
        confidence=confidence,
        reason=rationale,
        idempotency_key=idem,
    )
    session.add(event)
    session.flush()
    # docs/21 §6.3: every re-pointed link names the merge that moved it. Stamped after the event row
    # exists, so the foreign key holds on every dialect; the previous values are in the payload.
    for s in absorbed_sources:
        s.link_event_id = event.id
    session.flush()
    return event


def unmerge_proposal(session: Session, merge_event_id: _uuid.UUID, *, reason: str = "unmerge") -> Event:
    """Reverse a `merged` proposal event exactly, from its own `before` payload alone (docs/21
    §6.3 invariant M1: "every merged event must contain enough state to execute this without
    reading any other row"). Idempotent: a second call for the same merge event returns the
    existing `unmerged` event.

    The links that move back are `links_moved_by`'s: those still stamped with this merge (or
    carried onward from it by a later merge), each given back the `link_event_id` it had before;
    for a merge written before stamping, the listed `proposal_source_ids`. A link another unmerge
    has already returned stays where it is (C into B, B into A, then C out: B out leaves C's links).

    The survivor's `select_basis` loses exactly the source ids the merge added
    (`after.surviving.select_basis_added`), whatever their value now is: those entries belong to
    the links this unmerge moves back, and a reload of that source since the merge may have
    changed the value, not the owner. The survivor's own entries and those of other merges stay.
    Known limit: when a later merge into the same survivor brought a record of the *same* source,
    its entry lost to the one already there and was not recorded, so it leaves with this unmerge
    and returns on that source's next load. Events written before this field existed carry none
    and remove nothing."""
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

    # The quartet of the merge being reversed (docs/21 §6.2); a merge written before docs/22 §13.7
    # carries none, and the same rule derives one from the links that move back.
    listed = [
        ps
        for ps in (
            session.get(ProposalSource, _uuid.UUID(x)) for x in before["absorbed"]["proposal_source_ids"]
        )
        if ps is not None
    ]
    quartet = provenance.of_event(merge_event) or _required(
        provenance.for_proposal_merge(session, canonical.id, listed or _all_links(session, absorbed.id)),
        f"unmerge {merge_event.id}",
    )

    moved = links_moved_by(session, merge_event)
    restore_row(absorbed, before["absorbed"]["entity"])

    for ps, previous_link_event_id in moved:
        ps.proposal_id = absorbed.id
        ps.link_event_id = previous_link_event_id

    surviving = before["surviving"]
    canonical.source_count = surviving["source_count"]
    canonical.resolution_confidence = surviving["resolution_confidence"]
    basis_added = ((merge_event.after or {}).get("surviving") or {}).get(SELECT_BASIS_ADDED_KEY) or {}
    changed_keys = ["merged_into_id"]
    if _drop_select_basis(canonical, list(basis_added)):
        changed_keys.append("identifiers")
    carried = surviving.get(OVERRIDES_CARRIED_KEY) or {}
    if _uncarry_overrides(canonical, carried):
        changed_keys.extend(k for k in sorted(carried) if k not in changed_keys)
    # The survivor's fields: back to what they were before this merge, then recomputed over the
    # links it still holds when that is more than one (docs/22 §23). A survivor left with its own
    # link only is that source's record again, exactly as the loader wrote it.
    if SURVIVORSHIP_KEY in surviving:
        survivorship.restore_snapshot(canonical, surviving[SURVIVORSHIP_KEY])
        changed_keys.extend(
            k
            for k in (merge_event.after or {}).get("surviving", {}).get(SURVIVORSHIP_KEY, {})
            if k not in changed_keys
        )
    session.flush()
    if len(survivorship.proposal_members(session, canonical.id)) > 1:
        survived = survivorship.apply_survivorship(session, canonical, touch=False)
        changed_keys.extend(c.field for c in survived.changes if c.field not in changed_keys)
    # The restore above put both rows' sync columns back to their pre-merge values; the unmerge is
    # itself a change, so both move forward or `updated_since` and bulk sync never see it (backend
    # audit 2026-09-30 F6).
    canonical.last_changed = absorbed.last_changed = canonical.updated_at = absorbed.updated_at = utcnow()

    event = Event(
        subject_type="proposal",
        subject_id=canonical.id,
        event_type="unmerged",
        observed_at=utcnow(),
        **quartet.columns(),
        before=None,
        after={"restored_proposal_id": str(absorbed.id)},
        changed_keys=changed_keys,
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
#: The parent-link columns of `organization`, moved and restored as one fact (the claim and its
#: provenance travel together; `services/api/orgtree.py`'s `OwnershipEdge` reads all four).
_PARENT_FIELDS: tuple[str, ...] = ("parent_org_id", "parent_source_id", "parent_as_of", "parent_share_pct")


def _parent_snapshot(org: Organization) -> dict[str, Any]:
    return {key: _json_safe(getattr(org, key)) for key in _PARENT_FIELDS}


def _restore_parent(org: Organization, data: dict[str, Any]) -> None:
    restore_row(org, {key: data.get(key) for key in _PARENT_FIELDS})


def merge_organization(
    session: Session,
    *,
    canonical: Organization,
    absorbed: Organization,
    rationale: str,
    actor_type: str = "pipeline",
    source_url: str | None = None,
    retrieved_at: dt.datetime | None = None,
    source_id: str | None = None,
    licence_id: str | None = None,
) -> Event:
    """Merge `absorbed` organization into `canonical` and write one `merged` event (mirrors
    `merge_proposal`; docs/21 §6.3, applied to `organization` per its own `merged_into_id` column,
    docs/21 §3.5). Idempotent on the pair.

    A merged row is a redirect, not a company (`services/api/orgtree.py::_children_of`), so every
    fact that hangs off the absorbed row by foreign key moves to the survivor, and the event lists
    each moved row's id so `unmerge_organization` can move exactly those rows back (invariant M1):

    * `proposal.sponsor_org_id` -> `before.absorbed.sponsored_proposal_ids`;
    * `asset_owner.organization_id` -> `asset_owner_ids` (docs/22 §20: before 2026-09-26 these were
      left on the redirect, so an edge-only organisation's assets vanished from every company page);
    * `organization.parent_org_id` of the absorbed row's children -> `child_organization_ids`
      (otherwise the children hang off a redirect the tree walk never enters);
    * `organization_alias.organization_id` -> `organization_alias_ids` (otherwise the spellings the
      absorbed row carried stop resolving: `org_key_multimap` reads aliases of live rows only).

    **Collisions.** An `asset_owner` row whose `(asset_id, role, source_id)` the survivor already
    holds cannot move: `uq_asset_owner_edge` allows one such row per organisation, and the survivor's
    row already states the same fact from the same source. It stays on the absorbed row, untouched,
    listed in `asset_owner_collisions` with the survivor row it duplicates -- kept, not collapsed,
    because collapsing means deleting a row (this module never deletes) and because the two rows can
    differ in `share_pct`/`as_of`/`owner_name_raw`, which the event then still shows. An edge to the
    same asset and role from a *different* source is not a collision: it moves, and the survivor
    carries both sources' rows (the company page counts distinct assets, so nothing double-counts).
    An alias whose normalised spelling the survivor already carries (`one_alias_per_org`) is
    handled the same way (`organization_alias_collisions`). Unmerge is exact either way, since a
    collided row never moved.

    **Parent links.** If the survivor has no parent and the absorbed row has one, the survivor
    takes it (the claim was made about the same legal entity), and `before.surviving.parent` keeps
    the survivor's previous (empty) link for unmerge. If the survivor's parent *is* the absorbed row,
    the link would become a self-loop: the survivor takes the absorbed row's own parent instead, or
    none, recorded the same way. When both have different parents the survivor's stands; the
    absorbed row's is still in its snapshot.

    **The alias for the absorbed spelling** is written only when the survivor carries no alias with
    that normalised spelling after the moves above (usually the absorbed row's own alias has just
    moved and already records it, with its original provenance). Its provenance is the first active
    `proposal_source` of a proposal the absorbed row sponsored, else the absorbed row's first
    ownership edge -- the record the spelling was read from; with neither, no alias is written.

    **Provenance.** `source_id`/`source_url`/`retrieved_at`/`licence_id` stamp the event with the
    document a curated merge cites (`services/ingest/organizations.py::load_merges`); given, all four
    are required. The resolver's key-based merges pass none, and the event takes the quartet of the
    most restrictive stored row naming either organisation (docs/22 §13.7,
    `services/resolve/provenance.py`). Until 2026-10-07 those events carried no quartet, and the
    spelling alias's licence leaked into the event's `licence_id` alone (the alias tuple below
    rebound the `licence_id` argument): 139 of 151 key-based merges on the eval store.
    """
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
    quartet: provenance.Quartet | None
    if any(v is not None for v in (source_id, source_url, retrieved_at, licence_id)):
        if source_id is None or source_url is None or retrieved_at is None or licence_id is None:
            raise ValueError(
                "an explicit merge provenance needs all four of "
                "source_id, source_url, retrieved_at, licence_id"
            )
        quartet = provenance.Quartet(source_id, source_url, retrieved_at, licence_id)
    else:
        # Read before anything moves, so each side's evidence is still its own.
        quartet = _organization_quartet(session, canonical, absorbed, "merge")

    sponsored = session.scalars(
        select(Proposal).where(Proposal.sponsor_org_id == absorbed.id).order_by(Proposal.id)
    ).all()
    edges = session.scalars(
        select(AssetOwner).where(AssetOwner.organization_id == absorbed.id).order_by(AssetOwner.id)
    ).all()
    children = session.scalars(
        select(Organization)
        .where(Organization.parent_org_id == absorbed.id, Organization.id != canonical.id)
        .order_by(Organization.id)
    ).all()
    own_aliases = session.scalars(
        select(OrganizationAlias)
        .where(OrganizationAlias.organization_id == absorbed.id)
        .order_by(OrganizationAlias.id)
    ).all()

    survivor_edges = {
        (e.asset_id, e.role, e.source_id): e.id
        for e in session.scalars(select(AssetOwner).where(AssetOwner.organization_id == canonical.id))
    }
    moved_edges: list[AssetOwner] = []
    edge_collisions: list[dict[str, str]] = []
    for e in edges:
        clash = survivor_edges.get((e.asset_id, e.role, e.source_id))
        if clash is None:
            moved_edges.append(e)
        else:
            edge_collisions.append({"id": str(e.id), "survivor_edge_id": str(clash)})

    survivor_alias_keys = {
        a.alias_normalised: a.id
        for a in session.scalars(
            select(OrganizationAlias).where(OrganizationAlias.organization_id == canonical.id)
        )
    }
    moved_aliases: list[OrganizationAlias] = []
    alias_collisions: list[dict[str, str]] = []
    for a in own_aliases:
        clash_alias = survivor_alias_keys.get(a.alias_normalised)
        if clash_alias is None:
            moved_aliases.append(a)
            survivor_alias_keys[a.alias_normalised] = a.id
        else:
            alias_collisions.append({"id": str(a.id), "survivor_alias_id": str(clash_alias)})

    surviving_before: dict[str, Any] = {"last_changed": _json_safe(canonical.last_changed)}
    changed_keys = ["last_changed"]
    absorbed_parent_usable = absorbed.parent_org_id is not None and absorbed.parent_org_id != canonical.id
    new_parent: dict[str, Any] | None = None
    if canonical.parent_org_id == absorbed.id:
        # A self-loop once the two rows are one: the survivor takes the absorbed row's own parent
        # (the next link up the same chain), or none.
        new_parent = (
            {key: getattr(absorbed, key) for key in _PARENT_FIELDS}
            if absorbed_parent_usable
            else dict.fromkeys(_PARENT_FIELDS)
        )
    elif canonical.parent_org_id is None and absorbed_parent_usable:
        new_parent = {key: getattr(absorbed, key) for key in _PARENT_FIELDS}
    if new_parent is not None:
        surviving_before["parent"] = _parent_snapshot(canonical)
        changed_keys.append("parent_org_id")

    before_payload = {
        "surviving": surviving_before,
        "absorbed": {
            "id": str(absorbed.id),
            "public_id": absorbed.public_id,
            "slug": absorbed.slug,
            "entity": serialize_row(absorbed),
            "sponsored_proposal_ids": [str(p.id) for p in sponsored],
            "asset_owner_ids": [str(e.id) for e in moved_edges],
            "asset_owner_collisions": edge_collisions,
            "child_organization_ids": [str(c.id) for c in children],
            "organization_alias_ids": [str(a.id) for a in moved_aliases],
            "organization_alias_collisions": alias_collisions,
        },
    }

    alias_provenance = _absorbed_spelling_provenance(session, sponsored, edges)

    for p in sponsored:
        p.sponsor_org_id = canonical.id
    for e in moved_edges:
        e.organization_id = canonical.id
    for c in children:
        c.parent_org_id = canonical.id
    for a in moved_aliases:
        a.organization_id = canonical.id
    if new_parent is not None:
        for key, value in new_parent.items():
            setattr(canonical, key, value)

    written_alias: OrganizationAlias | None = None
    if alias_provenance is not None and absorbed.name_normalised not in survivor_alias_keys:
        alias_source_id, alias_url, alias_retrieved, alias_licence_id = alias_provenance
        written_alias = OrganizationAlias(
            organization_id=canonical.id,
            alias=absorbed.name_canonical,
            alias_normalised=absorbed.name_normalised,
            kind="filing_spelling",
            source_id=alias_source_id,
            source_url=alias_url,
            retrieved_at=alias_retrieved,
            licence_id=alias_licence_id,
            confidence=1.0,
            created_by="pipeline",
        )
        session.add(written_alias)
        session.flush()

    canonical.last_changed = utcnow()
    absorbed.merged_into_id = canonical.id

    after_payload: dict[str, Any] = {"surviving": {"last_changed": _json_safe(canonical.last_changed)}}
    if new_parent is not None:
        after_payload["surviving"]["parent"] = _parent_snapshot(canonical)
    if written_alias is not None:
        after_payload["written_alias_id"] = str(written_alias.id)

    event = Event(
        subject_type="organization",
        subject_id=canonical.id,
        event_type="merged",
        observed_at=utcnow(),
        **(quartet.columns() if quartet is not None else {}),
        before=before_payload,
        after=after_payload,
        changed_keys=changed_keys,
        actor_type=actor_type,
        confidence=1.0,
        reason=rationale,
        idempotency_key=idem,
    )
    session.add(event)
    session.flush()
    return event


def _organization_quartet(
    session: Session, canonical: Organization, absorbed: Organization, what: str
) -> provenance.Quartet | None:
    """The derived quartet of an organisation merge or unmerge (docs/22 §13.7). Unlike a proposal, an
    organisation can exist with no stored row naming it (a parent known only through its
    children's `parent_source_id`, which records a source but no URL or retrieval time): such an
    event is written without a quartet and logged, never given an invented one. Every loader that
    creates an organisation also writes an alias with a full quartet, so a loaded store has none
    (measured: 0 of 151 on the eval store, docs/22 §13.7)."""
    quartet = provenance.for_organization_merge(session, canonical, absorbed)
    if quartet is None:
        logger.warning(
            "organization %s with no stored row naming either side; event written without provenance",
            what,
            extra={"canonical": str(canonical.id), "absorbed": str(absorbed.id)},
        )
    return quartet


def _absorbed_spelling_provenance(
    session: Session, sponsored: Sequence[Proposal], edges: Sequence[AssetOwner]
) -> tuple[str, str, dt.datetime, str] | None:
    """(source_id, source_url, retrieved_at, licence_id) of the record the absorbed spelling was
    read from: a sponsored proposal's first active source link, else the first ownership edge."""
    if sponsored:
        link = session.scalar(
            select(ProposalSource).where(
                ProposalSource.proposal_id == sponsored[0].id, ProposalSource.active.is_(True)
            )
        )
        if link is not None:
            return link.source_id, link.source_url, link.retrieved_at, link.licence_id
    if edges:
        edge = edges[0]
        return edge.source_id, edge.source_url, edge.retrieved_at, edge.licence_id
    return None


def unmerge_organization(session: Session, merge_event_id: _uuid.UUID, *, reason: str = "unmerge") -> Event:
    """Organization counterpart of `unmerge_proposal`, same exact-restore contract, read from the
    merge event alone. Events written before 2026-09-26 carry only `sponsored_proposal_ids`; every
    newer key is read with a default, so those events still unmerge exactly what they moved.

    The alias written for the absorbed spelling (`after.written_alias_id`) stays on the survivor,
    as it always has: a spelling recorded once is still a spelling that organisation is known by
    (tests/test_resolve_store_unmerge.py). The absorbed row's *own* aliases move back."""
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

    # The quartet of the merge being reversed; a merge written before docs/22 §13.7 carries none, and
    # the merge rule derives one over both organisations (their combined evidence is the same set
    # before and after the rows move back).
    quartet = provenance.of_event(merge_event) or _organization_quartet(
        session, canonical, absorbed, "unmerge"
    )

    absorbed_payload = before["absorbed"]
    restore_row(absorbed, absorbed_payload["entity"])

    for proposal_id_str in absorbed_payload.get("sponsored_proposal_ids", []):
        p = session.get(Proposal, _uuid.UUID(proposal_id_str))
        if p is not None:
            p.sponsor_org_id = absorbed.id
    for edge_id_str in absorbed_payload.get("asset_owner_ids", []):
        edge = session.get(AssetOwner, _uuid.UUID(edge_id_str))
        if edge is not None:
            edge.organization_id = absorbed.id
    for child_id_str in absorbed_payload.get("child_organization_ids", []):
        child = session.get(Organization, _uuid.UUID(child_id_str))
        if child is not None:
            child.parent_org_id = absorbed.id
    for alias_id_str in absorbed_payload.get("organization_alias_ids", []):
        alias = session.get(OrganizationAlias, _uuid.UUID(alias_id_str))
        if alias is not None:
            alias.organization_id = absorbed.id

    surviving = before["surviving"]
    if "parent" in surviving:
        _restore_parent(canonical, surviving["parent"])
    # As for proposals: the unmerge is a change to both rows (backend audit 2026-09-30 F6).
    canonical.last_changed = absorbed.last_changed = canonical.updated_at = absorbed.updated_at = utcnow()

    event = Event(
        subject_type="organization",
        subject_id=canonical.id,
        event_type="unmerged",
        observed_at=utcnow(),
        **(quartet.columns() if quartet is not None else {}),
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
