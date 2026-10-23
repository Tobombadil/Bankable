"""Counties for the site finder: Census TIGER/Line® county boundaries (`us.census.tiger_counties`).

A candidate segment's county and state are the TIGER county its midpoint falls in. TIGER/Line is the
Census Bureau's full-resolution boundary file (a US government work, 17 U.S.C. §105; terms quoted in
`data/sources.yaml`); the boundaries are "for statistical data collection and tabulation purposes
only" and "are not legal land descriptions", which the finder says beside every county it names.

    python -m services.site_finder.counties --fetch [--states TX,OK] [--data-root DIR]

downloads the national county file (`tl_<year>_us_county.zip`, 83 MB for 2026) once, keeps the
counties of the named states (default: all), simplifies each outline by 5 m (Douglas-Peucker, every
kept vertex an original one) and writes `normalized/context/us.census.tiger_counties.parquet` under
the data root, with the provenance columns on every row. The zip is deleted as soon as it has been
read. A second run compares the file's `Last-Modified` and size with the copy it built from and
fetches nothing when they match.

When that file is absent, `CountyIndex.load` falls back to the vendored 1:20,000,000 Census
cartographic boundary counties (`data/vendored/regions/us_counties.geojson`,
`us.census.cartographic_boundaries`), and every county it names carries that source instead, so a
reader can tell the coarser lookup apart. The fallback can misplace a point within a kilometre or so
of a county line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import Registry

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_ID = "us.census.tiger_counties"
FALLBACK_SOURCE_ID = "us.census.cartographic_boundaries"
YEAR = 2026
URL = f"https://www2.census.gov/geo/tiger/TIGER{YEAR}/COUNTY/tl_{YEAR}_us_county.zip"
VENDORED_COUNTIES = ROOT / "data" / "vendored" / "regions" / "us_counties.geojson"
#: The vendored file's own source and fetch date (`data/sources.yaml` `us.census.cartographic_boundaries`).
VENDORED_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_county_20m.zip"
VENDORED_RETRIEVED_AT = "2026-09-18T00:00:00Z"
RELATIVE_PATH = pathlib.Path("normalized") / "context" / f"{SOURCE_ID}.parquet"
#: Douglas-Peucker tolerance, degrees (about 5 m north-south).
SIMPLIFY_DEG = 0.000045
#: What every county the finder names says about the boundary (TIGER/Line technical documentation
#: 2026, chapter 1.1, quoted in `data/sources.yaml`).
BOUNDARY_NOTE = "Census statistical boundaries, not legal land descriptions."


def output_path(data_root: pathlib.Path) -> pathlib.Path:
    return data_root / RELATIVE_PATH


def _state_codes() -> dict[str, str]:
    """`{state FIPS: "US-XX"}` from the vendored counties (each GEOID starts with its state's FIPS)."""
    doc = json.loads(VENDORED_COUNTIES.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for feat in doc["features"]:
        props = feat["properties"]
        if props.get("state_code"):
            out[str(props["region_id"])[:2]] = str(props["state_code"])
    return out


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = pa.schema(
    [
        ("geoid", pa.string()),
        ("name", pa.string()),
        ("namelsad", pa.string()),
        ("state_fips", pa.string()),
        ("state_code", pa.string()),
        ("geometry", pa.binary()),
        ("source_id", pa.string()),
        ("source_url", pa.string()),
        ("retrieved_at", pa.string()),
        ("licence", pa.string()),
        ("vintage", pa.string()),
    ]
)


def counties_from_zip(
    content: bytes, *, states: Iterable[str] | None, url: str, retrieved_at: str, licence: str, vintage: str
) -> pa.Table:
    """The counties of `states` (postal codes; None = all) from a TIGER county zip, simplified."""
    import shapefile  # type: ignore[import-untyped]  # pyshp ships no type stubs
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    zf = zipfile.ZipFile(io.BytesIO(content))
    members = {name.rsplit(".", 1)[-1].lower(): name for name in zf.namelist()}
    reader = shapefile.Reader(
        shp=io.BytesIO(zf.read(members["shp"])),
        dbf=io.BytesIO(zf.read(members["dbf"])),
        shx=io.BytesIO(zf.read(members["shx"])),
    )
    codes = _state_codes()
    wanted = {f"US-{s.strip().upper().removeprefix('US-')}" for s in states} if states else None
    cols: dict[str, list[Any]] = {name: [] for name in SCHEMA.names}
    for sr in reader.iterShapeRecords():
        rec = sr.record.as_dict()
        statefp = str(rec["STATEFP"])
        code = codes.get(statefp)
        if wanted is not None and code not in wanted:
            continue
        geom = shapely.geometry.shape(sr.shape.__geo_interface__)
        simplified = shapely.simplify(geom, SIMPLIFY_DEG, preserve_topology=False)
        if simplified.is_empty or not simplified.is_valid:
            simplified = shapely.make_valid(simplified) if not simplified.is_empty else geom
        cols["geoid"].append(str(rec["GEOID"]))
        cols["name"].append(str(rec["NAME"]))
        cols["namelsad"].append(str(rec.get("NAMELSAD") or rec["NAME"]))
        cols["state_fips"].append(statefp)
        cols["state_code"].append(code)
        cols["geometry"].append(shapely.to_wkb(simplified))
        cols["source_id"].append(SOURCE_ID)
        cols["source_url"].append(url)
        cols["retrieved_at"].append(retrieved_at)
        cols["licence"].append(licence)
        cols["vintage"].append(vintage)
    return pa.table(cols, schema=SCHEMA)


def build(
    data_root: pathlib.Path,
    *,
    states: Sequence[str] | None = None,
    session: PoliteSession | None = None,
    url: str = URL,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch the TIGER county file and write the counties of `states`; skip when unchanged."""
    out = output_path(data_root)
    sidecar = out.with_suffix(".json")
    session = session or PoliteSession()
    head = session.request("HEAD", url, allow_redirects=True)
    head.raise_for_status()
    remote = {
        "url": url,
        "last_modified": head.headers.get("Last-Modified"),
        "bytes": int(head.headers.get("Content-Length") or 0) or None,
        "states": sorted(s.upper() for s in states) if states else None,
    }
    if not force and out.exists() and sidecar.exists():
        held = json.loads(sidecar.read_text(encoding="utf-8"))
        if {k: held.get(k) for k in remote} == remote:
            return {**held, "fetched": False}
    entry = Registry().get(SOURCE_ID)
    retrieved_at = _now_iso()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix="tiger-") as tmp:
        zip_path = pathlib.Path(tmp) / "counties.zip"
        resp = session.get(url, stream=True)
        resp.raise_for_status()
        with zip_path.open("wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
        size = zip_path.stat().st_size
        if remote["bytes"] and size != remote["bytes"]:
            raise RuntimeError(f"{url}: {size} bytes received, {remote['bytes']} announced")
        table = counties_from_zip(
            zip_path.read_bytes(),
            states=states,
            url=url,
            retrieved_at=retrieved_at,
            licence=entry.license or "US Government work, public domain (17 U.S.C. §105)",
            vintage=f"TIGER/Line Shapefiles {YEAR}",
        )
    tmp_out = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_out, compression="zstd")
    tmp_out.replace(out)
    summary = {
        **remote,
        "retrieved_at": retrieved_at,
        "counties": table.num_rows,
        "out_bytes": out.stat().st_size,
    }
    sidecar.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return {**summary, "fetched": True}


# ------------------------------------------------------------------------------------- lookup
@dataclass(frozen=True)
class County:
    fips: str
    name: str
    state_code: str | None


def _with_descriptor(name: str, state_code: str | None) -> str:
    """The vendored outline file names a county without its descriptor ("Travis"); TIGER's NAMELSAD
    has it. Louisiana's are parishes; Alaska's boroughs and census areas, and the independent
    cities, already carry theirs in the name or have none to add."""
    if state_code == "US-AK" or name.lower().endswith(
        (" city", " county", " parish", " borough", " census area")
    ):
        return name
    return f"{name} Parish" if state_code == "US-LA" else f"{name} County"


class CountyIndex:
    """Point-in-county over one boundary file, with that file's provenance."""

    def __init__(self, counties: list[County], geoms: Sequence[Any], provenance: dict[str, Any]) -> None:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]
        from shapely.strtree import STRtree  # type: ignore[import-untyped,import-not-found,unused-ignore]

        self.counties = counties
        self.geoms = np.asarray(list(geoms), dtype=object)
        for g in self.geoms:
            shapely.prepare(g)
        self.tree = STRtree(self.geoms)
        self.provenance = provenance

    def __len__(self) -> int:
        return len(self.counties)

    def lookup(self, lons: Sequence[float], lats: Sequence[float]) -> list[County | None]:
        """The county each point lies in (the first, for a point on a boundary), else None."""
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        points = shapely.points(np.asarray(lons, dtype=float), np.asarray(lats, dtype=float))
        found: list[County | None] = [None] * len(points)
        if len(points) == 0:
            return found
        pairs = self.tree.query(points, predicate="intersects")
        for point_i, county_i in zip(pairs[0], pairs[1], strict=True):
            if found[int(point_i)] is None:
                found[int(point_i)] = self.counties[int(county_i)]
        return found

    @classmethod
    def from_parquet(cls, path: pathlib.Path) -> CountyIndex:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        table = pq.read_table(path)
        rows = table.drop_columns(["geometry"]).to_pylist()
        geoms = shapely.from_wkb(table.column("geometry").to_pylist())
        first = rows[0] if rows else {}
        return cls(
            # TIGER's NAMELSAD ("Travis County", "Orleans Parish", "Richmond city"): the name a reader
            # knows the area by, with its legal or statistical descriptor.
            [County(r["geoid"], r.get("namelsad") or r["name"], r["state_code"]) for r in rows],
            geoms,
            _provenance(
                SOURCE_ID,
                source_url=str(first.get("source_url") or URL),
                retrieved_at=first.get("retrieved_at"),
                vintage=first.get("vintage"),
            ),
        )

    @classmethod
    def from_vendored(cls, path: pathlib.Path = VENDORED_COUNTIES) -> CountyIndex:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        doc = json.loads(path.read_text(encoding="utf-8"))
        counties, geoms = [], []
        for feat in doc["features"]:
            props = feat["properties"]
            counties.append(
                County(
                    str(props["region_id"]),
                    _with_descriptor(str(props["name"]), props.get("state_code")),
                    props.get("state_code"),
                )
            )
            geoms.append(shapely.geometry.shape(feat["geometry"]))
        return cls(
            counties,
            geoms,
            _provenance(
                FALLBACK_SOURCE_ID,
                source_url=VENDORED_URL,
                retrieved_at=VENDORED_RETRIEVED_AT,
                vintage="Cartographic Boundary Files 2024, 1:20,000,000",
            ),
        )

    @classmethod
    def load(cls, data_root: pathlib.Path) -> CountyIndex:
        path = output_path(data_root)
        return cls.from_parquet(path) if path.exists() else cls.from_vendored()


def _provenance(source_id: str, *, source_url: str, retrieved_at: Any, vintage: Any) -> dict[str, Any]:
    """The finder's provenance shape for a value read from a file (`services/site_finder/provenance.py`)."""
    from services.site_finder.provenance import registry_provenance

    return registry_provenance(
        source_id, source_url=source_url, retrieved_at=retrieved_at, vintage=vintage, note=BOUNDARY_NOTE
    )


def main(argv: list[str] | None = None) -> None:
    from pipeline.connectors.store import DATA_DIR

    parser = argparse.ArgumentParser(prog="python -m services.site_finder.counties", description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download and build (default: report only)")
    parser.add_argument("--states", default="", help="postal codes, comma-separated; default every state")
    parser.add_argument("--data-root", type=pathlib.Path, default=DATA_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    if args.fetch:
        result = build(args.data_root, states=states, force=args.force)
    else:
        path = output_path(args.data_root)
        result = {"path": str(path), "exists": path.exists()}
    print(json.dumps(result, sort_keys=True, default=str))  # noqa: T201 - CLI summary line


if __name__ == "__main__":
    main()
