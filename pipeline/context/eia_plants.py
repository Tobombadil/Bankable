"""EIA-860M "Operating" sheet -> `built_plant` context rows (docs/21 §3.20, docs/00-PLAN.md
decision 2026-09-14 "Built-infrastructure context layer").

Same workbook, same header-row convention as `pipeline/connectors/us_eia_860m/connector.py`
(sheet header on the third row), but the "Operating" sheet instead of "Planned": this is the
existing fleet drawn as context beneath the proposals map, not a proposal source. One row per
Plant ID here, not per generator -- a plant's units are summed and split by raw technology label
(`services/db/models.py::BuiltPlant` docstring).

Since lane R1 (2026-10-06) the same workbook's "Retired" sheet is read too: a plant whose every
generator has retired becomes a row of its own (status `retired`), and every row carries the plant
status (`operating` | `retiring` | `retired`), `retirement_year` and `attributes.retirement`
derived by `pipeline/context/retirements.py`, plus `attributes.balancing_authority_code`. One EIA
plant id is one `power_plant` asset whichever of the three it is.

`capacity()`/`classify_tech()`/`SOURCE_META` are the same untyped Phase 2 prototype functions
`pipeline/normalize.py` gives the ISO/EIA connectors (`pipeline/connectors/canonical.py` is the
typed seam for the connector layer; this module is its own such seam for the context layer, same
boundary, same `no-untyped-call` allowance).
"""

# mypy: disable-error-code="no-untyped-call"
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import time
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError, to_parquet_safe
from pipeline.connectors.store import DATA_DIR
from pipeline.context.retirements import (
    GeneratorSheets,
    PlantRetirement,
    load_generator_status_map,
    parse_generator_sheets,
    records_from_sheets,
    summarise_plants,
)
from pipeline.normalize import SOURCE_META, capacity, classify_tech

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: Writable data (snapshots/, runs/, normalized/) lives under the connector data root, `INFRAQUE_DATA_DIR`
#: when set (a container's volume), else this checkout's data/ (`pipeline.connectors.store.DATA_DIR`).
SNAPSHOT_DIR = DATA_DIR / "snapshots" / "us.eia.860m"
RUNS_DIR = DATA_DIR / "runs" / "us.eia.860m"
DEFAULT_OUT = DATA_DIR / "normalized" / "context" / "us.eia.860m.plants.parquet"

SOURCE_ID = "us.eia.860m"
_, INDEX_URL, LICENCE = SOURCE_META["eia860m"]

#: World bounds a real coordinate must fall inside, `(0, 0)` excluded separately as the common
#: placeholder for an unset field -- the same rule `services/ingest/loader.py::_extract_exact_point`
#: applies to this same workbook's `Latitude`/`Longitude` columns on the Planned sheet.
_WORLD_LAT_RANGE = (-90.0, 90.0)
_WORLD_LON_RANGE = (-180.0, 180.0)


def _valid_point(lat_raw: Any, lon_raw: Any) -> tuple[float, float] | None:
    if lat_raw is None or lon_raw is None:
        return None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return None
    if pd.isna(lat) or pd.isna(lon):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if not (_WORLD_LAT_RANGE[0] <= lat <= _WORLD_LAT_RANGE[1]):
        return None
    if not (_WORLD_LON_RANGE[0] <= lon <= _WORLD_LON_RANGE[1]):
        return None
    return (lon, lat)


def _first_non_null(series: pd.Series) -> Any:
    s = series.dropna()
    return s.iloc[0] if len(s) else None


def parse_operating_sheet(content: bytes) -> pd.DataFrame:
    """Sheet "Operating", header on the third row (same convention as the Planned sheet).

    Drops rows with no Plant ID -- the trailing note rows the workbook appends after the last
    real row (no Plant ID, no Plant Name; the same shape `us_eia_860m/connector.py::parse`
    already drops from the Planned sheet).
    """
    df = pd.read_excel(io.BytesIO(content), sheet_name="Operating", header=2, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    if "Plant ID" not in df.columns:
        raise ParseError(f"Operating sheet layout changed: {list(df.columns)[:8]}")
    return df[df["Plant ID"].notna()].reset_index(drop=True)


def _plant_row(
    plant_id: Any,
    g: pd.DataFrame,
    *,
    url: str,
    retrieved_at: str,
    retirement: PlantRetirement | None,
) -> dict[str, Any]:
    """One `power_plant` row from a plant's generator rows (Operating rows, or the Retired rows of
    a plant with nothing left in service: both sheets carry the same identity, capacity,
    technology, year and coordinate columns)."""
    # `capacity()` treats a reported 0/NaN nameplate as missing, not a zero-MW unit (the same
    # rule `pipeline/normalize.py::capacity` documents for ERCOT/NYISO's co-located additions).
    nameplate = [capacity(v) for v in g["Nameplate Capacity (MW)"]]
    valid = [v for v in nameplate if v is not None]
    cap_total = round(sum(valid), 3) if valid else None

    tech_totals: dict[str, float] = {}
    for raw_tech, mw in zip(g["Technology"], nameplate, strict=True):
        if raw_tech is None or (isinstance(raw_tech, float) and pd.isna(raw_tech)):
            continue
        label = str(raw_tech).strip()
        if not label:
            continue
        tech_totals[label] = tech_totals.get(label, 0.0) + (mw or 0.0)
    tech_totals = {label: round(mw, 3) for label, mw in tech_totals.items()}

    dominant_label = max(tech_totals, key=lambda label: tech_totals[label]) if tech_totals else None
    technology, _kind = classify_tech(dominant_label) if dominant_label else ("unknown", "other")

    years = pd.to_numeric(g["Operating Year"], errors="coerce").dropna()
    earliest_year = int(years.min()) if len(years) else None

    point = _valid_point(_first_non_null(g["Latitude"]), _first_non_null(g["Longitude"]))
    lon, lat = point if point is not None else (None, None)

    state = _first_non_null(g["Plant State"])
    county = _first_non_null(g["County"])
    entity_name = _first_non_null(g["Entity Name"])
    plant_name = _first_non_null(g["Plant Name"])
    source_plant_id = str(int(plant_id))
    ba = _first_non_null(g["Balancing Authority Code"]) if "Balancing Authority Code" in g.columns else None

    attributes: dict[str, Any] = {}
    if ba is not None and str(ba).strip():
        attributes["balancing_authority_code"] = str(ba).strip()
    if retirement is not None and retirement.block is not None:
        attributes["retirement"] = retirement.block

    return {
        "source_id": SOURCE_ID,
        "source_plant_id": source_plant_id,
        "name": str(plant_name).strip() if plant_name else f"EIA Plant {source_plant_id}",
        "operator_name": str(entity_name).strip() if entity_name else None,
        "technology": technology,
        "technology_raw": dominant_label,
        "technologies": tech_totals,
        "capacity_mw": cap_total,
        "generator_count": len(g),
        "earliest_operating_year": earliest_year,
        "lon": lon,
        "lat": lat,
        "state_code": f"US-{str(state).strip().upper()}" if state else None,
        "county_name": str(county).strip() if county else None,
        "country": "US",
        "status": retirement.status if retirement is not None else "operating",
        "retirement_year": retirement.retirement_year if retirement is not None else None,
        "attributes": attributes,
        "source_url": url,
        "retrieved_at": retrieved_at,
        "licence": LICENCE,
    }


def aggregate_plants(
    df: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str | None = None,
    retired: pd.DataFrame | None = None,
    as_of: str | None = None,
) -> pd.DataFrame:
    """One row per Plant ID, columns exactly as `services/db/models.py::BuiltPlant` expects.

    `source_url` is a keyword the task's function signature omits but the output schema needs
    (the workbook URL, which only the caller -- the snapshot or the run record -- knows); it
    defaults to the EIA-860M index URL, the same fallback `SOURCE_META["eia860m"]` gives
    `pipeline/normalize.py`'s ISO/EIA connectors when a per-row URL isn't available.

    `retired` is the same workbook's Retired sheet (lane R1, 2026-10-06). With it, a plant whose
    every generator has retired is a row too (status `retired`, its capacity and technology split
    the retired units'), and every plant carries the status, `retirement_year` and
    `attributes.retirement` block `pipeline/context/retirements.py` derives from both sheets, so
    one plant is one record whether it is operating, retiring or retired. Without it every row is
    still classified from the Operating sheet's planned-retirement columns.
    """
    url = source_url or INDEX_URL
    retired = retired if retired is not None else df.iloc[0:0]
    sheets = GeneratorSheets(operating=df, retired=retired, as_of=as_of)
    summaries = summarise_plants(records_from_sheets(sheets, load_generator_status_map()), as_of=as_of)
    rows: list[dict[str, Any]] = []
    for plant_id, g in df.groupby("Plant ID", sort=False):
        pid = str(int(plant_id))
        rows.append(
            _plant_row(plant_id, g, url=url, retrieved_at=retrieved_at, retirement=summaries.get(pid))
        )
    in_operating = {str(int(p)) for p in df["Plant ID"].dropna().unique()}
    if len(retired):
        for plant_id, g in retired.groupby("Plant ID", sort=False):
            pid = str(int(plant_id))
            if pid in in_operating:
                continue
            rows.append(
                _plant_row(plant_id, g, url=url, retrieved_at=retrieved_at, retirement=summaries.get(pid))
            )
    out = pd.DataFrame(rows)
    if "retirement_year" in out.columns:
        out["retirement_year"] = out["retirement_year"].astype("Int64")
    return out


def _latest_snapshot(snapshot_dir: pathlib.Path = SNAPSHOT_DIR) -> pathlib.Path:
    candidates = sorted(snapshot_dir.glob("*.xlsx"))
    if not candidates:
        raise FileNotFoundError(f"no *.xlsx snapshot under {snapshot_dir}")
    return candidates[-1]


def _token_to_iso(token: str) -> str | None:
    try:
        ts = dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_metadata(
    workbook_path: pathlib.Path, runs_dir: pathlib.Path = RUNS_DIR
) -> tuple[str, str | None]:
    """`(retrieved_at_iso, source_url)` for a snapshot named the way `pipeline.connectors.store`
    names them (`<ts_token>.xlsx`): the token itself gives `retrieved_at`, and the matching
    `data/runs/us.eia.860m/<ts_token>.json` (when present) gives the exact workbook URL that run
    fetched (`snapshot.fetched_url` -- see that record's shape). Falls back to "now" and the
    index URL for a workbook that isn't one of this store's own snapshots (e.g. a test fixture)."""
    token = workbook_path.stem
    retrieved_at = _token_to_iso(token) or dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    run_path = runs_dir / f"{token}.json"
    source_url = None
    if run_path.exists():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
            source_url = ((record.get("snapshot") or {}).get("fetched_url")) or None
        except (json.JSONDecodeError, OSError):
            source_url = None
    return retrieved_at, source_url


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--workbook", type=pathlib.Path, help="Path to an EIA-860M .xlsx workbook")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Use the newest data/snapshots/us.eia.860m/*.xlsx"
    )
    parser.add_argument(
        "--data-root",
        type=pathlib.Path,
        default=None,
        help="Data root holding snapshots/, runs/ and normalized/ (default: INFRAQUE_DATA_DIR, else "
        "this checkout's data/). A worktree whose data lives elsewhere names it here, so the snapshot, "
        "its run record (the workbook URL) and the default --out all come from the same root.",
    )
    parser.add_argument("--out", type=pathlib.Path, default=None)
    args = parser.parse_args(argv)

    data_root = args.data_root or DATA_DIR
    snapshot_dir = data_root / "snapshots" / SOURCE_ID
    runs_dir = data_root / "runs" / SOURCE_ID
    out = args.out or data_root / "normalized" / "context" / DEFAULT_OUT.name

    t0 = time.monotonic()
    workbook_path = args.workbook or _latest_snapshot(snapshot_dir)
    retrieved_at, source_url = _snapshot_metadata(workbook_path, runs_dir)

    sheets = parse_generator_sheets(workbook_path.read_bytes())
    plants = aggregate_plants(
        sheets.operating,
        retrieved_at=retrieved_at,
        source_url=source_url,
        retired=sheets.retired,
        as_of=sheets.as_of,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(plants).to_parquet(out, index=False)

    with_coords = int(plants["lon"].notna().sum())
    by_technology = plants["technology"].value_counts().to_dict()
    summary = {
        "plants": len(plants),
        "with_coordinates": with_coords,
        "without_coordinates": len(plants) - with_coords,
        "by_technology": by_technology,
        "by_status": plants["status"].value_counts().to_dict(),
        "dropped_without_plant_id": sheets.dropped_without_plant_id,
        "as_of": sheets.as_of,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as web/build_data.py


if __name__ == "__main__":
    main()
