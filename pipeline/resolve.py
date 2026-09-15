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
    python pipeline/resolve.py [--threshold 75] [--sweep] [--labels data/eval/labels.csv]
"""

from __future__ import annotations

import argparse
import itertools
import pathlib
import re
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
CAP_TOL = 0.10  # +/-10% capacity band for blocking (docs/02 §5)


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
            if src[k] != src[i]:  # cross-source only
                out.append((idx[k], idx[i]))
    return out


def block_name_token(df: pd.DataFrame, tag: str = "B3_state_nametoken") -> pd.DataFrame:
    """Block on state + the first >=4-character token of the normalised name, with NO capacity
    constraint. B1/B2 both require capacity within +/-10%; measured against a name-only oracle
    that band is the single biggest recall limiter, because ISO queue MW (point of
    interconnection) and EIA nameplate MW frequently disagree by 20-60%, and because 1,774
    records carry no usable capacity at all."""
    d = df.dropna(subset=["state", "name_norm"]).copy()
    d["tok"] = d["name_norm"].astype(str).str.split().str[0]
    d = d[d["tok"].str.len() >= 4]
    rows: list[tuple[int, int]] = []
    for _, grp in d.groupby(["state", "tok"]):
        if len(grp) < 2 or grp["source_id"].nunique() < 2 or len(grp) > 60:
            continue
        idx, src = grp.index.to_numpy(), grp["source_id"].to_numpy()
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if src[a] != src[b]:
                    rows.append((min(idx[a], idx[b]), max(idx[a], idx[b])))
    out = pd.DataFrame(rows, columns=["li", "ri"])
    out["block"] = tag
    return out


def block(df: pd.DataFrame, keys: list[str], tag: str) -> pd.DataFrame:
    usable = df.dropna(subset=[*keys, "capacity_mw"])
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
NAME_SCORER = "mean"  # token_set | token_sort | mean; set by main() / tests


def _ratio(a, b, scorer: str = "token_set") -> float | None:
    if a is None or b is None or pd.isna(a) or pd.isna(b) or not a or not b:
        return None
    a, b = str(a), str(b)
    if scorer == "token_sort":
        return float(fuzz.token_sort_ratio(a, b))
    if scorer == "mean":
        # token_set_ratio returns 100 whenever one name's tokens are a subset of the other's
        # ("SANDOW" vs "SANDOW LAKES"), which over-merges. token_sort_ratio penalises the extra
        # tokens. The mean keeps abbreviation tolerance without the subset blind spot.
        return (float(fuzz.token_set_ratio(a, b)) + float(fuzz.token_sort_ratio(a, b))) / 2
    return float(fuzz.token_set_ratio(a, b))


PHASE_TOKEN = re.compile(r"(?<![A-Za-z0-9])(\d{1,3}|I{1,3}|IV|V|VI{1,3}|IX|X)(?![A-Za-z0-9])")
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def phase_tokens(name) -> set[int]:
    """Phase / unit numbers in a raw project name: 'Lazy U Solar 2' -> {2}, 'Solar Star III' -> {3}."""
    if name is None or pd.isna(name):
        return set()
    return {ROMAN.get(t, None) or int(t) for t in PHASE_TOKEN.findall(str(name)) if t.isdigit() or t in ROMAN}


def score_pair(left: dict, r: dict) -> dict:
    comp: dict[str, float | None] = {}
    comp["name"] = _ratio(left["name_norm"], r["name_norm"], NAME_SCORER)
    comp["sponsor"] = _ratio(left["sponsor_norm"], r["sponsor_norm"], NAME_SCORER)
    flags = []

    # Rule P: numbered phases. 'Lazy U Solar 1' vs 'Lazy U Solar 2' score 88 on tokens but are
    # different interconnection requests. When both names carry phase numbers and the sets
    # disagree, halve the name score. (6 of 10 false positives at threshold 72 before this rule.)
    pl, pr = phase_tokens(left["name_canonical"]), phase_tokens(r["name_canonical"])
    if comp["name"] is not None and pl and pr and pl != pr:
        comp["name"] *= 0.5
        flags.append("phase_conflict")

    # Rule S: SPV-vs-developer sponsor naming. EIA reports the project SPV ('Freestone Solar LLC')
    # where the ISO reports the developer (or vice versa). When the project name and county agree
    # almost exactly, sponsor disagreement is uninformative, so the component is dropped.
    if (
        comp["sponsor"] is not None
        and comp["sponsor"] < 50
        and (comp["name"] or 0) >= 90
        and left["county_norm"]
        and r["county_norm"]
        and left["county_norm"] == r["county_norm"]
    ):
        comp["sponsor"] = None
        flags.append("sponsor_ignored")

    lc, rc = left["county_norm"], r["county_norm"]
    comp["county"] = (
        None
        if (lc is None or rc is None or pd.isna(lc) or pd.isna(rc) or not lc or not rc)
        else (100.0 if lc == rc else (80.0 if (lc in rc or rc in lc) else 0.0))
    )

    lm, rm = left["capacity_mw"], r["capacity_mw"]
    if pd.notna(lm) and pd.notna(rm) and lm and rm and max(lm, rm) > 0:
        ratio = min(lm, rm) / max(lm, rm)
        comp["capacity"] = max(0.0, 100.0 * (ratio - (1 - CAP_TOL)) / CAP_TOL)
        cap_ratio = ratio
    else:
        comp["capacity"], cap_ratio = None, None

    ld, rd = left["proposed_cod"], r["proposed_cod"]
    if pd.notna(ld) and pd.notna(rd):
        days = abs((ld - rd).days)
        comp["cod"] = max(0.0, 100.0 - days / 3.65)
    else:
        days, comp["cod"] = None, None

    avail = {k: v for k, v in comp.items() if v is not None}
    evidence = sum(WEIGHTS[k] for k in avail)
    score = (sum(WEIGHTS[k] * v for k, v in avail.items()) / evidence) if evidence else 0.0

    # Rule V: vintage. A request withdrawn with a COD more than 5 years from the other side's COD
    # is a different proposal even when the name matches ('SIENNA' 2018 vs 'Sienna Solar Farm' 2028).
    if days is not None and days > 5 * 365 and "withdrawn" in (left["lifecycle_state"], r["lifecycle_state"]):
        score *= 0.8
        flags.append("stale_withdrawn")

    bits = [
        f"{k}={comp[k]:.0f}" for k in ("name", "sponsor", "county", "capacity", "cod") if comp[k] is not None
    ]
    rationale = f"{'; '.join(bits)}; evidence={evidence:.2f}" + (f"; {','.join(flags)}" if flags else "")
    return {
        "score": round(score, 2),
        "name_score": comp["name"],
        "sponsor_score": comp["sponsor"],
        "county_score": comp["county"],
        "capacity_score": comp["capacity"],
        "cod_score": comp["cod"],
        "capacity_ratio": cap_ratio,
        "cod_days": days,
        "evidence": round(evidence, 2),
        "rationale": rationale,
    }


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
    key = d["iso"].astype(str) + "|" + d["queue_id"].astype(str).str.upper().str.strip()
    rejected = 0
    for _, grp in d.groupby(key):
        if len(grp) < 2:
            continue
        if grp["state"].nunique(dropna=True) > 1:  # same id, different states -> id reuse
            rejected += 1
            continue
        for a, b in itertools.combinations(grp.index, 2):
            ca, cb = df.at[a, "county_norm"], df.at[b, "county_norm"]
            if pd.notna(ca) and pd.notna(cb) and ca != cb:  # same id, different county
                rejected += 1
                continue
            la, lb = df.at[a, "name_norm"], df.at[b, "name_norm"]
            if pd.notna(la) and pd.notna(lb) and la and lb and fuzz.token_set_ratio(str(la), str(lb)) < 60:
                rejected += 1
                continue
            out.append((a, b, "D2_queue_id", 100.0, "iso + queue id equal, state/county consistent"))
    if rejected:
        print(f"  D2 groups/pairs rejected as queue-id reuse: {rejected}", file=sys.stderr)

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
                    out.append((min(i, j), max(i, j), "D3_xref", 100.0, f"project name cites {ref}"))

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

    for a, b in zip(accepted["li"], accepted["ri"], strict=True):
        union(int(a), int(b))
    return pd.Series({i: find(i) for i in df.index if i in parent})


# ------------------------------------------------------------------ evaluation
def evaluate(matches: pd.DataFrame, labels: pd.DataFrame, thresholds) -> pd.DataFrame:
    """Precision/recall on hand labels. Rows with label -1 (uncertain) are excluded.

    Two views are reported: `sample_*` computed on the labelled pairs as they are, and
    `weighted_*` where each labelled pair is weighted by (population pairs in its score band /
    labelled pairs in that band), which corrects for the stratified sampling of labels.csv."""
    labels = labels[labels["label"].isin([0, 1])].copy()
    m = matches.drop_duplicates(subset=["left_id", "right_id"]).set_index(["left_id", "right_id"])
    bands = [
        (95, 101),
        (88, 95),
        (82, 88),
        (76, 82),
        (72, 76),
        (66, 72),
        (60, 66),
        (50, 60),
        (35, 50),
        (0, 35),
    ]
    pop = matches[matches["evidence"] >= MIN_EVIDENCE]

    def band(sc):
        for lo, hi in bands:
            if lo <= sc < hi:
                return (lo, hi)
        return (0, 35)

    found = []
    for _, lab in labels.iterrows():
        key = (lab["left_id"], lab["right_id"])
        rev = (lab["right_id"], lab["left_id"])
        row = m.loc[key] if key in m.index else (m.loc[rev] if rev in m.index else None)
        if row is None:
            found.append((None, 0.0, 0.0, False))
        else:
            found.append(
                (row, float(row["score"]), float(row["evidence"]), bool(str(row["pass"]).startswith("D")))
            )
    labels["score"] = [f[1] for f in found]
    labels["evidence"] = [f[2] for f in found]
    labels["det"] = [f[3] for f in found]
    labels["band"] = labels["score"].map(band)
    band_pop = pop["score"].map(band).value_counts()
    band_n = labels["band"].value_counts()
    labels["w"] = labels["band"].map(lambda b: band_pop.get(b, 0) / band_n.get(b, 1))
    missing = int((labels["evidence"] == 0).sum())

    rows = []
    for t in thresholds:
        pred = labels["det"] | ((labels["score"] >= t) & (labels["evidence"] >= MIN_EVIDENCE))
        truth = labels["label"] == 1
        tp, fp = int((pred & truth).sum()), int((pred & ~truth).sum())
        fn, tn = int((~pred & truth).sum()), int((~pred & ~truth).sum())
        w = labels["w"]
        wtp, wfp = float(w[pred & truth].sum()), float(w[pred & ~truth].sum())
        wfn = float(w[~pred & truth].sum())
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        wprec = wtp / (wtp + wfp) if wtp + wfp else float("nan")
        wrec = wtp / (wtp + wfn) if wtp + wfn else float("nan")
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else float("nan")
        rows.append(
            {
                "threshold": t,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "sample_precision": round(prec, 3),
                "sample_recall": round(rec, 3),
                "sample_f1": round(f1, 3),
                "weighted_precision": round(wprec, 3),
                "weighted_recall": round(wrec, 3),
                "n": len(labels),
                "unmatched_labels": missing,
            }
        )
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
        rows.append(
            {
                **first.to_dict(),
                "record_id": f"eia860m:plant-{pid}:{tech}",
                "source_record_id": f"plant-{pid}",
                "capacity_mw": float(g["capacity_mw"].sum()),
                "eia_generator_id": None,
                "proposed_cod": g["proposed_cod"].max(),
                "lifecycle_state": max(states, key=order.index) if states else "unknown",
                "status_rule": "eia860m.plant_rollup",
            }
        )
    return pd.DataFrame(rows)


def run(threshold: float, normalized: pathlib.Path, rollup: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_parquet(normalized)
    df["is_rollup"] = False
    if rollup:
        extra = eia_plant_rollup(df)
        if len(extra):
            extra["is_rollup"] = True
            extra = extra.reindex(columns=df.columns).astype(
                {c: t for c, t in df.dtypes.items() if c in extra.columns and t.name != "object"},
                errors="ignore",
            )
            df = pd.concat([df, extra], ignore_index=True)
            print(f"  EIA plant-level rollup records added: {len(extra):,}", file=sys.stderr)
    df.index = range(len(df))

    det = deterministic(df)
    cand = pd.concat(
        [
            block(df, ["state", "technology"], "B1_state_tech_cap"),
            block(df, ["state", "county_norm"], "B2_state_county_cap"),
            block_name_token(df),
        ],
        ignore_index=True,
    )
    cand = cand.groupby(["li", "ri"])["block"].apply(lambda s: "+".join(sorted(set(s)))).reset_index()
    print(f"  candidate pairs from blocking: {len(cand):,}", file=sys.stderr)

    recs = df.to_dict("index")
    scored = [score_pair(recs[int(a)], recs[int(b)]) for a, b in zip(cand["li"], cand["ri"], strict=True)]
    fuzzy = pd.concat([cand.reset_index(drop=True), pd.DataFrame(scored)], axis=1)
    fuzzy["pass"] = "F_fuzzy:" + fuzzy["block"]
    fuzzy["rationale"] = fuzzy["block"] + "; " + fuzzy["rationale"]

    det = det.assign(block="deterministic", evidence=1.0)
    matches = pd.concat([det, fuzzy.drop(columns=["block"])], ignore_index=True, sort=False)
    matches = matches.sort_values("score", ascending=False).drop_duplicates(subset=["li", "ri"])

    matches["accepted"] = matches["pass"].str.startswith("D") | (
        (matches["score"] >= threshold) & (matches["evidence"] >= MIN_EVIDENCE)
    )

    accepted = matches[matches["accepted"]]
    roots = cluster(df, accepted)
    matches["cluster_id"] = matches["li"].map(roots)

    for side in ("l", "r"):
        i = matches[f"{side}i"].astype(int)
        for col, out in [
            ("record_id", "id"),
            ("source_id", "source"),
            ("name_canonical", "name"),
            ("sponsor_name", "sponsor"),
            ("state", "state"),
            ("county", "county"),
            ("technology", "tech"),
            ("capacity_mw", "mw"),
            ("lifecycle_state", "state_lc"),
            ("proposed_cod", "cod"),
        ]:
            matches[f"{'left' if side == 'l' else 'right'}_{out}"] = df[col].reindex(i).to_numpy()

    clusters = df.copy()
    clusters["cluster_id"] = roots.reindex(clusters.index)
    clusters = clusters.dropna(subset=["cluster_id"])
    return matches, clusters


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75.0)
    ap.add_argument("--normalized", default=str(EVAL / "normalized.parquet"))
    ap.add_argument("--out", default=str(EVAL / "matches.parquet"))
    ap.add_argument("--labels", default=str(EVAL / "labels.csv"))
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--name-scorer", default="mean", choices=["token_set", "token_sort", "mean"])
    ap.add_argument(
        "--no-eia-rollup", action="store_true", help="disable the EIA plant-level rollup blocking view"
    )
    ap.add_argument(
        "--max-rows",
        type=int,
        default=400_000,
        help="cap on rows written to matches.parquet (keeps the file under 20 MB)",
    )
    args = ap.parse_args()

    globals()["NAME_SCORER"] = args.name_scorer
    matches, clusters = run(args.threshold, pathlib.Path(args.normalized), rollup=not args.no_eia_rollup)

    out = pathlib.Path(args.out)
    keep = [
        "left_id",
        "right_id",
        "pass",
        "score",
        "evidence",
        "accepted",
        "cluster_id",
        "name_score",
        "sponsor_score",
        "county_score",
        "capacity_score",
        "cod_score",
        "capacity_ratio",
        "cod_days",
        "rationale",
        "left_source",
        "right_source",
        "left_name",
        "right_name",
        "left_sponsor",
        "right_sponsor",
        "left_state",
        "right_state",
        "left_county",
        "right_county",
        "left_tech",
        "right_tech",
        "left_mw",
        "right_mw",
        "left_state_lc",
        "right_state_lc",
        "left_cod",
        "right_cod",
    ]
    written = matches[keep]
    sampled = False
    if len(written) > args.max_rows:
        written = pd.concat(
            [
                written[written["accepted"]],
                written[~written["accepted"]].sample(
                    n=max(0, args.max_rows - int(written["accepted"].sum())), random_state=0
                ),
            ]
        )
        sampled = True
    written.to_parquet(out, index=False)
    clusters.to_parquet(EVAL / "clusters.parquet", index=False)

    print(
        f"pairs scored: {len(matches):,}   written: {len(written):,}"
        f"{' (SAMPLED: rejected pairs down-sampled)' if sampled else ''}"
        f" -> {out} ({out.stat().st_size / 1e6:.2f} MB)"
    )
    print("\npairs by pass")
    print(matches["pass"].str.split(":").str[0].value_counts().to_string())
    print(f"\naccepted pairs at threshold {args.threshold}: {int(matches['accepted'].sum()):,}")
    print("\naccepted pairs by source pair")
    acc = matches[matches["accepted"]]
    sp = (acc["left_source"] + " x " + acc["right_source"]).value_counts()
    print(sp.to_string() if len(sp) else "  (none)")

    ncl = clusters["cluster_id"].nunique()
    sizes = clusters.groupby("cluster_id").size()
    print(
        f"\nclusters: {ncl:,} covering {len(clusters):,} records (max size {int(sizes.max()) if ncl else 0})"
    )
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
    print(f"  linked to >=1 EIA-860M planned unit: {n_link:,} ({100 * n_link / n_act:.1f}%)")
    per = (
        active.assign(linked=active["record_id"].isin(linked_ids))
        .groupby("source_id")["linked"]
        .agg(["sum", "count"])
    )
    per["rate_%"] = (100 * per["sum"] / per["count"]).round(1)
    print(per.to_string())
    raw_active = iso[iso["status_raw"].str.upper() == "ACTIVE"]
    nra = len(raw_active)
    nrl = raw_active["record_id"].isin(linked_ids).sum()
    print(f"raw-status ACTIVE rows: {nra:,}; linked: {nrl:,} ({100 * nrl / nra:.1f}%)")

    # --- cross-source duplicate counts
    print(
        "\ncross-source duplicate pairs (accepted, different sources): "
        f"{int((acc['left_source'] != acc['right_source']).sum()):,}"
    )
    iso_only = acc[
        (acc["left_source"] != acc["right_source"])
        & (acc["left_source"] != "eia860m")
        & (acc["right_source"] != "eia860m")
    ]
    print(f"  of which ISO-to-ISO (double-queued projects): {len(iso_only):,}")

    labels_path = pathlib.Path(args.labels)
    if labels_path.exists():
        labels = pd.read_csv(labels_path)
        ts = [50, 55, 60, 65, 70, 72, 75, 80, 85, 90] if args.sweep else [args.threshold]
        print(
            f"\nevaluation on {len(labels)} hand-labelled pairs "
            f"({int((labels['label'] == 1).sum())} positive / "
            f"{int((labels['label'] == 0).sum())} negative / "
            f"{int((labels['label'] == -1).sum())} uncertain, excluded)"
        )
        print(evaluate(written, labels, ts).to_string(index=False))
    else:
        print(f"\n(no labels at {labels_path}; skipping precision/recall)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
