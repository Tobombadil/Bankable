"""EIA-860 annual Schedule 2 ("Plant") -> the grid fields of each EIA plant (lane R1, 2026-10-06).

What a powered-land reader asks of a retired or retiring plant, beyond what EIA-860M carries: which
NERC region it is in, which utility's transmission or distribution system it connects to, and at
what voltage. Schedule 2 states all three per plant (`NERC Region`, `Transmission or Distribution
System Owner`, `Grid Voltage (kV)` / `Grid Voltage 2 (kV)` / `Grid Voltage 3 (kV)`), keyed by the
same `Plant Code` EIA-860M calls `Plant ID`, so it joins to `power_plant` assets with no matching.
`services/ingest/enrich.py::apply_eia860_plants` writes the result to `attributes["grid"]`.

Input: the EIA-860 annual zip the `us.eia.860` connector already snapshots (member
`2___Plant_Y<year>.xlsx`, sheet "Plant", header on the second row: row one is the title). Same
snapshot conventions as `pipeline/context/eia_owners.py` (`--latest-snapshot` or `--archive PATH`).

Measured on the 2025 final file (fetched 2026-09-19, 17,349 plant rows): grid voltage present on
17,208, NERC region on 17,096. Every EIA-860M plant in service or with a planned retirement is in
it (14,699 of 14,700; 237 of 237), but only 751 of the 1,772 plants EIA-860M lists as wholly retired,
because the annual file lists plants with a generator in the report year: a plant that retired
before 2025 is absent, so its grid fields stay unknown (docs/27 section R1.1).

A voltage of 0 kV is read as "not stated", not as a value. Reuse: US federal work, public domain.
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

from pipeline.connectors.base import ParseError, to_parquet_safe

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "data" / "snapshots" / "us.eia.860"
RUNS_DIR = ROOT / "data" / "runs" / "us.eia.860"
DEFAULT_OUT = ROOT / "data" / "normalized" / "context" / "us.eia.860.plants.parquet"

SOURCE_ID = "us.eia.860"
LICENCE = "public-domain"
FALLBACK_URL = "https://www.eia.gov/electricity/data/eia860/"
PLANT_MEMBER_RE = re.compile(r"^2___Plant_Y(\d{4})\.xlsx$", re.I)
PLANT_SHEET = "Plant"
VOLTAGE_COLUMNS = ("Grid Voltage (kV)", "Grid Voltage 2 (kV)", "Grid Voltage 3 (kV)")
REQUIRED = ("Plant Code", "NERC Region", "Grid Voltage (kV)", "Transmission or Distribution System Owner")


def find_plant_member(names: list[str]) -> tuple[str, int] | None:
    for name in names:
        m = PLANT_MEMBER_RE.match(pathlib.PurePosixPath(name).name)
        if m:
            return name, int(m.group(1))
    return None


def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _voltages(row: dict[str, Any]) -> list[float]:
    out: list[float] = []
    for col in VOLTAGE_COLUMNS:
        v = pd.to_numeric(row.get(col), errors="coerce")
        if pd.isna(v) or float(v) <= 0:
            continue
        kv = round(float(v), 1)
        if kv not in out:
            out.append(kv)
    return sorted(out, reverse=True)


def parse_plant_sheet(content: bytes) -> pd.DataFrame:
    df = pd.read_excel(io.BytesIO(content), sheet_name=PLANT_SHEET, header=1, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ParseError(f"EIA-860 Plant sheet layout changed; missing {missing}")
    return df[pd.to_numeric(df["Plant Code"], errors="coerce").notna()].reset_index(drop=True)


def build_plant_rows(sheet: pd.DataFrame, *, year: int, source_url: str, retrieved_at: str) -> pd.DataFrame:
    """One row per plant code with the grid fields `apply_eia860_plants` reads."""
    rows: list[dict[str, Any]] = []
    for record in sheet.to_dict("records"):
        row = {str(k): v for k, v in record.items()}
        rows.append(
            {
                "plant_id": str(int(float(row["Plant Code"]))),
                "report_year": year,
                "plant_name": _text(row.get("Plant Name")),
                "state": _text(row.get("State")),
                "nerc_region": _text(row.get("NERC Region")),
                "balancing_authority_code": _text(row.get("Balancing Authority Code")),
                "balancing_authority_name": _text(row.get("Balancing Authority Name")),
                "transmission_owner": _text(row.get("Transmission or Distribution System Owner")),
                "transmission_owner_id": _text(row.get("Transmission or Distribution System Owner ID")),
                "grid_voltage_kv": _voltages(row),
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "licence": LICENCE,
            }
        )
    return pd.DataFrame(rows)


def extract_plant_rows(archive_bytes: bytes, *, source_url: str, retrieved_at: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
        found = find_plant_member(z.namelist())
        if found is None:
            raise ParseError("EIA-860 zip has no 2___Plant_Y<year>.xlsx member")
        member, year = found
        sheet = parse_plant_sheet(z.read(member))
    return build_plant_rows(sheet, year=year, source_url=source_url, retrieved_at=retrieved_at)


def _latest_snapshot() -> pathlib.Path:
    candidates = sorted(SNAPSHOT_DIR.glob("*.zip"))
    if not candidates:
        raise FileNotFoundError(f"no *.zip snapshot under {SNAPSHOT_DIR}")
    return candidates[-1]


def _snapshot_metadata(archive_path: pathlib.Path) -> tuple[str, str]:
    """`(retrieved_at, source_url)` from a store snapshot's token and its run record, as
    `pipeline/context/eia_owners.py::_snapshot_metadata` does; "now" and the index page otherwise."""
    token = archive_path.stem
    try:
        ts = dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        ts = dt.datetime.now(dt.UTC)
    retrieved_at = ts.isoformat(timespec="seconds").replace("+00:00", "Z")
    run_path = RUNS_DIR / f"{token}.json"
    url: str | None = None
    if run_path.exists():
        try:
            url = (json.loads(run_path.read_text(encoding="utf-8")).get("snapshot") or {}).get("fetched_url")
        except (json.JSONDecodeError, OSError):
            url = None
    return retrieved_at, url or FALLBACK_URL


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--archive", type=pathlib.Path, help="Path to an EIA-860 annual .zip")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Newest data/snapshots/us.eia.860/*.zip"
    )
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    path = args.archive or _latest_snapshot()
    retrieved_at, source_url = _snapshot_metadata(path)
    plants = extract_plant_rows(path.read_bytes(), source_url=source_url, retrieved_at=retrieved_at)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(plants).to_parquet(args.out, index=False)
    summary = {
        "archive": str(path),
        "plants": len(plants),
        "with_grid_voltage": int((plants["grid_voltage_kv"].map(len) > 0).sum()),
        "with_nerc_region": int(plants["nerc_region"].notna().sum()),
        "with_transmission_owner": int(plants["transmission_owner"].notna().sum()),
        "report_year": int(plants["report_year"].iloc[0]) if len(plants) else None,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
