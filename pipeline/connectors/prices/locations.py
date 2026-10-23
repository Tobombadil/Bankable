"""The hubs and zones each price connector serves, where each is drawn, and the link-out markets.

Slice 1 of docs/29 (tranche 1, hubs and zones first; owner decision 2026-10-22). One entry per
pricing location a page or the API lists. `name` is the source's own spelling (the key every
parser matches on); `id` is ours and stable (`<market>.<name>`, lower case, punctuation folded to
`_`), so a URL never changes when a display label does.

Placement (docs/29 §1 point 1, §5): never a guessed precision.

* `point` only where the market itself publishes a display position for the location: SPP's
  three hubs, whose points come from SPP's own price contour map (`pricecontourmap.spp.org`,
  `PCM/DA_Features` layer 2, read 2026-10-22T20:28Z), labelled "as SPP's price map draws it".
  The connector refreshes them from the same feed on every DA run (`spp.py`).
* `region` everywhere else. A hub is an average over many buses and a zone is a settlement
  region; ERCOT, CAISO and NYISO publish no boundary file for either (docs/29 §1 point 1). The
  region is the state (or, for GB, the country) the location lies in, which is a fact the ISO's
  own footprint states; the map draws it as that region's polygon, never as a point (ADR 0008 §3).
  `label_point` is a place for a label inside the region polygon (the area centroid of its
  largest ring, computed from `data/vendored/regions/`), recorded with that method and never
  served as a location.

The basis reference (docs/29 §7.2 "basis to hub"; owner's slice 1 "basis to the market's
reference hub") is one hub per market, recorded with the figure: ERCOT North (HB_NORTH) and SP15
(TH_SP15_GEN-APND), the two the EIA wholesale table quotes for these markets (docs/29 §2.11), and
SPP North Hub, a choice recorded as such. NYISO and GB define no hub, so no basis is computed
there and the page says so (docs/29 §7.2: "It is not computed where no hub is defined").
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
REGIONS_DIR = ROOT / "data" / "vendored" / "regions"

KINDS = ("hub", "zone")
MARKETS = ("da", "rt")
GRADES = ("point", "region")


@dataclass(frozen=True)
class Placement:
    """Where a location is drawn and on what evidence (docs/29 §5.3 fields, at slice-1 grade)."""

    grade: str  # point | region
    method: str
    basis: str
    region_id: str
    lat: float | None = None
    lon: float | None = None
    evidence_url: str | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class PriceLocation:
    market: str  # ERCOT | SPP | CAISO | NYISO | GB
    source_id: str
    name: str
    label: str
    kind: str  # hub | zone
    timezone: str
    currency: str
    placement: Placement
    reference: str | None = None  # the `name` of the basis reference in the same market
    markets: tuple[str, ...] = ("da", "rt")
    note: str | None = None

    @property
    def id(self) -> str:
        return location_id(self.market, self.name)


def location_id(market: str, name: str) -> str:
    """`ERCOT`, `HB_NORTH` -> `ercot.hb_north`; `NYISO`, `N.Y.C.` -> `nyiso.n_y_c`."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return f"{market.lower()}.{slug}"


# ------------------------------------------------------------------------------------------ helpers
def _region(region_id: str, basis: str) -> Placement:
    return Placement(
        grade="region",
        method="region_of_market_footprint",
        basis=basis,
        region_id=region_id,
    )


_ERCOT_REGION = (
    "ERCOT publishes no hub location or load-zone boundary (docs/29 §2.1, §5.1); drawn as Texas, "
    "the state ERCOT's footprint lies in"
)
_CAISO_REGION = (
    "CAISO's OASIS names its trading hubs and default load aggregation points without a position, "
    "and its price map carries no trading hub (docs/29 §2.2); drawn as the state the area lies in"
)
_NYISO_REGION = "NYISO publishes no zone boundary file (docs/29 §1, §2.5); drawn as New York State"
_GB_REGION = "Great Britain is one price zone (docs/29 §2.8); drawn as the country"


# ------------------------------------------------------------------------------------------- ERCOT
ERCOT_SOURCE = "us.iso.ercot.prices"
_ERCOT_HUBS = (
    ("HB_NORTH", "North Hub"),
    ("HB_HOUSTON", "Houston Hub"),
    ("HB_SOUTH", "South Hub"),
    ("HB_WEST", "West Hub"),
    ("HB_PAN", "Panhandle Hub"),
    ("HB_HUBAVG", "Hub Average"),
    ("HB_BUSAVG", "Hub Bus Average"),
)
_ERCOT_ZONES = (
    ("LZ_HOUSTON", "Houston load zone"),
    ("LZ_NORTH", "North load zone"),
    ("LZ_SOUTH", "South load zone"),
    ("LZ_WEST", "West load zone"),
    ("LZ_AEN", "Austin Energy load zone"),
    ("LZ_CPS", "CPS Energy load zone"),
    ("LZ_LCRA", "LCRA load zone"),
    ("LZ_RAYBN", "Rayburn load zone"),
)

# ---------------------------------------------------------------------------------------------- SPP
SPP_SOURCE = "us.iso.spp.prices"
_SPP_MAP = "https://pricecontourmap.spp.org/arcgis/rest/services/PCM/DA_Features/MapServer/2"
_SPP_POINT_BASIS = (
    "SPP's own price contour map places the hub here (a display position, not a bus); read from "
    "PCM/DA_Features layer 2"
)
_SPP_HUBS = (
    # name, label, lat, lon, region the point falls in (vendored Census state polygons)
    ("SPPNORTH_HUB", "SPP North Hub", 41.20319320967241, -96.98877782732716, "US-NE"),
    ("SPPSOUTH_HUB", "SPP South Hub", 35.450628722295114, -98.0622028506405, "US-OK"),
    ("SWPW_HUB", "SPP RTO West Hub", 40.654198400033735, -105.74529919976321, "US-CO"),
)

# -------------------------------------------------------------------------------------------- CAISO
CAISO_SOURCE = "us.iso.caiso.oasis_prices"
_CAISO_LOCATIONS = (
    ("TH_SP15_GEN-APND", "SP15 trading hub", "hub", "US-CA"),
    ("TH_NP15_GEN-APND", "NP15 trading hub", "hub", "US-CA"),
    ("TH_ZP26_GEN-APND", "ZP26 trading hub", "hub", "US-CA"),
    ("DLAP_PGAE-APND", "PG&E default load aggregation point", "zone", "US-CA"),
    ("DLAP_SCE-APND", "SCE default load aggregation point", "zone", "US-CA"),
    ("DLAP_SDGE-APND", "SDG&E default load aggregation point", "zone", "US-CA"),
    # Valley Electric Association serves southern Nevada (Pahrump); its DLAP is in CAISO's market.
    ("DLAP_VEA-APND", "Valley Electric default load aggregation point", "zone", "US-NV"),
)

# -------------------------------------------------------------------------------------------- NYISO
NYISO_SOURCE = "us.iso.nyiso.prices"
#: The eleven in-state zones (A-K). The external proxy zones in the same files (H Q, NPX, O H, PJM)
#: are not New York zones and are left out.
_NYISO_ZONES = (
    ("WEST", "Zone A, West"),
    ("GENESE", "Zone B, Genesee"),
    ("CENTRL", "Zone C, Central"),
    ("NORTH", "Zone D, North"),
    ("MHK VL", "Zone E, Mohawk Valley"),
    ("CAPITL", "Zone F, Capital"),
    ("HUD VL", "Zone G, Hudson Valley"),
    ("MILLWD", "Zone H, Millwood"),
    ("DUNWOD", "Zone I, Dunwoodie"),
    ("N.Y.C.", "Zone J, New York City"),
    ("LONGIL", "Zone K, Long Island"),
)

# ----------------------------------------------------------------------------------------------- GB
ELEXON_SOURCE = "gb.elexon.bmrs_prices"


def _build() -> tuple[PriceLocation, ...]:
    out: list[PriceLocation] = []
    for name, label in _ERCOT_HUBS:
        out.append(
            PriceLocation(
                "ERCOT",
                ERCOT_SOURCE,
                name,
                f"ERCOT {label}",
                "hub",
                "US/Central",
                "USD",
                _region("US-TX", _ERCOT_REGION),
                reference="HB_NORTH",
            )
        )
    for name, label in _ERCOT_ZONES:
        out.append(
            PriceLocation(
                "ERCOT",
                ERCOT_SOURCE,
                name,
                f"ERCOT {label}",
                "zone",
                "US/Central",
                "USD",
                _region("US-TX", _ERCOT_REGION),
                reference="HB_NORTH",
            )
        )
    for name, label, lat, lon, region in _SPP_HUBS:
        out.append(
            PriceLocation(
                "SPP",
                SPP_SOURCE,
                name,
                label,
                "hub",
                "US/Central",
                "USD",
                Placement(
                    grade="point",
                    method="iso_display_point",
                    basis=_SPP_POINT_BASIS,
                    region_id=region,
                    lat=lat,
                    lon=lon,
                    evidence_url=_SPP_MAP,
                    retrieved_at="2026-10-22T20:28:18Z",
                ),
                reference="SPPNORTH_HUB",
                note=(
                    "Day-ahead statistics only: SPP's real-time hub price is read once an hour for "
                    "the latest readout (its acceptable-use policy limits polling)."
                ),
            )
        )
    for name, label, kind, region in _CAISO_LOCATIONS:
        out.append(
            PriceLocation(
                "CAISO",
                CAISO_SOURCE,
                name,
                f"CAISO {label}",
                kind,
                "US/Pacific",
                "USD",
                _region(region, _CAISO_REGION),
                reference="TH_SP15_GEN-APND",
            )
        )
    for name, label in _NYISO_ZONES:
        out.append(
            PriceLocation(
                "NYISO",
                NYISO_SOURCE,
                name,
                f"NYISO {label}",
                "zone",
                "US/Eastern",
                "USD",
                _region("US-NY", _NYISO_REGION),
                reference=None,
            )
        )
    out.append(
        PriceLocation(
            "GB",
            ELEXON_SOURCE,
            "SYSTEM",
            "Great Britain system price",
            "zone",
            "Europe/London",
            "GBP",
            _region("GB", _GB_REGION),
            reference=None,
            markets=("rt",),
            note=(
                "The imbalance (system) price per half-hour settlement period. GB's day-ahead "
                "auction prices belong to EPEX SPOT and Nord Pool and are not shown (docs/29 §2.10)."
            ),
        )
    )
    return tuple(out)


LOCATIONS: tuple[PriceLocation, ...] = _build()
BY_ID: dict[str, PriceLocation] = {loc.id: loc for loc in LOCATIONS}


def for_source(source_id: str) -> tuple[PriceLocation, ...]:
    return tuple(loc for loc in LOCATIONS if loc.source_id == source_id)


def by_name(source_id: str) -> dict[str, PriceLocation]:
    return {loc.name: loc for loc in for_source(source_id)}


def reference_of(loc: PriceLocation) -> PriceLocation | None:
    if loc.reference is None or loc.reference == loc.name:
        return None
    return by_name(loc.source_id).get(loc.reference)


# --------------------------------------------------------------------------------------- link-outs
@dataclass(frozen=True)
class LinkOut:
    """A market whose prices are not shown, with its own price page and the reason (docs/29)."""

    market: str
    label: str
    source_id: str
    url: str
    reason: str
    reference: str = field(default="docs/29")


LINK_OUTS: tuple[LinkOut, ...] = (
    LinkOut(
        "PJM",
        "PJM Interconnection",
        "us.iso.pjm.prices",
        "https://www.pjm.com/markets-and-operations/energy",
        "Every PJM price route is Data Miner 2, whose terms bar redistribution of data "
        '"contained in or derived from" it without a PJM Redistribution License, which we do not hold.',
        "docs/29 §2.7",
    ),
    LinkOut(
        "MISO",
        "MISO",
        "us.iso.miso.prices",
        "https://www.misoenergy.org/markets-and-operations/real-time--market-data/market-reports/",
        "MISO's legal page could not be read (a security challenge we do not bypass), and its terms "
        "must be read and recorded before any MISO price is published.",
        "docs/29 §2.3",
    ),
    LinkOut(
        "ISO-NE",
        "ISO New England",
        "us.iso.isone.prices",
        "https://www.iso-ne.com/isoexpress/",
        "ISO New England's terms say \"any duplication of the Content or non-personal use may violate "
        'copyright"; we link out until it permits reuse in writing.',
        "docs/29 §2.6",
    ),
)
#: A market shown only under one posture: its link-out stands in when its rows are not visible
#: (review 2026-10-23: under the commercial posture SPP vanished from the page without a word).
POSTURE_LINK_OUTS: tuple[LinkOut, ...] = (
    LinkOut(
        "SPP",
        "Southwest Power Pool",
        SPP_SOURCE,
        "https://www.spp.org/markets-operations/",
        'SPP\'s terms allow copying with citation except "within a commercial publication"; its prices '
        "are shown here only while the site runs non-commercially.",
        "docs/29 §2.4",
    ),
)
#: What each market's day-ahead and real-time figures are built from (docs/29 §7.2 as built): the
#: real-time market differs by ISO, and the page says which.
MARKET_LABELS: dict[str, dict[str, str]] = {
    "ERCOT": {"da": "day-ahead, hourly", "rt": "real-time, 15-minute settlement point prices"},
    "SPP": {"da": "day-ahead, hourly", "rt": "real-time, 5-minute (read once an hour)"},
    "CAISO": {"da": "day-ahead, hourly", "rt": "real-time, 15-minute pre-dispatch (RTPD)"},
    "NYISO": {"da": "day-ahead, hourly", "rt": "real-time, hourly integrated"},
    "GB": {"rt": "system price, half-hourly (imbalance settlement)"},
}


# ------------------------------------------------------------------------------------- label points
def _ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """(signed area, cx, cy) of one ring by the shoelace formula, in degrees."""
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area *= 0.5
    if area == 0:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return area, cx / (6 * area), cy / (6 * area)


@lru_cache(maxsize=1)
def _region_index() -> dict[str, list[list[list[list[float]]]]]:
    out: dict[str, list[list[list[list[float]]]]] = {}
    for name in ("us_states.geojson", "countries.geojson"):
        path = REGIONS_DIR / name
        if not path.exists():
            continue
        for feature in json.loads(path.read_text(encoding="utf-8"))["features"]:
            rid = feature["properties"].get("region_id")
            geom = feature.get("geometry") or {}
            if rid and geom.get("type") == "MultiPolygon":
                out[rid] = geom["coordinates"]
    return out


def label_point(region_id: str) -> dict[str, Any] | None:
    """A label position inside `region_id`'s polygon: the area centroid of its largest outer ring,
    5 decimals. A label, never a location (module docstring)."""
    polygons = _region_index().get(region_id)
    if not polygons:
        return None
    best: tuple[float, float, float] | None = None
    for polygon in polygons:
        area, cx, cy = _ring_centroid([list(map(float, p[:2])) for p in polygon[0]])
        if best is None or abs(area) > abs(best[0]):
            best = (area, cx, cy)
    if best is None:
        return None
    return {
        "lat": round(best[2], 5),
        "lon": round(best[1], 5),
        "method": "area centroid of the largest ring of the region polygon (label only, not a location)",
    }
