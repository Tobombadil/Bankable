"""Link each data centre to the operator its own name names (docs/22 §27; docs/28 B-9).

Before this module none of the 582 large-load records (Virginia DEQ air sites, EPA ICIS-Air facilities) had
a sponsor: both registers name a facility, not an owner, and their connectors leave `sponsor_name` null. So
`GET /v1/organizations?technology=load` returned no company at all, though 248 of the 582 names begin with a
known operator's (Amazon 73, Microsoft 28, Equinix 17).

**The rule** is the curated file `data/vendored/organizations/data_centre_operators.yaml` (its header states
the evidence rule): a record is linked to an operator only when its own name, or the name one of its source
links carries, contains that operator's own name. A project vehicle is linked only when its name carries the
operator's (`QTS Richmond V LLC`); a shell name (`SI NVA05D LLC`, `VADATA Inc`) is left unlinked rather than
guessed, a name two operators share links neither, and nothing links to a natural person. The check that a
match carries the operator's name runs on every match (`OperatorRule.match`), not only in the tests.

**What a link writes.** `proposal.sponsor_org_id`, and one `sponsor_linked` event on the proposal: `before`
the sponsor it replaced (always none, or a link this module made), `after` the organisation, the operator
row, the registry's digest and the evidence (source, record id, the name, the text matched), with the
provenance quartet of the source link whose name was matched (`services/resolve/provenance.py`: the most
restrictive licence first). The event has no `published_at`/`public_at`, like a merge, so no feed, alert,
pulse or trend counts it: a sponsor read off a name the register already published is not news. The
proposal's `last_changed` moves (docs/21 §3.1: the latest event that changed a canonical field), as a merge
moves it, so an API client syncing on `updated_since` sees the new sponsor.

**Reversible.** `unlink(event_id)` puts the sponsor back to `before` and writes `sponsor_unlinked`
(`reverses_event_id` set). A manual unlink is marked `suppress`, and the pass never links that record
again; an automatic one (the file no longer links the name) is not.

**Only its own links.** A record whose sponsor a source or an editor set is never touched; a link this
module made and someone later cleared is not re-made; one it made is updated or removed when the file
changes. An organisation merged after the link is followed to its survivor (`OrgRedirects`).

**Where it runs.** `services/resolve/merge.py::resolve_organizations` calls `link_data_centre_operators`
after its merges, so the scheduler's `resolve_tick` (after every load, and daily) reaches every stored
record, the 582 already held included, and the companies index's figures refresh in the same pass
(`infra/scheduler/jobs.py::default_resolve`). `python -m services.resolve.operators` runs it by hand
(`--dry-run` reports without writing).
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import pathlib
import re
import uuid as _uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from services.db.models import Event, Organization, Proposal, ProposalSource, new_uuid, utcnow
from services.ids import public_id, unique_slug
from services.ingest.org_redirects import OrgRedirects
from services.ingest.organizations import org_key_multimap
from services.personal_names import classify
from services.resolve import provenance

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "data" / "vendored" / "organizations" / "data_centre_operators.yaml"

LINK_EVENT = "sponsor_linked"
UNLINK_EVENT = "sponsor_unlinked"
EVENT_TYPES = (LINK_EVENT, UNLINK_EVENT)
#: The proposal kind this module links. Every large-load source files its records under it.
LOAD_KIND = "load"
#: The raw columns that hold a facility's name on a link written before the loader kept `normalised`
#: (Virginia DEQ `PLA_NAME`, EPA ICIS-Air `FACILITY_NAME`).
RAW_NAME_FIELDS = ("PLA_NAME", "FACILITY_NAME")

#: Rows per `IN (...)` query.
CHUNK = 500


class RegistryError(ValueError):
    """The operator file breaks its own schema or evidence rule; nothing is linked from it."""


def _squash(text: str) -> str:
    """Letters and digits only, upper case: the form the evidence check compares."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def name_pattern(name: str) -> str:
    """A whole-word pattern for one operator name: its words in order, with any punctuation or none
    between them (`Cyrus One` finds `CYRUSONE`, `Wal-Mart` finds `WALMART`), and `&` required where
    the name has it (`AT&T` finds `AT & T`, never `ATT`)."""
    parts: list[str] = []
    for part in re.findall(r"[A-Za-z0-9]+|&", name):
        if part == "&":
            parts.append(r"\s*&\s*")
            continue
        if parts and not parts[-1].endswith(r"\s*"):
            parts.append(r"[\W_]*")
        parts.append(re.escape(part))
    if not parts:
        raise RegistryError(f"operator name {name!r} has no letters or digits")
    return r"(?<![A-Za-z0-9])" + "".join(parts) + r"(?![A-Za-z0-9])"


@dataclass(frozen=True)
class OperatorRule:
    """One row of the operator file."""

    key: str
    organization: str
    country: str
    names: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    exclude: re.Pattern[str] | None
    examples: tuple[str, ...]
    #: `names` in the form the evidence check compares, computed once per row.
    squashed_names: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "squashed_names", tuple(s for s in map(_squash, self.names) if s))

    def match(self, text: str) -> str | None:
        """The text of `text` this row matched, or None. A match counts only when that text carries one
        of the row's own names (the evidence rule, checked here at run time)."""
        if not text or (self.exclude is not None and self.exclude.search(text)):
            return None
        for pattern in self.patterns:
            for found in pattern.finditer(text):
                span = found.group(0)
                if any(n in _squash(span) for n in self.squashed_names):
                    return span.strip()
        return None


@dataclass(frozen=True)
class Registry:
    version: str
    rules: tuple[OperatorRule, ...]


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise RegistryError(f"{where}: bad pattern {pattern!r}: {exc}") from exc


def parse_registry(doc: Mapping[str, Any]) -> Registry:
    """The operator file as rules. Raises `RegistryError` on a malformed row, a duplicate key, an
    organisation name with a natural person's shape, or an example its own row does not link."""
    rows = doc.get("operators")
    if not isinstance(rows, list) or not rows:
        raise RegistryError("the operator file lists no operators")
    rules: list[OperatorRule] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RegistryError(f"operators[{i}] is not a mapping")
        key = str(row.get("key") or "").strip()
        organization = str(row.get("organization") or "").strip()
        names = tuple(str(n).strip() for n in (row.get("names") or []) if str(n).strip())
        if not key or not organization or not names:
            raise RegistryError(f"operators[{i}] needs key, organization and names")
        if key in seen:
            raise RegistryError(f"operator key {key!r} appears twice")
        seen.add(key)
        if classify(organization).personal:
            raise RegistryError(f"{key}: organisation {organization!r} classifies as a natural person")
        raw_patterns = row.get("patterns") or [name_pattern(n) for n in names]
        patterns = tuple(_compile(str(p), key) for p in raw_patterns)
        exclude = _compile(str(row["exclude"]), key) if row.get("exclude") else None
        rule = OperatorRule(
            key=key,
            organization=organization,
            country=str(row.get("country") or "US").strip().upper()[:2],
            names=names,
            patterns=patterns,
            exclude=exclude,
            examples=tuple(str(e) for e in (row.get("examples") or [])),
        )
        if not rule.examples:
            raise RegistryError(f"{key}: no examples from the register")
        for example in rule.examples:
            if rule.match(example) is None:
                raise RegistryError(f"{key}: its example {example!r} does not link")
        rules.append(rule)
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return Registry(version=digest[:12], rules=tuple(rules))


@functools.lru_cache(maxsize=4)
def load_registry(path: pathlib.Path = REGISTRY_PATH) -> Registry:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return parse_registry(doc)


@dataclass(frozen=True)
class OperatorMatch:
    """What a record's names say: one operator, a conflict between several, or nothing."""

    rule: OperatorRule | None
    #: Every operator key matched when more than one was (the record is then not linked).
    conflict: tuple[str, ...] = ()
    #: `(name, matched text)` for each of the record's names that matched `rule`.
    evidence: tuple[tuple[str, str], ...] = ()


def operator_for(names: Iterable[str | None], registry: Registry | None = None) -> OperatorMatch:
    """The operator a record's names name, over all its names (its own and each link's)."""
    reg = registry if registry is not None else load_registry()
    hits: dict[str, tuple[OperatorRule, list[tuple[str, str]]]] = {}
    for name in dict.fromkeys(str(n).strip() for n in names if n and str(n).strip()):
        for rule in reg.rules:
            span = rule.match(name)
            if span is not None:
                hits.setdefault(rule.key, (rule, []))[1].append((name, span))
    if not hits:
        return OperatorMatch(rule=None)
    if len(hits) > 1:
        return OperatorMatch(rule=None, conflict=tuple(sorted(hits)))
    ((rule, evidence),) = hits.values()
    return OperatorMatch(rule=rule, evidence=tuple(evidence))


# ------------------------------------------------------------------------------------------- store pass
@dataclass
class OperatorLinkReport:
    considered: int = 0
    linked: int = 0
    relinked: int = 0
    unlinked: int = 0
    kept: int = 0
    unmatched: int = 0
    conflicts: int = 0
    sponsored_elsewhere: int = 0
    cleared_elsewhere: int = 0
    suppressed: int = 0
    refused_personal: int = 0
    no_link: int = 0
    organizations_created: int = 0
    by_operator: Counter[str] = field(default_factory=Counter)
    events: list[Event] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("events", "by_operator")}
        out["by_operator"] = dict(sorted(self.by_operator.items(), key=lambda kv: (-kv[1], kv[0])))
        return out


def _record_names(
    proposal: Proposal, links: Sequence[ProposalSource]
) -> list[tuple[str, ProposalSource | None]]:
    """Every name the record and its active links carry, each with the link it came from."""
    out: list[tuple[str, ProposalSource | None]] = [(proposal.name_canonical, None)]
    for link in links:
        normalised = link.normalised or {}
        raw = link.raw or {}
        for value in (normalised.get("name_canonical"), *(raw.get(f) for f in RAW_NAME_FIELDS)):
            if isinstance(value, str) and value.strip():
                out.append((value.strip(), link))
    return out


def _as_uuid(value: Any) -> _uuid.UUID | None:
    try:
        return _uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


class _Orgs:
    """The organisation lookup for one pass: `org_key` over live names and aliases, built on first use."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._index: dict[str, list[Organization]] | None = None
        self._slugs: set[str] = set()

    def _built(self) -> dict[str, list[Organization]]:
        if self._index is None:
            self._index = org_key_multimap(self.session)
            self._slugs = set(self.session.scalars(select(Organization.slug)))
        return self._index

    def find(self, rule: OperatorRule) -> Organization | None:
        found = [o for o in self._built().get(org_key(rule.organization), []) if o.merged_into_id is None]
        if not found:
            return None
        exact = [o for o in found if o.name_normalised == rule.organization.lower()]
        return min(exact or found, key=lambda o: (o.first_seen, str(o.id)))

    def create(self, rule: OperatorRule) -> Organization:
        index = self._built()
        org_id = new_uuid()
        pid = public_id("org", org_id)
        slug = unique_slug(rule.organization, pid, self._slugs, bare_first=True)
        org = Organization(
            id=org_id,
            public_id=pid,
            slug=slug,
            name_canonical=rule.organization,
            name_normalised=rule.organization.lower(),
            type="other",
            country=rule.country,
        )
        self.session.add(org)
        self.session.flush()
        self._slugs.add(slug)
        index.setdefault(org_key(rule.organization), []).append(org)
        return org


def _quartet(
    session: Session, links: Sequence[ProposalSource], triggering: set[_uuid.UUID]
) -> provenance.Quartet | None:
    evidence = provenance.link_evidence([lk for lk in links if lk.id in triggering], triggering=True)
    evidence += provenance.link_evidence([lk for lk in links if lk.id not in triggering], triggering=False)
    return provenance.choose(session, evidence)


def _write_event(
    session: Session,
    proposal: Proposal,
    *,
    event_type: str,
    before: dict[str, Any],
    after: dict[str, Any],
    quartet: provenance.Quartet | None,
    reason: str,
    sequence: int,
    actor_type: str = "pipeline",
    reverses: Event | None = None,
) -> Event:
    event = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type=event_type,
        observed_at=utcnow(),
        **(quartet.columns() if quartet is not None else {}),
        before=before,
        after=after,
        changed_keys=["sponsor_org_id"],
        actor_type=actor_type,
        confidence=1.0,
        reason=reason,
        reverses_event_id=reverses.id if reverses is not None else None,
        idempotency_key=f"operator_link:{proposal.id}:{sequence}",
    )
    session.add(event)
    session.flush()
    return event


def _history(session: Session, proposal_ids: Sequence[_uuid.UUID]) -> dict[_uuid.UUID, list[Event]]:
    out: dict[_uuid.UUID, list[Event]] = {}
    for i in range(0, len(proposal_ids), CHUNK):
        chunk = list(proposal_ids[i : i + CHUNK])
        rows = session.scalars(
            select(Event)
            .where(
                Event.subject_type == "proposal",
                Event.event_type.in_(EVENT_TYPES),
                Event.subject_id.in_(chunk),
            )
            .order_by(Event.seq)
        )
        for event in rows:
            out.setdefault(event.subject_id, []).append(event)
    return out


def link_data_centre_operators(
    session: Session, *, registry: Registry | None = None, dry_run: bool = False
) -> OperatorLinkReport:
    """Link every live large-load record to the operator its names name (module docstring). With
    `dry_run`, count what would change and write nothing (no organisation is created either)."""
    reg = registry if registry is not None else load_registry()
    report = OperatorLinkReport()
    proposals = list(
        session.scalars(
            select(Proposal)
            .where(Proposal.kind == LOAD_KIND, Proposal.merged_into_id.is_(None))
            .order_by(Proposal.id)
        )
    )
    if not proposals:
        return report
    ids = [p.id for p in proposals]
    links: dict[_uuid.UUID, list[ProposalSource]] = {}
    for i in range(0, len(ids), CHUNK):
        for link in session.scalars(
            select(ProposalSource)
            .where(ProposalSource.proposal_id.in_(ids[i : i + CHUNK]), ProposalSource.active.is_(True))
            .order_by(ProposalSource.id)
        ):
            links.setdefault(link.proposal_id, []).append(link)
    history = _history(session, ids)
    redirects = OrgRedirects(session)
    orgs = _Orgs(session)

    for proposal in proposals:
        report.considered += 1
        past = history.get(proposal.id, [])
        last = past[-1] if past else None
        current = proposal.sponsor_org_id
        linked_id = _as_uuid((last.after or {}).get("sponsor_org_id")) if last is not None else None
        ours = (
            last is not None
            and last.event_type == LINK_EVENT
            and current is not None
            and linked_id is not None
            and redirects.terminal_id(linked_id) == redirects.terminal_id(current)
        )
        if current is not None and not ours:
            report.sponsored_elsewhere += 1
            continue
        if current is None and last is not None and last.event_type == LINK_EVENT:
            report.cleared_elsewhere += 1
            continue
        if last is not None and last.event_type == UNLINK_EVENT and (last.after or {}).get("suppress"):
            report.suppressed += 1
            continue
        own_links = links.get(proposal.id, [])
        if not own_links:
            report.no_link += 1
            continue
        named = _record_names(proposal, own_links)
        match = operator_for((n for n, _ in named), reg)
        rule = match.rule
        org = orgs.find(rule) if rule is not None else None
        if rule is None or (org is not None and org.personal_data):
            if match.conflict:
                report.conflicts += 1
            elif rule is not None:
                report.refused_personal += 1
            else:
                report.unmatched += 1
            if ours and last is not None and not dry_run:
                report.events.append(
                    _unlink(
                        session,
                        proposal,
                        last,
                        len(past),
                        suppress=False,
                        reason="the operator file no longer links this record",
                    )
                )
                report.unlinked += 1
            continue
        report.by_operator[rule.key] += 1
        if ours and org is not None and current is not None and redirects.terminal_id(current) == org.id:
            report.kept += 1
            continue
        if dry_run:
            report.relinked += int(ours)
            report.linked += int(not ours)
            report.organizations_created += int(org is None)
            continue
        created = org is None
        if org is None:
            org = orgs.create(rule)
            report.organizations_created += 1
        matched = dict(match.evidence)
        triggering = {lk.id for name, lk in named if lk is not None and name in matched}
        evidence: list[dict[str, str | None]] = [
            {
                "source_id": lk.source_id,
                "source_record_id": lk.source_record_id,
                "name": name,
                "matched": matched[name],
            }
            for name, lk in dict.fromkeys(named)
            if lk is not None and name in matched
        ]
        if not evidence:  # only the record's own name matched; the quartet still cites its links
            evidence = [
                {"source_id": None, "source_record_id": None, "name": n, "matched": s}
                for n, s in match.evidence
            ]
        # A relink keeps the sponsor the record had before any link of this module, so one unlink
        # always returns the record to the state the register left it in.
        before: dict[str, Any] = (
            dict(last.before or {}) if ours and last is not None else {"sponsor_org_id": None}
        )
        if ours:
            before["replaced_sponsor_org_id"] = str(current)
        proposal.sponsor_org_id = org.id
        proposal.last_changed = utcnow()
        report.events.append(
            _write_event(
                session,
                proposal,
                event_type=LINK_EVENT,
                before=before,
                after={
                    "sponsor_org_id": str(org.id),
                    "organization": {"public_id": org.public_id, "name_canonical": org.name_canonical},
                    "organization_created": created,
                    "operator_key": rule.key,
                    "registry_version": reg.version,
                    "evidence": evidence,
                },
                quartet=_quartet(session, own_links, triggering),
                reason=f"the record's name names the operator ({rule.key}, data_centre_operators.yaml)",
                sequence=len(past) + 1,
            )
        )
        report.relinked += int(ours)
        report.linked += int(not ours)
    return report


def _unlink(
    session: Session,
    proposal: Proposal,
    link_event: Event,
    prior: int,
    *,
    suppress: bool,
    reason: str,
    actor_type: str = "pipeline",
) -> Event:
    before_sponsor = _as_uuid((link_event.before or {}).get("sponsor_org_id"))
    current = proposal.sponsor_org_id
    proposal.sponsor_org_id = before_sponsor
    proposal.last_changed = utcnow()
    return _write_event(
        session,
        proposal,
        event_type=UNLINK_EVENT,
        before={"sponsor_org_id": str(current) if current is not None else None},
        after={
            "sponsor_org_id": str(before_sponsor) if before_sponsor is not None else None,
            "suppress": suppress,
        },
        quartet=provenance.of_event(link_event),
        reason=reason,
        sequence=prior + 1,
        actor_type=actor_type,
        reverses=link_event,
    )


def unlink(session: Session, link_event_id: _uuid.UUID, *, reason: str = "unlink") -> Event:
    """Reverse one `sponsor_linked` event: the sponsor goes back to the event's `before`, and the pass
    never links that record again (`suppress`). Idempotent: a second call returns the first reversal.
    Refuses an event that is not the record's latest link (a later link or unlink supersedes it)."""
    link_event = session.get(Event, link_event_id)
    if link_event is None or link_event.event_type != LINK_EVENT:
        raise ValueError(f"{link_event_id} is not a {LINK_EVENT} event")
    existing = session.scalar(select(Event).where(Event.reverses_event_id == link_event.id))
    if existing is not None:
        return existing
    proposal = session.get(Proposal, link_event.subject_id)
    if proposal is None:
        raise ValueError(f"proposal {link_event.subject_id} is gone")
    past = _history(session, [proposal.id]).get(proposal.id, [])
    if not past or past[-1].id != link_event.id:
        raise ValueError(f"{link_event_id} is not the latest operator link of proposal {proposal.id}")
    return _unlink(session, proposal, link_event, len(past), suppress=True, reason=reason, actor_type="user")


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.resolve.operators",
        description="Link large-load records to the operator their names name (docs/22 §27).",
    )
    parser.add_argument("--db", type=pathlib.Path, default=None, help="SQLite path; default DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args(argv)
    from services.db.session import get_engine, get_sessionmaker

    url = f"sqlite+pysqlite:///{args.db}" if args.db else os.environ.get("DATABASE_URL")
    if not url:
        parser.error("set DATABASE_URL or pass --db")
    with get_sessionmaker(get_engine(url))() as session:
        report = link_data_centre_operators(session, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
    print(json.dumps(report.as_dict(), default=str))  # noqa: T201 -- CLI summary line
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
