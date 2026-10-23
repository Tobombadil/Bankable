"""`GET /v1/site-finder` and `GET /v1/site-finder/geo`: the powered-land site finder (owner decision
2026-10-22, docs/00-PLAN.md: sites within 1 mile of a 69 kV-or-higher line, within 5 miles of a
substation, outside the FEMA floodplain; all three adjustable).

The candidates are precomputed per segment of about half a mile (`services/site_finder/compute.py`,
table `site_candidate`). A request filters segments of the newest `ok` run, then merges the adjacent
passing segments of one line into **corridor bands** in SQL (a gaps-and-islands window over the
segment index), sorts and pages the bands, and only then reads what each band on the page shows.

**Criteria** (defaults in brackets): `min_kv` [69] (the line's highest stated voltage; 69 is the
floor the candidates were computed at), `adjacent_mi` [1] (the band's half-width: 0.25, 0.5, 1 or 2,
the widths the flood shares are stored at), `max_substation_mi` [5] (up to 10, the search radius,
rounded to a quarter mile), `exclude_flood` [true], `include_unknown_voltage` [false],
`exclude_protected` [false], `state`, `county` (five-digit FIPS), `sort` [`-voltage_kv`].

**Flood rule.** With `exclude_flood=true` a segment passes when its band has flood data throughout
(inside the FEMA build's area of interest and FEMA's effective digital map, not Zone D) and at least
`1 - MAX_FLOOD_SHARE` of it lies outside the Special Flood Hazard Area; the band's geometry then has
the floodplain cut out. A segment without flood data never passes as "outside": the summary says how
many miles met every other criterion but have no flood data (`flood_unavailable_mi`), and
`exclude_flood=false` shows them with status `unavailable`. Not a flood determination (44 CFR 65).

**Tier.** A band's line must be visible at the caller's tier (its source and licence, as an asset's
are, and a licence that lets its coordinates be served). Nothing aggregated over proposals is stored,
and nothing derived from one but its id and distance: a segment keeps each nearby large-load request
as (proposal id, miles) and each queue request naming a grid connection point as (proposal id, point
id, miles), whatever their visibility. Here a request is kept only when the caller's tier may see it
(`proposal_visibility_filter`, so not unpublished, not inside the free tier's delay, not from a
gated source) and may be served its exact location (`location_exact_permitted`, the rule
nearby-proposals applies); a connection point's nearest distance, queued MW and count are taken from
those survivors within the radius only, never from the point's other requests. Retiring plants are
assets, each re-read under the caller's tier; a band's retiring MW, shown and sorted on alike, is the
sum over the distinct visible plants near any of its segments.

**Cost.** The window query that merges segments into bands runs once per (run, tier, criteria but the
voltage floor) and is cached: a line's segments share its voltage, so the floor and the
unknown-voltage flag cannot change a band's extent and filter the cached bands instead, and
`max_substation_mi` is rounded to a quarter mile, so the store-wide query has a bounded set of keys.
Each sort's order is cached beside its bands; a page is a bisect and a slice.

**Provenance.** Every value names its source: the line and substation (OpenStreetMap, ODbL; "©
OpenStreetMap contributors"), the flood shares (FEMA NFHL), the county (Census TIGER/Line), each plant,
request and point (their own rows), each prospecting layer (its build's provenance and the sentence of
what the value is not). `licence_summary` credits every source a returned value came from. A band is
drawn from the vertices its run stored (`SiteCandidate.coords`), so a context load that later moves
a line cannot misplace a band the run measured.
"""

from __future__ import annotations

import bisect
import math
import os
import pathlib
import re
import threading
import time
import uuid as _uuid
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, defer, load_only, selectinload

from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import invalid_cursor, validation_error
from services.api.interconnection_points import PointTotals, point_proposal_filter
from services.api.lines import simplify_parts, tolerance_for_zoom
from services.api.pagination import decode_cursor, encode_cursor
from services.api.params import check_allowed, page_limit
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_row,
    provenance_quartet,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    asset_geometry_permitted,
    asset_visibility_filter,
    gated_record,
    interconnection_point_visibility_filter,
    location_exact_permitted,
    organization_visibility_filter,
    permitted_source_states,
    proposal_visibility_filter,
    visible_source_links,
)
from services.db.models import (
    SITE_FINDER_DISTANCES,
    Asset,
    AssetOwner,
    InterconnectionPoint,
    Licence,
    Location,
    Organization,
    Proposal,
    SiteCandidate,
    SiteFinderRun,
    Source,
)
from services.site_finder import prospect as prospect_mod
from services.site_finder.compute import CONTEXT_RADIUS_MI, SUBSTATION_SEARCH_MI, current_run
from services.site_finder.counties import BOUNDARY_NOTE
from services.site_finder.flood import FULL_COVER, NOT_A_DETERMINATION, SfhaPieces
from services.site_finder.geometry import (
    METRES_PER_MILE,
    LocalPlane,
    join_segments,
    length_mi,
    sub_line,
)
from services.site_finder.provenance import publishable, registry, registry_provenance

router = APIRouter()

#: The widths the flood shares are stored at (miles -> column suffix).
ADJACENT_MI: dict[float, str] = {d: k for k, d in SITE_FINDER_DISTANCES.items()}
DEFAULTS = {"min_kv": 69.0, "adjacent_mi": 1.0, "max_substation_mi": 5.0}
MIN_KV_FLOOR = 69.0
#: `max_substation_mi` is rounded to this grid (miles); the page's slider moves in half miles.
SUBSTATION_STEP_MI = 0.25
#: A segment passes `exclude_flood` when at most this share of its band is floodplain.
MAX_FLOOD_SHARE = 0.9
#: "Mostly on protected land": more than half the 1-mile band on GAP 1-2 land.
MOSTLY_PROTECTED = 0.5
SORTS = ("voltage_kv", "substation_mi", "length_mi", "retiring_mw")
DEFAULT_SORT = "-voltage_kv"
CRITERIA_PARAMS = {
    "min_kv",
    "adjacent_mi",
    "max_substation_mi",
    "exclude_flood",
    "include_unknown_voltage",
    "exclude_protected",
    "state",
    "county",
}
LIST_PARAMS = CRITERIA_PARAMS | {"sort", "limit", "cursor", "band", "geometry"}
#: A band's id: its line's public id, then its first and last segment index (`serialize_band`).
BAND_ID = re.compile(r"^(?P<line>[A-Za-z0-9_-]{1,64})\.(?P<first>\d{1,6})-(?P<last>\d{1,6})$")
GEO_PARAMS = CRITERIA_PARAMS | {"bbox", "zoom"}
DEFAULT_LIMIT, MAX_LIMIT = 25, 100
#: Bands drawn by the map endpoint at once, longest first.
GEO_CAP = 2000
#: The band's buffer is simplified by this many metres before the floodplain is cut out of it, so the
#: cut edge is the flood files' own (10 m precision) and no outline reaches into the floodplain; the
#: served coordinates are then rounded to `OUTLINE_PRECISION_DEG` (about a decimetre).
GEOMETRY_TOLERANCE_M = 15.0
OUTLINE_PRECISION_DEG = 1e-6
#: Reader-facing voltage classes (the page colours lines by these); `key` is the class's usual
#: nominal voltage, the label its exact range (a 161 kV line is in the 100-161 class).
VOLTAGE_CLASSES: tuple[tuple[str, str, float, float], ...] = (
    ("69", "69-99 kV", 69.0, 100.0),
    ("115", "100-161 kV", 100.0, 162.0),
    ("230", "162-287 kV", 162.0, 288.0),
    ("345", "288-399 kV", 288.0, 400.0),
    ("500", "400 kV and up", 400.0, math.inf),
)
FLOOD_STATUS_LABELS = {
    "outside": "Outside the floodplain",
    "partly_in": "Partly in the floodplain (cut out of the band)",
    "unavailable": "Flood data unavailable",
}
FLOOD_REASON_LABELS = {
    "not_computed": "not computed here yet (outside the areas the flood build covers)",
    "no_digital_map": "FEMA has no effective digital flood map here",
    "zone_d": "FEMA Zone D: flood hazard undetermined",
    "not_included": "an area FEMA's map does not include",
    "unrecognised": "a flood zone the build did not recognise",
}
SCREENING_NOTE = (
    "A screening list of corridor bands along public grid data, not a site assessment: it cannot see "
    "spare capacity at a substation, zoning, land ownership, easements or water. Ask the utility first."
)
GRID_CONNECTION_NOTE = (
    "Named by the queue requests within 5 miles of the band that this tier may see, with an exact "
    "location; the queued MW counts only those requests. A connection point has no location of its own."
)
#: Rows per `IN (...)` list.
_IN_CHUNK = 500
#: The most counties the facets list with no state chosen; past it the picker asks for a state.
COUNTY_FACET_CAP = 600


def voltage_class(kv: float | None) -> tuple[str, str]:
    if kv is None:
        return "unknown", "voltage not stated"
    for key, label, lo, hi in VOLTAGE_CLASSES:
        if lo <= kv < hi:
            return key, label
    return "69", "69-99 kV"


# ------------------------------------------------------------------------------------- criteria
@dataclass(frozen=True)
class Criteria:
    min_kv: float
    adjacent_mi: float
    max_substation_mi: float
    exclude_flood: bool
    include_unknown_voltage: bool
    exclude_protected: bool
    state: str | None
    county: str | None

    @property
    def key(self) -> str:
        return ADJACENT_MI[self.adjacent_mi]

    @property
    def base(self) -> Criteria:
        """These criteria without the voltage floor and with unknown voltages in: the key of the cached
        band list. A line's segments all carry its voltage, so the floor keeps or drops whole bands and
        never changes one's extent; `voltage_ok` applies it to the cached list."""
        return replace(self, min_kv=MIN_KV_FLOOR, include_unknown_voltage=True)

    def voltage_ok(self, voltage_kv: float | None, unknown: Any) -> bool:
        if unknown:
            return self.include_unknown_voltage
        return voltage_kv is not None and voltage_kv >= self.min_kv

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_kv": self.min_kv,
            "adjacent_mi": self.adjacent_mi,
            "max_substation_mi": self.max_substation_mi,
            "exclude_flood": self.exclude_flood,
            "include_unknown_voltage": self.include_unknown_voltage,
            "exclude_protected": self.exclude_protected,
            "state": self.state,
            "county": self.county,
        }


def _number(request: Request, name: str, default: float) -> float:
    raw = request.query_params.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise validation_error(name, f"{name} must be a number", request.url.path) from None
    if not math.isfinite(value):
        raise validation_error(name, f"{name} must be a finite number", request.url.path)
    return value


def _flag(request: Request, name: str, default: bool) -> bool:
    raw = request.query_params.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise validation_error(name, f"{name} must be true or false", request.url.path)


def snap_substation(value: float) -> float:
    """`max_substation_mi` on its quarter-mile grid (never under a quarter mile)."""
    return max(SUBSTATION_STEP_MI, round(value / SUBSTATION_STEP_MI) * SUBSTATION_STEP_MI)


def parse_criteria(request: Request) -> Criteria:
    path = request.url.path
    min_kv = _number(request, "min_kv", DEFAULTS["min_kv"])
    if not MIN_KV_FLOOR <= min_kv <= 1500:
        raise validation_error("min_kv", "min_kv must be between 69 and 1500 (kV)", path)
    adjacent = _number(request, "adjacent_mi", DEFAULTS["adjacent_mi"])
    if adjacent not in ADJACENT_MI:
        allowed = ", ".join(f"{d:g}" for d in sorted(ADJACENT_MI))
        raise validation_error("adjacent_mi", f"adjacent_mi must be one of {allowed} (miles)", path)
    max_sub = _number(request, "max_substation_mi", DEFAULTS["max_substation_mi"])
    if not 0 < max_sub <= SUBSTATION_SEARCH_MI:
        raise validation_error(
            "max_substation_mi",
            f"max_substation_mi must be above 0 and at most {SUBSTATION_SEARCH_MI:g}",
            path,
        )
    state = (request.query_params.get("state") or "").strip().upper() or None
    if state is not None:
        state = state if state.startswith("US-") else f"US-{state}"
        if len(state) != 5 or not state[3:].isalpha():
            raise validation_error("state", "state must be a US state code such as TX or US-TX", path)
    county = (request.query_params.get("county") or "").strip() or None
    if county is not None and not (len(county) == 5 and county.isdigit()):
        raise validation_error("county", "county must be a five-digit county FIPS code such as 48453", path)
    return Criteria(
        min_kv=min_kv,
        adjacent_mi=adjacent,
        # The value applied is echoed in `meta.site_finder.criteria`.
        max_substation_mi=snap_substation(max_sub),
        exclude_flood=_flag(request, "exclude_flood", True),
        include_unknown_voltage=_flag(request, "include_unknown_voltage", False),
        exclude_protected=_flag(request, "exclude_protected", False),
        state=state,
        county=county,
    )


def parse_sort(request: Request) -> tuple[str, bool]:
    raw = (request.query_params.get("sort") or "").strip() or DEFAULT_SORT
    descending = raw.startswith("-")
    field = raw.lstrip("-")
    if field not in SORTS:
        raise validation_error(
            "sort", f"sort must be one of {', '.join(SORTS)}, optionally prefixed with -", request.url.path
        )
    return field, descending


# ------------------------------------------------------------------------------------ the query
def candidate_visibility_filter(entitlement: str) -> list[ColumnElement[bool]]:
    """`asset_visibility_filter`'s two clauses over the candidate's own quartet, which is its line's,
    with `asset_geometry_permitted`'s too: a band is drawn from the line's coordinates."""
    lic = aliased(Licence)
    src = aliased(Source)
    return [
        exists(
            select(lic.id).where(
                lic.id == SiteCandidate.licence_id,
                lic.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES),
                lic.allows_raw_publication.is_(True),
            )
        ),
        exists(
            select(src.id).where(
                src.id == SiteCandidate.source_id, src.publish_state.in_(permitted_source_states(entitlement))
            )
        ),
    ]


def segment_filters(
    c: Criteria, run_id: _uuid.UUID, entitlement: str, *, flood: bool = True, place: bool = True
) -> list[ColumnElement[bool]]:
    share = getattr(SiteCandidate, f"flood_share_{c.key}")
    cover = getattr(SiteCandidate, f"flood_cover_{c.key}")
    voltage: ColumnElement[bool] = SiteCandidate.voltage_kv >= c.min_kv
    if c.include_unknown_voltage:
        voltage = or_(voltage, SiteCandidate.voltage_unknown.is_(True))
    clauses: list[ColumnElement[bool]] = [
        SiteCandidate.run_id == run_id,
        voltage,
        SiteCandidate.substation_mi <= c.max_substation_mi,
        *candidate_visibility_filter(entitlement),
    ]
    if flood and c.exclude_flood:
        clauses += [cover >= FULL_COVER, share <= MAX_FLOOD_SHARE]
    if c.exclude_protected:
        clauses.append(
            or_(
                SiteCandidate.hard_protected_share.is_(None),
                SiteCandidate.hard_protected_share <= MOSTLY_PROTECTED,
            )
        )
    if place and c.state:
        clauses.append(SiteCandidate.state_code == c.state)
    if place and c.county:
        clauses.append(SiteCandidate.county_fips == c.county)
    return clauses


def bands_subquery(c: Criteria, clauses: Sequence[ColumnElement[bool]]) -> Any:
    """Passing segments merged into bands: adjacent segment indexes of one line share an island
    (`segment_index - row_number()`), and each island is one band."""
    share = getattr(SiteCandidate, f"flood_share_{c.key}")
    cover = getattr(SiteCandidate, f"flood_cover_{c.key}")
    seg = (
        select(
            SiteCandidate.line_asset_id.label("line_id"),
            SiteCandidate.segment_index.label("idx"),
            SiteCandidate.length_mi.label("length_mi"),
            SiteCandidate.voltage_kv.label("voltage_kv"),
            sa.cast(SiteCandidate.voltage_unknown, sa.Integer).label("unknown"),
            SiteCandidate.substation_mi.label("substation_mi"),
            share.label("share"),
            cover.label("cover"),
            SiteCandidate.min_lon,
            SiteCandidate.min_lat,
            SiteCandidate.max_lon,
            SiteCandidate.max_lat,
            (
                SiteCandidate.segment_index
                - func.row_number().over(
                    partition_by=SiteCandidate.line_asset_id, order_by=SiteCandidate.segment_index
                )
            ).label("island"),
        )
        .where(*clauses)
        .subquery("seg")
    )
    return (
        select(
            seg.c.line_id,
            func.min(seg.c.idx).label("first"),
            func.max(seg.c.idx).label("last"),
            func.count().label("segments"),
            func.sum(seg.c.length_mi).label("length_mi"),
            func.max(seg.c.voltage_kv).label("voltage_kv"),
            func.max(seg.c.unknown).label("unknown"),
            func.min(seg.c.substation_mi).label("substation_mi"),
            func.min(seg.c.cover).label("cover"),
            (func.sum(func.coalesce(seg.c.share, 0.0) * seg.c.length_mi) / func.sum(seg.c.length_mi)).label(
                "share"
            ),
            func.max(seg.c.share).label("max_share"),
            func.min(seg.c.min_lon).label("min_lon"),
            func.min(seg.c.min_lat).label("min_lat"),
            func.max(seg.c.max_lon).label("max_lon"),
            func.max(seg.c.max_lat).label("max_lat"),
        )
        .group_by(seg.c.line_id, seg.c.island)
        .subquery("band")
    )


# --------------------------------------------------------------------------------- small caches
class _Ttl:
    """Values per (run, tier, criteria): the run never changes, so only a gate flip (a source
    unpublished) can move them; 60 s bounds that, as `geo_cache` does for the map."""

    def __init__(self, ttl_s: float = 60.0, max_entries: int = 64) -> None:
        self.ttl, self.max = ttl_s, max_entries
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None or time.monotonic() - hit[0] > self.ttl:
                return None
            self._data.move_to_end(key)
            return hit[1]

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


#: The summary of one request's criteria (bands, miles, the miles without flood data, the facets).
_SUMMARY = _Ttl()
#: The grouped rows the summary is built from, per (run, tier, criteria but the voltage floor).
_GROUPS = _Ttl()
#: Every band per (run, tier, criteria but the voltage floor), so a floor, a sort, a page or the map's
#: next view is a filter of one list, not another pass of the window query (about 0.8 s over Texas's
#: 120,699 segments on SQLite).
_BANDS = _Ttl(max_entries=16)
#: Each sort's order of those bands, so a page is a bisect and a slice.
_ORDERED = _Ttl(max_entries=32)
#: The MW of each retiring plant a run lists, visible at the tier, per (run, tier).
_PLANT_MW = _Ttl(max_entries=8)
#: Band polygons (buffer less floodplain) per (run, line, segments, width, cut): geometry only, never
#: a visibility decision, so an hour is safe; the run's inputs do not change under it.
_POLYGONS = _Ttl(ttl_s=3600.0, max_entries=4000)
#: Per run: each line's segments with retiring plants listed, and those plants (ids only).
_PLANT_SEGS: OrderedDict[str, dict[_uuid.UUID, tuple[list[int], list[tuple[str, ...]]]]] = OrderedDict()
_PLANT_LOCK = threading.Lock()


def clear_caches() -> None:
    """Forget every cached band, order, summary, polygon, plant, line and flood reader (tests; a new
    run is keyed apart anyway)."""
    for cache in (_SUMMARY, _GROUPS, _BANDS, _ORDERED, _PLANT_MW, _POLYGONS):
        cache.clear()
    with _PLANT_LOCK:
        _PLANT_SEGS.clear()
    with _LINES_LOCK:
        _LINES.clear()
    with _SFHA_LOCK:
        _SFHA.clear()


def _chunks(items: Sequence[Any], size: int = _IN_CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _ids(values: Iterable[Any]) -> list[_uuid.UUID]:
    out = []
    for v in values:
        try:
            out.append(v if isinstance(v, _uuid.UUID) else _uuid.UUID(str(v)))
        except (TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class _Plants:
    """The retiring plants a run lists near each line's segments, and the MW of those the tier may see:
    a band's retiring MW is the sum over the distinct visible plants near any of its segments, the
    same figure the drawer and the list show (`serialize_band`)."""

    by_line: dict[_uuid.UUID, tuple[list[int], list[tuple[str, ...]]]]
    mw: dict[str, float]

    def band_mw(self, line_id: _uuid.UUID, first: int, last: int) -> float | None:
        entry = self.by_line.get(line_id)
        if entry is None:
            return None
        indexes, plants = entry
        seen: set[str] = set()
        for k in range(bisect.bisect_left(indexes, first), bisect.bisect_right(indexes, last)):
            seen.update(p for p in plants[k] if p in self.mw)
        return round(sum(self.mw[p] for p in seen), 1) if seen else None


def _plants(db: Session, run_id: _uuid.UUID, entitlement: str) -> _Plants:
    key = str(run_id)
    with _PLANT_LOCK:
        by_line = _PLANT_SEGS.get(key)
    if by_line is None:
        rows: dict[_uuid.UUID, list[tuple[int, tuple[str, ...]]]] = {}
        # Only the `plants` member of each segment's context (a JSON path on both dialects).
        for line_id, index, listed in db.execute(
            select(
                SiteCandidate.line_asset_id, SiteCandidate.segment_index, SiteCandidate.context["plants"]
            ).where(SiteCandidate.run_id == run_id, SiteCandidate.retiring_count > 0)
        ):
            plant_ids = tuple(str(p[0]) for p in (listed or []) if p)
            if plant_ids:
                rows.setdefault(line_id, []).append((int(index), plant_ids))
        by_line = {}
        for line_id, entries in rows.items():
            entries.sort()
            by_line[line_id] = ([i for i, _ in entries], [p for _, p in entries])
        with _PLANT_LOCK:
            _PLANT_SEGS[key] = by_line
            while len(_PLANT_SEGS) > 2:
                _PLANT_SEGS.popitem(last=False)
    mw = _PLANT_MW.get((key, entitlement))
    if mw is None:
        listed = sorted({p for _, lists in by_line.values() for plants in lists for p in plants})
        mw = {}
        for chunk in _chunks(_ids(listed)):
            for asset_id, capacity in db.execute(
                select(Asset.id, Asset.capacity_mw).where(
                    Asset.id.in_(list(chunk)), *asset_visibility_filter(entitlement)
                )
            ):
                mw[str(asset_id)] = float(capacity) if capacity is not None else 0.0
        _PLANT_MW.put((key, entitlement), mw)
    return _Plants(by_line, mw)


@dataclass(frozen=True)
class BandRow:
    """One band as `bands_subquery` returns it. `retiring_mw` is filled (`_Plants.band_mw`) only for
    the retiring sort, the one order that needs every band's figure."""

    line_id: _uuid.UUID
    first: int
    last: int
    segments: int
    length_mi: float
    voltage_kv: float | None
    unknown: int
    substation_mi: float | None
    retiring_mw: float | None
    cover: float | None
    share: float | None
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def tiebreak(self) -> tuple[str, int]:
        return str(self.line_id), self.first


def _band_row(r: Any) -> BandRow:
    return BandRow(
        line_id=r.line_id,
        first=int(r.first),
        last=int(r.last),
        segments=int(r.segments),
        length_mi=float(r.length_mi or 0.0),
        voltage_kv=None if r.voltage_kv is None else float(r.voltage_kv),
        unknown=int(r.unknown or 0),
        substation_mi=None if r.substation_mi is None else float(r.substation_mi),
        retiring_mw=None,
        cover=None if r.cover is None else float(r.cover),
        share=None if r.share is None else float(r.share),
        min_lon=float(r.min_lon),
        min_lat=float(r.min_lat),
        max_lon=float(r.max_lon),
        max_lat=float(r.max_lat),
    )


def base_bands(db: Session, c: Criteria, run_id: _uuid.UUID, entitlement: str) -> list[BandRow]:
    """Every band under `c.base` (no voltage floor, unknown voltages in), cached."""
    base = c.base
    key = (str(run_id), entitlement, base)
    hit = _BANDS.get(key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    band = bands_subquery(base, segment_filters(base, run_id, entitlement))
    rows = [_band_row(r) for r in db.execute(select(band)).all()]
    _BANDS.put(key, rows)
    return rows


def band_rows(db: Session, c: Criteria, run_id: _uuid.UUID, entitlement: str) -> list[BandRow]:
    """The bands that pass `c`: the cached base bands whose line meets the voltage floor."""
    return [b for b in base_bands(db, c, run_id, entitlement) if c.voltage_ok(b.voltage_kv, b.unknown)]


def band_sort_value(b: BandRow, field: str, descending: bool) -> float:
    """One number per band that orders it, ascending (negated for descending), with a tie-break built
    in: voltage, then band length (at one voltage the longest band first, so a substation's
    switchyard stubs do not fill the first page); substation distance, then voltage; band length;
    retiring MW nearby, then voltage. The keyset cursor carries it, then (line id, first segment)."""
    kv = b.voltage_kv if b.voltage_kv is not None else 0.0
    sub = b.substation_mi if b.substation_mi is not None else 99.0
    if field == "voltage_kv":
        key = kv * 1e6 + min(b.length_mi, 99_999.0)
    elif field == "substation_mi":
        key = sub * 1e4 + (2000.0 - kv)
    elif field == "length_mi":
        key = b.length_mi
    else:
        key = (b.retiring_mw or 0.0) * 1e4 + kv
    return -key if descending else key


@dataclass(frozen=True)
class _Ordered:
    """Bands in one sort's order with their keyset keys (sort value, line id, first segment)."""

    keys: list[tuple[float, str, int]]
    rows: list[BandRow]


def _with_retiring(
    db: Session, rows: Iterable[BandRow], run_id: _uuid.UUID, entitlement: str
) -> list[BandRow]:
    plants = _plants(db, run_id, entitlement)
    return [replace(b, retiring_mw=plants.band_mw(b.line_id, b.first, b.last)) for b in rows]


def _order(rows: Iterable[BandRow], field: str, descending: bool) -> _Ordered:
    decorated = sorted(
        (((band_sort_value(b, field, descending), *b.tiebreak()), b) for b in rows), key=lambda t: t[0]
    )
    return _Ordered([k for k, _ in decorated], [b for _, b in decorated])


def ordered_bands(
    db: Session, c: Criteria, run_id: _uuid.UUID, entitlement: str, field: str, descending: bool
) -> _Ordered:
    """`c.base`'s bands in one sort's order, cached; `_page` applies the voltage floor as it reads."""
    key = (str(run_id), entitlement, c.base, field, descending)
    hit = _ORDERED.get(key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    rows = base_bands(db, c, run_id, entitlement)
    if field == "retiring_mw":
        rows = _with_retiring(db, rows, run_id, entitlement)
    ordered = _order(rows, field, descending)
    _ORDERED.put(key, ordered)
    return ordered


def _page(
    ordered: _Ordered, c: Criteria, after: tuple[float, str, int] | None, limit: int
) -> tuple[list[tuple[tuple[float, str, int], BandRow]], bool]:
    """Up to `limit` bands passing `c` after the cursor's key, and whether more follow."""
    start = bisect.bisect_right(ordered.keys, after) if after is not None else 0
    out: list[tuple[tuple[float, str, int], BandRow]] = []
    for i in range(start, len(ordered.rows)):
        b = ordered.rows[i]
        if c.voltage_ok(b.voltage_kv, b.unknown):
            out.append((ordered.keys[i], b))
            if len(out) > limit:
                break
    return out[:limit], len(out) > limit


def _grouped(db: Session, key: tuple[Any, ...], stmt: Any) -> list[tuple[Any, ...]]:
    hit = _GROUPS.get(key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    rows = [tuple(r) for r in db.execute(stmt).all()]
    _GROUPS.put(key, rows)
    return rows


def summary_and_facets(db: Session, c: Criteria, run_id: _uuid.UUID, entitlement: str) -> dict[str, Any]:
    """Bands and miles under `c`; the miles that meet every other criterion but have no flood data;
    and the place facets (every criterion but the place ones). The SQL groups by line voltage under
    `c.base`, so a new voltage floor is a filter of cached rows.

    Counties are listed for the state chosen or, with none chosen, for every state while they number
    at most `COUNTY_FACET_CAP` (Texas's run, with its border segments in five neighbours, has 284);
    past that the list is empty and `counties_need_state` is true (a national picker of 3,000
    counties is no picker)."""
    key = (str(run_id), entitlement, c)
    hit = _SUMMARY.get(key)
    if hit is not None:
        return dict(hit)
    bands = band_rows(db, c, run_id, entitlement)
    out: dict[str, Any] = {"bands": len(bands), "miles": round(sum(b.length_mi for b in bands), 1)}
    base = c.base
    run_key = str(run_id)
    if c.exclude_flood:
        cover = getattr(SiteCandidate, f"flood_cover_{c.key}")
        groups = _grouped(
            db,
            ("unavailable", run_key, entitlement, base),
            select(
                SiteCandidate.voltage_kv,
                SiteCandidate.voltage_unknown,
                func.coalesce(func.sum(SiteCandidate.length_mi), 0.0),
            )
            .where(*segment_filters(base, run_id, entitlement, flood=False), cover < FULL_COVER)
            .group_by(SiteCandidate.voltage_kv, SiteCandidate.voltage_unknown),
        )
        out["flood_unavailable_mi"] = round(
            sum(float(mi or 0.0) for kv, unknown, mi in groups if c.voltage_ok(kv, unknown)), 1
        )
    anywhere = replace(base, state=None, county=None)
    places = _grouped(
        db,
        ("places", run_key, entitlement, anywhere),
        select(
            SiteCandidate.state_code,
            SiteCandidate.county_fips,
            SiteCandidate.county_name,
            SiteCandidate.voltage_kv,
            SiteCandidate.voltage_unknown,
            func.sum(SiteCandidate.length_mi),
        )
        .where(*segment_filters(anywhere, run_id, entitlement))
        .group_by(
            SiteCandidate.state_code,
            SiteCandidate.county_fips,
            SiteCandidate.county_name,
            SiteCandidate.voltage_kv,
            SiteCandidate.voltage_unknown,
        ),
    )
    states: dict[str, float] = {}
    by_county: dict[tuple[str, str | None, str | None], float] = {}
    for state, fips, name, kv, unknown, mi in places:
        if not c.voltage_ok(kv, unknown):
            continue
        if state:
            states[state] = states.get(state, 0.0) + float(mi or 0.0)
        if fips:
            k = (str(fips), name, state)
            by_county[k] = by_county.get(k, 0.0) + float(mi or 0.0)
    need_state = c.state is None and len(by_county) > COUNTY_FACET_CAP
    counties = [
        {"county_fips": fips, "county_name": name, "state_code": state, "miles": round(mi, 1)}
        for (fips, name, state), mi in by_county.items()
        if not need_state and (c.state is None or state == c.state)
    ]
    counties.sort(key=lambda r: (str(r["state_code"]), str(r["county_name"])))
    out["facets"] = {
        "states": [{"state_code": s, "miles": round(m, 1)} for s, m in sorted(states.items())],
        "counties": counties,
        "counties_need_state": need_state,
    }
    _SUMMARY.put(key, out)
    return dict(out)


# --------------------------------------------------------------------------------- band detail
def _data_root() -> pathlib.Path:
    from pipeline.connectors.store import DATA_DIR

    env = os.environ.get("INFRAQUE_DATA_DIR")
    return pathlib.Path(env) if env else DATA_DIR


_SFHA: dict[str, SfhaPieces | None] = {}
_SFHA_LOCK = threading.Lock()


def _sfha_readers(run: SiteFinderRun) -> list[SfhaPieces]:
    """The run's flood build's SFHA files, where this server holds them (the worker's data root)."""
    from pipeline.context.fema_nfhl import local_path

    readers: list[SfhaPieces] = []
    for state in ((run.inputs or {}).get("flood") or {}).get("states", {}).values():
        key = state.get("sfha_key")
        if not key:
            continue
        with _SFHA_LOCK:
            if key not in _SFHA:
                path = local_path(str(key), _data_root())
                _SFHA[key] = SfhaPieces(path) if path.exists() else None
            reader = _SFHA[key]
        if reader is not None:
            readers.append(reader)
    return readers


def band_centreline(segs: Sequence[SiteCandidate]) -> Any:
    """The band's centreline: its segments' vertices as the run stored them, joined."""
    return join_segments([s.coords for s in segs if s.coords])


def band_polygon(
    centre: Any, adjacent_mi: float, sfha: Sequence[SfhaPieces], cut_floodplain: bool
) -> tuple[Any, bool]:
    """The band: the centreline's buffer at `adjacent_mi`, less the SFHA where the flood files are on
    this server. Returns (GeoJSON, whether the floodplain was cut out).

    The buffer is simplified first and the floodplain cut out of it after, so the cut edge is the flood
    files' own and the outline never reaches into the SFHA (simplifying after the cut moved it up to
    `GEOMETRY_TOLERANCE_M` into it); only the final rounding to `OUTLINE_PRECISION_DEG` remains."""
    import numpy as np
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    plane = LocalPlane.around(centre)
    line = shapely.LineString(plane.xy(centre))
    band = shapely.buffer(line, adjacent_mi * METRES_PER_MILE, quad_segs=8)
    band = shapely.simplify(band, GEOMETRY_TOLERANCE_M, preserve_topology=True)
    cut = False
    if cut_floodplain and sfha:
        band_deg = plane.to_deg(band)
        pieces = np.asarray([p for reader in sfha for p in reader.within(band_deg.bounds)], dtype=object)
        if len(pieces):
            # Only what touches the band, clipped to it: a union of a few small pieces, not of every
            # floodplain polygon whose box meets the band's.
            shapely.prepare(band_deg)
            hits = pieces[shapely.intersects(band_deg, pieces)]
            if len(hits):
                # Clipped to the band grown by a few metres, so no floodplain edge lies on the band's
                # own edge (that left slivers of floodplain in the cut band).
                clip = plane.to_deg(shapely.buffer(band, 5.0))
                flood = shapely.union_all(shapely.intersection(hits, clip))
                band = shapely.difference(band, plane.to_xy(flood))
        cut = True
    out = plane.to_deg(band)
    out = shapely.set_precision(out, OUTLINE_PRECISION_DEG)
    return shapely.geometry.mapping(out), cut


def _round_coords(coords: Any) -> list[list[float]]:
    return [[round(float(x), 5), round(float(y), 5)] for x, y in coords]


def _asset_url(asset: Asset) -> str:
    return f"{WEB_HOST}/assets/{asset.slug}"


def _quartet(source: Source, licence: Licence, source_url: str, retrieved_at: Any) -> dict[str, Any]:
    return provenance_quartet(source, licence, source_url=source_url, retrieved_at=retrieved_at)


@dataclass
class _Lookups:
    lines: dict[_uuid.UUID, Asset]
    substations: dict[_uuid.UUID, Asset]
    plants: dict[_uuid.UUID, Asset]
    owners: dict[_uuid.UUID, list[str]]
    #: Served views (`gated_record`) of the large-load requests the tier may see, located exactly.
    loads: dict[_uuid.UUID, Any]
    #: Served views of the queue requests the tier may see, located exactly, that name a point.
    requests: dict[_uuid.UUID, Any]
    points: dict[_uuid.UUID, InterconnectionPoint]


def _served_exact_location() -> list[ColumnElement[bool]]:
    """A proposal's location as this surface may serve a distance from it: an exact point under a
    licence that allows raw coordinates (`location_exact_permitted`, as nearby-proposals has it).
    A distance from a withheld point would disclose the point."""
    return [Location.precision == "exact", Location.geom.is_not(None), location_exact_permitted()]


def _visible_located(
    db: Session, ids: set[_uuid.UUID], entitlement: str, visible: Sequence[ColumnElement[bool]]
) -> dict[_uuid.UUID, Any]:
    """The proposals of `ids` the tier may see (`visible`) and may be served the exact location of,
    as served views; one whose location's own source the tier may not read is left out too."""
    out: dict[_uuid.UUID, Any] = {}
    for chunk in _chunks(sorted(ids, key=str)):
        rows = db.scalars(
            select(Proposal)
            .join(Location, Location.id == Proposal.location_id)
            .where(Proposal.id.in_(list(chunk)), *visible, *_served_exact_location())
            .options(selectinload(Proposal.sources))
        ).all()
        for p in rows:
            view = gated_record(p, entitlement)  # type: ignore[arg-type]
            if view.location is not None:
                out[p.id] = view
    return out


def _lookups(db: Session, segs: Sequence[SiteCandidate], entitlement: str) -> _Lookups:
    from services.api.assets import asset_owner_visibility_filter

    line_ids = {s.line_asset_id for s in segs}
    sub_ids = {s.substation_asset_id for s in segs if s.substation_asset_id}
    plant_ids = set(_ids(p[0] for s in segs for p in (s.context or {}).get("plants", [])))
    load_ids = set(_ids(p[0] for s in segs for p in (s.context or {}).get("loads", [])))
    request_ids = set(_ids(r[0] for s in segs for r in (s.context or {}).get("requests", []) if r))

    def assets(ids: set[Any], *extra: ColumnElement[bool]) -> dict[_uuid.UUID, Asset]:
        if not ids:
            return {}
        # A band is drawn from its run's vertices, so no asset's own geometry is decoded here.
        rows = db.scalars(
            select(Asset)
            .where(Asset.id.in_(list(ids)), *asset_visibility_filter(entitlement), *extra)
            .options(defer(Asset.geom_line), defer(Asset.geom))
        ).all()
        return {a.id: a for a in rows}

    plants = assets(plant_ids)
    owners: dict[_uuid.UUID, list[str]] = {}
    if plants:
        edges = db.execute(
            select(AssetOwner.asset_id, Organization.name_canonical)
            .join(Organization, Organization.id == AssetOwner.organization_id)
            .where(
                AssetOwner.asset_id.in_(list(plants)),
                AssetOwner.role == "owner",
                *asset_owner_visibility_filter(entitlement),
                *organization_visibility_filter(entitlement),
            )
            .order_by(AssetOwner.share_pct.desc().nulls_last(), Organization.name_canonical)
        ).all()
        for asset_id, name in edges:
            owners.setdefault(asset_id, []).append(name)
    loads = _visible_located(db, load_ids, entitlement, proposal_visibility_filter(entitlement))  # type: ignore[arg-type]
    requests = _visible_located(db, request_ids, entitlement, point_proposal_filter(entitlement))
    point_ids = {v.interconnection_point_id for v in requests.values() if v.interconnection_point_id}
    points: dict[_uuid.UUID, InterconnectionPoint] = {}
    if point_ids:
        rows_p = db.scalars(
            select(InterconnectionPoint).where(
                InterconnectionPoint.id.in_(list(point_ids)),
                *interconnection_point_visibility_filter(entitlement),  # type: ignore[arg-type]
            )
        ).all()
        points = {p.id: p for p in rows_p}
    return _Lookups(
        assets(line_ids, asset_geometry_permitted()), assets(sub_ids), plants, owners, loads, requests, points
    )


def _file_rows(
    provs: Iterable[dict[str, Any]], default_retrieved_at: str | None = None
) -> list[dict[str, Any]]:
    """Licence-summary rows for values read from files (flood, county, fiber, prospecting layers). A file
    that records no retrieval time is credited with the run's (`default_retrieved_at`)."""
    reg = registry()
    rows = []
    for p in provs:
        sid = str(p.get("source_id") or "")
        if not sid:
            continue
        entry = reg.get(sid) if sid in reg else None
        rows.append(
            {
                "source_id": sid,
                "name": entry.name if entry else (p.get("source_name") or sid),
                "operator": entry.operator if entry else None,
                "licence_id": (entry.licence_id if entry else None) or p.get("licence_id") or sid,
                "licence_name": f"{entry.name} terms of use"
                if entry
                else (p.get("licence") or "see provenance"),
                "licence_url": (entry.licence_url or None) if entry else p.get("licence_url"),
                "reuse_class": (entry.reuse if entry else None) or p.get("reuse_class") or "unknown",
                "attribution_text": (entry.credit_text if entry else None)
                or p.get("attribution_text")
                or p.get("attribution"),
                "requires_link_back": bool(entry and entry.reuse in ("attribution", "noncommercial")),
                "retrieved_at": p.get("retrieved_at") or default_retrieved_at,
            }
        )
    return rows


def _file_source_visible(db: Session, source_id: str, entitlement: str) -> bool:
    """A file-backed value is served when the register allows its source and, where the store holds
    the source, its publish state does too."""
    if not publishable(source_id):
        return False
    state = db.scalar(select(Source.publish_state).where(Source.id == source_id))
    return state is None or state in permitted_source_states(entitlement)


def _proposal_provenance(
    view: Any, entitlement: str, licence_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The quartets of a request's links the tier may read, each credited in the licence summary."""
    links = visible_source_links(view.sources, entitlement)
    for link in links:
        licence_rows.append(licence_summary_row(link.source, link.source.licence, link.retrieved_at))
    return [_quartet(link.source, link.source.licence, link.source_url, link.retrieved_at) for link in links]


def _grid_connection(
    segs: Sequence[SiteCandidate], look: _Lookups, entitlement: str, licence_rows: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The nearest grid connection point named by a surviving queue request (one the tier may see, with
    a servable exact location, within the radius of a segment, still naming the point stored with
    it), with the nearest such request's distance and the queued MW and count of the survivors that
    name it. Nothing from any other request: not its distance, not its MW."""
    nearest: dict[_uuid.UUID, float] = {}
    named: dict[_uuid.UUID, _uuid.UUID] = {}
    for s in segs:
        for entry in (s.context or {}).get("requests", []):
            if not entry or len(entry) < 3:
                continue
            req_ids, point_ids = _ids([entry[0]]), _ids([entry[1]])
            if not req_ids or not point_ids:
                continue
            req_id, named_id = req_ids[0], point_ids[0]
            view = look.requests.get(req_id)
            if view is None or view.interconnection_point_id != named_id or named_id not in look.points:
                continue
            miles = float(entry[2])
            if miles > CONTEXT_RADIUS_MI:
                continue
            nearest[req_id] = min(miles, nearest.get(req_id, math.inf))
            named[req_id] = named_id
    if not nearest:
        return None, []
    by_point: dict[_uuid.UUID, list[_uuid.UUID]] = {}
    for req_id, point_id in named.items():
        by_point.setdefault(point_id, []).append(req_id)
    closest = {pid: min(nearest[r] for r in reqs) for pid, reqs in by_point.items()}
    point_id = min(closest, key=lambda pid: (closest[pid], str(pid)))
    point = look.points[point_id]
    totals = PointTotals()
    provenance: list[dict[str, Any]] = []
    for req_id in sorted(by_point[point_id], key=lambda r: (nearest[r], str(r))):
        view = look.requests[req_id]
        mw = view.capacity_mw
        totals.add(
            str(view.lifecycle_state or "unknown"), view.technology, 1, float(mw) if mw is not None else 0.0
        )
        provenance.extend(_proposal_provenance(view, entitlement, licence_rows))
    t = totals.as_dict()
    prov = _quartet(point.source, point.licence, point.source_url, point.retrieved_at)
    licence_rows.append(licence_summary_row(point.source, point.licence, point.retrieved_at))
    return (
        {
            "public_id": point.public_id,
            "name": point.name_display,
            "url": f"{WEB_HOST}/interconnection-points/{point.public_id}",
            "iso": point.operator,
            "voltage_kv": float(point.voltage_kv) if point.voltage_kv is not None else None,
            "nearest_request_mi": round(closest[point_id], 2),
            "queued_mw": t["active_mw"],
            "queued_count": t["active_count"],
            "requests_counted": t["proposal_count"],
            "note": GRID_CONNECTION_NOTE,
            "provenance": prov,
            "request_provenance": provenance,
        },
        [prov, *provenance],
    )


def serialize_band(
    db: Session,
    run: SiteFinderRun,
    c: Criteria,
    segs: Sequence[SiteCandidate],
    look: _Lookups,
    entitlement: str,
    *,
    sfha: Sequence[SfhaPieces],
    gates: dict[str, bool],
    licence_rows: list[dict[str, Any]],
    file_provs: list[dict[str, Any]],
    with_geometry: bool = True,
) -> dict[str, Any]:
    line = look.lines[segs[0].line_asset_id]
    attrs = line.attributes or {}
    kv = segs[0].voltage_kv
    vkey, vlabel = voltage_class(kv)
    centre_line = band_centreline(segs)
    length = round(sum(s.length_mi for s in segs), 3)
    mid = (
        sub_line(centre_line, length_mi(centre_line) / 2, length_mi(centre_line) / 2)[0]
        if len(centre_line)
        else None
    )
    centre = {"lon": round(float(mid[0]), 6), "lat": round(float(mid[1]), 6)} if mid is not None else None
    line_prov = _quartet(line.source, line.licence, line.source_url, line.retrieved_at)
    licence_rows.append(licence_summary_row(line.source, line.licence, line.retrieved_at))
    provenance = [line_prov]

    # Nearest substation over the band, in miles from the line (the band's centreline).
    with_sub = [s for s in segs if s.substation_asset_id in look.substations]
    substation = None
    if with_sub:
        best = min(with_sub, key=lambda s: s.substation_mi or math.inf)
        sub = look.substations[best.substation_asset_id]  # type: ignore[index]
        sattrs = sub.attributes or {}
        sub_prov = _quartet(sub.source, sub.licence, sub.source_url, sub.retrieved_at)
        licence_rows.append(licence_summary_row(sub.source, sub.licence, sub.retrieved_at))
        provenance.append(sub_prov)
        substation = {
            "public_id": sub.public_id,
            "name": sub.name,
            "url": _asset_url(sub),
            "voltage_kv": sattrs.get("voltage_kv"),
            "substation_type": sattrs.get("substation_type"),
            "distance_mi": best.substation_mi,
            "osm_url": sub.source_url,
            "provenance": sub_prov,
        }

    # Flood.
    key = c.key
    covers = [float(getattr(s, f"flood_cover_{key}") or 0.0) for s in segs]
    shares = [getattr(s, f"flood_share_{key}") for s in segs]
    available = all(cv >= FULL_COVER for cv in covers)
    weights = [s.length_mi for s in segs]
    band_share = (
        round(
            sum(float(sh or 0.0) * w for sh, w in zip(shares, weights, strict=True)) / (sum(weights) or 1.0),
            4,
        )
        if available
        else None
    )
    status = "unavailable" if not available else ("partly_in" if (band_share or 0) > 0.0005 else "outside")
    reasons = sorted({s.flood_reason for s in segs if s.flood_reason})
    flood_states = ((run.inputs or {}).get("flood") or {}).get("states") or {}
    state_postal = (segs[0].state_code or "").removeprefix("US-")
    fstate: dict[str, Any] = flood_states.get(state_postal) or next(iter(flood_states.values()), {})
    flood_prov = registry_provenance(
        "us.fema.nfhl",
        source_url=str(fstate.get("source_url") or "https://msc.fema.gov/portal/advanceSearch"),
        retrieved_at=fstate.get("retrieved_at"),
        vintage=fstate.get("product") or fstate.get("version"),
    )
    flood_visible = gates.get("us.fema.nfhl", False)
    reported = sorted({r for s in segs for r in (s.context or {}).get("flood_reported", [])})
    panels = sorted({p for s in segs for p in (s.context or {}).get("firm_panels", [])})[:12]
    flood = {
        "status": status,
        "label": FLOOD_STATUS_LABELS[status],
        "share": band_share if flood_visible else None,
        "cover": round(min(covers), 4) if covers else 0.0,
        "reason": reasons[0] if (reasons and not available) else None,
        "reason_label": FLOOD_REASON_LABELS.get(reasons[0]) if (reasons and not available) else None,
        "reported": reported,
        "firm_panels": panels,
        "not": NOT_A_DETERMINATION,
        "provenance": flood_prov if flood_visible else None,
    }
    if flood_visible and status != "unavailable":
        file_provs.append(flood_prov)
        provenance.append(flood_prov)

    # County and state (the midpoint's county of the band's middle segment).
    middle = segs[len(segs) // 2]
    county_source = (middle.context or {}).get("county_source") or "us.census.tiger_counties"
    counties_input = (run.inputs or {}).get("counties") or {}
    county_prov = registry_provenance(
        county_source,
        source_url=str(counties_input.get("source_url") or "")
        if county_source == counties_input.get("source_id")
        else ("https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_county_20m.zip"),
        retrieved_at=counties_input.get("retrieved_at")
        if county_source == counties_input.get("source_id")
        else "2026-09-18T00:00:00Z",
        vintage=counties_input.get("vintage") if county_source == counties_input.get("source_id") else None,
        note=BOUNDARY_NOTE,
    )
    place_visible = gates.get(county_source, False)
    place = {
        "state_code": middle.state_code,
        "county_fips": middle.county_fips if place_visible else None,
        "county_name": middle.county_name if place_visible else None,
        "counties": sorted({s.county_name for s in segs if s.county_name}) if place_visible else [],
        "note": BOUNDARY_NOTE,
        "provenance": county_prov if place_visible else None,
    }
    if place_visible and middle.county_fips:
        file_provs.append(county_prov)
        provenance.append(county_prov)

    # Context within CONTEXT_RADIUS_MI.
    plant_best: dict[_uuid.UUID, float] = {}
    for s in segs:
        for pid, mi, *_ in (s.context or {}).get("plants", []):
            u = _ids([pid])
            if u and u[0] in look.plants:
                plant_best[u[0]] = min(float(mi), plant_best.get(u[0], math.inf))
    plants: list[dict[str, Any]] = []
    for pid, mi in sorted(plant_best.items(), key=lambda t: t[1]):
        p = look.plants[pid]
        prov = _quartet(p.source, p.licence, p.source_url, p.retrieved_at)
        licence_rows.append(licence_summary_row(p.source, p.licence, p.retrieved_at))
        provenance.append(prov)
        plants.append(
            {
                "public_id": p.public_id,
                "name": p.name,
                "url": _asset_url(p),
                "status": p.status,
                "capacity_mw": float(p.capacity_mw) if p.capacity_mw is not None else None,
                "retirement_year": p.retirement_year,
                "technology": p.technology,
                "owners": look.owners.get(pid, []),
                "distance_mi": round(mi, 2),
                "provenance": prov,
            }
        )
    load_best: dict[_uuid.UUID, float] = {}
    for s in segs:
        for pid, mi in (s.context or {}).get("loads", []):
            u = _ids([pid])
            if u and u[0] in look.loads:
                load_best[u[0]] = min(float(mi), load_best.get(u[0], math.inf))
    loads = []
    for pid, mi in sorted(load_best.items(), key=lambda t: t[1]):
        view = look.loads[pid]
        provs = _proposal_provenance(view, entitlement, licence_rows)
        provenance.extend(provs)
        loads.append(
            {
                "public_id": view.public_id,
                "name": view.name_canonical,
                "url": f"{WEB_HOST}/proposals/{view.slug}",
                "capacity_mw": float(view.capacity_mw) if view.capacity_mw is not None else None,
                "lifecycle_state": view.lifecycle_state,
                "distance_mi": round(mi, 2),
                "provenance": provs,
            }
        )
    grid_point, grid_provs = _grid_connection(segs, look, entitlement, licence_rows)
    provenance.extend(grid_provs)
    fiber_raw = (middle.context or {}).get("fiber")
    fiber: dict[str, Any] = {
        "status": "unavailable",
        "reason": "FCC fiber availability is not loaded on this server",
    }
    if fiber_raw and gates.get("us.fcc.bdc.fixed_summary", False):
        fprov = registry_provenance(
            "us.fcc.bdc.fixed_summary",
            source_url=str(fiber_raw.get("source_url") or ""),
            retrieved_at=fiber_raw.get("retrieved_at"),
            vintage=fiber_raw.get("vintage"),
        )
        file_provs.append(fprov)
        provenance.append(fprov)
        fiber = {
            "status": "ok",
            "level": "county",
            "fiber_share": fiber_raw.get("share"),
            "fiber_gigabit_share": fiber_raw.get("gigabit"),
            "units": fiber_raw.get("units"),
            "fcc_area_url": fiber_raw.get("url"),
            "note": (
                "Availability to homes and businesses in the county, reported by providers; "
                "not fiber routes or capacity."
            ),
            "provenance": fprov,
        }

    # Prospecting layers, withheld where their source is.
    pros = prospect_mod.band_prospect([(s.prospect or {}, s.length_mi) for s in segs], dict(run.layers or {}))
    for entry in pros["layers"]:
        prov = entry.get("provenance") or {}
        sid = str(prov.get("source_id") or "")
        if sid and not gates.get(sid, False):
            entry.update(status="withheld", provenance=None)
            for k in (
                "summary",
                "share",
                "hard_constraint_share",
                "qualifying_share",
                "coverage_share",
                "features",
            ):
                entry.pop(k, None)
        elif sid and entry.get("status") not in ("withheld", "unavailable"):
            file_provs.append({**prov, "source_id": sid})

    first, last = segs[0].segment_index, segs[-1].segment_index
    poly_key = (
        str(segs[0].run_id),
        str(segs[0].line_asset_id),
        first,
        last,
        c.adjacent_mi,
        c.exclude_flood and bool(sfha),
    )
    cached = _POLYGONS.get(poly_key) if with_geometry else (None, False)
    if cached is None:
        cached = (
            band_polygon(centre_line, c.adjacent_mi, sfha, c.exclude_flood)
            if len(centre_line)
            else (None, False)
        )
        _POLYGONS.put(poly_key, cached)
    geometry, cut = cached
    links = {
        "fema_flood_map": f"https://msc.fema.gov/portal/search?AddressQuery={centre['lat']}%2C{centre['lon']}"
        if centre
        else None,
        "google_maps": f"https://www.google.com/maps/search/?api=1&query={centre['lat']}%2C{centre['lon']}"
        if centre
        else None,
        "osm_way": line.source_url,
        "osm_substation": substation["osm_url"] if substation else None,
        "county": f"https://data.census.gov/profile?g=050XX00US{place['county_fips']}"
        if place.get("county_fips")
        else None,
    }
    return {
        "id": f"{line.public_id}.{first}-{last}",
        "line": {
            "public_id": line.public_id,
            "name": line.name,
            "url": _asset_url(line),
            "voltage_kv": kv,
            "voltages_kv": attrs.get("voltages_kv") or [],
            "voltage_unknown": bool(segs[0].voltage_unknown),
            "voltage_class": vkey,
            "voltage_label": vlabel,
            "operator": attrs.get("operator"),
            "underground": bool(segs[0].underground),
            "osm_url": line.source_url,
            "provenance": line_prov,
        },
        "segments": {"first": first, "last": last, "count": len(segs)},
        "length_mi": length,
        "adjacent_mi": c.adjacent_mi,
        "centre": centre,
        "bbox": [
            round(min(s.min_lon for s in segs), 6),
            round(min(s.min_lat for s in segs), 6),
            round(max(s.max_lon for s in segs), 6),
            round(max(s.max_lat for s in segs), 6),
        ],
        "centreline": {"type": "LineString", "coordinates": _round_coords(centre_line)},
        "geometry": geometry,
        "geometry_note": (
            f"Within {c.adjacent_mi:g} mi of the line"
            + (", floodplain cut out" if cut else "")
            + (
                " (outline left out: ask for the band by its id for it)"
                if not with_geometry
                else ""
                if cut or not c.exclude_flood
                else " (floodplain not cut out: the flood files are not on this server)"
            )
        ),
        "substation": substation,
        "flood": flood,
        "place": place,
        "context": {
            "radius_mi": CONTEXT_RADIUS_MI,
            "retiring_plants": plants,
            # The same figure the list sorts on (`_Plants.band_mw`): the distinct visible plants.
            "retiring_mw": round(sum(p["capacity_mw"] or 0 for p in plants), 1) if plants else None,
            "data_centres": loads,
            "grid_connection": grid_point,
            "fiber": fiber,
        },
        "prospect": pros,
        "links": links,
        "provenance": provenance,
    }


def _gates(db: Session, run: SiteFinderRun, entitlement: str) -> dict[str, bool]:
    ids = {
        "us.fema.nfhl",
        "us.census.tiger_counties",
        "us.census.cartographic_boundaries",
        "us.fcc.bdc.fixed_summary",
    }
    for meta in (run.layers or {}).values():
        prov = (meta or {}).get("provenance") or {}
        if prov.get("source_id"):
            ids.add(str(prov["source_id"]))
    return {sid: _file_source_visible(db, sid, entitlement) for sid in ids}


def _run_meta(run: SiteFinderRun | None) -> dict[str, Any]:
    if run is None:
        return {"computed": False}
    flood = (run.inputs or {}).get("flood") or {}
    return {
        "computed": True,
        "run_id": str(run.id),
        "computed_at": iso(run.finished_at),
        "segments": run.segments,
        "flood_version": flood.get("version"),
        "flood_states": sorted(flood.get("states") or {}),
        "osm": (run.inputs or {}).get("osm"),
    }


def _criteria_meta(c: Criteria, field: str, descending: bool) -> dict[str, Any]:
    return {
        **c.as_dict(),
        "sort": ("-" if descending else "") + field,
        "rules": {
            "segment_mi": 0.5,
            "max_flood_share": MAX_FLOOD_SHARE,
            "flood_cover_required": FULL_COVER,
            "mostly_protected": MOSTLY_PROTECTED,
            "context_radius_mi": CONTEXT_RADIUS_MI,
            "adjacent_mi_choices": sorted(ADJACENT_MI),
            "substation_step_mi": SUBSTATION_STEP_MI,
        },
    }


def _band_clauses(db: Session, request: Request, raw: str, entitlement: str) -> list[ColumnElement[bool]]:
    """`band=<line public id>.<first>-<last>`: the segments of that range of that line, so the list
    answers with that band (or what of it still passes the criteria). An unknown or hidden line
    matches nothing; a malformed id is a 400."""
    m = BAND_ID.match(raw)
    if m is None or int(m["first"]) > int(m["last"]):
        raise validation_error("band", "band must be a band id such as asset_01ABC.4-9", request.url.path)
    line_id = db.scalar(
        select(Asset.id).where(Asset.public_id == m["line"], *asset_visibility_filter(entitlement))
    )
    if line_id is None:
        return [sa.false()]
    return [
        SiteCandidate.line_asset_id == line_id,
        SiteCandidate.segment_index.between(int(m["first"]), int(m["last"])),
    ]


#: The candidate columns a band is served from (`serialize_band`): the quartet and the timestamps are
#: the line's and the run's, read from those rows, so they are not decoded for every segment.
_SEGMENT_COLUMNS = (
    SiteCandidate.run_id,
    SiteCandidate.line_asset_id,
    SiteCandidate.segment_index,
    SiteCandidate.length_mi,
    SiteCandidate.min_lon,
    SiteCandidate.min_lat,
    SiteCandidate.max_lon,
    SiteCandidate.max_lat,
    SiteCandidate.voltage_kv,
    SiteCandidate.voltage_unknown,
    SiteCandidate.underground,
    SiteCandidate.state_code,
    SiteCandidate.county_fips,
    SiteCandidate.county_name,
    SiteCandidate.substation_asset_id,
    SiteCandidate.substation_mi,
    *(
        getattr(SiteCandidate, f"flood_{kind}_{key}")
        for kind in ("share", "cover")
        for key in SITE_FINDER_DISTANCES
    ),
    SiteCandidate.flood_reason,
    SiteCandidate.coords,
    SiteCandidate.context,
    SiteCandidate.prospect,
)


def _cursor_key(request: Request) -> tuple[float, str, int] | None:
    raw = request.query_params.get("cursor") or None
    if not raw:
        return None
    cur = decode_cursor(raw, request.url.path)
    try:
        value = float(cur.sort_value)
        line_text, first_text = cur.tiebreaker_id.rsplit(":", 1)
        key = (value, str(_uuid.UUID(line_text)), int(first_text))
    except (TypeError, ValueError, AttributeError):
        raise invalid_cursor(request.url.path) from None
    if not math.isfinite(key[0]):
        raise invalid_cursor(request.url.path)
    return key


@router.get("/v1/site-finder")
def list_site_finder(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, LIST_PARAMS)
    c = parse_criteria(request)
    field, descending = parse_sort(request)
    limit = page_limit(request, default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
    with_geometry = _flag(request, "geometry", True)
    after = _cursor_key(request)
    entitlement = ctx.entitlement
    run = current_run(db)
    finder: dict[str, Any] = {
        "criteria": _criteria_meta(c, field, descending),
        "run": _run_meta(run),
        "screening": SCREENING_NOTE,
        "flood_note": NOT_A_DETERMINATION,
        "voltage_classes": [{"key": k, "label": label} for k, label, _lo, _hi in VOLTAGE_CLASSES]
        + [{"key": "unknown", "label": "voltage not stated"}],
    }
    meta_extra = {"site_finder": finder}
    if run is None:
        finder["summary"] = {
            "bands": 0,
            "miles": 0.0,
            "facets": {"states": [], "counties": [], "counties_need_state": False},
        }
        return build_list_envelope(
            [],
            meta=build_meta(lag_days=0, tier=entitlement, extra=meta_extra),
            licence_summary=build_licence_summary([]),
            page=build_page(None, None, False),
        )
    band_raw = (request.query_params.get("band") or "").strip()
    if band_raw:
        clauses = segment_filters(c, run.id, entitlement) + _band_clauses(db, request, band_raw, entitlement)
        candidates = [_band_row(r) for r in db.execute(select(bands_subquery(c, clauses))).all()]
        if field == "retiring_mw":
            candidates = _with_retiring(db, candidates, run.id, entitlement)
        ordered = _order(candidates, field, descending)
    else:
        ordered = ordered_bands(db, c, run.id, entitlement, field, descending)
    page_rows, has_more = _page(ordered, c, after, limit)
    rows = [b for _, b in page_rows]
    finder["summary"] = summary_and_facets(db, c, run.id, entitlement)
    data: list[dict[str, Any]] = []
    licence_rows: list[dict[str, Any]] = []
    file_provs: list[dict[str, Any]] = []
    if rows:
        # The run in every term, so each is one range of the (run, line, segment) unique index: with
        # the run outside the OR, SQLite scanned all the run's segments (0.4 s on Texas).
        conds = [
            and_(
                SiteCandidate.run_id == run.id,
                SiteCandidate.line_asset_id == r.line_id,
                SiteCandidate.segment_index.between(r.first, r.last),
            )
            for r in rows
        ]
        segs = db.scalars(
            select(SiteCandidate)
            .where(or_(*conds))
            .options(load_only(*_SEGMENT_COLUMNS))
            .order_by(SiteCandidate.line_asset_id, SiteCandidate.segment_index)
        ).all()
        by_band: dict[tuple[Any, int], list[SiteCandidate]] = {}
        on_line: dict[Any, list[BandRow]] = {}
        for r in rows:
            on_line.setdefault(r.line_id, []).append(r)
        for s in segs:
            for r in on_line.get(s.line_asset_id, ()):
                if r.first <= s.segment_index <= r.last:
                    by_band.setdefault((r.line_id, r.first), []).append(s)
                    break
        look = _lookups(db, segs, entitlement)
        gates = _gates(db, run, entitlement)
        sfha = _sfha_readers(run) if (c.exclude_flood and with_geometry) else []
        for r in rows:
            band_segs = by_band.get((r.line_id, r.first)) or []
            if not band_segs or band_segs[0].line_asset_id not in look.lines:
                continue
            data.append(
                serialize_band(
                    db,
                    run,
                    c,
                    band_segs,
                    look,
                    entitlement,
                    sfha=sfha,
                    gates=gates,
                    licence_rows=licence_rows,
                    file_provs=file_provs,
                    with_geometry=with_geometry,
                )
            )
    next_cursor = None
    if has_more and page_rows:
        (value, line_text, first), _last = page_rows[-1]
        next_cursor = encode_cursor(float(value), f"{line_text}:{first}")
    summary_rows = licence_rows + _file_rows(_dedupe(file_provs), iso(run.finished_at))
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=entitlement, extra=meta_extra),
        licence_summary=build_licence_summary(summary_rows),
        page=build_page(next_cursor, None, has_more),
    )


def _dedupe(provs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for p in provs:
        sid = str(p.get("source_id") or "")
        if sid and sid not in seen:
            seen[sid] = p
    return list(seen.values())


def _parse_bbox(request: Request) -> tuple[float, float, float, float] | None:
    raw = request.query_params.get("bbox")
    if not raw:
        return None
    try:
        parts = [float(v) for v in raw.split(",")]
    except ValueError:
        raise validation_error("bbox", "bbox must be minLon,minLat,maxLon,maxLat", request.url.path) from None
    if len(parts) != 4 or not (-180 <= parts[0] < parts[2] <= 180 and -90 <= parts[1] < parts[3] <= 90):
        raise validation_error(
            "bbox", "bbox must be minLon,minLat,maxLon,maxLat within range", request.url.path
        )
    return parts[0], parts[1], parts[2], parts[3]


def _parse_zoom(request: Request) -> int:
    raw = request.query_params.get("zoom") or "8"
    try:
        value = float(raw)
    except ValueError:
        raise validation_error("zoom", "zoom must be a number", request.url.path) from None
    if not math.isfinite(value):
        raise validation_error("zoom", "zoom must be a finite number from 0 to 22", request.url.path)
    return max(0, min(22, int(value)))


@dataclass(frozen=True)
class _MapLine:
    """A line the map draws bands on: its name and quartet keys, and its segments' vertices as the run
    stored them (index -> vertices), so a band is placed by the run that measured it."""

    public_id: str
    name: str | None
    source_id: str
    licence_id: str
    retrieved_at: Any
    segments: dict[int, Any]


#: Lines for the map, per (run, line): the run's segments do not change.
_LINES: OrderedDict[tuple[str, _uuid.UUID], _MapLine] = OrderedDict()
_LINES_MAX = 8000
_LINES_LOCK = threading.Lock()


def _map_lines(
    db: Session, run_id: _uuid.UUID, ids: set[_uuid.UUID], entitlement: str
) -> dict[_uuid.UUID, _MapLine]:
    """The lines the map draws bands on, visible at this tier, with the run's segment vertices."""
    import numpy as np

    out: dict[_uuid.UUID, _MapLine] = {}
    missing: list[_uuid.UUID] = []
    with _LINES_LOCK:
        for line_id in ids:
            hit = _LINES.get((str(run_id), line_id))
            if hit is None:
                missing.append(line_id)
            else:
                _LINES.move_to_end((str(run_id), line_id))
                out[line_id] = hit
    for part in _chunks(sorted(missing, key=str)):
        segments: dict[_uuid.UUID, dict[int, Any]] = {}
        for line_id, index, coords in db.execute(
            select(SiteCandidate.line_asset_id, SiteCandidate.segment_index, SiteCandidate.coords).where(
                SiteCandidate.run_id == run_id, SiteCandidate.line_asset_id.in_(list(part))
            )
        ):
            if coords:
                segments.setdefault(line_id, {})[int(index)] = np.asarray(coords, dtype=float)
        for row in db.execute(
            select(
                Asset.id, Asset.public_id, Asset.name, Asset.source_id, Asset.licence_id, Asset.retrieved_at
            ).where(Asset.id.in_(list(part)))
        ).all():
            line = _MapLine(
                row.public_id,
                row.name,
                row.source_id,
                row.licence_id,
                row.retrieved_at,
                segments.get(row.id, {}),
            )
            out[row.id] = line
            with _LINES_LOCK:
                _LINES[(str(run_id), row.id)] = line
                while len(_LINES) > _LINES_MAX:
                    _LINES.popitem(last=False)
    # The cache holds geometry; whether a line may be shown is asked of the store every time.
    if out:
        visible: set[_uuid.UUID] = set()
        for chunk in _chunks(sorted(out, key=str)):
            visible.update(
                db.scalars(
                    select(Asset.id).where(
                        Asset.id.in_(list(chunk)),
                        *asset_visibility_filter(entitlement),
                        asset_geometry_permitted(),
                    )
                )
            )
        out = {k: v for k, v in out.items() if k in visible}
    return out


@router.get("/v1/site-finder/geo")
def site_finder_geo(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    """The bands in view as GeoJSON centrelines (longest first, at most `GEO_CAP`), for the map; the
    list endpoint carries each band's polygon and facts."""
    check_allowed(request, GEO_PARAMS)
    c = parse_criteria(request)
    bbox = _parse_bbox(request)
    zoom = _parse_zoom(request)
    entitlement = ctx.entitlement
    run = current_run(db)
    features: list[dict[str, Any]] = []
    licence_rows: list[dict[str, Any]] = []
    total = 0
    if run is not None:
        # Longest first from the cached order: one pass counts the bands in view and keeps the first
        # `GEO_CAP` of them, with no sort per request.
        ordered = ordered_bands(db, c, run.id, entitlement, "length_mi", True)
        rows: list[BandRow] = []
        for b in ordered.rows:
            if not c.voltage_ok(b.voltage_kv, b.unknown):
                continue
            if bbox is not None:
                x0, y0, x1, y1 = bbox
                if not (b.max_lon >= x0 and b.min_lon <= x1 and b.max_lat >= y0 and b.min_lat <= y1):
                    continue
            total += 1
            if len(rows) < GEO_CAP:
                rows.append(b)
        lines = _map_lines(db, run.id, {b.line_id for b in rows}, entitlement)
        tolerance = tolerance_for_zoom(zoom)
        seen_sources: set[tuple[str, str]] = set()
        for r in rows:
            line = lines.get(r.line_id)
            if line is None:
                continue
            centre = join_segments(
                [line.segments[i] for i in range(r.first, r.last + 1) if i in line.segments]
            )
            if len(centre) < 2:
                continue
            simplified = simplify_parts((tuple((float(x), float(y)) for x, y in centre),), tolerance)
            kv = r.voltage_kv
            vkey, vlabel = voltage_class(kv)
            cover_ok = (r.cover or 0) >= FULL_COVER
            status = (
                "unavailable" if not cover_ok else ("partly_in" if (r.share or 0) > 0.0005 else "outside")
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": _round_coords(simplified[0])},
                    "properties": {
                        "id": f"{line.public_id}.{r.first}-{r.last}",
                        "name": line.name,
                        "voltage_kv": kv,
                        "voltage_class": vkey,
                        "voltage_label": vlabel,
                        "length_mi": round(float(r.length_mi), 2),
                        "substation_mi": r.substation_mi,
                        "flood_status": status,
                    },
                }
            )
            seen_sources.add((line.source_id, line.licence_id))
        if seen_sources:
            src_by_id = {
                x.id: x for x in db.scalars(select(Source).where(Source.id.in_({a for a, _ in seen_sources})))
            }
            lic_by_id = {
                x.id: x
                for x in db.scalars(select(Licence).where(Licence.id.in_({b for _, b in seen_sources})))
            }
            latest = {
                (sid, lid): max(
                    ln.retrieved_at for ln in lines.values() if (ln.source_id, ln.licence_id) == (sid, lid)
                )
                for sid, lid in seen_sources
            }
            for (sid, lid), retrieved in sorted(latest.items()):
                licence_rows.append(licence_summary_row(src_by_id[sid], lic_by_id[lid], retrieved))
    data = {"type": "FeatureCollection", "features": features}
    meta = build_meta(
        lag_days=0,
        tier=entitlement,
        extra={
            "site_finder": {
                "criteria": c.as_dict(),
                "run": _run_meta(run),
                "bands_in_view": total,
                "bands_drawn": len(features),
                "cap": GEO_CAP,
            }
        },
    )
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(licence_rows))
