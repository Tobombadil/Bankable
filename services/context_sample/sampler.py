"""Sample the prospect layers at a point and along a corridor, with provenance on every value.

    sample(lon, lat, layers=None)                      -> dict   (GET /v1/context/sample serves this)
    sample_line(geometry, buffer_mi, layers=None)      -> dict   (the site finder calls this per segment)

Each layer in the answer carries: its value (raster) or the polygons that contain the point (polygons);
`screening: "screening"` and `not`, the one sentence of what the value is not (owner, 2026-10-22); and
`provenance` -- `source_id`, `source_url`, `retrieved_at`, `licence` (short name) and `licence_id`, the
dataset's own `vintage`, the credit line and licence URL, and for NLR's data the notice its terms require in
every copy. When either NETL layer is asked for, `energy_community` gives the combined IRA answer (at a point
`qualifies`; along a corridor `share`, each lattice point counted once), screening grade, not tax advice,
with the `source_id`, each layer's `vintage` and the dataset's own `dataset_as_of` date.

Statuses: `ok`; `partial` (a corridor only partly inside the area the layer was built for: shares are of the
whole band, so lower bounds, and `coverage_share` says how much is covered); `no_data` (no value here: a
raster's nodata, or a point or band outside the layer's coverage -- never read it as zero); `withheld` (the
source's row in `data/sources.yaml` does not allow publication: terms read and quoted, `reuse` not
restricted/unknown, `publication` not `none`, a `noncommercial` source only under the noncommercial
posture; no value and no credit); `unavailable` (the layer is not built on this server).
"""

from __future__ import annotations

import json
import math
import pathlib
import threading
import time
from typing import Any

import numpy as np

from services.context_sample.catalog import (
    CATALOG,
    ENERGY_COMMUNITY_NOT,
    SCREENING_GRADE,
    LayerInfo,
    layer,
)
from services.context_sample.geometry import METRES_PER_MILE, corridor_lattice, parse_lines
from services.context_sample.geotiff import GeoTiff
from services.context_sample.paths import manifest_path, prospect_dir
from services.context_sample.vectors import PolygonLayer

#: Polygon features listed per layer in a corridor answer (largest share first).
MAX_FEATURES = 10
#: Statuses whose answer carries a value (a `partial` value is a lower bound over the band).
SERVED = ("ok", "partial")


class LayerUnavailable(Exception):
    pass


def _source_gate(source_id: str) -> str | None:
    """None when the source may publish values; else the reason it may not."""
    from services.posture import platform_posture

    registry = _registry()
    if source_id not in registry:
        return f"{source_id} has no row in data/sources.yaml (terms not recorded)"
    entry = registry.get(source_id)
    if entry.reuse in ("restricted", "unknown"):
        return f"{source_id} is reuse: {entry.reuse} in data/sources.yaml"
    if (entry.publication or "none") == "none":
        return f"{source_id} is publication: none in data/sources.yaml"
    if entry.reuse == "noncommercial" and platform_posture() != "noncommercial":
        return f"{source_id} is noncommercial and the platform posture is commercial"
    return None


_REGISTRY: Any = None
_REGISTRY_LOCK = threading.Lock()


def _registry() -> Any:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            from pipeline.connectors.registry import Registry

            _REGISTRY = Registry()
        return _REGISTRY


def _round(value: float | None, decimals: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), decimals)


def _outside(handle: PolygonLayer) -> str:
    label = (handle.coverage or {}).get("label") or "its region"
    return f"outside the area this layer was built for ({label})"


class Sampler:
    """The layers in one prospect directory. Files are opened lazily and kept open."""

    def __init__(self, directory: pathlib.Path | None = None) -> None:
        self.directory = directory or prospect_dir()
        path = manifest_path(self.directory)
        self.manifest: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {"layers": {}}
        self.manifest_mtime = path.stat().st_mtime if path.exists() else None
        self._handles: dict[str, GeoTiff | PolygonLayer] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def entry(self, layer_id: str) -> dict[str, Any] | None:
        e = self.manifest.get("layers", {}).get(layer_id)
        return e if isinstance(e, dict) else None

    def available(self) -> list[str]:
        return [lid for lid in CATALOG if self.entry(lid) is not None]

    def _handle(self, layer_id: str) -> GeoTiff | PolygonLayer:
        with self._lock:
            h = self._handles.get(layer_id)
            if h is not None:
                return h
            e = self.entry(layer_id)
            if e is None:
                raise LayerUnavailable(layer_id)
            path = self.directory / str(e["file"])
            if not path.exists():
                raise LayerUnavailable(f"{layer_id}: {path.name} missing")
            h = GeoTiff(path) if CATALOG[layer_id].kind == "raster" else PolygonLayer(path)
            self._handles[layer_id] = h
            return h

    def _base(self, info: LayerInfo) -> dict[str, Any]:
        e = self.entry(info.layer_id) or {}
        return {
            "layer": info.layer_id,
            "title": info.title,
            "group": info.group,
            "kind": info.kind,
            "unit": info.unit,
            "screening": SCREENING_GRADE,
            "not": info.not_statement,
            "provenance": e.get("provenance"),
        }

    def _resolve(self, layers: list[str] | None) -> list[LayerInfo]:
        ids = layers if layers else list(CATALOG)
        return [layer(lid) for lid in ids]

    def _gate(self, info: LayerInfo, out: dict[str, Any]) -> bool:
        """Fill `out` with a non-ok status and return False when the layer cannot be served."""
        reason = _source_gate(info.source_id)
        if reason is not None:
            out.update(status="withheld", reason=reason, provenance=None)
            return False
        if self.entry(info.layer_id) is None:
            out.update(status="unavailable", reason="layer not built on this server")
            return False
        return True

    def _feature_props(self, info: LayerInfo, props: dict[str, Any]) -> dict[str, Any]:
        out = {name: props.get(name) for name in info.fields}
        if props.get("vintage") is not None:
            out["vintage"] = props.get("vintage")
        # A feature from another service than the layer's own source_url (NETL's FFE-only areas) says so.
        layer_url = ((self.entry(info.layer_id) or {}).get("provenance") or {}).get("source_url")
        if props.get("source_url") and props.get("source_url") != layer_url:
            out["source_url"] = props.get("source_url")
        return out

    # ------------------------------------------------------------------ point
    def sample(self, lon: float, lat: float, layers: list[str] | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        results = []
        ec_hits: dict[str, bool] = {}
        for info in self._resolve(layers):
            out = self._base(info)
            if self._gate(info, out):
                try:
                    handle = self._handle(info.layer_id)
                except LayerUnavailable as exc:
                    out.update(status="unavailable", reason=str(exc))
                else:
                    if isinstance(handle, GeoTiff):
                        value = _round(handle.value_at(lon, lat), info.decimals)
                        out.update(status="ok" if value is not None else "no_data", value=value)
                    else:
                        out.update(self._polygon_point(info, handle, lon, lat, ec_hits))
            results.append(out)
        answer: dict[str, Any] = {
            "point": {"lon": lon, "lat": lat},
            "screening": SCREENING_GRADE,
            "layers": results,
        }
        ec = _energy_community(results, ec_hits, key="qualifies")
        if ec is not None:
            answer["energy_community"] = ec
        answer["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return answer

    def _polygon_point(
        self, info: LayerInfo, handle: PolygonLayer, lon: float, lat: float, ec_hits: dict[str, bool]
    ) -> dict[str, Any]:
        feats = handle.containing(lon, lat)
        covered = handle.covered(np.array([lon]), np.array([lat]))
        if not feats and covered is not None and not bool(covered[0]):
            return {"status": "no_data", "reason": _outside(handle), "coverage": handle.coverage}
        out: dict[str, Any] = {
            "status": "ok",
            "inside": bool(feats),
            "features": [self._feature_props(info, f.props) for f in feats[:MAX_FEATURES]],
        }
        if info.hard_constraint_field:
            out["hard_constraint"] = any(bool(f.props.get(info.hard_constraint_field)) for f in feats)
        qualifying = [f for f in feats if _qualifies(info, f.props)]
        if info.qualifying_field:
            out["qualifies"] = bool(qualifying)
        if info.energy_community:
            ec_hits[info.layer_id] = bool(qualifying)
        return out

    # ------------------------------------------------------------------ corridor
    def sample_line(
        self,
        geometry: Any,
        buffer_mi: float,
        layers: list[str] | None = None,
        *,
        spacing_m: float | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        lines = parse_lines(geometry)
        lat = corridor_lattice(lines, buffer_mi, spacing_m=spacing_m)
        lons, lats = lat.lons, lat.lats
        n = lat.count
        results = []
        ec_masks: dict[str, Any] = {}
        for info in self._resolve(layers):
            out = self._base(info)
            if self._gate(info, out):
                try:
                    handle = self._handle(info.layer_id)
                except LayerUnavailable as exc:
                    out.update(status="unavailable", reason=str(exc))
                else:
                    if isinstance(handle, GeoTiff):
                        out.update(self._raster_summary(info, handle.values_at(lons, lats), n))
                    else:
                        out.update(self._polygon_summary(info, handle, lons, lats, n, ec_masks))
            results.append(out)
        # The corridor share in an energy community: a lattice point counts once, whether it is in a
        # coal-closure tract, a qualifying statistical area, or both (the layers overlap).
        union = np.zeros(n, dtype=bool)
        for mask in ec_masks.values():
            union |= mask
        ec = _energy_community(
            results,
            dict.fromkeys(ec_masks, True),
            key="share",
            value=round(int(union.sum()) / n, 4) if n else 0.0,
        )
        groups = f" in {lat.groups} groups of nearby parts" if lat.groups > 1 else ""
        answer: dict[str, Any] = {
            "buffer_mi": buffer_mi,
            "length_mi": round(lat.length_m / METRES_PER_MILE, 3),
            "sample_points": n,
            "spacing_m": round(lat.spacing_m, 1),
            "method": (
                f"Values over an even, equal-area lattice of {n} points at {lat.spacing_m:.0f} m spacing "
                f"within {buffer_mi} mi of the line{groups}; a polygon share is the share of lattice points "
                "inside it, a raster summary is over the lattice points with data, and coverage_share is "
                "the share of points inside the area the layer was built for."
            ),
            "screening": SCREENING_GRADE,
            "layers": results,
        }
        if ec is not None:
            answer["energy_community"] = ec
        answer["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return answer

    @staticmethod
    def _raster_summary(info: LayerInfo, values: np.ndarray[Any, Any], n: int) -> dict[str, Any]:
        valid = values[np.isfinite(values)]
        if n == 0 or valid.size == 0:
            return {"status": "no_data", "summary": {"coverage_share": 0.0}}
        d = info.decimals
        return {
            "status": "ok",
            "summary": {
                "mean": _round(float(valid.mean()), d),
                "min": _round(float(valid.min()), d),
                "max": _round(float(valid.max()), d),
                "p10": _round(float(np.percentile(valid, 10)), d),
                "p90": _round(float(np.percentile(valid, 90)), d),
                "coverage_share": round(valid.size / n, 4),
            },
        }

    def _polygon_summary(
        self,
        info: LayerInfo,
        handle: PolygonLayer,
        lons: Any,
        lats: Any,
        n: int,
        ec_masks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if n == 0:
            return {"status": "no_data", "share": 0.0, "features": []}
        members = handle.membership(lons, lats)
        any_inside = np.zeros(n, dtype=bool)
        hard = np.zeros(n, dtype=bool)
        qualifying = np.zeros(n, dtype=bool)
        feats = []
        for feature, mask in members:
            any_inside |= mask
            if info.hard_constraint_field and feature.props.get(info.hard_constraint_field):
                hard |= mask
            if _qualifies(info, feature.props):
                qualifying |= mask
            item = self._feature_props(info, feature.props)
            item["share"] = round(int(mask.sum()) / n, 4)
            feats.append(item)
        # A point inside one of the layer's own polygons is always covered. A file that records no coverage
        # (built before 2026-10-23) reports none rather than claim the whole band.
        covered = handle.covered(lons, lats)
        uncovered = 0 if covered is None else int((~(np.asarray(covered) | any_inside)).sum())
        if covered is not None and uncovered == n:
            return {
                "status": "no_data",
                "reason": _outside(handle),
                "coverage": handle.coverage,
                "coverage_share": 0.0,
            }
        feats.sort(key=lambda f: -float(f["share"]))
        out: dict[str, Any] = {
            "status": "partial" if uncovered else "ok",
            "share": round(int(any_inside.sum()) / n, 4),
            "features": feats[:MAX_FEATURES],
            "feature_count": len(feats),
        }
        if covered is not None:
            out["coverage_share"] = round((n - uncovered) / n, 4)
        if uncovered:
            out["reason"] = (
                f"{(n - uncovered) / n:.0%} of the band is inside the area this layer was built for "
                f"({(handle.coverage or {}).get('label')}); the shares are of the whole band, so lower bounds"
            )
        if info.hard_constraint_field:
            out["hard_constraint_share"] = round(int(hard.sum()) / n, 4)
        if info.qualifying_field:
            out["qualifying_share"] = round(int(qualifying.sum()) / n, 4)
        if info.energy_community and ec_masks is not None:
            ec_masks[info.layer_id] = qualifying
        return out


def _qualifies(info: LayerInfo, props: dict[str, Any]) -> bool:
    return bool(props.get(info.qualifying_field)) if info.qualifying_field else True


def _energy_community(
    results: list[dict[str, Any]], served: dict[str, Any], *, key: str, value: Any = None
) -> dict[str, Any] | None:
    """The combined IRA energy-community answer over the requested energy-community layers: at a point,
    `qualifies` (inside a coal-closure tract or adjoining tract, or a statistical area that is an energy
    community that year); along a corridor, `share` of the corridor in either. `partial` when only some
    of the layers could be served, or a corridor is only partly covered (the answer is then a lower
    bound). None when none was requested.

    It names what it was computed from: `source_id`, and per layer used the build's `vintage` and the
    dataset's own `dataset_as_of` date, so a reader sees which year's list the answer reflects (Treasury
    revises the statistical-area list every year)."""
    requested = [r for r in results if CATALOG[r["layer"]].energy_community]
    if not requested:
        return None
    used = [r for r in requested if r["layer"] in served and r.get("status") in SERVED]
    missing = [r["layer"] for r in requested if r not in used]
    sources = sorted({CATALOG[r["layer"]].source_id for r in requested})
    out: dict[str, Any] = {
        "screening": SCREENING_GRADE,
        "not": ENERGY_COMMUNITY_NOT,
        "source_id": sources[0] if len(sources) == 1 else ", ".join(sources),
        "layers": [r["layer"] for r in used],
    }
    if not used:
        out.update(status="unavailable", reason=f"no energy-community layer could be served: {missing}")
        return out
    out["vintage"] = {r["layer"]: (r.get("provenance") or {}).get("vintage") for r in used}
    out["dataset_as_of"] = {r["layer"]: (r.get("provenance") or {}).get("dataset_as_of") for r in used}
    partly = [r["layer"] for r in used if r.get("status") == "partial"]
    reasons = []
    if missing:
        reasons.append(f"not served: {missing}; the answer covers {[r['layer'] for r in used]} only")
    if partly:
        reasons.append(f"only part of the band is covered by {partly}")
    out["status"] = "partial" if reasons else "ok"
    if reasons:
        out["reason"] = "; ".join(reasons)
    out[key] = any(served[r["layer"]] for r in used) if value is None else value
    return out


_DEFAULT: Sampler | None = None
_DEFAULT_LOCK = threading.Lock()


def default_sampler() -> Sampler:
    """The process-wide sampler for `prospect_dir()`, reopened when its manifest changes on disk."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        path = manifest_path(prospect_dir())
        mtime = path.stat().st_mtime if path.exists() else None
        if _DEFAULT is None or _DEFAULT.directory != prospect_dir() or _DEFAULT.manifest_mtime != mtime:
            _DEFAULT = Sampler(prospect_dir())
        return _DEFAULT


def reset_default_sampler() -> None:
    """Test hook."""
    global _DEFAULT, _REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT = None
    with _REGISTRY_LOCK:
        _REGISTRY = None
