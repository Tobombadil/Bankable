"""Read polygons from an ArcGIS REST feature/map service layer as GeoJSON in EPSG:4326, page by page.

The query asks the server for `f=geojson&outSR=4326` (the server reprojects, so no pyproj here),
filters by `where` and, optionally, an envelope (`bbox`, intersecting), and pages with `resultOffset`
in steps of the layer's own `maxRecordCount` (capped at `PAGE_SIZE`); `max_allowable_offset` asks the
server to generalise the geometry (degrees, since the output is EPSG:4326). Every request goes through the
polite session and is logged for the run record. `parse_features` is the network-free half, used by the
tests with recorded pages.
"""

from __future__ import annotations

import json
from typing import Any

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.context.prospect.common import FetchLog, now_iso

PAGE_SIZE = 1000
MAX_PAGES = 500


def parse_features(page: bytes | str | dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """One GeoJSON page -> (features, exceeded_transfer_limit). An ArcGIS error body raises."""
    doc = json.loads(page) if isinstance(page, bytes | str) else page
    if not isinstance(doc, dict):
        raise ParseError("not a JSON object")
    if "error" in doc:
        raise ParseError(f"ArcGIS error: {doc['error']}")
    feats = doc.get("features")
    if not isinstance(feats, list):
        raise ParseError("no features array")
    exceeded = bool(
        doc.get("exceededTransferLimit") or (doc.get("properties") or {}).get("exceededTransferLimit")
    )
    return feats, exceeded


def layer_info(session: PoliteSession, layer_url: str, *, log: FetchLog) -> dict[str, Any]:
    resp = session.get(layer_url, params={"f": "json"}, honour_robots=False)
    if resp.status_code != 200:
        raise HttpFailed(f"{layer_url} answered HTTP {resp.status_code}")
    log.add(url=resp.url, status=resp.status_code, bytes=len(resp.content), retrieved_at=now_iso())
    info = resp.json()
    if "error" in info:
        raise ParseError(f"{layer_url}: {info['error']}")
    return dict(info)


def query_geojson(
    session: PoliteSession,
    layer_url: str,
    *,
    log: FetchLog,
    where: str = "1=1",
    out_fields: str = "*",
    bbox: tuple[float, float, float, float] | None = None,
    page_size: int | None = None,
    max_allowable_offset: float | None = None,
) -> list[dict[str, Any]]:
    info = layer_info(session, layer_url, log=log)
    size = min(page_size or PAGE_SIZE, int(info.get("maxRecordCount") or PAGE_SIZE))
    params: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "f": "geojson",
        "returnGeometry": "true",
        "resultRecordCount": size,
        "orderByFields": info.get("objectIdField") or "OBJECTID",
    }
    if max_allowable_offset:
        # Server-side generalisation, in the output units (degrees for outSR=4326).
        params["maxAllowableOffset"] = max_allowable_offset
    if bbox is not None:
        params.update(
            geometry=",".join(str(v) for v in bbox),
            geometryType="esriGeometryEnvelope",
            inSR=4326,
            spatialRel="esriSpatialRelIntersects",
        )
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        params["resultOffset"] = page * size
        resp = session.get(f"{layer_url}/query", params=params, honour_robots=False)
        if resp.status_code != 200:
            raise HttpFailed(f"{layer_url}/query answered HTTP {resp.status_code}")
        feats, exceeded = parse_features(resp.content)
        log.add(
            url=resp.url,
            status=resp.status_code,
            bytes=len(resp.content),
            features=len(feats),
            retrieved_at=now_iso(),
        )
        out.extend(feats)
        if not exceeded and len(feats) < size:
            break
    else:
        raise HttpFailed(f"{layer_url}: more than {MAX_PAGES} pages")
    return out
