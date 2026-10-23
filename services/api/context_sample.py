"""`GET /v1/context/sample?lat=&lon=[&layers=]`: the prospecting layers at one point (docs/25 "Prospecting
layers"; docs/00-PLAN.md 2026-10-22 "Prospecting layers ... screening grade only").

Returns `services.context_sample.sample(lon, lat)` inside the attribution envelope: one entry per layer
with its value or containing polygons, `screening: "screening"`, `not` (the one sentence of what the
value is not) and `provenance` (`source_id`, `source_url`, `retrieved_at`, `licence`, `vintage`, credit,
licence URL, and NLR's notice for the solar layers). A layer whose source's terms do not allow publication
is `status: "withheld"`; a layer not built on this server is `status: "unavailable"`; neither carries a
value. A polygon layer at a point outside the area it was built for (PAD-US and NETL in New Mexico, in the
Texas build) is `status: "no_data"` with its `coverage`, never `inside: false`. The `energy_community` block
names its `source_id` and each layer's `dataset_as_of`. `licence_summary` lists every source a returned value
came from, with its credit line.

Public tier, no database: the sampler reads the compact files under `PROSPECT_DATA_DIR`. The site finder
calls `services.context_sample.sample_line` in process for corridors; this route answers single points
(map clicks, the API).
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Request

from services.api.errors import validation_error
from services.api.params import check_allowed, csv_param
from services.api.serialize import build_envelope, build_meta
from services.context_sample import CATALOG, default_sampler

router = APIRouter()

ALLOWED = {"lat", "lon", "layers"}


def _coordinate(request: Request, name: str, limit: float) -> float:
    raw = request.query_params.get(name)
    if raw is None or raw.strip() == "":
        raise validation_error(name, f"{name} is required", request.url.path)
    try:
        value = float(raw)
    except ValueError:
        raise validation_error(name, f"{name} must be a number", request.url.path) from None
    if not math.isfinite(value) or abs(value) > limit:
        raise validation_error(name, f"{name} must be between -{limit:g} and {limit:g}", request.url.path)
    return value


def licence_summary(result: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    """One entry per source that supplied a served value (status ok or no_data), with its credit."""
    by_source: dict[str, dict[str, Any]] = {}
    for item in result["layers"]:
        prov = item.get("provenance")
        if item.get("status") not in ("ok", "no_data") or not prov:
            continue
        sid = str(prov["source_id"])
        src = sources.get(sid) or {}
        entry = by_source.setdefault(
            sid,
            {
                "source_id": sid,
                "name": src.get("name") or sid,
                "operator": src.get("operator"),
                "licence_id": src.get("licence_id") or sid,
                "licence_name": item.get("licence_name")
                or src.get("licence_name")
                or "see provenance.licence",
                "licence_url": prov.get("licence_url") or src.get("licence_url"),
                "reuse_class": src.get("reuse") or "unknown",
                "attribution_text": prov.get("attribution") or src.get("attribution"),
                "requires_link_back": (src.get("reuse") in ("attribution", "noncommercial")),
                "record_count": 0,
                "retrieved_at_max": prov.get("retrieved_at"),
            },
        )
        entry["record_count"] += 1
        if prov.get("retrieved_at") and (entry["retrieved_at_max"] or "") < prov["retrieved_at"]:
            entry["retrieved_at_max"] = prov["retrieved_at"]
    names = [s["name"] for s in by_source.values()]
    return {
        "sources": list(by_source.values()),
        "attribution_line": f"Sources: {'; '.join(names)}." if names else "Sources: none in this response.",
        "redistribution": (
            "Screening-grade values with each source's credit and licence; see each layer's provenance and "
            "the sentence of what the value is not."
        ),
    }


@router.get("/v1/context/sample")
def get_context_sample(request: Request) -> Any:
    check_allowed(request, ALLOWED)
    lat = _coordinate(request, "lat", 90.0)
    lon = _coordinate(request, "lon", 180.0)
    layers = csv_param(request.query_params.get("layers"))
    if layers is not None:
        unknown = [lid for lid in layers if lid not in CATALOG]
        if unknown:
            raise validation_error(
                "layers", f"unknown layer(s) {unknown}; known: {sorted(CATALOG)}", request.url.path
            )
    sampler = default_sampler()
    result = sampler.sample(lon, lat, layers or None)
    for item in result["layers"]:
        entry = sampler.entry(item["layer"]) or {}
        if item.get("status") in ("ok", "no_data") and entry.get("licence_name"):
            item["licence_name"] = entry["licence_name"]
    result["layers_built"] = sampler.available()
    summary = licence_summary(result, dict(sampler.manifest.get("sources") or {}))
    return build_envelope(result, meta=build_meta(lag_days=0), licence_summary=summary)
