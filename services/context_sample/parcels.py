"""Read the Texas parcel layer a server pulled from the private object store (`python -m
pipeline.context.txgio_parcels pull`, with the server's `R2_*` credentials; source `us.tx.txgio.parcels`,
docs/25-siting-parcels.md). The files are never on a public host: the public reads parcels only through
the API, behind the register gate.

The pull writes `<data root>/normalized/context/us.tx.txgio.parcels/current.json` last, after every
county's files are in place under `<version>/parcels/<fips>.parquet` and `<version>/holdings/<fips>.parquet`.
`ParcelLayer` opens that manifest and each county's parcels file lazily. A read prunes row groups by the
statistics of the file's GeoParquet 1.1 bbox covering (the build writes rows in Z-order, 2,048 to a
group), then keeps the rows whose own box meets the query box. Every read of a shared Parquet file takes
that file's lock (one pyarrow `ParquetFile` read from several threads at once returns wrong rows;
`services/context_sample/vectors.py`). A changed `current.json` (a new pull) is noticed by its mtime.

The files hold no owner name or address (`pipeline/context/txgio_parcels.py`): what this module returns
is what the site finder may show.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped,unused-ignore]
import pyarrow.parquet as pq  # type: ignore[import-untyped,unused-ignore]

SOURCE_ID = "us.tx.txgio.parcels"
RELATIVE_DIR = pathlib.Path("normalized") / "context" / SOURCE_ID
CURRENT_NAME = "current.json"
BBOX_KEYS = ("xmin", "ymin", "xmax", "ymax")
#: What the map and the finder read; never the provenance columns row by row (they are the county's).
MAP_COLUMNS = (
    "parcel_key",
    "fips",
    "prop_id",
    "acres",
    "land_use",
    "holding_id",
    "holding_acres",
    "holding_parcels",
    "holding_kind",
)
FINDER_COLUMNS = (
    "fips",
    "acres",
    "holding_id",
    "holding_acres",
    "holding_parcels",
    "holding_land_use",
    "holding_value_per_acre",
    "holding_link_prop_id",
    "holding_kind",
    "holding_overlaps",
    "holding_overlap_rest_acres",
)

Box = tuple[float, float, float, float]


def parcels_dir(data_root: pathlib.Path | None = None) -> pathlib.Path:
    """`PARCELS_DATA_DIR`, else `<data root>/normalized/context/us.tx.txgio.parcels` (the data root is
    `INFRAQUE_DATA_DIR`, else the pipeline's)."""
    explicit = (os.environ.get("PARCELS_DATA_DIR") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    if data_root is None:
        env = os.environ.get("INFRAQUE_DATA_DIR")
        if env:
            data_root = pathlib.Path(env)
        else:
            from pipeline.connectors.store import DATA_DIR

            data_root = DATA_DIR
    return data_root / RELATIVE_DIR


def _meets(a: Box, b: Sequence[float]) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


class CountyFile:
    """One county's parcels file: row-group boxes from its statistics, reads under a lock."""

    def __init__(self, path: pathlib.Path) -> None:
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
            stats = [rg.column(leaf[f"bbox.{k}"]).statistics for k in BBOX_KEYS]
            if any(s is None or not s.has_min_max for s in stats):
                bounds.append((-np.inf, -np.inf, np.inf, np.inf))
            else:
                bounds.append((stats[0].min, stats[1].min, stats[2].max, stats[3].max))
        self.row_group_bounds = np.array(bounds, dtype=np.float64).reshape(-1, 4)
        self.num_rows = md.num_rows
        self.names = frozenset(self._pf.schema_arrow.names)
        self._lock = threading.Lock()

    def read(self, bbox: Box, columns: Sequence[str]) -> Any:
        """The rows whose box meets `bbox`, with those of `columns` the file has, `geometry` and the box
        (a file from an earlier layout lacks the newer columns; readers default them)."""
        x0, y0, x1, y1 = bbox
        b = self.row_group_bounds
        groups = np.nonzero((b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0))[0]
        wanted = [c for c in dict.fromkeys([*columns, "geometry", "bbox"]) if c in self.names]
        if not len(groups):
            return None
        with self._lock:
            table = self._pf.read_row_groups([int(g) for g in groups], columns=wanted)
        box = table.column("bbox").combine_chunks()
        xmin, ymin, xmax, ymax = (np.asarray(box.field(k)) for k in BBOX_KEYS)
        keep = (xmin <= x1) & (xmax >= x0) & (ymin <= y1) & (ymax >= y0)
        return table.filter(pa.array(keep))


@dataclass(frozen=True)
class CountyEntry:
    fips: str
    name: str
    parcels: int
    holdings: int
    bbox: tuple[float, float, float, float] | None
    source_url: str
    retrieved_at: str
    path: pathlib.Path


class ParcelLayer:
    """The pulled parcel layer of one server (module docstring)."""

    def __init__(self, directory: pathlib.Path) -> None:
        self.directory = directory
        self.manifest_path = directory / CURRENT_NAME
        self._lock = threading.Lock()
        self._files: dict[str, CountyFile] = {}
        self._mtime = -1.0
        self.manifest: dict[str, Any] = {}
        self.counties: dict[str, CountyEntry] = {}
        self._load()

    def _load(self) -> None:
        mtime = self.manifest_path.stat().st_mtime
        doc = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        counties: dict[str, CountyEntry] = {}
        for fips, entry in (doc.get("counties") or {}).items():
            files = entry.get("files") or {}
            local = (files.get("parcels") or {}).get("local")
            if not local:
                continue
            box = entry.get("bbox")
            counties[str(fips)] = CountyEntry(
                fips=str(fips),
                name=str(entry.get("name") or fips),
                parcels=int(entry.get("parcels") or 0),
                holdings=int(entry.get("holdings") or 0),
                bbox=tuple(float(v) for v in box) if isinstance(box, list) and len(box) == 4 else None,  # type: ignore[arg-type]
                source_url=str(entry.get("source_url") or ""),
                retrieved_at=str(entry.get("retrieved_at") or ""),
                path=self.directory / str(local),
            )
        self.manifest = doc
        self.counties = counties
        self._files = {}
        self._mtime = mtime

    def refresh(self) -> None:
        """Reopen when the pull replaced `current.json`."""
        try:
            mtime = self.manifest_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    self._load()

    @property
    def version(self) -> str | None:
        return self.manifest.get("version")

    def summary(self) -> dict[str, Any]:
        """What a site-finder run records about the layer it read (no owner field exists to record)."""
        vintage = self.manifest.get("vintage") or {}
        return {
            "source_id": SOURCE_ID,
            "version": self.version,
            "vintage": {k: vintage.get(k) for k in ("collection_id", "acquisition_date", "publication_date")},
            "counties": {
                f: {
                    "name": c.name,
                    "parcels": c.parcels,
                    "holdings": c.holdings,
                    "retrieved_at": c.retrieved_at,
                }
                for f, c in sorted(self.counties.items())
            },
            "group_distance_m": self.manifest.get("group_distance_m"),
        }

    def loaded(self, fips: str | None) -> bool:
        return bool(fips) and fips in self.counties

    def counties_in(self, bbox: Box) -> list[str]:
        return [f for f, c in sorted(self.counties.items()) if c.bbox is None or _meets(bbox, c.bbox)]

    def _file(self, fips: str) -> CountyFile:
        with self._lock:
            handle = self._files.get(fips)
            if handle is None:
                handle = CountyFile(self.counties[fips].path)
                self._files[fips] = handle
            return handle

    def read_bbox(
        self, bbox: Box, columns: Sequence[str] = FINDER_COLUMNS, counties: Iterable[str] | None = None
    ) -> Any:
        """Every parcel whose box meets `bbox` in the loaded counties (or those named), as one table."""
        tables = []
        for fips in counties if counties is not None else self.counties_in(bbox):
            if fips not in self.counties:
                continue
            table = self._file(fips).read(bbox, columns)
            if table is not None and table.num_rows:
                tables.append(table)
        if not tables:
            return None
        return pa.concat_tables(tables) if len(tables) > 1 else tables[0]


_LAYERS: dict[str, ParcelLayer | None] = {}
_LAYERS_LOCK = threading.Lock()


def default_layer(data_root: pathlib.Path | None = None) -> ParcelLayer | None:
    """The process's parcel layer for `data_root` (None when no pull has been made there)."""
    directory = parcels_dir(data_root)
    key = str(directory)
    with _LAYERS_LOCK:
        layer = _LAYERS.get(key)
        if layer is None:
            if not (directory / CURRENT_NAME).exists():
                return None
            layer = ParcelLayer(directory)
            _LAYERS[key] = layer
    layer.refresh()
    return layer


def clear_cache() -> None:
    with _LAYERS_LOCK:
        _LAYERS.clear()
