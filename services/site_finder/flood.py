"""Floodplain shares of the candidate bands, from the FEMA NFHL build (`pipeline/context/fema_nfhl.py`,
docs/25 §5; source `us.fema.nfhl`).

The manifest is the local mirror of `data/normalized/context/us.fema.nfhl/latest.json` under the data
root (`fema_nfhl.local_path` maps an object key to it). Each state's files are read at the keys the
manifest names for that state (`states[ST].files[layer].key`, its area of interest at
`states[ST].aoi.key`), never under `r2_prefix`, because a state carried forward by a partial rebuild
lives under an older version. Only the columns the finder uses are read.

The rule is the flood lane's `FloodLayer` (the reference implementation of the manifest's
`finder_rule`), built here from those columns. For each segment and each band half-width (0.25, 0.5,
1 and 2 miles) this module measures, in a plane centred on the segment's chunk:

- `cover`: the share of the band that has flood data: inside some state's area of interest
  intersected with that state's covering FIRM panels (`FloodLayer.mapped`), less any Zone D, "area
  not included" or unrecognised piece. Under 1, the band is "flood data unavailable" and the reason is
  the first rule it fails (`not_computed`, `no_digital_map`, or the zone's category), as in
  `FloodLayer.assess`.
- `share`: the share of the covered part inside the Special Flood Hazard Area.

The 1-mile band also records the shaded X (0.2 % annual chance) and open water it touches (reported,
never a reason to exclude: owner's instruction) and the FIRM panels under it.

This is a screening measure on FEMA's effective maps at the files' 10 m precision, not a flood
determination (44 CFR 65): every surface that shows it says so (`NOT_A_DETERMINATION`).
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from services.db.models import SITE_FINDER_DISTANCES
from services.site_finder.geometry import METRES_PER_MILE, LocalPlane

SOURCE_ID = "us.fema.nfhl"
MANIFEST_RELATIVE = pathlib.Path("normalized") / "context" / SOURCE_ID / "latest.json"
#: A band counts as having flood data when at least this share of it does (the rest is rounding at
#: the edge of a simplified area of interest).
FULL_COVER = 0.999
MAX_PANELS = 8
NOT_A_DETERMINATION = (
    "Screening from FEMA's effective National Flood Hazard Layer at 10 m precision; not an official "
    "flood determination (44 CFR 65). Letters of map amendment are not applied."
)
FEMA_VIEWER = "https://msc.fema.gov/portal/search?AddressQuery={lat:.6f}%2C{lon:.6f}"


def manifest_path(data_root: pathlib.Path) -> pathlib.Path:
    return data_root / MANIFEST_RELATIVE


@dataclass
class FloodSegment:
    share: dict[str, float | None]
    cover: dict[str, float]
    reason: str | None = None
    reported: list[str] = field(default_factory=list)
    panels: list[str] = field(default_factory=list)


def unavailable(reason: str, n: int) -> list[FloodSegment]:
    return [
        FloodSegment(
            share=dict.fromkeys(SITE_FINDER_DISTANCES),
            cover=dict.fromkeys(SITE_FINDER_DISTANCES, 0.0),
            reason=reason,
        )
        for _ in range(n)
    ]


class _Tiled:
    """A large polygon (a state's area of interest, its mapped area) cut into grid tiles once, so a
    chunk's clip touches a few small pieces instead of the whole state's outline."""

    def __init__(self, geom: Any, cell_deg: float = 0.25) -> None:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]
        from shapely.strtree import STRtree  # type: ignore[import-untyped,import-not-found,unused-ignore]

        self.geom = geom
        tiles: list[Any] = []
        if geom is not None and not geom.is_empty:
            x0, y0, x1, y1 = geom.bounds
            boxes = [
                shapely.box(x, y, x + cell_deg, y + cell_deg)
                for x in np.arange(math.floor(x0 / cell_deg) * cell_deg, x1, cell_deg)
                for y in np.arange(math.floor(y0 / cell_deg) * cell_deg, y1, cell_deg)
            ]
            cut = shapely.intersection(geom, np.asarray(boxes, dtype=object))
            tiles = [t for t in cut if not t.is_empty]
        self.tiles = np.asarray(tiles, dtype=object)
        self.tree = STRtree(self.tiles) if len(self.tiles) else None

    def clip(self, region: Any) -> Any:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        if self.tree is None:
            return shapely.Polygon()
        idx = self.tree.query(region, predicate="intersects")
        if len(idx) == 0:
            return shapely.Polygon()
        return shapely.intersection(shapely.union_all(self.tiles[idx]), region)


class FloodData:
    """One flood manifest and the states it holds; `layer_for` builds the rule for a set of them.

    Two caches keep a run from re-reading files: the layer of the last set of states asked for (the
    job measures lines grouped by that set, `compute.measuring_order`), and each state's decoded
    columns for the last `STATE_CACHE` states, so a border set ({TX, LA}) after {TX} reads only
    Louisiana. `layers_built` and `state_reads` count both, for the run's stats."""

    #: States whose decoded files are kept (a state and the neighbours its border lines reach).
    STATE_CACHE = 6

    def __init__(self, manifest: dict[str, Any], data_root: pathlib.Path) -> None:
        from collections import OrderedDict

        self.manifest = manifest
        self.data_root = data_root
        self.states = set(manifest.get("states") or {})
        self._cached: tuple[frozenset[str], Any] | None = None
        self._state_parts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.layers_built = 0
        self.state_reads = 0

    @classmethod
    def load(cls, data_root: pathlib.Path) -> FloodData | None:
        path = manifest_path(data_root)
        if not path.exists():
            return None
        return cls(json.loads(path.read_text(encoding="utf-8")), data_root)

    @property
    def version(self) -> str | None:
        v = self.manifest.get("version")
        return str(v) if v is not None else None

    def summary(self) -> dict[str, Any]:
        """What the run records about the flood build it read."""
        states = self.manifest.get("states") or {}
        return {
            "source_id": SOURCE_ID,
            "version": self.version,
            "built_at": self.manifest.get("built_at"),
            "states": {
                st: {
                    "version": e.get("version"),
                    "product": (e.get("product") or {}).get("name")
                    if isinstance(e.get("product"), dict)
                    else e.get("product"),
                    "source_url": self.state_url(st),
                    "retrieved_at": self.state_retrieved_at(st),
                    # Where the API reads this state's SFHA pieces to cut them out of a band.
                    "sfha_key": ((e.get("files") or {}).get("sfha") or {}).get("key"),
                }
                for st, e in sorted(states.items())
            },
            "finder_rule": self.manifest.get("finder_rule"),
        }

    def state_url(self, state: str) -> str:
        entry = (self.manifest.get("states") or {}).get(state) or {}
        for key in ("source_url", "url", "product_url"):
            if entry.get(key):
                return str(entry[key])
        product = entry.get("product")
        if isinstance(product, dict) and product.get("url"):
            return str(product["url"])
        return "https://msc.fema.gov/portal/advanceSearch"

    def state_retrieved_at(self, state: str) -> str | None:
        entry = (self.manifest.get("states") or {}).get(state) or {}
        for key in ("retrieved_at", "fetched_at", "built_at"):
            if entry.get(key):
                return str(entry[key])
        return str(self.manifest.get("built_at")) if self.manifest.get("built_at") else None

    def layer_for(self, states: Iterable[str]) -> Any:
        """The `FloodLayer` for these states (those the manifest holds), kept until a different set is
        asked for: the finder works state by state, so one set is held at a time."""
        wanted = frozenset(s for s in states if s in self.states)
        if not wanted:
            return None
        if self._cached is not None and self._cached[0] == wanted:
            return self._cached[1]
        self._cached = None  # release the previous set's polygons before reading the next
        layer = MeasuredLayer(self._read(wanted))
        self.layers_built += 1
        self._cached = (wanted, layer)
        return layer

    #: The columns the finder reads of each layer.
    COLUMNS: ClassVar[dict[str, list[str]]] = {
        "sfha": ["geometry", "category", "state"],
        "zones_other": ["geometry", "category", "state"],
        "coverage": ["geometry", "covers", "state", "firm_panel"],
    }

    def _state(self, state: str) -> dict[str, Any]:
        """One state's area of interest and decoded layers, read once while it stays in the cache."""
        hit = self._state_parts.get(state)
        if hit is not None:
            self._state_parts.move_to_end(state)
            return hit
        parts = self.read_state(state)
        self.state_reads += 1
        self._state_parts[state] = parts
        while len(self._state_parts) > self.STATE_CACHE:
            self._state_parts.popitem(last=False)
        return parts

    def read_state(self, state: str) -> dict[str, Any]:
        """One state's files, at the keys the manifest names for it: `aoi` (its area of interest),
        `tolerance_m`, and per layer of `COLUMNS` its (rows, geometries)."""
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        from pipeline.context.fema_nfhl import DEFAULT_TOLERANCE_M, aoi_for_state, local_path

        entry = self.manifest["states"][state]
        parts: dict[str, Any] = {
            "aoi": aoi_for_state(local_path(entry["aoi"]["key"], self.data_root), state),
            "tolerance_m": float(entry.get("tolerance_m") or DEFAULT_TOLERANCE_M),
        }
        for layer, cols in self.COLUMNS.items():
            table = pq.read_table(local_path(entry["files"][layer]["key"], self.data_root), columns=cols)
            parts[layer] = (
                table.drop_columns(["geometry"]).to_pylist(),
                list(shapely.from_wkb(table.column("geometry").to_pylist())),
            )
        return parts

    def _read(self, states: frozenset[str]) -> Any:
        from pipeline.context.fema_nfhl import DEFAULT_TOLERANCE_M, FloodLayer

        aoi: dict[str, Any] = {}
        layers: dict[str, tuple[list[dict[str, Any]], list[Any]]] = {k: ([], []) for k in self.COLUMNS}
        tolerance = DEFAULT_TOLERANCE_M
        for state in sorted(states):
            parts = self._state(state)
            aoi[state] = parts["aoi"]
            tolerance = max(tolerance, parts["tolerance_m"])
            for layer in self.COLUMNS:
                rows, geoms = layers[layer]
                rows.extend(parts[layer][0])
                geoms.extend(parts[layer][1])
        return FloodLayer(
            aoi=aoi,
            sfha=layers["sfha"],
            zones_other=layers["zones_other"],
            coverage=layers["coverage"],
            tolerance_m=tolerance,
        )


class MeasuredLayer:
    """A `FloodLayer` with what measuring many bands needs: its pieces as arrays, and its computed and
    mapped areas tiled."""

    def __init__(self, layer: Any) -> None:
        from pipeline.context.fema_nfhl import REPORTED_CATEGORIES, UNAVAILABLE_CATEGORIES

        self.layer = layer
        self.sfha = np.asarray(layer.sfha.geoms, dtype=object)
        self.other = np.asarray(layer.other.geoms, dtype=object)
        self.other_category = np.asarray([r.get("category") for r in layer.other.rows], dtype=object)
        self.unavailable_mask = np.isin(self.other_category, list(UNAVAILABLE_CATEGORIES))
        self.reported_mask = np.isin(self.other_category, list(REPORTED_CATEGORIES))
        self.mapped = _Tiled(layer.mapped)

    def measure(self, plane: LocalPlane, lines_xy: Sequence[Any]) -> list[FloodSegment]:
        """Shares and cover for each segment (a shapely LineString in `plane`'s metres)."""
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        from pipeline.context.fema_nfhl import UNAVAILABLE_CATEGORIES

        n = len(lines_xy)
        lines = np.asarray(lines_xy, dtype=object)
        bufs = {
            k: shapely.buffer(lines, d * METRES_PER_MILE, quad_segs=8)
            for k, d in SITE_FINDER_DISTANCES.items()
        }
        areas = {k: shapely.area(b) for k, b in bufs.items()}
        region_xy = shapely.union_all(bufs["200"])
        region_deg = plane.to_deg(region_xy)
        layer = self.layer
        if not layer.computed.intersects(region_deg):
            return unavailable("not_computed", n)
        full = bool(layer.mapped.covers(region_deg))
        cover_geom = region_xy if full else plane.to_xy(self.mapped.clip(region_deg))
        other_idx = layer.other.tree.query(region_deg, predicate="intersects") if layer.other.tree else []
        other_idx = np.asarray(other_idx, dtype=np.int64)
        unav_idx = other_idx[self.unavailable_mask[other_idx]] if len(other_idx) else other_idx
        if len(unav_idx):
            cover_geom = shapely.difference(cover_geom, plane.to_xy(shapely.union_all(self.other[unav_idx])))
        whole = full and not len(unav_idx)
        sf_idx = layer.sfha.tree.query(region_deg, predicate="intersects") if layer.sfha.tree else []
        sfha_xy = None
        if len(sf_idx):
            clipped = shapely.intersection(self.sfha[np.asarray(sf_idx)], region_deg)
            sfha_xy = plane.to_xy(shapely.union_all(clipped))
            shapely.prepare(sfha_xy)
        if not whole:
            shapely.prepare(cover_geom)
        results = [FloodSegment(share={}, cover={}) for _ in range(n)]
        for key, buf in bufs.items():
            covered = areas[key] if whole else shapely.area(shapely.intersection(buf, cover_geom))
            flooded = shapely.area(shapely.intersection(buf, sfha_xy)) if sfha_xy is not None else np.zeros(n)
            for i in range(n):
                total = float(areas[key][i])
                c = float(covered[i])
                results[i].cover[key] = round(min(1.0, c / total), 4) if total > 0 else 0.0
                results[i].share[key] = round(min(1.0, float(flooded[i]) / c), 4) if c > 0 else None
        # Reported zones and FIRM panels under the 1-mile band; the reason the 2-mile band lacks data.
        buf1_deg = np.asarray([plane.to_deg(b) for b in bufs["100"]], dtype=object)
        rep_idx = other_idx[self.reported_mask[other_idx]] if len(other_idx) else other_idx
        for i in range(n):
            if len(rep_idx):
                hits = shapely.intersects(buf1_deg[i], self.other[rep_idx])
                results[i].reported = sorted({str(c) for c in self.other_category[rep_idx][hits]})
            if layer.panels.tree is not None:
                p_idx = layer.panels.tree.query(buf1_deg[i], predicate="intersects")
                results[i].panels = sorted({str(layer.panels.rows[int(j)].get("firm_panel")) for j in p_idx})[
                    :MAX_PANELS
                ]
            if results[i].cover["200"] < FULL_COVER:
                buf2_deg = plane.to_deg(bufs["200"][i])
                if not layer.computed.covers(buf2_deg):
                    results[i].reason = "not_computed"
                elif not layer.mapped.covers(buf2_deg):
                    results[i].reason = "no_digital_map"
                else:
                    cats = {str(c) for c in self.other_category[unav_idx]} if len(unav_idx) else set()
                    results[i].reason = next(
                        (c for c in UNAVAILABLE_CATEGORIES if c in cats), "no_digital_map"
                    )
        return results


class SfhaPieces:
    """A state's SFHA pieces by bounding box, read on request to cut the floodplain out of a band
    (`services/api/site_finder.py`). Row groups are pruned by the statistics of the GeoParquet `bbox`
    covering (the flood files are in Z-order, so a band touches a few groups); only `geometry` and
    `bbox` are read; decoded groups are cached per process. One `ParquetFile` is not safe to read from
    several threads at once (`services/context_sample/vectors.py`), so reads take a lock."""

    CACHE_GROUPS = 256

    def __init__(self, path: pathlib.Path) -> None:
        import threading
        from collections import OrderedDict

        import pyarrow.parquet as pq

        self.path = path
        self._pf = pq.ParquetFile(path)
        md = self._pf.metadata
        leaf = (
            {md.row_group(0).column(i).path_in_schema: i for i in range(md.num_columns)}
            if md.num_row_groups
            else {}
        )
        bounds = []
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            stats = [rg.column(leaf[f"bbox.{k}"]).statistics for k in ("xmin", "ymin", "xmax", "ymax")]
            if any(s is None or not s.has_min_max for s in stats):
                bounds.append((-np.inf, -np.inf, np.inf, np.inf))
            else:
                bounds.append((stats[0].min, stats[1].min, stats[2].max, stats[3].max))
        self.bounds = np.asarray(bounds, dtype=np.float64).reshape(-1, 4)
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, tuple[Any, Any]] = OrderedDict()

    def _group(self, g: int) -> tuple[Any, Any]:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        with self._lock:
            hit = self._cache.get(g)
            if hit is not None:
                self._cache.move_to_end(g)
                return hit
            table = self._pf.read_row_group(g, columns=["geometry", "bbox"])
        geoms = shapely.from_wkb(table.column("geometry").to_pylist())
        box = table.column("bbox").combine_chunks()
        boxes = np.column_stack([box.field(k).to_numpy() for k in ("xmin", "ymin", "xmax", "ymax")])
        with self._lock:
            self._cache[g] = (geoms, boxes)
            if len(self._cache) > self.CACHE_GROUPS:
                self._cache.popitem(last=False)
        return geoms, boxes

    def within(self, bbox: tuple[float, float, float, float]) -> list[Any]:
        x0, y0, x1, y1 = bbox
        b = self.bounds
        groups = np.nonzero((b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0))[0]
        out: list[Any] = []
        for g in groups:
            geoms, boxes = self._group(int(g))
            keep = (boxes[:, 0] <= x1) & (boxes[:, 2] >= x0) & (boxes[:, 1] <= y1) & (boxes[:, 3] >= y0)
            out.extend(geoms[keep])
        return out
