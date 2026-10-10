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

A stale release is stale whatever the last run said (2026-10-10, review docs/51 §2.7 item 4). An
`unchanged` run counts as a success, so a source asked on schedule whose newest release we never
fetched read `fresh`: ERCOT's September report, published after the monthly tick, would have waited a
month behind a green badge. Where the run records name the release we hold
(`services.ingest.vintage.from_run_record`: ERCOT's GIS report name, NESO's register file), `assess`
also takes that vintage and the source's own `cadence`: once the end of the period the vintage
names, plus one release interval (`release_interval`), plus `VINTAGE_GRACE` has passed, the next
release is overdue and the source is `stale` (`vintage_stale`), even when its last run succeeded an
hour ago. ERCOT's September 2026 report (period end 1 October) is overdue from 6 November; NESO's
register of 10 October from 18 October. Sources whose run records name no release, and EIA-860M,
whose label is the data month published about two months later, are assessed as before. The store
report applies the rule; the scheduler's `freshness_tick` reads `source` rows, which do not carry
the run-record vintage, and assesses as before until it is given one (`assess_source(vintage=...)`).

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
from services.ingest.vintage import NOT_STATED_VINTAGE, Vintage, from_run_record, period_end

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

#: How far apart a source's releases are, by keyword of its own `cadence` (how often it publishes,
#: not how often we ask it), most frequent first as in `cadence._KEYWORD_BUCKET`.
_RELEASE_INTERVAL: tuple[tuple[str, dt.timedelta], ...] = (
    ("daily", dt.timedelta(days=1)),
    ("twice weekly", dt.timedelta(days=3.5)),
    ("weekly", dt.timedelta(days=7)),
    ("monthly", dt.timedelta(days=31)),
    ("quarterly", dt.timedelta(days=92)),
    ("annual", dt.timedelta(days=366)),
)
#: Slack past one release interval before the next release counts as overdue: a release published
#: a few days late, or a weekly rather than twice-weekly NESO week, is not an alert.
VINTAGE_GRACE = dt.timedelta(days=5)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Freshness:
    status: str
    bucket: str | None
    last_success_at: dt.datetime | None
    age_hours: float | None
    allowance_hours: float | None
    stale_after_hours: float | None
    #: The release the source states we hold (`services/ingest/vintage.py`), when one was given.
    vintage: str | None = None
    #: When the release after `vintage` is overdue (`vintage_overdue_at`).
    vintage_overdue_at: dt.datetime | None = None
    #: That moment has passed: the source is `stale` however recent its last success.
    vintage_stale: bool = False

    @property
    def alert(self) -> bool:
        """The ops signal: stale, or a scheduled source that has never succeeded."""
        return self.status in ("stale", "never")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["last_success_at"] = _iso(self.last_success_at)
        out["vintage_overdue_at"] = _iso(self.vintage_overdue_at)
        out["alert"] = self.alert
        return out


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def release_interval(cadence: str) -> dt.timedelta | None:
    """The time between two releases a `cadence` string describes; None when it names none."""
    text = cadence.strip().lower()
    for keyword, interval in _RELEASE_INTERVAL:
        if keyword in text:
            return interval
    return None


def vintage_overdue_at(vintage: str | None, cadence: str | None) -> dt.datetime | None:
    """When the release after `vintage` is overdue: the end of the period it names
    (`services.ingest.vintage.period_end`), plus one release interval of `cadence`, plus
    `VINTAGE_GRACE`. None when either is unknown."""
    end = period_end(vintage)
    interval = release_interval(cadence) if cadence else None
    if end is None or interval is None:
        return None
    return dt.datetime(end.year, end.month, end.day, tzinfo=dt.UTC) + interval + VINTAGE_GRACE


def assess(
    poll: str,
    last_success_at: dt.datetime | None,
    now: dt.datetime,
    *,
    scheduled: bool = True,
    paused: bool = False,
    vintage: str | None = None,
    cadence: str | None = None,
) -> Freshness:
    """Freshness of one source polled at cadence string `poll` (module docstring). With the
    `vintage` it states and its own `cadence`, a source whose next release is overdue is `stale`
    whatever its last success says."""
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
    overdue = vintage_overdue_at(vintage, cadence) if vintage else None
    vintage_stale = overdue is not None and now > overdue
    if vintage_stale and status in ("fresh", "late"):
        status = "stale"
    return Freshness(status, bucket, last, age_h, allowance_h, stale_h, vintage, overdue, vintage_stale)


def assess_source(source: Any, now: dt.datetime, *, vintage: str | None = None) -> Freshness:
    """`assess` for a `services.db.models.Source` row (or anything shaped like one): the poll
    cadence is the manifest's `poll` when the row's manifest entry has one, else `cadence`;
    scheduled means a connector runs it. `vintage` is the run-record vintage when the caller has
    one (`held_vintage`); the row's own `source.vintage` is not used, because the loader records it
    from record URLs and for EIA-860M it names the data month (module docstring)."""
    return assess(
        manifest_poll(str(source.id)) or str(source.cadence or ""),
        source.last_success_at,
        now,
        scheduled=bool(source.implemented),
        paused=bool(source.paused),
        vintage=vintage,
        cadence=str(source.cadence or ""),
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


def held_vintage(runs: Iterable[Mapping[str, Any]]) -> Vintage:
    """The release named by the newest successful run whose record names one
    (`services.ingest.vintage.from_run_record`); not stated when none does."""
    best: Vintage = NOT_STATED_VINTAGE
    best_at: dt.datetime | None = None
    for r in runs:
        if r.get("status") not in SUCCESS_STATUSES:
            continue
        vintage = from_run_record(r)
        if not vintage.stated:
            continue
        at = _parse(r.get("finished_at")) or _parse(r.get("started_at"))
        if not best.stated or (at is not None and (best_at is None or at > best_at)):
            best, best_at = vintage, at
    return best


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
        vintage = held_vintage(runs)
        fr = assess(
            str(entry.raw.get("poll") or entry.cadence),
            last,
            now,
            scheduled=not entry.gated,
            vintage=vintage.value,
            cadence=str(entry.cadence),
        )
        rows.append(
            {
                "source_id": source_id,
                "state": row.get("state"),
                "reuse": entry.reuse,
                "poll": str(entry.raw.get("poll") or entry.cadence),
                "runs": len(runs),
                "latest_run_status": latest_status,
                "vintage_label": vintage.label,
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
