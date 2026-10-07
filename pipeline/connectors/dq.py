"""Data-quality gates run at the end of every source run (docs/04 DA-6, docs/20 §10, §12).

| Check                          | warn                              | hold                             |
|--------------------------------|-----------------------------------|----------------------------------|
| row-count drift vs previous ok | |delta| >= 10 %                   | |delta| > 30 % either direction  |
| (incremental: same weekdays)   | (see below)                       | and >= 10 rows                   |
| zero rows                      | —                                 | 0 rows, unless declared empty    |
| vocabulary drift               | any new status/technology value   | unmapped status > 5 % of rows    |
| null spike on required field   | +5 pp vs trailing median (5 runs) | +10 pp                           |
| field nulled on common rows    | —                                 | > 20 % (>= 3) re-fetched rows    |
| duplicate record_id            | suffixed duplicates (declared)    | any remaining                    |
| provenance completeness        | —                                 | any row missing the quartet      |
| schema drift (source columns)  | any added/removed column          | a declared key column missing    |
| window truncated (page cap)    | —                                 | the fetch stopped at its page cap |

A held run keeps its raw snapshot (evidence) but writes nothing publishable and records why
(`source_run.dq`). Thresholds are per-source configuration with these defaults: a manifest entry's
`dq_thresholds` mapping overrides any of `DEFAULT_THRESHOLDS` (the runner passes it; audit F13).

Incremental sources (2026-10-07, audit 2026-09-30 F6). A window source's row count follows its
publication week: TED publishes on business days, so a 3-day window held 690 rows on a Thursday
and 230 on a Monday, and the old previous-run comparison held TED on 16 of 28 simulated days. For a
connector that declares a `window_date_column` the runner passes `daily_counts`, the rows per
complete UTC day its window covered; the gate compares those days with the median count of the
same weekday over the last `profile_weeks` observations in the history, and holds only when the
like-for-like total moves more than the hold threshold *and* by at least `row_drift_min_rows`
(small windows are noisy). A weekday with no history is skipped; no comparable day is `info`.

Schema changes (audit F5). A declared `key_source_columns` entry missing from the run's header
holds, with or without a previous run to compare with; a header rename is a removal plus an
addition, so a renamed key column holds too. As a backstop for the columns nobody declared, a
watched field (status, capacity, date, county, sponsor, name, state) that was set on a row in
the previous output and is null on the same row now, on more than `nulled_hold_share` of the
re-fetched rows, holds (`field_nulled:<field>`): a renamed column reads as nulls, and nulls must
not be published as change events.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

import pandas as pd

from pipeline.connectors.base import PROVENANCE

DEFAULT_THRESHOLDS: dict[str, float] = {
    "row_drift_warn_pct": 10.0,
    "row_drift_hold_pct": 30.0,
    "unmapped_hold_share": 0.05,
    "null_spike_warn_pp": 5.0,
    "null_spike_hold_pp": 10.0,
    "null_history_runs": 5,
    "row_drift_min_rows": 10,
    "profile_weeks": 4,
    "nulled_hold_share": 0.2,
    "nulled_min_rows": 3,
}
#: Watched fields for the `field_nulled` gate, by kind; status counts `unknown` as null.
NULLED_FIELDS = {
    "proposal": (
        "lifecycle_state",
        "capacity_mw",
        "proposed_cod",
        "county",
        "sponsor_name",
        "name_canonical",
        "state",
    ),
    "opportunity": ("status", "due_at", "issuer", "title", "jurisdiction"),
    "document": ("title", "published_date", "docket_refs", "filer"),
}
_UNKNOWN_STATES = frozenset({"", "unknown"})
STATUS_COL = {"proposal": "lifecycle_state", "opportunity": "status", "document": "lifecycle_state"}
DEFAULT_REQUIRED = {
    "proposal": ("name_canonical", "capacity_mw", "technology_raw", "state"),
    "opportunity": ("title", "issuer", "jurisdiction", "due_at"),
    "document": ("title", "accession_number", "docket_refs", "published_date"),
}
VOCAB_COLS = {
    "proposal": ("status_raw", "technology_raw", "kind"),
    "opportunity": ("status_raw", "kind"),
    "document": ("status_raw", "document_class"),
}


@dataclass
class Check:
    check: str
    level: str  # pass | warn | hold | info
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DQResult:
    status: str  # pass | warn | hold  (maps onto source_run.dq_status pass|warn|fail)
    checks: list[Check]
    stats: dict[str, Any]

    @property
    def held(self) -> bool:
        return self.status == "hold"

    @property
    def dq_status(self) -> str:
        return {"pass": "pass", "warn": "warn", "hold": "fail"}[self.status]

    def hold_reasons(self) -> list[str]:
        return [f"{c.check}: {c.detail}" for c in self.checks if c.level == "hold"]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": [asdict(c) for c in self.checks], "stats": self.stats}


def snapshot_stats(
    df: pd.DataFrame, kind: str, source_columns: list[str], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The per-run numbers later runs compare against (stored on source_run)."""
    req = required or DEFAULT_REQUIRED[kind]
    n = len(df)
    null_rates = {c: (float(df[c].isna().mean() * 100) if c in df.columns and n else 0.0) for c in req}
    vocab: dict[str, list[str]] = {}
    for c in VOCAB_COLS[kind]:
        if c in df.columns:
            vocab[c] = sorted({str(v) for v in df[c].dropna().unique()})[:500]
    return {"rows": n, "null_rates_pct": null_rates, "vocabulary": vocab, "source_columns": source_columns}


def run_gates(
    df: pd.DataFrame,
    kind: str,
    source_columns: list[str],
    history: list[dict[str, Any]],
    *,
    thresholds: dict[str, float] | None = None,
    required: tuple[str, ...] = (),
    key_source_columns: tuple[str, ...] = (),
    duplicates_resolved: int = 0,
    rows_fetched: int | None = None,
    daily_counts: dict[str, int] | None = None,
    incremental: bool = False,
    may_be_empty: bool = False,
    window_days: float | None = None,
    previous: pd.DataFrame | None = None,
    fetched: pd.DataFrame | None = None,
    truncated: bool = False,
) -> DQResult:
    """Evaluate every gate. `history` is the list of previous *successful* runs' `stats` dicts,
    oldest first; `rows_fetched` overrides the row count used for drift (incremental sources).

    `daily_counts` (incremental sources with a date column) switches the row-count gate to the
    like-for-like weekday comparison; `incremental` without it skips the count comparison, since
    a window's length varies with the gap since the last promoted run. `previous` is the previous
    promoted output (already restated) and `fetched` this run's own rows before carried-forward
    rows were added, for the `field_nulled` gate. `truncated` says the fetch stopped at its page
    cap. `may_be_empty` and `window_days` decide the zero-row hold."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    req = required or DEFAULT_REQUIRED[kind]
    checks: list[Check] = []
    stats = snapshot_stats(df, kind, source_columns, req)
    n = rows_fetched if rows_fetched is not None else len(df)
    stats["rows_fetched"] = n
    if daily_counts is not None:
        stats["daily_counts"] = dict(sorted(daily_counts.items()))
    prev = history[-1] if history else None

    # 0. zero rows (audit F13): a register that answers nothing is broken unless it may be empty;
    # a window source can be empty over a quiet weekend, so only a window of a week or more holds.
    if n == 0 and not may_be_empty and (not incremental or (window_days or 0) >= 7):
        checks.append(Check("zero_rows", "hold", "0 rows fetched", {"window_days": window_days}))
    if truncated:
        checks.append(
            Check(
                "window_truncated",
                "hold",
                "the fetch stopped at its page cap; records past it were not fetched",
                {"rows": n},
            )
        )

    # 1. row-count drift
    previous_rows = int(prev.get("rows_fetched") or prev.get("rows") or 0) if prev else 0
    if daily_counts is not None:
        checks.append(_weekday_drift(daily_counts, history, t))
    elif incremental:
        checks.append(
            Check(
                "row_count_drift",
                "info",
                f"{n} rows in a variable window; no daily profile to compare",
                {"current": n, "basis": "none"},
            )
        )
    elif previous_rows:
        base = previous_rows
        drift = (n - base) / base * 100.0
        level = (
            "hold"
            if abs(drift) > t["row_drift_hold_pct"]
            else "warn"
            if abs(drift) >= t["row_drift_warn_pct"]
            else "pass"
        )
        checks.append(
            Check(
                "row_count_drift",
                level,
                f"{base} -> {n} rows ({drift:+.1f} %)",
                {"previous": base, "current": n, "delta_pct": round(drift, 2)},
            )
        )
    else:
        checks.append(Check("row_count_drift", "info", f"no baseline; {n} rows", {"current": n}))

    # 2. vocabulary drift: unmapped statuses (rule id ends with .unmapped) and new raw values
    status_col = STATUS_COL[kind]
    if "status_rule" in df.columns and len(df):
        unmapped = df["status_rule"].astype("string").str.endswith(".unmapped").fillna(False)
        share = float(unmapped.mean())
        values = sorted({str(v) for v in df.loc[unmapped, "status_raw"].dropna().unique()})[:50]
        level = "hold" if share > t["unmapped_hold_share"] else "warn" if share > 0 else "pass"
        checks.append(
            Check(
                "vocabulary_unmapped",
                level,
                f"{int(unmapped.sum())} rows ({share * 100:.1f} %) with unmapped {status_col}",
                {"share": round(share, 4), "values": values},
            )
        )
    for col, values in stats["vocabulary"].items():
        prev_vals = set((prev or {}).get("vocabulary", {}).get(col, [])) if prev else None
        if prev_vals is None:
            continue
        new = sorted(set(values) - prev_vals)
        if new:
            checks.append(
                Check(
                    "vocabulary_drift",
                    "warn",
                    f"new {col} values: {new[:10]}",
                    {"column": col, "new_values": new[:50]},
                )
            )
    if not any(c.check == "vocabulary_drift" for c in checks):
        checks.append(Check("vocabulary_drift", "pass" if prev else "info", "no new values"))

    # 3. null spikes vs trailing median of the last N successful runs
    for col in req:
        rate = stats["null_rates_pct"].get(col, 0.0)
        hist = [
            h["null_rates_pct"][col]
            for h in history[-int(t["null_history_runs"]) :]
            if col in h.get("null_rates_pct", {})
        ]
        if not hist:
            checks.append(
                Check(
                    f"null_rate:{col}",
                    "info",
                    f"{rate:.1f} % null (no baseline)",
                    {"rate_pct": round(rate, 2)},
                )
            )
            continue
        baseline = float(median(hist))
        delta = rate - baseline
        level = (
            "hold"
            if delta >= t["null_spike_hold_pp"]
            else "warn"
            if delta >= t["null_spike_warn_pp"]
            else "pass"
        )
        checks.append(
            Check(
                f"null_rate:{col}",
                level,
                f"{rate:.1f} % null vs median {baseline:.1f} % ({delta:+.1f} pp)",
                {"rate_pct": round(rate, 2), "baseline_pct": round(baseline, 2), "delta_pp": round(delta, 2)},
            )
        )

    # 4. duplicate keys
    dups = int(df["record_id"].duplicated().sum()) if "record_id" in df.columns else 0
    if dups:
        checks.append(Check("duplicate_keys", "hold", f"{dups} duplicate record_id values", {"count": dups}))
    elif duplicates_resolved:
        checks.append(
            Check(
                "duplicate_keys",
                "warn",
                f"{duplicates_resolved} duplicate source ids suffixed deterministically",
                {"resolved": duplicates_resolved},
            )
        )
    else:
        checks.append(Check("duplicate_keys", "pass", "none"))

    # 5. provenance completeness
    missing = 0
    for c in PROVENANCE:
        if c not in df.columns:
            missing += len(df)
        else:
            s = df[c]
            missing += int((s.isna() | (s.astype("string").str.strip() == "")).sum())
    checks.append(
        Check(
            "provenance",
            "hold" if missing else "pass",
            f"{missing} provenance cells missing" if missing else "quartet present on every row",
            {"missing_cells": missing},
        )
    )

    # 6. schema drift of the source columns
    declared_missing = sorted(set(key_source_columns) - set(source_columns)) if n and source_columns else []
    if declared_missing and not (prev and prev.get("source_columns") is not None):
        checks.append(
            Check(
                "schema_drift",
                "hold",
                f"declared key columns missing: {declared_missing}",
                {"missing": declared_missing},
            )
        )
    elif prev and prev.get("source_columns") is not None:
        before, after = set(prev["source_columns"]), set(source_columns)
        added, removed = sorted(after - before), sorted(before - after)
        key_removed = sorted((set(removed) & set(key_source_columns)) | set(declared_missing))
        if key_removed:
            checks.append(
                Check(
                    "schema_drift",
                    "hold",
                    f"removed key columns: {key_removed}",
                    {"added": added, "removed": removed},
                )
            )
        elif added or removed:
            checks.append(
                Check(
                    "schema_drift",
                    "warn",
                    f"added {added[:10]} removed {removed[:10]}",
                    {"added": added, "removed": removed},
                )
            )
        else:
            checks.append(Check("schema_drift", "pass", "source columns unchanged"))
    else:
        checks.append(Check("schema_drift", "info", f"{len(source_columns)} source columns recorded"))

    # 7. watched fields nulled on rows this run fetched again
    if previous is not None and fetched is not None and len(previous) and len(fetched):
        checks.extend(_nulled_checks(kind, previous, fetched, t))

    levels = {c.level for c in checks}
    status = "hold" if "hold" in levels else "warn" if "warn" in levels else "pass"
    return DQResult(status=status, checks=checks, stats=stats)


def daily_profile(dates: pd.Series, start: dt.datetime, end: dt.datetime) -> dict[str, int]:
    """Rows per UTC day for every day the window `[start, end]` covers completely (zeros
    included), from each row's publication date. Days the window only partly covers are left
    out: their count says as much about the window as about the source."""
    s, e = start.astimezone(dt.UTC), end.astimezone(dt.UTC)
    first = s.date() if s.time() == dt.time(0) else s.date() + dt.timedelta(days=1)
    last = e.date() - dt.timedelta(days=1)
    if last < first:
        return {}
    days = pd.to_datetime(dates, errors="coerce", utc=True).dt.date
    counts = days.value_counts()
    out: dict[str, int] = {}
    d = first
    while d <= last:
        out[d.isoformat()] = int(counts.get(d, 0))
        d += dt.timedelta(days=1)
    return out


def _weekday_drift(current: dict[str, int], history: list[dict[str, Any]], t: dict[str, float]) -> Check:
    """Like-for-like row-count comparison for a window source (module docstring)."""
    observed: dict[str, int] = {}
    for h in history:
        for day, count in (h.get("daily_counts") or {}).items():
            observed[str(day)] = int(count)  # a later observation of a day wins
    by_weekday: dict[int, list[int]] = {}
    for day in sorted(observed):
        if day in current:
            continue
        by_weekday.setdefault(dt.date.fromisoformat(day).weekday(), []).append(observed[day])
    weeks = int(t["profile_weeks"])
    baseline = {wd: float(median(v[-weeks:])) for wd, v in by_weekday.items() if v}
    comparable = [d for d in current if dt.date.fromisoformat(d).weekday() in baseline]
    if not comparable:
        return Check(
            "row_count_drift",
            "info",
            f"{sum(current.values())} rows over {len(current)} complete day(s); no same-weekday baseline",
            {"basis": "weekday_profile", "days": current},
        )
    cur = sum(current[d] for d in comparable)
    base = sum(baseline[dt.date.fromisoformat(d).weekday()] for d in comparable)
    delta = cur - base
    data = {
        "basis": "weekday_profile",
        "days": {d: current[d] for d in comparable},
        "baseline": round(base, 1),
        "current": cur,
    }
    if base <= 0:
        return Check("row_count_drift", "pass", f"{cur} rows on days that usually have none", data)
    drift = delta / base * 100.0
    data["delta_pct"] = round(drift, 2)
    big_enough = abs(delta) >= t["row_drift_min_rows"]
    level = (
        "hold"
        if abs(drift) > t["row_drift_hold_pct"] and big_enough
        else "warn"
        if abs(drift) >= t["row_drift_warn_pct"] and big_enough
        else "pass"
    )
    return Check(
        "row_count_drift",
        level,
        f"{base:.0f} -> {cur} rows on {len(comparable)} like-for-like day(s) ({drift:+.1f} %)",
        data,
    )


def _is_null(series: pd.Series, *, status: bool) -> pd.Series:
    out = series.isna()
    if status:
        out = out | series.astype("string").fillna("").str.strip().str.lower().isin(_UNKNOWN_STATES)
    elif series.dtype == object or str(series.dtype) == "string":
        out = out | (series.astype("string").fillna("").str.strip() == "")
    return out.fillna(True).astype(bool)


def _nulled_checks(
    kind: str, previous: pd.DataFrame, fetched: pd.DataFrame, t: dict[str, float]
) -> list[Check]:
    """`field_nulled:<field>` for each watched field that was set on a re-fetched row in the
    previous output and is null (status: `unknown`) now (module docstring)."""
    if "record_id" not in previous.columns or "record_id" not in fetched.columns:
        return []
    b = previous.drop_duplicates("record_id").set_index("record_id")
    a = fetched.drop_duplicates("record_id").set_index("record_id")
    common = a.index.intersection(b.index)
    if not len(common):
        return []
    status_col = STATUS_COL[kind]
    out: list[Check] = []
    for col in NULLED_FIELDS[kind]:
        if col not in a.columns or col not in b.columns:
            continue
        status = col == status_col
        was_set = ~_is_null(b.loc[common, col], status=status)
        now_null = _is_null(a.loc[common, col], status=status)
        lost = int((was_set.to_numpy() & now_null.to_numpy()).sum())
        base = int(was_set.sum())
        if not base:
            continue
        share = lost / base
        if share > t["nulled_hold_share"] and lost >= t["nulled_min_rows"]:
            out.append(
                Check(
                    f"field_nulled:{col}",
                    "hold",
                    f"{lost} of {base} re-fetched rows that had {col} have none now ({share * 100:.0f} %)",
                    {"rows": lost, "of": base, "share": round(share, 4)},
                )
            )
    if not out:
        out.append(Check("field_nulled", "pass", f"no watched field lost on {len(common)} re-fetched rows"))
    return out
