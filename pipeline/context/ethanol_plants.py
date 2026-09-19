"""EIA US Energy Atlas "Ethanol Plants" layer -> `ethanol_plant` asset rows
(`us.eia.atlas.ethanol_plants`, data/sources.yaml section K; ADR 0008; owner decision 2026-09-19).

Two routes to the same layer, tried in this order by `fetch_layer`:

1. The ArcGIS feature service the Atlas item points at,
   `https://services7.arcgis.com/FGr1D95XCGALKXqM/.../Ethanol_Plants_US_EIA/FeatureServer/112`
   (org `FGr1D95XCGALKXqM` is "U.S. Energy Information Administration", urlKey `eia`, confirmed
   through the ArcGIS portal record). On 2026-09-19, through the sandbox proxy, its metadata and
   query endpoints answered `{"error": {"code": 499, "message": "Token Required"}}` anonymously,
   as did the sibling biodiesel, pipelines and processing-plant services; none appears among the
   org's 79 public feature-service items or in the Atlas DCAT feed. The connector treats that
   reply as a block (`HttpBlocked`), never a retry, and falls through to route 2.
2. EIA's own downloadable copy of the layer, the shapefile zip
   `https://www.eia.gov/maps/map_data/Ethanol_Plants_US_EIA.zip` (HTTP 200, 37,644 bytes on
   2026-09-19; `www.eia.gov/robots.txt` does not disallow `/maps/`). Its FGDC/ISO metadata
   (`Ethanol_Plants_US_20210101.shp.xml`) carries the dataset-level terms the browser task in
   docs/40 §0 was meant to read: use limitation "None (public use). Users are advised to
   thoroughly review the metadata to understand the appropriate use and limitations of the data.
   These data and related graphics, if available, are not legal documents and are not intended
   to be used as such. The information contained in these data is dynamic and may change over
   time. The U.S. Energy Information Administration gives no warranty, expressed or implied, as
   to the accuracy, reliability, or completeness of these data."; credit "U.S. Energy
   Information Administration"; abstract "Operating fuel ethanol production plants in the United
   States as of January 1, 2021. Attribute data come from EIA, U.S. Fuel Ethanol Plant Production
   Capacity ... Locations determined through publicly available information such as company
   websites and media reports." The copy is a 2021-01-01 vintage (197 plants); the annual table
   (`ethanol_capacity.py`) is the current capacity primary, this layer supplies the coordinates.

Both routes are parsed to GeoJSON-shaped features (`parse`) and normalised by `build_assets`
through one field-alias table: the service spells the fields `Company, Site, State (postal), PADD,
Cap_Mmgal, Source, Period, Longitude, Latitude`; the shapefile `Company, Site_Name, Capacity,
State (full name), PADD, Data_Perio, Source, Longitude, Latitude`.

Objective feature set: nameplate capacity in MMgal/yr (`capacity_value`;
`attributes.nameplate_capacity_mmgal_yr`), the company string (`owner_raw`, `operator_name`),
site, PADD, the EIA data period and source note, and the point (exact: EIA's own coordinate).
No feedstock field exists on the layer; `feedstock` stays null rather than assumed. Identity is
`<STATE>-<company slug>-<site slug>` (ArcGIS OBJECTIDs are reassigned on republish, so they are
kept in `raw` only). Every listed plant is operating (the layer is "operating fuel ethanol
production plants"), so `status = operating`.

atlas.eia.gov/robots.txt sets `Crawl-delay: 60` for every agent; `fuels.RATE_LIMITS` honours it.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import time
import zipfile
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import shapefile  # type: ignore[import-untyped]  # pyshp: a dependency since pipeline/context/regions.py

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpBlocked, PoliteSession
from pipeline.context import fuels
from pipeline.normalize import US_STATES
from services.ids import slugify

SOURCE_ID = "us.eia.atlas.ethanol_plants"
ABOUT_URL = "https://atlas.eia.gov/datasets/eia::ethanol-plants/about"
LAYER_URL = "https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services/Ethanol_Plants_US_EIA/FeatureServer/112"
ZIP_URL = "https://www.eia.gov/maps/map_data/Ethanol_Plants_US_EIA.zip"
DEFAULT_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"
PAGE_SIZE = 1000
#: ArcGIS error codes that mean "not for anonymous clients": 498 invalid token, 499 token
#: required, 403 forbidden. A block, never a retry (docs/20 §4.3).
TOKEN_ERRORS = frozenset({403, 498, 499})

#: Canonical name -> the spellings seen on the feature service and on the shapefile.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("Company",),
    "site": ("Site", "Site_Name"),
    "state": ("State",),
    "padd": ("PADD",),
    "capacity_mmgal": ("Cap_Mmgal", "Capacity"),
    "period": ("Period", "Data_Perio"),
    "source": ("Source",),
    "latitude": ("Latitude",),
    "longitude": ("Longitude",),
}
REQUIRED = ("company", "state", "capacity_mmgal")


# ------------------------------------------------------------------ fetch
def query_url(layer_url: str, offset: int, page_size: int = PAGE_SIZE) -> str:
    params = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "returnGeometry": "true",
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
        "f": "geojson",
    }
    return f"{layer_url}/query?{urlencode(params)}"


def _check_error(payload: dict[str, Any], url: str) -> None:
    err = payload.get("error")
    if not err:
        return
    code = int(err.get("code", 0) or 0)
    msg = f"ArcGIS error {code} {err.get('message')!r} from {url}"
    if code in TOKEN_ERRORS:
        raise HttpBlocked(msg + " (anonymous access refused; browser access task, docs/40 §0)")
    raise ParseError(msg)


def fetch_service(session: PoliteSession, layer_url: str = LAYER_URL) -> tuple[bytes, str]:
    """Route 1: every feature of the service as one GeoJSON FeatureCollection (paged by
    `resultOffset`), plus the first page's URL. An API endpoint, so robots are not consulted;
    `PoliteSession` keeps the host rate. Raises `HttpBlocked` on a token-required reply."""
    features: list[dict[str, Any]] = []
    offset = 0
    first_url = query_url(layer_url, 0)
    while True:
        url = query_url(layer_url, offset)
        content = fuels.fetch(session, url, honour_robots=False)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as e:
            raise ParseError(f"non-JSON reply from {url}: {content[:120]!r}") from e
        _check_error(payload, url)
        page = payload.get("features") or []
        features.extend(page)
        exceeded = bool(payload.get("exceededTransferLimit")) or bool(
            (payload.get("properties") or {}).get("exceededTransferLimit")
        )
        if not page or not exceeded:
            break
        offset += len(page)
    collection = {"type": "FeatureCollection", "features": features}
    return json.dumps(collection).encode("utf-8"), first_url


def fetch_zip(session: PoliteSession, zip_url: str = ZIP_URL) -> tuple[bytes, str]:
    """Route 2: EIA's downloadable shapefile copy (robots honoured for www.eia.gov)."""
    content = fuels.fetch(session, zip_url)
    if not content.startswith(b"PK"):
        raise ParseError(f"{zip_url} did not return a zip (first bytes {content[:8]!r})")
    return content, zip_url


def fetch_layer(
    session: PoliteSession, *, layer_url: str = LAYER_URL, zip_url: str = ZIP_URL
) -> tuple[bytes, str, str]:
    """`(content, fetched_url, ext)`: the service when it answers anonymously, else the zip.
    Any other failure on the service (a schema change, a 5xx after retries) propagates -- only
    the documented token block is a reason to fall through."""
    try:
        content, url = fetch_service(session, layer_url)
        return content, url, "geojson"
    except HttpBlocked:
        content, url = fetch_zip(session, zip_url)
        return content, url, "zip"


# ------------------------------------------------------------------ parse
def _shapefile_features(content: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = {n.lower().rsplit(".", 1)[-1]: n for n in zf.namelist() if "." in n}
        missing = [ext for ext in ("shp", "dbf", "shx") if ext not in names]
        if missing:
            raise ParseError(f"shapefile zip lacks {missing}: {sorted(zf.namelist())}")
        reader = shapefile.Reader(
            shp=io.BytesIO(zf.read(names["shp"])),
            dbf=io.BytesIO(zf.read(names["dbf"])),
            shx=io.BytesIO(zf.read(names["shx"])),
        )
    features: list[dict[str, Any]] = []
    for sr in reader.iterShapeRecords():
        points = list(sr.shape.points) if sr.shape.shapeType != shapefile.NULL else []
        geometry = {"type": "Point", "coordinates": [points[0][0], points[0][1]]} if points else None
        features.append({"type": "Feature", "properties": sr.record.as_dict(), "geometry": geometry})
    return features


def parse(content: bytes) -> list[dict[str, Any]]:
    """GeoJSON-shaped features from either snapshot format (zip or GeoJSON)."""
    if content.startswith(b"PK"):
        features = _shapefile_features(content)
    else:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as e:
            raise ParseError("ethanol plants snapshot is neither a zip nor JSON") from e
        _check_error(payload, LAYER_URL)
        raw_features = payload.get("features")
        if not isinstance(raw_features, list):
            raise ParseError("ethanol plants snapshot has no features list")
        features = [f for f in raw_features if isinstance(f, dict)]
    if not features:
        raise ParseError("ethanol plants snapshot has zero features")
    props = features[0].get("properties") or {}
    missing = [k for k in REQUIRED if _alias(props, k) is None]
    if missing:
        raise ParseError(f"Atlas ethanol layer schema changed: missing {missing}; got {sorted(props)[:12]}")
    return features


def _alias(props: dict[str, Any], key: str) -> Any:
    for name in FIELD_ALIASES[key]:
        if name in props:
            return props[name]
    return None


def _state_code(value: Any) -> str | None:
    text = fuels.clean_str(value)
    if text is None:
        return None
    if len(text) == 2:
        return text.upper()
    code = US_STATES.get(text.lower())
    return str(code) if code else None


# ------------------------------------------------------------------ normalise
def build_assets(
    features: list[dict[str, Any]],
    *,
    retrieved_at: str,
    source_url: str,
    licence_id: str,
    placement: fuels.Placement | None = None,
) -> pd.DataFrame:
    place = placement or fuels.Placement()
    seen: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for f in features:
        props: dict[str, Any] = f.get("properties") or {}
        company = fuels.clean_str(_alias(props, "company")) or "Unknown company"
        site = fuels.clean_str(_alias(props, "site"))
        state = _state_code(_alias(props, "state"))
        capacity = fuels.to_float(_alias(props, "capacity_mmgal"))
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") if geom.get("type") == "Point" else None
        point = None
        if isinstance(coords, list | tuple) and len(coords) >= 2:
            point = fuels.valid_point(coords[1], coords[0])
        if point is None:
            point = fuels.valid_point(_alias(props, "latitude"), _alias(props, "longitude"))
        lon, lat = point if point is not None else (None, None)
        company_slug = slugify(company, max_length=40)
        base = f"{state or 'XX'}-{company_slug}-{slugify(site or 'unknown', max_length=30)}"
        n = seen.get(base, 0) + 1
        seen[base] = n
        key = base if n == 1 else f"{base}-{n}"
        placed = place.resolve(state, None)
        if point is not None:
            placed.update({"placement_precision": "exact", "centroid_lon": None, "centroid_lat": None})
        rows.append(
            fuels.asset_row(
                source_id=SOURCE_ID,
                source_url=source_url,
                retrieved_at=retrieved_at,
                licence_id=licence_id,
                source_asset_id=key,
                name=f"{company} ({site}, {state})" if site and state else company,
                operator_name=company,
                owner_raw=company,
                status="operating",
                status_raw="operating fuel ethanol production plant (EIA Atlas layer)",
                technology="ethanol",
                capacity_value=capacity,
                capacity_unit="MMgal/yr",
                lon=lon,
                lat=lat,
                state_code=fuels.state_code(state),
                attributes=fuels.numeric_attributes({"nameplate_capacity_mmgal_yr": capacity}),
                attributes_text=fuels.text_attributes(
                    {
                        "site": site,
                        "padd": _alias(props, "padd"),
                        "state_name": _alias(props, "state")
                        if state and len(str(_alias(props, "state"))) > 2
                        else None,
                        "data_period": _alias(props, "period"),
                        "source_note": _alias(props, "source"),
                    }
                ),
                raw={"properties": props, "geometry": geom},
                **placed,
            )
        )
    return fuels.assemble(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fetch", action="store_true", help="Fetch the layer (service, else EIA zip) and snapshot it"
    )
    group.add_argument("--file", type=pathlib.Path, help="Parse this .zip or .geojson instead of a snapshot")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Newest data/snapshots snapshot (default)"
    )
    parser.add_argument("--layer-url", default=LAYER_URL)
    parser.add_argument("--zip-url", default=ZIP_URL)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--manifest", type=pathlib.Path, help="Alternate data/sources.yaml (default: the repo's)"
    )
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, args.manifest)
    if args.fetch:
        session = fuels.polite_session()
        content, url, ext = fetch_layer(session, layer_url=args.layer_url, zip_url=args.zip_url)
        path, retrieved_at = fuels.record_snapshot(
            SOURCE_ID,
            content,
            ext=ext,
            fetched_url=url,
            retrieved_at=fuels.utc_now(),
            requests_made=session.requests_made,
            meta={"about_url": ABOUT_URL, "route": "service" if ext == "geojson" else "eia_map_data_zip"},
        )
        source_url = url
    else:
        path = args.file or _latest_snapshot()
        retrieved_at, source_url = fuels.snapshot_metadata(path, SOURCE_ID, ABOUT_URL)

    features = parse(path.read_bytes())
    assets = build_assets(
        features, retrieved_at=retrieved_at, source_url=source_url, licence_id=entry.licence_id
    )
    fuels.write_context_parquet(assets, args.out)
    summary = fuels.summarise(
        assets,
        data_period=fuels.clean_str(_alias(features[0].get("properties") or {}, "period")),
        snapshot=str(path),
        out=str(args.out),
        elapsed_s=round(time.monotonic() - t0, 2),
    )
    print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as eia_plants.py


def _latest_snapshot() -> pathlib.Path:
    candidates: list[pathlib.Path] = []
    for ext in ("zip", "geojson"):
        try:
            candidates.append(fuels.latest_snapshot(SOURCE_ID, ext))
        except FileNotFoundError:
            continue
    if not candidates:
        raise FileNotFoundError(f"no zip/geojson snapshot for {SOURCE_ID}")
    return max(candidates, key=lambda p: p.stem)


if __name__ == "__main__":
    main()
