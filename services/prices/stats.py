"""The trailing statistics per location and market (docs/29 §7.2; slice 1 of the owner's tranche 1).

Window: the trailing 365 days ending with the last complete market-local day, or from the first
day held if history is shorter ("trailing 12 months where history allows"); the figure says which
(`window_start`, `window_end`, `days`, `sample_count`). From the daily summaries
(`services/prices/daily.py`):

* `mean` — time-weighted mean of every interval in the window; `mean_30d` the same over the last
  30 days of it (its own coverage).
* `basis_mean` — mean of location minus reference hub, interval by interval, over the intervals
  both have; none where the market defines no hub (NYISO, GB) or for the reference itself.
* `negative_share` — intervals (real time) or hours (day ahead) priced below zero, over all.
* `spread_mean` — the mean over complete days of top-4 minus bottom-4 hourly prices.

**Coverage.** A statistic is withheld below 90% interval coverage of its window (docs/29 §7.2):
`expected_count` counts every interval the window's days should hold, DST-length days included,
so a gap in the source shows as lower coverage rather than a quietly shorter average. A withheld
figure is null, with its reason in `withheld`. Statistics are derived values; they carry the
provenance of the rows they summarise: the source, licence, URL and the newest `retrieved_at`.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.prices.daily import day_hours
from services.prices.models import PriceDaily, PriceLocation, PriceStat

log = logging.getLogger("services.prices.stats")

WINDOW = "trailing_12m"
WINDOW_DAYS = 365
RECENT_DAYS = 30
MIN_COVERAGE = 0.9


@dataclass(frozen=True)
class Day:
    """One `price_daily` row, as the computation reads it."""

    day: dt.date
    intervals: int
    expected: int
    minutes: int
    mean: float
    negative: int
    top4: float | None
    bottom4: float | None
    basis: float | None
    basis_n: int
    source_id: str
    licence_id: str
    source_url: str
    retrieved_at: dt.datetime


def _weighted(days: Iterable[Day]) -> tuple[float | None, int]:
    total = n = 0
    acc = 0.0
    for d in days:
        acc += d.mean * d.intervals
        total += d.intervals
        n += 1
    return (acc / total if total else None), total


def _expected(tz: str, start: dt.date, end: dt.date, minutes: int) -> int:
    count = 0
    cursor = start
    while cursor <= end:
        count += day_hours(tz, cursor) * 60 // minutes
        cursor += dt.timedelta(days=1)
    return count


def compute(
    days: Sequence[Day], *, tz: str, as_of: dt.date, has_reference: bool, is_reference: bool
) -> dict[str, Any] | None:
    """The statistics row for one location and market from its daily rows; None without data."""
    held = sorted((d for d in days if d.day < as_of), key=lambda d: d.day)
    if not held:
        return None
    complete = [d for d in held if d.intervals >= d.expected]
    end = complete[-1].day if complete else held[-1].day
    start = max(end - dt.timedelta(days=WINDOW_DAYS - 1), held[0].day)
    window = [d for d in held if start <= d.day <= end]
    minutes = window[-1].minutes
    expected = _expected(tz, start, end, minutes)
    mean, samples = _weighted(window)
    coverage = samples / expected if expected else 0.0
    recent_start = max(end - dt.timedelta(days=RECENT_DAYS - 1), start)
    recent = [d for d in window if d.day >= recent_start]
    mean_30d, samples_30d = _weighted(recent)
    expected_30d = _expected(tz, recent_start, end, minutes)
    withheld: dict[str, str] = {}
    if coverage < MIN_COVERAGE:
        reason = f"coverage {coverage:.0%} of the window is under {MIN_COVERAGE:.0%}"
        withheld.update(dict.fromkeys(("mean", "negative_share", "spread_mean", "basis_mean"), reason))
    if expected_30d and samples_30d / expected_30d < MIN_COVERAGE:
        withheld["mean_30d"] = f"coverage {samples_30d / expected_30d:.0%} of the last 30 days is under 90%"
    negatives = sum(d.negative for d in window)
    spreads = [d.top4 - d.bottom4 for d in window if d.top4 is not None and d.bottom4 is not None]
    basis_n = sum(d.basis_n for d in window)
    basis = sum((d.basis or 0.0) * d.basis_n for d in window) / basis_n if basis_n else None
    if not has_reference:
        withheld["basis_mean"] = "the market defines no reference hub"
    elif is_reference:
        withheld["basis_mean"] = "this is the reference hub"
    elif basis is None:
        withheld.setdefault("basis_mean", "no interval shared with the reference hub")
    newest = max(window, key=lambda d: d.retrieved_at)
    return {
        "window_kind": WINDOW,
        "window_start": start,
        "window_end": end,
        "days": len(window),
        "sample_count": samples,
        "expected_count": expected,
        "interval_minutes": minutes,
        "mean": None if "mean" in withheld else mean,
        "mean_30d": None if "mean_30d" in withheld else mean_30d,
        "window_30d_start": recent_start,
        "samples_30d": samples_30d,
        "expected_30d": expected_30d,
        "basis_mean": None if "basis_mean" in withheld else basis,
        "basis_samples": basis_n,
        "negative_share": None
        if "negative_share" in withheld
        else (negatives / samples if samples else None),
        "spread_mean": None if "spread_mean" in withheld or not spreads else sum(spreads) / len(spreads),
        "spread_days": len(spreads),
        "withheld": withheld,
        "source_id": newest.source_id,
        "licence_id": newest.licence_id,
        "source_url": newest.source_url,
        "retrieved_at_max": newest.retrieved_at,
    }


def run_stats(
    session: Session, *, as_of: dt.date | None = None, source_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """Recompute `price_stat` for every location (or those of `source_ids`) from `price_daily`.
    `as_of` (default today, UTC) is the first day not counted: the window ends before it."""
    t0 = time.monotonic()
    as_of = as_of or dt.datetime.now(dt.UTC).date()
    query = sa.select(PriceLocation)
    if source_ids:
        query = query.where(PriceLocation.source_id.in_(source_ids))
    locations = list(session.scalars(query))
    written = withheld = 0
    for loc in locations:
        rows = session.scalars(
            sa.select(PriceDaily)
            .where(
                PriceDaily.location_id == loc.id,
                PriceDaily.day >= as_of - dt.timedelta(days=WINDOW_DAYS + 40),
            )
            .order_by(PriceDaily.market, PriceDaily.day)
        ).all()
        by_market: dict[str, list[Day]] = {}
        for r in rows:
            by_market.setdefault(r.market, []).append(
                Day(
                    day=r.day,
                    intervals=r.intervals,
                    expected=r.expected_intervals,
                    minutes=r.interval_minutes,
                    mean=r.mean,
                    negative=r.negative_intervals,
                    top4=r.top4_mean,
                    bottom4=r.bottom4_mean,
                    basis=r.basis_mean,
                    basis_n=r.basis_intervals,
                    source_id=r.source_id,
                    licence_id=r.licence_id,
                    source_url=r.source_url,
                    retrieved_at=r.retrieved_at,
                )
            )
        for market, days in by_market.items():
            stat = compute(
                days,
                tz=loc.timezone,
                as_of=as_of,
                has_reference=loc.reference_id is not None,
                is_reference=loc.reference_id == loc.id,
            )
            if stat is None:
                continue
            existing = session.get(PriceStat, (loc.id, market, WINDOW))
            values = {**stat, "basis_reference_id": loc.reference_id, "computed_at": dt.datetime.now(dt.UTC)}
            if existing is None:
                session.add(PriceStat(location_id=loc.id, market=market, **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            written += 1
            withheld += len(stat["withheld"])
    session.flush()
    report = {
        "locations": len(locations),
        "stats_written": written,
        "figures_withheld": withheld,
        "seconds": round(time.monotonic() - t0, 2),
    }
    log.info("price stats %s", report, extra=report)
    return report
