"""EIA-923 annual generation and fuel -> per-plant operating features (owner decision 2026-09-18:
"capacity factor and heat rate derived from EIA-923", objective features only; ADR 0008 §4).

    fetch()            -> FetchResult    network; snapshot + run record under data/snapshots|runs
    parse(bytes)       -> DataFrame      the "Page 1 Generation and Fuel Data" sheet, no network
    aggregate(frame)   -> DataFrame      one row per Plant Id, written to parquet

`Plant Id` is the same plant key EIA-860M uses, so a row joins to a `power_plant` asset on
`source_asset_id` with no name matching at all (`services/ingest/enrich.py`).

**Which file (measured 2026-09-19).** `https://www.eia.gov/electricity/data/eia923/` links one zip
per year; everything under `archive/` is disallowed by eia.gov's robots.txt (`Disallow: /*archive/`),
and the two years outside it are `xls/f923_2025.zip` and `xls/f923_2026.zip`. The page states
"Annual release date: September 14, 2026; Final release 2025 data" and "All data prior to 2025 are
final", so 2026 is an early release of partial-year monthly data. `find_annual_zip` therefore takes
the newest non-archive year and `parse` requires the workbook member to be a `_Final` one, falling
back to the previous year otherwise -- the connector never derives a capacity factor from a
partial year by accident.

**What is derived here and what is not.** This module emits only what the form states, summed over
every fuel and prime-mover row of a plant: annual net generation (MWh), total fuel consumption
(MMBtu) and the electric-only fuel consumption (MMBtu). Capacity factor and heat rate need the
asset's nameplate capacity, which lives on the `asset` row, so they are computed at enrichment
(`services/ingest/enrich.py`), where the implausible-value flags are applied too.

Note on sign: EIA-923 net generation is negative for plants that consumed more than they produced
over the year (1,019 of 14,045 plants in the 2025 final file are <= 0). Nothing is clamped here;
the enrichment records a flag instead.

CLI: `python -m pipeline.context.eia923 [--snapshot PATH] [--year YYYY]`.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any

import openpyxl
import pandas as pd

from pipeline.connectors.base import ParseError, json_default
from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.connectors.store import Store, ts_token
from pipeline.context import fuels

SOURCE_ID = "us.eia.form923"
INDEX_URL = "https://www.eia.gov/electricity/data/eia923/"
BASE_URL = "https://www.eia.gov/electricity/data/eia923/"

RATE_LIMITS: dict[str, float] = {"www.eia.gov": 0.5}

#: `xls/f923_<year>.zip`, the non-archive links on the index page. `archive/xls/...` is excluded
#: deliberately: eia.gov's robots.txt disallows `/*archive/` (checked live 2026-09-19).
ZIP_LINK_RE = re.compile(r'href="(xls/f923_(\d{4})\.zip)"', re.IGNORECASE)
#: The generation-and-fuel workbook inside the zip (schedules 2, 3, 4, 5).
GENERATION_MEMBER_RE = re.compile(r"EIA923_Schedules_2_3_4_5.*\.xlsx$", re.IGNORECASE)
SHEET = "Page 1 Generation and Fuel Data"
#: Five title rows precede the header in the EIA-923 workbooks (measured on the 2025 final file).
HEADER_ROW = 6

PLANT_ID = "Plant Id"
NET_GENERATION = "Net Generation (Megawatthours)"
TOTAL_FUEL = "Total Fuel Consumption MMBtu"
ELEC_FUEL = "Elec Fuel Consumption MMBtu"

COLUMNS: list[str] = [
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "plant_id",
    "plant_name",
    "plant_state",
    "operator_name",
    "data_year",
    "net_generation_mwh",
    "total_fuel_mmbtu",
    "elec_fuel_mmbtu",
    "fuel_rows",
    "fuel_types",
]


# --------------------------------------------------------------------------------------- fetch
@dataclass
class FetchResult:
    source_url: str
    retrieved_at: str
    content: bytes
    sha256: str
    year: int
    last_modified: str | None = None
    snapshot_path: pathlib.Path | None = None
    run_path: pathlib.Path | None = None

    @property
    def bytes_(self) -> int:
        return len(self.content)


def polite_session() -> PoliteSession:
    return PoliteSession(rate_limits=RATE_LIMITS, default_rps=0.5, timeout=180.0)


def annual_zips(index_html: bytes) -> dict[int, str]:
    """Every annual zip the index page links outside `archive/`, by report year."""
    found = {
        int(year): f"{BASE_URL}{href}"
        for href, year in ZIP_LINK_RE.findall(index_html.decode("utf-8", "replace"))
    }
    if not found:
        raise ParseError("no xls/f923_<year>.zip link on the EIA-923 index page")
    return found


def find_annual_zip(index_html: bytes) -> tuple[str, int]:
    """`(url, year)` of the newest annual zip the index page links outside `archive/`."""
    found = annual_zips(index_html)
    year = max(found)
    return found[year], year


def fetch(
    *,
    year: int | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
    write: bool = True,
) -> FetchResult:
    """Index page -> the newest annual zip (or `year`). Snapshot + run record unless `write=False`."""
    session = session or polite_session()
    store = store or Store()
    if year is None:
        index = session.get(INDEX_URL, honour_robots=True)
        if index.status_code != 200:
            raise HttpFailed(f"GET {INDEX_URL} -> HTTP {index.status_code}")
        url, year = find_annual_zip(index.content)
    else:
        url = f"{BASE_URL}xls/f923_{year}.zip"
    resp = session.get(url, honour_robots=True)
    if resp.status_code != 200 or resp.content[:2] != b"PK":
        raise HttpFailed(
            f"{url} answered HTTP {resp.status_code} ({resp.headers.get('Content-Type')}), not a zip"
        )
    content: bytes = resp.content
    retrieved_at = fuels.iso(fuels.utc_now())
    result = FetchResult(
        source_url=url,
        retrieved_at=retrieved_at,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        year=year,
        last_modified=resp.headers.get("Last-Modified"),
    )
    if write:
        token = ts_token(fuels.utc_now())
        result.snapshot_path = store.write_snapshot(SOURCE_ID, token, "zip", content)
        result.run_path = store.write_run(
            SOURCE_ID,
            token,
            {
                "id": f"{SOURCE_ID}:{token}",
                "source_id": SOURCE_ID,
                "kind": "feature",
                "status": "ok",
                "retrieved_at": retrieved_at,
                "bytes": len(content),
                "snapshot": {
                    "fetched_url": url,
                    "byte_size": len(content),
                    "sha256": result.sha256,
                    "last_modified": result.last_modified,
                    "path": str(result.snapshot_path.relative_to(store.root)),
                },
                "requests_made": session.requests_made,
            },
        )
    return result


# --------------------------------------------------------------------------------------- parse
def _member(zf: zipfile.ZipFile, *, require_final: bool) -> str:
    members = [n for n in zf.namelist() if GENERATION_MEMBER_RE.search(n)]
    if not members:
        raise ParseError(f"no EIA923_Schedules_2_3_4_5 member (members: {zf.namelist()[:5]})")
    finals = [n for n in members if "final" in n.lower()]
    if require_final and not finals:
        raise ParseError(
            f"{members[0]} is not a final release; EIA's early-release file covers a partial year"
        )
    return (finals or members)[0]


def parse(content: bytes, *, require_final: bool = True) -> pd.DataFrame:
    """The generation-and-fuel sheet as a frame, one row per plant/prime-mover/fuel combination."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = _member(zf, require_final=require_final)
        raw = zf.read(name)
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        if SHEET not in book.sheetnames:
            raise ParseError(f"workbook {name} has no sheet {SHEET!r} (sheets: {book.sheetnames})")
        rows = book[SHEET].iter_rows(min_row=HEADER_ROW, values_only=True)
        try:
            header = [re.sub(r"\s+", " ", str(h)).strip() if h is not None else "" for h in next(rows)]
        except StopIteration as e:
            raise ParseError(f"sheet {SHEET!r} is empty") from e
        missing = {PLANT_ID, NET_GENERATION, TOTAL_FUEL} - set(header)
        if missing:
            raise ParseError(f"{SHEET!r} header lacks {sorted(missing)} (got {header[:6]})")
        body = [r for r in rows if any(v is not None for v in r)]
    finally:
        book.close()
    frame = pd.DataFrame(body, columns=header)
    frame.attrs["member"] = name
    return frame


def aggregate(
    frame: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str,
    licence_id: str = "",
    year: int | None = None,
) -> pd.DataFrame:
    """One row per `Plant Id`: net generation and fuel consumption summed over every fuel and
    prime-mover row the plant reported."""
    if not len(frame):
        return pd.DataFrame(columns=COLUMNS)
    work = frame.copy()
    work["plant_id"] = work[PLANT_ID].map(_plant_id)
    work = work[work["plant_id"].notna()]
    for column, out in ((NET_GENERATION, "net"), (TOTAL_FUEL, "fuel"), (ELEC_FUEL, "elec")):
        work[out] = pd.to_numeric(work.get(column), errors="coerce") if column in work else pd.NA
    data_year = year
    if data_year is None and "YEAR" in work.columns:
        years = pd.to_numeric(work["YEAR"], errors="coerce").dropna()
        data_year = int(years.mode().iloc[0]) if len(years) else None

    rows: list[dict[str, Any]] = []
    for plant_id, group in work.groupby("plant_id", dropna=True):
        fuel_types = sorted(
            {t for t in group.get("Reported Fuel Type Code", pd.Series(dtype=str)).map(fuels.clean_str) if t}
        )
        rows.append(
            {
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "licence": fuels.LICENCE_CLASS,
                "licence_id": licence_id,
                "plant_id": str(plant_id),
                "plant_name": fuels.clean_str(group.get("Plant Name", pd.Series(dtype=str)).iloc[0])
                if "Plant Name" in group
                else None,
                "plant_state": fuels.clean_str(group.get("Plant State", pd.Series(dtype=str)).iloc[0])
                if "Plant State" in group
                else None,
                "operator_name": fuels.clean_str(group.get("Operator Name", pd.Series(dtype=str)).iloc[0])
                if "Operator Name" in group
                else None,
                "data_year": data_year,
                "net_generation_mwh": _sum(group["net"]),
                "total_fuel_mmbtu": _sum(group["fuel"]),
                "elec_fuel_mmbtu": _sum(group["elec"]),
                "fuel_rows": len(group),
                "fuel_types": fuel_types,
            }
        )
    out = pd.DataFrame(rows, columns=COLUMNS)
    if len(out) and out["plant_id"].duplicated().any():
        dups = sorted(out.loc[out["plant_id"].duplicated(), "plant_id"].unique())
        raise ValueError(f"duplicate plant_id: {dups[:5]}")
    return out


def _plant_id(value: Any) -> str | None:
    text = fuels.clean_str(value)
    if text is None:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text if re.fullmatch(r"\d+", text) else None


def _sum(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return None if not len(values) else round(float(values.sum()), 3)


def default_output() -> pathlib.Path:
    return fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"


# ----------------------------------------------------------------------------------------- run
def run(
    *,
    snapshot: pathlib.Path | None = None,
    year: int | None = None,
    out: pathlib.Path | None = None,
    store: Store | None = None,
    manifest: pathlib.Path | None = None,
    require_final: bool = True,
) -> dict[str, Any]:
    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, manifest)
    store = store or Store()
    meta: dict[str, Any] = {}
    if snapshot is not None:
        content = snapshot.read_bytes()
        retrieved_at, source_url = fuels.snapshot_metadata(snapshot, SOURCE_ID, INDEX_URL, store)
        meta = {"snapshot": str(snapshot)}
        frame = parse(content, require_final=require_final)
    else:
        # Newest year first, stepping back to the previous year when the newest is still an early
        # release (EIA publishes the current year monthly, final only in the following September).
        session = polite_session()
        candidates: list[int | None] = [year]
        if year is None:
            index = session.get(INDEX_URL, honour_robots=True)
            if index.status_code != 200:
                raise HttpFailed(f"GET {INDEX_URL} -> HTTP {index.status_code}")
            candidates = sorted(annual_zips(index.content), reverse=True)  # type: ignore[assignment]
        skipped: list[str] = []
        frame = pd.DataFrame()
        retrieved_at = source_url = ""
        for candidate in candidates:
            fetched = fetch(year=candidate, session=session, store=store)
            try:
                frame = parse(fetched.content, require_final=require_final)
            except ParseError as e:
                if "not a final release" not in str(e):
                    raise
                skipped.append(f"{fetched.year}: {e}")
                continue
            year = fetched.year
            retrieved_at, source_url = fetched.retrieved_at, fetched.source_url
            meta = {
                "fetched_url": fetched.source_url,
                "bytes": fetched.bytes_,
                "sha256": fetched.sha256,
                "last_modified": fetched.last_modified,
                "skipped_early_release": skipped,
            }
            break
        else:
            raise ParseError(f"no final EIA-923 annual file among {candidates}: {skipped}")
    features = aggregate(
        frame, retrieved_at=retrieved_at, source_url=source_url, licence_id=entry.licence_id, year=year
    )
    out = out or default_output()
    fuels.write_context_parquet(features, out)
    thermal = features["total_fuel_mmbtu"].fillna(0) > 0
    return {
        "source_id": SOURCE_ID,
        "member": frame.attrs.get("member"),
        "sheet_rows": len(frame),
        "plants": len(features),
        "thermal_plants": thermal.sum(),
        "data_year": (
            int(features["data_year"].iloc[0]) if len(features) and features["data_year"].iloc[0] else None
        ),
        "net_generation_mwh_total": round(float(features["net_generation_mwh"].fillna(0).sum()), 1),
        "retrieved_at": retrieved_at,
        "out": str(out),
        "parquet_bytes": out.stat().st_size,
        "elapsed_s": round(time.monotonic() - t0, 2),
        **meta,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot", type=pathlib.Path, help="Parse this recorded zip")
    parser.add_argument("--year", type=int, help="Fetch this report year instead of the newest")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    summary = run(snapshot=args.snapshot, year=args.year, out=args.out)
    print(json.dumps(summary, default=json_default))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
