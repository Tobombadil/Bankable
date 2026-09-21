"""Reproduce every number in docs/22 §18 from a loaded store, and write the distribution to
`data/eval/slippage_distribution.csv`.

Why this exists as a script rather than as a paragraph of ad-hoc queries: the 90-day grace period
in `services/api/slippage.py` is a judgement call justified by one table (flagged count and
EIA-860M share against grace period), and that table has to be repeatable on the next load. If a
register changes its date granularity or its publication lag, the shape of the curve changes and
the constant should be revisited -- this is the command that shows it.

    DATABASE_URL="sqlite+pysqlite:///$PWD/web/.data/dev.db" .venv/bin/python scripts/measure_slippage.py

Reads only; writes one CSV.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import os
import pathlib
import sys

# Run from anywhere: `services` lives at the repo root, not beside this file.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.api.slippage import (
    SLIP_ACTIVE_STATES,
    SLIP_BUCKETS,
    slip_bucket,
    slip_filter,
)
from services.db.models import Proposal, ProposalSource
from services.db.session import get_engine

GRACE_SWEEP = (0, 31, 60, 90, 120, 180, 365)
DEFAULT_OUT = pathlib.Path("data/eval/slippage_distribution.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--on", default=None, help="Evaluate as at this date (default: today, UTC).")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("set DATABASE_URL or pass --database-url")
    on = dt.date.fromisoformat(args.on) if args.on else dt.datetime.now(dt.UTC).date()

    with Session(get_engine(args.database_url)) as db:
        total = db.scalar(sa.select(sa.func.count()).select_from(Proposal)) or 0
        dated = (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(Proposal)
                .where(Proposal.proposed_online_date.is_not(None))
            )
            or 0
        )
        # One row per (proposal, active source) so the per-source share is attributable.
        rows = db.execute(
            sa.select(Proposal.lifecycle_state, Proposal.proposed_online_date, ProposalSource.source_id)
            .join(ProposalSource, ProposalSource.proposal_id == Proposal.id)
            .where(
                ProposalSource.active.is_(True),
                Proposal.proposed_online_date.is_not(None),
                Proposal.proposed_online_date < on,
                Proposal.lifecycle_state.in_(SLIP_ACTIVE_STATES),
            )
        ).all()

        print(f"as at {on}: {total} proposals, {dated} with a proposed_online_date")
        print(f"active and past their date (no grace): {len(rows)}\n")

        out_rows: list[dict[str, object]] = []
        print(f"{'grace':>6} {'flagged':>8} {'eia-860m':>9} {'under_constr':>13}")
        for grace in GRACE_SWEEP:
            kept = [r for r in rows if (on - r.proposed_online_date).days > grace]
            eia = sum(1 for r in kept if r.source_id == "us.eia.860m")
            uc = sum(1 for r in kept if r.lifecycle_state == "under_construction")
            print(f"{grace:>5}d {len(kept):>8} {eia:>9} {uc:>13}")
            out_rows.append(
                {"grace_days": grace, "flagged": len(kept), "eia_860m": eia, "under_construction": uc}
            )

        print("\nbuckets at the shipped grace, from the SQL twin:")
        for name in SLIP_BUCKETS:
            n = db.scalar(
                sa.select(sa.func.count())
                .select_from(Proposal)
                .where(slip_filter(slipped=None, buckets=[name], on=on))
            )
            print(f"  {name:>9}: {n}")

        shipped = [r for r in rows if (on - r.proposed_online_date).days > 90]
        print("\nat the shipped grace, by source:")
        for source_id, n in collections.Counter(r.source_id for r in shipped).most_common():
            print(f"  {source_id:>28}: {n}")
        print("\nartefact check -- target dates of the no-grace set, most common first:")
        for date, n in collections.Counter(r.proposed_online_date for r in rows).most_common(5):
            print(f"  {date}: {n}")
        print("\nbucket spread of the no-grace set (what the grace is discarding):")
        spread: collections.Counter[str] = collections.Counter()
        for r in rows:
            late = (on - r.proposed_online_date).days
            spread["within grace" if late <= 90 else slip_bucket(late)] += 1
        for name, n in spread.most_common():
            print(f"  {name:>13}: {n}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["grace_days", "flagged", "eia_860m", "under_construction"])
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
