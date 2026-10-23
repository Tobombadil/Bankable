"""Water stress: WRI Aqueduct 4.0 baseline annual water stress per HydroBASINS level 6 sub-basin.

Source: ``https://files.wri.org/aqueduct/aqueduct-4-0-water-risk-data.zip`` (261 MB, Last-Modified
2023-08-07), file geodatabase ``Aq40_Y2023D07M05.gdb``, layer ``baseline_annual`` (68,506 features: each
HydroBASINS level 6 sub-basin split by first-level administrative unit; EPSG:4326). Fields kept:
``bws_raw`` (withdrawals over available renewable supply), ``bws_score`` (0-5), ``bws_cat`` (-1 arid and
low water use, 0-4), ``bws_label``, ``pfaf_id`` (the sub-basin), ``name_0``/``name_1`` (country, state).
Baseline period 1979-2019.

Terms: the dataset page says "License: Creative Commons"; WRI's Open Data Commitment adopts CC BY 4.0 as
its standard for data (both quoted in data/sources.yaml; the version is inferred, docs/13 §6). The
README's own cautions travel with the layer: "Aqueduct risk indicators cannot be measured directly and
therefore have not been validated" and "Aqueduct does not model interbasin transfers".

Reading the geodatabase needs GDAL (pyogrio, a build requirement); `normalise` and the write are pure and
tested with a recorded fixture.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.store import Store
from pipeline.context.geo import simplify
from pipeline.context.prospect.common import (
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
from pipeline.context.prospect.coverage import public, region_box
from pipeline.context.prospect.geoparquet import write_geoparquet

SOURCE_ID = "global.wri.aqueduct40"
LAYER_ID = "water_stress"
LICENCE_NAME = "CC BY 4.0 (WRI standard licence; the dataset page says Creative Commons)"
URL = "https://files.wri.org/aqueduct/aqueduct-4-0-water-risk-data.zip"
TERMS_URL = "https://www.wri.org/data/open-data-commitment"
TERMS_PHRASES = (
    "We have adopted the Creative Commons Attribution 4.0 International License as our standard license for "
    "publications and data",
)
VINTAGE = "Aqueduct 4.0 (release of 2023-08), baseline 1979-2019, annual"
CAUTIONS = (
    "Aqueduct risk indicators cannot be measured directly and therefore have not been validated. "
    "Aqueduct does not model interbasin transfers."
)
LAYER = "baseline_annual"
FIELDS = ("aqid", "pfaf_id", "name_0", "name_1", "bws_raw", "bws_score", "bws_cat", "bws_label")
SIMPLIFY_DEG = 0.0002
SCHEMA = [
    ("basin_id", pa.string()),
    ("country", pa.string()),
    ("admin1", pa.string()),
    ("raw_ratio", pa.float64()),
    ("score", pa.float64()),
    ("category", pa.int32()),
    ("label", pa.string()),
]


def _num(v: Any, digits: int = 4) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= -9999:
        return None
    return round(f, digits)


def normalise(features: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, geoms = [], []
    for f in features:
        p, g = f.get("properties") or {}, f.get("geometry")
        if not g:
            continue
        cat = _num(p.get("bws_cat"), 0)
        pfaf = p.get("pfaf_id")
        rows.append(
            {
                "basin_id": None if pfaf is None else str(int(float(pfaf))),
                "country": (p.get("name_0") or None),
                "admin1": (p.get("name_1") or None),
                "raw_ratio": _num(p.get("bws_raw")),
                "score": _num(p.get("bws_score"), 3),
                "category": None if cat is None else int(cat),
                "label": (p.get("bws_label") or None),
            }
        )
        geoms.append(g)
    return rows, geoms


def read_gdb(gdb: pathlib.Path, bbox: tuple[float, float, float, float]) -> Iterator[dict[str, Any]]:
    import pyogrio.raw  # type: ignore[import-not-found, import-untyped, unused-ignore]

    from services.context_sample.vectors import decode_wkb

    meta, _fids, geometry, field_data = pyogrio.raw.read(
        str(gdb), layer=LAYER, columns=list(FIELDS), bbox=bbox
    )
    names = list(meta["fields"])
    for i, wkb in enumerate(geometry):
        if wkb is None:
            continue
        polys = [[simplify(ring.tolist(), SIMPLIFY_DEG) for ring in poly] for poly in decode_wkb(bytes(wkb))]
        props = {
            n: (v.item() if hasattr(v, "item") else v)
            for n, v in zip(names, (c[i] for c in field_data), strict=True)
        }
        yield {"properties": props, "geometry": {"type": "MultiPolygon", "coordinates": polys}}


def build_from_features(
    features: list[dict[str, Any]],
    *,
    out_dir: pathlib.Path,
    retrieved_at: str,
    coverage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows, geoms = normalise(features)
    prov = provenance_for(
        SOURCE_ID, source_url=URL, retrieved_at=retrieved_at, licence=LICENCE_NAME, vintage=VINTAGE
    )
    out = out_dir / f"{LAYER_ID}.parquet"
    extra: dict[str, Any] = {"cautions": CAUTIONS}
    if coverage:
        extra["coverage"] = coverage
    summary = write_geoparquet(out, rows, geoms, schema=SCHEMA, provenance=prov, extra_metadata=extra)
    output = {
        "layer_id": LAYER_ID,
        "file": out.name,
        "kind": "polygons",
        "provenance": {**prov.record(), "cautions": CAUTIONS},
        "licence_name": LICENCE_NAME,
        "summary": summary,
    }
    if coverage:
        output["coverage"] = public(coverage)
    return [output]


def build(
    *,
    region: str = "texas",
    out_dir: pathlib.Path | None = None,
    snapshot: pathlib.Path | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
    work_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """`snapshot`: the Aqueduct zip, its extracted `.gdb`, or a recorded `.geojson` FeatureCollection."""
    bbox = REGIONS[region]
    out_dir = output_dir(out_dir)
    if snapshot is not None and snapshot.suffix == ".geojson":
        feats = json.loads(snapshot.read_text())["features"]
        return build_from_features(
            feats, out_dir=out_dir, retrieved_at=snapshot_retrieved_at(snapshot), coverage=region_box(region)
        )
    log = FetchLog(SOURCE_ID)
    retrieved_at = snapshot_retrieved_at(snapshot) if snapshot is not None else now_iso()
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmpdir = pathlib.Path(tmp)
        if snapshot is None:
            session = session or PoliteSession(rate_limits={"files.wri.org": 1.0, "www.wri.org": 1.0})
            verify_terms(session, TERMS_URL, TERMS_PHRASES, log=log)
            info = download(session, URL, tmpdir / "aqueduct.zip", log=log)
            retrieved_at = info["retrieved_at"]
            snapshot = tmpdir / "aqueduct.zip"
        gdb = snapshot
        if snapshot.suffix == ".zip":
            gdb = extract_gdb(snapshot, tmpdir)
            if snapshot.parent == tmpdir:
                snapshot.unlink()
        outputs = build_from_features(
            list(read_gdb(gdb, bbox)),
            out_dir=out_dir,
            retrieved_at=retrieved_at,
            coverage=region_box(region),
        )
        if gdb.parent == tmpdir:
            shutil.rmtree(gdb)
    log.write(store, region=region, rows=outputs[0]["summary"]["rows"])
    return outputs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument("--snapshot", type=pathlib.Path)
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(region=args.region, out_dir=args.out_dir, snapshot=args.snapshot, store=Store())
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
