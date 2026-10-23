"""Build the prospect layers for a region and write `layers.json`, the manifest the sampler reads.

    python -m pipeline.context.prospect.build --region texas [--sources solar,oil_gas,...]
        [--snapshots DIR] [--out-dir DIR]

Without `--snapshots`, every source fetches (politely, terms re-read first) except the Global Wind
Atlas, whose Terms forbid automated access: it builds only from a file a person downloaded
(`<snapshots>/global_wind_atlas/*.tif`) and is otherwise reported as skipped. With `--snapshots`, each
source reads what is in its subdirectory instead (layout in `SNAPSHOT_LAYOUT`), which is how GitHub
Actions builds from the private bucket's inbox and how the tests run.

The manifest is merged, not replaced: building one source keeps the others' entries. A source that
fails keeps its previous entry, is listed under `failures`, and the command exits 1, so a broken source
never wipes a working layer (the basemap build's rule, applied to a manifest).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from pipeline.context.prospect import (
    eia_oil_gas,
    eia_reserves,
    gwa_wind,
    netl_energy_communities,
    nrel_nsrdb,
    usgs_nshm,
    usgs_padus,
    wri_aqueduct,
)
from pipeline.context.prospect.common import REGIONS, now_iso, output_dir, sha256_file
from services.context_sample.catalog import CATALOG
from services.context_sample.paths import MANIFEST_NAME

FORMAT = "inframation.prospect.v1"

Builder = Callable[[str, pathlib.Path, pathlib.Path | None, Store | None], list[dict[str, Any]]]


def _first(directory: pathlib.Path | None, *patterns: str) -> pathlib.Path | None:
    if directory is None or not directory.exists():
        return None
    for pattern in patterns:
        hits = sorted(directory.glob(pattern))
        if hits:
            return hits[-1]
    return None


def _solar(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return nrel_nsrdb.build(region=region, out_dir=out, snapshot_dir=snaps, store=store)


def _wind(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    tif = _first(snaps, "*.tif", "*.tiff")
    if tif is None:
        raise gwa_wind.ManualSourceOnly(
            "no Global Wind Atlas file: a person downloads it (Terms forbid automated access) into "
            "<snapshots>/global_wind_atlas/"
        )
    meta = tif.with_suffix(".json")
    downloaded_at = json.loads(meta.read_text()).get("downloaded_at") if meta.exists() else None
    return gwa_wind.build(region=region, out_dir=out, snapshot=tif, downloaded_at=downloaded_at, store=store)


def _oil_gas(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return eia_oil_gas.build(region=region, out_dir=out, snapshot_dir=snaps, store=store)


def _reserves(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return eia_reserves.build(region=region, out_dir=out, snapshot=_first(snaps, "*.xlsx"), store=store)


def _energy_communities(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return netl_energy_communities.build(region=region, out_dir=out, snapshot_dir=snaps, store=store)


def _protected(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    snap = _first(snaps, "*.gdb", "*.zip", "*.geojson")
    return usgs_padus.build(region=region, out_dir=out, snapshot=snap, store=store)


def _water(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    snap = _first(snaps, "*.gdb", "*.zip", "*.geojson")
    return wri_aqueduct.build(region=region, out_dir=out, snapshot=snap, store=store)


def _seismic(
    region: str, out: pathlib.Path, snaps: pathlib.Path | None, store: Store | None
) -> list[dict[str, Any]]:
    return usgs_nshm.build(region=region, out_dir=out, snapshot=_first(snaps, "*.csv"), store=store)


#: name -> (builder, snapshot subdirectory, the files that subdirectory holds)
SOURCES: dict[str, tuple[Builder, str, str]] = {
    "solar": (
        _solar,
        "nrel_nsrdb",
        "nsrdbv3_ghi.zip + nsrdbv3_dni.zip, or the extracted nsrdb3_ghi.tif/nsrdb3_dni.tif",
    ),
    "wind": (_wind, "global_wind_atlas", "the downloaded GeoTIFF, optional <name>.json {downloaded_at}"),
    "oil_gas": (_oil_gas, "eia_oil_gas", "oil_gas_basins.geojson, oil_gas_plays.geojson"),
    "reserves": (_reserves, "eia_reserves", "ARR_<year>_TABLES_ALL.xlsx"),
    "energy_communities": (_energy_communities, "netl", "coal.geojson, ec.geojson, ffe_only.geojson"),
    "protected": (_protected, "padus", "a PAD-US zip or .gdb (or a recorded .geojson)"),
    "water": (_water, "wri_aqueduct", "the Aqueduct zip or .gdb (or a recorded .geojson)"),
    "seismic": (_seismic, "usgs_nshm", "PGA map.csv"),
}
SNAPSHOT_LAYOUT = {name: (sub, files) for name, (_b, sub, files) in SOURCES.items()}


def source_entries(
    registry: Registry, source_ids: set[str], missing: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Each source's terms summary from data/sources.yaml. A source whose row is gone is left out and
    named in `missing` (the sampler then withholds its layers: no row, no publication); it never stops
    the manifest being written."""
    out = {}
    for sid in sorted(source_ids):
        if sid not in registry:
            if missing is not None:
                missing.append(sid)
            continue
        e = registry.get(sid)
        out[sid] = {
            "name": e.name,
            "operator": e.operator,
            "reuse": e.reuse,
            "publication": e.publication,
            "attribution": e.credit_text,
            "licence_url": e.licence_url or None,
            "licence_id": e.licence_id,
        }
    return out


def load_manifest(out: pathlib.Path) -> dict[str, Any]:
    path = out / MANIFEST_NAME
    if path.exists():
        doc = json.loads(path.read_text())
        if doc.get("format") == FORMAT:
            return dict(doc)
    return {"format": FORMAT, "layers": {}, "sources": {}}


def write_manifest(out: pathlib.Path, manifest: dict[str, Any]) -> pathlib.Path:
    path = out / MANIFEST_NAME
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False))
    tmp.replace(path)
    return path


def _finish(name: str, o: dict[str, Any], *, out: pathlib.Path, region: str, t0: float) -> dict[str, Any]:
    """One builder output made a manifest entry: catalogue check, hashes, sizes, build stamps."""
    if o["layer_id"] not in CATALOG:
        raise KeyError(
            f"{name} built {o['layer_id']}, which services/context_sample/catalog.py does not list"
        )
    path = out / o["file"]
    o["sha256"] = sha256_file(path)
    o["bytes"] = path.stat().st_size
    o["built_at"] = now_iso()
    o["region"] = region
    o["build_seconds"] = round(time.monotonic() - t0, 2)
    for extra in o.get("extra_files", []):
        o.setdefault("extra_sha256", {})[extra] = sha256_file(out / extra)
    return o


def run(
    *,
    region: str,
    sources: list[str],
    out_dir: pathlib.Path | None = None,
    snapshots: pathlib.Path | None = None,
    store: Store | None = None,
) -> dict[str, Any]:
    out = output_dir(out_dir)
    manifest = load_manifest(out)
    if manifest.get("region") not in (None, region):
        # One directory holds one region; a different region starts a fresh manifest.
        manifest = {"format": FORMAT, "layers": {}, "sources": {}}
    report: dict[str, Any] = {"built": [], "skipped": {}, "failures": {}}
    for name in sources:
        builder, sub, _files = SOURCES[name]
        # A source with no snapshot subdirectory fetches (the wind source is skipped instead).
        snaps = snapshots / sub if snapshots is not None and (snapshots / sub).exists() else None
        t0 = time.monotonic()
        try:
            # Everything a source's outputs need before they enter the manifest happens inside this try:
            # an unknown layer id or a missing file fails that source alone, and none of its outputs is
            # merged (the previous entries stay), so the run still writes the manifest.
            entries = [
                _finish(name, o, out=out, region=region, t0=t0) for o in builder(region, out, snaps, store)
            ]
        except gwa_wind.ManualSourceOnly as exc:
            report["skipped"][name] = str(exc)
            continue
        except Exception as exc:  # one source failing must not stop the others (module docstring)
            report["failures"][name] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc(file=sys.stderr)
            continue
        for o in entries:
            manifest["layers"][o["layer_id"]] = o
            report["built"].append(o["layer_id"])
    used = {str(entry["provenance"]["source_id"]) for entry in manifest["layers"].values()}
    missing: list[str] = []
    try:
        manifest["sources"] = source_entries(Registry(), used, missing)
    except Exception as exc:  # an unreadable data/sources.yaml: keep the previous summaries
        report["failures"]["registry"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(file=sys.stderr)
    for sid in missing:
        report["failures"][f"registry:{sid}"] = (
            f"{sid} has no row in data/sources.yaml; its layers stay in the manifest and are withheld"
        )
    manifest.update(format=FORMAT, region=region, bbox=list(REGIONS[region]), updated_at=now_iso())
    manifest["failures"] = report["failures"]
    report["manifest"] = str(write_manifest(out, manifest))
    report["files"] = {
        lid: {"file": e["file"], "bytes": e["bytes"]} for lid, e in sorted(manifest["layers"].items())
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument(
        "--sources", default=",".join(SOURCES), help=f"comma-separated; any of {', '.join(SOURCES)}"
    )
    parser.add_argument(
        "--snapshots", type=pathlib.Path, help="read recorded inputs from DIR/<source>/ instead of fetching"
    )
    parser.add_argument("--out-dir", type=pathlib.Path)
    parser.add_argument("--no-run-records", action="store_true", help="do not write data/runs records")
    args = parser.parse_args(argv)
    names = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        parser.error(f"unknown sources {unknown}; known: {list(SOURCES)}")
    t0 = time.monotonic()
    report = run(
        region=args.region,
        sources=names,
        out_dir=args.out_dir,
        snapshots=args.snapshots,
        store=None if args.no_run_records else Store(),
    )
    report["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(report))  # noqa: T201 - CLI summary line
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
