"""EPA GHGRP facility register -> the emitter context parquet, plus the facility-to-asset matcher
(docs/02 §12; docs/24 constraint: no third unresolved source of the same real-world assets).

Same split as `us.eia.860` / `pipeline/context/eia_owners.py`: the connector
(`pipeline/connectors/us_epa_ghgrp/connector.py`) owns fetch/parse and the shared parsers; this
module is the producer of the consumer frames and the *pure* matching logic. Nothing here opens a
database — `services/ingest/ghgrp.py` reads the assets out of a session, calls `match_facilities`,
and writes `asset_owner` edges and `asset.attributes` for the accepted matches.

Outputs (`python -m pipeline.context.ghgrp`):
- `data/normalized/context/us.epa.ghgrp.parquet` — one row per facility-year, every GHGRP facility
  of the newest reporting year, matched or not (`FACILITY_COLUMNS` in the connector). This is the
  only place an unmatched facility lives; it is never written to `asset`.

Matching (`match_facilities`). GHGRP facilities are emitters that already exist in `asset` under
EIA-860M, the EIA Atlas layers, the capacity report and EPA LMOP/AgSTAR. Two paths, in order:

1. **`oris_crosswalk`** — EPA's own `GHGRP ↔ ORIS` power-plant crosswalk
   (`ghgrp_oris_power_plant_crosswalk_12_13_21.xlsx`, 2,150 rows, 2,131 GHGRP facilities, up to
   five ORIS codes each, 29 with more than one; vendored trimmed at
   `data/vendored/ghgrp/oris_crosswalk.csv`). An ORIS code *is* the EIA plant code that
   `asset.source_asset_id` carries for `power_plant` rows, so this is a deterministic key stated by
   the publisher, confidence 1.0. It is dated December 2021 and so misses plants that began
   reporting after RY2020; those fall to path 2.
2. **`geo_name`** — blocked on `state_code` and a haversine distance of at most
   `MAX_DISTANCE_KM` (1 km), scored on distance, name-token overlap and NAICS/asset-type
   compatibility (`score_pair`), assigned globally greedy (best pair first, each facility and each
   asset consumed once — docs/24 §2.3's lesson), accepted at `score >= threshold`.

The threshold is chosen against path 1 as a gold set: for the crosswalked facilities, path 2 is
run blind and its accepted pairs are compared with EPA's crosswalk (`evaluate_against_crosswalk`);
the non-power types are hand-checked on a stratified sample (`data/eval/ghgrp_match_labels.csv`).
The numbers and the chosen `DEFAULT_THRESHOLD` are in docs/02 §12.

Summary-zip enrichment (`parse_summary_zip`, `--summary-zip`). The subpart-level quantities are
not in `pub_dim_facility` and Envirofacts has no `rr_*`/`uu_*` tables (they 404 — source-map lane,
2026-09-25); they sit on two sheets of EPA's RY2023 Data Summary Spreadsheets zip
(`2023_data_summary_spreadsheets.zip`, 28,389,973 bytes, sha256 `895349c8…3fa8f`, member
`ghgp_data_2023.xlsx`, header on the fourth row): "Geologic Sequestration of CO2" (Subpart RR,
20 facilities, `Total Mass of CO2 Sequestered`, metric tons) and "CO2 Injection" (Subpart UU, 81
facilities, `Quantity of CO2 Received for Injection`). Joined on `Facility Id` into
`rr_co2_sequestered_t` / `uu_co2_received_t`, with a `*_confidential` flag where EPA prints the
literal `confidential` instead of a number (80 of the 81 UU rows in RY2023, so the UU quantity is
almost never usable; the RR mass is numeric on 19 of 20, one blank); never used to decide an asset
type (RR reporters are 8 dedicated sequestration sites and 12 EOR fields under MRV plans).

Owner edges (`build_owner_rows`): one row per (accepted match, parent) with `share_pct` exactly as
the register states it, `as_of` = 31 December of the reporting year (GHGRP is an annual register;
the parent list is reported for the year, no finer date exists), and the facility's `share_flag`
so a consumer can see when the stated shares do not sum to 100. Nothing is rescaled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import pathlib
import re
import time
import zipfile
from typing import Any

import pandas as pd

from pipeline.connectors.base import RawSnapshot, to_parquet_safe
from pipeline.connectors.registry import Registry
from pipeline.connectors.us_epa_ghgrp.connector import Connector, build_facilities
from pipeline.context import fuels

SOURCE_ID = "us.epa.ghgrp"
ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"
DEFAULT_CROSSWALK = ROOT / "data" / "vendored" / "ghgrp" / "oris_crosswalk.csv"
CROSSWALK_URL = (
    "https://www.epa.gov/system/files/documents/2022-04/ghgrp_oris_power_plant_crosswalk_12_13_21.xlsx"
)

MAX_DISTANCE_KM = 1.0
#: Accept a `geo_name` pair at this score or above (docs/02 §12 says how it was chosen).
DEFAULT_THRESHOLD = 0.55
METHODS = ("oris_crosswalk", "geo_name")

#: NAICS prefixes compatible with each asset type — a feature, not a filter (a paper mill's
#: cogeneration unit is a `power_plant` with NAICS 322). Keys are prefixes matched with startswith.
NAICS_COMPATIBLE: dict[str, tuple[str, ...]] = {
    "power_plant": ("2211", "221330"),
    "gas_processing_plant": ("211", "213112"),
    "gas_storage": ("486210", "221210", "211"),
    "lng_terminal": ("486210", "221210", "424710", "211"),
    "ethanol_plant": ("325193", "311221", "325199"),
    "rng_project": ("562212", "562219", "221210", "112"),
    "refinery": ("324110",),
    "compressor_station": ("486210",),
}

#: `asset.technology` values that cannot be a GHGRP emitter's own generating unit: a GHGRP
#: facility is a combustion or process source, so a co-located solar farm, battery or wind
#: registration under the same site name is a different asset (hand-check, docs/02 §12: a retired
#: gas station's name on the BESS that replaced it, a chemical plant's name on its solar array).
#: Applied on the `geo_name` path only; the crosswalk is EPA's own statement.
NON_EMITTING_TECHNOLOGIES = frozenset(
    {
        "solar",
        "wind",
        "wind_offshore",
        "hydro",
        "pumped_storage",
        "storage",
        "solar_storage",
        "nuclear",
        "geothermal",
    }
)

#: Tokens that carry no identity between an emitter's name and a registry name.
_GENERIC = frozenset(
    {
        "THE",
        "OF",
        "AND",
        "AT",
        "IN",
        "DE",
        "LA",
        "LLC",
        "INC",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LP",
        "LTD",
        "LLP",
        "PLC",
        "INCORPORATED",
        "PLANT",
        "STATION",
        "FACILITY",
        "FACILITIES",
        "PROJECT",
        "SITE",
        "UNIT",
        "UNITS",
        "CENTER",
        "CENTRE",
        "GENERATING",
        "GENERATION",
        "GENERATOR",
        "GENERATORS",
        "ENERGY",
        "POWER",
        "ELECTRIC",
        "ELECTRICITY",
        "GAS",
        "NATURAL",
        "PLANTS",
        "OPERATIONS",
        "SYSTEM",
        "SYSTEMS",
        "NO",
        "NUMBER",
        "CITY",
        "COUNTY",
    }
)
_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6", "VII": "7", "VIII": "8"}
_TOKEN_RE = re.compile(r"[A-Z0-9]+")


def name_tokens(name: Any) -> frozenset[str]:
    """Identity-bearing tokens of a facility or asset name (upper-cased, punctuation split,
    legal forms and industry generics dropped, roman numerals folded to digits)."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return frozenset()
    text = str(name).upper().replace("&", " AND ")
    toks = [_ROMAN.get(t, t) for t in _TOKEN_RE.findall(text)]
    return frozenset(t for t in toks if (t not in _GENERIC and len(t) > 1) or t.isdigit())


def name_score(a: Any, b: Any) -> float:
    """max(Jaccard, containment) of the two names' tokens; 0.0 when either has no tokens."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    containment = inter / min(len(ta), len(tb))
    return round(max(jaccard, containment), 4)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def naics_compatible(naics: Any, asset_type: str) -> bool:
    code = "" if naics is None or (isinstance(naics, float) and pd.isna(naics)) else str(naics).strip()
    return any(code.startswith(p) for p in NAICS_COMPATIBLE.get(asset_type, ()))


def distance_score(km: float) -> float:
    if km <= 0.25:
        return 1.0
    if km <= 0.5:
        return 0.7
    if km <= MAX_DISTANCE_KM:
        return 0.4
    return 0.0


def score_pair(distance_km: float, name: float, compatible: bool) -> float:
    """0.45 distance + 0.35 name + 0.20 NAICS compatibility; a pair beyond `MAX_DISTANCE_KM` is 0."""
    d = distance_score(distance_km)
    if d == 0.0:
        return 0.0
    return round(0.45 * d + 0.35 * name + 0.20 * (1.0 if compatible else 0.0), 4)


MATCH_COLUMNS: list[str] = [
    "ghgrp_facility_id",
    "asset_id",
    "asset_type",
    "asset_source_id",
    "source_asset_id",
    "asset_name",
    "method",
    "score",
    "distance_km",
    "name_score",
    "naics_compatible",
    "accepted",
]


def load_crosswalk(path: pathlib.Path = DEFAULT_CROSSWALK) -> pd.DataFrame:
    """`ghgrp_facility_id, oris_code` pairs (one row per pair) from the vendored crosswalk CSV."""
    df = pd.read_csv(path, dtype=str, comment="#").fillna("")
    df["ghgrp_facility_id"] = df["ghgrp_facility_id"].str.strip()
    df["oris_code"] = df["oris_code"].str.strip()
    return df[(df["ghgrp_facility_id"] != "") & (df["oris_code"] != "")].drop_duplicates()


def crosswalk_from_xlsx(content: bytes) -> pd.DataFrame:
    """The vendored CSV's rows from EPA's workbook (sheet "ORIS Crosswalk", `ORIS CODE` … `ORIS CODE 5`)."""
    cw = pd.read_excel(io.BytesIO(content), sheet_name="ORIS Crosswalk", dtype=str)
    cw.columns = [str(c).strip() for c in cw.columns]
    oris_cols = [c for c in cw.columns if c.upper().startswith("ORIS CODE")]
    long = cw.melt(id_vars=["GHGRP Facility ID"], value_vars=oris_cols, value_name="oris_code")
    long = long.dropna(subset=["oris_code"])
    long["oris_code"] = long["oris_code"].str.replace(r"\.0$", "", regex=True).str.strip()
    long = long.rename(columns={"GHGRP Facility ID": "ghgrp_facility_id"})
    long["ghgrp_facility_id"] = long["ghgrp_facility_id"].str.replace(r"\.0$", "", regex=True).str.strip()
    out = (
        long[["ghgrp_facility_id", "oris_code"]]
        .drop_duplicates()
        .sort_values(["ghgrp_facility_id", "oris_code"])
    )
    return out.reset_index(drop=True)


def _pt(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_facilities(
    facilities: pd.DataFrame,
    assets: pd.DataFrame,
    *,
    crosswalk: pd.DataFrame | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    max_distance_km: float = MAX_DISTANCE_KM,
    use_crosswalk: bool = True,
) -> pd.DataFrame:
    """Best asset per facility (`MATCH_COLUMNS`), or no row when nothing is within reach.

    `facilities` needs `ghgrp_facility_id, name, state_code, lon, lat, naics_code`; `assets`
    needs `asset_id, asset_type, source_id, source_asset_id, name, state_code, lon, lat` and
    optionally `technology` (`NON_EMITTING_TECHNOLOGIES` are then never path-2 candidates).
    Path 1 (crosswalk) pairs are `accepted`; path 2 pairs are `accepted` at `score >= threshold`.
    An asset is consumed by at most one facility on path 2; a crosswalk facility may map to several
    EIA plants (29 do), and those assets are then not offered to path 2.
    """
    fac = facilities.reset_index(drop=True)
    ast = assets.reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    used_assets: set[str] = set()
    matched_facilities: set[str] = set()

    # ---- path 1: EPA's own crosswalk, keyed on the EIA plant code
    if use_crosswalk and crosswalk is not None and len(crosswalk):
        plants = ast[ast["asset_type"] == "power_plant"]
        by_code: dict[str, list[int]] = {}
        for i, code in plants["source_asset_id"].astype(str).str.strip().items():
            by_code.setdefault(code, []).append(int(i))
        fac_idx = {str(f): int(i) for i, f in fac["ghgrp_facility_id"].astype(str).items()}
        for fid, oris in crosswalk[["ghgrp_facility_id", "oris_code"]].itertuples(index=False):
            i = fac_idx.get(str(fid))
            if i is None:
                continue
            for j in by_code.get(str(oris), []):
                f, a = fac.iloc[i], ast.iloc[j]
                dist = _distance(f, a)
                rows.append(
                    {
                        "ghgrp_facility_id": str(f["ghgrp_facility_id"]),
                        "asset_id": str(a["asset_id"]),
                        "asset_type": a["asset_type"],
                        "asset_source_id": a["source_id"],
                        "source_asset_id": str(a["source_asset_id"]),
                        "asset_name": a["name"],
                        "method": "oris_crosswalk",
                        "score": 1.0,
                        "distance_km": dist,
                        "name_score": name_score(f["name"], a["name"]),
                        "naics_compatible": naics_compatible(f.get("naics_code"), a["asset_type"]),
                        "accepted": True,
                    }
                )
                used_assets.add(str(a["asset_id"]))
                matched_facilities.add(str(f["ghgrp_facility_id"]))

    # ---- path 2: state + distance blocking, scored, globally greedy
    cells: dict[tuple[str, int, int], list[int]] = {}
    # 0.02° cells: ±1 cell always covers `MAX_DISTANCE_KM` (0.01° of longitude is only 0.8 km at
    # 42° N, so a 1 km pair could sit two cells apart — caught by the synthetic test).
    cell = 0.02
    has_tech = "technology" in ast.columns
    for j, a in ast.iterrows():
        lon, lat = _pt(a["lon"]), _pt(a["lat"])
        if lon is None or lat is None or str(a["asset_id"]) in used_assets:
            continue
        if (
            has_tech
            and a["asset_type"] == "power_plant"
            and str(a["technology"]) in NON_EMITTING_TECHNOLOGIES
        ):
            continue
        key = (str(a["state_code"]), math.floor(lat / cell), math.floor(lon / cell))
        cells.setdefault(key, []).append(int(str(j)))

    candidates: list[tuple[float, int, int, float, float, bool]] = []
    for i, f in fac.iterrows():
        fid = str(f["ghgrp_facility_id"])
        if fid in matched_facilities:
            continue
        lon, lat = _pt(f["lon"]), _pt(f["lat"])
        if lon is None or lat is None:
            continue
        cy, cx = math.floor(lat / cell), math.floor(lon / cell)
        state = str(f["state_code"])
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in cells.get((state, cy + dy, cx + dx), []):
                    a = ast.iloc[j]
                    alon, alat = _pt(a["lon"]), _pt(a["lat"])
                    if alon is None or alat is None:
                        continue
                    km = haversine_km(lat, lon, alat, alon)
                    if km > max_distance_km:
                        continue
                    ns = name_score(f["name"], a["name"])
                    comp = naics_compatible(f.get("naics_code"), a["asset_type"])
                    s = score_pair(km, ns, comp)
                    if s > 0:
                        candidates.append((s, int(i), j, km, ns, comp))

    candidates.sort(key=lambda c: (-c[0], c[3], c[1], c[2]))
    for s, i, j, km, ns, comp in candidates:
        f, a = fac.iloc[i], ast.iloc[j]
        fid, aid = str(f["ghgrp_facility_id"]), str(a["asset_id"])
        if fid in matched_facilities or aid in used_assets:
            continue
        matched_facilities.add(fid)
        used_assets.add(aid)
        rows.append(
            {
                "ghgrp_facility_id": fid,
                "asset_id": aid,
                "asset_type": a["asset_type"],
                "asset_source_id": a["source_id"],
                "source_asset_id": str(a["source_asset_id"]),
                "asset_name": a["name"],
                "method": "geo_name",
                "score": s,
                "distance_km": round(km, 4),
                "name_score": ns,
                "naics_compatible": bool(comp),
                "accepted": bool(s >= threshold),
            }
        )
    out = pd.DataFrame(rows, columns=MATCH_COLUMNS)
    return out.sort_values(["ghgrp_facility_id", "asset_id"]).reset_index(drop=True)


def _distance(f: pd.Series, a: pd.Series) -> float | None:
    flon, flat, alon, alat = _pt(f["lon"]), _pt(f["lat"]), _pt(a["lon"]), _pt(a["lat"])
    if flon is None or flat is None or alon is None or alat is None:
        return None
    return round(haversine_km(flat, flon, alat, alon), 4)


def evaluate_against_crosswalk(
    facilities: pd.DataFrame, assets: pd.DataFrame, crosswalk: pd.DataFrame, *, thresholds: list[float]
) -> pd.DataFrame:
    """Path 2 run blind on the crosswalked facilities, judged by EPA's crosswalk at each threshold.

    Gold = every (facility, power_plant asset) pair the crosswalk states and the asset table holds.
    A blind pair is correct when its (facility, asset) is in gold. Returns one row per threshold:
    `threshold, pairs_accepted, correct, precision, recall`.
    """
    plants = assets[assets["asset_type"] == "power_plant"]
    codes = plants["source_asset_id"].astype(str).str.strip()
    ids_by_code: dict[str, list[str]] = {}
    for aid, code in zip(plants["asset_id"].astype(str), codes, strict=True):
        ids_by_code.setdefault(code, []).append(aid)
    gold: set[tuple[str, str]] = set()
    for fid, oris in crosswalk[["ghgrp_facility_id", "oris_code"]].itertuples(index=False):
        for aid in ids_by_code.get(str(oris).strip(), []):
            gold.add((str(fid), aid))
    gold_fids = {f for f, _ in gold}
    subset = facilities[facilities["ghgrp_facility_id"].astype(str).isin(gold_fids)]
    blind = match_facilities(subset, assets, crosswalk=None, threshold=0.0, use_crosswalk=False)
    out = []
    for t in thresholds:
        acc = blind[blind["score"] >= t]
        pairs = set(zip(acc["ghgrp_facility_id"].astype(str), acc["asset_id"].astype(str), strict=True))
        correct = len(pairs & gold)
        out.append(
            {
                "threshold": t,
                "gold_pairs": len(gold),
                "pairs_accepted": len(pairs),
                "correct": correct,
                "precision": round(correct / len(pairs), 4) if pairs else None,
                "recall": round(correct / len(gold), 4) if gold else None,
            }
        )
    return pd.DataFrame(out)


SUMMARY_MEMBER_RE = re.compile(r"^ghgp_data_(\d{4})\.xlsx$", re.I)
SUMMARY_SHEETS: dict[str, tuple[str, str]] = {
    # sheet -> (quantity column on that sheet, output column)
    "Geologic Sequestration of CO2": ("Total Mass of CO2 Sequestered", "rr_co2_sequestered_t"),
    "CO2 Injection": ("Quantity of CO2 Received for Injection", "uu_co2_received_t"),
}
SUMMARY_COLUMNS: list[str] = [
    "ghgrp_facility_id",
    "summary_year",
    "rr_co2_sequestered_t",
    "rr_co2_sequestered_confidential",
    "uu_co2_received_t",
    "uu_co2_received_confidential",
]
#: EPA prints this literal in place of a quantity claimed as CBI (80 of 81 UU rows in RY2023).
CONFIDENTIAL = "confidential"


def parse_summary_zip(content: bytes) -> pd.DataFrame:
    """Subpart RR and UU quantities (`SUMMARY_COLUMNS`) from the newest `ghgp_data_<year>.xlsx`
    member of a Data Summary Spreadsheets zip, or from a bare xlsx with the same sheets."""
    if content.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [
                (int(m.group(1)), n)
                for n in zf.namelist()
                if (m := SUMMARY_MEMBER_RE.match(pathlib.PurePosixPath(n).name))
            ]
            if members:
                year, member = max(members)
                workbook = zf.read(member)
            else:
                year, workbook = 0, content  # a bare xlsx is itself a zip container
    else:
        raise ValueError("summary payload is neither a zip nor an xlsx")
    book = pd.ExcelFile(io.BytesIO(workbook))
    frames: list[pd.DataFrame] = []
    for sheet, (quantity, out_col) in SUMMARY_SHEETS.items():
        if sheet not in book.sheet_names:
            raise ValueError(f"summary workbook has no sheet {sheet!r}: {book.sheet_names}")
        df = book.parse(sheet, header=3, dtype=object)
        df.columns = [str(c).strip() for c in df.columns]
        if "Facility Id" not in df.columns or quantity not in df.columns:
            raise ValueError(f"sheet {sheet!r} layout changed: {list(df.columns)[:14]}")
        df = df[df["Facility Id"].notna()]
        raw_q = df[quantity]
        confidential = raw_q.map(lambda v: isinstance(v, str) and v.strip().lower() == CONFIDENTIAL)
        frames.append(
            pd.DataFrame(
                {
                    "ghgrp_facility_id": [str(int(float(v))) for v in df["Facility Id"]],
                    out_col: pd.to_numeric(raw_q, errors="coerce"),
                    out_col.rsplit("_t", 1)[0] + "_confidential": confidential.astype(bool).to_numpy(),
                }
            )
        )
    out = frames[0].merge(frames[1], on="ghgrp_facility_id", how="outer") if len(frames) > 1 else frames[0]
    for col in ("rr_co2_sequestered_confidential", "uu_co2_received_confidential"):
        out[col] = out[col].where(out[col].notna(), False).astype(bool)
    if year == 0:
        year_cell = book.parse(next(iter(SUMMARY_SHEETS)), header=None, nrows=1).iloc[0, 0]
        m = re.search(r"(\d{4})", str(year_cell))
        year = int(m.group(1)) if m else 0
    out["summary_year"] = year
    return out[SUMMARY_COLUMNS].reset_index(drop=True)


def enrich_with_summary(facilities: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    """Left-join the RR/UU quantities onto the facility frame (columns added or replaced)."""
    out = facilities.drop(columns=[c for c in SUMMARY_COLUMNS[1:] if c in facilities.columns])
    keyed = summary.assign(ghgrp_facility_id=summary["ghgrp_facility_id"].astype(str))
    return out.merge(keyed, on="ghgrp_facility_id", how="left")


OWNER_COLUMNS: list[str] = [
    "asset_id",
    "ghgrp_facility_id",
    "owner_name",
    "share_pct",
    "share_flag",
    "as_of",
    "match_method",
    "match_score",
    "source_url",
    "retrieved_at",
    "licence",
]


def build_owner_rows(facilities: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """One row per (accepted match, parent) — `OWNER_COLUMNS`; shares exactly as stated."""
    accepted = matches[matches["accepted"].astype(bool)]
    fac = facilities.set_index(facilities["ghgrp_facility_id"].astype(str))
    rows: list[dict[str, Any]] = []
    for m in accepted.to_dict("records"):
        f = fac.loc[str(m["ghgrp_facility_id"])]
        parents = f["parents"]
        if isinstance(parents, str):
            parents = json.loads(parents) if parents else []
        year = f["reporting_year"]
        as_of = dt.date(int(year), 12, 31) if year is not None and not pd.isna(year) else None
        for p in parents or []:
            rows.append(
                {
                    "asset_id": m["asset_id"],
                    "ghgrp_facility_id": str(m["ghgrp_facility_id"]),
                    "owner_name": p["name"],
                    "share_pct": p.get("share_pct"),
                    "share_flag": f["share_flag"],
                    "as_of": as_of,
                    "match_method": m["method"],
                    "match_score": m["score"],
                    "source_url": f["source_url"],
                    "retrieved_at": f["retrieved_at"],
                    "licence": f["licence"],
                }
            )
    out = pd.DataFrame(rows, columns=OWNER_COLUMNS)
    out["share_pct"] = out["share_pct"].astype("Float64")
    return out


# ------------------------------------------------------------------ CLI
def _latest_snapshot() -> pathlib.Path:
    return fuels.latest_snapshot(SOURCE_ID, "csv")


def facilities_from_snapshot(path: pathlib.Path, *, manifest: pathlib.Path | None = None) -> pd.DataFrame:
    entry = fuels.entry_for(SOURCE_ID, manifest)
    retrieved_at, source_url = fuels.snapshot_metadata(path, SOURCE_ID, entry.url)
    raw = RawSnapshot.from_file(
        path,
        source_url,
        "text/csv",
        retrieved_at=dt.datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")),
    )
    connector = Connector(entry)
    rows = connector.parse(raw)
    return build_facilities(rows, retrieved_at=retrieved_at, licence_id=entry.licence_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--snapshot", type=pathlib.Path, help="A stored pub_dim_facility CSV snapshot")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Newest data/snapshots/us.epa.ghgrp/*.csv"
    )
    group.add_argument(
        "--fetch", action="store_true", help="Fetch the newest reporting year now (polite session)"
    )
    parser.add_argument("--manifest", type=pathlib.Path, default=None)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--summary-zip",
        type=pathlib.Path,
        default=None,
        help="EPA Data Summary Spreadsheets zip for RR/UU quantities",
    )
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    if args.fetch:
        entry = fuels.entry_for(SOURCE_ID, args.manifest)
        connector = Connector(entry, http=fuels.polite_session())
        raw = connector.fetch()
        content = connector.redact(raw.content)
        path, _ = fuels.record_snapshot(
            SOURCE_ID,
            content,
            ext="csv",
            fetched_url=raw.url,
            retrieved_at=raw.retrieved_at,
            requests_made=raw.requests_made,
            meta={**raw.meta, "redacted_columns": list(connector.personal_data_columns)},
        )
    else:
        path = args.snapshot or _latest_snapshot()
    facilities = facilities_from_snapshot(path, manifest=args.manifest)
    summary_rows = 0
    if args.summary_zip is not None:
        quantities = parse_summary_zip(args.summary_zip.read_bytes())
        facilities = enrich_with_summary(facilities, quantities)
        summary_rows = len(quantities)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(facilities).to_parquet(args.out, index=False)
    summary: dict[str, Any] = {
        "snapshot": str(path),
        "out": str(args.out),
        "facilities": len(facilities),
        "reporting_years": sorted(int(y) for y in facilities["reporting_year"].dropna().unique()),
        "with_point": int(facilities["lon"].notna().sum()),
        "with_frs_id": int(facilities["frs_id"].notna().sum()),
        "co2_captured": int(facilities["co2_captured"].eq(True).sum()),
        "rr_mrv_plan_url": int(facilities["rr_mrv_plan_url"].notna().sum()),
        "subpart_rr": int(facilities["subpart_rr"].astype(bool).sum()),
        "subpart_uu": int(facilities["subpart_uu"].astype(bool).sum()),
        "subpart_pp": int(facilities["subpart_pp"].astype(bool).sum()),
        "summary_rows_joined": summary_rows,
        "share_flags": {k: int(v) for k, v in facilities["share_flag"].value_counts().items()},
        "registry_version": Registry(args.manifest).version if args.manifest else Registry().version,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_owners.py


if __name__ == "__main__":
    main()
