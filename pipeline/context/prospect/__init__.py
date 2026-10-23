"""Prospecting layers: resource, constraint, incentive and hazard data for the base map and the site
finder (docs/00-PLAN.md decision row of 2026-10-22 "Prospecting layers ... screening grade only";
docs/25 "Prospecting layers").

One module per source, each with the same three steps as the other context builders:

    fetch(...)      network, polite (`pipeline.connectors.http.PoliteSession`), a run record per fetch
    parse(...)      bytes or a recorded fixture -> plain Python structures, no network
    build(...)      -> one compact file per layer under the prospect data directory, with provenance

Rasters become cloud-optimised GeoTIFFs (`cog.py`), polygons GeoParquet (`geoparquet.py`); the
`build.py` CLI runs the sources for a region (Texas for development, CONUS in GitHub Actions) and
writes `layers.json`, the manifest `services/context_sample` reads. No module here serves anything:
`services/context_sample` samples the files and `GET /v1/context/sample` returns the values.
"""

from __future__ import annotations
