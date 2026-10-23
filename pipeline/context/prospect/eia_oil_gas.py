"""Oil and gas: EIA sedimentary basins and low-permeability (tight oil and shale gas) plays.

Both come from EIA's US Energy Atlas feature services (EIA's own ``www.eia.gov/maps`` pages answer 404
since the maps moved to the Atlas; measured 2026-10-22):

- basins: ``SedimentaryBasins_US_EIA/FeatureServer/109`` ("Lower 48 Sedimentary Basins", ``Name``,
  "associated with the EIA shale plays as of February 2024"; data last edited 2024-06-07);
- plays: ``TightOil_ShaleGas_Plays_Lower48_EIA/FeatureServer/0`` (50 plays: ``Shale_play``,
  ``Basin``, ``Lithology``, ``Age_shale``; the 2021 update adds the Midland Basin benches).

Terms: public domain (EIA's Copyrights and Reuse page, quoted in data/sources.yaml); EIA asks for an
acknowledgement with the publication date, which the `attribution` carries. The plays overlap (benches of
one basin stack), so a point can sit in several plays; each is a separate feature.

EIA's oil and gas field boundaries were not found (data/sources.yaml `us.eia.oil_gas_field_boundaries`),
so this module builds two layers, not three.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.store import Store
from pipeline.context.prospect.arcgis import query_geojson
from pipeline.context.prospect.common import (
    REGIONS,
    FetchLog,
    now_iso,
    output_dir,
    provenance_for,
    snapshot_time,
    verify_terms,
)
from pipeline.context.prospect.coverage import public, region_box
from pipeline.context.prospect.geoparquet import write_geoparquet

ATLAS = "https://services7.arcgis.com/FGr1D95XCGALKXqM/ArcGIS/rest/services"
TERMS_URL = "https://www.eia.gov/about/copyrights_reuse.php"
TERMS_PHRASES = (
    "U.S. government publications are in the public domain and are not subject to copyright protection.",
    "You may use and/or distribute any of our data, files, databases, reports, graphs, charts, and other "
    "information products",
)
LICENCE_NAME = "Public domain (EIA, 17 U.S.C. §105)"
#: The Atlas services are an API (ArcGIS REST); one request per second is ample.
ATLAS_RPS = 1.0


LAYERS: dict[str, dict[str, Any]] = {
    "oil_gas_basins": {
        "source_id": "us.eia.atlas.sedimentary_basins",
        "url": f"{ATLAS}/SedimentaryBasins_US_EIA/FeatureServer/109",
        "vintage": (
            "EIA sedimentary basins associated with shale plays, as of February 2024 (data edited 2024-06-07)"
        ),
        "schema": [("name", pa.string()), ("area_sq_mi", pa.float64())],
    },
    "oil_gas_plays": {
        "source_id": "us.eia.atlas.shale_plays",
        "url": f"{ATLAS}/TightOil_ShaleGas_Plays_Lower48_EIA/FeatureServer/0",
        "vintage": "EIA tight oil and shale gas plays, 2021 update (service of 2022-02-01)",
        "schema": [
            ("name", pa.string()),
            ("basin", pa.string()),
            ("lithology", pa.string()),
            ("age", pa.string()),
            ("area_sq_mi", pa.float64()),
        ],
    },
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _num(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 1)
    except (TypeError, ValueError):
        return None


def normalise(
    layer_id: str, features: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """GeoJSON features -> (attribute rows, geometries) in the layer's schema."""
    rows: list[dict[str, Any]] = []
    geoms: list[dict[str, Any]] = []
    for f in features:
        p = f.get("properties") or {}
        geom = f.get("geometry")
        if not geom:
            continue
        if layer_id == "oil_gas_basins":
            row = {"name": _text(p.get("Name")), "area_sq_mi": _num(p.get("Area_sq_mi"))}
        else:
            row = {
                "name": _text(p.get("Shale_play")),
                "basin": _text(p.get("Basin")),
                "lithology": _text(p.get("Lithology")),
                "age": _text(p.get("Age_shale")),
                "area_sq_mi": _num(p.get("Area_sq_mi")),
            }
        rows.append(row)
        geoms.append(geom)
    return rows, geoms


def build_layer(
    layer_id: str,
    features: list[dict[str, Any]],
    *,
    out_dir: pathlib.Path,
    retrieved_at: str,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = LAYERS[layer_id]
    prov = provenance_for(
        spec["source_id"],
        source_url=spec["url"],
        retrieved_at=retrieved_at,
        licence=LICENCE_NAME,
        vintage=spec["vintage"],
    )
    rows, geoms = normalise(layer_id, features)
    out = out_dir / f"{layer_id}.parquet"
    extra = {"coverage": coverage} if coverage else None
    summary = write_geoparquet(out, rows, geoms, schema=spec["schema"], provenance=prov, extra_metadata=extra)
    summary["features_in"] = len(features)
    output = {
        "layer_id": layer_id,
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
    snapshot_dir: pathlib.Path | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    """Both layers, every feature intersecting the region's bounding box (a play crossing the state line is
    kept whole: a corridor near the line needs it). `snapshot_dir` holds `<layer_id>.geojson` pages."""
    bbox = REGIONS[region]
    out_dir = output_dir(out_dir)
    outputs = []
    if snapshot_dir is not None:
        for layer_id in LAYERS:
            page = snapshot_dir / f"{layer_id}.geojson"
            doc = json.loads(page.read_text())
            outputs.append(
                build_layer(
                    layer_id,
                    doc["features"],
                    out_dir=out_dir,
                    retrieved_at=snapshot_time(page),
                    coverage=region_box(region),
                )
            )
        return outputs
    session = session or PoliteSession(rate_limits={"services7.arcgis.com": ATLAS_RPS, "www.eia.gov": 1.0})
    for layer_id, spec in LAYERS.items():
        log = FetchLog(spec["source_id"])
        verify_terms(session, TERMS_URL, TERMS_PHRASES, log=log)
        features = query_geojson(session, spec["url"], log=log, bbox=bbox)
        outputs.append(
            build_layer(
                layer_id, features, out_dir=out_dir, retrieved_at=now_iso(), coverage=region_box(region)
            )
        )
        log.write(store, region=region, layer=layer_id, features=len(features))
    return outputs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument("--snapshot-dir", type=pathlib.Path)
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(region=args.region, out_dir=args.out_dir, snapshot_dir=args.snapshot_dir, store=Store())
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
