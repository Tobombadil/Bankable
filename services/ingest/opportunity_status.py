"""An opportunity's served status follows its deadline, whether or not its source has run since
(docs/21 §7.2 "open --> closed: deadline passed"; docs/22 §8.2; audit 2026-10-07 DATA-14 / UX-2).

The runner closes an `open` notice whose `due_at` has passed at each run of its source
(`pipeline.connectors.opportunity.close_past_deadline`), so the stored status lagged a deadline by up
to one run of that source, and for ever when the source stopped running: on the 2026-10-07 dev store
121 notices were served `open` with a past deadline (TED 68, World Bank 22, grants.gov 31), and the
default "open, soonest deadline first" list led with them.

The same rule now runs against the clock rather than a fetch time, as a store sweep,
`close_past_deadline`, in two places:

* at the end of every opportunity load, over the records that load touched, at the load's own time
  (`services/ingest/loader.py::load_dataframe`), so a frame loaded after its rows' deadlines never
  leaves them served `open`; the link row keeps what the source stated (`normalised.status`);
* hourly over the whole store, for the time that passes between loads (`infra/scheduler/app.py`
  `deadline_tick`), and by hand:

      python -m services.ingest.opportunity_status [--now 2026-10-07T18:00:00Z] [--db web/.data/dev.db]

Neither writes an event. The change is still news, and it is published where docs/22 §8.2 put it:
the source's next run diffs its previous frame (`open`) against the closed one and emits the
`status_change` open -> closed, which the loader writes as an event on a record that already reads
`closed`. A source that never runs again closes its notices without an event; that is the read-time
rule's behaviour too, and the record's `field_provenance.status.rule` says why it reads `closed`.

What this does not decide: a notice with no `due_at` keeps the status its source states (45 `open`
on the dev store: TED 29, grants.gov 16). docs/21 §7.2 has no rule that closes an undated notice and
none is invented here; its `due_at` is served as null, so a reader sees there is no stated deadline.
An operator override of `status` (docs/21 §6.4) is never touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.opportunity import DEADLINE_PASSED_RULE
from services.db.models import Opportunity


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def served_status(status: str | None, due_at: dt.datetime | None, now: dt.datetime) -> str:
    """The status a notice is served with at `now`: `closed` when its source says `open` and its
    deadline is before `now` (docs/21 §7.2), else the source's own status. An undated notice keeps
    what its source says."""
    stated = status or "unknown"
    if stated == "open" and due_at is not None and _aware(due_at) < _aware(now):
        return "closed"
    return stated


def deadline_provenance(entry: dict[str, Any] | None, now: dt.datetime) -> dict[str, Any]:
    """`field_provenance.status` for a status the deadline rule set: the source's quartet as it
    was, plus the rule and when it was applied."""
    out = dict(entry or {})
    out["rule"] = DEADLINE_PASSED_RULE
    out["derived_at"] = _aware(now).astimezone(dt.UTC).isoformat()
    return out


@dataclass
class DeadlineReport:
    closed: int = 0
    skipped_override: int = 0
    by_source: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed": self.closed,
            "skipped_override": self.skipped_override,
            "by_source": dict(sorted(self.by_source.items())),
        }


#: Ids per `IN (...)` when the sweep is limited to the records one load touched.
_ID_CHUNK = 500


def close_past_deadline(
    session: Session, now: dt.datetime | None = None, *, ids: Iterable[uuid.UUID] | None = None
) -> DeadlineReport:
    """Set every live `open` opportunity (or every one among `ids`) whose `due_at` is before `now`
    to `closed` (module docstring). Moves `last_changed`, stamps `field_provenance.status` with the
    rule, writes no event. Idempotent: a second sweep at the same or a later time finds nothing
    new to close."""
    at = _aware(now or dt.datetime.now(dt.UTC))
    report = DeadlineReport()
    base = select(Opportunity).where(
        Opportunity.status == "open",
        Opportunity.due_at.is_not(None),
        Opportunity.due_at < at,
        Opportunity.merged_into_id.is_(None),
    )
    if ids is None:
        rows: list[Opportunity] = list(session.scalars(base))
    else:
        wanted = list(dict.fromkeys(ids))
        rows = []
        for i in range(0, len(wanted), _ID_CHUNK):
            rows.extend(session.scalars(base.where(Opportunity.id.in_(wanted[i : i + _ID_CHUNK]))))
    for opp in rows:
        if "status" in (opp.overrides or {}):
            report.skipped_override += 1
            continue
        provenance = dict(opp.field_provenance or {})
        provenance["status"] = deadline_provenance(provenance.get("status"), at)
        opp.status = "closed"
        opp.field_provenance = provenance  # reassigned so the JSON column is marked dirty
        opp.last_changed = at
        report.closed += 1
        report.by_source[str((provenance["status"] or {}).get("source_id") or "unknown")] += 1
    session.flush()
    return report


def _parse_now(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return _aware(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI over a tested function
    ap = argparse.ArgumentParser(prog="python -m services.ingest.opportunity_status")
    ap.add_argument("--now", default=None, help="ISO time to judge deadlines at (default: now, UTC)")
    ap.add_argument(
        "--db", type=Path, default=Path("web/.data/dev.db"), help="SQLite file without DATABASE_URL"
    )
    args = ap.parse_args(argv)
    from services.db.session import get_engine, get_sessionmaker

    engine = get_engine(os.environ.get("DATABASE_URL") or f"sqlite+pysqlite:///{args.db}")
    with get_sessionmaker(engine)() as session:
        report = close_past_deadline(session, _parse_now(args.now))
        session.commit()
    sys.stdout.write(json.dumps(report.to_dict()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
