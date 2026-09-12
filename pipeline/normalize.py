#!/usr/bin/env python3
"""Normalise the five reachable ISO interconnection queues and the EIA-860M "Planned" sheet
into one canonical proposal table (docs/02 §5, docs/20 §3.4).

Reads   data/eval/raw/<source>.<date>.parquet   (written by pipeline/pull.py)
Writes  data/eval/normalized.parquet
Status harmonisation lives in pipeline/status_map.yaml, not here.

Usage:
    python pipeline/normalize.py [--date YYYY-MM-DD] [--out data/eval/normalized.parquet]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "eval" / "raw"
STATUS_MAP_PATH = pathlib.Path(__file__).resolve().parent / "status_map.yaml"

CANONICAL_COLUMNS = [
    # identity / provenance
    "record_id", "source_id", "source_record_id", "source_url", "retrieved_at", "licence",
    # what it is
    "kind", "name_canonical", "name_norm", "sponsor_name", "sponsor_norm",
    "technology", "technology_raw", "capacity_mw", "storage_mwh",
    # where it is
    "iso", "state", "county", "county_norm",
    # lifecycle
    "lifecycle_state", "status_raw", "status_rule", "status_conflict",
    "queue_date", "proposed_cod",
    # resolution keys
    "queue_id", "eia_plant_id", "eia_generator_id", "cross_refs",
]

SOURCE_META = {
    # licence classes from docs/02 §4
    "caiso": ("CAISO", "https://www.caiso.com/planning/generator-interconnection-queue",
              "iso-attribution"),
    "ercot": ("ERCOT", "https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933",
              "iso-permissive"),
    "spp": ("SPP", "https://opsportal.spp.org/Studies/GenerateActiveCsv", "iso-unknown"),
    "nyiso": ("NYISO", "https://www.nyiso.com/interconnections", "iso-unknown"),
    "isone": ("ISONE", "https://irtt.iso-ne.com/reports/external", "iso-unknown"),
    "eia860m": (None, "https://www.eia.gov/electricity/data/eia860m/", "public-domain"),
}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}

# ---------------------------------------------------------------- technology vocabulary
# Ordered: first regex that matches the raw technology string wins.
TECH_RULES: list[tuple[str, str, str]] = [
    (r"pumped\s*storage", "pumped_storage", "storage"),
    (r"(solar|photovolt|\bsun\b|\bpv\b).*(storage|batter|\bbat\b)", "solar_storage", "generation"),
    (r"(storage|batter|\bbat\b).*(solar|photovolt|\bsun\b|\bpv\b)", "solar_storage", "generation"),
    (r"(wind|\bwnd\b).*(storage|batter|\bbat\b)", "wind_storage", "generation"),
    (r"(storage|batter|\bbat\b).*(wind|\bwnd\b)", "wind_storage", "generation"),
    (r"offshore\s*wind", "wind_offshore", "generation"),
    (r"wind|\bwnd\b", "wind", "generation"),
    (r"solar\s*thermal", "solar_thermal", "generation"),
    (r"solar|photovolt|\bsun\b|\bpv\b", "solar", "generation"),
    (r"batter|\bbess\b|energy\s*storage|^storage|\bbat\b|\bstorage\b", "storage", "storage"),
    # ERCOT spells out exclusions: "... Turbine, but not part of a Combined-Cycle" (52 rows),
    # "Steam Turbine other than Combined-Cycle" (3 rows). Those must not hit the CC rule.
    (r"steam\s*turbine.*(other than|not part of).*combined", "gas_steam", "generation"),
    (r"(combustion|gas)[\s()a-z]*turbine.*(other than|not part of).*combined", "gas_ct", "generation"),
    (r"combined[\s-]*cycle|\bcc\b", "gas_cc", "generation"),
    (r"(combustion|gas)\s*turbine|\bct\b|\bgt\b", "gas_ct", "generation"),
    (r"internal\s*combustion|reciprocating|\bice\b", "gas_ice", "generation"),
    (r"nuclear|\bnuc\b", "nuclear", "generation"),
    (r"geotherm", "geothermal", "generation"),
    (r"landfill|biomass|\bwds\b|\blfg\b|biogas|digest|wood", "biomass", "generation"),
    (r"municipal\s*solid|waste", "waste", "generation"),
    (r"coal|\bbit\b|lignite|\bsub\b", "coal", "generation"),
    (r"petroleum|fuel\s*oil|\bdfo\b|diesel|\bjf\b|oil", "oil", "generation"),
    (r"fuel\s*cell|\bfc\b", "fuel_cell", "generation"),
    (r"hydrogen", "hydrogen", "generation"),
    (r"hydro|\bwat\b|water", "hydro", "generation"),
    (r"steam\s*turbine|\bst\b", "gas_steam", "generation"),
    (r"natural\s*gas|methane|dual\s*fuel|\bng\b|^gas", "gas_other", "generation"),
    (r"transmission|\bac\b|\bdc\b|line|cable", "transmission", "transmission"),
    (r"load|data\s*cent", "load", "load"),
]

CORP_SUFFIXES = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|lp|l\.p|llp|ltd|limited|"
    r"holdings?|energy|energies|power|renewables?|solar|wind|storage|development|developments?|"
    r"partners?|group|usa|us|america|american|north|project|projects)\b")

NAME_NOISE = re.compile(
    r"\b(project|solar|wind|energy|center|centre|storage|bess|battery|farm|park|facility|"
    r"generating|generation|station|plant|llc|inc|lp|phase|site|hybrid|expansion)\b")

# Cross-ISO references embedded in project names, e.g. "Chazy Lake BESS (NYISO-C24-308)".
# The id part must contain a digit, otherwise ordinary prose ("PJM Rainey") is picked up as a ref.
XREF = re.compile(r"\b(NYISO|ISO-?NE|ISONE|PJM|MISO|SPP|CAISO|ERCOT)[\s:-]+([A-Z0-9\-]*\d[A-Z0-9\-]*)",
                  re.I)


# ---------------------------------------------------------------- small normalisers
def norm_state(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    s = str(v).strip()
    if not s:
        return None
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return US_STATES.get(s.lower(), s.upper()[:2] if s.isalpha() else None)


def norm_county(v) -> str | None:
    if v is None or pd.isna(v):
        return None
    s = str(v).upper().strip()
    s = re.sub(r"\bCOUNT(Y|IES)\b", " ", s)
    s = re.sub(r"\bPARISH\b|\bBOROUGH\b", " ", s)
    s = re.sub(r"[^A-Z0-9/ ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def norm_name(v) -> str | None:
    if v is None or pd.isna(v):
        return None
    s = str(v).upper().strip()
    s = re.sub(r"\([^)]*\)", " ", s)          # drop parenthetical cross-refs
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(I{1,3}|IV|V|VI{0,3}|IX|X)\b", " ", s)  # roman numerals / phase markers
    s = NAME_NOISE.sub(" ", s.lower()).upper()
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def norm_org(v) -> str | None:
    if v is None or pd.isna(v):
        return None
    s = str(v).lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = CORP_SUFFIXES.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper() or None


def classify_tech(raw) -> tuple[str, str]:
    if raw is None or pd.isna(raw) or not str(raw).strip():
        return "unknown", "other"
    s = str(raw).lower()
    for pattern, tech, kind in TECH_RULES:
        if re.search(pattern, s):
            return tech, kind
    return "other", "other"


def to_date(v):
    if v is None or (not isinstance(v, (str, dt.date, dt.datetime, pd.Timestamp)) and pd.isna(v)):
        return pd.NaT
    try:
        ts = pd.to_datetime(v, errors="coerce")
    except Exception:  # noqa: BLE001
        return pd.NaT
    if pd.isna(ts):
        return pd.NaT
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def capacity(*vals):
    """First positive capacity among (nameplate, summer, winter).

    A reported 0.0 MW is treated as missing, not as a zero-MW project: ERCOT publishes 0.0 for
    75 co-located "SLF" storage additions and repowers, and NYISO for 1,541 mostly-withdrawn rows.
    Treating those as 0 would put them in their own capacity block and make them unmatchable."""
    for v in vals:
        f = to_float(v)
        if f is not None and f > 0:
            return f
    return None


def to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def cross_refs(name) -> str:
    if name is None or pd.isna(name):
        return ""
    out = {f"{m.group(1).upper().replace('-', '')}:{m.group(2).upper()}" for m in XREF.finditer(str(name))}
    return "|".join(sorted(out))


# ---------------------------------------------------------------- status harmonisation
def load_status_map(path: pathlib.Path = STATUS_MAP_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def harmonise_status(source_id: str, ctx: dict, status_map: dict) -> tuple[str, str]:
    """Return (canonical_state, rule_id).

    `ctx` carries the normalised status inputs for one row: status_raw, status_original,
    ia_status, project_status, ia_signed, approved_for_energization. Refine rules are tried
    in file order and the first whose every condition matches wins; otherwise the base map on
    the source's `field` value applies; otherwise `unknown`.
    """
    cfg = status_map["sources"].get(source_id)
    if cfg is None:
        return "unknown", "no_source_config"
    for rule in cfg.get("refine") or []:
        conds = rule["when"]
        if all(str(ctx.get(k) or "").strip() == str(v).strip() for k, v in conds.items()):
            return rule["then"], rule["id"]
    field_key = {"Status": "status_raw", "Status (Original)": "status_original"}[cfg["field"]]
    key = ctx.get(field_key)
    mapping = cfg["map"]
    if key is None or (isinstance(key, str) and not key.strip()):
        # SPP falls back to gridstatus's harmonised Status (its own vocabulary) when blank
        key = ctx.get("status_raw")
        mapping = cfg.get("fallback_map") or cfg["map"]
    if key is None or (isinstance(key, str) and not key.strip()):
        return "unknown", f"{source_id}.blank"
    mapped = mapping.get(str(key).strip())
    if mapped is None:
        return "unknown", f"{source_id}.unmapped"
    return mapped, f"{source_id}.map"


# ---------------------------------------------------------------- per-source normalisers
def _blank(n: int):
    return pd.Series([None] * n, dtype="object")


def normalize_iso(df: pd.DataFrame, source_id: str, status_map: dict,
                  retrieved_at: str) -> pd.DataFrame:
    iso, url, licence = SOURCE_META[source_id]
    n = len(df)
    get = lambda c: df[c] if c in df.columns else _blank(n)  # noqa: E731

    ctxs = pd.DataFrame({
        "status_raw": get("Status").astype("object"),
        "status_original": get("Status (Original)").astype("object"),
        "ia_status": get("Interconnection Agreement Status").astype("object"),
        "project_status": get("Project Status").astype("object"),
        "ia_signed": get("IA Signed").notna().map({True: "yes", False: "no"}),
        "approved_for_energization": get("Approved for Energization").notna().map(
            {True: "yes", False: "no"}),
    })
    harmonised = [harmonise_status(source_id, r, status_map) for r in ctxs.to_dict("records")]

    tech = [classify_tech(v) for v in get("Generation Type")]
    qid = get("Queue ID").astype("string").str.strip()
    name = get("Project Name")
    sponsor = get("Interconnecting Entity")
    # docs/20 §3.1: when the source gives no id (NYISO: 1,350 withdrawn rows have no queue
    # position) the source_record_id is a content hash of the identifying columns.
    ident = pd.DataFrame({"n": name.astype("string"), "c": get("County").astype("string"),
                          "s": get("State").astype("string"),
                          "m": get("Capacity (MW)").astype("string"),
                          "d": get("Queue Date").astype("string"),
                          "st": get("Status").astype("string")}).fillna("").agg("|".join, axis=1)
    srid = qid.copy()
    no_id = srid.isna() | (srid == "")
    srid[no_id] = "h" + ident[no_id].map(lambda t: hashlib.sha1(t.encode()).hexdigest()[:12])

    out = pd.DataFrame({
        "source_id": source_id,
        "source_record_id": srid,
        "source_url": url,
        "retrieved_at": retrieved_at,
        "licence": licence,
        "kind": [k for _, k in tech],
        "name_canonical": name.astype("string").str.strip(),
        "name_norm": [norm_name(v) for v in name],
        "sponsor_name": sponsor.astype("string").str.strip(),
        "sponsor_norm": [norm_org(v) for v in sponsor],
        "technology": [t for t, _ in tech],
        "technology_raw": get("Generation Type").astype("string"),
        "capacity_mw": pd.array(
            [capacity(a, b, c) for a, b, c in zip(get("Capacity (MW)"),
                                                  get("Summer Capacity (MW)"),
                                                  get("Winter Capacity (MW)"))], dtype="Float64"),
        "storage_mwh": pd.array([None] * n, dtype="Float64"),
        "iso": iso,
        "state": [norm_state(v) for v in get("State")],
        "county": get("County").astype("string").str.strip(),
        "county_norm": [norm_county(v) for v in get("County")],
        "lifecycle_state": [s for s, _ in harmonised],
        "status_raw": ctxs["status_raw"].astype("string"),
        "status_rule": [r for _, r in harmonised],
        "queue_date": [to_date(v) for v in get("Queue Date")],
        "proposed_cod": [to_date(v) for v in get("Proposed Completion Date")],
        "queue_id": qid,
        "eia_plant_id": pd.array([None] * n, dtype="string"),
        "eia_generator_id": pd.array([None] * n, dtype="string"),
        "cross_refs": [cross_refs(v) for v in name],
    })
    # status_conflict: withdrawn row that also carries evidence of a later lifecycle point
    later = ctxs["project_status"].isin(["Under Study", "In Service", "Under Construction"]) | \
        ctxs["ia_status"].eq("Executed")
    out["status_conflict"] = (out["lifecycle_state"].eq("withdrawn") & later).fillna(False)
    out["record_id"] = out["source_id"] + ":" + out["source_record_id"].fillna("")
    return out


def normalize_eia(df: pd.DataFrame, status_map: dict, retrieved_at: str) -> pd.DataFrame:
    _, url, licence = SOURCE_META["eia860m"]
    n = len(df)
    get = lambda c: df[c] if c in df.columns else _blank(n)  # noqa: E731
    ctxs = pd.DataFrame({"status_raw": get("Status").astype("object")})
    harmonised = [harmonise_status("eia860m", r, status_map) for r in ctxs.to_dict("records")]
    tech = [classify_tech(v) for v in get("Technology")]

    plant = get("Plant ID").map(lambda v: None if pd.isna(v) else str(int(float(v))))
    gen = get("Generator ID").astype("string").str.strip()
    srid = (plant.fillna("NA") + "-" + gen.fillna("NA")).astype("string")

    # planned COD from month/year
    def cod(row):
        y, m = row
        if pd.isna(y):
            return pd.NaT
        try:
            return pd.Timestamp(int(float(y)), int(float(m)) if not pd.isna(m) else 1, 1)
        except (ValueError, TypeError):
            return pd.NaT

    out = pd.DataFrame({
        "source_id": "eia860m",
        "source_record_id": srid,
        "source_url": url,
        "retrieved_at": retrieved_at,
        "licence": licence,
        "kind": [k for _, k in tech],
        "name_canonical": get("Plant Name").astype("string").str.strip(),
        "name_norm": [norm_name(v) for v in get("Plant Name")],
        "sponsor_name": get("Entity Name").astype("string").str.strip(),
        "sponsor_norm": [norm_org(v) for v in get("Entity Name")],
        "technology": [t for t, _ in tech],
        "technology_raw": get("Technology").astype("string"),
        "capacity_mw": pd.array(
            [capacity(a, b) for a, b in zip(get("Nameplate Capacity (MW)"),
                                            get("Net Summer Capacity (MW)"))], dtype="Float64"),
        "storage_mwh": pd.array([None] * n, dtype="Float64"),
        "iso": get("Balancing Authority Code").astype("string"),
        "state": [norm_state(v) for v in get("Plant State")],
        "county": get("County").astype("string").str.strip(),
        "county_norm": [norm_county(v) for v in get("County")],
        "lifecycle_state": [s for s, _ in harmonised],
        "status_raw": ctxs["status_raw"].astype("string"),
        "status_rule": [r for _, r in harmonised],
        "status_conflict": False,
        "queue_date": pd.array([pd.NaT] * n, dtype="datetime64[ns]"),
        "proposed_cod": list(map(cod, zip(get("Planned Operation Year"),
                                          get("Planned Operation Month")))),
        "queue_id": pd.array([None] * n, dtype="string"),
        "eia_plant_id": plant,
        "eia_generator_id": gen,
        "cross_refs": "",
    })
    out["record_id"] = "eia860m:" + out["source_record_id"].fillna("")
    return out


# ---------------------------------------------------------------- driver
def build(date: str, raw_dir: pathlib.Path = RAW) -> pd.DataFrame:
    status_map = load_status_map()
    manifest_path = raw_dir / f"manifest.{date}.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    frames = []
    for source_id in ["caiso", "ercot", "spp", "nyiso", "isone", "eia860m"]:
        path = raw_dir / f"{source_id}.{date}.parquet"
        if not path.exists():
            print(f"  ! missing {path}", file=sys.stderr)
            continue
        raw = pd.read_parquet(path)
        retrieved_at = (manifest.get(source_id) or {}).get("retrieved_at") or f"{date}T00:00:00Z"
        if source_id == "eia860m":
            frames.append(normalize_eia(raw, status_map, retrieved_at))
        else:
            frames.append(normalize_iso(raw, source_id, status_map, retrieved_at))
    out = pd.concat(frames, ignore_index=True)[CANONICAL_COLUMNS]
    for c in ("source_record_id", "name_canonical", "name_norm", "sponsor_name", "sponsor_norm",
              "technology", "technology_raw", "state", "county", "county_norm", "status_raw",
              "queue_id", "eia_plant_id", "eia_generator_id", "cross_refs", "iso"):
        out[c] = out[c].astype("string")
    out["status_conflict"] = out["status_conflict"].fillna(False).astype(bool)
    # record_id must be unique. ISO-NE reuses queue ids (92 ids over 242 rows) and NYISO has 2
    # duplicates, so suffix the 2nd, 3rd... occurrence with #2, #3 in file order.
    dup_n = out.groupby("record_id").cumcount()
    out.loc[dup_n > 0, "record_id"] = out["record_id"] + "#" + (dup_n + 1).astype(str)
    assert out["record_id"].is_unique
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-12")
    ap.add_argument("--out", default=str(ROOT / "data" / "eval" / "normalized.parquet"))
    args = ap.parse_args()

    df = build(args.date)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)

    print(f"normalized {len(df):,} records -> {out} ({out.stat().st_size/1e6:.2f} MB)")
    print("\nrows per source")
    print(df["source_id"].value_counts().rename_axis("source").to_frame("rows").to_string())
    print("\nrows per canonical lifecycle state")
    print(df["lifecycle_state"].value_counts().rename_axis("state").to_frame("rows").to_string())
    print("\nsource x canonical state")
    print(pd.crosstab(df["source_id"], df["lifecycle_state"]).to_string())
    print("\nstatus_conflict rows:", int(df["status_conflict"].sum()))
    print("unknown lifecycle_state rows:", int((df["lifecycle_state"] == "unknown").sum()))
    print("\ntechnology (top 12)")
    print(df["technology"].value_counts().head(12).to_string())
    print("\nfield coverage (non-null %)")
    cov = (df.notna().mean() * 100).round(1)
    print(cov[["name_canonical", "sponsor_name", "state", "county", "capacity_mw",
               "queue_date", "proposed_cod", "eia_plant_id"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
