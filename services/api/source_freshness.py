"""Per-source run facts for the public surfaces: last successful fetch, last promoted (loaded) run,
and freshness against the poll allowance (expert review 2026-10-07, interconnection analyst finding:
`/v1/sources` answered `last_success_at: null` for every source while every source had a recorded
successful run).

`source.last_success_at` is written only by the scheduler's connector runner, so a source fetched
through the CLI or the dev/file path keeps NULL there although its `source_run` row says `ok`. The
answer here is the newer of the two: the column, and the end of the newest `source_run` whose status
counts as a success (`infra/scheduler/freshness.py::SUCCESS_STATUSES`, the set the scheduler's own
freshness tick uses). Freshness is that module's `assess`, the same computation the scheduler and
the admin source-health page apply, with the manifest's `poll` override.

`last_loaded_at` is `source.last_loaded_ts`, the snapshot token of the newest run whose load
committed (migration 0032): the run whose rows the site is serving, which can be older than the last
successful fetch when a fetch found nothing new or its load has not run yet.

One grouped query over `source_run` (indexed on `(source_id, started_at)`), however many sources.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from infra.scheduler.freshness import SUCCESS_STATUSES, assess, manifest_poll
from services.api.common import iso
from services.db.models import Source, SourceRun

#: The `SourceFreshness` keys a public surface prints (api/openapi.yaml).
FRESHNESS_KEYS = (
    "status",
    "bucket",
    "last_success_at",
    "age_hours",
    "allowance_hours",
    "stale_after_hours",
    "alert",
)


@dataclass(frozen=True)
class SourceRunFacts:
    last_success_at: dt.datetime | None
    last_loaded_at: dt.datetime | None
    freshness: dict[str, Any]

    def as_fields(self) -> dict[str, Any]:
        return {
            "last_success_at": iso(self.last_success_at),
            "last_loaded_at": iso(self.last_loaded_at),
            "freshness": self.freshness,
        }


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def loaded_at(token: str | None) -> dt.datetime | None:
    """`YYYYMMDDTHHMMSSZ` (a snapshot token) as an instant; `None` for NULL or anything else."""
    if not token:
        return None
    try:
        return dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def successful_run_ends(db: Session, source_ids: Iterable[str] | None = None) -> dict[str, dt.datetime]:
    """The end (else the start) of the newest successful `source_run` per source."""
    ended = func.max(func.coalesce(SourceRun.finished_at, SourceRun.started_at))
    stmt = (
        select(SourceRun.source_id, ended)
        .where(SourceRun.status.in_(SUCCESS_STATUSES))
        .group_by(SourceRun.source_id)
    )
    if source_ids is not None:
        stmt = stmt.where(SourceRun.source_id.in_(list(source_ids)))
    out: dict[str, dt.datetime] = {}
    for source_id, value in db.execute(stmt).all():
        if isinstance(value, str):  # SQLite returns a coalesce()'d timestamp as text
            value = dt.datetime.fromisoformat(value)
        if (aware := _aware(value)) is not None:
            out[str(source_id)] = aware
    return out


def facts_for(source: Source, run_ends: dict[str, dt.datetime], now: dt.datetime) -> SourceRunFacts:
    candidates = [v for v in (_aware(source.last_success_at), run_ends.get(source.id)) if v is not None]
    last = max(candidates) if candidates else None
    fr = assess(
        manifest_poll(source.id) or source.cadence or "",
        last,
        now,
        scheduled=bool(source.implemented),
        paused=bool(source.paused),
    ).to_dict()
    return SourceRunFacts(last, loaded_at(source.last_loaded_ts), {k: fr[k] for k in FRESHNESS_KEYS})


def source_run_facts(
    db: Session, sources: Iterable[Source], now: dt.datetime | None = None
) -> dict[str, SourceRunFacts]:
    rows = list(sources)
    now = now or dt.datetime.now(dt.UTC)
    ends = successful_run_ends(db, [s.id for s in rows])
    return {s.id: facts_for(s, ends, now) for s in rows}


def oldest_and_newest_success(db: Session) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Over the sources whose rows the site serves (a load has committed: `last_loaded_ts` set), the
    oldest and newest last successful fetch: everything served was fetched no earlier than the first
    and no later than the second (`GET /v1/health` `data_as_of`)."""
    loaded = list(db.scalars(select(Source).where(Source.last_loaded_ts.is_not(None))))
    if not loaded:
        return None, None
    facts = source_run_facts(db, loaded)
    times = [f.last_success_at or f.last_loaded_at for f in facts.values()]
    known = [t for t in times if t is not None]
    if not known:
        return None, None
    return min(known), max(known)


__all__ = [
    "FRESHNESS_KEYS",
    "SourceRunFacts",
    "facts_for",
    "loaded_at",
    "oldest_and_newest_success",
    "source_run_facts",
    "successful_run_ends",
]
