#!/usr/bin/env python3
"""Entity resolution over the canonical proposal table (docs/02 §5 key order, docs/20 §3.5).

Passes, in the order docs/02 §5 gives:
  D1  EIA plant id + generator id equal on both sides          (deterministic)
  D2  queue id + ISO equal on both sides                       (deterministic, within-source dupes)
  D3  cross-reference queue id embedded in the other side's project name (deterministic)
  B1  block: state + technology + capacity +/-10%              (candidate generation)
  B2  block: state + county   + capacity +/-10%                (candidate generation)
  F   fuzzy score over name / sponsor / county / capacity / COD, tunable threshold

Reads   data/eval/normalized.parquet
Writes  data/eval/matches.parquet   (pairwise, with score, components, rationale, cluster_id)
        data/eval/clusters.parquet  (one row per cluster member)

Usage:
    python pipeline/resolve.py [--threshold 72] [--sweep] [--labels data/eval/labels.csv]
"""
from __future__ import annotations

import argparse
import itertools
import pathlib
import sys

import pandas as pd
from rapidfuzz import fuzz

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVAL = ROOT / "data" / "eval"

# lifecycle states that mean "still a live proposal" (docs/02 §1 lifecycle, minus terminal states)
ACTIVE_STATES = {"announced", "filed", "studied", "permitted", "contracted", "under_construction"}
ISO_SOURCES = ["caiso", "ercot", "spp", "nyiso", "isone"]

# Component weights. A component only contributes when both sides carry the field; the score is
# the weighted mean over the components that are actually available, and `evidence` is the sum of
# those weights. A pair is only eligible for acceptance when evidence >= MIN_EVIDENCE, which stops
# name-less sources (SPP: 0/3,074 names, 0/3,074 sponsors) being merged on geography alone.
WEIGHTS = {"name": 0.40, "sponsor": 0.25, "county": 0.20, "capacity": 0.10, "cod": 0.05}
MIN_EVIDENCE = 0.50
CAP_TOL = 0.10        # +/-10% capacity band for blocking (docs/02 §5)


# ------------------------------------------------------------------ candidate generation
def _pairs_within_capacity(sub: pd.DataFrame, tol: float = CAP_TOL) -> list[tuple[int, int]]:
    """Two-pointer sweep over capacity-sorted rows; yields index pairs within +/-tol relative."""
    s = sub.sort_values("capacity_mw")
    caps = s["capacity_mw"].to_numpy()
    idx = s.index.to_numpy()
    src = s["source_id"].to_numpy()
    out: list[tuple[int, int]] = []
    j = 0
    for i in range(len(caps)):
        lo = caps[i] * (1 - tol)
        while caps[j] < lo:
            j += 1
        for k in range(j, i):
            if src[k] != src[i]:                      # cross-source only
                out.append((idx[k], idx[i]))
    return out


def block(df: pd.DataFrame, keys: list[str], tag: str) -> pd.DataFrame:
    usable = df.dropna(subset=keys + ["capacity_mw"])
    usable = usable[usable["capacity_mw"] > 0]
    rows: list[tuple[int, int]] = []
    for _, grp in usable.groupby(keys, dropna=True, observed=True):
        if len(grp) < 2 or grp["source_id"].nunique() < 2:
            continue
        rows.extend(_pairs_within_capacity(grp))
    out = pd.DataFrame(rows, columns=["li", "ri"])
    out["block"] = tag
    return out


# ------------------------------------------------------------------ scoring
def _ratio(a, b) -> float | None:
    if not a or not b or pd.isna(a) or pd.isna(b):
        return None
    return float(fuzz.token_set_ratio(str(a), str(b)))


def score_pair(l: dict, r: dict) -> dict:
    comp: dict[str, float | None] = {}
    comp["name"] = _ratio(l["name_norm"], r["name_norm"])
    comp["sponsor"] = _ratio(l["sponsor_norm"], r["sponsor_norm"])

    lc, rc = l["county_norm"], r["county_norm"]
    comp["county"] = None if (not lc or not rc or pd.isna(lc) or pd.isna(rc)) else (
        100.0 if lc == rc else (80.0 if (lc in rc or rc in lc) else 0.0))

    lm, rm = l["capacity_mw"], r["capacity_mw"]
    if lm and rm and not pd.isna(lm) and not pd.isna(rm) and max(lm, rm) > 0:
        ratio = min(lm, rm) / max(lm, rm)
        comp["capacity"] = max(0.0, 100.0 * (ratio - (1 - CAP_TOL)) / CAP_TOL)
        cap_ratio = ratio
    else:
        comp["capacity"], cap_ratio = None, None

    ld, rd = l["proposed_cod"], r["proposed_cod"]
    if pd.notna(ld) and pd.notna(rd):
        days = abs((ld - rd).days)
        comp["cod"] = max(0.0, 100.0 - days / 3.65)
    else:
        days, comp["cod"] = None, None

    avail = {k: v for k, v in comp.items() if v is not None}
    evidence = sum(WEIGHTS[k] for k in avail)
    score = (sum(WEIGHTS[k] * v for k, v in avail.items()) / evidence) if evidence else 0.0

    bits = [f"{k}={comp[k]:.0f}" for k in ("name", "sponsor", "county", "capacity", "cod")
            if comp[k] is not None]
    rationale = f"{'; '.join(bits)}; evidence={evidence:.2f}"
    return {"score": round(score, 2), "name_score": comp["name"], "sponsor_score": comp["sponsor"],
            "county_score": comp["county"], "capacity_score": comp["capacity"],
            "cod_score": comp["cod"], "capacity_ratio": cap_ratio, "cod_days": days,
            "evidence": round(evidence, 2), "rationale": rationale}


# ------------------------------------------------------------------ deterministic passes
def deterministic(df: pd.DataFrame) -> pd.DataFrame:
    out = []

    # D1 - EIA plant id + generator id on both sides
    d = df.dropna(subset=["eia_plant_id", "eia_generator_id"])
    key = d["eia_plant_id"].astype(str) + "|" + d["eia_generator_id"].astype(str)
    for _, grp in d.groupby(key):
        if grp["source_id"].nunique() > 1:
            for a, b in itertools.combinations(grp.index, 2):
                out.append((a, b, "D1_eia_id", 100.0, "eia plant+generator id equal"))

    # D2 - queue id + ISO. Only applied to sources whose queue id is proven unique in this
    # snapshot: ISO-NE reuses queue ids across unrelated projects (92 ids over 242 rows, 27 of
    # them spanning more than one state), so an unguarded D2 would hard-merge different plants.
    d = df.dropna(subset=["queue_id", "iso"])
    dupe_rate = d.groupby("source_id")["queue_id"].apply(lambda s: s.duplicated().any())
    unsafe = set(dupe_rate[dupe_rate].index)
    if unsafe:
        print(f"  D2 disabled for non-unique queue ids: {sorted(unsafe)}", file=sys.stderr)
    d = d[~d["source_id"].isin(unsafe)]
    key = d["iso"].astype(str) + "|" + d["queue_id"].astype(str).str.upper().str.strip()
    for _, grp in d.groupby(key):
        if len(grp) > 1:
            for a, b in itertools.combinations(grp.index, 2):
                out.append((a, b, "D2_queue_id", 100.0, "iso + queue id equal"))

    # D3 - queue id quoted inside another source's project name, e.g. "(NYISO-C24-308)"
    lookup: dict[str, list[int]] = {}
    for i, row in df.dropna(subset=["queue_id", "iso"]).iterrows():
        lookup.setdefault(f"{row['iso']}:{str(row['queue_id']).upper().strip()}", []).append(i)
    for i, refs in df["cross_refs"].dropna().items():
        if not refs:
            continue
        for ref in str(refs).split("|"):
            for j in lookup.get(ref, []):
                if j != i and df.at[i, "source_id"] != df.at[j, "source_id"]:
                    out.append((min(i, j), max(i, j), "D3_xref", 100.0,
                                f"project name cites {ref}"))

    cols = ["li", "ri", "pass", "score", "rationale"]
    res = pd.DataFrame(out, columns=cols)
    return res.drop_duplicates(subset=["li", "ri"])


# ------------------------------------------------------------------ clustering
def cluster(df: pd.DataFrame, accepted: pd.DataFrame) -> pd.Series:
    parent: dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in zip(accepted["li"], accepted["ri"]):
        union(int(a), int(b))
    return pd.Series({i: find(i) for i in df.index if i in parent})


# ------------------------------------------------------------------ evaluation
def evaluate(matches: pd.DataFrame, labels: pd.DataFrame, thresholds) -> pd.DataFrame:
    m = matches.set_index(["left_id", "right_id"])
    rows = []
    for t in thresholds:
        tp = fp = fn = tn = 0
        for _, lab in labels.iterrows():
            key = (lab["left_id"], lab["right_id"])
            rev = (lab["right_id"], lab["left_id"])
            row = m.loc[key] if key in m.index else (m.loc[rev] if rev in m.index else None)
            if row is None:
                pred = False
                sc, ev = 0.0, 0.0
            else:
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                sc, ev = float(row["score"]), float(row["evidence"])
                pred = bool(row["pass"].startswith("D") or (sc >= t and ev >= MIN_EVIDENCE))
            truth = int(lab["label"]) == 1
            tp += pred and truth
            fp += pred and not truth
            fn += (not pred) and truth
            tn += (not pred) and (not truth)
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * prec * rec / (prec + rec) if prec and rec and prec + rec else float("nan")
        rows.append({"threshold": t, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                     "precision": round(prec, 3), "recall": round(rec, 3),
                     "f1": round(f1, 3) if f1 == f1 else None, "n": len(labels)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ driver
def eia_plant_rollup(df: pd.DataFrame) -> pd.DataFrame:
    """EIA-860M is one row per *generator*; ISO queues are one row per *interconnection request*.
    326 of 1,595 planned plants carry more than one generator, so a 300 MW queue entry can face
    three 100 MW EIA rows and fall outside the +/-10% capacity block. Add one synthetic
    plant-level record per (plant, technology) group of size > 1 so both granularities can block."""
    e = df[(df["source_id"] == "eia860m") & df["eia_plant_id"].notna()]
    grp = e.groupby(["eia_plant_id", "technology"], dropna=False)
    rows = []
    order = ["announced", "filed", "permitted", "contracted", "under_construction", "built"]
    for (pid, tech), g in grp:
        if len(g) < 2:
            continue
        first = g.iloc[0]
        states = [s for s in g["lifecycle_state"] if s in order]
        rows.append({**first.to_dict(),
                     "record_id": f"eia860m:plant-{pid}:{tech}",
                     "source_record_id": f"plant-{pid}",
                     "capacity_mw": float(g["capacity_mw"].sum()),
                     "eia_generator_id": None,
                     "proposed_cod": g["proposed_cod"].max(),
                     "lifecycle_state": max(states, key=order.index) if states else "unknown",
                     "status_rule": "eia860m.plant_rollup"})
    return pd.DataFrame(rows)


def run(threshold: float, normalized: pathlib.Path, rollup: bool = True
        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_parquet(normalized)
    df["is_rollup"] = False
    if rollup:
        extra = eia_plant_rollup(df)
        if len(extra):
            extra["is_rollup"] = True
            df = pd.concat([df, extra], ignore_index=True)
            print(f"  EIA plant-level rollup records added: {len(extra):,}", file=sys.stderr)
    df.index = range(len(df))

    det = deterministic(df)
    cand = pd.concat([
        block(df, ["state", "technology"], "B1_state_tech_cap"),
        block(df, ["state", "county_norm"], "B2_state_county_cap"),
    ], ignore_index=True)
    cand = (cand.groupby(["li", "ri"])["block"].apply(lambda s: "+".join(sorted(set(s))))
            .reset_index())
    print(f"  candidate pairs from blocking: {len(cand):,}", file=sys.stderr)

    recs = df.to_dict("index")
    scored = [score_pair(recs[int(a)], recs[int(b)]) for a, b in zip(cand["li"], cand["ri"])]
    fuzzy = pd.concat([cand.reset_index(drop=True), pd.DataFrame(scored)], axis=1)
    fuzzy["pass"] = "F_fuzzy:" + fuzzy["block"]
    fuzzy["rationale"] = fuzzy["block"] + "; " + fuzzy["rationale"]

    det = det.assign(block="deterministic", evidence=1.0)
    matches = pd.concat([det, fuzzy.drop(columns=["block"])], ignore_index=True, sort=False)
    matches = matches.sort_values("score", ascending=False).drop_duplicates(subset=["li", "ri"])

    matches["accepted"] = matches["pass"].str.startswith("D") | (
        (matches["score"] >= threshold) & (matches["evidence"] >= MIN_EVIDENCE))

    accepted = matches[matches["accepted"]]
    roots = cluster(df, accepted)
    matches["cluster_id"] = matches["li"].map(roots)

    for side in ("l", "r"):
        i = matches[f"{side}i"].astype(int)
        for col, out in [("record_id", "id"), ("source_id", "source"), ("name_canonical", "name"),
                         ("sponsor_name", "sponsor"), ("state", "state"), ("county", "county"),
                         ("technology", "tech"), ("capacity_mw", "mw"),
                         ("lifecycle_state", "state_lc"), ("proposed_cod", "cod")]:
            matches[f"{'left' if side == 'l' else 'right'}_{out}"] = df[col].reindex(i).to_numpy()

    clusters = df.copy()
    clusters["cluster_id"] = roots.reindex(clusters.index)
    clusters = clusters.dropna(subset=["cluster_id"])
    return matches, clusters


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=72.0)
    ap.add_argument("--normalized", default=str(EVAL / "normalized.parquet"))
    ap.add_argument("--out", default=str(EVAL / "matches.parquet"))
    ap.add_argument("--labels", default=str(EVAL / "labels.csv"))
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--no-eia-rollup", action="store_true",
                    help="disable the EIA plant-level rollup blocking view")
    ap.add_argument("--max-rows", type=int, default=400_000,
                    help="cap on rows written to matches.parquet (keeps the file under 20 MB)")
    args = ap.parse_args()

    matches, clusters = run(args.threshold, pathlib.Path(args.normalized),
                            rollup=not args.no_eia_rollup)

    out = pathlib.Path(args.out)
    keep = ["left_id", "right_id", "pass", "score", "evidence", "accepted", "cluster_id",
            "name_score", "sponsor_score", "county_score", "capacity_score", "cod_score",
            "capacity_ratio", "cod_days", "rationale",
            "left_source", "right_source", "left_name", "right_name", "left_sponsor",
            "right_sponsor", "left_state", "right_state", "left_county", "right_county",
            "left_tech", "right_tech", "left_mw", "right_mw", "left_state_lc", "right_state_lc",
            "left_cod", "right_cod"]
    written = matches[keep]
    sampled = False
    if len(written) > args.max_rows:
        written = pd.concat([written[written["accepted"]],
                             written[~written["accepted"]].sample(
                                 n=max(0, args.max_rows - int(written["accepted"].sum())),
                                 random_state=0)])
        sampled = True
    written.to_parquet(out, index=False)
    clusters.to_parquet(EVAL / "clusters.parquet", index=False)

    print(f"pairs scored: {len(matches):,}   written: {len(written):,}"
          f"{' (SAMPLED: rejected pairs down-sampled)' if sampled else ''}"
          f" -> {out} ({out.stat().st_size/1e6:.2f} MB)")
    print("\npairs by pass")
    print(matches["pass"].str.split(":").str[0].value_counts().to_string())
    print(f"\naccepted pairs at threshold {args.threshold}: {int(matches['accepted'].sum()):,}")
    print("\naccepted pairs by source pair")
    acc = matches[matches["accepted"]]
    sp = (acc["left_source"] + " x " + acc["right_source"]).value_counts()
    print(sp.to_string() if len(sp) else "  (none)")

    ncl = clusters["cluster_id"].nunique()
    sizes = clusters.groupby("cluster_id").size()
    print(f"\nclusters: {ncl:,} covering {len(clusters):,} records "
          f"(max size {int(sizes.max()) if ncl else 0})")
    print("cluster size distribution:", sizes.value_counts().sort_index().to_dict() if ncl else {})
    multi = clusters.groupby("cluster_id")["source_id"].nunique()
    print(f"clusters spanning >1 source: {int((multi > 1).sum()):,}")

    # --- headline measurement: ACTIVE queue records linked to an EIA-860M planned unit
    norm = pd.read_parquet(args.normalized)
    iso = norm[norm["source_id"].isin(ISO_SOURCES)]
    active = iso[iso["lifecycle_state"].isin(ACTIVE_STATES)]
    linked_ids = set()
    for _, r in acc.iterrows():
        if r["left_source"] == "eia860m":
            linked_ids.add(r["right_id"])
        elif r["right_source"] == "eia860m":
            linked_ids.add(r["left_id"])
    n_act = len(active)
    n_link = active["record_id"].isin(linked_ids).sum()
    print(f"\nACTIVE (non-terminal lifecycle) ISO queue records: {n_act:,}")
    print(f"  linked to >=1 EIA-860M planned unit: {n_link:,} ({100*n_link/n_act:.1f}%)")
    per = (active.assign(linked=active["record_id"].isin(linked_ids))
           .groupby("source_id")["linked"].agg(["sum", "count"]))
    per["rate_%"] = (100 * per["sum"] / per["count"]).round(1)
    print(per.to_string())
    raw_active = iso[iso["status_raw"].str.upper() == "ACTIVE"]
    nra = len(raw_active)
    nrl = raw_active["record_id"].isin(linked_ids).sum()
    print(f"raw-status ACTIVE rows: {nra:,}; linked: {nrl:,} ({100*nrl/nra:.1f}%)")

    # --- cross-source duplicate counts
    print("\ncross-source duplicate pairs (accepted, different sources): "
          f"{int((acc['left_source'] != acc['right_source']).sum()):,}")
    iso_only = acc[(acc["left_source"] != acc["right_source"]) &
                   (acc["left_source"] != "eia860m") & (acc["right_source"] != "eia860m")]
    print(f"  of which ISO-to-ISO (double-queued projects): {len(iso_only):,}")

    labels_path = pathlib.Path(args.labels)
    if labels_path.exists():
        labels = pd.read_csv(labels_path)
        ts = [50, 55, 60, 65, 70, 72, 75, 80, 85, 90] if args.sweep else [args.threshold]
        print(f"\nevaluation on {len(labels)} hand-labelled pairs "
              f"({int((labels['label'] == 1).sum())} positive / "
              f"{int((labels['label'] == 0).sum())} negative)")
        print(evaluate(written, labels, ts).to_string(index=False))
    else:
        print(f"\n(no labels at {labels_path}; skipping precision/recall)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
