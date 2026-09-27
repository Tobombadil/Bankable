"""Recompute the `match` table and write the `match_added` / `match_removed` events (docs/10 US-401
AC1-AC2; docs/21 §3.11, §7.3). `python -m services.match.run [--all]`.

**Scope of one run.** Three modes, one code path:

- `full` (`--all`, or the first run on a store, or when any active match was produced by a
  different `rule_set_version`): every eligible proposal against every eligible opportunity.
- `incremental` (the default): the proposals and opportunities whose `updated_at` is after the
  previous run's start (`worker_watermark` row `match_run`), plus every opportunity that holds an
  active match and whose `due_at` has passed since -- a deadline expires without any row
  changing, and a match must not outlive it just because nothing was re-loaded.
- `scoped` (`proposal_ids=` / `opportunity_ids=`, what the intake approval calls): exactly those
  records. It does not move the watermark, because it did not look at anything else.

"Eligible" is `merged_into_id IS NULL AND publish_state <> 'unpublished'`: a `pending_review`
intake record is matched (its matches stay invisible until it is published, because every read
composes the visibility predicate on both sides -- `services/api/matches.py`), a merged-away or
unpublished one is not, and its active matches are removed.

**Blocking is exact.** Only pairs that share a country and a technology family (or face an
all-source opportunity) are scored: any other pair fails the jurisdiction or the technology rule
by construction (`services/match/engine.py` "blocking keys"). On data/normalized that is ~1,200
scored pairs out of 7.4 million.

**Writes.** One active `match` row per pair (the partial unique index `uq_match_active_pair`).
A pair that still matches keeps its row (and `first_matched_at`); score, rationale, rule-set
version and `last_evaluated_at` are refreshed. A pair that stops matching is set `status =
'removed'` with `removed_at`; if it matches again later it gets a new row. Every add and remove
writes **two** events, one with the proposal as subject and one with the opportunity as subject,
so the change appears in both timelines (`GET /v1/proposals/{id}/events`, `.../opportunities/
{id}/events`), which select on `(subject_type, subject_id)`:

- provenance is the *counterpart's* best active source link, so the event's own licence and
  source clauses in `services/api/visibility.py::event_visibility_filter` gate the side the event
  discloses, while its subject clause gates the side it is filed under;
- `public_at` / `published_at` are set only when the rule set's `publish` gate is on
  (`data/match_rules.yaml`; `match-rules@v1` is withheld because it misses docs/10 US-401 AC3) and
  both sides pass the public / Pro predicate at the moment of writing. Otherwise the event is
  recorded (the audit trail is complete) with both null and never surfaces on a non-admin read.
  Recorded limitation: such an event does not surface later -- not when the hidden side is
  published, and not when the gate is opened; the match itself does, through the match routes,
  which evaluate the gate and visibility at read time.
- `idempotency_key = match:<match uuid>:<event_type>:<subject_type>`, one per row and side.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import uuid as _uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api.common import WEB_HOST
from services.api.visibility import opportunity_visibility_filter, proposal_visibility_filter
from services.db.models import (
    Event,
    Match,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
    WorkerWatermark,
)
from services.ids import public_id
from services.match.engine import (
    MatchResult,
    OpportunityFacts,
    ProposalFacts,
    jurisdiction_country,
    opportunity_families,
    proposal_families,
    score_pair,
)
from services.match.rules import RuleSet, load_rules

log = logging.getLogger(__name__)

#: `worker_watermark.name` for this job; `seq` holds `MAX(event.seq)` at the run's start (for an
#: operator reading the table) and `updated_at` the run's start time, which is what incremental
#: mode compares `updated_at` against.
WATERMARK_NAME = "match_run"


@dataclass
class RunReport:
    mode: str
    rule_set_version: str
    proposals_considered: int = 0
    opportunities_considered: int = 0
    pairs_scored: int = 0
    added: int = 0
    kept: int = 0
    removed: int = 0
    events_written: int = 0
    active_total: int = 0
    added_ids: list[_uuid.UUID] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"mode={self.mode} rules={self.rule_set_version} proposals={self.proposals_considered} "
            f"opportunities={self.opportunities_considered} pairs_scored={self.pairs_scored} "
            f"added={self.added} kept={self.kept} removed={self.removed} "
            f"events={self.events_written} active_total={self.active_total}"
        )


# ------------------------------------------------------------------------------------------ facts
def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def proposal_facts(row: Proposal) -> ProposalFacts:
    return ProposalFacts(
        technology=row.technology,
        jurisdiction=row.jurisdiction,
        capacity_mw=_float(row.capacity_mw),
        lifecycle_state=row.lifecycle_state,
    )


def opportunity_facts(row: Opportunity) -> OpportunityFacts:
    return OpportunityFacts(
        technologies=list(row.technologies or []),
        jurisdiction=row.jurisdiction,
        capacity_sought_mw=_float(row.capacity_sought_mw),
        status=row.status,
        due_at=row.due_at,
    )


def _eligible_proposals(session: Session) -> dict[_uuid.UUID, ProposalFacts]:
    rows = session.execute(
        select(
            Proposal.id,
            Proposal.technology,
            Proposal.jurisdiction,
            Proposal.capacity_mw,
            Proposal.lifecycle_state,
        ).where(Proposal.merged_into_id.is_(None), Proposal.publish_state != "unpublished")
    ).all()
    return {r[0]: ProposalFacts(r[1], r[2], _float(r[3]), r[4]) for r in rows}


def _eligible_opportunities(session: Session) -> dict[_uuid.UUID, OpportunityFacts]:
    rows = session.execute(
        select(
            Opportunity.id,
            Opportunity.technologies,
            Opportunity.jurisdiction,
            Opportunity.capacity_sought_mw,
            Opportunity.status,
            Opportunity.due_at,
        ).where(Opportunity.merged_into_id.is_(None), Opportunity.publish_state != "unpublished")
    ).all()
    return {r[0]: OpportunityFacts(list(r[1] or []), r[2], _float(r[3]), r[4], r[5]) for r in rows}


# --------------------------------------------------------------------------------------- blocking
class _Index:
    """Proposals keyed by `(country, family)` and by country, for exact candidate generation."""

    def __init__(self, proposals: dict[_uuid.UUID, ProposalFacts], rules: RuleSet) -> None:
        self.by_family: dict[tuple[str, str], set[_uuid.UUID]] = defaultdict(set)
        self.by_country: dict[str, set[_uuid.UUID]] = defaultdict(set)
        for pid, facts in proposals.items():
            country = jurisdiction_country(facts.jurisdiction)
            self.by_country[country].add(pid)
            for family in proposal_families(facts, rules):
                self.by_family[(country, family)].add(pid)

    def proposals_for(self, opportunity: OpportunityFacts, rules: RuleSet) -> set[_uuid.UUID]:
        country = jurisdiction_country(opportunity.jurisdiction)
        families = opportunity_families(opportunity, rules)
        if families is None:
            return set(self.by_country.get(country, ()))
        out: set[_uuid.UUID] = set()
        for family in families:
            out |= self.by_family.get((country, family), set())
        return out


def candidate_pairs(
    proposals: dict[_uuid.UUID, ProposalFacts],
    opportunities: dict[_uuid.UUID, OpportunityFacts],
    rules: RuleSet,
    *,
    proposal_scope: set[_uuid.UUID] | None = None,
    opportunity_scope: set[_uuid.UUID] | None = None,
) -> set[tuple[_uuid.UUID, _uuid.UUID]]:
    """Every `(proposal_id, opportunity_id)` that could pass both the jurisdiction and the
    technology rule. With no scope, all of them; with a scope, the pairs touching it."""
    index = _Index(proposals, rules)
    pairs: set[tuple[_uuid.UUID, _uuid.UUID]] = set()
    full = proposal_scope is None and opportunity_scope is None
    for oid, ofacts in opportunities.items():
        candidates = index.proposals_for(ofacts, rules)
        if full or (opportunity_scope is not None and oid in opportunity_scope):
            pairs.update((pid, oid) for pid in candidates)
        elif proposal_scope:
            pairs.update((pid, oid) for pid in candidates & proposal_scope)
    return pairs


# ------------------------------------------------------------------------------------------ events
@dataclass(frozen=True)
class _Side:
    public_id: str
    name: str
    url: str
    source: Source | None
    source_url: str | None
    retrieved_at: dt.datetime | None


def _best_link(links: Sequence[ProposalSource] | Sequence[OpportunitySource]) -> Any:
    """The active link whose source is most publishable: `public` sources first, then the least
    restrictive licence, then `source_id` -- the provenance a timeline event should carry so that
    it is gated on the most permissive of the counterpart's own sources, never a stricter one."""
    order = {"open": 0, "attribution": 1, "noncommercial": 2, "restricted": 3, "unknown": 4}
    active = [link for link in links if link.active]
    if not active:
        return None
    return min(
        active,
        key=lambda link: (
            link.source.publish_state != "public",
            order.get(link.source.licence.reuse_class, 9),
            link.source_id,
        ),
    )


def _side(record: Proposal | Opportunity) -> _Side:
    link = _best_link(record.sources)
    if isinstance(record, Proposal):
        name, segment = record.name_canonical, "proposals"
    else:
        name, segment = record.title, "opportunities"
    return _Side(
        public_id=record.public_id,
        name=name,
        url=f"{WEB_HOST}/{segment}/{record.slug}",
        source=link.source if link is not None else None,
        source_url=link.source_url if link is not None else None,
        retrieved_at=link.retrieved_at if link is not None else None,
    )


def _visible_ids(
    session: Session, model: type[Proposal] | type[Opportunity], ids: Iterable[_uuid.UUID], entitlement: str
) -> set[_uuid.UUID]:
    wanted = list(set(ids))
    if not wanted:
        return set()
    predicate = (
        proposal_visibility_filter(entitlement)
        if model is Proposal
        else opportunity_visibility_filter(entitlement)
    )
    return set(session.scalars(select(model.id).where(model.id.in_(wanted), *predicate)))


class _EventWriter:
    def __init__(self, session: Session, now: dt.datetime, job_id: str, *, publish: bool) -> None:
        self.session = session
        self.now = now
        self.publish = publish
        self.job_id = job_id
        self.written = 0
        self._sides: dict[_uuid.UUID, _Side] = {}
        self._visible: dict[str, set[_uuid.UUID]] = {}

    def prepare(self, matches: Sequence[Match]) -> None:
        """Load both sides of every match that will get an event, and which of them are visible
        on the public and Pro tiers right now -- four queries for the whole run, not per event."""
        pids = {m.proposal_id for m in matches}
        oids = {m.opportunity_id for m in matches}
        for proposal in self.session.scalars(select(Proposal).where(Proposal.id.in_(list(pids)))).unique():
            self._sides[proposal.id] = _side(proposal)
        for opportunity in self.session.scalars(
            select(Opportunity).where(Opportunity.id.in_(list(oids)))
        ).unique():
            self._sides[opportunity.id] = _side(opportunity)
        for entitlement in ("public", "pro"):
            self._visible[entitlement] = _visible_ids(
                self.session, Proposal, pids, entitlement
            ) | _visible_ids(self.session, Opportunity, oids, entitlement)

    def _both_visible(self, match: Match, entitlement: str) -> bool:
        visible = self._visible.get(entitlement, set())
        return match.proposal_id in visible and match.opportunity_id in visible

    def write(self, match: Match, event_type: str, detail: dict[str, Any]) -> None:
        proposal = self._sides[match.proposal_id]
        opportunity = self._sides[match.opportunity_id]
        # The rule set's publish gate first (`data/match_rules.yaml` `publish`): a withheld rule
        # set's events are recorded and never visible on any tier, feed or webhook.
        public_at = self.now if self.publish and self._both_visible(match, "public") else None
        published_at = self.now if self.publish and self._both_visible(match, "pro") else None
        match_public_id = public_id("mat", match.id)
        for subject_type, subject_id, counterpart_type, counterpart in (
            ("proposal", match.proposal_id, "opportunity", opportunity),
            ("opportunity", match.opportunity_id, "proposal", proposal),
        ):
            payload = {
                "match_id": match_public_id,
                "counterpart": {
                    "type": counterpart_type,
                    "public_id": counterpart.public_id,
                    "name": counterpart.name,
                    "url": counterpart.url,
                },
                **detail,
            }
            source = counterpart.source
            event = Event(
                subject_type=subject_type,
                subject_id=subject_id,
                event_type=event_type,
                observed_at=self.now,
                published_at=published_at,
                public_at=public_at,
                source_id=source.id if source is not None else None,
                source_url=counterpart.source_url,
                retrieved_at=counterpart.retrieved_at,
                licence_id=source.licence_id if source is not None else None,
                before=None if event_type == "match_added" else {"match": "active"},
                after=payload,
                changed_keys=["match"],
                actor_type="system",
                job_id=self.job_id,
                idempotency_key=f"match:{match.id}:{event_type}:{subject_type}",
            )
            self.session.add(event)
            # One flush per event: `Event.seq`'s before-insert listener reads MAX(seq) and two
            # events flushed together would collide (services/db/models.py, the loader's rule).
            self.session.flush()
            self.written += 1


# --------------------------------------------------------------------------------------------- run
def _watermark(session: Session) -> WorkerWatermark | None:
    return session.get(WorkerWatermark, WATERMARK_NAME)


def _as_aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _incremental_scope(
    session: Session, since: dt.datetime, now: dt.datetime
) -> tuple[set[_uuid.UUID], set[_uuid.UUID]]:
    changed_p = set(session.scalars(select(Proposal.id).where(Proposal.updated_at > since)))
    changed_o = set(session.scalars(select(Opportunity.id).where(Opportunity.updated_at > since)))
    expired = session.scalars(
        select(Match.opportunity_id)
        .join(Opportunity, Opportunity.id == Match.opportunity_id)
        .where(Match.status == "active", Opportunity.due_at.is_not(None), Opportunity.due_at <= now)
    )
    changed_o.update(expired)
    return changed_p, changed_o


def _removal_detail(
    pair: tuple[_uuid.UUID, _uuid.UUID],
    proposals: dict[_uuid.UUID, ProposalFacts],
    opportunities: dict[_uuid.UUID, OpportunityFacts],
    rules: RuleSet,
    now: dt.datetime,
) -> dict[str, Any]:
    pid, oid = pair
    if pid not in proposals:
        return {"reason": "proposal no longer eligible (merged or unpublished)"}
    if oid not in opportunities:
        return {"reason": "opportunity no longer eligible (merged or unpublished)"}
    result = score_pair(proposals[pid], opportunities[oid], rules, now=now)
    if result.rules_failed:
        return {
            "reason": "rules failed: " + ", ".join(result.rules_failed),
            "rules_failed": list(result.rules_failed),
            "rationale_text": result.rationale_text,
            "score": result.score,
        }
    return {
        "reason": f"score {result.score:.3f} below threshold {result.threshold:.2f}",
        "score": result.score,
    }


def _apply(match: Match, result: MatchResult, now: dt.datetime) -> None:
    match.score = result.score
    match.rationale = result.rationale
    match.rationale_text = result.rationale_text
    match.rule_set_version = result.rule_set_version
    match.last_evaluated_at = now


def run_matches(
    session: Session,
    *,
    full: bool = False,
    proposal_ids: Iterable[_uuid.UUID] | None = None,
    opportunity_ids: Iterable[_uuid.UUID] | None = None,
    rules: RuleSet | None = None,
    now: dt.datetime | None = None,
) -> RunReport:
    """Recompute matches (module docstring for the three modes) and flush; the caller commits."""
    rules = rules or load_rules()
    now = now or dt.datetime.now(dt.UTC)
    p_scope = set(proposal_ids) if proposal_ids is not None else None
    o_scope = set(opportunity_ids) if opportunity_ids is not None else None
    scoped = p_scope is not None or o_scope is not None
    watermark = _watermark(session)

    if scoped:
        mode = "scoped"
    elif full or watermark is None:
        mode = "full"
    else:
        stale_version = session.scalar(
            select(func.count())
            .select_from(Match)
            .where(Match.status == "active", Match.rule_set_version != rules.version)
        )
        mode = "full" if stale_version else "incremental"
        if mode == "incremental":
            p_scope, o_scope = _incremental_scope(session, _as_aware(watermark.updated_at), now)

    report = RunReport(mode=mode, rule_set_version=rules.version)
    proposals = _eligible_proposals(session)
    opportunities = _eligible_opportunities(session)

    if mode == "full":
        pairs = candidate_pairs(proposals, opportunities, rules)
        in_scope_active = list(session.scalars(select(Match).where(Match.status == "active")))
        report.proposals_considered, report.opportunities_considered = len(proposals), len(opportunities)
    else:
        p_set, o_set = p_scope or set(), o_scope or set()
        pairs = candidate_pairs(
            proposals, opportunities, rules, proposal_scope=p_set, opportunity_scope=o_set
        )
        in_scope_active = (
            list(
                session.scalars(
                    select(Match).where(
                        Match.status == "active",
                        Match.proposal_id.in_(list(p_set)) | Match.opportunity_id.in_(list(o_set)),
                    )
                )
            )
            if (p_set or o_set)
            else []
        )
        report.proposals_considered, report.opportunities_considered = len(p_set), len(o_set)

    matched: dict[tuple[_uuid.UUID, _uuid.UUID], MatchResult] = {}
    for pid, oid in pairs:
        result = score_pair(proposals[pid], opportunities[oid], rules, now=now)
        report.pairs_scored += 1
        if result.is_match:
            matched[(pid, oid)] = result

    existing = {(m.proposal_id, m.opportunity_id): m for m in in_scope_active}
    added: list[Match] = []
    removed: list[tuple[Match, dict[str, Any]]] = []
    for pair, match in existing.items():
        if pair in matched:
            _apply(match, matched[pair], now)
            report.kept += 1
        else:
            match.status = "removed"
            match.removed_at = now
            match.last_evaluated_at = now
            removed.append((match, _removal_detail(pair, proposals, opportunities, rules, now)))
    # Flush the removals before inserting: a pair that is removed and re-added in one run cannot
    # happen (it either matches or it does not), but the partial unique index is only satisfied
    # once the old row's status is written.
    session.flush()
    for (pid, oid), result in sorted(matched.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        if (pid, oid) in existing:
            continue
        match = Match(
            id=_uuid.uuid4(),
            proposal_id=pid,
            opportunity_id=oid,
            created_by="rule",
            status="active",
            first_matched_at=now,
            score=result.score,
            rationale=result.rationale,
            rationale_text=result.rationale_text,
            rule_set_version=result.rule_set_version,
            last_evaluated_at=now,
        )
        session.add(match)
        added.append(match)
    session.flush()
    report.added = len(added)
    report.removed = len(removed)
    report.added_ids = [m.id for m in added]

    if added or removed:
        writer = _EventWriter(
            session, now, job_id=f"match:{mode}:{now.isoformat(timespec='seconds')}", publish=rules.publish
        )
        writer.prepare([*added, *(m for m, _ in removed)])
        for match in added:
            writer.write(
                match,
                "match_added",
                {
                    "score": float(match.score),
                    "rationale_text": match.rationale_text,
                    "rule_set_version": match.rule_set_version,
                },
            )
        for match, detail in removed:
            writer.write(match, "match_removed", {"status": "removed", **detail})
        report.events_written = writer.written

    if mode != "scoped":
        max_seq = session.scalar(select(func.max(Event.seq))) or 0
        if watermark is None:
            watermark = WorkerWatermark(name=WATERMARK_NAME, seq=max_seq, updated_at=now)
            session.add(watermark)
        else:
            watermark.seq = max_seq
            watermark.updated_at = now
    session.flush()
    report.active_total = (
        session.scalar(select(func.count()).select_from(Match).where(Match.status == "active")) or 0
    )
    return report


def active_match_count(
    session: Session, *, proposal_id: _uuid.UUID | None = None, opportunity_id: _uuid.UUID | None = None
) -> int:
    stmt = select(func.count()).select_from(Match).where(Match.status == "active")
    if proposal_id is not None:
        stmt = stmt.where(Match.proposal_id == proposal_id)
    if opportunity_id is not None:
        stmt = stmt.where(Match.opportunity_id == opportunity_id)
    return session.scalar(stmt) or 0


# --------------------------------------------------------------------------------------------- CLI
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m services.match.run",
        description="Recompute proposal <-> opportunity matches (docs/10 US-401).",
    )
    parser.add_argument("--all", action="store_true", help="re-evaluate every pair, not only changed records")
    parser.add_argument("--proposal", action="append", default=[], help="a proposal public id (repeatable)")
    parser.add_argument(
        "--opportunity", action="append", default=[], help="an opportunity public id (repeatable)"
    )
    parser.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    from services.db.session import get_engine, get_sessionmaker

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    engine = get_engine(args.database_url)
    with get_sessionmaker(engine)() as session:
        proposal_ids = opportunity_ids = None
        if args.proposal or args.opportunity:
            proposal_ids = list(
                session.scalars(select(Proposal.id).where(Proposal.public_id.in_(args.proposal)))
            )
            opportunity_ids = list(
                session.scalars(select(Opportunity.id).where(Opportunity.public_id.in_(args.opportunity)))
            )
            unknown = len(args.proposal) + len(args.opportunity) - len(proposal_ids) - len(opportunity_ids)
            if unknown:
                sys.stderr.write(f"{unknown} public id(s) not found\n")
                return 2
        report = run_matches(
            session, full=args.all, proposal_ids=proposal_ids, opportunity_ids=opportunity_ids
        )
        session.commit()
    sys.stdout.write(report.summary() + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "WATERMARK_NAME",
    "RunReport",
    "active_match_count",
    "candidate_pairs",
    "main",
    "opportunity_facts",
    "proposal_facts",
    "run_matches",
]
