"""Seismic hazard: USGS 2023 NSHM (revised, CONUS.2023.R2), PGA with a 2% chance of exceedance in 50 years.

Source: "1.2 CONUS 2023.R2 hazard curves and uniform-hazard ground motion values" (ScienceBase item
691dfb9ad4be02142cbce6d8, published 2026-08-10, CC0), one zip per NEHRP site class. The layer uses site
class BC (Vs30 760 m/s), the reference condition of the national hazard maps, and from that 1.9 GB zip
reads only ``.../vs30-760/PGA/map.csv`` -- uniform-hazard PGA (g) at the 475-, 975- and 2,475-year return
periods on a 0.05 degree grid -- by HTTP range requests (`remote_zip.RemoteZip`), so nothing large is
downloaded, here or in Actions.

The grid points become the centres of 0.05 degree cells, so a sample returns the value of the nearest grid
node (USGS's own tools interpolate between nodes; at 5.5 km spacing the difference is small away from
faults, and the served sentence says this is not a site value). Stored as uint16 at 0.0001 g.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import pathlib
import time
from typing import Any

import numpy as np

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.store import Store
from pipeline.context.prospect.cog import Grid
from pipeline.context.prospect.common import (
    REGIONS,
    FetchLog,
    now_iso,
    output_dir,
    provenance_for,
    snapshot_time,
    verify_terms,
)
from pipeline.context.prospect.rasters import write_scaled
from pipeline.context.prospect.remote_zip import RemoteZip

SOURCE_ID = "us.usgs.nshm2023"
LAYER_ID = "seismic_pga_2pct50"
LICENCE_NAME = "CC0 1.0 (USGS)"
ITEM = "691dfb9ad4be02142cbce6d8"
ZIP_URL = (
    f"https://prod-is-usgs-sb-prod-publish.s3.amazonaws.com/{ITEM}/conus_2023.R2-MPRS-scBC-vs760-0p05.zip"
)
MEMBER = "conus_2023.R2-MPRS-scBC-vs760-0p05/vs30-760/PGA/map.csv"
TERMS_URL = "https://www.sciencebase.gov/catalog/item/67f8202ad4be024f83a3a018?format=json"
TERMS_PHRASES = ("Creative Commons Zero v1.0 Universal (CC0-1.0) public domain dedication",)
VINTAGE = "2023 NSHM, revision CONUS.2023.R2 (data release 2026-08-10), site class BC (Vs30 760 m/s)"
SPACING = 0.05
SCALE = 0.0001
#: The map.csv column for a 2% probability of exceedance in 50 years.
RETURN_PERIOD = "2475"


def parse_map_csv(content: bytes, *, column: str = RETURN_PERIOD) -> tuple[np.ndarray[Any, Any], ...]:
    """map.csv -> (lons, lats, values) for the chosen return-period column."""
    reader = csv.reader(io.StringIO(content.decode("utf-8")))
    header = [h.strip().lower() for h in next(reader)]
    try:
        ilon, ilat = header.index("lon"), header.index("lat")
    except ValueError as exc:
        raise ParseError(f"map.csv header has no lon/lat: {header}") from exc

    def period(h: str) -> float | None:
        try:
            return float(h.replace("-year", "").replace("yr", "").strip())
        except ValueError:
            return None

    want = next((i for i, h in enumerate(header) if period(h) == float(column)), None)
    if want is None:
        raise ParseError(f"map.csv has no {column}-year column: {header}")
    lons, lats, vals = [], [], []
    for row in reader:
        if len(row) <= max(ilon, ilat, want):
            continue
        try:
            lons.append(float(row[ilon]))
            lats.append(float(row[ilat]))
            vals.append(float(row[want]))
        except ValueError:
            continue
    if not lons:
        raise ParseError("map.csv has no rows")
    return np.array(lons), np.array(lats), np.array(vals)


def to_grid(
    lons: np.ndarray[Any, Any],
    lats: np.ndarray[Any, Any],
    vals: np.ndarray[Any, Any],
    *,
    spacing: float = SPACING,
) -> Grid:
    """Scattered grid nodes -> a full north-up grid whose cell centres are the nodes (NaN where absent)."""
    lon0, lat1 = float(lons.min()), float(lats.max())
    cols = np.rint((lons - lon0) / spacing).astype(int)
    rows = np.rint((lat1 - lats) / spacing).astype(int)
    data = np.full((rows.max() + 1, cols.max() + 1), np.nan)
    data[rows, cols] = vals
    return Grid(data=data, west=lon0 - spacing / 2, north=lat1 + spacing / 2, dx=spacing, dy=spacing)


def build_from_csv(
    content: bytes, *, region: str, out_dir: pathlib.Path, retrieved_at: str
) -> list[dict[str, Any]]:
    lons, lats, vals = parse_map_csv(content)
    grid = to_grid(lons, lats, vals).clip(REGIONS[region])
    prov = provenance_for(
        SOURCE_ID,
        source_url=f"{ZIP_URL}#{MEMBER}",
        retrieved_at=retrieved_at,
        licence=LICENCE_NAME,
        vintage=VINTAGE,
    )
    out = out_dir / f"{LAYER_ID}.tif"
    summary = write_scaled(
        grid, out, scale=SCALE, provenance=prov, extra_metadata={"return_period_years": RETURN_PERIOD}
    )
    summary["grid_nodes_in_file"] = int(lons.size)
    return [
        {
            "layer_id": LAYER_ID,
            "file": out.name,
            "kind": "raster",
            "provenance": prov.record(),
            "licence_name": LICENCE_NAME,
            "summary": summary,
        }
    ]


def build(
    *,
    region: str = "texas",
    out_dir: pathlib.Path | None = None,
    snapshot: pathlib.Path | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    out_dir = output_dir(out_dir)
    if snapshot is not None:
        return build_from_csv(
            snapshot.read_bytes(), region=region, out_dir=out_dir, retrieved_at=snapshot_time(snapshot)
        )
    session = session or PoliteSession(
        rate_limits={"prod-is-usgs-sb-prod-publish.s3.amazonaws.com": 2.0, "www.sciencebase.gov": 1.0}
    )
    log = FetchLog(SOURCE_ID)
    verify_terms(session, TERMS_URL, TERMS_PHRASES, log=log, honour_robots=False)
    rz = RemoteZip(session, ZIP_URL, log=log).open()
    content = rz.read(MEMBER)
    if store is not None:
        import datetime as dt

        from pipeline.connectors.store import ts_token

        store.write_snapshot(SOURCE_ID, ts_token(dt.datetime.now(dt.UTC)), "csv", content)
    outputs = build_from_csv(content, region=region, out_dir=out_dir, retrieved_at=now_iso())
    log.write(
        store,
        region=region,
        member=MEMBER,
        member_bytes=len(content),
        zip_bytes=rz.size,
        last_modified=rz.last_modified,
    )
    return outputs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument("--snapshot", type=pathlib.Path, help="A map.csv already extracted")
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(region=args.region, out_dir=args.out_dir, snapshot=args.snapshot, store=Store())
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
