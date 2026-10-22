"""Refresh `organization_stats` (migration 0038) from the store: `services/api/org_stats.py`
computes every row the table should hold, and this writes only the difference, in one transaction.

    python -m services.api.org_stats_refresh                        # the store DATABASE_URL names
    python -m services.api.org_stats_refresh --db path/to/dev.db    # a SQLite dev store
    python -m services.api.org_stats_refresh --if-stale             # only when the gate key moved

**When it runs.** At the end of every resolve pass and after every context load
(`infra/scheduler/jobs.py::default_resolve`, `context_load_job`): those are the passes that change
what a company holds (assets and their owner edges; merged organisations and proposals, suppressions,
natural-person flags). A load between them adds rows the figures pick up at the next resolve pass,
which every load chains. `org_stats_tick` (every few minutes) refreshes only when the gate key has
moved, so figures an operator's takedown withdrew from the list come back without waiting for one.

**Idempotent.** A row is written only when a figure or the gate key changed; a second refresh over
the same store writes nothing (`RefreshReport.written == 0`). Rows of an organisation that is no
longer listed (merged, taken down, or named only by a register now hidden) are deleted.

**Cost.** Measured 2026-10-22 on a copy of the 2026-10-20 store (8,454 listed organisations, 10,023
edges, 11,176 proposals, 1,349 opportunities; SQLite, one core), median of three: 4.3 s for a first
refresh into an empty table (33,816 rows inserted), 3.9 s for a second over the same store (nothing
written). About 3 s of either is reading and folding the store (`compute`); the rest is reading the
table back and comparing. At 12,000 synthetic companies with 1,200 parents, 9.9 s for a first
refresh: the cost grows with rows, not with trees (the group walk is in memory). It is a small part
of the passes it follows (a resolve pass measured 43 s, a context load 190 s; `infra/scheduler/jobs.py`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.org_stats import compute, forget_gate_key, gate_key
from services.api.visibility import organization_visibility_filter
from services.db.models import Organization, OrganizationStats, utcnow

log = logging.getLogger("services.api.org_stats_refresh")

#: Rows per DELETE statement (SQLite's host-parameter limit is 32,766; three per row).
_DELETE_CHUNK = 500
_TABLE = cast(sa.Table, OrganizationStats.__table__)
_KEY_COLUMNS = ("organization_id", "scope", "tier")
_MW_COLUMNS = frozenset({"operating_mw", "owned_mw", "pipeline_mw"})


@dataclass(frozen=True)
class RefreshReport:
    rows: int
    inserted: int
    updated: int
    rekeyed: int
    deleted: int
    unchanged: int
    gate_key_changed: bool
    elapsed_s: float

    @property
    def written(self) -> int:
        return self.inserted + self.updated + self.rekeyed + self.deleted

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "written": self.written}


def _comparable(column: str, value: Any) -> Any:
    if column in _MW_COLUMNS:
        return None if value is None else round(float(value), 3)
    return value


def _same(stored: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    return all(
        _comparable(column, stored[column]) == _comparable(column, value)
        for column, value in values.items()
        if column not in _KEY_COLUMNS
    )


def _stored_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row["organization_id"]), row["scope"], row["tier"])


def refresh(session: Session, *, now: dt.datetime | None = None) -> RefreshReport:
    """Bring the table to what `compute` says, writing only what differs (module docstring)."""
    started = time.monotonic()
    now = now or utcnow()
    key, rows = compute(session)
    stored: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in session.execute(select(_TABLE)):
        row = dict(record._mapping)
        stored[_stored_key(row)] = row
    previous_keys = {row["gate_key"] for row in stored.values()}
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    rekeys: list[dict[str, Any]] = []
    unchanged = 0
    for stats in rows:
        values = stats.columns()
        current = stored.pop(stats.key, None)
        if current is None:
            inserts.append({**values, "gate_key": key, "computed_at": now})
        elif not _same(current, values):
            updates.append({**values, "gate_key": key, "computed_at": now})
        elif current["gate_key"] != key:
            rekeys.append({**{c: values[c] for c in _KEY_COLUMNS}, "gate_key": key})
        else:
            unchanged += 1
    if inserts:
        session.execute(sa.insert(OrganizationStats), inserts)
    if updates:
        session.execute(sa.update(OrganizationStats), updates)
    if rekeys:
        session.execute(sa.update(OrganizationStats), rekeys)
    gone = [tuple(row[c] for c in _KEY_COLUMNS) for row in stored.values()]
    for start in range(0, len(gone), _DELETE_CHUNK):
        chunk = gone[start : start + _DELETE_CHUNK]
        session.execute(
            sa.delete(OrganizationStats).where(
                sa.tuple_(
                    OrganizationStats.organization_id, OrganizationStats.scope, OrganizationStats.tier
                ).in_(chunk)
            )
        )
    session.flush()
    report = RefreshReport(
        rows=len(rows),
        inserted=len(inserts),
        updated=len(updates),
        rekeyed=len(rekeys),
        deleted=len(gone),
        unchanged=unchanged,
        gate_key_changed=previous_keys != {key} and bool(rows or previous_keys),
        elapsed_s=round(time.monotonic() - started, 3),
    )
    log.info("organization stats: %s", report.as_dict(), extra={"job": "org_stats", **report.as_dict()})
    return report


def is_stale(session: Session) -> bool:
    """True when a row was written under another gate key than the current one, or the table is
    empty while organisations exist (never refreshed): what `org_stats_tick` refreshes on."""
    keys = set(session.scalars(select(OrganizationStats.gate_key).distinct()))
    if not keys:
        listed = select(Organization.id).where(
            Organization.merged_into_id.is_(None), *organization_visibility_filter()
        )
        return session.scalar(select(listed.exists())) is True
    return keys != {gate_key(session)}


def run(session_factory: Any, *, if_stale: bool = False) -> dict[str, Any]:
    """One refresh in a transaction of its own, reported as counts (the scheduler's hook). With
    `if_stale`, a store whose rows are current under the gate key is left untouched."""
    from services.db.session import session_scope

    with session_scope(session_factory) as session:
        if if_stale and not is_stale(session):
            return {"skipped": "current"}
        report = refresh(session)
    forget_gate_key()
    return report.as_dict()


def _ensure_table(engine: sa.Engine) -> None:
    """A SQLite dev store built before migration 0038 (`web.dev_up --skip-load` reuses one): create
    the one table, as `init_db` would have. On Postgres the migration is the only path."""
    if sa.inspect(engine).has_table(_TABLE.name):
        return
    if engine.dialect.name != "sqlite":
        raise SystemExit("organization_stats is missing: run the migrations (alembic upgrade head) first")
    _TABLE.create(engine)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.api.org_stats_refresh", description=__doc__)
    parser.add_argument("--db", help="a SQLite store file (default: the store DATABASE_URL names)")
    parser.add_argument("--if-stale", action="store_true", help="refresh only when the gate key moved")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from services.db.session import get_engine, get_sessionmaker

    engine = get_engine(f"sqlite+pysqlite:///{args.db}" if args.db else None)
    _ensure_table(engine)
    report = run(get_sessionmaker(engine), if_stale=args.if_stale)
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


__all__ = ["RefreshReport", "is_stale", "main", "refresh", "run"]

if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
