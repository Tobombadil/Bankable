"""The trailing statistics: window, coverage withholding, 30-day mean, basis, negative share and
spread (docs/29 §7.2)."""

from __future__ import annotations

import datetime as dt

import pytest

from services.prices.daily import day_hours
from services.prices.stats import Day, compute

TZ = "US/Central"
AS_OF = dt.date(2026, 10, 22)
RETRIEVED = dt.datetime(2026, 10, 22, 22, 0, tzinfo=dt.UTC)


def day(
    d: dt.date,
    mean: float,
    *,
    intervals: int | None = None,
    negative: int = 0,
    basis: float | None = None,
    top4: float | None = None,
    bottom4: float | None = None,
) -> Day:
    expected = day_hours(TZ, d)
    n = expected if intervals is None else intervals
    return Day(
        day=d,
        intervals=n,
        expected=expected,
        minutes=60,
        mean=mean,
        negative=negative,
        top4=top4,
        bottom4=bottom4,
        basis=basis,
        basis_n=n if basis is not None else 0,
        source_id="s",
        licence_id="l",
        source_url="u",
        retrieved_at=RETRIEVED,
    )


def run_of(first: dt.date, last: dt.date, **kw) -> list[Day]:
    out = []
    cursor = first
    while cursor <= last:
        out.append(day(cursor, **kw))
        cursor += dt.timedelta(days=1)
    return out


def test_a_full_year_uses_the_trailing_365_days_ending_with_the_last_complete_day() -> None:
    days = run_of(
        dt.date(2025, 9, 1), dt.date(2026, 10, 21), mean=40.0, negative=2, basis=-3.0, top4=60.0, bottom4=20.0
    )
    stat = compute(days, tz=TZ, as_of=AS_OF, has_reference=True, is_reference=False)
    assert stat is not None
    assert stat["window_end"] == dt.date(2026, 10, 21) and stat["window_start"] == dt.date(2025, 10, 22)
    assert stat["days"] == 365 and stat["sample_count"] == stat["expected_count"]
    assert stat["mean"] == pytest.approx(40.0) and stat["mean_30d"] == pytest.approx(40.0)
    assert stat["basis_mean"] == pytest.approx(-3.0)
    assert stat["negative_share"] == pytest.approx(2 / 24, rel=1e-3)
    assert stat["spread_mean"] == pytest.approx(40.0) and stat["spread_days"] == 365
    assert stat["withheld"] == {}


def test_today_is_not_counted_and_a_partial_last_day_does_not_end_the_window() -> None:
    days = run_of(dt.date(2026, 9, 1), dt.date(2026, 10, 20), mean=10.0)
    days.append(day(dt.date(2026, 10, 21), 99.0, intervals=12))  # half a day
    days.append(day(dt.date(2026, 10, 22), 500.0))  # today: not counted
    stat = compute(days, tz=TZ, as_of=AS_OF, has_reference=False, is_reference=False)
    assert stat is not None and stat["window_end"] == dt.date(2026, 10, 20)
    assert stat["window_start"] == dt.date(2026, 9, 1)  # history shorter than a year: from the first day held
    assert stat["mean"] == pytest.approx(10.0)


def test_coverage_under_90_percent_withholds_the_figures_with_a_reason() -> None:
    days = run_of(dt.date(2026, 7, 1), dt.date(2026, 10, 21), mean=30.0)
    holes = [d for i, d in enumerate(days) if i % 5 != 0]  # a fifth of the days missing: 80% coverage
    stat = compute(holes, tz=TZ, as_of=AS_OF, has_reference=True, is_reference=False)
    assert stat is not None
    assert stat["mean"] is None and stat["negative_share"] is None and stat["spread_mean"] is None
    assert "under 90%" in stat["withheld"]["mean"]
    assert stat["sample_count"] < 0.9 * stat["expected_count"]


def test_the_30_day_mean_has_its_own_coverage() -> None:
    days = run_of(dt.date(2025, 10, 1), dt.date(2026, 9, 15), mean=20.0)
    days += run_of(dt.date(2026, 10, 15), dt.date(2026, 10, 21), mean=80.0)
    stat = compute(days, tz=TZ, as_of=AS_OF, has_reference=False, is_reference=False)
    assert stat is not None
    assert stat["mean"] is not None  # 358 of 365 days held
    assert stat["mean_30d"] is None and "last 30 days" in stat["withheld"]["mean_30d"]


@pytest.mark.parametrize(
    ("has_reference", "is_reference", "reason"),
    [(False, False, "defines no reference hub"), (True, True, "this is the reference hub")],
)
def test_basis_withheld_reasons(has_reference: bool, is_reference: bool, reason: str) -> None:
    days = run_of(dt.date(2026, 9, 1), dt.date(2026, 10, 21), mean=10.0, basis=1.0)
    stat = compute(days, tz=TZ, as_of=AS_OF, has_reference=has_reference, is_reference=is_reference)
    assert stat is not None and stat["basis_mean"] is None and reason in stat["withheld"]["basis_mean"]


def test_dst_days_count_their_own_length_in_the_expected_total() -> None:
    days = run_of(dt.date(2026, 3, 1), dt.date(2026, 3, 31), mean=1.0)
    stat = compute(days, tz=TZ, as_of=dt.date(2026, 4, 1), has_reference=False, is_reference=False)
    assert stat is not None and stat["expected_count"] == 31 * 24 - 1 == stat["sample_count"]


def test_no_days_no_row() -> None:
    assert compute([], tz=TZ, as_of=AS_OF, has_reference=False, is_reference=False) is None
    assert compute([day(AS_OF, 1.0)], tz=TZ, as_of=AS_OF, has_reference=False, is_reference=False) is None
