"""Data-quality gates run at the end of every source run (docs/04 DA-6, docs/20 §10, §12).

| Check                          | warn                              | hold                                  |
|--------------------------------|-----------------------------------|---------------------------------------|
| row-count drift vs previous ok | |delta| >= 10 %                   | |delta| > 30 % either direction       |
| vocabulary drift               | any new status/technology value   | unmapped status > 5 % of rows         |
| null spike on required field   | +5 pp vs trailing median (5 runs) | +10 pp                                |
| duplicate record_id            | suffixed duplicates (declared)    | any remaining                         |
| provenance completeness        | —                                 | any row missing the quartet           |
| schema drift (source columns)  | any added/removed column          | a removed column that feeds a canonical field |

A held run keeps its raw snapshot (evidence) but writes nothing publishable and records why
(`source_run.dq`). Thresholds are per-source configuration with these defaults.
"""

from __future__ import annotations

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
}
STATUS_COL = {"proposal": "lifecycle_state", "opportunity": "status"}
DEFAULT_REQUIRED = {
    "proposal": ("name_canonical", "capacity_mw", "technology_raw", "state"),
    "opportunity": ("title", "issuer", "jurisdiction", "due_at"),
}
VOCAB_COLS = {"proposal": ("status_raw", "technology_raw", "kind"), "opportunity": ("status_raw", "kind")}


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


def snapshot_stats(df: pd.DataFrame, kind: str, source_columns: list[str],
                   required: tuple[str, ...] = ()) -> dict[str, Any]:
    """The per-run numbers later runs compare against (stored on source_run)."""
    req = required or DEFAULT_REQUIRED[kind]
    n = len(df)
    null_rates = {c: (float(df[c].isna().mean() * 100) if c in df.columns and n else 0.0) for c in req}
    vocab: dict[str, list[str]] = {}
    for c in VOCAB_COLS[kind]:
        if c in df.columns:
            vocab[c] = sorted({str(v) for v in df[c].dropna().unique()})[:500]
    return {"rows": n, "null_rates_pct": null_rates, "vocabulary": vocab, "source_columns": source_columns}


def run_gates(df: pd.DataFrame, kind: str, source_columns: list[str], history: list[dict[str, Any]],
              *, thresholds: dict[str, float] | None = None, required: tuple[str, ...] = (),
              key_source_columns: tuple[str, ...] = (), duplicates_resolved: int = 0,
              rows_fetched: int | None = None) -> DQResult:
    """Evaluate every gate. `history` is the list of previous *successful* runs' `stats` dicts,
    oldest first; `rows_fetched` overrides the row count used for drift (incremental sources)."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    req = required or DEFAULT_REQUIRED[kind]
    checks: list[Check] = []
    stats = snapshot_stats(df, kind, source_columns, req)
    n = rows_fetched if rows_fetched is not None else len(df)
    stats["rows_fetched"] = n
    prev = history[-1] if history else None

    # 1. row-count drift
    if prev and prev.get("rows_fetched", prev.get("rows")):
        base = int(prev.get("rows_fetched", prev.get("rows")))
        delta = (n - base) / base * 100.0
        level = ("hold" if abs(delta) > t["row_drift_hold_pct"] else
                 "warn" if abs(delta) >= t["row_drift_warn_pct"] else "pass")
        checks.append(Check("row_count_drift", level, f"{base} -> {n} rows ({delta:+.1f} %)",
                            {"previous": base, "current": n, "delta_pct": round(delta, 2)}))
    else:
        checks.append(Check("row_count_drift", "info", f"no baseline; {n} rows", {"current": n}))

    # 2. vocabulary drift: unmapped statuses (rule id ends with .unmapped) and new raw values
    status_col = STATUS_COL[kind]
    if "status_rule" in df.columns and len(df):
        unmapped = df["status_rule"].astype("string").str.endswith(".unmapped").fillna(False)
        share = float(unmapped.mean())
        values = sorted({str(v) for v in df.loc[unmapped, "status_raw"].dropna().unique()})[:50]
        level = "hold" if share > t["unmapped_hold_share"] else "warn" if share > 0 else "pass"
        checks.append(Check("vocabulary_unmapped", level,
                            f"{int(unmapped.sum())} rows ({share * 100:.1f} %) with unmapped {status_col}",
                            {"share": round(share, 4), "values": values}))
    for col, values in stats["vocabulary"].items():
        prev_vals = set((prev or {}).get("vocabulary", {}).get(col, [])) if prev else None
        if prev_vals is None:
            continue
        new = sorted(set(values) - prev_vals)
        if new:
            checks.append(Check("vocabulary_drift", "warn", f"new {col} values: {new[:10]}",
                                {"column": col, "new_values": new[:50]}))
    if not any(c.check == "vocabulary_drift" for c in checks):
        checks.append(Check("vocabulary_drift", "pass" if prev else "info", "no new values"))

    # 3. null spikes vs trailing median of the last N successful runs
    for col in req:
        rate = stats["null_rates_pct"].get(col, 0.0)
        hist = [h["null_rates_pct"][col] for h in history[-int(t["null_history_runs"]):]
                if col in h.get("null_rates_pct", {})]
        if not hist:
            checks.append(Check(f"null_rate:{col}", "info", f"{rate:.1f} % null (no baseline)",
                                {"rate_pct": round(rate, 2)}))
            continue
        base = float(median(hist))
        delta = rate - base
        level = ("hold" if delta >= t["null_spike_hold_pp"] else
                 "warn" if delta >= t["null_spike_warn_pp"] else "pass")
        checks.append(Check(f"null_rate:{col}", level, f"{rate:.1f} % null vs median {base:.1f} % ({delta:+.1f} pp)",
                            {"rate_pct": round(rate, 2), "baseline_pct": round(base, 2), "delta_pp": round(delta, 2)}))

    # 4. duplicate keys
    dups = int(df["record_id"].duplicated().sum()) if "record_id" in df.columns else 0
    if dups:
        checks.append(Check("duplicate_keys", "hold", f"{dups} duplicate record_id values", {"count": dups}))
    elif duplicates_resolved:
        checks.append(Check("duplicate_keys", "warn",
                            f"{duplicates_resolved} duplicate source ids suffixed deterministically",
                            {"resolved": duplicates_resolved}))
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
    checks.append(Check("provenance", "hold" if missing else "pass",
                        f"{missing} provenance cells missing" if missing else "quartet present on every row",
                        {"missing_cells": missing}))

    # 6. schema drift of the source columns
    if prev and prev.get("source_columns") is not None:
        before, after = set(prev["source_columns"]), set(source_columns)
        added, removed = sorted(after - before), sorted(before - after)
        key_removed = sorted(set(removed) & set(key_source_columns))
        if key_removed:
            checks.append(Check("schema_drift", "hold", f"removed key columns: {key_removed}",
                                {"added": added, "removed": removed}))
        elif added or removed:
            checks.append(Check("schema_drift", "warn", f"added {added[:10]} removed {removed[:10]}",
                                {"added": added, "removed": removed}))
        else:
            checks.append(Check("schema_drift", "pass", "source columns unchanged"))
    else:
        checks.append(Check("schema_drift", "info", f"{len(source_columns)} source columns recorded"))

    levels = {c.level for c in checks}
    status = "hold" if "hold" in levels else "warn" if "warn" in levels else "pass"
    return DQResult(status=status, checks=checks, stats=stats)
