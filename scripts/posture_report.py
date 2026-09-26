#!/usr/bin/env python3
"""Which sources hold rows under the `noncommercial` reuse class, and how many (docs/26 §5).

The first thing the runbook for switching the platform posture back to `commercial` asks for:
after the flip makes every `noncommercial` row invisible on every non-admin surface (that is the
predicate, `services/api/visibility.py`), the purge-or-relicence decision is taken **per source**,
and this is the table it is taken from:

    SELECT source_id, count(*) FROM <table> JOIN licence ... WHERE licence.reuse_class = 'noncommercial'
    GROUP BY source_id

run over every table that carries a `licence_id` next to a `source_id` (`proposal_source`,
`opportunity_source`, `event`, `asset`, `asset_owner`, `location`, `organization_alias`,
`document`, `extraction`, `snapshot`), plus the `noncommercial` licences themselves with the
sources pointing at them. Read-only by construction: the only statements issued are the
SELECTs above, on a connection that never begins a write. It prints the table and exits 0;
`--json` prints the same rows as JSON for a script. It prints "none" and exits 0 when no row
carries the class, which is what a `commercial` deployment that never ingested one should see.

Usage:  python scripts/posture_report.py [--database-url URL] [--json]
        (defaults to `DATABASE_URL` from the environment, as every other store entrypoint does)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

import sqlalchemy as sa

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.db.models import (  # noqa: E402 -- sys.path must be set first
    Asset,
    AssetOwner,
    Document,
    Event,
    Extraction,
    Licence,
    Location,
    OpportunitySource,
    OrganizationAlias,
    ProposalSource,
    Snapshot,
    Source,
)

REUSE_CLASS = "noncommercial"

#: Every table with a `licence_id` beside a `source_id` (docs/21 invariant L2: the licence that
#: applied at fetch time is copied onto each observation row, which is exactly why a row can be
#: found by its own licence rather than by its source's current one).
LINKED_TABLES: tuple[Any, ...] = (
    ProposalSource,
    OpportunitySource,
    Event,
    Asset,
    AssetOwner,
    Location,
    OrganizationAlias,
    Document,
    Extraction,
    Snapshot,
)


def collect(engine: sa.Engine) -> dict[str, Any]:
    """`{"licences": [...], "rows": [...]}` — the noncommercial licences with their sources, and
    one `(table, source_id, count)` per table/source pair that holds rows under the class."""
    licences: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        lic_stmt = (
            sa.select(Licence.id, Licence.name, Source.id.label("source_id"))
            .select_from(Licence)
            .outerjoin(Source, Source.licence_id == Licence.id)
            .where(Licence.reuse_class == REUSE_CLASS)
            .order_by(Licence.id, Source.id)
        )
        for licence_id, name, source_id in conn.execute(lic_stmt):
            licences.append({"licence_id": licence_id, "name": name, "source_id": source_id})
        for model in LINKED_TABLES:
            table = model.__table__
            stmt = (
                sa.select(table.c.source_id, sa.func.count().label("n"))
                .select_from(table)
                .join(Licence, Licence.id == table.c.licence_id)
                .where(Licence.reuse_class == REUSE_CLASS)
                .group_by(table.c.source_id)
                .order_by(table.c.source_id)
            )
            for source_id, n in conn.execute(stmt):
                rows.append({"table": table.name, "source_id": source_id, "rows": int(n)})
    return {"reuse_class": REUSE_CLASS, "licences": licences, "rows": rows}


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if not report["licences"] and not report["rows"]:
        return f"none: no licence and no row carries reuse_class = {REUSE_CLASS!r}\n"
    lines.append(f"licences with reuse_class = {REUSE_CLASS!r}:")
    for lic in report["licences"]:
        lines.append(f"  {lic['licence_id']}  ({lic['name']})  source: {lic['source_id'] or '-'}")
    lines.append("")
    lines.append(f"{'table':<20} {'source_id':<44} {'rows':>8}")
    total = 0
    for row in report["rows"]:
        lines.append(f"{row['table']:<20} {row['source_id'] or '-':<44} {row['rows']:>8,}")
        total += row["rows"]
    lines.append(f"{'total':<20} {'':<44} {total:>8,}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("no database URL: pass --database-url or set DATABASE_URL")
    engine = sa.create_engine(args.database_url, future=True)
    try:
        report = collect(engine)
    finally:
        engine.dispose()
    sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
