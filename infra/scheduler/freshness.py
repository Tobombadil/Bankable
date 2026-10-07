"""Source freshness: how old a source's last successful run is against how often we poll it
(2026-10-07, audit 2026-09-30 data engineer F2).

The audit found nine implemented sources 16.7 days stale with every `source` row reading
`health = ok`: health only counts consecutive *failures*, so a source the scheduler stops running
(no worker, a dead tick, a bucket that never fires) stays green for ever. Freshness is the other
half: the age of `source.last_success_at` (a run that ended `ok` or `unchanged`) measured in units
of the source's poll allowance.

| status        | meaning                                                                       |
|---------------|-------------------------------------------------------------------------------|
| `fresh`       | the last success is within the allowance for its poll bucket                  |
| `late`        | older than the allowance, not yet twice it: shown, no alert                   |
| `stale`       | older than `STALE_FACTOR` x the allowance: the ops signal (`freshness_tick`)  |
| `never`       | an implemented, scheduled source with no successful run on record            |
| `paused`      | an operator paused it: not expected to be fresh                               |
| `unscheduled` | no connector runs it (a manifest-only or context-loaded source)               |

The allowance per bucket (`ALLOWANCE`) is the bucket's interval plus room for one missed tick and
a slow run: 15 minutes -> 1 hour, hourly -> 3 hours, daily -> 2 days, weekly -> 8 days, monthly
-> 32 days, quarterly -> 93 days, annual -> 397 days (a year plus the run month's slack). A
source's bucket comes from its poll cadence (`cadence.poll_cadence`), so the allowance follows the
schedule the source is actually on.

Pure functions (`assess`) are shared by the scheduler's `freshness_tick` (from `source` rows), the
admin source-health API and the methodology data (`services/api`), and the store report below,
which reads run records and needs no database:

    python -m infra.scheduler.freshness [--now 2026-10-07T12:00:00Z] [--data-dir data]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from infra.scheduler.cadence import bucket_for_cadence

ALLOWANCE: dict[str, dt.timedelta] = {
    "15min": dt.timedelta(hours=1),
    "hourly": dt.timedelta(hours=3),
    "daily": dt.timedelta(days=2),
    "weekly": dt.timedelta(days=8),
    "monthly": dt.timedelta(days=32),
    "quarterly": dt.timedelta(days=93),
    "annual": dt.timedelta(days=397),
}
#: Multiples of the allowance past which a source is `stale` and the ops signal fires.
STALE_FACTOR = 2.0
FRESHNESS_STATES = ("fresh", "late", "stale", "never", "paused", "unscheduled")
#: Run statuses that count as a success (`infra/scheduler/jobs.py::_update_health`).
SUCCESS_STATUSES = ("ok", "unchanged")


@dataclass(frozen=True)
class Freshness:
    status: str
    bucket: str | None
    last_success_at: dt.datetime | None
    age_hours: float | None
    allowance_hours: float | None
    stale_after_hours: float | None

    @property
    def alert(self) -> bool:
        """The ops signal: stale, or a scheduled source that has never succeeded."""
        return self.status in ("stale", "never")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["last_success_at"] = (
            self.last_success_at.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            if self.last_success_at
            else None
        )
        out["alert"] = self.alert
        return out


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def assess(
    poll: str,
    last_success_at: dt.datetime | None,
    now: dt.datetime,
    *,
    scheduled: bool = True,
    paused: bool = False,
) -> Freshness:
    """Freshness of one source polled at cadence string `poll` (module docstring)."""
    bucket = bucket_for_cadence(poll).bucket
    allowance = ALLOWANCE[bucket]
    allowance_h = round(allowance.total_seconds() / 3600.0, 2)
    stale_h = round(allowance_h * STALE_FACTOR, 2)
    last = _aware(last_success_at) if last_success_at is not None else None
    age_h = round((now - last).total_seconds() / 3600.0, 2) if last is not None else None
    if not scheduled:
        status = "unscheduled"
    elif paused:
        status = "paused"
    elif last is None or age_h is None:
        status = "never"
    elif age_h <= allowance_h:
        status = "fresh"
    elif age_h <= stale_h:
        status = "late"
    else:
        status = "stale"
    return Freshness(status, bucket, last, age_h, allowance_h, stale_h)


def assess_source(source: Any, now: dt.datetime) -> Freshness:
    """`assess` for a `services.db.models.Source` row (or anything shaped like one): the poll
    cadence is the manifest's `poll` when the row's manifest entry has one, else `cadence`;
    scheduled means a connector runs it."""
    return assess(
        manifest_poll(str(source.id)) or str(source.cadence or ""),
        source.last_success_at,
        now,
        scheduled=bool(source.implemented),
        paused=bool(source.paused),
    )


_POLL_CACHE: dict[str, str] | None = None


def manifest_poll(source_id: str) -> str | None:
    """The `poll` override recorded for `source_id` in `data/sources.yaml`, if any (read once)."""
    global _POLL_CACHE
    if _POLL_CACHE is None:
        import yaml

        root = Path(__file__).resolve().parents[2]
        doc = yaml.safe_load((root / "data" / "sources.yaml").read_text(encoding="utf-8")) or {}
        entries = [s for s in doc.get("sources", []) if isinstance(s, dict)]
        _POLL_CACHE = {str(s["id"]): str(s["poll"]) for s in entries if s.get("poll")}
    return _POLL_CACHE.get(source_id)


# --------------------------------------------------------------------------------- store report
def _parse(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def last_success_from_runs(runs: Iterable[Mapping[str, Any]]) -> tuple[dt.datetime | None, str | None]:
    """The finish time of the newest successful run among run records, and the newest run's status."""
    last: dt.datetime | None = None
    latest_status: str | None = None
    latest_at: dt.datetime | None = None
    for r in runs:
        snap = r.get("snapshot")
        finished = (
            _parse(r.get("finished_at"))
            or _parse(r.get("started_at"))
            or _parse(r.get("retrieved_at"))  # a context loader's record carries only this
            or (_parse(snap.get("retrieved_at")) if isinstance(snap, Mapping) else None)
        )
        if finished is not None and (latest_at is None or finished > latest_at):
            latest_at, latest_status = finished, str(r.get("status"))
        if r.get("status") in SUCCESS_STATUSES and finished is not None and (last is None or finished > last):
            last = finished
    return last, latest_status


def store_report(now: dt.datetime, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Freshness of every implemented connector from the run records in the store (no database):
    what the audit's `fresh.py` measured by hand, as a command."""
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.store import open_store

    registry = Registry()
    store = open_store(data_dir)
    rows: list[dict[str, Any]] = []
    for row in registry.status():
        source_id = str(row["id"])
        entry = registry.get(source_id)
        if not entry.implemented or entry.never_ingest:
            continue
        runs = store.runs(source_id)
        last, latest_status = last_success_from_runs(runs)
        fr = assess(str(entry.raw.get("poll") or entry.cadence), last, now, scheduled=not entry.gated)
        rows.append(
            {
                "source_id": source_id,
                "state": row.get("state"),
                "reuse": entry.reuse,
                "poll": str(entry.raw.get("poll") or entry.cadence),
                "runs": len(runs),
                "latest_run_status": latest_status,
                **fr.to_dict(),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m infra.scheduler.freshness")
    ap.add_argument("--now", default=None, help="ISO time to measure at (default: now, UTC)")
    ap.add_argument("--data-dir", default=None, help="store root (default: the pipeline's DATA_DIR)")
    args = ap.parse_args(argv)
    now = _parse(args.now) if args.now else dt.datetime.now(dt.UTC)
    if now is None:
        ap.error(f"--now is not an ISO time: {args.now!r}")
    for line in store_report(now, Path(args.data_dir) if args.data_dir else None):
        sys.stdout.write(json.dumps(line, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
