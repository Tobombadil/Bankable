#!/usr/bin/env python3
"""Entity resolution over the canonical proposal table (docs/02 §5 key order, docs/20 §3.5).

Passes, in the order docs/02 §5 gives:
  D1  EIA plant id + generator id equal on both sides          (deterministic)
  D2  queue id + ISO equal on both sides                       (deterministic, within-source dupes)
  D3  cross-reference queue id embedded in the other side's project name, or the same
      facility-registry id (ICIS-Air, FRS) cited by both sides           (deterministic)
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

import numpy as np
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
#: `cross_refs` namespaces that are facility-registry identifiers (one id = one facility), so two
#: sources citing the same one are the same facility (D3). `icis_air`: EPA ICIS-Air PGM_SYS_ID;
#: `frs`: EPA Facility Registry Service id. Name-derived citations (`NYISO:…`) are not in here.
SHARED_ID_NAMESPACES = frozenset({"icis_air", "frs"})
#: EIA-860M's source id in the evaluation fixture ("eia860m") and in the registry, which is what the
#: scheduler and the dev store pass ("us.eia.860m"). Both are one plant/generator register.
EIA_SOURCE_IDS = frozenset({"eia860m", "us.eia.860m"})

# ------------------------------------------------------------------ fuzzy vetoes (docs/22 §22)
# A veto refuses a fuzzy pair whatever its score. Deterministic (D) pairs are never vetoed.

#: Technology -> the identity families it can belong to. `load` (data centres) and `transmission`
#: are not generation and only ever match their own kind. Two different generation families
#: (solar vs wind, wind vs gas) are not one project. Storage is compatible with every generation
#: family except thermal: hybrid plants file solar and storage as separate requests, while every
#: storage-vs-thermal candidate scoring >= 70 in the evaluation and dev frames (2 of 2: 'Montgomery
#: Energy Storage' vs biomass 'TBE-Montgomery LLC'; 'MERCED POWER' gas vs 'Merced BESS') is two
#: projects. `unknown`, `other` and missing technology are compatible with everything.
TECH_FAMILIES: dict[str, frozenset[str]] = {
    "load": frozenset({"load"}),
    "transmission": frozenset({"transmission"}),
    "storage": frozenset({"storage"}),
    "solar": frozenset({"solar"}),
    "solar_thermal": frozenset({"solar"}),
    "solar_storage": frozenset({"solar", "storage"}),
    "wind": frozenset({"wind"}),
    "wind_offshore": frozenset({"wind"}),
    "wind_storage": frozenset({"wind", "storage"}),
    "hydro": frozenset({"hydro"}),
    "pumped_storage": frozenset({"hydro"}),
    "nuclear": frozenset({"nuclear"}),
    "geothermal": frozenset({"geothermal"}),
    **{
        t: frozenset({"thermal"})
        for t in (
            "gas_cc",
            "gas_ct",
            "gas_steam",
            "gas_ice",
            "gas_other",
            "oil",
            "coal",
            "biomass",
            "waste",
            "hydrogen",
            "fuel_cell",
        )
    },
}
NON_GENERATION = frozenset({"load", "transmission"})
#: Without a sponsor component, name is the only identity evidence; county, capacity and COD are
#: shared by neighbouring projects. Below this name score such a pair is refused.
NAME_FLOOR_NO_SPONSOR = 70.0
#: A queue request more than this many times the whole EIA plant (all its generators) is neither
#: that plant nor a part of it. Largest legitimate ratio measured: 2.42 (CAISO 1632 "SANBORN
#: HYBRID 3", 1,400 MW, over the 578 MW EIA plant of the same name and number). Between two
#: non-EIA records the ratio is symmetric. A request much *smaller* than an EIA plant is not
#: vetoed: phases and storage add-ons of one plant are exactly that.
CAP_VETO_FACTOR = 4.0
#: Phase surplus: the phase-matching partners must account for this share of the anchor's
#: capacity before a differently numbered request is refused.
PHASE_COVER = 0.9


def tech_families(tech: object) -> frozenset[str] | None:
    """The identity families of a technology value; None when unknown (compatible with all)."""
    if tech is None or tech is pd.NA or (isinstance(tech, float) and pd.isna(tech)):
        return None
    return TECH_FAMILIES.get(str(tech))


def tech_compatible(a: object, b: object) -> bool:
    """False only when both technologies are known and cannot describe one project (docs/22 §22)."""
    fa, fb = tech_families(a), tech_families(b)
    if fa is None or fb is None:
        return True
    if (fa | fb) & NON_GENERATION:
        return fa == fb
    if fa & fb:
        return True
    both = fa | fb
    return "storage" in both and "thermal" not in both


def _num(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out == out and out > 0 else None


def is_eia(rec: dict) -> bool:
    plant = rec.get("eia_plant_id")
    return rec.get("source_id") in EIA_SOURCE_IDS and plant is not None and not pd.isna(plant)


def capacity_veto(left: dict, right: dict, factor: float = CAP_VETO_FACTOR) -> bool:
    """Rule C. An EIA record is compared through its whole plant (`plant_mw`), because a queue
    request may be one generator, the plant, or a complex holding the plant."""
    lcap, rcap = _num(left.get("capacity_mw")), _num(right.get("capacity_mw"))
    if lcap is None or rcap is None:
        return False
    lext = _num(left.get("plant_mw")) or lcap
    rext = _num(right.get("plant_mw")) or rcap
    if is_eia(left) and not is_eia(right):
        return rcap > factor * lext
    if is_eia(right) and not is_eia(left):
        return lcap > factor * rext
    return max(lcap, rcap) > factor * min(lcap, rcap)


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


def phase_key(name) -> frozenset[int]:
    """`phase_tokens`, with a lone phase 1 read as no number: 'Moonlight Flats Solar Power 1' and
    'Moonlight Flats Solar' are the same first phase."""
    tokens = phase_tokens(name)
    return frozenset() if tokens == {1} else frozenset(tokens)


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

    # Vetoes (docs/22 §22): refuse the pair whatever its score.
    vetoes = []
    if not tech_compatible(left.get("technology"), r.get("technology")):
        vetoes.append("veto_tech_class")  # Rule T: a data centre is not a solar plant
    if comp["sponsor"] is None and comp["name"] is not None and comp["name"] < NAME_FLOOR_NO_SPONSOR:
        vetoes.append("veto_name_floor")  # Rule N: geography + MW alone never make two named projects one
    if capacity_veto(left, r):
        vetoes.append("veto_capacity")  # Rule C: a request far larger than the whole plant
    flags.extend(vetoes)

    bits = [
        f"{k}={comp[k]:.0f}" for k in ("name", "sponsor", "county", "capacity", "cod") if comp[k] is not None
    ]
    rationale = f"{'; '.join(bits)}; evidence={evidence:.2f}" + (f"; {','.join(flags)}" if flags else "")
    return {
        "veto": ",".join(vetoes),
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


SCORE_COLUMNS = [
    "veto",
    "score",
    "name_score",
    "sponsor_score",
    "county_score",
    "capacity_score",
    "cod_score",
    "capacity_ratio",
    "cod_days",
    "evidence",
    "rationale",
]


def shared_ids(refs: object) -> dict[str, set[str]]:
    """`cross_refs` tokens in `SHARED_ID_NAMESPACES`, by namespace."""
    out: dict[str, set[str]] = {}
    if refs is None or (isinstance(refs, float) and pd.isna(refs)) or refs is pd.NA:
        return out
    for ref in str(refs).split("|"):
        ns, _, value = ref.strip().partition(":")
        if ns in SHARED_ID_NAMESPACES and value:
            out.setdefault(ns, set()).add(value)
    return out


def shared_id_conflict(left_refs: object, right_refs: object) -> bool:
    """True when both sides cite a facility-registry id in the same namespace and share none."""
    left, right = shared_ids(left_refs), shared_ids(right_refs)
    return any(ns in right and not (ids & right[ns]) for ns, ids in left.items())


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

    # D3 (shared registry id) - records of different sources that cite the same facility-registry
    # identifier (`SHARED_ID_NAMESPACES`), e.g. Virginia DEQ's `icis_air:<PLA_ICIS_ID>` and EPA
    # ICIS-Air's own `icis_air:<PGM_SYS_ID>`. Only an unambiguous group pairs: one record per
    # source; a source that repeats the id (two registrations of one campus) leaves it to review.
    by_ref: dict[str, list[int]] = {}
    for i, refs in df["cross_refs"].dropna().items():
        for ref in {r.strip() for r in str(refs).split("|") if r.strip()}:
            if ref.split(":", 1)[0] in SHARED_ID_NAMESPACES:
                by_ref.setdefault(ref, []).append(i)
    for ref, idx in by_ref.items():
        sources = df.loc[idx, "source_id"]
        if len(idx) < 2 or sources.nunique() < 2 or sources.duplicated().any():
            continue
        for a, b in itertools.combinations(sorted(idx), 2):
            out.append((a, b, "D3_xref", 100.0, f"both cite {ref}"))

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
            found.append((None, 0.0, 0.0, False, False))
        else:
            veto, conflict = row.get("veto", ""), row.get("id_conflict", False)
            vetoed = (isinstance(veto, str) and veto != "") or (
                isinstance(conflict, bool | np.bool_) and conflict
            )
            found.append(
                (
                    row,
                    float(row["score"]),
                    float(row["evidence"]),
                    bool(str(row["pass"]).startswith("D")),
                    vetoed,
                )
            )
    labels["score"] = [f[1] for f in found]
    labels["evidence"] = [f[2] for f in found]
    labels["det"] = [f[3] for f in found]
    labels["vetoed"] = [f[4] for f in found]
    labels["band"] = labels["score"].map(band)
    band_pop = pop["score"].map(band).value_counts()
    band_n = labels["band"].value_counts()
    labels["w"] = labels["band"].map(lambda b: band_pop.get(b, 0) / band_n.get(b, 1))
    missing = int((labels["evidence"] == 0).sum())

    rows = []
    for t in thresholds:
        # A vetoed fuzzy pair is never predicted, at any threshold (docs/22 §22). The phase-surplus
        # veto is computed at the run's own threshold, so off-threshold rows of a sweep carry it too.
        pred = labels["det"] | (
            (labels["score"] >= t) & (labels["evidence"] >= MIN_EVIDENCE) & ~labels["vetoed"]
        )
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
    # Both spellings of the EIA-860M source id: until 2026-09-29 only the fixture's short id was
    # matched, so the scheduler and the dev store (registry ids) never got rollup records.
    e = df[df["source_id"].isin(EIA_SOURCE_IDS) & df["eia_plant_id"].notna()]
    grp = e.groupby(["source_id", "eia_plant_id", "technology"], dropna=False)
    rows = []
    order = ["announced", "filed", "permitted", "contracted", "under_construction", "built"]
    for (src, pid, tech), g in grp:
        if len(g) < 2:
            continue
        first = g.iloc[0]
        states = [s for s in g["lifecycle_state"] if s in order]
        rows.append(
            {
                **first.to_dict(),
                "record_id": f"{src}:plant-{pid}:{tech}",
                "source_record_id": f"plant-{pid}",
                "capacity_mw": float(g["capacity_mw"].sum()),
                "eia_generator_id": None,
                "proposed_cod": g["proposed_cod"].max(),
                "lifecycle_state": max(states, key=order.index) if states else "unknown",
                "status_rule": "eia860m.plant_rollup",
            }
        )
    return pd.DataFrame(rows)


def plant_extents(df: pd.DataFrame) -> pd.DataFrame:
    """Per row: `plant_mw`, the whole EIA plant (every generator, every technology), and
    `plant_poi_mw`, the largest per-technology total, which is what a hybrid's single
    interconnection request is sized to (Bellefield 2: 500 MW solar + 500 MW storage behind one
    500 MW request). Non-EIA rows carry their own capacity in both. Rollup rows are not counted
    (they repeat their generators)."""
    out = pd.DataFrame(
        {
            "plant_mw": df["capacity_mw"].astype("float64"),
            "plant_poi_mw": df["capacity_mw"].astype("float64"),
        },
        index=df.index,
    )
    rollup = df["is_rollup"].astype(bool) if "is_rollup" in df.columns else pd.Series(False, index=df.index)
    eia = df["source_id"].isin(EIA_SOURCE_IDS) & df["eia_plant_id"].notna()
    real = df[eia & ~rollup]
    if real.empty:
        return out
    cap = real["capacity_mw"].astype("float64")
    total = cap.groupby([real["source_id"], real["eia_plant_id"]]).sum(min_count=1)
    per_tech = cap.groupby([real["source_id"], real["eia_plant_id"], real["technology"].astype(str)]).sum(
        min_count=1
    )
    poi = per_tech.groupby(level=[0, 1]).max()
    keys = list(zip(df.loc[eia, "source_id"], df.loc[eia, "eia_plant_id"], strict=True))
    out.loc[eia, "plant_mw"] = [total.get(k, float("nan")) for k in keys]
    out.loc[eia, "plant_poi_mw"] = [poi.get(k, float("nan")) for k in keys]
    return out


def phase_surplus(df: pd.DataFrame, matches: pd.DataFrame, eligible: pd.Series) -> pd.Series:
    """Rule Q, phase surplus (docs/22 §22). For an eligible fuzzy pair (p, q) whose phase numbers
    differ ('BONANZA SOLAR 2' vs 'Bonanza Solar and Storage Project'), refuse it when q already has
    eligible partners *from p's own source* that carry q's phase number and account for q on their
    own: their MW sum to at least `PHASE_COVER` of q's extent. When a capacity needed to tell is
    missing the rule abstains (the EIA plant 'Chokecherry and Sierra Madre Wind' holds both phases
    that the Permitting Dashboard lists without MW). Phases that add up to a plant are kept
    (Roseland Solar + Roseland Solar II = the 500 MW EIA plant; Vast Sands Power I + II = its two
    440 MW turbines), because the matching phase alone does not cover it. A phase is also kept when
    it *completes* the plant, its MW plus its partners' within +/-10 % (`CAP_TOL`) of q's extent:
    Sunrise Wind (880 MW) covers 95 % of the 924 MW EIA plant, and Sunrise Wind II (44 MW) is the
    rest (docs/22 §22.8, "Q-v2"). Agricola Wind 2 (97 + 79.3 against 99) is still refused.

    Coverage is counted within p's technology family when p has exactly one (a storage phase is
    measured against the plant's storage and against storage partners, so 'Indigo Storage 2' is
    not refused because 'Indigo Solar' covers the plant's solar). q's extent is then the EIA
    plant's capacity in that family; for a hybrid or unknown p it is the plant's `plant_poi_mw`;
    for a non-EIA q it is q's own capacity. Both orientations are checked. Returns a boolean
    Series aligned with `matches`."""
    fuzzy = ~matches["pass"].str.startswith("D")
    edges = matches[eligible | ~fuzzy]
    adj: dict[int, set[int]] = {}
    for a, b in zip(edges["li"].astype(int), edges["ri"].astype(int), strict=True):
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    names, src, tech, cap = df["name_canonical"], df["source_id"], df["technology"], df["capacity_mw"]
    phases: dict[int, frozenset[int]] = {}

    def phase(i: int) -> frozenset[int]:
        if i not in phases:
            phases[i] = phase_key(names.at[i])
        return phases[i]

    rollup = df["is_rollup"].astype(bool) if "is_rollup" in df.columns else pd.Series(False, index=df.index)
    eia = df["source_id"].isin(EIA_SOURCE_IDS) & df["eia_plant_id"].notna()
    plant_nodes: dict[tuple, list[int]] = {}
    for i in df.index[eia]:
        plant_nodes.setdefault((src.at[i], df.at[i, "eia_plant_id"]), []).append(int(i))
    poi = df["plant_poi_mw"] if "plant_poi_mw" in df.columns else cap

    def real_rows(i: int) -> list[int]:
        if not rollup.at[i]:
            return [i]
        key = (src.at[i], df.at[i, "eia_plant_id"])
        return [j for j in plant_nodes.get(key, []) if not rollup.at[j] and tech.at[j] == tech.at[i]]

    def single_family(i: int) -> str | None:
        fam = tech_families(tech.at[i])
        return next(iter(fam)) if fam is not None and len(fam) == 1 else None

    def in_family(i: int, family: str) -> bool:
        fam = tech_families(tech.at[i])
        return fam is None or family in fam

    def extent(q: int, family: str | None) -> float | None:
        if not eia.at[q]:
            return _num(cap.at[q])
        if family is not None:
            gens = [
                j
                for j in plant_nodes[(src.at[q], df.at[q, "eia_plant_id"])]
                if not rollup.at[j] and family in (tech_families(tech.at[j]) or frozenset())
            ]
            total = sum(c for c in (_num(cap.at[j]) for j in gens) if c is not None)
            if total > 0:
                return total
        return _num(poi.at[q])

    def surplus(p: int, q: int) -> bool:
        anchors = plant_nodes[(src.at[q], df.at[q, "eia_plant_id"])] if eia.at[q] else [q]
        family = single_family(p)
        partners: set[int] = set()
        for n in anchors:
            for z in adj.get(n, ()):
                if z != p and src.at[z] == src.at[p] and phase(z) == phase(q):
                    partners.update(r for r in real_rows(z) if family is None or in_family(r, family))
        partners.discard(p)
        if not partners:
            return False
        size = extent(q, family)
        caps = [_num(cap.at[z]) for z in partners]
        if size is None or any(c is None for c in caps):
            return False  # cannot show the phase is surplus without every capacity: abstain
        covered = sum(c for c in caps if c is not None)
        mine = _num(cap.at[p])
        if mine is not None and abs(covered + mine - size) <= CAP_TOL * size:
            return False  # p completes the plant: Sunrise Wind 880 + Sunrise Wind II 44 = 924 MW
        return covered >= PHASE_COVER * size

    out = pd.Series(False, index=matches.index)
    cand = matches[eligible & fuzzy]
    for k, a, b in zip(cand.index, cand["li"].astype(int), cand["ri"].astype(int), strict=True):
        if phase(a) != phase(b) and (surplus(a, b) or surplus(b, a)):
            out.at[k] = True
    return out


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
    df[["plant_mw", "plant_poi_mw"]] = plant_extents(df)

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
    # Named columns so a frame with no candidate pairs still has them (it raised KeyError before).
    fuzzy = pd.concat([cand.reset_index(drop=True), pd.DataFrame(scored, columns=SCORE_COLUMNS)], axis=1)
    fuzzy["pass"] = "F_fuzzy:" + fuzzy["block"]
    fuzzy["rationale"] = fuzzy["block"] + "; " + fuzzy["rationale"]

    det = det.assign(block="deterministic", evidence=1.0)
    matches = pd.concat([det, fuzzy.drop(columns=["block"])], ignore_index=True, sort=False)
    matches = matches.sort_values("score", ascending=False).drop_duplicates(subset=["li", "ri"])

    # A fuzzy pair whose two sides cite *different* ids in the same facility-registry namespace
    # is two facilities (one operator's neighbouring campuses score 75-82 on name + county alone:
    # "Microsoft Corp - LVL Data Center" vs "Microsoft Corp - AVC17 Datacenter"), so it is never
    # accepted, whatever its score.
    refs = df["cross_refs"]
    matches["id_conflict"] = [
        shared_id_conflict(refs.at[int(a)], refs.at[int(b)])
        for a, b in zip(matches["li"], matches["ri"], strict=True)
    ]
    matches["veto"] = matches["veto"].fillna("") if "veto" in matches.columns else ""
    eligible = (
        ~matches["pass"].str.startswith("D")
        & (matches["score"] >= threshold)
        & (matches["evidence"] >= MIN_EVIDENCE)
        & ~matches["id_conflict"]
        & (matches["veto"] == "")
    )
    surplus = phase_surplus(df, matches, eligible)
    matches.loc[surplus, "veto"] = "veto_phase_surplus"
    matches.loc[surplus, "rationale"] = matches.loc[surplus, "rationale"] + "; veto_phase_surplus"
    matches["accepted"] = matches["pass"].str.startswith("D") | (eligible & ~surplus)

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
        "veto",
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
