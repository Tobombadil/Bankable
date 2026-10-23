"""Land parcels per candidate segment (Texas, TxGIO StratMap; `us.tx.txgio.parcels`;
docs/25-siting-parcels.md; owner decision 2026-10-23: "Inframation serves the owner's own powered-land
deals first").

**What a segment stores** (`SiteCandidate.context["parcels"]`, the per-segment context store; no new
table or column). For the segment's band at each half-width the finder offers (0.25, 0.5, 1 and 2 miles,
`WIDTHS`), from the parcels the server pulled (`services/context_sample/parcels.py`):

- `cov`: `ok` when every county the 2-mile band reaches has parcels loaded, `partial` when some do,
  `none` when none does; `miss`: those without, as `[fips, name]` ("parcels not loaded for this
  county"). Coverage is read at the segment's midpoint and at points around its 2-mile band.
- `n`: how many distinct holdings touch the band at each width (every kind).
- `x`: how many of them are left out of the acreage figures (`LEFT_OUT_KINDS`: right-of-way strips,
  exempt land, utility land; `pipeline/context/txgio_parcels.py` decides the kind at build time).
- `h`: the holdings touching the 2-mile band that matter for a deal, as `[key, acres, parcels, w]`
  (`key` is `<county FIPS>:<holding id>`, since a holding id is drawn per county build; `w` the index
  of the narrowest width whose band they touch, so a holding touches every wider band too): every
  counted holding of `LIST_MIN_ACRES` or more, and each width's `TOP_PER_WIDTH` largest whatever their
  size; largest first, at most `MAX_LISTED` (`more` says some were left out, never one of a width's
  largest).
- `d`: for each width's largest holdings, `[land-use category, market value per acre, county FIPS,
  the property id of the holding's largest parcel]`, so the drawer can link to the appraisal record
  without the parcel files.
- `b`: per width, the largest contiguous acreage reachable from at most 1, 3, 5 and 10 owners
  (`OWNER_CAPS`): 1 is the largest single holding touching the band; 3, 5 and 10 join neighbouring
  holdings (parcels of two holdings within 30 m of each other), each of which touches the band. A
  greedy search from the largest holdings (`assemble`): a lower bound, not an optimum. A holding added
  to an assemblage brings its acres less the land it shares with the holdings already in (two owners
  on one outline, an account drawn inside another; the build lists each holding's partners, `Shared`),
  so one tract is never counted twice.

Nothing here is personal: the parcel files carry no owner field, and a holding id is random per build.
A band's figures are merged from its segments' (`merge_band`): exact for the largest holding, the
holdings of 20 acres or more and each width's ten largest; "at least" for the count of every holding
touching a band of more than one segment (a holding beside two segments is counted once in each, and
only the large ones are stored by id).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from services.site_finder.geometry import METRES_PER_MILE, LocalPlane

#: The band half-widths, in the order of `services.db.models.SITE_FINDER_DISTANCES` ("025" ... "200").
WIDTHS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)
#: "Owners at most": the caps the finder offers; "any" is the last (`ANY_OWNERS`).
OWNER_CAPS: tuple[int, ...] = (1, 3, 5, 10)
ANY_OWNERS = OWNER_CAPS[-1]
#: "Contiguous acres at least": the thresholds the finder offers.
ACRE_THRESHOLDS: tuple[int, ...] = (20, 50, 100, 200, 500, 1000)
LIST_MIN_ACRES = 20.0
TOP_PER_WIDTH = 10
MAX_LISTED = 150
#: A holding smaller than this never joins an assemblage (a town lot cannot make a site).
ASSEMBLY_MIN_ACRES = 1.0
#: Holdings this close (metres) are neighbours: the same rule that groups one owner's parcels.
NEIGHBOUR_M = 30.0
SEEDS = 8
#: Points sampled around the 2-mile band to find the counties it reaches.
COVERAGE_POINTS = 16
#: Holding kinds counted (`n`, `x`) but left out of every acreage figure, the listing and the
#: assemblies: a railroad's or a road's fee-owned right of way would otherwise top "largest holding" and
#: pass "contiguous acres at least 500" along a line; exempt and utility land is not for a deal.
LEFT_OUT_KINDS = frozenset({"strip", "exempt", "utility"})
#: Texas's state FIPS code: a county outside it is "Texas only", not "not loaded".
TEXAS_FIPS = "48"

NOTE = "Owner names are on the appraisal district's record; Inframation does not store them."
NOT_A_SURVEY = (
    "Appraisal-district parcels compiled by TxGIO: for screening, not survey grade and not for legal "
    "purposes. A holding is one owner's parcels that touch or lie within 30 m of each other in the "
    "appraisal roll; acres are measured from the outlines, each outline once."
)
LEFT_OUT_NOTE = (
    "Right-of-way strips, exempt land and utility land are counted but left out of the acreage figures."
)


def holding_key(fips: Any, holding_id: Any) -> str:
    """A holding's key across counties: its id is drawn per county build, so the county is part of it."""
    return f"{fips}:{holding_id}"


def split_key(key: str) -> tuple[str | None, str]:
    """(county FIPS, holding id) of a key; a key stored without its county (older runs) has no FIPS."""
    fips, sep, hid = key.partition(":")
    return (fips, hid) if sep else (None, key)


# ------------------------------------------------------------------------------------- assemblies
@dataclass(frozen=True)
class Shared:
    """Land two holdings both cover (`pipeline/context/txgio_parcels.py` builds it per county): `pairs[h]`
    the acres `h` shares with each listed partner, `rest[h]` what it shares with partners not listed. The
    build puts every shared pair on each side, listed or in `rest`, so whichever joins first, the second
    subtracts it."""

    pairs: Mapping[str, Mapping[str, float]]
    rest: Mapping[str, float]

    def gain(self, h: str, acres: float, members: Iterable[str]) -> float:
        """What `h` adds to an assemblage of `members`: its acres less the land it shares with them, and
        less all it shares with partners not listed (whether or not they joined): never above the union."""
        mine = self.pairs.get(h) or {}
        shared = sum(a for m, a in mine.items() if m in members) if mine else 0.0
        return max(0.0, acres - shared - float(self.rest.get(h, 0.0)))


NOTHING_SHARED = Shared({}, {})


def assemble(
    nodes: Mapping[str, float],
    neighbours: Mapping[str, set[str]],
    caps: Sequence[int] = OWNER_CAPS,
    shared: Shared = NOTHING_SHARED,
) -> list[float]:
    """The largest contiguous acreage from at most `cap` holdings, for each cap: from each of the
    `SEEDS` largest holdings, add the neighbouring holding that adds the most until the cap. A holding
    added brings its acres less the land it shares with the holdings already in (`Shared.gain`: two
    owners on one outline, an account drawn inside another), so the sum never exceeds the union's area:
    a lower bound, never a double count."""
    order = sorted(nodes, key=lambda h: (-nodes[h], h))
    best = [0.0] * len(caps)
    if not order:
        return best
    top = max(caps)
    for seed in order[:SEEDS]:
        members = {seed}
        total = nodes[seed]
        by_size = [total]
        frontier = {n for n in neighbours.get(seed, ()) if n in nodes}
        while len(members) < top:
            frontier -= members
            if not frontier:
                break
            gains = {h: shared.gain(h, nodes[h], members) for h in frontier}
            nxt = max(frontier, key=lambda h: (gains[h], nodes[h], h))
            members.add(nxt)
            total += gains[nxt]
            by_size.append(total)
            frontier |= {n for n in neighbours.get(nxt, ()) if n in nodes}
        for i, cap in enumerate(caps):
            value = by_size[min(cap, len(by_size)) - 1]
            if value > best[i]:
                best[i] = value
    return best


def shared_land(table: Any, keys: Sequence[str | None], fips: Sequence[Any]) -> Shared:
    """The `Shared` land of the holdings in a parcel table (one entry per holding: every parcel of a
    holding carries the same figures). A file from before the overlaps were built shares nothing."""
    pairs: dict[str, dict[str, float]] = {}
    rest: dict[str, float] = {}
    if "holding_overlaps" not in table.column_names:
        return NOTHING_SHARED
    lists = table.column("holding_overlaps").to_pylist()
    rests = _column(table, "holding_overlap_rest_acres", 0.0)
    for key, f, found, extra in zip(keys, fips, lists, rests, strict=True):
        if key is None or key in pairs or key in rest:
            continue
        if extra:
            rest[key] = float(extra)
        if found:
            pairs[key] = {holding_key(f, e["holding_id"]): float(e["acres"]) for e in found}
    return Shared(pairs, rest)


def _column(table: Any, name: str, default: Any = None) -> list[Any]:
    """A column of a parcel table, or `default` per row when the file predates it."""
    if name in table.column_names:
        values: list[Any] = table.column(name).to_pylist()
        return values
    return [default] * table.num_rows


# --------------------------------------------------------------------------------- per chunk
@dataclass
class ParcelContext:
    """The compute job's view of the parcel layer: `layer` (or None when this worker holds none) and a
    county lookup for coverage (`lookup(lons, lats) -> [(fips, name) | None]`)."""

    layer: Any
    lookup: Callable[[Sequence[float], Sequence[float]], list[tuple[str, str] | None]]

    def summary(self) -> dict[str, Any]:
        """What the run records about the parcels it measured. `measure_id` is new every time segments
        are measured (a full run or `compute.refresh_parcels`): the API keys its parcel caches on it, so
        a re-measure that keeps the layer's version is never served from the old figures."""
        import datetime as dt
        import uuid

        stamp = {
            "measured_at": dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "measure_id": uuid.uuid4().hex,
        }
        if self.layer is None:
            return {"source_id": "us.tx.txgio.parcels", "loaded": False, "counties": {}, **stamp}
        return {**self.layer.summary(), "loaded": True, **stamp}

    def measure(self, plane: LocalPlane, segments_xy: Sequence[Any]) -> list[dict[str, Any] | None]:
        """The `parcels` context of each segment of one chunk (vertices in `plane`'s metres)."""
        if self.layer is None or not segments_xy:
            return [None] * len(segments_xy)
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        reach = WIDTHS[-1] * METRES_PER_MILE
        # Every parcel within the widest band of any segment of the chunk, plus a neighbour margin.
        allx = np.concatenate([np.asarray(xy)[:, 0] for xy in segments_xy])
        ally = np.concatenate([np.asarray(xy)[:, 1] for xy in segments_xy])
        pad = reach + NEIGHBOUR_M + 50.0
        lon0, lat0 = plane.inverse(allx.min() - pad, ally.min() - pad)
        lon1, lat1 = plane.inverse(allx.max() + pad, ally.max() + pad)
        box = (float(lon0), float(lat0), float(lon1), float(lat1))
        if not self.layer.counties_in(box):
            # No loaded county anywhere near: nothing is stored, and the API names the band's own
            # county as one whose parcels are not loaded.
            return [None] * len(segments_xy)
        lines = [shapely.LineString(xy) for xy in segments_xy]
        bands = [[shapely.buffer(line, w * METRES_PER_MILE, quad_segs=8) for w in WIDTHS] for line in lines]
        coverage = [self._coverage(plane, line, reach) for line in lines]
        loaded = sorted({f for cov in coverage for f in cov["loaded"]})
        table = self.layer.read_bbox(box, counties=loaded) if loaded else None
        out: list[dict[str, Any] | None] = []
        if table is None or table.num_rows == 0:
            for cov in coverage:
                out.append(self._empty(cov))
            return out
        geoms = plane.to_xy(shapely.from_wkb(np.asarray(table.column("geometry").to_pylist(), dtype=object)))
        fips = table.column("fips").to_pylist()
        keys = [
            holding_key(f, h) if h is not None else None
            for f, h in zip(fips, table.column("holding_id").to_pylist(), strict=True)
        ]
        h_acres = table.column("holding_acres").to_pylist()
        h_parcels = table.column("holding_parcels").to_pylist()
        h_use = table.column("holding_land_use").to_pylist()
        h_vpa = table.column("holding_value_per_acre").to_pylist()
        h_link = table.column("holding_link_prop_id").to_pylist()
        h_kind = _column(table, "holding_kind", "land")
        counted = [k not in LEFT_OUT_KINDS for k in h_kind]
        shared = shared_land(table, keys, fips)
        tree = shapely.STRtree(geoms)
        neighbours = self._neighbours(geoms, keys, h_acres, counted)
        for k, cov in enumerate(coverage):
            if not cov["loaded"]:
                out.append(self._empty(cov))
                continue
            idx = tree.query(bands[k][-1], predicate="intersects")
            narrowest = np.full(len(idx), len(WIDTHS) - 1, dtype=np.int64)
            if len(idx):
                for w in range(len(WIDTHS) - 2, -1, -1):
                    hit = shapely.intersects(bands[k][w], geoms[idx])
                    narrowest[hit] = w
            holdings: dict[str, list[Any]] = {}
            left_out: dict[str, int] = {}
            for i, w in zip(idx.tolist(), narrowest.tolist(), strict=True):
                key = keys[i]
                if key is None:
                    continue
                if not counted[i]:
                    left_out[key] = min(w, left_out.get(key, w))
                    continue
                entry = holdings.get(key)
                if entry is None:
                    holdings[key] = [float(h_acres[i] or 0.0), int(h_parcels[i] or 1), w, i]
                elif w < entry[2]:
                    entry[2] = w
            out.append(self._segment(cov, holdings, left_out, neighbours, shared, h_use, h_vpa, fips, h_link))
        return out

    # ------------------------------------------------------------------------------------ helpers
    def _coverage(self, plane: LocalPlane, line: Any, reach: float) -> dict[str, Any]:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        ring = shapely.buffer(line, reach, quad_segs=4).exterior
        pts = [
            shapely.line_interpolate_point(ring, t, normalized=True)
            for t in np.linspace(0, 1, COVERAGE_POINTS, endpoint=False)
        ]
        pts.append(shapely.line_interpolate_point(line, 0.5, normalized=True))
        xs = np.array([p.x for p in pts])
        ys = np.array([p.y for p in pts])
        lons, lats = plane.inverse(xs, ys)
        found = self.lookup(list(map(float, lons)), list(map(float, lats)))
        names = dict(c for c in found if c is not None)
        loaded = sorted(f for f in names if self.layer.loaded(f))
        missing = sorted(f for f in names if f not in loaded)
        status = "ok" if loaded and not missing else ("partial" if loaded else "none")
        return {"cov": status, "loaded": loaded, "miss": [[f, names[f]] for f in missing]}

    @staticmethod
    def _empty(cov: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"cov": cov["cov"], "miss": cov["miss"]}
        if cov["loaded"]:
            # Counties loaded, no parcel near: zero, not unknown.
            zeros = [0] * len(WIDTHS)
            out.update(n=zeros, x=list(zeros), h=[], d={}, b=[[0.0] * len(OWNER_CAPS) for _ in WIDTHS])
        return out

    @staticmethod
    def _neighbours(
        geoms: Any, keys: Sequence[Any], acres: Sequence[Any], counted: Sequence[bool] | None = None
    ) -> dict[str, set[str]]:
        """Counted holdings of an acre or more whose parcels lie within `NEIGHBOUR_M` of each other."""
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        ok = counted if counted is not None else [True] * len(keys)
        big = np.array(
            [
                (a or 0.0) >= ASSEMBLY_MIN_ACRES and h is not None and c
                for a, h, c in zip(acres, keys, ok, strict=True)
            ]
        )
        sel = np.nonzero(big)[0]
        out: dict[str, set[str]] = {}
        if len(sel) < 2:
            return out
        sub = geoms[sel]
        left, right = shapely.STRtree(sub).query(sub, predicate="dwithin", distance=NEIGHBOUR_M)
        for a, b in zip(sel[left].tolist(), sel[right].tolist(), strict=True):
            ha, hb = keys[a], keys[b]
            if ha != hb:
                out.setdefault(ha, set()).add(hb)
                out.setdefault(hb, set()).add(ha)
        return out

    @staticmethod
    def _segment(
        cov: Mapping[str, Any],
        holdings: Mapping[str, list[Any]],
        left_out: Mapping[str, int],
        neighbours: Mapping[str, set[str]],
        shared: Shared,
        use: Sequence[Any],
        vpa: Sequence[Any],
        fips: Sequence[Any],
        link: Sequence[Any],
    ) -> dict[str, Any]:
        ranked = sorted(holdings.items(), key=lambda kv: (-kv[1][0], kv[0]))
        x = [sum(1 for w0 in left_out.values() if w0 <= w) for w in range(len(WIDTHS))]
        n = [sum(1 for _, e in ranked if e[2] <= w) + x[w] for w in range(len(WIDTHS))]
        tops: list[str] = []
        for w in range(len(WIDTHS)):
            tops.extend([h for h, e in ranked if e[2] <= w][:TOP_PER_WIDTH])
        keep_top = dict.fromkeys(tops)
        listed = [h for h, _ in ranked if h in keep_top]
        others = [h for h, e in ranked if h not in keep_top and e[0] >= LIST_MIN_ACRES]
        room = max(0, MAX_LISTED - len(listed))
        more = len(others) > room
        chosen = set(listed) | set(others[:room])
        h_list = [[h, round(e[0], 1), int(e[1]), int(e[2])] for h, e in ranked if h in chosen]
        details = {}
        for h in keep_top:
            i = holdings[h][3]
            v = vpa[i]
            details[h] = [use[i] or "not_stated", int(v) if v is not None else None, fips[i], link[i]]
        b: list[list[float]] = []
        for w in range(len(WIDTHS)):
            nodes = {h: e[0] for h, e in holdings.items() if e[2] <= w and e[0] >= ASSEMBLY_MIN_ACRES}
            if not nodes:
                # A band of town lots only: the largest single holding still counts as one owner.
                single = max((e[0] for e in holdings.values() if e[2] <= w), default=0.0)
                b.append([round(single, 1)] * len(OWNER_CAPS))
                continue
            b.append([round(v, 1) for v in assemble(nodes, neighbours, shared=shared)])
        return {
            "cov": cov["cov"],
            "miss": cov["miss"],
            "n": n,
            "x": x,
            "h": h_list,
            "d": details,
            "b": b,
            "more": more,
        }


# ------------------------------------------------------------------------------------ band merge
@dataclass(frozen=True)
class Holding:
    holding_id: str
    acres: float
    parcels: int
    land_use: str | None
    value_per_acre: int | None
    fips: str | None
    link_prop_id: str | None


def width_index(adjacent_mi: float) -> int:
    return min(range(len(WIDTHS)), key=lambda i: abs(WIDTHS[i] - adjacent_mi))


def owners_index(max_owners: int | None) -> int:
    """The `b` column for an owners cap (None = any: up to `ANY_OWNERS` holdings)."""
    return OWNER_CAPS.index(max_owners) if max_owners in OWNER_CAPS else len(OWNER_CAPS) - 1


def merge_band(
    contexts: Iterable[Mapping[str, Any] | None],
    adjacent_mi: float,
    *,
    max_owners: int | None = None,
    min_acres: float | None = None,
    top: int = TOP_PER_WIDTH,
) -> dict[str, Any]:
    """One band's parcels figures from its segments' stored contexts (module docstring). Holdings are
    keyed by county and id together.

    `holdings_touching` is the count of distinct holdings touching the band: exact for a band of one
    segment, and when every holding touching it is large enough to be stored by id; otherwise the
    largest count it can be shown to reach (`holdings_touching_exact` false)."""
    w = width_index(adjacent_mi)
    oi = owners_index(max_owners)
    statuses: list[str] = []
    missing: dict[str, str] = {}
    entries: dict[str, list[Any]] = {}
    details: dict[str, list[Any]] = {}
    smaller_at_least = 0
    most_on_one = 0
    left_out = 0
    measured_segments = 0
    any_smaller = False
    assembly = 0.0
    largest = 0.0
    truncated = False
    for ctx in contexts:
        if not ctx:
            statuses.append("absent")
            continue
        statuses.append(str(ctx.get("cov") or "none"))
        for f, name in ctx.get("miss") or []:
            missing[str(f)] = str(name)
        if "b" not in ctx:
            continue
        measured_segments += 1
        seg_more = bool(ctx.get("more"))
        truncated = truncated or seg_more
        seg_listed_big = 0
        for key, acres, parcels, hw in ctx.get("h") or []:
            if int(hw) > w:
                continue
            entries[key] = [float(acres), int(parcels)]
            if float(acres) >= LIST_MIN_ACRES:
                seg_listed_big += 1
        for key, d in (ctx.get("d") or {}).items():
            details.setdefault(key, list(d))
        n = ctx.get("n") or [0] * len(WIDTHS)
        x = ctx.get("x") or [0] * len(WIDTHS)
        seg_left_out = int(x[w]) if len(x) > w else 0
        left_out = max(left_out, seg_left_out)
        most_on_one = max(most_on_one, int(n[w]))
        smaller = int(n[w]) - seg_left_out - seg_listed_big
        if smaller > 0:
            any_smaller = True
        if not seg_more:
            # Every counted holding of 20 acres or more beside this segment is listed, so the rest are
            # smaller ones: distinct from every listed large holding of the band.
            smaller_at_least = max(smaller_at_least, smaller)
        bw = (ctx.get("b") or [])[w] if len(ctx.get("b") or []) > w else None
        if bw:
            assembly = max(assembly, float(bw[oi]))
            largest = max(largest, float(bw[0]))
    measured = measured_segments > 0
    if not measured:
        status = "not_loaded"
    elif any(s in ("partial", "none", "absent") for s in statuses):
        status = "partial"
    else:
        status = "ok"
    ranked = sorted(entries.items(), key=lambda kv: (-kv[1][0], kv[0]))
    big = [h for h, e in ranked if e[0] >= LIST_MIN_ACRES]
    holdings = []
    for key, e in ranked[:top]:
        fips, hid = split_key(key)
        d = details.get(key) or [None, None, None, None]
        holdings.append(
            Holding(
                holding_id=hid,
                acres=e[0],
                parcels=e[1],
                land_use=d[0],
                value_per_acre=d[1],
                fips=fips or d[2],
                link_prop_id=d[3],
            )
        )
    thresholds = {str(t): sum(1 for _, e in ranked if e[0] >= t) for t in ACRE_THRESHOLDS}
    touching = most_on_one
    if not truncated:
        touching = max(touching, len(big) + smaller_at_least + left_out)
    exact = measured_segments == 1 or (not truncated and not any_smaller and left_out == 0)
    return {
        "status": status,
        "counties_missing": [{"fips": f, "name": n} for f, n in sorted(missing.items())],
        "largest_acres": round(largest, 1) if measured else None,
        "assembly_acres": round(assembly, 1) if measured else None,
        "owners_cap": OWNER_CAPS[oi],
        "holdings_20_plus": len(big),
        "holdings_20_plus_exact": not truncated,
        "smaller_holdings_at_least": smaller_at_least,
        "holdings_touching": touching if measured else None,
        "holdings_touching_exact": exact if measured else None,
        "left_out_at_least": left_out,
        "at_threshold": thresholds,
        "meets_min_acres": (assembly >= min_acres) if (measured and min_acres) else None,
        "top": holdings,
    }


__all__ = [
    "ACRE_THRESHOLDS",
    "ANY_OWNERS",
    "LEFT_OUT_KINDS",
    "LEFT_OUT_NOTE",
    "NOTE",
    "NOTHING_SHARED",
    "NOT_A_SURVEY",
    "OWNER_CAPS",
    "TEXAS_FIPS",
    "WIDTHS",
    "Holding",
    "ParcelContext",
    "Shared",
    "assemble",
    "holding_key",
    "merge_band",
    "owners_index",
    "shared_land",
    "split_key",
    "width_index",
]
