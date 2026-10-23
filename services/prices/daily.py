"""Interval prices to daily summaries, one per location, market and market-local day (docs/29 §7.2).

Pure: a frame in (`pipeline/connectors/prices/base.py::PRICE_COLUMNS`), a frame out. Days run in
the market's own time zone across daylight-saving changes, so a spring day expects 23 hours and an
autumn day 25.

* `mean` is the time-weighted mean of the day's intervals (every interval of one market has the
  same length, so it is the plain mean); not load-weighted.
* `negative_intervals` counts intervals priced below zero.
* `top4_mean` / `bottom4_mean`: the mean of the four highest and four lowest **hourly** prices of
  the day (a real-time market's intervals averaged to the hour first), set only when every hour of
  the day is present. Their difference is the storage spread, an upper bound on a four-hour
  battery's arbitrage with perfect foresight, before losses (docs/29 §7.2).
* `basis_mean` is the mean, interval by interval, of the location's price minus its reference
  hub's, over the intervals both have (docs/29 §7.2 "basis to hub"); `basis_intervals` counts them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import pandas as pd

DAILY_COLUMNS = [
    "location_id",
    "market",
    "day",
    "intervals",
    "expected_intervals",
    "interval_minutes",
    "mean",
    "min",
    "max",
    "negative_intervals",
    "hours",
    "top4_mean",
    "bottom4_mean",
    "basis_mean",
    "basis_intervals",
    "source_id",
    "licence_id",
    "source_url",
    "retrieved_at",
]


def day_hours(tz: str, day: dt.date) -> int:
    """Hours in `day` in `tz`: 23, 24 or 25."""
    start = pd.Timestamp(day).tz_localize(tz)
    end = pd.Timestamp(day + dt.timedelta(days=1)).tz_localize(tz)
    hours: int = round((end - start).total_seconds() / 3600)
    return hours


def summarise(
    intervals: pd.DataFrame,
    timezones: Mapping[str, str],
    references: Mapping[str, str | None],
    days: set[dt.date] | None = None,
) -> pd.DataFrame:
    """`intervals`: rows of one source. `timezones`/`references` map a `location_id` to its time
    zone and its reference hub's `location_id` (None: no basis). `days`, when given, keeps only
    those market-local days (the ones a run touched). Locations missing from `timezones` are
    dropped, so a catalogue change never summarises an unknown location."""
    if intervals.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    df = intervals[intervals["location_id"].isin(timezones)].copy()
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True)
    out: list[pd.DataFrame] = []
    for tz, part in df.groupby(df["location_id"].map(timezones)):
        tz = str(tz)
        local = part["interval_start"].dt.tz_convert(tz)
        # The hour bucket is floored in UTC: every served zone is a whole-hour offset, so it is
        # the local clock hour, and the repeated hour on the day DST ends stays two hours (a
        # local floor there is ambiguous and raises).
        part = part.assign(_day=local.dt.date, _hour=part["interval_start"].dt.floor("h"))
        if days is not None:
            part = part[part["_day"].isin(days)]
        if part.empty:
            continue
        out.append(_summarise_zone(part, tz))
    if not out:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    daily = pd.concat(out, ignore_index=True)
    return _with_basis(daily, df, timezones, references, days)


_KEYS = ["location_id", "market", "_day"]


def _extreme_hours(hourly: pd.DataFrame, *, largest: bool) -> pd.Series:
    """Per day, the mean of its four highest (or lowest) hourly prices."""
    ordered = hourly.sort_values([*_KEYS, "price"], ascending=[True, True, True, not largest])
    return ordered.groupby(_KEYS, sort=False).head(4).groupby(_KEYS)["price"].mean()


def _summarise_zone(part: pd.DataFrame, tz: str) -> pd.DataFrame:
    """One row per location, market and market-local day of `part` (the rows of one time zone), as
    grouped aggregates: a year of fifteen-minute prices is about 35,000 rows per location."""
    grouped = part.groupby(_KEYS)
    out = grouped.agg(
        intervals=("price", "size"),
        interval_minutes=("interval_minutes", "first"),
        mean=("price", "mean"),
        min=("price", "min"),
        max=("price", "max"),
        retrieved_at=("retrieved_at", "max"),
    )
    out["negative_intervals"] = part.assign(_neg=part["price"] < 0).groupby(_KEYS)["_neg"].sum().astype(int)
    hourly = part.groupby([*_KEYS, "_hour"], as_index=False)["price"].mean()
    out["hours"] = hourly.groupby(_KEYS).size()
    top4 = _extreme_hours(hourly, largest=True)
    bottom4 = _extreme_hours(hourly, largest=False)
    newest = part.sort_values("retrieved_at", kind="stable").groupby(_KEYS).tail(1).set_index(_KEYS)
    for col in ("source_id", "licence_id", "source_url"):
        out[col] = newest[col]
    out = out.reset_index().rename(columns={"_day": "day"})
    hours_expected = out["day"].map({d: day_hours(tz, d) for d in set(out["day"])}).astype(int)
    minutes = out["interval_minutes"].astype(int)
    out["interval_minutes"] = minutes
    out["expected_intervals"] = hours_expected * 60 // minutes
    full = (
        (out["hours"] == hours_expected)
        & (out["intervals"] * minutes >= hours_expected * 60)
        & (out["hours"] >= 8)
    )
    index = pd.MultiIndex.from_frame(out[["location_id", "market", "day"]])
    top = pd.Series(top4.reindex(index).to_numpy(), index=out.index)
    bottom = pd.Series(bottom4.reindex(index).to_numpy(), index=out.index)
    out["top4_mean"] = top.astype(object).where(full, None)
    out["bottom4_mean"] = bottom.astype(object).where(full, None)
    out["intervals"] = out["intervals"].astype(int)
    out["hours"] = out["hours"].astype(int)
    return out


def _with_basis(
    daily: pd.DataFrame,
    intervals: pd.DataFrame,
    timezones: Mapping[str, str],
    references: Mapping[str, str | None],
    days: set[dt.date] | None,
) -> pd.DataFrame:
    present = set(intervals["location_id"])
    parts: list[pd.DataFrame] = []
    for location_id, reference in references.items():
        if (
            not reference
            or reference == location_id
            or location_id not in present
            or reference not in present
        ):
            continue
        mine = intervals.loc[intervals["location_id"] == location_id, ["market", "interval_start", "price"]]
        ref = intervals.loc[intervals["location_id"] == reference, ["market", "interval_start", "price"]]
        joined = mine.merge(ref, on=["market", "interval_start"], suffixes=("", "_ref"))
        if joined.empty:
            continue
        local = joined["interval_start"].dt.tz_convert(timezones[location_id]).dt.date
        joined = joined.assign(day=local, _diff=joined["price"] - joined["price_ref"])
        if days is not None:
            joined = joined[joined["day"].isin(days)]
        agg = (
            joined.groupby(["market", "day"])["_diff"]
            .agg(basis_mean="mean", basis_intervals="size")
            .reset_index()
        )
        parts.append(agg.assign(location_id=location_id))
    if parts:
        basis = pd.concat(parts, ignore_index=True)
        daily = daily.merge(basis, on=["location_id", "market", "day"], how="left")
        daily["basis_intervals"] = daily["basis_intervals"].fillna(0).astype(int)
        daily["basis_mean"] = daily["basis_mean"].astype(object).where(daily["basis_mean"].notna(), None)
    else:
        daily = daily.assign(basis_mean=None, basis_intervals=0)
    return daily[DAILY_COLUMNS]
