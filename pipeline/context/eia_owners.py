"""EIA-860 Schedule 4 "Ownership" sheet -> the ownership-graph parquet (docs/21 §3.23,
`docs/adr/0008-assets-first-class-and-placement-grades.md` decision 2).

Same split as `pipeline/context/eia_plants.py`: `pipeline/connectors/us_eia_860/connector.py`
owns the shared parsers (`find_owner_member`, `parse_owner_sheet`) and the crawl/health-check
path; this CLI is the actual producer of the ownership rows `asset_owner` loads from, because
the real annual archive sits under `archive/xls/`, a path eia.gov's robots.txt disallows and the
connector must never request (module docstring of `us_eia_860/connector.py`). A human downloads
the zip in a browser; this CLI reads it.

`--archive PATH` takes that zip directly. `--latest-snapshot` picks the newest
`data/snapshots/us.eia.860/*.zip` -- present only if a future run stores one there by some other
route (e.g. a licensed feed); today's crawler cannot populate that directory itself (see above),
so this flag exists for parity with `eia_plants.py` and for whenever that changes.

Output columns exactly: `source_plant_id` (str), `generator_id` (str), `owner_name` (str),
`ownership_pct` (float, 0-100), `as_of` (date, December 31 of the data year -- EIA-860 is an
annual snapshot with no finer-grained date), `source_url`, `retrieved_at` (ISO 8601 UTC),
`licence` ("public-domain"). Written with plain `DataFrame.to_parquet`, not
`pipeline.connectors.base.to_parquet_safe`: every column here is already a homogeneous Python
type (str/float/date), and pyarrow infers `string`/`double`/`date32[day]` from it directly
(verified 2026-09-18) -- `to_parquet_safe`'s stringify-everything pass would turn `as_of` into
text, which the schema above does not want.

Row filtering, measured against the real 2024 archive (`archive/xls/eia8602024.zip`, 22,100,342
bytes -> `4___Owner_Y2024.xlsx`, 495,686 bytes), 2026-09-18:
- 5,496 raw rows on the "Ownership" sheet.
- 1 row has no Generator ID at all (Cranberry Point Energy Storage, plant 62844) and is dropped:
  `generator_id` is a required, non-null string in the output schema.
- 2 rows have a blank (whitespace) `Percent Owned` cell; `ownership_pct` is left null for those
  rows rather than dropping them -- the plant/generator/owner identity is still real and usable
  for the ownership graph, just without a share percentage that period.
- -> 5,495 owner rows out of the 5,496 raw rows (measured 2026-09-18).
`Percent Owned` in the source is a **fraction** (0-1: e.g. `0.6`, `1`), not a percentage; this
module multiplies by 100 to match the documented 0-100 range.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import time
import zipfile
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.us_eia_860.connector import find_owner_member, parse_owner_sheet

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "data" / "snapshots" / "us.eia.860"
DEFAULT_OUT = ROOT / "data" / "normalized" / "context" / "us.eia.860.owners.parquet"

SOURCE_ID = "us.eia.860"
LICENCE = "public-domain"
ARCHIVE_URL_TEMPLATE = "https://www.eia.gov/electricity/data/eia860/archive/xls/eia860{year}.zip"

OUTPUT_COLUMNS: list[str] = [
    "source_plant_id",
    "generator_id",
    "owner_name",
    "ownership_pct",
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


def build_owner_rows(
    df: pd.DataFrame,
    *,
    year: int,
    source_url: str,
    retrieved_at: str,
) -> pd.DataFrame:
    """Raw "Ownership" sheet rows -> the exact `OUTPUT_COLUMNS` schema (see module docstring)."""
    keep = df[df["Plant Code"].notna() & df["Generator ID"].notna()].reset_index(drop=True)
    pct_fraction = pd.to_numeric(keep["Percent Owned"], errors="coerce")
    as_of = dt.date(year, 12, 31)
    out = pd.DataFrame(
        {
            "source_plant_id": keep["Plant Code"].map(lambda v: str(int(v))),
            "generator_id": keep["Generator ID"].map(lambda v: str(v).strip()),
            "owner_name": keep["Owner Name"].map(lambda v: str(v).strip()),
            "ownership_pct": (pct_fraction * 100.0).round(3),
            "as_of": [as_of] * len(keep),
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "licence": LICENCE,
        }
    )
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


def _retrieved_at_for(archive_path: pathlib.Path) -> str:
    """The store's `<ts_token>.zip` naming gives an exact `retrieved_at`; a manually downloaded
    archive (the normal case -- see module docstring) has no such token, so this falls back to
    "now", the same convention `pipeline/context/eia_plants.py::_snapshot_metadata` uses for a
    workbook that isn't one of its own store's snapshots."""
    return _token_to_iso(archive_path.stem) or dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


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
    retrieved_at = _retrieved_at_for(archive_path)

    sheet, year = extract_owner_sheet(archive_path.read_bytes())
    source_url = ARCHIVE_URL_TEMPLATE.format(year=year)
    owners = build_owner_rows(sheet, year=year, source_url=source_url, retrieved_at=retrieved_at)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    owners.to_parquet(args.out, index=False)

    with_pct = int(owners["ownership_pct"].notna().sum())
    summary: dict[str, Any] = {
        "raw_rows": len(sheet),
        "owner_rows": len(owners),
        "dropped_no_generator_id": len(sheet) - len(owners),
        "with_ownership_pct": with_pct,
        "without_ownership_pct": len(owners) - with_pct,
        "unique_plants": int(owners["source_plant_id"].nunique()),
        "year": year,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
