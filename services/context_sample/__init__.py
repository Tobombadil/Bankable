"""Prospecting layers at a point and along a corridor (docs/25 "Prospecting layers").

The functions the site finder and `GET /v1/context/sample` call:

    sample(lon, lat, *, layers=None) -> dict
        Each layer's value at the point (raster) or the polygons containing it, with provenance,
        the screening flag and the sentence of what the value is not.

    sample_line(geometry, buffer_mi, *, layers=None, spacing_m=None) -> dict
        The same layers summarised over the corridor within `buffer_mi` of a GeoJSON LineString /
        MultiLineString: a raster's mean/min/max/p10/p90 and data coverage, a polygon layer's share
        of the corridor and the features in it (largest share first), PAD-US's hard-constraint share,
        and `energy_community.share` across the two NETL layers.

    SITE_FINDER_LAYERS: the layer ids the site finder asks for per corridor segment.

Files are read from `services.context_sample.paths.prospect_dir()`; nothing here needs GDAL.
"""

from __future__ import annotations

from typing import Any

from services.context_sample.catalog import CATALOG, SITE_FINDER_LAYERS, LayerInfo
from services.context_sample.sampler import Sampler, default_sampler, reset_default_sampler

__all__ = [
    "CATALOG",
    "SITE_FINDER_LAYERS",
    "LayerInfo",
    "Sampler",
    "default_sampler",
    "reset_default_sampler",
    "sample",
    "sample_line",
]


def sample(lon: float, lat: float, *, layers: list[str] | None = None) -> dict[str, Any]:
    return default_sampler().sample(lon, lat, layers)


def sample_line(
    geometry: Any, buffer_mi: float, *, layers: list[str] | None = None, spacing_m: float | None = None
) -> dict[str, Any]:
    return default_sampler().sample_line(geometry, buffer_mi, layers, spacing_m=spacing_m)
