"""Protected lands: USGS PAD-US 4.1, fee and designation features; GAP status 1-2 is the hard constraint.

Source: the PAD-US 4.1 file geodatabase on ScienceBase, national (``PADUS4_1Geodatabase.zip``, 1.52 GB,
built in GitHub Actions only) or per state (``PADUS4_1_State_TX_GDB_KMZ.zip``, 261 MB, for development),
through ScienceBase's catalogue download route
``https://www.sciencebase.gov/catalog/file/get/<item>?name=<file>`` (its file-manager links answer a
JavaScript shell; measured 2026-10-22). The USGS map service on edits.nationalmap.gov carries the Fee
layer only, so it cannot replace the geodatabase.

Read: the ``...Fee...`` and ``...Designation...`` feature classes (state GDB: ``PADUS4_1Fee_State_TX``,
``PADUS4_1Designation_State_TX``; national: ``PADUS4_1Fee``, ``PADUS4_1Designation``), in USA Contiguous
Albers Equal Area, reprojected to EPSG:4326 and simplified by `SIMPLIFY_DEG` (~5 m, under PAD-US's own
positional accuracy). Reading a geodatabase needs GDAL, so `read_gdb` uses pyogrio and pyproj, which
`.github/workflows/prospect-layers.yml` installs for the build only (the API never imports them);
everything after it (`normalise`, the GeoParquet write) is pure Python and tested with a recorded
GeoJSON fixture. Measured on the Texas geodatabase (2026-10-22): 11,864 fee and 80 designation features
read in 30 s, of which 309 fee features are curved (re-read linearised, `read_gdb`).

Fields kept: unit name, category (Fee or Designation), designation type, manager name and type, GAP
status, public access, and `hard_constraint` = GAP status 1 or 2 (managed for biodiversity: the site
finder excludes it). The decoded ``d_*`` text is used where the geodatabase carries it. Terms: public
domain (USGS; the ScienceBase item's rights statement, quoted in data/sources.yaml).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import struct
import tempfile
import time
from collections.abc import Iterable, Iterator
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.store import Store
from pipeline.context.geo import simplify
from pipeline.context.prospect.common import (
    REGION_STATE_CODES,
    REGIONS,
    FetchLog,
    download,
    extract_gdb,
    now_iso,
    output_dir,
    provenance_for,
    snapshot_retrieved_at,
    verify_terms,
)
from pipeline.context.prospect.coverage import for_state_filtered, public
from pipeline.context.prospect.geoparquet import write_geoparquet_features

SOURCE_ID = "us.usgs.padus"
LICENCE_NAME = "Public domain (USGS)"
LAYER_ID = "protected_lands"
VERSION = "4.1"
VINTAGE = "PAD-US 4.1 (revision of 2025-03-31)"
SCIENCEBASE = "https://www.sciencebase.gov/catalog"
NATIONAL_ITEM = "652d4fc5d34e44db0e2ee45e"
STATE_ITEM = "6759abcfd34edfeb8710a004"
TERMS_URL = f"{SCIENCEBASE}/item/65294599d34e44db0e2ed7cf?format=json"
TERMS_PHRASES = ("Public Domain.",)
#: ScienceBase serves the files from www.sciencebase.gov; one request a second, a few requests a build.
SCIENCEBASE_RPS = 1.0
SIMPLIFY_DEG = 0.00005
HARD_CONSTRAINT_GAP = frozenset({"1", "2"})
FEATURE_CLASSES = ("Fee", "Designation")

SCHEMA = [
    ("unit_name", pa.string()),
    ("category", pa.string()),
    ("feature_class", pa.string()),
    ("designation", pa.string()),
    ("manager", pa.string()),
    ("manager_type", pa.string()),
    ("owner_type", pa.string()),
    ("gap_status", pa.string()),
    ("gap_status_label", pa.string()),
    ("public_access", pa.string()),
    ("gis_acres", pa.float64()),
    ("hard_constraint", pa.bool_()),
]


def file_url(region: str) -> str:
    codes = REGION_STATE_CODES.get(region)
    if codes and len(codes) == 1:
        name = f"PADUS{VERSION.replace('.', '_')}_State_{codes[0]}_GDB_KMZ.zip"
        return f"{SCIENCEBASE}/file/get/{STATE_ITEM}?name={name}"
    return f"{SCIENCEBASE}/file/get/{NATIONAL_ITEM}?name=PADUS{VERSION.replace('.', '_')}Geodatabase.zip"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _num(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 2)
    except (TypeError, ValueError):
        return None


def gap_code(props: dict[str, Any]) -> str | None:
    raw = _text(props.get("GAP_Sts"))
    if raw is None:
        return None
    m = re.match(r"\s*(\d)", raw)
    return m.group(1) if m else None


def normalise(
    features: list[dict[str, Any]], feature_class: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """GeoJSON-like features (EPSG:4326) of one feature class -> (rows, geometries)."""
    rows, geoms = [], []
    for f in features:
        p, g = f.get("properties") or {}, f.get("geometry")
        if not g:
            continue
        gap = gap_code(p)
        rows.append(
            {
                "unit_name": _text(p.get("Unit_Nm")) or _text(p.get("Loc_Nm")),
                "category": _text(p.get("d_Category")) or _text(p.get("Category")),
                "feature_class": feature_class,
                "designation": _text(p.get("d_Des_Tp")) or _text(p.get("Des_Tp")),
                "manager": _text(p.get("d_Mang_Name")) or _text(p.get("Mang_Name")),
                "manager_type": _text(p.get("d_Mang_Type")) or _text(p.get("Mang_Type")),
                "owner_type": _text(p.get("d_Own_Type")) or _text(p.get("Own_Type")),
                "gap_status": gap,
                "gap_status_label": _text(p.get("d_GAP_Sts")),
                "public_access": _text(p.get("d_Pub_Access")) or _text(p.get("Pub_Access")),
                "gis_acres": _num(p.get("GIS_Acres")),
                "hard_constraint": gap in HARD_CONSTRAINT_GAP,
            }
        )
        geoms.append(g)
    return rows, geoms


def find_layers(layer_names: list[str]) -> dict[str, str]:
    """{feature class: layer name} for Fee and Designation in a PAD-US geodatabase."""
    out = {}
    for fc in FEATURE_CLASSES:
        pattern = re.compile(rf"^PADUS\d_\d{fc}(_State_[A-Z]{{2}})?$")
        match = next((n for n in layer_names if pattern.match(n)), None)
        if match is None:
            raise LookupError(f"no {fc} feature class among {layer_names}")
        out[fc] = match
    return out


#: The geodatabase columns `normalise` reads (raw codes and their decoded ``d_*`` text).
SOURCE_COLUMNS = (
    "Unit_Nm",
    "Loc_Nm",
    "Category",
    "d_Category",
    "Des_Tp",
    "d_Des_Tp",
    "Mang_Name",
    "d_Mang_Name",
    "Mang_Type",
    "d_Mang_Type",
    "Own_Type",
    "d_Own_Type",
    "GAP_Sts",
    "d_GAP_Sts",
    "Pub_Access",
    "d_Pub_Access",
    "GIS_Acres",
)
#: Features per Arrow batch: a national layer streams through in bounded memory.
BATCH_SIZE = 2_000


#: WKB base types of curved polygons (CurvePolygon, MultiSurface). GDAL's Arrow stream passes them
#: through as curves; pyogrio's row reader linearises them, so they are re-read that way by id.
CURVED_WKB_TYPES = frozenset({10, 12})
#: Curved features re-read per `pyogrio.raw.read(fids=...)` call.
CURVE_CHUNK = 500


def _wkb_base_type(wkb: bytes) -> int:
    (flags,) = struct.unpack_from("<I" if wkb[0] == 1 else ">I", wkb, 1)
    return int((flags & 0xFFFF) % 1000)


def read_gdb(
    gdb: pathlib.Path,
    layer: str,
    *,
    simplify_deg: float = SIMPLIFY_DEG,
    batch_size: int = BATCH_SIZE,
    counts: dict[str, int] | None = None,
    bbox_lonlat: tuple[float, float, float, float] | None = None,
) -> Iterator[dict[str, Any]]:
    """Features of one geodatabase layer as GeoJSON-like dicts in EPSG:4326 (pyogrio + pyproj), streamed
    in Arrow batches of `batch_size` with only `SOURCE_COLUMNS` read, so the national geodatabase never
    sits in memory whole.

    `bbox_lonlat` (the region box) is handed to GDAL in the layer's own CRS (the box's outline densified
    and reprojected, so it covers the region), so features outside it -- Alaska, Hawaii and the
    territories in the national file, for a CONUS build -- are never read, reprojected or simplified.
    The caller still tests each feature against the lon/lat box exactly.

    Curved features (309 of Texas's 11,864 fee features: arcs in park and lake outlines) arrive from the
    Arrow stream as curves; their ids are kept and they are re-read at the end through pyogrio's row
    reader, which returns them linearised. `counts`, when given, gets `features`, `curved_relinearised`
    and `undecodable` (a geometry type the WKB decoder does not take, skipped)."""
    import numpy as np
    import pyogrio  # type: ignore[import-not-found, import-untyped, unused-ignore]
    import pyogrio.raw  # type: ignore[import-not-found, import-untyped, unused-ignore]
    from pyproj import Transformer  # type: ignore[import-not-found, import-untyped, unused-ignore]

    from services.context_sample.vectors import WkbError, decode_wkb

    tally = counts if counts is not None else {}
    for key in ("features", "curved_relinearised", "undecodable"):
        tally.setdefault(key, 0)
    info = pyogrio.read_info(str(gdb), layer=layer)
    present = set(info["fields"])
    columns = [c for c in SOURCE_COLUMNS if c in present]
    layer_bbox = None
    if bbox_lonlat is not None:
        to_layer = Transformer.from_crs("EPSG:4326", info["crs"], always_xy=True)
        layer_bbox = tuple(float(v) for v in to_layer.transform_bounds(*bbox_lonlat, densify_pts=21))

    def feature(wkb: bytes, props: dict[str, Any], transformer: Any) -> dict[str, Any] | None:
        try:
            decoded = decode_wkb(wkb)
        except WkbError:
            tally["undecodable"] += 1
            return None
        polys = []
        for poly in decoded:
            rings = []
            for ring in poly:
                lon, lat = transformer.transform(ring[:, 0], ring[:, 1])
                pts = np.column_stack([lon, lat]).tolist()
                rings.append(simplify(pts, simplify_deg) if simplify_deg else pts)
            polys.append(rings)
        tally["features"] += 1
        return {"properties": props, "geometry": {"type": "MultiPolygon", "coordinates": polys}}

    curved: list[int] = []
    with pyogrio.raw.open_arrow(
        str(gdb),
        layer=layer,
        columns=columns,
        batch_size=batch_size,
        use_pyarrow=True,
        return_fids=True,
        bbox=layer_bbox,
    ) as (meta, reader):
        geometry_name = meta.get("geometry_name") or "wkb_geometry"
        fid_name = reader.schema.names[0]
        transformer = Transformer.from_crs(meta["crs"], "EPSG:4326", always_xy=True)
        for batch in reader:
            cols = batch.to_pydict()
            wkbs = cols.pop(geometry_name)
            fids = cols.pop(fid_name)
            for i, wkb in enumerate(wkbs):
                if wkb is None:
                    continue
                if _wkb_base_type(wkb) in CURVED_WKB_TYPES:
                    curved.append(int(fids[i]))
                    continue
                f = feature(bytes(wkb), {name: values[i] for name, values in cols.items()}, transformer)
                if f is not None:
                    yield f
    for start in range(0, len(curved), CURVE_CHUNK):
        chunk = curved[start : start + CURVE_CHUNK]
        meta, _fids, geometry, field_data = pyogrio.raw.read(
            str(gdb), layer=layer, fids=chunk, columns=columns
        )
        names = list(meta["fields"])
        transformer = Transformer.from_crs(meta["crs"], "EPSG:4326", always_xy=True)
        for i, wkb in enumerate(geometry):
            if wkb is None:
                continue
            props = {
                n: (v.item() if hasattr(v, "item") else v)
                for n, v in zip(names, (col[i] for col in field_data), strict=True)
            }
            f = feature(bytes(wkb), props, transformer)
            if f is not None:
                tally["curved_relinearised"] += 1
                yield f


def list_gdb_layers(gdb: pathlib.Path) -> list[str]:
    import pyogrio  # type: ignore[import-not-found, import-untyped, unused-ignore]

    return [str(name) for name, _ in pyogrio.list_layers(str(gdb))]


def _intersects(geom: dict[str, Any], bbox: tuple[float, float, float, float]) -> bool:
    xs = [p[0] for poly in geom["coordinates"] for p in poly[0]]
    ys = [p[1] for poly in geom["coordinates"] for p in poly[0]]
    return not (min(xs) > bbox[2] or max(xs) < bbox[0] or min(ys) > bbox[3] or max(ys) < bbox[1])


def build_from_features(
    features: Iterable[tuple[str, dict[str, Any]]],
    *,
    out_dir: pathlib.Path,
    source_url: str,
    retrieved_at: str,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`features`: (feature class, GeoJSON-like feature) pairs, consumed once and streamed into the
    GeoParquet writer (no list of every feature's coordinates is ever held)."""
    counts: dict[str, int] = {}
    hard = 0

    def pairs() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        nonlocal hard
        for fc, feature in features:
            rows, geoms = normalise([feature], fc)
            for row, geom in zip(rows, geoms, strict=True):
                counts[fc] = counts.get(fc, 0) + 1
                hard += int(bool(row["hard_constraint"]))
                yield row, geom

    prov = provenance_for(
        SOURCE_ID, source_url=source_url, retrieved_at=retrieved_at, licence=LICENCE_NAME, vintage=VINTAGE
    )
    out = out_dir / f"{LAYER_ID}.parquet"
    extra = {"coverage": coverage} if coverage else None
    summary = write_geoparquet_features(out, pairs(), schema=SCHEMA, provenance=prov, extra_metadata=extra)
    summary["by_feature_class"] = counts
    summary["hard_constraint_rows"] = hard
    output = {
        "layer_id": LAYER_ID,
        "file": out.name,
        "kind": "polygons",
        "provenance": prov.record(),
        "licence_name": LICENCE_NAME,
        "summary": summary,
    }
    if coverage:
        output["coverage"] = public(coverage)
    return output


def build(
    *,
    region: str = "texas",
    out_dir: pathlib.Path | None = None,
    snapshot: pathlib.Path | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
    work_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """`snapshot`: a PAD-US zip, an extracted `.gdb` directory, or a recorded `.geojson` FeatureCollection
    whose features carry a `feature_class` property (the test fixture). Otherwise fetch the region's zip,
    read it, and delete the raw files."""
    bbox = REGIONS[region]
    out_dir = output_dir(out_dir)
    url = file_url(region)
    retrieved_at = snapshot_retrieved_at(snapshot) if snapshot is not None else now_iso()
    if snapshot is not None and snapshot.suffix == ".geojson":
        doc = json.loads(snapshot.read_text())
        recorded = ((str(f["properties"].get("feature_class", "Fee")), f) for f in doc["features"])
        return [
            build_from_features(
                recorded,
                out_dir=out_dir,
                source_url=url,
                retrieved_at=retrieved_at,
                coverage=for_state_filtered(region),
            )
        ]
    log = FetchLog(SOURCE_ID)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmpdir = pathlib.Path(tmp)
        if snapshot is None:
            session = session or PoliteSession(rate_limits={"www.sciencebase.gov": SCIENCEBASE_RPS})
            verify_terms(session, TERMS_URL, TERMS_PHRASES, log=log, honour_robots=False)
            info = download(session, url, tmpdir / "padus.zip", log=log)
            retrieved_at = info["retrieved_at"]
            snapshot = tmpdir / "padus.zip"
        gdb = snapshot
        if snapshot.suffix == ".zip":
            gdb = extract_gdb(snapshot, tmpdir)
            if snapshot.parent == tmpdir:
                snapshot.unlink()
        layers = find_layers(list_gdb_layers(gdb))
        read_counts: dict[str, dict[str, int]] = {fc: {} for fc in layers}
        streamed = (
            (fc, f)
            for fc, name in layers.items()
            for f in read_gdb(gdb, name, counts=read_counts[fc], bbox_lonlat=bbox)
            if _intersects(f["geometry"], bbox)
        )
        output = build_from_features(
            streamed,
            out_dir=out_dir,
            source_url=url,
            retrieved_at=retrieved_at,
            coverage=for_state_filtered(region),
        )
        output["summary"]["read"] = read_counts
        if gdb.parent == tmpdir:
            shutil.rmtree(gdb)
    log.write(store, region=region, rows=output["summary"]["rows"])
    return [output]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument(
        "--snapshot", type=pathlib.Path, help="A PAD-US zip, .gdb directory or recorded .geojson"
    )
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(region=args.region, out_dir=args.out_dir, snapshot=args.snapshot, store=Store())
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
