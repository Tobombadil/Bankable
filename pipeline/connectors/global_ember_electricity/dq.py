"""Data-quality checks on one dataset of one Ember run, before anything is written (docs/04 DA-6).

Each check returns `{check, level, detail, data}` (the shape of `pipeline.connectors.dq.Check`, so the
admin view reads one structure). A `hold` keeps that dataset's rows out of `normalized/` (the snapshot
stays as evidence, the run ends `partial`, and the figures already loaded stay served); a `warn` is
recorded. The comparison is with the previous successful run's profile of the same dataset and route.

| check            | warn                                    | hold                                      |
|------------------|-----------------------------------------|-------------------------------------------|
| `no_rows`        | —                                       | an answer that gave no value at all       |
| `provenance`     | —                                       | any row without the quartet               |
| `duplicate_keys` | —                                       | one key twice                             |
| `row_count`      | moved 10 % or more                      | fell more than 30 %                       |
| `country_count`  | moved 10 % or more                      | no country at all, or fell more than 30 % |
| `entity_code_missing` | an area Ember flags as a country without a usable code | —                     |
| `latest_period`  | —                                       | earlier than the previous run's           |
| `vocabulary`     | any series name not in `ember.SERIES`   | unknown names on more than 5 % of rows    |
| `null_spike`     | a metric's missing share up 5 points    | up 10 points                              |
| `value_range`    | any share outside 0-100.5 or a negative | on more than 1 % of rows                  |

Ember's latest year covers fewer countries than earlier years until its national sources report, so
`country_count` compares the dataset's whole span (every country with any value), not the latest year.
A dataset with no country holds even on its first load: every area would be World or a region, so
`area=US` would not exist and coverage and the ranking would be empty. On a dataset and route's first
load, `country_count` compares with the newest load of the same dataset by the other route (a
person's first yearly download against the API's yearly answer), so a file that keys far fewer
countries than the figures already served is held too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from pipeline.connectors.base import PROVENANCE

ROW_WARN = 0.10
ROW_HOLD = 0.30
UNMAPPED_HOLD = 0.05
NULL_WARN_PP = 5.0
NULL_HOLD_PP = 10.0
RANGE_HOLD = 0.01


def _check(check: str, level: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"check": check, "level": level, "detail": detail, "data": data}


def profile(df: pd.DataFrame, *, observations: int, missing: Mapping[str, int]) -> dict[str, Any]:
    """What the next run compares with: rows, areas, countries, the latest period, countries per
    year with a generation value (the coverage the API reports), and each metric's missing share."""
    if df.empty:
        return {
            "rows": 0,
            "areas": 0,
            "countries": 0,
            "latest_period": None,
            "countries_by_year": {},
            "missing_share": {},
            "observations": observations,
        }
    countries = df[df["area_kind"] == "country"]
    generation = countries[(countries["metric"] == "generation_twh") & (~countries["is_aggregate"])]
    by_year = generation.groupby("year")["area_id"].nunique()
    return {
        "rows": len(df),
        "areas": int(df["area_id"].nunique()),
        "countries": int(countries["area_id"].nunique()),
        "latest_period": str(df["period"].max()),
        "countries_by_year": {str(int(y)): int(n) for y, n in by_year.items()},
        "missing_share": {m: round(n / observations, 4) for m, n in missing.items()} if observations else {},
        "observations": observations,
    }


def _drift(check: str, now: int, before: int | None, noun: str) -> dict[str, Any]:
    if not before:
        return _check(check, "info", f"{now} {noun}; no previous run to compare", now=now)
    change = (now - before) / before
    level = "hold" if change < -ROW_HOLD else "warn" if abs(change) >= ROW_WARN else "pass"
    return _check(
        check, level, f"{now} {noun} against {before} in the previous run", now=now, previous=before
    )


def _countries(now: int, before: int | None, baseline: tuple[str, int | None] | None) -> dict[str, Any]:
    if now == 0:
        return _check(
            "country_count",
            "hold",
            "no country in this dataset: every area keyed as World or a region (a country code missing?)",
            now=0,
            previous=before,
        )
    if before or baseline is None or not baseline[1]:
        return _drift("country_count", now, before, "countries")
    label, other = baseline
    check = _drift("country_count", now, other, "countries")
    check["detail"] = (
        f"{now} countries against {other} in the last load of {label} (first load of this route)"
    )
    check["data"]["baseline"] = label
    return check


def run_checks(
    df: pd.DataFrame,
    prof: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    unmapped: Mapping[str, int],
    uncoded: Mapping[str, int] | None = None,
    baseline: tuple[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The checks of one dataset. `previous` is the profile of the last run that wrote the same dataset
    and route; `baseline`, `(label, profile)` of the other route's last load of the same dataset, is
    read for `country_count` only, when this route has no previous load."""
    checks: list[dict[str, Any]] = []
    rows = len(df)
    if rows == 0:
        checks.append(_check("no_rows", "hold", "the answer gave no value this parser reads"))
        return checks
    lacking = 0
    for col in PROVENANCE:
        lacking += int(df[col].isna().sum() + (df[col].astype(str).str.strip() == "").sum())
    checks.append(
        _check(
            "provenance",
            "hold" if lacking else "pass",
            f"{lacking} rows lack a provenance field",
            rows=lacking,
        )
    )
    dupes = int(df["record_id"].duplicated().sum())
    checks.append(
        _check("duplicate_keys", "hold" if dupes else "pass", f"{dupes} repeated keys", repeated=dupes)
    )
    prev = previous or {}
    checks.append(_drift("row_count", rows, prev.get("rows"), "values"))
    other = (baseline[0], baseline[1].get("countries")) if baseline is not None else None
    checks.append(_countries(int(prof.get("countries") or 0), prev.get("countries"), other))
    if uncoded:
        checks.append(
            _check(
                "entity_code_missing",
                "warn",
                f"{len(uncoded)} areas flagged as countries carry no usable code; kept as regions, so they "
                "drop out of coverage and the rankings",
                areas=dict(sorted(uncoded.items())),
            )
        )
    latest, before = prof.get("latest_period"), prev.get("latest_period")
    if latest and before and str(latest) < str(before):
        checks.append(
            _check(
                "latest_period",
                "hold",
                f"latest period {latest} is earlier than {before}",
                now=latest,
                previous=before,
            )
        )
    else:
        checks.append(_check("latest_period", "pass", f"latest period {latest}", now=latest, previous=before))
    observations = int(prof.get("observations") or 0)
    unknown = sum(unmapped.values())
    share = unknown / observations if observations else 0.0
    level = "hold" if share > UNMAPPED_HOLD else "warn" if unknown else "pass"
    checks.append(
        _check(
            "vocabulary",
            level,
            f"{unknown} rows name a series this parser does not know" if unknown else "every series known",
            unmapped=dict(sorted(unmapped.items())),
            share=round(share, 4),
        )
    )
    now_missing = prof.get("missing_share") or {}
    before_missing = prev.get("missing_share") or {}
    worst = "pass"
    rises: dict[str, float] = {}
    for metric, value in now_missing.items():
        if metric not in before_missing:
            continue
        rise = (float(value) - float(before_missing[metric])) * 100
        rises[metric] = round(rise, 2)
        if rise > NULL_HOLD_PP:
            worst = "hold"
        elif rise > NULL_WARN_PP and worst != "hold":
            worst = "warn"
    checks.append(
        _check("null_spike", worst, "missing values per metric against the previous run", rise_pp=rises)
    )
    share_rows = df[df["metric"] == "share_of_generation_pct"]["value"]
    bad = int(((share_rows < 0) | (share_rows > 100.5)).sum())
    bad += int((df[df["metric"] != "share_of_generation_pct"]["value"] < 0).sum())
    level = "hold" if bad > RANGE_HOLD * rows else "warn" if bad else "pass"
    checks.append(_check("value_range", level, f"{bad} values out of range", rows=bad))
    return checks


def status_of(checks: list[dict[str, Any]]) -> str:
    levels = {c["level"] for c in checks}
    return "hold" if "hold" in levels else "warn" if "warn" in levels else "pass"
