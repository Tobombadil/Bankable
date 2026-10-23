"""Wind: Global Wind Atlas mean wind speed at 100 m (250 m grid), from a file a person downloaded.

Terms (Global Wind Atlas Terms of Use, quoted in data/sources.yaml, read 2026-10-22): "The Works are
licensed under the Creative Commons Attribution 4.0 International license, CC BY 4.0", with the
attribution text and citation the Terms request, and -- in the same Terms -- "you agree not to: ... Use
any robot, spider or other automatic device, process or means to access the GWA App for any purpose,
including monitoring or copying any of the material on the GWA App".

So the licence is open but the route is manual: **this module never contacts globalwindatlas.info.** A
person downloads the GIS layer (wind speed, 100 m; a country GeoTIFF such as the United States one) in a
browser, and the build reads that file: locally with ``--snapshot PATH``, in GitHub Actions from the
private bucket key ``prospect/inbox/global_wind_atlas/`` (`.github/workflows/prospect-layers.yml`).
`fetch` exists only to say so.

The GeoTIFF is expected as a lon/lat grid (EPSG:4326) of float32 m/s; `services.context_sample.geotiff`
reads it whatever its compression (LZW, Deflate, none) and tiling. Stored as uint16 at 0.01 m/s.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
from typing import Any

from pipeline.connectors.store import Store
from pipeline.context.prospect.common import REGIONS, output_dir, provenance_for, sha256_file
from pipeline.context.prospect.rasters import read_scaled, write_quantised
from services.context_sample.geotiff import GeoTiff, GeoTiffError

SOURCE_ID = "global.dtu.global_wind_atlas"
LAYER_ID = "wind_speed_100m"
LICENCE_NAME = "CC BY 4.0"
SOURCE_URL = "https://globalwindatlas.info"
SCALE = 0.01
#: Plausible bounds of a mean wind speed (m/s); anything outside is treated as no data.
VALID_RANGE = (0.0, 40.0)


class ManualSourceOnly(RuntimeError):
    """The Global Wind Atlas forbids automated access to its application: download by hand."""


def fetch(*_args: Any, **_kwargs: Any) -> None:
    raise ManualSourceOnly(
        "The Global Wind Atlas Terms of Use forbid automated access to the GWA App, so no connector fetches "
        "from globalwindatlas.info. Download the 100 m wind speed GeoTIFF in a browser and pass --snapshot, "
        "or upload it to prospect/inbox/global_wind_atlas/ in the private bucket for the Actions build."
    )


def build(
    *,
    region: str = "texas",
    out_dir: pathlib.Path | None = None,
    snapshot: pathlib.Path | None = None,
    downloaded_at: str | None = None,
    version: str = "4.0",
    store: Store | None = None,
) -> list[dict[str, Any]]:
    """Build the layer from a downloaded GeoTIFF. `downloaded_at` is when the person downloaded it (ISO
    8601); without it the file's modification time stands in, and the provenance says so."""
    if snapshot is None:
        fetch()
        return []
    out_dir = output_dir(out_dir)
    with GeoTiff(snapshot) as tif:
        if tif.epsg not in (None, 4326):
            raise GeoTiffError(f"{snapshot.name}: expected EPSG:4326, found EPSG:{tif.epsg}")
    grid, stats = read_scaled(snapshot, REGIONS[region], scale=SCALE, valid_range=VALID_RANGE)
    if downloaded_at is None:
        mtime = dt.datetime.fromtimestamp(snapshot.stat().st_mtime, dt.UTC)
        downloaded_at = mtime.isoformat(timespec="seconds").replace("+00:00", "Z")
        when = "file modification time (download date not given)"
    else:
        when = "download date given by the person who downloaded it"
    prov = provenance_for(
        SOURCE_ID,
        source_url=SOURCE_URL,
        retrieved_at=downloaded_at,
        licence=LICENCE_NAME,
        vintage=f"Global Wind Atlas version {version}, mean wind speed at 100 m (long-term mean)",
    )
    out = out_dir / f"{LAYER_ID}.tif"
    summary = write_quantised(
        grid,
        out,
        scale=SCALE,
        provenance=prov,
        stats=stats,
        extra_metadata={"downloaded_by_hand": "true", "retrieved_at_basis": when},
    )
    summary["snapshot_sha256"] = sha256_file(snapshot)
    summary["snapshot_bytes"] = snapshot.stat().st_size
    if store is not None:
        store.write_run(
            SOURCE_ID,
            dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            {"source_id": SOURCE_ID, "retrieved_at": downloaded_at, "manual_snapshot": summary},
        )
    return [
        {
            "layer_id": LAYER_ID,
            "file": out.name,
            "kind": "raster",
            "provenance": {**prov.record(), "retrieved_at_basis": when},
            "licence_name": LICENCE_NAME,
            "summary": summary,
        }
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument(
        "--snapshot", type=pathlib.Path, required=True, help="The GeoTIFF a person downloaded"
    )
    parser.add_argument("--downloaded-at", help="ISO 8601 date-time of the download")
    parser.add_argument("--version", default="4.0")
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(
        region=args.region,
        out_dir=args.out_dir,
        snapshot=args.snapshot,
        downloaded_at=args.downloaded_at,
        version=args.version,
        store=Store(),
    )
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
