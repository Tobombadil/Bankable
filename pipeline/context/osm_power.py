"""The US high-voltage network from OpenStreetMap -> `transmission_line` and `substation` asset rows
(owner decisions 2026-10-22: the powered-land site finder, and substations from OpenStreetMap, public
now; docs/25 §2.7; docs/13 §6 row `us.osm.power`; `data/sources.yaml` `us.osm.power`).

**Why OpenStreetMap.** The line layer the platform had (LBNL's FERC Form 1 x HIFLD subset,
`pipeline/context/lbnl_transmission.py`, CC BY 4.0) covers about a fifth of US transmission
mileage, and no openly published national substation layer exists outside OpenStreetMap: DHS
classifies the HIFLD substation layer "restricted public", and that layer and anything derived from
it are never used (docs/13 §2.19). OpenStreetMap's power features are mapped independently from
imagery and survey and are published under the Open Database License 1.0. OpenInfraMap is a renderer
of the same data and is never scraped; this module reads OpenStreetMap's own data, as Geofabrik
extracts it.

**What is kept.**

* Lines: ways tagged ``power=line`` or ``power=cable`` (a cable is underground or submarine and is
  flagged ``underground``, unless its ``location`` says ``overhead``: an aerial cable) whose highest
  stated voltage is 69 kV or more, plus every such way that
  states no usable voltage (``voltage_unknown``), so the site finder can include them on request.
  ``line=busbar`` and ``line=bay`` ways are substation internals, not lines, and are dropped.
  ``voltage`` is parsed by :func:`parse_voltage`: OpenStreetMap writes volts, ``;`` separates the
  circuits of a multi-circuit line (``"138000;69000"`` -> highest 138 kV, all values kept, a repeated
  one too: ``"138000;138000"`` -> ``[138, 138]``), and
  mappers also write ``"138 kV"``/``"138kV"``, a comma list (``"12470,69000"``), a thousands mark
  (``"138,000"``) and a decimal comma (``"12,47 kV"``). Kept tags: ``operator``, ``name``, ``ref``,
  ``circuits``, ``cables``, ``frequency``, ``location``, ``line`` and the raw ``voltage``.
* Substations: ``power=substation`` on a node (a point) or an area (a closed way or a multipolygon
  relation: the centroid is the point, the outline is kept), with ``voltage``, ``name``,
  ``operator``, ``ref`` and the ``substation=*`` subtype. ``substation=traction`` (railway feeders)
  and ``substation=minor_distribution`` (pole and pad transformers) are dropped; every other subtype
  and untagged ones are kept, subtype recorded, so the finder can filter. No voltage floor applies
  to substations: a distribution substation fed at 69 kV and up is what a large load connects to.
  A substation mapped twice, as a closed way and as the multipolygon relation whose only outer is
  that way, is one substation: the relation is kept (with any tag only the way states) and the way
  is not.

**Pipeline.** Three stages, each a CLI sub-command:

1. ``build`` (GitHub Actions, `infra/scripts/build_grid_layers.sh`; or a developer with one state's
   file): for each Geofabrik state extract, download (one request per `GEOFABRIK_MIN_INTERVAL_S`,
   robots honoured; checked against Geofabrik's ``.md5`` and the Content-Length, and a failed
   transfer retried for that state alone, `download`), filter to the power features and the nodes
   and members they reference with pyosmium (`filter_power_file`), delete the raw extract, read the
   filtered file with node
   locations and area assembly (`read_power_file`), and keep the records. An element in two
   extracts (a way across a state line, or one Geofabrik's slightly generous state polygons both
   hold) is kept once, and every extract that held it is recorded (see "Regions" below).
   Writes compact GeoParquet (`lines.parquet`, `substations.parquet`: WKB geometry, zstd) and a
   `manifest.json` (per-state extract timestamps, counts, sha256). These files are the derived
   database: they are published as they are, under the ODbL (docs/25 §2.7).
2. ``pull`` (the server's monthly `context_build`): fetch ``latest.json`` and the two files it
   names from the public R2 prefix the build uploaded to, check their sha256, and write the
   asset-shaped frames `normalized/context/us.osm.power.lines.parquet` and
   `us.osm.power.substations.parquet`, which `services/ingest/context_layers.py` loads. Only the
   states ``GRID_LAYERS_REGIONS`` names (``US-TX``, or ``all``); unset is refused
   (`default_regions`). The frames and their manifest copy are renamed into place together, one
   ``frame_set`` token on all three (`write_asset_frames`).
3. ``normalise``: GeoParquet -> those asset frames, without the network (what ``pull`` calls, and
   what a developer runs after ``build``).

**Provenance per record.** ``source_id`` `us.osm.power`; ``source_url`` the element's own page
(``https://www.openstreetmap.org/way/<id>``); ``retrieved_at`` the extract's replication timestamp
(the moment the OpenStreetMap data it holds is current to, from the PBF header), not the download
time; ``licence`` the manifest's quoted ODbL terms. ``source_asset_id`` is ``way/<id>`` (or
``node/``, ``relation/``), stable across extracts, which is what makes a reload idempotent and lets
the loader withdraw an element that a later extract of the same state no longer has.

**Regions.** Every row carries four region fields, written by the build:

* ``region``: the state the element is in, from the vendored state polygons
  (`STATES_GEOJSON`) at the line's representative point or the substation's point, falling back to
  the extract that held it when the point is in no polygon (offshore). This is the row's
  ``state_code``.
* ``states_crossed``: every state a vertex of the line falls in, plus ``region``.
* ``extract_regions``: every Geofabrik extract that held the element.
* ``scope_regions``: ``states_crossed`` and ``extract_regions`` together. **One rule** uses it:
  a load of regions *W* (``GRID_LAYERS_REGIONS``) holds exactly the rows whose ``scope_regions``
  meet *W* (`write_asset_frames`), the loader withdraws only rows whose ``scope_regions`` meet *W*
  (`services/ingest/osm_power.py`), and the manifest's per-region counts are counted the same way,
  so the promotion gate compares what a load would get. A way crossing the Red River is in a
  US-TX load whichever state labels it, and so is a way wholly inside Texas that the vendored
  polygons (Census 1:20m, about 550 vertices for Texas) misplace across the river, because the
  Texas extract holds it. On each asset row, ``coverage_regions`` is ``scope_regions`` narrowed to
  the regions the load covered: the map's source rule reads it to know where OpenStreetMap's
  network is complete (`services/api/assets.py`, `MAP_LINE_SOURCE_PREFERENCE`).

**Geometry stored on the asset.** The GeoParquet keeps OpenStreetMap's own coordinates (seven
decimals). The asset row's line is Douglas-Peucker simplified at `ASSET_SIMPLIFY_DEG` (about 5 m)
and written at six decimals: tower-to-tower vertices along a straight run carry nothing a map or the
site finder can use, and the line index `services/api/assets.py` holds in memory scales with vertex
count (docs/25 §2.7 measures it on Texas). Every endpoint and every bend over 5 m is kept.

**Operators are shown, not linked.** ``operator`` is free text ("Oncor", "Oncor Electric
Delivery", "AEP Texas Central"); handing it to the organisation resolver would mint duplicates of
utilities the registry already holds, as LBNL's owner strings would have (docs/25 §2.2). It rides in
``attributes.operator`` and ``operator_name`` stays empty, so the edge loader writes nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import struct
import time
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped,unused-ignore]
import pyarrow.compute as pc  # type: ignore[import-untyped,unused-ignore]
import pyarrow.parquet as pq  # type: ignore[import-untyped,unused-ignore]

from pipeline.connectors.store import DATA_DIR
from pipeline.context.eia_atlas import ASSET_COLUMNS
from pipeline.context.geo import (
    StateIndex,
    geodesic_length_miles,
    representative_point,
    simplify,
    wkt_multilinestring,
)
from pipeline.context.lbnl_transmission import voltage_class

ROOT = pathlib.Path(__file__).resolve().parents[2]
NORMALIZED_DIR = DATA_DIR / "normalized" / "context"
STATES_GEOJSON = ROOT / "data" / "vendored" / "regions" / "us_states.geojson"

SOURCE_ID = "us.osm.power"
LINE_ASSET_TYPE = "transmission_line"
SUBSTATION_ASSET_TYPE = "substation"
LINES_FILE = f"{SOURCE_ID}.lines.parquet"
SUBSTATIONS_FILE = f"{SOURCE_ID}.substations.parquet"
#: The published GeoParquet names inside one version prefix (`infra/scripts/build_grid_layers.sh`).
GEO_LINES_NAME = "lines.parquet"
GEO_SUBSTATIONS_NAME = "substations.parquet"
MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"
#: Bumped when a published column changes meaning; part of the R2 key, so a reader of an older
#: layout never sees a newer one under the same name.
LAYOUT_VERSION = "v1"
#: The asset frames' own manifest copy, beside them in `normalized/context/`.
FRAMES_MANIFEST_FILE = f"{SOURCE_ID}.manifest.json"
#: Parquet key-value metadata on each asset frame, and the manifest copy's ``frame_set``: one token
#: per `write_asset_frames` call, so the loader can tell three files written together from a mix
#: of two writes (`services/ingest/osm_power.py`, `frames_consistent`).
FRAME_SET_KEY = "frame_set"

OSM_ELEMENT_URL = "https://www.openstreetmap.org/{osm_type}/{osm_id}"
GEOFABRIK_BASE = "https://download.geofabrik.de/north-america/us"
#: Geofabrik states no request rate (docs/25 §2.7: its download server's own notes could not be read
#: from the lane's sandbox); one state file every five seconds, sequentially, is the platform's
#: polite default for a bulk host (GDELT's 1 per 5 s).
GEOFABRIK_MIN_INTERVAL_S = 5.0

#: The voltage floor of the site finder's "high voltage" (owner, 2026-10-22).
MIN_KV = 69.0
LINE_POWER_VALUES = ("line", "cable")
#: `line=*` values that mark a substation's internal conductors, not a line between places.
NON_LINE_LINE_VALUES = frozenset({"busbar", "bay"})
DROPPED_SUBSTATION_TYPES = frozenset({"traction", "minor_distribution"})
#: Tags carried onto each line / substation record (the raw strings; parsed fields sit beside them).
LINE_TAGS = ("operator", "name", "ref", "circuits", "cables", "frequency", "location", "line", "voltage")
SUBSTATION_TAGS = ("substation", "operator", "name", "ref", "voltage")

#: Douglas-Peucker tolerance for the asset row's stored line, in degrees (~5 m at the equator, less
#: east-west at US latitudes). Module docstring, "Geometry stored on the asset".
ASSET_SIMPLIFY_DEG = 0.00005
COORD_PRECISION = 6

#: Geofabrik's US state extracts, `<GEOFABRIK_BASE>/<slug>-latest.osm.pbf`, with the ISO 3166-2
#: code each one covers. Order matters only for ways in two extracts (module docstring).
US_STATE_EXTRACTS: tuple[tuple[str, str], ...] = (
    ("US-AL", "alabama"),
    ("US-AK", "alaska"),
    ("US-AZ", "arizona"),
    ("US-AR", "arkansas"),
    ("US-CA", "california"),
    ("US-CO", "colorado"),
    ("US-CT", "connecticut"),
    ("US-DE", "delaware"),
    ("US-DC", "district-of-columbia"),
    ("US-FL", "florida"),
    ("US-GA", "georgia"),
    ("US-HI", "hawaii"),
    ("US-ID", "idaho"),
    ("US-IL", "illinois"),
    ("US-IN", "indiana"),
    ("US-IA", "iowa"),
    ("US-KS", "kansas"),
    ("US-KY", "kentucky"),
    ("US-LA", "louisiana"),
    ("US-ME", "maine"),
    ("US-MD", "maryland"),
    ("US-MA", "massachusetts"),
    ("US-MI", "michigan"),
    ("US-MN", "minnesota"),
    ("US-MS", "mississippi"),
    ("US-MO", "missouri"),
    ("US-MT", "montana"),
    ("US-NE", "nebraska"),
    ("US-NV", "nevada"),
    ("US-NH", "new-hampshire"),
    ("US-NJ", "new-jersey"),
    ("US-NM", "new-mexico"),
    ("US-NY", "new-york"),
    ("US-NC", "north-carolina"),
    ("US-ND", "north-dakota"),
    ("US-OH", "ohio"),
    ("US-OK", "oklahoma"),
    ("US-OR", "oregon"),
    ("US-PA", "pennsylvania"),
    ("US-PR", "puerto-rico"),
    ("US-RI", "rhode-island"),
    ("US-SC", "south-carolina"),
    ("US-SD", "south-dakota"),
    ("US-TN", "tennessee"),
    ("US-TX", "texas"),
    ("US-UT", "utah"),
    ("US-VT", "vermont"),
    ("US-VA", "virginia"),
    ("US-WA", "washington"),
    ("US-WV", "west-virginia"),
    ("US-WI", "wisconsin"),
    ("US-WY", "wyoming"),
)


class BuildError(RuntimeError):
    """A build or pull that must not publish: a state failed, a checksum did not match, a file is
    not what the manifest says."""


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------- voltage
#: One voltage value once its commas are resolved (`_comma_values`): a number with an optional
#: ``.`` decimal part, then an optional unit.
_VALUE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(kv|v|k)?$", re.IGNORECASE)
_LEADING_DIGITS_RE = re.compile(r"^\d+")
#: OpenStreetMap writes volts, so a bare number of 1,000 or more is volts. Under it, a bare number is
#: read as kilovolts only when it is a North American transmission nominal (`KV_NOMINALS`: ``"138"``
#: is a 138 kV line, no line runs at 138 V) or has a decimal part (``"34.5"``, ``"12.47"``: nobody
#: writes 34.5 V). Any other bare integer under it is volts: ``"240"``, ``"208"`` and ``"480"`` are
#: service drops mis-tagged ``power=line`` (29 ways in the Texas build of 2026-10-22, every bare
#: integer there), which read as kilovolts would have shown as 230 kV- and 500 kV-class lines.
BARE_KV_BELOW = 1000.0
#: The US transmission nominal voltages in kV, which mappers sometimes write without a unit.
KV_NOMINALS = frozenset({69.0, 115.0, 138.0, 161.0, 230.0, 287.0, 345.0, 500.0, 765.0})


@dataclass(frozen=True)
class Voltage:
    """A parsed ``voltage=*`` tag: every stated value in kV (repeats kept, one per value as tagged),
    highest first, and how it was read."""

    values_kv: tuple[float, ...] = ()
    raw: str | None = None
    #: A bare number under `BARE_KV_BELOW` was read as kilovolts (a nominal, or a decimal).
    assumed_kv: bool = False

    @property
    def max_kv(self) -> float | None:
        return self.values_kv[0] if self.values_kv else None

    @property
    def unknown(self) -> bool:
        return not self.values_kv


def _kv(value: float) -> float:
    return round(value, 3)


def _comma_values(part: str) -> list[str]:
    """One ``;``/``/``-separated part -> its values, each comma resolved by what surrounds it:

    * a **list separator** when the text before it is not a bare integer (``"138 kV,69 kV"``),
      when nothing numeric follows, when four or more digits follow (``"12470,69000"``,
      ``"138000,69000"``), or when the integer before it is already 1,000 or more (volts:
      ``"13800,480"``). Four or more digits after it covers "1,000 or more on both sides";
    * a **thousands mark** when exactly three digits follow a bare integer of one to three digits,
      or a number that already took a group (``"138,000"`` -> 138000, ``"1,000,000"``);
    * else a **decimal mark** (``"12,47 kV"`` -> 12.47, ``"13,8"`` -> 13.8).
    """
    pieces = [p.strip() for p in part.split(",")]
    values: list[str] = []
    current, grouped = pieces[0], False
    for piece in pieces[1:]:
        lead = _LEADING_DIGITS_RE.match(piece)
        digits = len(lead.group(0)) if lead else 0
        bare = current.isdigit()
        if bare and digits == 3 and (grouped or len(current) <= 3):
            current, grouped = current + piece, True
        elif not bare or digits == 0 or digits >= 4 or grouped or int(current) >= 1000:
            values.append(current)
            current, grouped = piece, False
        else:
            current, grouped = f"{current}.{piece}", False
    values.append(current)
    return values


def parse_voltage(raw: object) -> Voltage:
    """``voltage=*`` -> :class:`Voltage`. ``;`` separates circuits (OpenStreetMap's multi-value
    convention); ``/`` is read the same way because mappers use it for the same meaning
    (``"138000/69000"``), and a comma is a separator, a thousands mark or a decimal mark by what
    surrounds it (`_comma_values`). A part that is not a number with an optional ``V``/``kV`` unit
    (``"high"``, ``"unknown"``, ``"13.8 kV + 69 kV"``) is skipped; a tag with no readable part is
    ``unknown``. A bare number is volts unless `BARE_KV_BELOW` says otherwise. Every value is
    kept, a repeated one too (``"138000;138000"`` is two 138 kV circuits -> ``(138, 138)``), sorted
    highest first; `raw` keeps the tag as written, in its own order."""
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return Voltage()
    text = str(raw).strip()
    if not text:
        return Voltage()
    found: list[float] = []
    assumed = False
    values = (value for part in re.split(r"[;/]", text) for value in _comma_values(part.strip()))
    for value in values:
        m = _VALUE_RE.match(value.strip())
        if not m:
            continue
        number = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("kv", "k"):
            kv = number
        elif unit == "v":
            kv = number / 1000.0
        elif number < BARE_KV_BELOW and (number in KV_NOMINALS or not number.is_integer()):
            kv, assumed = number, True
        else:
            kv = number / 1000.0
        if kv > 0:
            found.append(_kv(kv))
    return Voltage(tuple(sorted(found, reverse=True)), text, assumed and bool(found))


# ----------------------------------------------------------------------------------- tag rules
def line_kept(tags: dict[str, str]) -> tuple[bool, str]:
    """`(kept, reason)` for a way's tags: kept when it is a ``power=line``/``power=cable`` between
    places (not a busbar or bay) whose highest voltage is 69 kV or more, or whose voltage is
    unknown. `reason` names the rule that decided, for the manifest's drop counts."""
    if tags.get("power") not in LINE_POWER_VALUES:
        return False, "not_a_line"
    if (tags.get("line") or "").strip().lower() in NON_LINE_LINE_VALUES:
        return False, "busbar_or_bay"
    v = parse_voltage(tags.get("voltage"))
    if v.unknown:
        return True, "voltage_unknown"
    if (v.max_kv or 0.0) < MIN_KV:
        return False, "below_69kv"
    return True, "kept"


def substation_kept(tags: dict[str, str]) -> tuple[bool, str]:
    if tags.get("power") != "substation":
        return False, "not_a_substation"
    subtype = (tags.get("substation") or "").strip().lower()
    if subtype in DROPPED_SUBSTATION_TYPES:
        return False, subtype
    return True, "kept"


#: ``location=*`` values that settle whether a line or cable is buried, whatever ``power`` says.
UNDERGROUND_LOCATIONS = frozenset({"underground", "underwater"})
OVERHEAD_LOCATIONS = frozenset({"overhead", "overground"})


def is_underground(tags: dict[str, str]) -> bool:
    """An explicit ``location`` wins: ``underground``/``underwater`` is buried (a ``power=line`` so
    tagged is mis-tagged but means the same), ``overhead``/``overground`` is not (an aerial cable,
    ``power=cable`` + ``location=overhead``). Without one, a ``power=cable`` is underground or
    submarine, as OpenStreetMap defines it, and a ``power=line`` is overhead."""
    location = (tags.get("location") or "").strip().lower()
    if location in UNDERGROUND_LOCATIONS:
        return True
    if location in OVERHEAD_LOCATIONS:
        return False
    return tags.get("power") == "cable"


# ----------------------------------------------------------------------------------- WKB I/O
_WKB_POINT, _WKB_LINESTRING, _WKB_POLYGON, _WKB_MULTILINESTRING, _WKB_MULTIPOLYGON = 1, 2, 3, 5, 6
Ring = list[tuple[float, float]]
PolygonRings = list[Ring]


def _wkb_coords(coords: Sequence[Sequence[float]]) -> bytes:
    return struct.pack("<I", len(coords)) + b"".join(struct.pack("<dd", c[0], c[1]) for c in coords)


def wkb_point(lon: float, lat: float) -> bytes:
    return struct.pack("<BIdd", 1, _WKB_POINT, lon, lat)


def wkb_linestring(coords: Sequence[Sequence[float]]) -> bytes:
    return struct.pack("<BI", 1, _WKB_LINESTRING) + _wkb_coords(coords)


def _wkb_polygon_body(rings: Sequence[Sequence[Sequence[float]]]) -> bytes:
    return struct.pack("<I", len(rings)) + b"".join(_wkb_coords(r) for r in rings)


def wkb_polygon(rings: Sequence[Sequence[Sequence[float]]]) -> bytes:
    return struct.pack("<BI", 1, _WKB_POLYGON) + _wkb_polygon_body(rings)


def wkb_multipolygon(polygons: Sequence[Sequence[Sequence[Sequence[float]]]]) -> bytes:
    return struct.pack("<BII", 1, _WKB_MULTIPOLYGON, len(polygons)) + b"".join(
        wkb_polygon(p) for p in polygons
    )


def _read_coords(buf: bytes, offset: int, endian: str) -> tuple[list[tuple[float, float]], int]:
    (n,) = struct.unpack_from(f"{endian}I", buf, offset)
    offset += 4
    flat = struct.unpack_from(f"{endian}{2 * n}d", buf, offset)
    return [(flat[2 * i], flat[2 * i + 1]) for i in range(n)], offset + 16 * n


def _read_geometry(buf: bytes, offset: int) -> tuple[dict[str, Any], int]:
    endian = "<" if buf[offset] == 1 else ">"
    (gtype,) = struct.unpack_from(f"{endian}I", buf, offset + 1)
    offset += 5
    if gtype == _WKB_POINT:
        x, y = struct.unpack_from(f"{endian}dd", buf, offset)
        return {"type": "Point", "coordinates": [x, y]}, offset + 16
    if gtype == _WKB_LINESTRING:
        coords, offset = _read_coords(buf, offset, endian)
        return {"type": "LineString", "coordinates": [list(c) for c in coords]}, offset
    if gtype == _WKB_POLYGON:
        (n,) = struct.unpack_from(f"{endian}I", buf, offset)
        offset += 4
        rings = []
        for _ in range(n):
            coords, offset = _read_coords(buf, offset, endian)
            rings.append([list(c) for c in coords])
        return {"type": "Polygon", "coordinates": rings}, offset
    if gtype in (_WKB_MULTILINESTRING, _WKB_MULTIPOLYGON):
        (n,) = struct.unpack_from(f"{endian}I", buf, offset)
        offset += 4
        members = []
        for _ in range(n):
            member, offset = _read_geometry(buf, offset)
            members.append(member["coordinates"])
        kind = "MultiLineString" if gtype == _WKB_MULTILINESTRING else "MultiPolygon"
        return {"type": kind, "coordinates": members}, offset
    raise ValueError(f"unsupported WKB geometry type {gtype}")


def wkb_to_geojson(buf: bytes | None) -> dict[str, Any] | None:
    """WKB (either byte order; Point, LineString, Polygon, MultiLineString, MultiPolygon) ->
    a GeoJSON geometry dict. ``None`` passes through."""
    if buf is None:
        return None
    geometry, _ = _read_geometry(bytes(buf), 0)
    return geometry


# ------------------------------------------------------------------------------ polygon helpers
def _ring_area_centroid(ring: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """Signed planar area and centroid of one closed ring (shoelace), in degrees."""
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a /= 2.0
    if a == 0:
        xs = [c[0] for c in ring]
        ys = [c[1] for c in ring]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return a, cx / (6.0 * a), cy / (6.0 * a)


def polygon_centroid(polygons: Sequence[Sequence[Sequence[Sequence[float]]]]) -> tuple[float, float] | None:
    """Area-weighted planar centroid of a multipolygon (outer rings add, holes subtract). At a
    substation's size (tens to hundreds of metres) the planar error is far below a metre. A
    degenerate polygon falls back to the mean of its outer vertices."""
    total = sx = sy = 0.0
    vertices: list[Sequence[float]] = []
    for rings in polygons:
        for i, ring in enumerate(rings):
            if len(ring) < 4:
                continue
            a, cx, cy = _ring_area_centroid(ring)
            weight = abs(a) if i == 0 else -abs(a)
            total += weight
            sx += cx * weight
            sy += cy * weight
            if i == 0:
                vertices.extend(ring[:-1])
    if total > 0:
        return sx / total, sy / total
    if vertices:
        return sum(v[0] for v in vertices) / len(vertices), sum(v[1] for v in vertices) / len(vertices)
    return None


# ------------------------------------------------------------------------- OSM file extraction
@dataclass
class ExtractStats:
    """What one region's extraction kept and dropped, by rule (written to the manifest)."""

    region: str = ""
    timestamp: str | None = None
    #: The rows a load of this region holds: every published row whose `scope_regions` names it
    #: (module docstring, "Regions"), counted once the whole build is in (`build`). What the
    #: promotion gate compares and what a server loading this region alone gets.
    lines_kept: int = 0
    substations_kept: int = 0
    #: What this extract's file kept under the tag rules, including elements an earlier extract
    #: already held (`duplicates_skipped` of them).
    lines_read: int = 0
    substations_read: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    #: Kept, but worth counting: a substation way that does not close into an area (kept at the
    #: mean of its vertices, no outline), or one folded into its multipolygon relation.
    notes: Counter[str] = field(default_factory=Counter)
    duplicates_skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "timestamp": self.timestamp,
            "lines_kept": self.lines_kept,
            "substations_kept": self.substations_kept,
            "lines_read": self.lines_read,
            "substations_read": self.substations_read,
            "dropped": dict(sorted(self.dropped.items())),
            "notes": dict(sorted(self.notes.items())),
            "duplicates_skipped": self.duplicates_skipped,
        }


def _osmium() -> Any:
    """pyosmium, imported where it is used: the build needs it, the server's pull and every test
    that does not read an OSM file do not (it is not in requirements.txt; the build installs it,
    `infra/scripts/build_grid_layers.sh`)."""
    import osmium  # type: ignore[import-not-found,unused-ignore]

    return osmium


def power_tag_filter() -> Any:
    osmium = _osmium()
    return osmium.filter.TagFilter(("power", "line"), ("power", "cable"), ("power", "substation"))


def filter_power_file(src: pathlib.Path, dst: pathlib.Path) -> int:
    """Copy the power features of `src` to `dst` with every node and relation member they
    reference (pyosmium's `BackReferenceWriter`, the equivalent of ``osmium tags-filter``): a few
    megabytes out of a state's hundreds. Returns the number of tagged objects written."""
    osmium = _osmium()
    written = 0
    with osmium.BackReferenceWriter(str(dst), ref_src=str(src), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(src)).with_filter(power_tag_filter()):
            writer.add(obj)
            written += 1
    return written


def file_timestamp(path: pathlib.Path) -> str | None:
    """The extract's replication timestamp from the PBF header (Geofabrik writes
    ``osmosis_replication_timestamp``), normalised to ``YYYY-MM-DDTHH:MM:SSZ``; ``None`` when the
    header carries none (an OSM XML file, for instance)."""
    osmium = _osmium()
    reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
    try:
        value = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    return normalise_timestamp(value) if value else None


def normalise_timestamp(value: str) -> str:
    """``2026-10-21T20:21:02Z`` / ``2026-10-21T20:21:02+00:00`` -> ``2026-10-21T20:21:02Z``."""
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class LineRecord:
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    coords: list[tuple[float, float]]


@dataclass
class SubstationRecord:
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    point: tuple[float, float]
    #: Multipolygon rings (outer first in each polygon) for an area, `None` for a node.
    outline: list[PolygonRings] | None = None


def read_power_file(
    path: pathlib.Path, stats: ExtractStats
) -> tuple[list[LineRecord], list[SubstationRecord]]:
    """Every kept line and substation in an OSM file (PBF or XML), with node locations resolved
    and substation areas assembled (closed ways and multipolygon relations). A ``power=substation``
    way that does not close into an area keeps the mean of its vertices as its point and no
    outline (`stats.notes`); a substation relation that does not assemble into an area (not a
    multipolygon, or members missing from the extract) has no geometry and is dropped. A substation
    way that is the only outer of a kept substation relation is the same substation mapped twice:
    the relation stays, takes any tag only the way states (old-style multipolygons tag the outer),
    and the way is dropped (`stats.notes`), so nearby-grid and the subtype counts see it once.
    Objects are copied out of pyosmium's buffers as they are read."""
    osmium = _osmium()
    lines: list[LineRecord] = []
    subs: dict[tuple[str, int], SubstationRecord] = {}
    open_sub_ways: dict[int, SubstationRecord] = {}
    sub_relations: set[int] = set()
    #: kept substation relation -> the ids of its outer member ways (role ``outer`` or empty).
    relation_outers: dict[int, list[int]] = {}
    processor = (
        osmium.FileProcessor(str(path))
        .with_locations()
        .with_areas(osmium.filter.TagFilter(("power", "substation")))
        .with_filter(power_tag_filter())
    )
    for obj in processor:
        tags = {t.k: t.v for t in obj.tags}
        if obj.is_area():
            if not substation_kept(tags)[0]:
                continue  # counted when its way or relation went past
            polygons: list[PolygonRings] = []
            for outer in obj.outer_rings():
                rings: PolygonRings = [[(n.lon, n.lat) for n in outer]]
                rings.extend([(n.lon, n.lat) for n in inner] for inner in obj.inner_rings(outer))
                polygons.append(rings)
            centroid = polygon_centroid(polygons)
            if centroid is None:
                continue
            osm_type = "way" if obj.from_way() else "relation"
            key = (osm_type, int(obj.orig_id()))
            subs[key] = SubstationRecord(osm_type, key[1], tags, centroid, polygons)
            continue
        if obj.is_node():
            kept, reason = substation_kept(tags)
            if not kept:
                stats.dropped[f"substation_{reason}"] += 1
                continue
            if not obj.location.valid():
                stats.dropped["substation_no_location"] += 1
                continue
            subs[("node", obj.id)] = SubstationRecord(
                "node", obj.id, tags, (obj.location.lon, obj.location.lat)
            )
            continue
        if obj.is_way():
            if tags.get("power") == "substation":
                kept, reason = substation_kept(tags)
                if not kept:
                    stats.dropped[f"substation_{reason}"] += 1
                    continue
                if ("way", obj.id) in subs:
                    continue
                coords = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
                if coords:
                    lon = sum(c[0] for c in coords) / len(coords)
                    lat = sum(c[1] for c in coords) / len(coords)
                    open_sub_ways[obj.id] = SubstationRecord("way", obj.id, tags, (lon, lat), None)
                continue
            kept, reason = line_kept(tags)
            if not kept:
                stats.dropped[f"line_{reason}"] += 1
                continue
            coords = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            if len(coords) < 2:
                stats.dropped["line_no_geometry"] += 1
                continue
            lines.append(LineRecord("way", obj.id, tags, coords))
            continue
        if obj.is_relation() and tags.get("power") == "substation":
            kept, reason = substation_kept(tags)
            if kept:
                sub_relations.add(obj.id)
                relation_outers[obj.id] = [
                    int(m.ref) for m in obj.members if m.type == "w" and m.role in ("outer", "")
                ]
            else:
                stats.dropped[f"substation_{reason}"] += 1
    # A closed way's area may come before or after the way itself; whichever way never became an
    # area keeps its vertex-mean point.
    for way_id, record in open_sub_ways.items():
        if ("way", way_id) not in subs:
            subs[("way", way_id)] = record
            stats.notes["substation_way_not_an_area_kept_as_vertex_mean"] += 1
    for relation_id in sub_relations:
        if ("relation", relation_id) not in subs:
            stats.dropped["substation_relation_without_area"] += 1
            continue
        outers = relation_outers.get(relation_id) or []
        twin = subs.get(("way", outers[0])) if len(outers) == 1 else None
        if twin is not None:
            relation = subs[("relation", relation_id)]
            for tag, value in twin.tags.items():
                relation.tags.setdefault(tag, value)
            del subs[("way", outers[0])]
            stats.notes["substation_way_folded_into_its_relation"] += 1
    out_subs = sorted(subs.values(), key=lambda r: (r.osm_type, r.osm_id))
    lines.sort(key=lambda r: r.osm_id)
    return lines, out_subs


# ------------------------------------------------------------------------------- GeoParquet
LINE_COLUMNS = (
    "osm_type",
    "osm_id",
    "power",
    "voltage_kv",
    "voltages_kv",
    "voltage_raw",
    "voltage_unknown",
    "voltage_assumed_kv",
    "underground",
    "location",
    "circuits",
    "cables",
    "frequency",
    "line",
    "operator",
    "name",
    "ref",
    "region",
    "states_crossed",
    "extract_regions",
    "scope_regions",
    "extract_timestamp",
    "geometry",
)
SUBSTATION_COLUMNS = (
    "osm_type",
    "osm_id",
    "substation",
    "voltage_kv",
    "voltages_kv",
    "voltage_raw",
    "voltage_unknown",
    "voltage_assumed_kv",
    "operator",
    "name",
    "ref",
    "region",
    "states_crossed",
    "extract_regions",
    "scope_regions",
    "extract_timestamp",
    "geometry",
    "outline",
)
_STRING = pa.string()
LINE_SCHEMA = pa.schema(
    [
        ("osm_type", _STRING),
        ("osm_id", pa.int64()),
        ("power", _STRING),
        ("voltage_kv", pa.float64()),
        ("voltages_kv", pa.list_(pa.float64())),
        ("voltage_raw", _STRING),
        ("voltage_unknown", pa.bool_()),
        ("voltage_assumed_kv", pa.bool_()),
        ("underground", pa.bool_()),
        ("location", _STRING),
        ("circuits", _STRING),
        ("cables", _STRING),
        ("frequency", _STRING),
        ("line", _STRING),
        ("operator", _STRING),
        ("name", _STRING),
        ("ref", _STRING),
        ("region", _STRING),
        ("states_crossed", pa.list_(_STRING)),
        ("extract_regions", pa.list_(_STRING)),
        ("scope_regions", pa.list_(_STRING)),
        ("extract_timestamp", _STRING),
        ("geometry", pa.binary()),
    ]
)
SUBSTATION_SCHEMA = pa.schema(
    [
        ("osm_type", _STRING),
        ("osm_id", pa.int64()),
        ("substation", _STRING),
        ("voltage_kv", pa.float64()),
        ("voltages_kv", pa.list_(pa.float64())),
        ("voltage_raw", _STRING),
        ("voltage_unknown", pa.bool_()),
        ("voltage_assumed_kv", pa.bool_()),
        ("operator", _STRING),
        ("name", _STRING),
        ("ref", _STRING),
        ("region", _STRING),
        ("states_crossed", pa.list_(_STRING)),
        ("extract_regions", pa.list_(_STRING)),
        ("scope_regions", pa.list_(_STRING)),
        ("extract_timestamp", _STRING),
        ("geometry", pa.binary()),
        ("outline", pa.binary()),
    ]
)


def _tag(tags: dict[str, str], key: str) -> str | None:
    value = (tags.get(key) or "").strip()
    return value or None


def scope_of(states_crossed: Iterable[str], extract_regions: Iterable[str]) -> list[str]:
    """`scope_regions` (module docstring, "Regions"): the states the geometry touches and every
    extract that held the element, together."""
    return sorted({str(r) for r in states_crossed if r} | {str(r) for r in extract_regions if r})


def in_scope(scope_regions: Iterable[str] | None, regions: Collection[str]) -> bool:
    """The one region rule: does a row with `scope_regions` belong to a load of `regions`?"""
    return any(r in regions for r in (scope_regions or ()))


def label_line(
    coords: Sequence[Sequence[float]], extract: str, states: StateIndex | None
) -> tuple[str, list[str]]:
    """`(region, states_crossed)` for a line: the state polygon at its representative point,
    falling back to `extract` (no index, or a point in no polygon), and every state a vertex falls in
    plus that region."""
    line = [[float(c[0]), float(c[1])] for c in coords]
    if states is None:
        return extract, [extract]
    rep = representative_point([line]) or line[0]
    region = states.lookup(rep[0], rep[1]) or extract
    return region, sorted(set(states.states_for([line])) | {region})


def label_point(lon: float, lat: float, extract: str, states: StateIndex | None) -> str:
    """A substation's `region`: the state polygon at its point, else the extract that held it."""
    return (states.lookup(lon, lat) if states else None) or extract


def line_row(
    record: LineRecord, region: str, timestamp: str, *, states: StateIndex | None = None
) -> dict[str, Any]:
    """One GeoParquet line row from `region`'s extract. `states` labels it (`label_line`); without
    it the extract's region stands in, as for a single-region test row."""
    v = parse_voltage(record.tags.get("voltage"))
    home, crossed = label_line(record.coords, region, states)
    return {
        "osm_type": record.osm_type,
        "osm_id": record.osm_id,
        "power": record.tags.get("power"),
        "voltage_kv": v.max_kv,
        "voltages_kv": list(v.values_kv),
        "voltage_raw": v.raw,
        "voltage_unknown": v.unknown,
        "voltage_assumed_kv": v.assumed_kv,
        "underground": is_underground(record.tags),
        "location": _tag(record.tags, "location"),
        "circuits": _tag(record.tags, "circuits"),
        "cables": _tag(record.tags, "cables"),
        "frequency": _tag(record.tags, "frequency"),
        "line": _tag(record.tags, "line"),
        "operator": _tag(record.tags, "operator"),
        "name": _tag(record.tags, "name"),
        "ref": _tag(record.tags, "ref"),
        "region": home,
        "states_crossed": crossed,
        "extract_regions": [region],
        "scope_regions": scope_of(crossed, [region]),
        "extract_timestamp": timestamp,
        "geometry": wkb_linestring([(round(x, 7), round(y, 7)) for x, y in record.coords]),
    }


def substation_row(
    record: SubstationRecord, region: str, timestamp: str, *, states: StateIndex | None = None
) -> dict[str, Any]:
    """One GeoParquet substation row from `region`'s extract, labelled by its point (`label_point`)."""
    v = parse_voltage(record.tags.get("voltage"))
    outline = None
    if record.outline:
        polygons = [
            [[(round(x, 7), round(y, 7)) for x, y in ring] for ring in poly] for poly in record.outline
        ]
        outline = wkb_polygon(polygons[0]) if len(polygons) == 1 else wkb_multipolygon(polygons)
    lon, lat = record.point
    home = label_point(lon, lat, region, states)
    return {
        "osm_type": record.osm_type,
        "osm_id": record.osm_id,
        "substation": _tag(record.tags, "substation"),
        "voltage_kv": v.max_kv,
        "voltages_kv": list(v.values_kv),
        "voltage_raw": v.raw,
        "voltage_unknown": v.unknown,
        "voltage_assumed_kv": v.assumed_kv,
        "operator": _tag(record.tags, "operator"),
        "name": _tag(record.tags, "name"),
        "ref": _tag(record.tags, "ref"),
        "region": home,
        "states_crossed": [home],
        "extract_regions": [region],
        "scope_regions": scope_of([home], [region]),
        "extract_timestamp": timestamp,
        "geometry": wkb_point(round(lon, 7), round(lat, 7)),
        "outline": outline,
    }


def _geo_metadata(columns: dict[str, list[str]], bbox: list[float] | None) -> bytes:
    """GeoParquet 1.1 file metadata. No ``crs`` key: the default is OGC:CRS84 (WGS 84 lon/lat),
    which OpenStreetMap coordinates are."""
    cols: dict[str, Any] = {}
    for name, types in columns.items():
        cols[name] = {"encoding": "WKB", "geometry_types": types}
        if bbox is not None and name == "geometry":
            cols[name]["bbox"] = bbox
    return json.dumps({"version": "1.1.0", "primary_column": "geometry", "columns": cols}).encode()


def _bbox_of(rows: Iterable[dict[str, Any]]) -> list[float] | None:
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for row in rows:
        geometry = wkb_to_geojson(row["geometry"])
        if geometry is None:
            continue
        coords = geometry["coordinates"]
        points = [coords] if geometry["type"] == "Point" else coords
        for x, y in points:
            minx, miny, maxx, maxy = min(minx, x), min(miny, y), max(maxx, x), max(maxy, y)
    return None if minx is math.inf else [minx, miny, maxx, maxy]


def write_geoparquet(rows: list[dict[str, Any]], path: pathlib.Path, *, kind: str) -> dict[str, Any]:
    """`rows` (from `line_row` / `substation_row`) -> one GeoParquet file. Returns its byte size
    and sha256 for the manifest."""
    if kind == "lines":
        schema, geo = LINE_SCHEMA, {"geometry": ["LineString"]}
    elif kind == "substations":
        schema, geo = SUBSTATION_SCHEMA, {"geometry": ["Point"], "outline": ["Polygon", "MultiPolygon"]}
    else:
        raise ValueError(kind)
    table = pa.Table.from_pylist(rows, schema=schema)
    meta = dict(table.schema.metadata or {})
    meta[b"geo"] = _geo_metadata(geo, _bbox_of(rows))
    table = table.replace_schema_metadata(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    data = path.read_bytes()
    return {"rows": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


#: Rows per record batch when `read_geoparquet` filters by region.
READ_BATCH_ROWS = 65_536


def _batch_in_scope(batch: Any, wanted: Any) -> Any:
    """The rows of `batch` whose `scope_regions` list names a `wanted` region (pyarrow compute: the
    list is flattened, matched and mapped back to its parent rows, never turned into Python)."""
    column = batch.column("scope_regions")
    hits = pc.is_in(pc.list_flatten(column), value_set=wanted)
    rows = pc.unique(pc.filter(pc.list_parent_indices(column), hits))
    return batch.take(rows)


def read_geoparquet(path: pathlib.Path, *, regions: Collection[str] | None = None) -> list[dict[str, Any]]:
    """A GeoParquet this module wrote -> its rows as dicts (geometry columns left as WKB). Refuses
    a file without GeoParquet metadata. With `regions`, only the rows `in_scope` of them, filtered
    batch by batch in pyarrow, so a one-state load of the national file holds that state's rows
    as Python objects, never the nation's (`pq.read_table(filters=...)` cannot test membership in a
    list column)."""
    parquet = pq.ParquetFile(path)
    if b"geo" not in (parquet.schema_arrow.metadata or {}):
        raise BuildError(f"{path} carries no GeoParquet 'geo' metadata")
    if regions is None:
        everything: list[dict[str, Any]] = parquet.read().to_pylist()
        return everything
    wanted = pa.array(sorted(regions), type=pa.string())
    rows: list[dict[str, Any]] = []
    for batch in parquet.iter_batches(batch_size=READ_BATCH_ROWS):
        rows.extend(_batch_in_scope(batch, wanted).to_pylist())
    return rows


# --------------------------------------------------------------------------------- build
@dataclass
class BuildResult:
    lines: list[dict[str, Any]] = field(default_factory=list)
    substations: list[dict[str, Any]] = field(default_factory=list)
    regions: list[ExtractStats] = field(default_factory=list)
    #: `(osm_type, osm_id)` -> `(kind, index in that list, completeness of the held copy)`: an
    #: element is held once across every extract, as a line or as a substation.
    held: dict[tuple[str, int], tuple[str, int, tuple[int, int]]] = field(default_factory=dict)


def _line_completeness(record: LineRecord) -> tuple[int, int]:
    return (len(record.coords), 0)


def _substation_completeness(record: SubstationRecord) -> tuple[int, int]:
    outline = record.outline or []
    return (1 if outline else 0, sum(len(ring) for polygon in outline for ring in polygon))


def _add_record(
    result: BuildResult,
    kind: str,
    key: tuple[str, int],
    score: tuple[int, int],
    region: str,
    build_row: Callable[[], dict[str, Any]],
) -> bool:
    """Hold one element (True when it is new). One already held joins this region's extract to its
    `extract_regions` and `scope_regions`; when this extract's copy is more complete (more resolved
    vertices; an assembled outline, a state extract can cut a way at its edge) it replaces the
    held copy, labelled afresh from its own geometry."""
    rows = result.lines if kind == "lines" else result.substations
    held = result.held.get(key)
    if held is None:
        result.held[key] = (kind, len(rows), score)
        rows.append(build_row())
        return True
    held_kind, index, held_score = held
    held_rows = result.lines if held_kind == "lines" else result.substations
    row = held_rows[index]
    extracts = sorted(set(row["extract_regions"]) | {region})
    if held_kind == kind and score > held_score:
        row = build_row()
        held_rows[index] = row
        result.held[key] = (kind, index, score)
    row["extract_regions"] = extracts
    row["scope_regions"] = scope_of(row["states_crossed"], extracts)
    return False


def add_region(
    result: BuildResult,
    lines: list[LineRecord],
    substations: list[SubstationRecord],
    stats: ExtractStats,
    *,
    states: StateIndex | None = None,
) -> None:
    """Add one region's records, labelled by `states` (`line_row`, `substation_row`). An element an
    earlier region already holds is held once (`_add_record`), whichever extract came first: its
    label comes from its geometry, never from the extracts' table order. Call `count_scope` once
    every region is in."""
    timestamp = stats.timestamp or ""
    region = stats.region
    for line in lines:
        stats.lines_read += 1
        if not _add_record(
            result,
            "lines",
            (line.osm_type, line.osm_id),
            _line_completeness(line),
            region,
            partial(line_row, line, region, timestamp, states=states),
        ):
            stats.duplicates_skipped += 1
    for sub in substations:
        stats.substations_read += 1
        if not _add_record(
            result,
            "substations",
            (sub.osm_type, sub.osm_id),
            _substation_completeness(sub),
            region,
            partial(substation_row, sub, region, timestamp, states=states),
        ):
            stats.duplicates_skipped += 1
    result.regions.append(stats)


def count_scope(result: BuildResult) -> None:
    """Each region's `lines_kept` / `substations_kept`: the rows whose `scope_regions` name it,
    which is what a load of that region alone holds (`write_asset_frames`) and what the promotion
    gate compares (`compare_manifests`)."""
    lines = Counter(r for row in result.lines for r in row["scope_regions"])
    subs = Counter(r for row in result.substations for r in row["scope_regions"])
    for stats in result.regions:
        stats.lines_kept = lines[stats.region]
        stats.substations_kept = subs[stats.region]


def clip_to_region(
    lines: list[LineRecord],
    substations: list[SubstationRecord],
    region: str,
    states: StateIndex,
    stats: ExtractStats,
) -> tuple[list[LineRecord], list[SubstationRecord]]:
    """Keep the lines with a vertex inside `region`'s polygon and the substations whose point is
    inside it. For an input that covers more than its region (the development route's bounding-box
    queries, docs/25 §2.7); a Geofabrik state extract is already cut to its state and is not
    clipped. A line that crosses the border is kept whole, as Geofabrik keeps it."""
    kept_lines = [ln for ln in lines if any(states.lookup(x, y) == region for x, y in ln.coords)]
    kept_subs = [s for s in substations if states.lookup(s.point[0], s.point[1]) == region]
    stats.dropped["line_outside_region"] += len(lines) - len(kept_lines)
    stats.dropped["substation_outside_region"] += len(substations) - len(kept_subs)
    return kept_lines, kept_subs


def extract_region(
    path: pathlib.Path,
    region: str,
    *,
    work_dir: pathlib.Path,
    timestamp: str | None = None,
    delete_source: bool = False,
) -> tuple[list[LineRecord], list[SubstationRecord], ExtractStats]:
    """Filter one OSM file to its power features, read them, and clean up after itself: the
    filtered file always, the source file too when `delete_source` (a downloaded extract). The
    timestamp is the file's own (PBF header) unless one is given; a file stating none and given
    none is refused, because `retrieved_at` would otherwise be invented."""
    stats = ExtractStats(region=region)
    stats.timestamp = normalise_timestamp(timestamp) if timestamp else file_timestamp(path)
    if stats.timestamp is None:
        raise BuildError(f"{path} states no replication timestamp; pass one (--timestamp)")
    work_dir.mkdir(parents=True, exist_ok=True)
    filtered = work_dir / f"{region}.power.osm.pbf"
    try:
        filter_power_file(path, filtered)
        if delete_source:
            path.unlink(missing_ok=True)
        lines, substations = read_power_file(filtered, stats)
    finally:
        filtered.unlink(missing_ok=True)
        if delete_source:
            path.unlink(missing_ok=True)
    return lines, substations, stats


def geofabrik_url(slug: str) -> str:
    return f"{GEOFABRIK_BASE}/{slug}-latest.osm.pbf"


def select_states(spec: str) -> list[tuple[str, str]]:
    """``all`` or a comma list of ISO codes / Geofabrik slugs (``US-TX,oklahoma``), in
    `US_STATE_EXTRACTS` order. An unknown name is an error, not a skip."""
    if spec.strip().lower() == "all":
        return list(US_STATE_EXTRACTS)
    wanted = {s.strip().lower() for s in spec.split(",") if s.strip()}
    known = {code.lower() for code, _ in US_STATE_EXTRACTS} | {slug for _, slug in US_STATE_EXTRACTS}
    unknown = sorted(wanted - known)
    if unknown:
        raise BuildError(f"unknown state(s) {unknown}; use ISO codes (US-TX) or Geofabrik slugs (texas)")
    return [(code, slug) for code, slug in US_STATE_EXTRACTS if code.lower() in wanted or slug in wanted]


#: Attempts per state extract. A reset mid-stream, a body shorter than its Content-Length or one
#: whose md5 differs from Geofabrik's ``.md5`` file re-downloads that state alone, after the delay
#: below, so one bad transfer late in a national run does not throw away the states before it.
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAYS_S = (30.0, 120.0)
_MD5_RE = re.compile(r"^([0-9a-fA-F]{32})(?:\s|$)")


class DownloadIncomplete(BuildError):
    """One transfer of an extract that cannot be used: cut short, or not the file its checksum names.
    Retried (`download`); the other `BuildError`s (a 404, a malformed checksum file) are not."""


def fetch_md5(session: Any, url: str) -> str:
    """Geofabrik publishes ``<file>.md5`` beside every extract (``<32 hex>  texas-latest.osm.pbf``)."""
    resp = session.get(f"{url}.md5", honour_robots=True, timeout=60)
    if resp.status_code != 200:
        raise BuildError(f"{url}.md5 answered HTTP {resp.status_code}")
    match = _MD5_RE.match(str(resp.text).strip())
    if not match:
        raise BuildError(f"{url}.md5 holds no md5 checksum: {str(resp.text)[:80]!r}")
    return match.group(1).lower()


def _download_once(session: Any, url: str, dest: pathlib.Path) -> dict[str, Any]:
    """One transfer: the checksum first, then the file streamed to `dest`, its md5 computed on the
    way; refused when it is shorter or longer than the announced Content-Length or its md5 is not the
    checksum's (`DownloadIncomplete`). The checksum is read first so a file Geofabrik replaces between
    the two requests fails the comparison instead of passing with a stale checksum."""
    expected_md5 = fetch_md5(session, url)
    resp = session.get(url, honour_robots=True, stream=True, timeout=600)
    if resp.status_code != 200:
        resp.close()
        raise BuildError(f"{url} answered HTTP {resp.status_code}")
    announced = str(resp.headers.get("Content-Length") or "").strip()
    digest = hashlib.md5(usedforsecurity=False)
    written = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    finally:
        resp.close()
    if announced.isdigit() and int(announced) != written:
        raise DownloadIncomplete(f"{url}: {written} bytes received of the {announced} announced")
    md5 = digest.hexdigest()
    if md5 != expected_md5:
        raise DownloadIncomplete(f"{url}: md5 {md5} is not the {expected_md5} that {url}.md5 states")
    return {
        "url": url,
        "bytes": written,
        "md5": md5,
        "last_modified": resp.headers.get("Last-Modified"),
    }


def download(
    url: str,
    dest: pathlib.Path,
    *,
    session: Any = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Stream one extract to `dest` through the platform's polite session (robots honoured, each
    request retried with backoff by the session) and check it against Geofabrik's ``.md5`` and the
    Content-Length (`_download_once`). A transfer that fails part-way (a reset, a short body, a
    checksum mismatch) or a request the session gave up on is retried for this state alone, up to
    `attempts` times, `DOWNLOAD_RETRY_DELAYS_S` apart; a refusal (robots.txt, a challenge page, an
    HTTP error such as 404) is not. The partial file is deleted after every failed attempt. Returns
    the bytes, md5, Last-Modified and the attempts it took."""
    import requests

    from pipeline.connectors.http import HttpFailed, PoliteSession

    session = session or PoliteSession(rate_limits={"download.geofabrik.de": 1.0 / GEOFABRIK_MIN_INTERVAL_S})
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            return {**_download_once(session, url, dest), "attempts": attempt}
        except (DownloadIncomplete, HttpFailed, requests.RequestException) as exc:
            last_error = str(exc)[:300]
            dest.unlink(missing_ok=True)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        if attempt < attempts:
            delay = DOWNLOAD_RETRY_DELAYS_S[min(attempt - 1, len(DOWNLOAD_RETRY_DELAYS_S) - 1)]
            print(  # noqa: T201 -- build progress on the Actions log, like the CLI's summary line
                f"osm_power: {url} attempt {attempt} of {attempts} failed ({last_error}); "
                f"retrying in {delay:g} s",
                flush=True,
            )
            sleep(delay)
    raise BuildError(f"{url}: {attempts} attempts failed; the last: {last_error}")


def build(
    inputs: Sequence[tuple[str, pathlib.Path | str]],
    *,
    work_dir: pathlib.Path,
    out_dir: pathlib.Path,
    timestamp: str | None = None,
    from_geofabrik: bool = False,
    session: Any = None,
    clip: bool = False,
    states: StateIndex | None = None,
) -> dict[str, Any]:
    """`inputs` is `(region, path)` for local files or `(region, slug)` with `from_geofabrik`.
    Writes `lines.parquet`, `substations.parquet` and `manifest.json` into `out_dir`; any region
    that fails stops the build before anything is written (a partial build is never published,
    because the loader withdraws what a covered region no longer has). `clip` cuts each local
    file to its region's polygon (`clip_to_region`). Every row is labelled with the vendored state
    polygons (`label_line`, `label_point`; module docstring, "Regions"); `states` is for a test that
    passes its own index."""
    t0 = time.monotonic()
    if states is None and STATES_GEOJSON.exists():
        states = StateIndex.from_file(STATES_GEOJSON)
    if clip and states is None:
        raise BuildError(f"--clip needs the vendored state polygons ({STATES_GEOJSON})")
    result = BuildResult()
    downloads: list[dict[str, Any]] = []
    for i, (region, ref) in enumerate(inputs):
        if from_geofabrik:
            if i:
                time.sleep(GEOFABRIK_MIN_INTERVAL_S)
            url = geofabrik_url(str(ref))
            path = work_dir / f"{ref}-latest.osm.pbf"
            downloads.append({"region": region, **download(url, path, session=session)})
            lines, subs, stats = extract_region(path, region, work_dir=work_dir, delete_source=True)
        else:
            lines, subs, stats = extract_region(
                pathlib.Path(ref), region, work_dir=work_dir, timestamp=timestamp
            )
            if clip and states is not None:
                lines, subs = clip_to_region(lines, subs, region, states, stats)
        add_region(result, lines, subs, stats, states=states)
    count_scope(result)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines_meta = write_geoparquet(result.lines, out_dir / GEO_LINES_NAME, kind="lines")
    subs_meta = write_geoparquet(result.substations, out_dir / GEO_SUBSTATIONS_NAME, kind="substations")
    timestamps = sorted(s.timestamp for s in result.regions if s.timestamp)
    manifest = {
        "source_id": SOURCE_ID,
        "layout": LAYOUT_VERSION,
        "licence": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "licence_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "built_at": _now_iso(),
        "data_timestamp": timestamps[-1] if timestamps else None,
        "regions": [s.as_dict() for s in result.regions],
        "files": {GEO_LINES_NAME: lines_meta, GEO_SUBSTATIONS_NAME: subs_meta},
        "downloads": downloads,
        "rules": {
            "min_kv": MIN_KV,
            "line_power_values": list(LINE_POWER_VALUES),
            "dropped_line_values": sorted(NON_LINE_LINE_VALUES),
            "dropped_substation_types": sorted(DROPPED_SUBSTATION_TYPES),
        },
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    manifest["version"] = version_key(manifest)
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    return manifest


#: Most a region's line or substation count may fall between the published build and a new one
#: before the new one is refused promotion (the connectors' row-drift gate, for this layer; a real
#: month of OpenStreetMap edits moves a state by well under 1%).
MAX_REGION_DROP = 0.10
#: ...and only when at least this many rows went: in a small state (DC, Rhode Island) a handful of
#: ordinary edits is over 10%, and must not block the monthly national promotion. A region that
#: falls to nothing is blocked whatever its size.
MIN_REGION_DROP_ROWS = 50


def compare_manifests(
    new: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    max_drop: float = MAX_REGION_DROP,
    min_rows: int = MIN_REGION_DROP_ROWS,
) -> list[str]:
    """Why `new` must not replace `previous` as `latest.json`, one line per problem; empty when it
    may. A region the published build has and the new one lacks, a region whose kept lines or
    substations fell by more than `max_drop` and by at least `min_rows`, a region that fell to none,
    and an empty file each block promotion. The counts are per region by `scope_regions`
    (`count_scope`), what a server loading that region gets."""
    problems: list[str] = []
    for name, meta in sorted((new.get("files") or {}).items()):
        if not int((meta or {}).get("rows") or 0):
            problems.append(f"{name}: no rows")
    before = {r["region"]: r for r in (previous or {}).get("regions", []) if r.get("region")}
    after = {r["region"]: r for r in new.get("regions", []) if r.get("region")}
    for region in sorted(set(before) - set(after)):
        problems.append(f"{region}: in the published build, missing from this one")
    for region, row in sorted(after.items()):
        old = before.get(region)
        if not old:
            continue
        for key in ("lines_kept", "substations_kept"):
            was, now = int(old.get(key) or 0), int(row.get(key) or 0)
            if was and not now:
                problems.append(f"{region}: {key} fell from {was} to none")
            elif was and was - now >= min_rows and (was - now) / was > max_drop:
                problems.append(
                    f"{region}: {key} fell from {was} to {now} (more than {max_drop:.0%} and {min_rows} rows)"
                )
    return problems


def version_key(manifest: dict[str, Any]) -> str:
    """The R2 prefix a build uploads under: layout, data timestamp, build time
    (``v1/20261021T202102Z-20261022T061500Z``) -- a rebuild of the same extracts gets a new key, so
    an upload never overwrites a published version in place."""

    def token(value: str | None) -> str:
        return (value or "unknown").replace("-", "").replace(":", "")

    return f"{LAYOUT_VERSION}/{token(manifest.get('data_timestamp'))}-{token(manifest.get('built_at'))}"


# --------------------------------------------------------------------- GeoParquet -> asset rows
def element_url(osm_type: str, osm_id: int) -> str:
    return OSM_ELEMENT_URL.format(osm_type=osm_type, osm_id=osm_id)


def _fmt_kv(kv: float | None) -> str | None:
    if kv is None:
        return None
    return f"{int(kv)}" if float(kv).is_integer() else f"{kv:g}"


def line_display_name(row: dict[str, Any]) -> str:
    """The line's own ``name`` when it has one; else what it is and who operates it, with the way id
    so two unnamed lines of one operator are told apart: "Oncor 138 kV line (OSM way 123)",
    "Power line, voltage not stated (OSM way 2)"."""
    if row.get("name"):
        return str(row["name"])
    kv = _fmt_kv(row.get("voltage_kv"))
    kind = "cable" if row.get("power") == "cable" else "line"
    what = f"{kv} kV {kind}" if kv else f"Power {kind}, voltage not stated"
    operator = f"{row['operator']} " if row.get("operator") else ""
    if not kv and operator:
        what = what[0].lower() + what[1:]
    return f"{operator}{what} (OSM {row['osm_type']} {row['osm_id']})"


def substation_display_name(row: dict[str, Any]) -> str:
    if row.get("name"):
        name = str(row["name"])
        return name if "substation" in name.lower() or "switch" in name.lower() else f"{name} substation"
    operator = f"{row['operator']} substation" if row.get("operator") else "Substation"
    return f"{operator} (OSM {row['osm_type']} {row['osm_id']})"


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _strings(values: object) -> list[str]:
    return [str(v) for v in values if v] if isinstance(values, (list, tuple)) else []


def region_attributes(
    row: dict[str, Any],
    covered: Collection[str] | None,
    *,
    line: Sequence[Sequence[float]] | None = None,
    point: tuple[float, float] | None = None,
    states: StateIndex | None = None,
) -> dict[str, Any]:
    """The asset row's region fields from a GeoParquet row (module docstring, "Regions"):
    `region`, `states_crossed`, `extract_regions`, `scope_regions`, and `coverage_regions` =
    `scope_regions` narrowed to `covered` (the regions the load covers; `None` keeps the whole
    scope). The build labels every row; a row without labels is labelled here from `line` or
    `point` with `states`."""
    extracts = _strings(row.get("extract_regions"))
    region = str(row.get("region") or "")
    crossed = _strings(row.get("states_crossed"))
    if not region:
        fallback = extracts[0] if extracts else ""
        if line is not None:
            region, crossed = label_line(line, fallback, states)
        elif point is not None:
            region = label_point(point[0], point[1], fallback, states)
    crossed = sorted({c for c in crossed if c} | ({region} if region else set()))
    scope = _strings(row.get("scope_regions")) or scope_of(crossed, extracts)
    coverage = scope if covered is None else sorted(set(scope) & set(covered))
    return {
        "region": region or None,
        "states_crossed": crossed,
        "extract_regions": extracts,
        "scope_regions": scope,
        "coverage_regions": coverage,
    }


def normalise_lines(
    rows: Iterable[dict[str, Any]],
    *,
    licence: str,
    states: StateIndex | None = None,
    simplify_deg: float = ASSET_SIMPLIFY_DEG,
    covered: Collection[str] | None = None,
) -> pd.DataFrame:
    """GeoParquet line rows -> `transmission_line` asset rows (`ASSET_COLUMNS`), one per way.
    `covered` is the regions the load covers (`region_attributes`)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        geometry = wkb_to_geojson(row.get("geometry"))
        if not geometry or geometry["type"] != "LineString" or len(geometry["coordinates"]) < 2:
            continue
        full = [[float(x), float(y)] for x, y in geometry["coordinates"]]
        stored = [
            [round(x, COORD_PRECISION), round(y, COORD_PRECISION)] for x, y in simplify(full, simplify_deg)
        ]
        rep = representative_point([full])
        lon, lat = (rep[0], rep[1]) if rep else (full[0][0], full[0][1])
        regions = region_attributes(row, covered, line=full, states=states)
        osm_type, osm_id = str(row["osm_type"]), int(row["osm_id"])
        kv = row.get("voltage_kv")
        kv = float(kv) if kv is not None and not (isinstance(kv, float) and math.isnan(kv)) else None
        attributes: dict[str, Any] = {
            "voltage_kv": kv,
            "voltages_kv": [float(v) for v in (row.get("voltages_kv") or [])],
            "voltage_raw": row.get("voltage_raw"),
            "voltage_unknown": bool(row.get("voltage_unknown")),
            "voltage_assumed_kv": bool(row.get("voltage_assumed_kv")),
            "voltage_class": voltage_class(kv),
            "power": row.get("power"),
            "underground": bool(row.get("underground")),
            "location": row.get("location"),
            "circuits": _int_or_none(row.get("circuits")),
            "circuits_raw": row.get("circuits"),
            "cables": row.get("cables"),
            "frequency": row.get("frequency"),
            "operator": row.get("operator"),
            "osm_name": row.get("name"),
            "ref": row.get("ref"),
            "miles": round(geodesic_length_miles([full]), 3),
            "vertex_count": len(stored),
            "vertex_count_source": len(full),
            **regions,
            "osm_type": osm_type,
            "osm_id": osm_id,
        }
        out.append(
            {
                "source_id": SOURCE_ID,
                "source_asset_id": f"{osm_type}/{osm_id}",
                "name": line_display_name(row),
                "operator_name": None,  # module docstring: shown, not linked
                "owner_name": None,
                "status": "operating",
                "technology": None,
                "technology_raw": None,
                "capacity_value": None,
                "capacity_unit": None,
                "unit_count": None,
                "lon": round(lon, COORD_PRECISION),
                "lat": round(lat, COORD_PRECISION),
                "geom_line_wkt": wkt_multilinestring([stored], precision=COORD_PRECISION),
                "state_code": regions["region"],
                "county_name": None,
                "country": "US",
                "attributes": attributes,
                "source_url": element_url(osm_type, osm_id),
                "retrieved_at": row.get("extract_timestamp"),
                "licence": licence,
            }
        )
    return pd.DataFrame(out, columns=ASSET_COLUMNS)


def _outline_geojson(outline: bytes | None) -> dict[str, Any] | None:
    geometry = wkb_to_geojson(outline)
    if geometry is None:
        return None

    def rnd(ring: list[list[float]]) -> list[list[float]]:
        return [[round(x, COORD_PRECISION), round(y, COORD_PRECISION)] for x, y in ring]

    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rnd(r) for r in geometry["coordinates"]]}
    return {"type": "MultiPolygon", "coordinates": [[rnd(r) for r in p] for p in geometry["coordinates"]]}


def normalise_substations(
    rows: Iterable[dict[str, Any]],
    *,
    licence: str,
    states: StateIndex | None = None,
    covered: Collection[str] | None = None,
) -> pd.DataFrame:
    """GeoParquet substation rows -> `substation` asset rows: the point is the node or the area's
    centroid; the outline (an area's polygon) rides in `attributes.outline` as GeoJSON."""
    out: list[dict[str, Any]] = []
    for row in rows:
        point = wkb_to_geojson(row.get("geometry"))
        if not point or point["type"] != "Point":
            continue
        lon, lat = float(point["coordinates"][0]), float(point["coordinates"][1])
        regions = region_attributes(row, covered, point=(lon, lat), states=states)
        osm_type, osm_id = str(row["osm_type"]), int(row["osm_id"])
        kv = row.get("voltage_kv")
        kv = float(kv) if kv is not None and not (isinstance(kv, float) and math.isnan(kv)) else None
        attributes: dict[str, Any] = {
            "voltage_kv": kv,
            "voltages_kv": [float(v) for v in (row.get("voltages_kv") or [])],
            "voltage_raw": row.get("voltage_raw"),
            "voltage_unknown": bool(row.get("voltage_unknown")),
            "voltage_assumed_kv": bool(row.get("voltage_assumed_kv")),
            "voltage_class": voltage_class(kv),
            "substation_type": row.get("substation"),
            "operator": row.get("operator"),
            "osm_name": row.get("name"),
            "ref": row.get("ref"),
            "outline": _outline_geojson(row.get("outline")),
            "location_method": "node"
            if osm_type == "node"
            else ("area_centroid" if row.get("outline") else "vertex_mean"),
            **regions,
            "osm_type": osm_type,
            "osm_id": osm_id,
        }
        out.append(
            {
                "source_id": SOURCE_ID,
                "source_asset_id": f"{osm_type}/{osm_id}",
                "name": substation_display_name(row),
                "operator_name": None,
                "owner_name": None,
                "status": "operating",
                "technology": None,
                "technology_raw": None,
                "capacity_value": None,
                "capacity_unit": None,
                "unit_count": None,
                "lon": round(lon, COORD_PRECISION),
                "lat": round(lat, COORD_PRECISION),
                "geom_line_wkt": None,
                "state_code": regions["region"],
                "county_name": None,
                "country": "US",
                "attributes": attributes,
                "source_url": element_url(osm_type, osm_id),
                "retrieved_at": row.get("extract_timestamp"),
                "licence": licence,
            }
        )
    return pd.DataFrame(out, columns=ASSET_COLUMNS)


def _licence_text() -> str:
    from pipeline.connectors.registry import Registry

    return Registry().get(SOURCE_ID).license


def check_regions(regions: Sequence[str] | None) -> list[str] | None:
    """`regions` as sorted ISO codes (``US-TX``), or `None` for every region; a code that names no
    `US_STATE_EXTRACTS` region is an error, so a typo never silently loads nothing."""
    if regions is None:
        return None
    wanted = sorted({r.strip().upper() for r in regions if r.strip()})
    known = {code for code, _ in US_STATE_EXTRACTS}
    unknown = [r for r in wanted if r not in known]
    if unknown:
        raise BuildError(f"unknown region(s) {unknown}; use ISO codes such as US-TX")
    return wanted or None


def parse_regions(value: str) -> list[str] | None:
    """A regions setting: ``all`` -> `None` (every region the build has), else a comma list of ISO
    codes (`check_regions`)."""
    if value.strip().lower() == "all":
        return None
    wanted = check_regions(value.split(","))
    if wanted is None:
        raise BuildError("no region named; use ISO codes such as US-TX, or 'all'")
    return wanted


def default_regions() -> list[str] | None:
    """`GRID_LAYERS_REGIONS`, which must be set: a comma list of ISO codes the server loads
    (``US-TX,US-OK``), or ``all`` for every region the published build has. Unset or empty is
    refused rather than read as national: the national line index needs about 1.3 GB more per API
    process, which the one-server beta does not have (docs/25 §2.7, limit 3), so national is a
    choice someone writes down, never a default."""
    value = (os.environ.get("GRID_LAYERS_REGIONS") or "").strip()
    if not value:
        raise BuildError(
            "GRID_LAYERS_REGIONS is not set: name the states this server loads (US-TX for the beta), or "
            "'all' for the national layer (about 1.3 GB more per API process; docs/25 section 2.7, limit 3)"
        )
    return parse_regions(value)


def write_asset_frames(
    geo_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    *,
    with_states: bool = True,
    licence: str | None = None,
    regions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`geo_dir`'s GeoParquet (and manifest) -> `normalized/context/us.osm.power.{lines,substations}.parquet`.

    The covered regions are the manifest's, narrowed to `regions` when given (a region the build
    does not hold is reported in `regions_missing`, never covered). The frames hold exactly the rows
    `in_scope` of them, filtered in pyarrow before any row becomes a Python object
    (`read_geoparquet`); each asset row's `coverage_regions` is its scope within them, which the
    map's source rule reads; the manifest copied beside the frames lists them (`regions`, and
    `regions_loaded` = the filter asked for), which the loader reads to decide what a reload may
    withdraw. One rule for all three (module docstring, "Regions").

    The three files are written as temporary files first and then renamed into place, the manifest
    last, and all three carry one `FRAME_SET_KEY` token: a crash part-way leaves either the previous
    set whole or a mix whose tokens differ, which the loader refuses rather than pair new frames
    with the previous load's covered regions."""
    from pipeline.connectors.base import to_parquet_safe

    t0 = time.monotonic()
    out_dir = out_dir or NORMALIZED_DIR
    wanted = check_regions(regions)
    licence = licence if licence is not None else _licence_text()
    states = StateIndex.from_file(STATES_GEOJSON) if with_states and STATES_GEOJSON.exists() else None
    manifest = json.loads((geo_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    built = [str(r["region"]) for r in manifest.get("regions", []) if r.get("region")]
    covered = sorted(r for r in built if wanted is None or r in wanted)
    missing = sorted(set(wanted or ()) - set(built))
    row_filter = None if wanted is None else set(covered)
    line_rows = read_geoparquet(geo_dir / GEO_LINES_NAME, regions=row_filter)
    sub_rows = read_geoparquet(geo_dir / GEO_SUBSTATIONS_NAME, regions=row_filter)
    manifest["regions"] = [r for r in manifest.get("regions", []) if r.get("region") in covered]
    manifest["regions_loaded"] = wanted
    frame_set = uuid.uuid4().hex
    manifest[FRAME_SET_KEY] = frame_set
    lines = normalise_lines(line_rows, licence=licence, states=states, covered=covered)
    subs = normalise_substations(sub_rows, licence=licence, states=states, covered=covered)
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        for frame, name in ((lines, LINES_FILE), (subs, SUBSTATIONS_FILE)):
            table = pa.Table.from_pandas(to_parquet_safe(frame), preserve_index=False)
            meta = {**(table.schema.metadata or {}), FRAME_SET_KEY.encode(): frame_set.encode()}
            tmp = out_dir / f".{name}.{frame_set}.tmp"
            staged.append((tmp, out_dir / name))  # before the write, so a partial file is removed too
            pq.write_table(table.replace_schema_metadata(meta), tmp)
        tmp = out_dir / f".{FRAMES_MANIFEST_FILE}.{frame_set}.tmp"
        staged.append((tmp, out_dir / FRAMES_MANIFEST_FILE))
        tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        for tmp, final in staged:
            os.replace(tmp, final)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    return {
        "source_id": SOURCE_ID,
        "version": manifest.get("version"),
        "regions": covered,
        "regions_missing": missing,
        "lines": len(lines),
        "substations": len(subs),
        "out": str(out_dir),
        "elapsed_s": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------------- pull
def default_base_url() -> str | None:
    """Where the build's public files live: `GRID_LAYERS_URL`, else the tiles host of the
    deployment's domain (the bucket `basemap.pmtiles` is served from), else nothing."""
    explicit = (os.environ.get("GRID_LAYERS_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    domain = (os.environ.get("BANKABLE_DOMAIN") or "").strip()
    return f"https://tiles.{domain}/grid/osm-power" if domain else None


def _fetch(session: Any, url: str) -> bytes:
    resp = session.get(url, honour_robots=False, timeout=300)
    if resp.status_code != 200:
        raise BuildError(f"{url} answered HTTP {resp.status_code}")
    content: bytes = resp.content
    return content


def pull(
    base_url: str,
    *,
    work_dir: pathlib.Path,
    out_dir: pathlib.Path | None = None,
    session: Any = None,
    with_states: bool = True,
    regions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`<base_url>/latest.json` -> the version it names -> its two GeoParquet files, each checked
    against the manifest's sha256 before anything is written, then the asset frames (only
    `regions`' rows when given: `write_asset_frames`). Our own bucket, so robots.txt is not
    consulted. A version already held (`<out_dir>/us.osm.power.manifest.json`) with the same
    region filter is not downloaded again."""
    from pipeline.connectors.http import PoliteSession

    wanted = check_regions(regions)
    session = session or PoliteSession()
    out_dir = out_dir or NORMALIZED_DIR
    latest = json.loads(_fetch(session, f"{base_url}/{LATEST_NAME}"))
    version = str(latest.get("version") or "")
    if not version or ".." in version or version.startswith("/"):
        raise BuildError(f"{base_url}/{LATEST_NAME} names no usable version: {version!r}")
    held_path = out_dir / FRAMES_MANIFEST_FILE
    if held_path.exists() and (out_dir / LINES_FILE).exists() and (out_dir / SUBSTATIONS_FILE).exists():
        held = json.loads(held_path.read_text(encoding="utf-8"))
        if held.get("version") == version and held.get("regions_loaded") == wanted:
            return {"source_id": SOURCE_ID, "version": version, "status": "unchanged"}
    geo_dir = work_dir / "osm-power-pull"
    geo_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name in (GEO_LINES_NAME, GEO_SUBSTATIONS_NAME):
            expected = (latest.get("files") or {}).get(name) or {}
            content = _fetch(session, f"{base_url}/{version}/{name}")
            digest = hashlib.sha256(content).hexdigest()
            if not expected.get("sha256") or digest != expected["sha256"]:
                raise BuildError(
                    f"{name}: sha256 {digest} does not match the manifest's {expected.get('sha256')}"
                )
            (geo_dir / name).write_bytes(content)
        (geo_dir / MANIFEST_NAME).write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
        summary = write_asset_frames(geo_dir, out_dir, with_states=with_states, regions=wanted)
    finally:
        for name in (GEO_LINES_NAME, GEO_SUBSTATIONS_NAME, MANIFEST_NAME):
            (geo_dir / name).unlink(missing_ok=True)
    return {**summary, "status": "pulled", "base_url": base_url}


# ------------------------------------------------------------------------------- summaries
def measure(lines: pd.DataFrame, substations: pd.DataFrame) -> dict[str, Any]:
    """Counts the docs report: lines and miles by voltage class (and unknown), underground share,
    substations by subtype."""
    by_class: dict[str, dict[str, float]] = {}
    for attrs in lines["attributes"]:
        a = attrs if isinstance(attrs, dict) else json.loads(attrs)
        key = a.get("voltage_class") or "voltage unknown"
        bucket = by_class.setdefault(key, {"lines": 0, "miles": 0.0, "underground_miles": 0.0})
        bucket["lines"] += 1
        bucket["miles"] += float(a.get("miles") or 0.0)
        if a.get("underground"):
            bucket["underground_miles"] += float(a.get("miles") or 0.0)
    for bucket in by_class.values():
        bucket["miles"] = round(bucket["miles"], 1)
        bucket["underground_miles"] = round(bucket["underground_miles"], 1)
    subtypes: Counter[str] = Counter()
    sub_kinds: Counter[str] = Counter()
    for attrs in substations["attributes"]:
        a = attrs if isinstance(attrs, dict) else json.loads(attrs)
        subtypes[a.get("substation_type") or "(untagged)"] += 1
        sub_kinds[str(a.get("location_method"))] += 1
    return {
        "lines": len(lines),
        "line_miles": round(sum(b["miles"] for b in by_class.values()), 1),
        "by_voltage_class": dict(sorted(by_class.items())),
        "substations": len(substations),
        "substations_by_type": dict(subtypes.most_common()),
        "substations_by_location_method": dict(sub_kinds.most_common()),
    }


def _iter_inputs(values: Sequence[str]) -> Iterator[tuple[str, pathlib.Path]]:
    for value in values:
        region, _, path = value.partition("=")
        if not path:
            raise BuildError(f"--file takes REGION=PATH (US-TX=texas-latest.osm.pbf), got {value!r}")
        yield region.strip(), pathlib.Path(path.strip())


# ------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="OSM extracts -> GeoParquet + manifest (needs pyosmium)")
    src = p_build.add_mutually_exclusive_group(required=True)
    src.add_argument("--states", help="Download Geofabrik state extracts: 'all' or a list (US-TX,oklahoma)")
    src.add_argument(
        "--file", action="append", default=[], help="REGION=PATH of a local OSM file; repeatable"
    )
    p_build.add_argument("--timestamp", help="Data timestamp for a local file whose header states none")
    p_build.add_argument(
        "--clip", action="store_true", help="Cut each --file to its region's polygon (a file covering more)"
    )
    p_build.add_argument("--out-dir", type=pathlib.Path, required=True)
    p_build.add_argument("--work-dir", type=pathlib.Path, required=True)
    p_build.add_argument(
        "--normalise-out", type=pathlib.Path, help="Also write the asset frames into this directory (dev)"
    )

    p_pull = sub.add_parser("pull", help="Fetch the published GeoParquet and write the asset frames")
    p_pull.add_argument(
        "--base-url", help="Default: $GRID_LAYERS_URL, else https://tiles.$BANKABLE_DOMAIN/grid/osm-power"
    )
    p_pull.add_argument("--work-dir", type=pathlib.Path, default=DATA_DIR / "tmp")
    p_pull.add_argument("--out-dir", type=pathlib.Path)
    p_pull.add_argument(
        "--regions",
        help="ISO codes to load (US-TX,US-OK) or 'all'. Default: $GRID_LAYERS_REGIONS, which must be set",
    )

    p_check = sub.add_parser("check", help="May a new build replace the published one? Exit 1 if not")
    p_check.add_argument("--manifest", type=pathlib.Path, required=True)
    p_check.add_argument("--previous", type=pathlib.Path, help="The published latest.json, if there is one")
    p_check.add_argument("--max-drop", type=float, default=MAX_REGION_DROP)
    p_check.add_argument("--min-drop-rows", type=int, default=MIN_REGION_DROP_ROWS)

    p_norm = sub.add_parser("normalise", help="GeoParquet directory -> asset frames, no network")
    p_norm.add_argument("--geo-dir", type=pathlib.Path, required=True)
    p_norm.add_argument("--out-dir", type=pathlib.Path)
    p_norm.add_argument("--no-states", action="store_true")
    p_norm.add_argument("--regions", help="ISO codes to keep (US-TX,US-OK) or 'all'. Default: every region")

    args = parser.parse_args(argv)
    summary: dict[str, Any]
    if args.command == "build":
        if args.states:
            inputs: list[tuple[str, pathlib.Path | str]] = list(select_states(args.states))
            summary = build(inputs, work_dir=args.work_dir, out_dir=args.out_dir, from_geofabrik=True)
        else:
            files: list[tuple[str, pathlib.Path | str]] = list(_iter_inputs(args.file))
            summary = build(
                files, work_dir=args.work_dir, out_dir=args.out_dir, timestamp=args.timestamp, clip=args.clip
            )
        if args.normalise_out:
            summary = {
                "manifest": summary,
                "normalised": write_asset_frames(args.out_dir, args.normalise_out),
            }
    elif args.command == "pull":
        base_url = args.base_url or default_base_url()
        if not base_url:
            raise SystemExit("osm_power pull: set GRID_LAYERS_URL or BANKABLE_DOMAIN, or pass --base-url")
        try:
            regions = parse_regions(args.regions) if args.regions else default_regions()
        except BuildError as exc:
            raise SystemExit(f"osm_power pull: {exc}") from exc
        summary = pull(base_url.rstrip("/"), work_dir=args.work_dir, out_dir=args.out_dir, regions=regions)
    elif args.command == "check":
        new = json.loads(args.manifest.read_text(encoding="utf-8"))
        previous = (
            json.loads(args.previous.read_text(encoding="utf-8"))
            if args.previous and args.previous.exists()
            else None
        )
        problems = compare_manifests(new, previous, max_drop=args.max_drop, min_rows=args.min_drop_rows)
        print(json.dumps({"version": new.get("version"), "promote": not problems, "problems": problems}))  # noqa: T201
        if problems:
            raise SystemExit(1)
        return
    else:
        summary = write_asset_frames(
            args.geo_dir,
            args.out_dir,
            with_states=not args.no_states,
            regions=parse_regions(args.regions) if args.regions else None,
        )
    print(json.dumps(summary, default=str))  # noqa: T201 -- CLI summary line, same convention as eia_atlas.py


if __name__ == "__main__":
    main()
