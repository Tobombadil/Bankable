"""`GET /v1/geo/regions` (docs/23 §3.1, ADR 0008): region polygons for the map.

Serves `MultiPolygon` GeoJSON features from vendored Census cartographic-boundary files
(`us_counties.geojson`, `us_states.geojson`) and a country outline file (`countries.geojson`) --
not computed, not inlined per proposal/asset feature (`GET /v1/proposals/geo`'s region features
carry only a representative point plus counts; the polygon a client fills in comes from here,
keyed by `region_id`). Each vendored file is loaded once per process and kept in a module-level
cache; `services/README.md`-style caveat: this cache is invalidated only by process restart, which
is fine for a file that changes on redeploy, not on write.

`REGIONS_DIR` (env var, default `data/vendored/regions`) points at the three files so tests never
depend on the real ~1.6 MB `us_counties.geojson` -- a test sets `REGIONS_DIR` to a tiny fixture
directory instead (`services/api/test_regions.py`).
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

from fastapi import APIRouter, Request

from services.api.errors import not_found, validation_error
from services.api.params import check_allowed, csv_param
from services.api.serialize import build_envelope, build_licence_summary, build_meta

router = APIRouter()

REGION_LEVELS = ("county", "state", "country")
MAX_IDS = 500

_FILENAMES = {
    "county": "us_counties.geojson",
    "state": "us_states.geojson",
    "country": "countries.geojson",
}


def _regions_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("REGIONS_DIR", "data/vendored/regions"))


class _RegionsCache:
    """Loaded GeoJSON features per `(regions_dir, level)`, decoded once. `regions_dir` is part of
    the key (not just `level`) so a test overriding `REGIONS_DIR` between cases never reads a
    previous test's cached fixture -- the process-wide production case only ever has one
    `regions_dir`, so this costs nothing there."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def get(self, level: str) -> list[dict[str, Any]]:
        directory = _regions_dir()
        key = (str(directory), level)
        with self._lock:
            cached = self._by_key.get(key)
            if cached is not None:
                return cached
        path = directory / _FILENAMES[level]
        if not path.exists():
            raise FileNotFoundError(str(path))
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
        features = list(doc.get("features") or [])
        with self._lock:
            self._by_key[key] = features
        return features

    def reset(self) -> None:
        """Test-only hook, mirroring every other process-local cache in this service
        (`services.api.context_routes._reset_plant_index_cache`,
        `services.api.ratelimit.default_limiter.reset`)."""
        with self._lock:
            self._by_key.clear()


_cache = _RegionsCache()


def _reset_regions_cache() -> None:
    _cache.reset()


@router.get("/v1/geo/regions")
def get_geo_regions(request: Request) -> Any:
    """`level` (required) selects the vendored file; `ids` (optional, csv, max 500) narrows the
    response to those `region_id`s -- omitted, every feature in that level's file is returned
    (bounded by the vendored file's own size: ~3,200 counties, 52 states, ~180 countries)."""
    check_allowed(request, {"level", "ids"})
    level = request.query_params.get("level")
    if level not in REGION_LEVELS:
        raise validation_error("level", f"level must be one of {REGION_LEVELS!r}", request.url.path)

    ids_param = request.query_params.get("ids")
    ids = csv_param(ids_param) if ids_param else None
    if ids is not None and len(ids) > MAX_IDS:
        raise validation_error("ids", f"at most {MAX_IDS} ids per request", request.url.path)

    try:
        features = _cache.get(level)
    except FileNotFoundError as exc:
        raise not_found(
            request.url.path,
            detail=f"no vendored region file for level={level!r} ({exc}); see REGIONS_DIR",
        ) from exc

    if ids is not None:
        id_set = set(ids)
        features = [f for f in features if f.get("properties", {}).get("region_id") in id_set]

    fc = {"type": "FeatureCollection", "features": features}
    meta = build_meta(lag_days=0)
    return build_envelope(fc, meta=meta, licence_summary=build_licence_summary([]))
