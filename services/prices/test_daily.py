"""Daily summaries from interval prices: market-local days across DST, the four-hour spread,
negative intervals and basis to the reference hub (docs/29 §7.2)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from services.prices.daily import DAILY_COLUMNS, day_hours, summarise

TZ = "US/Central"


def frame(
    location_id: str, start_local: str, prices: list[float], minutes: int = 60, tz: str = TZ
) -> pd.DataFrame:
    start = pd.Timestamp(start_local).tz_localize(tz).tz_convert("UTC")
    starts = [start + pd.Timedelta(minutes=minutes * i) for i in range(len(prices))]
    return pd.DataFrame(
        {
            "location_id": location_id,
            "market": "da" if minutes == 60 else "rt",
            "interval_start": starts,
            "interval_minutes": minutes,
            "price": prices,
            "source_id": "us.iso.ercot.prices",
            "licence_id": "ercot-lic",
            "source_url": "https://example.test/x",
            "retrieved_at": "2026-10-22T22:00:00+00:00",
        }
    )


def test_day_lengths_follow_daylight_saving() -> None:
    assert day_hours(TZ, dt.date(2026, 3, 8)) == 23
    assert day_hours(TZ, dt.date(2025, 11, 2)) == 25
    assert day_hours(TZ, dt.date(2026, 10, 21)) == 24
    assert day_hours("Europe/London", dt.date(2026, 3, 29)) == 23


def test_a_full_day_has_mean_extremes_negatives_and_the_four_hour_spread() -> None:
    prices = [float(p) for p in range(-2, 22)]  # 24 hours: -2 .. 21
    out = summarise(
        frame("ercot.hb_west", "2026-10-21 00:00", prices), {"ercot.hb_west": TZ}, {"ercot.hb_west": None}
    )
    assert list(out.columns) == DAILY_COLUMNS and len(out) == 1
    row = out.iloc[0]
    assert row["day"] == dt.date(2026, 10, 21) and row["intervals"] == row["expected_intervals"] == 24
    assert row["mean"] == pytest.approx(sum(prices) / 24)
    assert (row["min"], row["max"], row["negative_intervals"]) == (-2.0, 21.0, 2)
    assert row["top4_mean"] == pytest.approx((21 + 20 + 19 + 18) / 4)
    assert row["bottom4_mean"] == pytest.approx((-2 - 1 + 0 + 1) / 4)
    assert row["basis_mean"] is None and row["basis_intervals"] == 0


def test_the_spread_needs_every_hour_and_real_time_is_averaged_to_the_hour_first() -> None:
    short = summarise(frame("x", "2026-10-21 00:00", [10.0] * 23), {"x": TZ}, {"x": None}).iloc[0]
    assert short["top4_mean"] is None and short["bottom4_mean"] is None and short["expected_intervals"] == 24

    quarter = [
        float(h) if q < 3 else float(h) + 4 for h in range(24) for q in range(4)
    ]  # hour h averages h + 1
    rt = summarise(frame("x", "2026-10-21 00:00", quarter, minutes=15), {"x": TZ}, {"x": None}).iloc[0]
    assert rt["intervals"] == 96 and rt["hours"] == 24
    assert rt["top4_mean"] == pytest.approx((24 + 23 + 22 + 21) / 4)
    assert rt["bottom4_mean"] == pytest.approx((1 + 2 + 3 + 4) / 4)


def test_the_short_spring_day_is_complete_at_23_hours() -> None:
    out = summarise(frame("x", "2026-03-08 00:00", [5.0] * 23), {"x": TZ}, {"x": None}).iloc[0]
    assert out["expected_intervals"] == 23 and out["hours"] == 23 and out["top4_mean"] == 5.0


@pytest.mark.parametrize(
    ("tz", "day_start"), [(TZ, "2025-11-02 00:00"), ("Europe/London", "2025-10-26 00:00")]
)
def test_the_long_autumn_day_has_25_hours_including_the_repeated_one(tz: str, day_start: str) -> None:
    hourly = summarise(
        frame("x", day_start, [float(h) for h in range(25)], tz=tz), {"x": tz}, {"x": None}
    ).iloc[0]
    assert hourly["expected_intervals"] == hourly["intervals"] == hourly["hours"] == 25
    assert hourly["top4_mean"] == pytest.approx((24 + 23 + 22 + 21) / 4)
    half = summarise(frame("x", day_start, [1.0] * 50, minutes=30, tz=tz), {"x": tz}, {"x": None}).iloc[0]
    assert half["expected_intervals"] == half["intervals"] == 50 and half["hours"] == 25


def test_basis_is_interval_by_interval_over_shared_intervals() -> None:
    hub = frame("ercot.hb_north", "2026-10-21 00:00", [20.0] * 24)
    zone = frame("ercot.lz_west", "2026-10-21 00:00", [25.0] * 12 + [15.0] * 10)  # 22 hours only
    out = summarise(
        pd.concat([hub, zone]),
        {"ercot.hb_north": TZ, "ercot.lz_west": TZ},
        {"ercot.hb_north": None, "ercot.lz_west": "ercot.hb_north"},
    ).set_index("location_id")
    assert out.loc["ercot.lz_west", "basis_intervals"] == 22
    assert out.loc["ercot.lz_west", "basis_mean"] == pytest.approx((5 * 12 - 5 * 10) / 22)
    assert out.loc["ercot.hb_north", "basis_mean"] is None


def test_unknown_locations_and_untouched_days_are_left_out() -> None:
    two_days = frame("x", "2026-10-20 00:00", [1.0] * 48)
    out = summarise(
        pd.concat([two_days, frame("y", "2026-10-20 00:00", [1.0] * 24)]),
        {"x": TZ},
        {"x": None},
        days={dt.date(2026, 10, 21)},
    )
    assert list(out["location_id"]) == ["x"] and list(out["day"]) == [dt.date(2026, 10, 21)]
    assert summarise(pd.DataFrame(), {"x": TZ}, {}).empty
