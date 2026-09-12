#!/usr/bin/env python3
"""Docket linkage: FERC eLibrary filings -> ISO interconnection-queue records (docs/22 §10).

`docs/22`'s recalibration proposal (owner decision D5, `docs/00-PLAN.md`) adds "FERC docket
linkage" as a resolution key alongside the measured EIA-860M link rate, but flags it as
**untested**. This script is the first measurement.

Reads the latest normalised snapshot of `us.ferc.elibrary` and of every `us.iso.*.gen_queue`
source under `data/normalized/` (ERCOT, CAISO, NYISO today; SPP/ISO-NE/PJM/MISO join once their
connectors exist, with no change to this script). Proposes a link between a filing and a queue
record by two independent methods, matching the task's brief exactly:

(a) **Explicit match**: the queue record's `queue_id` or normalised project name (`name_norm`)
    appears verbatim inside the filing's description (`document.title`) or its regex-lifted
    `project_name_hint`. Guarded against noise: a bare `queue_id` must be at least 5 characters
    *and* contain a letter (CAISO's queue ids are small bare integers like "22"/"32" — a 2-digit
    number is a false-positive machine against dollar figures, docket numbers and dates in filing
    text, so CAISO is only matched on name, never on queue_id) and a name must be at least 6
    characters. Score 100, method `name_or_queue_id`.
(b) **Sponsor fuzzy match**: `rapidfuzz.fuzz.token_set_ratio` between the queue record's sponsor
    and the filing's `filer`, computed for every (record, filing) pair with
    `rapidfuzz.process.cdist` (vectorised — the full cross product is a few hundred thousand
    pairs, cheap for cdist, prohibitively slow as a Python double loop). Both strings first have a
    curated list of generic organisation words stripped (`GENERIC_ORG_WORDS`: "energy", "power",
    "solar", "generation", "llc", "authority", ...) and a comparison is skipped unless **both**
    sides still have at least two distinctive words left. This is not optional polish: measured
    while building this script, scoring the raw or `sponsor_norm` names let boilerplate-only
    overlap through at 85-100 — "NRG Energy, Inc." vs "Puget Sound Energy, Inc." (86, sharing only
    "Energy, Inc."), "NY Power Authority" vs "Platte River Power Authority" (91, sharing only
    "Power Authority"), "Great Plains Solar, LLC" vs "Gypsum Plains Solar, LLC" (85, sharing only
    "Plains Solar, LLC") — every one a different company. Stripping the generic words first and
    requiring two distinctive words left removes all of those (each collapses to zero or one
    surviving word) while keeping true matches like "Central Hudson Gas & Electric" against
    "Central Hudson Gas & Electric Corporation" (both keep "CENTRAL HUDSON"). Kept when the score
    on the stripped strings is at or above `--threshold` (default 85). Restricted to filings
    carrying an `ER` docket (electric rate/interconnection filings) since every implemented ISO
    queue is electric generation; a `CP` (gas) filing cannot be the interconnection agreement for
    a generator queue entry.

A (record, filing) pair kept by either method is written once, tagged with whichever method(s)
matched (a pair can satisfy both).

Writes `data/eval/docket_links.parquet`: one row per accepted link with `record_id` (ISO queue),
`ferc_record_id`, `method`, `score`, `rationale`, and enough of both sides' identifying fields
(`queue_id`, `name_canonical`, `sponsor_name`, `iso`, `lifecycle_state`, `ferc_accession_number`,
`ferc_docket_refs`, `ferc_title`) to audit a link without re-joining the source parquets.

Reports, to stdout, the measurement `docs/22` §10 needs: how many *active* (non-terminal
lifecycle, `pipeline.resolve.ACTIVE_STATES`) ISO queue records gained at least one docket link,
both as a count and as a percentage of all active records — measured against whatever is on disk
under `data/normalized/` at run time, never estimated.

Usage:
    python pipeline/link_dockets.py [--threshold 85] [--out data/eval/docket_links.parquet]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

ROOT = pathlib.Path(__file__).resolve().parents[1]
NORMALIZED = ROOT / "data" / "normalized"
EVAL = ROOT / "data" / "eval"

FERC_SOURCE_ID = "us.ferc.elibrary"
ISO_QUEUE_GLOB = "us.iso.*.gen_queue"
# pipeline/resolve.py's definition of "still a live proposal" (docs/02 §1 lifecycle minus
# terminal states) — reused verbatim so the two measurements are comparable.
ACTIVE_STATES = {"announced", "filed", "studied", "permitted", "contracted", "under_construction"}

MIN_NAME_LEN = 6
MIN_QUEUE_ID_LEN = 5
MIN_SPONSOR_WORDS = 2
_HAS_LETTER = re.compile(r"[A-Z]")

# Inlined rather than imported from pipeline.normalize: this script is a standalone CLI
# (`python pipeline/link_dockets.py`), same convention as resolve.py/diff.py, neither of which
# cross-imports another pipeline module either.
US_STATE_NAMES = (
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT",
    "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS",
    "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA",
    "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY",
    "NEW MEXICO", "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON",
    "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS",
    "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING",
)  # fmt: skip

# Stripped from both sides before sponsor-vs-filer fuzzy scoring (see module docstring, method b).
# "INTERCONNECTION"/"HOLDINGS"/"RESOURCES" earned their place the same way as the rest: measured
# false positives while building this script. FERC interconnection-agreement counterparties are
# routinely named "<Sponsor> Interconnection Holdings, LLC" or "<ISO/RTO> Interconnection, LLC" —
# without stripping "INTERCONNECTION" a completely unrelated pair like "NextEra Energy
# Interconnection Holdings, LLC" and "PJM Interconnection, L.L.C." scored 88 on that one shared,
# functionally meaningless word. US state names are stripped too, for the same reason: "New York
# Power Authority" vs "New York Independent System Operator, Inc." scored 100 on a shared state
# name alone ("NEW YORK" is a subset of every token in the filer's residual) — two organisations
# that both operate in New York are not thereby the same organisation.
GENERIC_ORG_WORDS = re.compile(
    r"\b(LLC|L\.L\.C\.?|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LP|L\.P\.?|LLP|LTD|LIMITED|"
    r"HOLDINGS?|ENERGY|ENERGIES|POWER|RENEWABLES?|SOLAR|WIND|STORAGE|GENERATION|GENERATING|"
    r"DEVELOPMENT|DEVELOPMENTS?|PARTNERS?|PARTNERSHIP|GROUP|SERVICES|TRANSMISSION|ELECTRIC|GAS|"
    r"AUTHORITY|CENTER|CENTRE|UTILITIES?|SYSTEMS?|RESOURCES?|INTERCONNECTION|USA|US|AMERICA|"
    r"AMERICAN|NORTH|SOUTH|" + "|".join(sorted(US_STATE_NAMES, key=len, reverse=True)) + r")\b"
)


def _distinctive(name: str) -> str:
    """Upper-cased name with GENERIC_ORG_WORDS and punctuation stripped, collapsed whitespace."""
    s = GENERIC_ORG_WORDS.sub(" ", name.upper())
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


LINK_COLUMNS = [
    "record_id",
    "ferc_record_id",
    "method",
    "score",
    "rationale",
    "iso",
    "queue_id",
    "name_canonical",
    "sponsor_name",
    "lifecycle_state",
    "ferc_accession_number",
    "ferc_docket_refs",
    "ferc_title",
]


def _latest_parquet(directory: pathlib.Path) -> pd.DataFrame | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob("*.parquet"))
    if not files:
        return None
    return pd.read_parquet(files[-1])


def load_ferc_documents() -> pd.DataFrame:
    df = _latest_parquet(NORMALIZED / FERC_SOURCE_ID)
    return df if df is not None else pd.DataFrame(columns=["record_id"])


def load_iso_queue_records() -> pd.DataFrame:
    """Every `us.iso.*.gen_queue` source's latest snapshot, concatenated. Picks up new ISO
    connectors automatically (SPP/ISO-NE/PJM/MISO) with no change to this script — they are simply
    absent from `data/normalized/` until licensed (`CLAUDE.md`, `docs/13`)."""
    frames = [df for d in sorted(NORMALIZED.glob(ISO_QUEUE_GLOB)) if (df := _latest_parquet(d)) is not None]
    if not frames:
        return pd.DataFrame(columns=["record_id"])
    return pd.concat(frames, ignore_index=True)


_PAREN_RE = re.compile(r"\([^)]*\)")


def _explicit_matches(iso: pd.DataFrame, docs: pd.DataFrame) -> list[dict[str, Any]]:
    """Method (a): queue_id or project name quoted verbatim in the filing text.

    The name check uses `name_canonical` (the raw project name), not `name_norm`: `name_norm`
    strips generic noise words ("solar", "project", "energy", ...) precisely so `resolve.py` can
    block on the distinctive remainder, but that remainder is frequently a single common word or
    place name on its own ("KANSAS", "GATEWAY", "PACIFIC") — safe as a *blocking key* combined
    with capacity/county evidence, a false-positive machine as a bare substring test against
    freeform filing text. Requiring at least two words guards against exactly that (measured while
    building this script: single-word `name_norm` matches like "KANSAS" or "GATEWAY" hit unrelated
    filings by geographic coincidence; requiring the full multi-word name removed every one)."""
    doc_text = (docs["title"].fillna("") + " " + docs["project_name_hint"].fillna("")).str.upper()
    out: list[dict[str, Any]] = []
    for _i, rec in iso.iterrows():
        qid_raw, name_raw = rec.get("queue_id"), rec.get("name_canonical")
        qid = "" if pd.isna(qid_raw) else str(qid_raw).strip().upper()
        name = "" if pd.isna(name_raw) else _PAREN_RE.sub(" ", str(name_raw)).strip().upper()
        candidates = []
        if qid and len(qid) >= MIN_QUEUE_ID_LEN and _HAS_LETTER.search(qid):
            candidates.append(("queue_id", qid))
        if name and len(name) >= MIN_NAME_LEN and len(name.split()) >= 2:
            candidates.append(("name", name))
        if not candidates:
            continue
        for kind, needle in candidates:
            hits = doc_text[doc_text.str.contains(re.escape(needle), regex=True, na=False)]
            for j in hits.index:
                out.append(
                    {
                        "record_id": rec["record_id"],
                        "ferc_record_id": docs.at[j, "record_id"],
                        "method": "name_or_queue_id",
                        "score": 100.0,
                        "rationale": f"{kind} {needle!r} found in filing text",
                    }
                )
    return out


def _sponsor_matches(iso: pd.DataFrame, docs: pd.DataFrame, threshold: float) -> list[dict[str, Any]]:
    """Method (b): rapidfuzz sponsor-vs-filer match on the distinctive residual of each name
    (generic organisation words stripped, see module docstring and `GENERIC_ORG_WORDS`),
    restricted to ER (electric) filings — every implemented ISO queue is electric generation."""
    er_docs = docs[docs["docket_refs"].fillna("").str.contains(r"\bER\d", regex=True)]
    if er_docs.empty:
        return []
    sponsors_raw = iso["sponsor_name"].fillna("").astype(str).tolist()
    filers_raw = er_docs["filer"].fillna("").astype(str).tolist()
    sponsors = [_distinctive(s) for s in sponsors_raw]
    filers = [_distinctive(f) for f in filers_raw]
    keep_row = [len(s.split()) >= MIN_SPONSOR_WORDS for s in sponsors]
    keep_col = [len(f.split()) >= MIN_SPONSOR_WORDS for f in filers]
    if not any(keep_row) or not any(keep_col):
        return []
    matrix = process.cdist(sponsors, filers, scorer=fuzz.token_set_ratio)
    out: list[dict[str, Any]] = []
    for ri, row_ok in enumerate(keep_row):
        if not row_ok:
            continue
        for ci, col_ok in enumerate(keep_col):
            if not col_ok or matrix[ri, ci] < threshold:
                continue
            out.append(
                {
                    "record_id": iso.iloc[ri]["record_id"],
                    "ferc_record_id": er_docs.iloc[ci]["record_id"],
                    "method": "sponsor_fuzzy",
                    "score": float(matrix[ri, ci]),
                    "rationale": (
                        f"sponsor {sponsors_raw[ri]!r} vs filer {filers_raw[ci]!r}"
                        f" (distinctive {sponsors[ri]!r} vs {filers[ci]!r}) = {matrix[ri, ci]:.0f}"
                    ),
                }
            )
    return out


def run(docs: pd.DataFrame, iso: pd.DataFrame, threshold: float = 85.0) -> pd.DataFrame:
    if docs.empty or iso.empty:
        print(
            f"  no data to link: {len(docs):,} FERC documents, {len(iso):,} ISO queue records",
            file=sys.stderr,
        )
        return pd.DataFrame(columns=LINK_COLUMNS)

    pairs = _explicit_matches(iso, docs) + _sponsor_matches(iso, docs, threshold)
    if not pairs:
        return pd.DataFrame(columns=LINK_COLUMNS)

    links = pd.DataFrame(pairs)
    # a pair found by both methods keeps its higher-scoring (explicit, 100) row and folds the
    # method label together so the audit trail shows both kinds of evidence agreed.
    links = links.sort_values("score", ascending=False)
    method_by_pair = links.groupby(["record_id", "ferc_record_id"])["method"].apply(
        lambda s: "+".join(sorted(set(s)))
    )
    links = links.drop_duplicates(subset=["record_id", "ferc_record_id"], keep="first").set_index(
        ["record_id", "ferc_record_id"]
    )
    links["method"] = method_by_pair
    links = links.reset_index()

    iso_cols = iso.set_index("record_id")[
        ["iso", "queue_id", "name_canonical", "sponsor_name", "lifecycle_state"]
    ]
    docs_cols = docs.set_index("record_id")[["accession_number", "docket_refs", "title"]].rename(
        columns={
            "accession_number": "ferc_accession_number",
            "docket_refs": "ferc_docket_refs",
            "title": "ferc_title",
        }
    )
    links = links.join(iso_cols, on="record_id").join(docs_cols, on="ferc_record_id")
    return links[LINK_COLUMNS]


def active_link_stats(iso: pd.DataFrame, links: pd.DataFrame) -> dict[str, Any]:
    """The docs/22 §10 measurement: active (non-terminal lifecycle) ISO queue records that
    gained at least one docket link, overall and per source. Pure function so it is unit-testable
    without touching disk."""
    if iso.empty:
        return {"n_active": 0, "n_linked": 0, "rate_pct": 0.0, "per_source": pd.DataFrame()}
    active = iso[iso["lifecycle_state"].isin(ACTIVE_STATES)]
    linked_ids = set(links["record_id"]) if len(links) else set()
    linked_active_ids = linked_ids & set(active["record_id"])
    n_active, n_linked = len(active), len(linked_active_ids)
    per = (
        active.assign(linked=active["record_id"].isin(linked_active_ids))
        .groupby("source_id")["linked"]
        .agg(["sum", "count"])
    )
    per["rate_%"] = (100 * per["sum"] / per["count"]).round(2)
    return {
        "n_active": n_active,
        "n_linked": n_linked,
        "rate_pct": (100 * n_linked / n_active) if n_active else 0.0,
        "per_source": per,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=85.0)
    ap.add_argument("--out", default=str(EVAL / "docket_links.parquet"))
    args = ap.parse_args()

    docs = load_ferc_documents()
    iso = load_iso_queue_records()
    links = run(docs, iso, args.threshold)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    links.to_parquet(out, index=False)
    print(f"FERC documents: {len(docs):,}   ISO queue records: {len(iso):,}")
    print(f"links written: {len(links):,} -> {out}")
    if len(links):
        print("\nlinks by method")
        print(links["method"].value_counts().to_string())
        print("\nlinks by ISO")
        print(links["iso"].value_counts().to_string())

    if not iso.empty:
        stats = active_link_stats(iso, links)
        print(f"\nACTIVE (non-terminal lifecycle) ISO queue records: {stats['n_active']:,}")
        print(f"  gained a docket link: {stats['n_linked']:,} ({stats['rate_pct']:.2f}%)")
        print(stats["per_source"].to_string())
    else:
        print("\nno ISO queue records on disk; nothing to measure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
