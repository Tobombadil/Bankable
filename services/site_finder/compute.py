"""Compute the site finder's candidate segments (owner decision 2026-10-22; table `site_candidate`).

For every OpenStreetMap line (`us.osm.power`) whose highest stated voltage is 69 kV or more, and every
one that states no voltage (flagged `voltage_unknown`), the stored line is cut into segments of about
half a mile (`geometry.split_line`). For each segment:

- **Nearest substation** (OpenStreetMap `substation` assets) within `SUBSTATION_SEARCH_MI`, measured
  from the segment itself, in miles.
- **Floodplain**: the share of its band inside the FEMA Special Flood Hazard Area, and whether the
  band has flood data, at 0.25, 0.5, 1 and 2 miles (`flood.MeasuredLayer`).
- **County and state**: the Census TIGER/Line county of its midpoint (`counties.CountyIndex`; the
  vendored 1:20m counties where no TIGER county contains it, labelled as such).
- **Context within `CONTEXT_RADIUS_MI`**: retiring and retired power plants (asset ids, distance, MW),
  data-centre and other large-load requests with an exact location (proposal ids and distances
  only), the queue requests with an exact location that name a grid connection point (proposal id,
  point id and distance, one entry per request), and the county's fiber availability (FCC BDC) where
  that file is present.
- **Its vertices** (`coords`), so the API draws a band from the geometry this run measured.
- **Prospecting layers** over its 1-mile band (`prospect`, `services.context_sample.sample_line`),
  with PAD-US's protected and hard-constraint shares stored as columns for the protected-land filter.

Nothing aggregated over proposals is stored (docs/21 §8 item 4), and nothing derived from a proposal
apart from its own id and its distance: requests are stored whatever their visibility, and the API
drops every one the caller's tier may not see, or whose exact location it may not serve, before it
takes a nearest distance or sums a queued MW (`services/api/site_finder.py`). A stored distance is
therefore never served for a hidden request, nor attributed to a connection point it does not name.

Lines are measured grouped by the flood states they need (`flood_key`), so each set of states'
flood files is read once per run, and each state's once while its neighbours are measured
(`flood.FloodData`).

A run writes its rows under a new `site_finder_run` (status `running`), in committed batches, then
marks it `ok` and deletes every older run's rows in one transaction; the API serves the newest `ok`
run only, so a refresh never shows half a store, and a failed run deletes its own rows and leaves
the previous one serving.

    python -m services.site_finder [--states TX] [--data-root DIR] [--no-prospect]

runs it against `DATABASE_URL`; the scheduler runs it after each context load (`site_finder_refresh`).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import pathlib
import time
import uuid as _uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import (
    SITE_FINDER_DISTANCES,
    Asset,
    Location,
    Proposal,
    SiteCandidate,
    SiteFinderRun,
)
from services.site_finder import prospect as prospect_mod
from services.site_finder.counties import CountyIndex
from services.site_finder.flood import FloodData, FloodSegment, unavailable
from services.site_finder.geometry import (
    METRES_PER_MILE,
    LocalPlane,
    PointIndex,
    Segment,
    points_to_polyline_m,
    split_line,
)

logger = logging.getLogger(__name__)

#: The network the finder is defined on (owner decision 2026-10-22: the LBNL layer covers about a
#: fifth of US transmission mileage, so the network comes from OpenStreetMap).
LINE_SOURCES = ("us.osm.power",)
MIN_KV = 69.0
SEGMENT_MI = 0.5
#: The largest substation distance the finder's slider offers (1-10 miles).
SUBSTATION_SEARCH_MI = 10.0
CONTEXT_RADIUS_MI = 5.0
#: Segments measured together in one plane (about five miles): one flood clip serves them all.
CHUNK_SEGMENTS = 10
INSERT_BATCH = 1000
#: How many of each kind of nearby item a segment keeps, nearest first. Generous, so a band's figures
#: (the API unions its segments' lists) rarely miss one: Texas's busiest segment has 4 requests.
MAX_PLANTS, MAX_LOADS, MAX_REQUESTS = 25, 10, 50
#: Decimal places of a stored vertex (about 0.1 m).
COORD_DECIMALS = 6
RETIRING_STATUSES = ("retiring", "retired")
FIBER_FILE = pathlib.Path("normalized") / "context" / "us.fcc.bdc.fixed_summary.parquet"
OSM_MANIFEST = pathlib.Path("normalized") / "context" / "us.osm.power.manifest.json"
#: Runs whose record is kept (rows are kept only for the newest `ok` one).
KEEP_RUN_RECORDS = 10


@dataclass
class LineInput:
    id: _uuid.UUID
    public_id: str
    attributes: dict[str, Any]
    parts: list[np.ndarray[Any, np.dtype[np.float64]]]
    source_id: str
    source_url: str
    retrieved_at: dt.datetime
    licence_id: str
    state_code: str | None

    @property
    def voltage_kv(self) -> float | None:
        kv = self.attributes.get("voltage_kv")
        return (
            float(kv)
            if isinstance(kv, (int, float)) and not isinstance(kv, bool) and math.isfinite(kv)
            else None
        )

    @property
    def voltage_unknown(self) -> bool:
        return self.voltage_kv is None

    @property
    def scope(self) -> set[str]:
        regions = set(self.attributes.get("scope_regions") or []) | set(
            self.attributes.get("states_crossed") or []
        )
        if self.state_code:
            regions.add(self.state_code)
        return {str(r) for r in regions}


@dataclass
class Points:
    """Points of one kind with their ids and an optional value (MW), indexed for "near this box"."""

    ids: list[str]
    lons: list[float]
    lats: list[float]
    values: list[float | None] = field(default_factory=list)
    #: A second id per point where the kind has one (a request's grid connection point).
    refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.index = PointIndex(self.lons, self.lats)


@dataclass
class Inputs:
    lines: list[LineInput]
    substations: Points
    plants: Points
    loads: Points
    requests: Points
    counties: CountyIndex
    fallback_counties: CountyIndex | None
    flood: FloodData | None
    sampler: Any
    fiber: dict[str, dict[str, Any]]
    summary: dict[str, Any]


# ---------------------------------------------------------------------------------------- inputs
def _parts(wkt: Any) -> list[np.ndarray[Any, np.dtype[np.float64]]]:
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    if not wkt:
        return []
    try:
        geom = shapely.from_wkt(wkt) if isinstance(wkt, str) else shapely.geometry.shape(wkt)
    except Exception:
        return []
    return [
        np.asarray(part.coords, dtype=np.float64)[:, :2]
        for part in shapely.get_parts(geom)
        if not part.is_empty
    ]


def load_lines(session: Session, states: Iterable[str] | None = None) -> list[LineInput]:
    wanted = {f"US-{s.upper().removeprefix('US-')}" for s in states} if states else None
    stmt = select(
        Asset.id,
        Asset.public_id,
        Asset.attributes,
        Asset.geom_line,
        Asset.source_id,
        Asset.source_url,
        Asset.retrieved_at,
        Asset.licence_id,
        Asset.state_code,
    ).where(
        Asset.asset_type == "transmission_line",
        Asset.source_id.in_(LINE_SOURCES),
        Asset.geom_line.is_not(None),
    )
    out: list[LineInput] = []
    for row in session.execute(stmt):
        line = LineInput(
            id=row.id,
            public_id=row.public_id,
            attributes=dict(row.attributes or {}),
            parts=_parts(row.geom_line),
            source_id=row.source_id,
            source_url=row.source_url,
            retrieved_at=row.retrieved_at,
            licence_id=row.licence_id,
            state_code=row.state_code,
        )
        kv = line.voltage_kv
        if kv is not None and kv < MIN_KV:
            continue
        if wanted is not None and not (line.scope & wanted):
            continue
        if line.parts:
            out.append(line)
    out.sort(key=lambda ln: (ln.state_code or "", ln.public_id))
    return out


def _point(geom: Any) -> tuple[float, float] | None:
    if isinstance(geom, (list, tuple)) and len(geom) >= 2:
        return float(geom[0]), float(geom[1])
    return None


def load_points(session: Session) -> tuple[Points, Points, Points, Points]:
    """Substations, retiring and retired plants, exactly-located large loads, and the exactly-located
    requests that name a grid connection point (one point per request, keyed by the request).

    Requests and loads are read whatever their visibility: the run serves every tier, so the API
    applies each caller's (and the exact-location rule) per request when it reads them back."""
    from services.api.visibility import asset_visibility_filter

    subs = Points([], [], [])
    for aid, geom in session.execute(
        select(Asset.id, Asset.geom).where(Asset.asset_type == "substation", Asset.geom.is_not(None))
    ):
        p = _point(geom)
        if p:
            subs.ids.append(str(aid))
            subs.lons.append(p[0])
            subs.lats.append(p[1])
    # The MW stored for sorting counts only plants the public may see (their licence and source).
    plants = Points([], [], [], [])
    for aid, geom, mw in session.execute(
        select(Asset.id, Asset.geom, Asset.capacity_mw).where(
            Asset.asset_type == "power_plant",
            Asset.status.in_(RETIRING_STATUSES),
            Asset.geom.is_not(None),
            *asset_visibility_filter("public"),
        )
    ):
        p = _point(geom)
        if p:
            plants.ids.append(str(aid))
            plants.lons.append(p[0])
            plants.lats.append(p[1])
            plants.values.append(float(mw) if mw is not None else None)
    exact = (Location.precision == "exact", Location.geom.is_not(None), Proposal.merged_into_id.is_(None))
    loads = Points([], [], [])
    for pid, geom in session.execute(
        select(Proposal.id, Location.geom)
        .join(Location, Proposal.location_id == Location.id)
        .where(Proposal.kind == "load", *exact)
    ):
        p = _point(geom)
        if p:
            loads.ids.append(str(pid))
            loads.lons.append(p[0])
            loads.lats.append(p[1])
    requests = Points([], [], [])
    for pid, point_id, geom in session.execute(
        select(Proposal.id, Proposal.interconnection_point_id, Location.geom)
        .join(Location, Proposal.location_id == Location.id)
        .where(Proposal.interconnection_point_id.is_not(None), *exact)
    ):
        p = _point(geom)
        if p:
            requests.ids.append(str(pid))
            requests.refs.append(str(point_id))
            requests.lons.append(p[0])
            requests.lats.append(p[1])
    for pts in (subs, plants, loads, requests):
        pts.index = PointIndex(pts.lons, pts.lats)
    return subs, plants, loads, requests


def load_fiber(data_root: pathlib.Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    path = data_root / FIBER_FILE
    if not path.exists():
        return {}, None
    from services.ingest.fiber_availability import read_county_fiber

    try:
        batch = read_county_fiber(path)
    except Exception as exc:
        logger.warning("site finder: fiber file not used: %s", exc)
        return {}, {"source_id": "us.fcc.bdc.fixed_summary", "used": False, "reason": str(exc)[:300]}
    by_fips = {
        fips: {
            "share": r.get("fiber_share"),
            "gigabit": r.get("fiber_gigabit_share"),
            "units": r.get("bsl_units"),
            "vintage": r.get("vintage"),
            "as_of": r.get("as_of_date"),
            "url": r.get("fcc_area_url"),
            "source_url": r.get("source_url"),
            "retrieved_at": r.get("retrieved_at"),
        }
        for fips, r in batch.by_fips.items()
    }
    return by_fips, {
        "source_id": batch.source_id,
        "used": True,
        "vintage": batch.vintage.value,
        "counties": len(by_fips),
    }


def gather_inputs(
    session: Session,
    data_root: pathlib.Path,
    *,
    states: Iterable[str] | None = None,
    sampler: Any = None,
    use_prospect: bool = True,
) -> Inputs:
    lines = load_lines(session, states)
    subs, plants, loads, requests = load_points(session)
    counties = CountyIndex.load(data_root)
    fallback = (
        CountyIndex.from_vendored()
        if counties.provenance["source_id"] != "us.census.cartographic_boundaries"
        else None
    )
    flood = FloodData.load(data_root)
    if use_prospect and sampler is None:
        from services.context_sample import default_sampler

        sampler = default_sampler()
    if not use_prospect:
        sampler = None
    fiber, fiber_summary = load_fiber(data_root)
    osm_manifest_path = data_root / OSM_MANIFEST
    osm: dict[str, Any] = {"source_id": "us.osm.power"}
    if osm_manifest_path.exists():
        m = json.loads(osm_manifest_path.read_text(encoding="utf-8"))
        osm.update(
            {
                k: m.get(k)
                for k in ("version", "data_timestamp", "built_at", "regions_loaded", "licence", "attribution")
            }
        )
    summary = {
        "osm": osm,
        "flood": flood.summary() if flood else {"source_id": "us.fema.nfhl", "version": None, "states": {}},
        "counties": {**counties.provenance, "counties": len(counties)},
        "prospect": {
            "built": sorted(sampler.available()) if sampler is not None else [],
            "directory": str(getattr(sampler, "directory", "")) if sampler is not None else None,
        },
        "fiber": fiber_summary,
        "states": sorted(states) if states else None,
    }
    return Inputs(lines, subs, plants, loads, requests, counties, fallback, flood, sampler, fiber, summary)


# ------------------------------------------------------------------------------------- measuring
def _near(
    points: Points, bbox: tuple[float, float, float, float], radius_m: float, plane: LocalPlane, seg_xy: Any
) -> list[tuple[int, float]]:
    """(index, metres) of `points` within `radius_m` of the segment, nearest first."""
    idx = points.index.near(bbox, radius_m)
    if len(idx) == 0:
        return []
    px, py = plane.forward(points.index.lons[idx], points.index.lats[idx])
    d = points_to_polyline_m(np.asarray(px), np.asarray(py), seg_xy)
    keep = d <= radius_m
    pairs = sorted(zip(idx[keep].tolist(), d[keep].tolist(), strict=True), key=lambda t: (t[1], t[0]))
    return [(int(i), float(m)) for i, m in pairs]


def _mi(metres: float) -> float:
    return round(metres / METRES_PER_MILE, 3)


def _prospect(sampler: Any, seg: Segment) -> dict[str, Any]:
    if sampler is None:
        return {}
    from services.context_sample.catalog import SITE_FINDER_LAYERS

    try:
        answer = sampler.sample_line(
            {"type": "LineString", "coordinates": seg.coords.tolist()},
            prospect_mod.BUFFER_MI,
            list(SITE_FINDER_LAYERS),
            spacing_m=prospect_mod.SPACING_M,
        )
    except Exception as exc:
        return {"error": str(exc)[:200]}
    return prospect_mod.compact(answer)


def flood_key(inputs: Inputs, line: LineInput) -> tuple[str, ...]:
    """The flood build's states a line's bands may reach (its own, those it crosses, its extract's
    scope), as the flood layer is keyed: lines with one key share one layer."""
    if inputs.flood is None:
        return ()
    return tuple(sorted({s.removeprefix("US-") for s in line.scope} & inputs.flood.states))


def measuring_order(inputs: Inputs) -> list[LineInput]:
    """The lines in the order they are measured: by home state, then by the set of flood states they
    need, so each set's layer is built once and a state's files stay read while its border lines are
    measured (a national run otherwise rebuilt a layer at every border-crossing line)."""
    return sorted(inputs.lines, key=lambda ln: (ln.state_code or "", flood_key(inputs, ln), ln.public_id))


def _flood_layer(inputs: Inputs, line: LineInput) -> Any:
    if inputs.flood is None:
        return None
    return inputs.flood.layer_for(flood_key(inputs, line))


def _coords(coords: Any) -> list[list[float]]:
    return [[round(float(x), COORD_DECIMALS), round(float(y), COORD_DECIMALS)] for x, y in coords]


def line_rows(inputs: Inputs, line: LineInput, run_id: _uuid.UUID, now: dt.datetime) -> list[dict[str, Any]]:
    """The candidate rows of one line."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    rows: list[dict[str, Any]] = []
    layer = _flood_layer(inputs, line)
    offset = 0
    for part_index, part in enumerate(line.parts):
        segments = split_line(part, SEGMENT_MI)
        for start in range(0, len(segments), CHUNK_SEGMENTS):
            chunk = segments[start : start + CHUNK_SEGMENTS]
            plane = LocalPlane.around(np.vstack([s.coords for s in chunk]))
            xy = [plane.xy(s.coords) for s in chunk]
            flood: list[FloodSegment] = (
                layer.measure(plane, [shapely.LineString(c) for c in xy])
                if layer is not None
                else unavailable("not_computed", len(chunk))
            )
            mids = [s.midpoint for s in chunk]
            found = inputs.counties.lookup([m[0] for m in mids], [m[1] for m in mids])
            county_source = [inputs.counties.provenance["source_id"]] * len(chunk)
            if inputs.fallback_counties is not None and any(c is None for c in found):
                miss = [i for i, c in enumerate(found) if c is None]
                extra = inputs.fallback_counties.lookup(
                    [mids[i][0] for i in miss], [mids[i][1] for i in miss]
                )
                for i, c in zip(miss, extra, strict=True):
                    found[i] = c
                    county_source[i] = inputs.fallback_counties.provenance["source_id"]
            for k, seg in enumerate(chunk):
                bbox = seg.bbox
                sub = _near(inputs.substations, bbox, SUBSTATION_SEARCH_MI * METRES_PER_MILE, plane, xy[k])
                radius = CONTEXT_RADIUS_MI * METRES_PER_MILE
                plants = _near(inputs.plants, bbox, radius, plane, xy[k])[:MAX_PLANTS]
                loads = _near(inputs.loads, bbox, radius, plane, xy[k])
                requests = _near(inputs.requests, bbox, radius, plane, xy[k])
                mw = [inputs.plants.values[i] for i, _ in plants]
                county = found[k]
                fiber = inputs.fiber.get(county.fips) if county is not None else None
                pros = _prospect(inputs.sampler, seg)
                prot = pros.get("protected_lands") or {}
                prot_ok = prot.get("st") in ("ok", "partial")
                fs = flood[k]
                row: dict[str, Any] = {
                    "id": _uuid.uuid4(),
                    "run_id": run_id,
                    "line_asset_id": line.id,
                    "segment_index": offset + seg.index,
                    "start_mi": round(seg.start_mi, 4),
                    "length_mi": round(seg.length_mi, 4),
                    "min_lon": bbox[0],
                    "min_lat": bbox[1],
                    "max_lon": bbox[2],
                    "max_lat": bbox[3],
                    "mid_lon": round(mids[k][0], 6),
                    "mid_lat": round(mids[k][1], 6),
                    "voltage_kv": line.voltage_kv,
                    "voltage_unknown": line.voltage_unknown,
                    "underground": bool(line.attributes.get("underground")),
                    "state_code": (
                        county.state_code if county is not None and county.state_code else line.state_code
                    ),
                    "county_fips": county.fips if county is not None else None,
                    "county_name": county.name if county is not None else None,
                    "substation_asset_id": _uuid.UUID(inputs.substations.ids[sub[0][0]]) if sub else None,
                    "substation_mi": _mi(sub[0][1]) if sub else None,
                    "flood_reason": fs.reason,
                    "retiring_mw": round(sum(v for v in mw if v is not None), 3) if plants else None,
                    "retiring_count": len(plants),
                    "protected_share": prot.get("share") if prot_ok else None,
                    "hard_protected_share": prot.get("hard") if prot_ok else None,
                    "coords": _coords(seg.coords),
                    "context": {
                        # Which part of a multi-part line the segment is on (`start_mi` is along it).
                        "part": part_index,
                        "plants": [
                            [inputs.plants.ids[i], _mi(m), inputs.plants.values[i]] for i, m in plants
                        ],
                        "loads": [[inputs.loads.ids[i], _mi(m)] for i, m in loads[:MAX_LOADS]],
                        # One entry per request, never one per point: the API keeps the requests the
                        # caller may see and only then takes a point's nearest distance and queued MW.
                        "requests": [
                            [inputs.requests.ids[i], inputs.requests.refs[i], _mi(m)]
                            for i, m in requests[:MAX_REQUESTS]
                        ],
                        "fiber": fiber,
                        "county_source": county_source[k] if county is not None else None,
                        "firm_panels": fs.panels,
                        "flood_reported": fs.reported,
                    },
                    "prospect": pros,
                    "source_id": line.source_id,
                    "source_url": line.source_url,
                    "retrieved_at": line.retrieved_at,
                    "licence_id": line.licence_id,
                    "computed_at": now,
                }
                for key in SITE_FINDER_DISTANCES:
                    row[f"flood_share_{key}"] = fs.share.get(key)
                    row[f"flood_cover_{key}"] = fs.cover.get(key, 0.0)
                rows.append(row)
        # A gap of one index between parts, so a band never joins two parts of a multi-part line.
        offset += len(segments) + 1
    return rows


# ------------------------------------------------------------------------------------------ runs
def _flood_status_counts(rows: Sequence[dict[str, Any]], key: str = "100") -> dict[str, int]:
    out = {"outside": 0, "partly_in": 0, "unavailable": 0}
    for r in rows:
        if (r[f"flood_cover_{key}"] or 0) < 0.999:
            out["unavailable"] += 1
        elif (r[f"flood_share_{key}"] or 0) > 0:
            out["partly_in"] += 1
        else:
            out["outside"] += 1
    return out


def refresh(
    session_factory: Callable[[], Session],
    data_root: pathlib.Path,
    *,
    states: Iterable[str] | None = None,
    sampler: Any = None,
    use_prospect: bool = True,
    now: Callable[[], dt.datetime] | None = None,
) -> dict[str, Any]:
    """One full run: compute, write in batches, then serve it and drop the previous runs' rows."""
    clock = now or (lambda: dt.datetime.now(dt.UTC))
    started = time.monotonic()
    with session_factory() as session:
        run = SiteFinderRun(status="running", started_at=clock())
        session.add(run)
        session.commit()
        run_id = run.id
    timings: dict[str, float] = {}
    try:
        with session_factory() as session:
            t0 = time.monotonic()
            inputs = gather_inputs(
                session, data_root, states=states, sampler=sampler, use_prospect=use_prospect
            )
            timings["inputs_s"] = round(time.monotonic() - t0, 1)
            layers_meta = prospect_mod.layer_metadata(inputs.sampler)
            t0 = time.monotonic()
            pending: list[dict[str, Any]] = []
            totals = {"segments": 0, "lines": 0, "miles": 0.0, "voltage_unknown_segments": 0}
            status_counts = {"outside": 0, "partly_in": 0, "unavailable": 0}
            stamp = clock()
            for line in measuring_order(inputs):
                rows = line_rows(inputs, line, run_id, stamp)
                totals["lines"] += 1
                totals["segments"] += len(rows)
                totals["miles"] += sum(r["length_mi"] for r in rows)
                totals["voltage_unknown_segments"] += sum(1 for r in rows if r["voltage_unknown"])
                for k, v in _flood_status_counts(rows).items():
                    status_counts[k] += v
                pending.extend(rows)
                if len(pending) >= INSERT_BATCH:
                    session.execute(sa.insert(SiteCandidate), pending)
                    session.commit()
                    pending = []
            if pending:
                session.execute(sa.insert(SiteCandidate), pending)
            timings["compute_s"] = round(time.monotonic() - t0, 1)
            done = session.get(SiteFinderRun, run_id)
            if done is None:
                raise RuntimeError(f"site finder run {run_id} vanished while it ran")
            older = list(session.scalars(select(SiteFinderRun.id).where(SiteFinderRun.id != run_id)))
            if older:
                session.execute(sa.delete(SiteCandidate).where(SiteCandidate.run_id.in_(older)))
            keep = list(
                session.scalars(
                    select(SiteFinderRun.id)
                    .where(SiteFinderRun.id != run_id)
                    .order_by(SiteFinderRun.started_at.desc())
                    .limit(KEEP_RUN_RECORDS - 1)
                )
            )
            session.execute(
                sa.delete(SiteFinderRun).where(
                    SiteFinderRun.id != run_id, SiteFinderRun.id.not_in(keep or [run_id])
                )
            )
            done.status = "ok"
            done.finished_at = clock()
            done.segments = int(totals["segments"])
            done.inputs = inputs.summary
            done.layers = layers_meta
            timings["total_s"] = round(time.monotonic() - started, 1)
            done.stats = {
                **totals,
                "miles": round(totals["miles"], 1),
                "flood_status_1mi": status_counts,
                # How often the flood files were read and a layer built: one per state and one per
                # set of states when the grouping holds (`measuring_order`).
                "flood_reads": {
                    "states": inputs.flood.state_reads if inputs.flood else 0,
                    "layers": inputs.flood.layers_built if inputs.flood else 0,
                },
                "timings": timings,
            }
            session.commit()
            report = {"run_id": str(run_id), "status": "ok", **done.stats}
    except Exception as exc:
        with session_factory() as session:
            session.execute(sa.delete(SiteCandidate).where(SiteCandidate.run_id == run_id))
            failed = session.get(SiteFinderRun, run_id)
            if failed is not None:
                failed.status = "failed"
                failed.finished_at = clock()
                failed.error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
        raise
    logger.info("site finder: %s", json.dumps(report, default=str), extra={"site_finder": report})
    return report


def current_run(session: Session) -> SiteFinderRun | None:
    """The run the API serves: the newest that finished `ok`."""
    return session.scalars(
        select(SiteFinderRun)
        .where(SiteFinderRun.status == "ok")
        .order_by(SiteFinderRun.finished_at.desc(), SiteFinderRun.started_at.desc())
        .limit(1)
    ).first()
