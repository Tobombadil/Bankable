#!/usr/bin/env python3
"""Snapshot diff -> change events (docs/20 §3.3, docs/02 §1 "the value is in the change events").

Compares two normalised snapshots keyed by `record_id` and emits one row per event:
    new              record present in the new snapshot only
    removed          record present in the old snapshot only (never a hard delete downstream;
                     ERCOT and EIA-860M only ever signal withdrawal/completion this way)
    withdrawn        lifecycle_state moved to withdrawn or cancelled
    status_change    lifecycle_state changed to any other state
    capacity_change  capacity_mw changed by more than CAP_ABS_MW and CAP_REL
    cod_change       proposed_cod changed

Usage:
    python pipeline/diff.py --before A.parquet --after B.parquet [--out events.parquet]
    python pipeline/diff.py --demo            # perturb today's normalized.parquet and diff it
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVAL = ROOT / "data" / "eval"

CAP_ABS_MW = 0.5      # ignore rounding noise below both of these
CAP_REL = 0.01
TERMINAL = {"withdrawn", "cancelled"}
EVENT_TYPES = ["new", "status_change", "capacity_change", "cod_change", "withdrawn", "removed"]
KEY = "record_id"


def _s(v):
    """Stringify a scalar for the before/after columns (None for missing)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    return str(v)


def diff_snapshots(before: pd.DataFrame, after: pd.DataFrame,
                   observed_at: str | None = None) -> pd.DataFrame:
    """Deterministic, model-free diff of two snapshots keyed by record_id."""
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    b = before.drop_duplicates(KEY).set_index(KEY)
    a = after.drop_duplicates(KEY).set_index(KEY)
    events: list[dict] = []

    def emit(rid, etype, field=None, bv=None, av=None, src=None):
        events.append({"event_type": etype, "record_id": rid, "source_id": src, "field": field,
                       "before": _s(bv), "after": _s(av), "observed_at": observed_at})

    for rid in a.index.difference(b.index):
        emit(rid, "new", "lifecycle_state", None, a.at[rid, "lifecycle_state"], a.at[rid, "source_id"])
    for rid in b.index.difference(a.index):
        emit(rid, "removed", "lifecycle_state", b.at[rid, "lifecycle_state"], None, b.at[rid, "source_id"])

    common = a.index.intersection(b.index)
    ab, aa = b.loc[common].copy(), a.loc[common].copy()
    ab.index.name = aa.index.name = KEY

    bs = ab["lifecycle_state"].astype("string").fillna("").to_numpy()
    as_ = aa["lifecycle_state"].astype("string").fillna("").to_numpy()
    changed = bs != as_
    for rid in common[changed]:
        new_state = aa.at[rid, "lifecycle_state"]
        etype = "withdrawn" if new_state in TERMINAL and ab.at[rid, "lifecycle_state"] not in TERMINAL \
            else "status_change"
        emit(rid, etype, "lifecycle_state", ab.at[rid, "lifecycle_state"], new_state, aa.at[rid, "source_id"])

    bc = pd.to_numeric(ab["capacity_mw"], errors="coerce").astype(float).to_numpy()
    ac = pd.to_numeric(aa["capacity_mw"], errors="coerce").astype(float).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        delta = np.abs(ac - bc)
        rel = delta / np.maximum(np.abs(bc), np.abs(ac))
    cap_changed = ((delta > CAP_ABS_MW) & (rel > CAP_REL)) | (np.isnan(bc) ^ np.isnan(ac))
    for i in np.flatnonzero(cap_changed):
        rid = common[i]
        emit(rid, "capacity_change", "capacity_mw", bc[i], ac[i], aa.at[rid, "source_id"])

    bd = pd.to_datetime(ab["proposed_cod"], errors="coerce").to_numpy()
    ad = pd.to_datetime(aa["proposed_cod"], errors="coerce").to_numpy()
    cod_changed = (bd != ad) & ~(pd.isna(bd) & pd.isna(ad))
    for i in np.flatnonzero(cod_changed):
        rid = common[i]
        emit(rid, "cod_change", "proposed_cod", pd.Timestamp(bd[i]) if not pd.isna(bd[i]) else None,
             pd.Timestamp(ad[i]) if not pd.isna(ad[i]) else None, aa.at[rid, "source_id"])

    cols = ["event_type", "record_id", "source_id", "field", "before", "after", "observed_at"]
    out = pd.DataFrame(events, columns=cols)
    out["event_type"] = pd.Categorical(out["event_type"], categories=EVENT_TYPES)
    return out.sort_values(["event_type", "record_id"]).reset_index(drop=True)


# ------------------------------------------------------------------ synthetic perturbation
def perturb(df: pd.DataFrame, seed: int = 0, n_new: int = 50, n_status: int = 80,
            n_capacity: int = 60, n_cod: int = 70, n_withdrawn: int = 40, n_removed: int = 30
            ) -> tuple[pd.DataFrame, dict[str, int]]:
    """Return (perturbed copy, expected event counts). Each perturbation hits a disjoint set of
    rows so the expected counts are exact. Only non-terminal rows are used for status/withdrawn."""
    rng = np.random.default_rng(seed)
    out = df.copy().reset_index(drop=True)
    live = out.index[~out["lifecycle_state"].isin(TERMINAL | {"unknown", "built"})].to_numpy()
    rng.shuffle(live)
    need = n_status + n_withdrawn + n_capacity + n_cod + n_removed
    if len(live) < need:
        raise ValueError(f"not enough live rows ({len(live)}) for {need} perturbations")
    cur = 0

    def take(n):
        nonlocal cur
        sel = live[cur:cur + n]
        cur += n
        return sel

    # status_change: move to the next non-terminal state that differs
    ladder = ["announced", "filed", "studied", "permitted", "contracted", "under_construction"]
    sel = take(n_status)
    out.loc[sel, "lifecycle_state"] = [
        ladder[(ladder.index(s) + 1) % len(ladder)] if s in ladder else "studied"
        for s in out.loc[sel, "lifecycle_state"]]
    # withdrawn
    sel = take(n_withdrawn)
    out.loc[sel, "lifecycle_state"] = "withdrawn"
    # capacity_change: +20 % or +1 MW, whichever is larger, so the change clears the diff's
    # noise floor (CAP_ABS_MW / CAP_REL) on small records too; null capacities are filled first
    sel = take(n_capacity)
    cap = pd.to_numeric(out.loc[sel, "capacity_mw"], errors="coerce").fillna(100.0).astype(float)
    out.loc[sel, "capacity_mw"] = np.maximum(cap * 1.2, cap + 1.0).round(2).to_numpy()
    # cod_change: +180 days, filling missing CODs first
    sel = take(n_cod)
    cod = pd.to_datetime(out.loc[sel, "proposed_cod"], errors="coerce").fillna(pd.Timestamp("2028-01-01"))
    # the fill itself counts as a change (None -> date), which is what a real snapshot would show
    out.loc[sel, "proposed_cod"] = (cod + pd.Timedelta(days=180)).to_numpy()
    # removed
    sel = take(n_removed)
    out = out.drop(index=sel)
    # new: clone rows under fresh ids
    src = out.sample(n=n_new, random_state=seed).copy()
    src["record_id"] = [f"synthetic:new-{i:04d}" for i in range(n_new)]
    src["source_record_id"] = src["record_id"].str.split(":").str[1]
    out = pd.concat([out, src], ignore_index=True)

    expected = {"new": n_new, "status_change": n_status, "capacity_change": n_capacity,
                "cod_change": n_cod, "withdrawn": n_withdrawn, "removed": n_removed}
    return out, expected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--out", default=str(EVAL / "events.parquet"))
    ap.add_argument("--demo", action="store_true",
                    help="diff data/eval/normalized.parquet against a seeded synthetic perturbation")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.demo:
        before = pd.read_parquet(EVAL / "normalized.parquet")
        after, expected = perturb(before, seed=args.seed)
        after.to_parquet(EVAL / "normalized.perturbed.parquet", index=False)
        print(f"perturbed copy: {len(before):,} -> {len(after):,} rows "
              f"-> {EVAL / 'normalized.perturbed.parquet'}")
    else:
        if not (args.before and args.after):
            ap.error("--before and --after are required unless --demo")
        before, after = pd.read_parquet(args.before), pd.read_parquet(args.after)
        expected = None

    events = diff_snapshots(before, after)
    out = pathlib.Path(args.out)
    events.to_parquet(out, index=False)
    counts = events["event_type"].value_counts().reindex(EVENT_TYPES, fill_value=0)
    print(f"\n{len(events):,} events -> {out}")
    table = counts.rename("observed").to_frame()
    if expected:
        table["expected"] = pd.Series(expected)
        table["ok"] = table["observed"] == table["expected"]
    print(table.to_string())
    print("\nevents by source")
    print(pd.crosstab(events["source_id"], events["event_type"]).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
