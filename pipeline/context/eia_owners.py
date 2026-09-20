"""EIA-860 Schedule 4 "Ownership" sheet -> the ownership-graph parquet (docs/21 §3.23,
`docs/adr/0008-assets-first-class-and-placement-grades.md` decision 2).

Same split as `pipeline/context/eia_plants.py`: `pipeline/connectors/us_eia_860/connector.py`
owns the shared parsers (`find_owner_member`, `parse_owner_sheet`) and the crawl/health-check
path; this CLI is the actual producer of the ownership rows `asset_owner` loads from.

Where the zip comes from. The current final release sits at `xls/eia860<year>.zip` under the
EIA-860 index page, which robots.txt allows, so `python -m pipeline.connectors run us.eia.860`
fetches it and stores it as `data/snapshots/us.eia.860/<ts_token>.zip` (first real run 2026-09-19:
`xls/eia8602025.zip`, 23,622,347 bytes). Older years move to `archive/xls/`, a path eia.gov's
robots.txt disallows (`/*archive/`) and the connector never requests; a human downloads such a
year in a browser and hands it in with `--archive PATH`. `--latest-snapshot` (the default when
neither flag is given) picks the newest store snapshot.

Output columns exactly (`OUTPUT_COLUMNS`): `source_plant_id` (str), `generator_id` (str),
`owner_name` (str), `ownership_pct` (float, 0-100), `generator_status` (str, the sheet's
`Status` code: OP operating, SB standby, OS out of service, RE retired, CN cancelled, and the
proposed codes P/L/T/U/V/TS/IP/OA -- null when the sheet lacks the column),
`generator_capacity_mw` (float, nullable), `plant_capacity_mw` (float, nullable), `as_of` (date,
December 31 of the data year -- EIA-860 is an annual snapshot with no finer-grained date),
`source_url`, `retrieved_at` (ISO 8601 UTC), `licence` ("public-domain").

The two capacity columns come from the same zip's `3_1_Generator_Y<year>.xlsx`, sheet
"Operable" (header on the second row, like the Ownership sheet): `generator_capacity_mw` is that
generator's `Nameplate Capacity (MW)` and `plant_capacity_mw` the sum of nameplate over **all**
operable generators of the plant, including the ones Schedule 4 does not list. Schedule 4 lists
"Jointly or Third-Party Owned" generators only (its own title row), so a plant's listed shares do
not sum to 100 % of the plant: the remainder is the operator's wholly-owned generators, which
`services/ingest/ownership.py` leaves implicit rather than inventing an edge for. A generator that
is not on the Operable sheet (retired, cancelled, proposed) gets a null `generator_capacity_mw`;
an archive with no generator member (the 60-row test fixture) gets both columns null and the
loader falls back to its unweighted mean. Written with plain `DataFrame.to_parquet`, not
`pipeline.connectors.base.to_parquet_safe`: every column is already a homogeneous Python type
and pyarrow infers `string`/`double`/`date32[day]` directly; `to_parquet_safe` would turn `as_of`
into text.

Measured against the real 2025 release (`xls/eia8602025.zip` -> `4___Owner_Y2025.xlsx`, 514,124
bytes; `3_1_Generator_Y2025.xlsx`, 11,223,392 bytes), 2026-09-19:
- 5,680 raw rows on the "Ownership" sheet, none without a Generator ID (the 2024 file had one).
- 1 row has a blank `Percent Owned`; `ownership_pct` is left null rather than dropping the row.
- 2,534 plants, 4,018 generators, 2,038 distinct owner strings; `Status` OP 4,819, RE 415.
- 2 generators whose listed shares sum over 100 % (plant 341 CT5 at 150 %, plant 70387 BESS1 at
  100.09 %); the loader flags these and does not normalise them.
`Percent Owned` in the source is a **fraction** (0-1: e.g. `0.6`, `1`), not a percentage; this
module multiplies by 100 to match the documented 0-100 range.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import re
import time
import zipfile
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.us_eia_860.connector import find_owner_member, parse_owner_sheet

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "data" / "snapshots" / "us.eia.860"
RUNS_DIR = ROOT / "data" / "runs" / "us.eia.860"
DEFAULT_OUT = ROOT / "data" / "normalized" / "context" / "us.eia.860.owners.parquet"

SOURCE_ID = "us.eia.860"
LICENCE = "public-domain"
ARCHIVE_URL_TEMPLATE = "https://www.eia.gov/electricity/data/eia860/archive/xls/eia860{year}.zip"

#: `3_1_Generator_Y2025.xlsx` -- Schedule 3 generator data; sheet "Operable" carries nameplate.
GENERATOR_MEMBER_RE = re.compile(r"^3_1_Generator_Y(\d{4})\.xlsx$", re.I)
GENERATOR_SHEET = "Operable"
NAMEPLATE_COLUMN = "Nameplate Capacity (MW)"

OUTPUT_COLUMNS: list[str] = [
    "source_plant_id",
    "generator_id",
    "owner_name",
    "ownership_pct",
    "generator_status",
    "generator_capacity_mw",
    "plant_capacity_mw",
    "as_of",
    "source_url",
    "retrieved_at",
    "licence",
]


def extract_owner_sheet(archive_bytes: bytes) -> tuple[pd.DataFrame, int]:
    """`(Ownership sheet, data year)` from an EIA-860 annual zip's `4___Owner_Y<year>.xlsx`."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        found = find_owner_member(zf.namelist())
        if found is None:
            raise ParseError(f"no 4___Owner_Y<year>.xlsx member among {zf.namelist()[:10]}")
        member, year = found
        content = zf.read(member)
    return parse_owner_sheet(content), year


def find_generator_member(names: list[str]) -> str | None:
    """The `3_1_Generator_Y<year>.xlsx` member name, or `None` if the archive lacks one."""
    for name in names:
        if GENERATOR_MEMBER_RE.match(pathlib.PurePosixPath(name).name):
            return name
    return None


def parse_generator_capacity(content: bytes) -> pd.DataFrame:
    """Sheet "Operable" of `3_1_Generator_Y<year>.xlsx` -> one row per operable generator:
    `source_plant_id`, `generator_id`, `generator_capacity_mw` (a reported 0/blank nameplate is
    null, the same reading `pipeline/context/eia_plants.py::capacity` uses)."""
    df = pd.read_excel(io.BytesIO(content), sheet_name=GENERATOR_SHEET, header=1, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in ("Plant Code", "Generator ID", NAMEPLATE_COLUMN) if c not in df.columns]
    if missing:
        raise ParseError(f"Generator sheet layout changed, missing {missing}: {list(df.columns)[:8]}")
    keep = df[df["Plant Code"].notna() & df["Generator ID"].notna()]
    mw = pd.to_numeric(keep[NAMEPLATE_COLUMN], errors="coerce")
    out = pd.DataFrame(
        {
            "source_plant_id": keep["Plant Code"].map(lambda v: str(int(v))),
            "generator_id": keep["Generator ID"].map(lambda v: str(v).strip()),
            "generator_capacity_mw": mw.where(mw > 0),
        }
    )
    return out.drop_duplicates(["source_plant_id", "generator_id"]).reset_index(drop=True)


def extract_generator_capacity(archive_bytes: bytes) -> pd.DataFrame | None:
    """Operable-generator nameplate from the zip's Schedule 3 member, or `None` when the archive
    has no such member (a trimmed fixture)."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        member = find_generator_member(zf.namelist())
        if member is None:
            return None
        content = zf.read(member)
    return parse_generator_capacity(content)


def build_owner_rows(
    df: pd.DataFrame,
    *,
    year: int,
    source_url: str,
    retrieved_at: str,
    generators: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Raw "Ownership" sheet rows -> the exact `OUTPUT_COLUMNS` schema (see module docstring).
    `generators` is `parse_generator_capacity`'s frame; when given, each owner row gets its
    generator's operable nameplate and its plant's operable total, both null otherwise."""
    keep = df[df["Plant Code"].notna() & df["Generator ID"].notna()].reset_index(drop=True)
    pct_fraction = pd.to_numeric(keep["Percent Owned"], errors="coerce")
    as_of = dt.date(year, 12, 31)
    status = (
        keep["Status"].map(lambda v: None if pd.isna(v) else str(v).strip() or None)
        if "Status" in keep.columns
        else pd.Series([None] * len(keep), dtype="object")
    )
    out = pd.DataFrame(
        {
            "source_plant_id": keep["Plant Code"].map(lambda v: str(int(v))),
            "generator_id": keep["Generator ID"].map(lambda v: str(v).strip()),
            "owner_name": keep["Owner Name"].map(lambda v: str(v).strip()),
            "ownership_pct": (pct_fraction * 100.0).round(3),
            "generator_status": status.astype("object"),
            "as_of": [as_of] * len(keep),
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "licence": LICENCE,
        }
    )
    if generators is not None and len(generators):
        plant_total = (
            generators.groupby("source_plant_id")["generator_capacity_mw"]
            .sum(min_count=1)
            .rename("plant_capacity_mw")
            .reset_index()
        )
        out = out.merge(generators, on=["source_plant_id", "generator_id"], how="left")
        out = out.merge(plant_total, on="source_plant_id", how="left")
    else:
        out["generator_capacity_mw"] = pd.Series([float("nan")] * len(out), dtype="float64")
        out["plant_capacity_mw"] = pd.Series([float("nan")] * len(out), dtype="float64")
    out["generator_capacity_mw"] = out["generator_capacity_mw"].astype("float64")
    out["plant_capacity_mw"] = out["plant_capacity_mw"].astype("float64")
    return out[OUTPUT_COLUMNS]


def _latest_snapshot() -> pathlib.Path:
    candidates = sorted(SNAPSHOT_DIR.glob("*.zip"))
    if not candidates:
        raise FileNotFoundError(f"no *.zip snapshot under {SNAPSHOT_DIR}")
    return candidates[-1]


def _token_to_iso(token: str) -> str | None:
    try:
        ts = dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_metadata(archive_path: pathlib.Path, year: int) -> tuple[str, str]:
    """`(retrieved_at_iso, source_url)`, the same convention as
    `pipeline/context/eia_plants.py::_snapshot_metadata`: a store snapshot named `<ts_token>.zip`
    gives `retrieved_at` from its token and the exact URL the run fetched from
    `data/runs/us.eia.860/<ts_token>.json` (`snapshot.fetched_url`); a browser-downloaded archive
    has neither, so `retrieved_at` is "now" and the URL the `archive/xls/` template for its year."""
    token = archive_path.stem
    retrieved_at = _token_to_iso(token) or dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    source_url: str | None = None
    run_path = RUNS_DIR / f"{token}.json"
    if run_path.exists():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
            source_url = ((record.get("snapshot") or {}).get("fetched_url")) or None
        except (json.JSONDecodeError, OSError):
            source_url = None
    return retrieved_at, source_url or ARCHIVE_URL_TEMPLATE.format(year=year)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--archive", type=pathlib.Path, help="Path to a browser-downloaded EIA-860 annual .zip"
    )
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Use the newest data/snapshots/us.eia.860/*.zip"
    )
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    archive_path = args.archive or _latest_snapshot()
    archive_bytes = archive_path.read_bytes()

    sheet, year = extract_owner_sheet(archive_bytes)
    generators = extract_generator_capacity(archive_bytes)
    retrieved_at, source_url = _snapshot_metadata(archive_path, year)
    owners = build_owner_rows(
        sheet, year=year, source_url=source_url, retrieved_at=retrieved_at, generators=generators
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    owners.to_parquet(args.out, index=False)

    with_pct = int(owners["ownership_pct"].notna().sum())
    per_generator = owners.groupby(["source_plant_id", "generator_id"])["ownership_pct"].sum(min_count=1)
    summary: dict[str, Any] = {
        "archive": str(archive_path),
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "archive_bytes": len(archive_bytes),
        "raw_rows": len(sheet),
        "owner_rows": len(owners),
        "dropped_no_generator_id": len(sheet) - len(owners),
        "with_ownership_pct": with_pct,
        "without_ownership_pct": len(owners) - with_pct,
        "with_generator_capacity_mw": int(owners["generator_capacity_mw"].notna().sum()),
        "generators_over_100_pct": int((per_generator > 100.05).sum()),
        "unique_plants": int(owners["source_plant_id"].nunique()),
        "unique_generators": int(per_generator.shape[0]),
        "unique_owner_names": int(owners["owner_name"].nunique()),
        "year": year,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
