"""Apply the natural-person rule to the store (2026-09-30 legal audit L-5; `docs/13` §5.5).

Two passes, both idempotent and both reversible:

**`classify_organizations(session)`** re-applies `services.personal_names.classify` to every
organisation and writes `personal_data` / `personal_data_basis` where they differ. New rows are
classified at insert by the column default (`services/db/models.py`); this pass is for existing
rows when the rule or `data/vendored/organizations/personal_data_overrides.yaml` changes, and is
what makes a curated correction take effect (remove the line, re-run, and the row is back as it was).

**`redact_person_addresses(session)`** finds proposals sponsored by a flagged organisation whose
name carries a street address ("NY - 12 Example Hill Rd - 2": a person's name beside what is
likely their home or land) and replaces the address with `[street address withheld]` as an
**admin-style override**: `overrides["name_canonical"]` records the served value, the event that
set it and the basis, so the loader leaves the field alone on every later load (`loader.py`
`_update_existing_entity`, L-1) and the served view serves it as made (`visibility.GatedRecord`).
Reversal is the ordinary admin route: `PATCH /admin/v1/proposals/{id}` with
`clear_overrides: ["name_canonical"]`; the next load restores the register's spelling. The record
of the redaction is a non-public `admin_edit` event (`actor_type="system"`, no `published_at`)
whose `before` holds no copy of the address, because an audit trail that kept the address would
defeat the redaction.

What this does **not** do: re-slug. A proposal's slug was minted from its first name
(`ny-12-example-hill-rd-2-...`) and still carries the address in its URL. Changing it needs a
slug-history redirect the proposal table does not have (only organisations and merges have one);
recorded as an open item in `docs/13` §5.5.

CLI: `python -m services.ingest.personal_data classify|redact|all [--db PATH] [--dry-run]`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Event, Organization, Proposal
from services.db.session import get_engine, get_sessionmaker
from services.ids import public_id as make_public_id
from services.personal_names import classify

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
REDACTION = "[street address withheld]"
REDACTION_BASIS = "personal_data_street_address"
REDACTION_REASON = (
    "Street address cut from a proposal name sponsored by a natural person (docs/13 §5.5; "
    "2026-09-30 legal audit L-5). Reversible: clear the name_canonical override."
)

#: A house number, up to five words, and a street designator: "12 Example Hill Rd",
#: "1200 N. County Road 5", "8 Old Mill Lane". Deliberately narrow: a capacity ("74 MW") or a
#: phase ("Phase 2") has no street word after it, so it is not touched.
STREET_ADDRESS = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'\-]+\s+){0,4}?"
    r"(?:Rd|Road|St|Street|Ave|Avenue|Ln|Lane|Dr|Drive|Hwy|Highway|Blvd|Boulevard|Way|Ct|Court|"
    r"Pl|Place|Pike|Tpke|Turnpike|Rte|Route|Cir|Circle|Pkwy|Parkway|Ter|Terrace|Trl|Trail)\b\.?"
    r"(?:\s+\d{1,4}[A-Za-z]?\b)?",  # "County Road 5", "Route 9W"
    re.IGNORECASE,
)


def redact_street_address(name: str) -> str | None:
    """`name` with every street address replaced, or `None` when it carries none."""
    redacted, count = STREET_ADDRESS.subn(REDACTION, name)
    return redacted if count else None


@dataclass
class ClassifyReport:
    organizations: int = 0
    flagged: int = 0
    changed: int = 0
    by_basis: dict[str, int] = field(default_factory=dict)


def classify_organizations(session: Session, *, dry_run: bool = False) -> ClassifyReport:
    report = ClassifyReport()
    for org in session.scalars(select(Organization)).all():
        result = classify(org.name_canonical)
        report.organizations += 1
        report.by_basis[result.basis] = report.by_basis.get(result.basis, 0) + 1
        if result.personal:
            report.flagged += 1
        if org.personal_data != result.personal or org.personal_data_basis != result.basis:
            report.changed += 1
            if not dry_run:
                org.personal_data = result.personal
                org.personal_data_basis = result.basis
    if not dry_run:
        session.flush()
    return report


@dataclass
class RedactReport:
    candidates: int = 0
    redacted: list[str] = field(default_factory=list)
    already: int = 0


def redact_person_addresses(
    session: Session, *, dry_run: bool = False, now: dt.datetime | None = None
) -> RedactReport:
    """Override the name of every proposal sponsored by a `personal_data` organisation whose name
    carries a street address. A proposal whose name an operator already overrode is left alone:
    that is a human decision, and this pass never overwrites one."""
    now = now or dt.datetime.now(dt.UTC)
    report = RedactReport()
    rows = session.scalars(
        select(Proposal)
        .join(Organization, Proposal.sponsor_org_id == Organization.id)
        .where(Organization.personal_data.is_(True))
    ).all()
    for proposal in rows:
        report.candidates += 1
        overrides = dict(proposal.overrides or {})
        if "name_canonical" in overrides:
            report.already += 1
            continue
        redacted = redact_street_address(proposal.name_canonical)
        if redacted is None:
            continue
        report.redacted.append(proposal.public_id)
        if dry_run:
            continue
        event = Event(
            subject_type="proposal",
            subject_id=proposal.id,
            event_type="admin_edit",
            observed_at=now,
            published_at=None,
            public_at=None,
            before={"name_canonical": "[withheld: contained a street address]"},
            after={"name_canonical": redacted},
            changed_keys=["name_canonical"],
            actor_type="system",
            reason=REDACTION_REASON,
            idempotency_key=f"personal_data:redact:{proposal.id}:{_uuid.uuid4()}",
        )
        session.add(event)
        session.flush()
        overrides["name_canonical"] = {
            "value": redacted,
            "event_id": make_public_id("evt", event.id),
            "set_at": now.isoformat(),
            "user_id": None,
            "basis": REDACTION_BASIS,
        }
        proposal.overrides = overrides
        proposal.name_canonical = redacted
        proposal.last_changed = now
    if not dry_run:
        session.flush()
    return report


def run(session: Session, *, what: str, dry_run: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if what in ("classify", "all"):
        c = classify_organizations(session, dry_run=dry_run)
        out["classify"] = {
            "organizations": c.organizations,
            "flagged": c.flagged,
            "changed": c.changed,
            "by_basis": c.by_basis,
        }
    if what in ("redact", "all"):
        r = redact_person_addresses(session, dry_run=dry_run)
        out["redact"] = {
            "candidates": r.candidates,
            "redacted": len(r.redacted),
            "already_overridden": r.already,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the natural-person rule to the store (docs/13 §5.5).")
    parser.add_argument("what", choices=("classify", "redact", "all"))
    parser.add_argument("--db", type=pathlib.Path, default=None, help="SQLite path; default DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    url = f"sqlite+pysqlite:///{args.db}" if args.db else os.environ.get("DATABASE_URL")
    if not url:
        url = f"sqlite+pysqlite:///{DEFAULT_DB_PATH}"
    sessionmaker_ = get_sessionmaker(get_engine(url))
    with sessionmaker_() as session:
        result = run(session, what=args.what, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
    print(json.dumps(result))  # noqa: T201 — CLI summary line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
