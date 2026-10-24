"""us.va.loudoun.data_centers — Loudoun County's own data-centre pipeline: land with a data-centre
land-use application in review or approved.

What the source is (measured 2026-10-23, docs/08-data-centre-sources.md §4.5): the county's
"Pipeline_Data_Center_Areas" hosted feature layer (layer 1, named "Proposed_DC_Parcels_March1_SINGLE"),
published by Loudoun County GIS on ArcGIS Online behind the county's "Existing & Potential Data Centers"
map. One polygon per
application: the parcels a data-centre application covers, dissolved, with the application number
(`Application`), its status (`FIRST_Status`: Active, Approved, Approved: Conditionally), the
subdivision (`FIRST_Subdivision`), the zoning case it relies on (`FIRST_Zoning_Case_Num`), the
building floor area applied for (`FIRST_Overall_SQ_FT`) and the pipeline acreage
(`SUM_Pipeline_Acres`). 85 rows on 2026-10-23. Application prefixes seen: EPLAN, LEGI, STPL, STMP,
SPAM, ZMAP (county) and TLREZN, TLSPEX (Town of Leesburg).

Terms (read 2026-10-23, docs/13 §6): the layer's own ArcGIS item (`ITEM_PAGE`) carries the licence
"These data were generated for use by Loudoun County and are available to the public. These data are
intended for use at 1:2400 scale or smaller. Acknowledgement of Loudoun County would be appreciated in
products derived from this data." plus the county's as-is disclaimer. The credit renders on every record
(`data/sources.yaml` `attribution`). `fetch` reads that licence text first on every run and the run is
`blocked` when it no longer hashes to `LICENCE_SHA256`, until a reviewer re-reads it, updates
`data/sources.yaml` and docs/13 and moves the constant (the docs/00-PLAN.md rule for reviewed terms).
Only the licence text is kept from the item: its other fields (the publishing account among them)
never reach the snapshot.

Fetch (four requests, 0.5 rps per host, robots.txt read on each host first): the item's licence, the
layer's metadata (name, `dataLastEditDate`, field names; a missing or renamed field fails the run), the
layer's rows with an explicit field list (`FIELDS`, never `outFields=*`), and Virginia DEQ's data-centre
air sites (`us.va.deq.data_center_air_sites`, the same public layer and selection rule as that
connector, five columns and the point, no name), which only decide the held-site links below.

Record model: one `proposal` per distinct site, `kind = load`, `technology = load`, as for Virginia DEQ
and EPA ICIS-Air (docs/25 §3). A site is the land of one application, or of several applications on the
same land: two applications whose polygons overlap by at least half of the smaller one
(`SAME_LAND_SHARE`) are one site (4 pairs on 2026-10-23, each a legislative application and a site
plan on identical land; the next largest overlap is 0.2 %, i.e. neighbours sharing a boundary).
Neighbouring parcels stay separate sites: the filings prove separate applications on separate land.
The grouping is transitive, so a large application (a rezoning over several separate site plans) could
chain land that is not shared into one site. `parse` refuses that: a site whose applications do not
all share land with each other fails the run (`chained`) until a reviewer splits the group or records
it in `ACCEPTED_CHAINS`. None on 2026-10-24.

Fields:
- source_record_id: the site's founding application number (earliest year, then type, then sequence),
  stripped and upper-cased ("LEGI-2024-0047 " arrives with a trailing space). Stable while that
  application stays in the layer. source_url: the layer query that returns exactly the site's
  applications as an HTML page, matched on the numbers as the layer stores them (`published_numbers`
  in `raw`; `record_url` says why).
- name_canonical: "<application(s)> data center application(s)", then "<subdivision> subdivision". The
  application number leads so the resolver's name-token block never pairs these rows with an operator's
  other campuses on a shared first word (docs/22; the FAA lane's 2026-10-23 finding). The subdivision is
  the recorded plat's name, labelled as one: some plats carry the name of a company that once held the
  land ("PHILIPS ELECTRONICS NA CORP"), which is neither the applicant nor the owner today.
- sponsor_name: null. The layer names no applicant.
- lifecycle_state: `FIRST_Status` through `status_map.yaml` (Active -> filed; Approved and Approved:
  Conditionally -> permitted), the most advanced among a site's applications. The caveats there say
  what each value proves and what it does not.
- capacity_mw: null. The layer gives floor area, not power; `FIRST_Overall_SQ_FT` stays in `raw` and is
  never converted into MW.
- location: a point inside the site's land (its centroid, or shapely's representative point when the
  centroid falls outside), WGS84, 6 decimals, written to `raw` as `Latitude`/`Longitude` so the loader
  places it `exact`. Parcel outlines are not stored. county: from that point (`county_for_point`, the
  spelling Virginia DEQ and ICIS-Air share).
- cross_refs: `icis_air:<id>` when exactly one Virginia DEQ data-centre air site lies inside this site's
  land (and inside no other site's), taken from that DEQ site's `PLA_ICIS_ID`. Virginia DEQ and EPA
  ICIS-Air cite the same id, so the resolver's deterministic shared-registry pass (`pipeline/resolve.py`
  D3) folds this site into the record already held for that facility instead of publishing it twice:
  a real-world site is one record. A site holding two or more DEQ facilities, or one without an ICIS id,
  carries no link; `held_link_basis` in `raw` says which, and those are reported, not merged.
  Only DEQ's own facility point decides. ICIS-Air's FRS geocode for the same Loudoun facility lies a
  median 190 m from DEQ's point (41 facilities with both, 2026-10-24; 5 % within 30 m), so an ICIS-Air
  point inside or beside a site's land proves nothing either way, and nearness is never a link: in
  these business parks the next lot can be another building or another operator. A DEQ point just
  outside a site's land is reported, never merged (data/sources.yaml lists the cases checked).

Personal data (CLAUDE.md, docs/13 §5.5): the pipeline layer has no owner column, and the field list
keeps it that way; `redact` and `parse` also drop any attribute outside `FIELDS`, so an owner, taxpayer
or editor column added upstream never reaches the snapshot or a row. A subdivision is a recorded plat
name, and plats can be named after the people who platted them: `FIRST_Subdivision` is kept only when
`services.personal_names` proves it is an organisation or place by its structure (a digit, a business,
industry or place word). Every other subdivision is nulled before the snapshot is stored (`fetch` and
`redact`, `withhold_subdivision`), with a `subdivision_withheld` flag on the feature so the count
survives; `parse` applies the same rule again (12 of 52 distinct names, on 17 of the 85 applications,
on 2026-10-24).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import pathlib
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import quote, urlencode, urlsplit

import pandas as pd
from shapely.geometry import Point, Polygon  # type: ignore[import-untyped,import-not-found,unused-ignore]
from shapely.ops import unary_union  # type: ignore[import-untyped,import-not-found,unused-ignore]

from pipeline.connectors.base import BlockedError, ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name
from pipeline.connectors.us_va_deq_data_center_air_sites import connector as deq
from services.personal_names import classify

ITEM_ID = "2a084dcc039249f286685205f1f4b33b"
ITEM_URL = f"https://www.arcgis.com/sharing/rest/content/items/{ITEM_ID}"
ITEM_PAGE = f"https://www.arcgis.com/home/item.html?id={ITEM_ID}"
SERVICE_URL = "https://services1.arcgis.com/MxjRokvPm7bjslyR/arcgis/rest/services/Pipeline_Data_Center_Areas/FeatureServer"
LAYER_URL = f"{SERVICE_URL}/1"
QUERY_URL = f"{LAYER_URL}/query"
#: The only attributes ever requested or kept. `OBJECTID` pages the query and is dropped from rows.
FIELDS = (
    "OBJECTID",
    "Application",
    "FIRST_Status",
    "FIRST_Subdivision",
    "FIRST_Zoning_Case_Num",
    "FIRST_Overall_SQ_FT",
    "SUM_Pipeline_Acres",
)
ROW_FIELDS = FIELDS[1:]
REQUIRED_ATTRIBUTES = ("Application", "FIRST_Status")
PAGE_SIZE = 1000  # the layer's maxRecordCount is 2000
MAX_PAGES = 10
ARCGIS_RPS = 0.5
#: sha256 of `licence_text` over the item's `licenseInfo`, read 2026-10-23 (data/sources.yaml, docs/13 §6).
LICENCE_SHA256 = "f85e0490a3e2513c93f2896c248b9a7dfbd0482e31a61b6fa8e19fe79ba930a7"
LICENCE_REVIEWED = "2026-10-23"
#: Two applications are one site when their land overlaps by at least this share of the smaller one.
SAME_LAND_SHARE = 0.5
#: Groups a reviewer checked and accepted as one site although not all their land is shared (`chained`):
#: each entry is the set of the group's application numbers. Empty: no group has needed it.
ACCEPTED_CHAINS: frozenset[frozenset[str]] = frozenset()
COORD_DECIMALS = 6
#: Virginia DEQ's air-site layer, read only to link sites to the facility records already held.
DEQ_SOURCE_ID = deq.Connector.source_id
DEQ_QUERY_URL = deq.QUERY_URL
DEQ_FIELDS = (
    "PLA_REG_NUM",
    "PLA_ICIS_ID",
    "AIR_OP_STATUS",
    "PLA_DATA_CENTER_YN",
    "PLA_PRINCIPAL_PRODUCT",
)
DEQ_PAGE_SIZE = 1000  # DEQ's layer maxRecordCount
#: Application number: prefix, optional dash, year, dash, sequence ("EPLAN-2023-0083", "TLREZN2024-0001").
_APP_RE = re.compile(r"^([A-Z]+)-?(\d{4})-(\d+)$")
_TAG_RE = re.compile(r"<[^>]+>")
#: Lifecycle order for a site holding several applications (status_map.yaml caveats).
STATE_ORDER = ("unknown", "filed", "permitted")
#: Set on a pipeline feature (beside `attributes`) whose subdivision was withheld before storage.
WITHHELD_FLAG = "subdivision_withheld"


def licence_text(markup: str | None) -> str:
    """The item's `licenseInfo` as plain text: tags dropped, entities decoded, whitespace collapsed."""
    text = html.unescape(_TAG_RE.sub(" ", markup or ""))
    return " ".join(text.split())


def licence_sha256(markup: str | None) -> str:
    return hashlib.sha256(licence_text(markup).encode("utf-8")).hexdigest()


def canon_application(value: Any) -> str:
    """`Application` as the county writes it, trimmed, spaces collapsed, upper-cased."""
    if value is None:
        return ""
    return " ".join(str(value).split()).upper()


def application_order(app: str) -> tuple[int, str, int, str]:
    """Founding order: earliest year, then prefix, then sequence; an unreadable number sorts last."""
    m = _APP_RE.match(app)
    if m is None:
        return (9999, app, 0, app)
    return (int(m.group(2)), m.group(1), int(m.group(3)), app)


def keep_subdivision(value: Any) -> str | None:
    """The subdivision when its structure proves an organisation or place, else None (module docstring)."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    verdict = classify(text)
    return text if (not verdict.personal and verdict.basis == "structure") else None


def withhold_subdivision(feat: dict[str, Any]) -> bool:
    """Null, in place, a subdivision `keep_subdivision` does not keep and flag the feature
    (`WITHHELD_FLAG`, outside `attributes`); True when the feature changed. Runs before the snapshot
    is stored, so a plat named after a person never reaches storage."""
    attrs = feat.get("attributes")
    if not isinstance(attrs, dict):
        return False
    value = attrs.get("FIRST_Subdivision")
    if not str(value or "").strip() or keep_subdivision(value) is not None:
        return False
    attrs["FIRST_Subdivision"] = None
    feat[WITHHELD_FLAG] = True
    return True


def record_url(published: Sequence[str]) -> str:
    """ArcGIS HTML view of exactly the site's applications (the nearest addressable page). `published`
    are the application numbers exactly as the layer stores them ("LEGI-2024-0047 ", with its trailing
    space): the hosted layer refuses TRIM in `where` (HTTP 200 with "'where' parameter is invalid",
    measured 2026-10-24), so the clause matches the stored text and uses no SQL function at all."""
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in published)
    where = f"Application IN ({quoted})"
    params = {"where": where, "outFields": ",".join(ROW_FIELDS), "f": "html"}
    return f"{QUERY_URL}?" + urlencode(params, quote_via=quote)


def site_name(applications: Sequence[str], subdivision: str | None) -> str:
    """The record name: the application number(s) first, then the plat, labelled as a subdivision so a
    company-named plat never reads as the applicant (module docstring, name_canonical)."""
    if len(applications) == 1:
        head = f"{applications[0]} data center application"
    else:
        head = f"{', '.join(applications[:-1])} and {applications[-1]} data center applications"
    return f"{head}, {subdivision} subdivision" if subdivision else head


def _signed_area(ring: Sequence[Sequence[float]]) -> float:
    return 0.5 * sum(
        float(x1) * float(y2) - float(x2) * float(y1)
        for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True)
    )


def polygon_from_rings(rings: Any) -> Any:
    """An Esri polygon (outer rings clockwise, holes counter-clockwise) as one shapely geometry."""
    if not isinstance(rings, list) or not rings:
        return None
    outers: list[Any] = []
    holes: list[Any] = []
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            continue
        coords = [(float(p[0]), float(p[1])) for p in ring]
        shape = Polygon(coords)
        if not shape.is_valid:
            shape = shape.buffer(0)
        (outers if _signed_area(coords) < 0 else holes).append(shape)
    if not outers:
        return None
    geom = unary_union(outers)
    if holes:
        geom = geom.difference(unary_union(holes))
    return None if geom.is_empty else geom


def inside_point(geom: Any) -> tuple[float, float]:
    """(lon, lat) of a point inside `geom`: its centroid, else shapely's representative point."""
    pt = geom.centroid
    if not geom.covers(pt):
        pt = geom.representative_point()
    return round(float(pt.x), COORD_DECIMALS), round(float(pt.y), COORD_DECIMALS)


@dataclass
class Application:
    number: str
    attributes: dict[str, Any]
    geom: Any
    subdivision_withheld: bool = False
    #: the number exactly as the layer stores it, once per spelling (`record_url`)
    published: set[str] = field(default_factory=set)


@dataclass
class Site:
    applications: list[Application]
    geom: Any
    held: list[dict[str, Any]] = field(default_factory=list)

    @property
    def numbers(self) -> list[str]:
        return [a.number for a in self.applications]

    @property
    def key(self) -> str:
        return self.applications[0].number


def shares_land(a: Any, b: Any, share: float = SAME_LAND_SHARE) -> bool:
    """Two applications' land overlaps by at least `share` of the smaller polygon."""
    if not a.intersects(b):
        return False
    smaller = min(float(a.area), float(b.area))
    return smaller > 0 and float(a.intersection(b).area) >= share * smaller


def group_same_land(apps: Sequence[Application], share: float = SAME_LAND_SHARE) -> list[Site]:
    """Applications whose land overlaps by `share` of the smaller polygon are one site (union-find,
    so transitive; `chained` finds a group joined only through a third application). Members and
    sites come out in founding order, so the result is deterministic."""
    ordered = sorted(apps, key=lambda a: application_order(a.number))
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if shares_land(ordered[i].geom, ordered[j].geom, share):
                parent[find(j)] = find(i)
    groups: dict[int, list[Application]] = {}
    for i, app in enumerate(ordered):
        groups.setdefault(find(i), []).append(app)
    sites = [Site(members, unary_union([m.geom for m in members])) for members in groups.values()]
    return sorted(sites, key=lambda s: application_order(s.key))


def chained(site: Site, share: float = SAME_LAND_SHARE) -> bool:
    """True when two of a site's applications do not share land with each other, so the transitive
    grouping joined them only through a third (a rezoning over several separate site plans), and no
    reviewer has accepted the group (`ACCEPTED_CHAINS`)."""
    apps = site.applications
    if frozenset(site.numbers) in ACCEPTED_CHAINS:
        return False
    return any(
        not shares_land(apps[i].geom, apps[j].geom, share)
        for i in range(len(apps))
        for j in range(i + 1, len(apps))
    )


def _held_sites(features: Iterable[Any]) -> list[dict[str, Any]]:
    """Virginia DEQ data-centre air sites with a point, under that connector's own selection rule."""
    out: list[dict[str, Any]] = []
    for feat in features:
        attrs = dict((feat or {}).get("attributes") or {})
        geom = (feat or {}).get("geometry") or {}
        if deq.select_basis(attrs) is None or geom.get("x") is None or geom.get("y") is None:
            continue
        reg = attrs.get("PLA_REG_NUM")
        reg_text = str(int(reg)) if isinstance(reg, float) and reg.is_integer() else str(reg or "").strip()
        out.append(
            {
                "PLA_REG_NUM": reg_text,
                "PLA_ICIS_ID": str(attrs.get("PLA_ICIS_ID") or "").strip() or None,
                "AIR_OP_STATUS": attrs.get("AIR_OP_STATUS"),
                "point": Point(float(geom["x"]), float(geom["y"])),
            }
        )
    return out


def link_held(sites: Sequence[Site], held: Sequence[dict[str, Any]]) -> None:
    """Attach each held DEQ air site to the one site whose land covers its point (none when two do)."""
    for h in held:
        covering = [s for s in sites if s.geom.covers(h["point"])]
        if len(covering) == 1:
            covering[0].held.append(h)


def held_link(site: Site) -> tuple[str | None, str | None]:
    """(cross-reference, basis) for a site's held DEQ air sites (module docstring, `cross_refs`)."""
    if not site.held:
        return None, None
    ids = {h["PLA_ICIS_ID"] for h in site.held}
    if None in ids:
        return None, "deq_air_site_without_icis_id"
    if len(ids) > 1:
        return None, "several_deq_air_sites_inside"
    return f"icis_air:{next(iter(ids))}", "one_deq_air_site_inside"


def _clean_page(page: dict[str, Any], keep: Sequence[str]) -> dict[str, Any]:
    """A query page with only the requested attributes and the geometry: nothing else is stored."""
    feats = []
    for feat in page.get("features") or []:
        attrs = {k: v for k, v in dict((feat or {}).get("attributes") or {}).items() if k in keep}
        feats.append({"attributes": attrs, "geometry": (feat or {}).get("geometry")})
    return {"features": feats, "exceededTransferLimit": bool(page.get("exceededTransferLimit"))}


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.va.loudoun.data_centers"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True
    status_key: ClassVar[str] = "loudoun_dc_pipeline"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "state", "county")
    key_source_columns: ClassVar[tuple[str, ...]] = ("site_key", "applications", "Latitude", "Longitude")

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        before = self.http.requests_made
        for url in (ITEM_URL, DEQ_QUERY_URL):
            self.http.rate_limits.setdefault(urlsplit(url).netloc, ARCGIS_RPS)
        terms = self._read_terms()
        layer = self._read_layer()
        pages, status, headers = self._query(
            QUERY_URL,
            {
                "where": "1=1",
                "outFields": ",".join(FIELDS),
                "returnGeometry": "true",
                "outSR": "4326",
                "geometryPrecision": "7",
                "orderByFields": "OBJECTID",
                "resultRecordCount": str(PAGE_SIZE),
                "f": "json",
            },
            FIELDS,
        )
        for page in pages:
            for feat in page["features"]:
                withhold_subdivision(feat)
        held_pages, _, _ = self._query(
            DEQ_QUERY_URL,
            {
                "where": deq.WHERE,
                "outFields": ",".join(DEQ_FIELDS),
                "returnGeometry": "true",
                "outSR": "4326",
                "orderByFields": "PLA_REG_NUM",
                "resultRecordCount": str(DEQ_PAGE_SIZE),
                "f": "json",
            },
            DEQ_FIELDS,
        )
        doc = {
            "layer": LAYER_URL,
            "terms": terms,
            "layer_info": layer,
            "pages": pages,
            "held": {
                "source_id": DEQ_SOURCE_ID,
                "url": DEQ_QUERY_URL,
                "where": deq.WHERE,
                "pages": held_pages,
            },
        }
        return RawSnapshot(
            content=json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            url=LAYER_URL,
            retrieved_at=self.now().astimezone(dt.UTC),
            http_status=status,
            ext="json",
            headers=headers,
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={
                "pages": len(pages),
                "held_pages": len(held_pages),
                "licence_sha256": terms["sha256"],
                "data_last_edit": layer.get("data_last_edit"),
            },
        )

    def _get_json(self, url: str, params: dict[str, str]) -> tuple[dict[str, Any], int, dict[str, str]]:
        r = self.http.get(url, params=params, honour_robots=self.honour_robots, timeout=120)
        if r.status_code != 200:
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ParseError(f"{url} did not return JSON: {r.content[:200]!r}") from e
        if not isinstance(body, dict):
            raise ParseError(f"{url} returned {type(body).__name__}, not an object")
        if "error" in body:
            # ArcGIS answers HTTP 200 with an error body (docs/02 §7): a renamed or removed layer or field.
            raise ConnectorError(f"{url} error: {str(body['error'])[:300]}")
        return body, r.status_code, dict(r.headers)

    def _read_terms(self) -> dict[str, Any]:
        item, _, _ = self._get_json(ITEM_URL, {"f": "json"})
        if item.get("url") != SERVICE_URL:
            raise BlockedError(
                f"ArcGIS item {ITEM_ID} now describes {item.get('url')!r}, not the reviewed layer; a "
                "reviewer re-reads the item and its licence before anything is fetched"
            )
        digest = licence_sha256(item.get("licenseInfo"))
        if digest != LICENCE_SHA256:
            raise BlockedError(
                f"the licence on ArcGIS item {ITEM_ID} changed since it was reviewed on {LICENCE_REVIEWED} "
                f"(sha256 {digest}, reviewed {LICENCE_SHA256}); nothing is fetched until a reviewer re-reads "
                "it, updates data/sources.yaml and docs/13 §6 and moves LICENCE_SHA256"
            )
        # Only the licence is kept: the item's other fields name the publishing account.
        return {"item": ITEM_PAGE, "licence": licence_text(item.get("licenseInfo")), "sha256": digest}

    def _read_layer(self) -> dict[str, Any]:
        layer, _, _ = self._get_json(LAYER_URL, {"f": "json"})
        names = {str(f.get("name")) for f in layer.get("fields") or [] if isinstance(f, dict)}
        missing = [f for f in FIELDS if f not in names]
        if missing:
            raise ConnectorError(f"Loudoun pipeline layer lacks {missing}: the layer was rebuilt or replaced")
        edit = (layer.get("editingInfo") or {}).get("dataLastEditDate")
        last_edit = (
            dt.datetime.fromtimestamp(int(edit) / 1000, dt.UTC).isoformat(timespec="seconds")
            if isinstance(edit, (int, float))
            else None
        )
        return {"name": layer.get("name"), "data_last_edit": last_edit}

    def _query(
        self, url: str, params: dict[str, str], keep: Sequence[str]
    ) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
        pages: list[dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page, status, headers = self._get_json(url, {**params, "resultOffset": str(offset)})
            cleaned = _clean_page(page, keep)
            pages.append(cleaned)
            n = len(cleaned["features"])
            if not cleaned["exceededTransferLimit"] or n == 0:
                return pages, status, headers
            offset += n
        raise ConnectorError(f"{url} did not finish within {MAX_PAGES} pages")

    def redact(self, content: bytes) -> bytes:
        """Before the snapshot is stored: drop any attribute outside `FIELDS` from the layer pages (an
        owner, taxpayer or editor column added upstream) and withhold a subdivision that may name a
        person (`withhold_subdivision`). `fetch` already does both; this catches whatever reaches the
        runner another way. Bytes are untouched when there is nothing to strip."""
        try:
            doc = json.loads(content)
        except json.JSONDecodeError:
            return content
        stripped = False
        for page in (doc.get("pages") if isinstance(doc, dict) else None) or []:
            for feat in page.get("features") or []:
                attrs = feat.get("attributes") or {}
                for k in [k for k in attrs if k not in FIELDS]:
                    attrs.pop(k)
                    stripped = True
                stripped = withhold_subdivision(feat) or stripped
        if not stripped:
            return content
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            doc = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"Loudoun pipeline snapshot is not JSON: {e}") from e
        pages = doc.get("pages") if isinstance(doc, dict) else None
        held_doc = doc.get("held") if isinstance(doc, dict) else None
        if not isinstance(pages, list) or not pages:
            raise ParseError("Loudoun pipeline snapshot has no pages")
        if not isinstance(held_doc, dict) or not isinstance(held_doc.get("pages"), list):
            raise ParseError("Loudoun pipeline snapshot has no held-site pages")
        apps: dict[str, Application] = {}
        skipped = {"no_application": 0, "no_geometry": 0}
        for page in pages:
            if "error" in page:
                raise ParseError(f"Loudoun pipeline page carries an error: {str(page['error'])[:300]}")
            feats = page.get("features")
            if not isinstance(feats, list):
                raise ParseError("Loudoun pipeline page without features (layout changed?)")
            for feat in feats:
                attrs = {k: v for k, v in dict(feat.get("attributes") or {}).items() if k in ROW_FIELDS}
                missing = [c for c in REQUIRED_ATTRIBUTES if c not in attrs]
                if missing:
                    raise ParseError(f"Loudoun pipeline row lacks {missing}: {sorted(attrs)}")
                number = canon_application(attrs.get("Application"))
                if not number:
                    skipped["no_application"] += 1
                    continue
                geom = polygon_from_rings((feat.get("geometry") or {}).get("rings"))
                if geom is None:
                    skipped["no_geometry"] += 1
                    continue
                # withheld before storage (the flag), or by the same rule now (defence in depth)
                withheld = bool(feat.get(WITHHELD_FLAG)) or (
                    bool(str(attrs.get("FIRST_Subdivision") or "").strip())
                    and keep_subdivision(attrs.get("FIRST_Subdivision")) is None
                )
                published = str(attrs.get("Application"))
                if number in apps:  # the same application twice: one application, its land joined
                    apps[number].geom = unary_union([apps[number].geom, geom])
                    apps[number].subdivision_withheld |= withheld
                    apps[number].published.add(published)
                    continue
                apps[number] = Application(number, attrs, geom, withheld, {published})
        sites = group_same_land(list(apps.values()))
        chains = [s.numbers for s in sites if chained(s)]
        if chains:
            raise ParseError(
                f"applications grouped into one site through land they do not all share: {chains}; a "
                "reviewer splits the group or records it in ACCEPTED_CHAINS before the run is loaded"
            )
        held: list[dict[str, Any]] = []
        for page in held_doc["pages"]:
            if not isinstance(page, dict) or not isinstance(page.get("features"), list):
                raise ParseError("held-site page without features")
            held.extend(_held_sites(page["features"]))
        link_held(sites, held)
        rows = [self._row(site) for site in sites]
        raw.meta.update(
            {
                "applications": len(apps),
                "sites": len(sites),
                "largest_site": max((len(s.applications) for s in sites), default=0),
                "skipped": skipped,
                "held_air_sites_read": len(held),
                "held_linked": sum(1 for r in rows if r["held_link"]),
                "held_unlinked": sum(1 for r in rows if r["held_air_sites"] and not r["held_link"]),
            }
        )
        return rows

    @staticmethod
    def _row(site: Site) -> dict[str, Any]:
        withheld = 0
        applications: list[dict[str, Any]] = []
        for app in site.applications:
            a = dict(app.attributes)
            a["Application"] = app.number
            withheld += int(app.subdivision_withheld)
            a["FIRST_Subdivision"] = (
                None if app.subdivision_withheld else keep_subdivision(a.get("FIRST_Subdivision"))
            )
            applications.append({k: a.get(k) for k in ROW_FIELDS})
        lon, lat = inside_point(site.geom)
        link, basis = held_link(site)
        return {
            "site_key": site.key,
            "applications": applications,
            "published_numbers": sorted(set().union(*(app.published for app in site.applications))),
            "subdivisions_withheld": withheld,
            "Latitude": lat,
            "Longitude": lon,
            "held_air_sites": [
                {k: h[k] for k in ("PLA_REG_NUM", "PLA_ICIS_ID", "AIR_OP_STATUS")}
                for h in sorted(site.held, key=lambda h: h["PLA_REG_NUM"])
            ],
            "held_link": link,
            "held_link_basis": basis,
        }

    # ------------------------------------------------------------------ normalize
    def _state(self, row: dict[str, Any]) -> tuple[str, str, Any]:
        """(lifecycle state, rule, deciding status_raw): the most advanced of the site's applications."""
        best: tuple[int, str, str, Any] | None = None
        unmapped: str | None = None
        for app in row.get("applications") or []:
            status = app.get("FIRST_Status")
            state, rule = harmonise_status(self.status_key, {"status_raw": status}, self.status_map)
            if state == "unknown":
                unmapped = unmapped or rule
            rank = STATE_ORDER.index(state) if state in STATE_ORDER else 0
            if best is None or rank > best[0]:
                best = (rank, state, rule, status)
        if best is None:
            return "unknown", f"{self.status_key}.blank", None
        # A value the map does not know stays visible to the vocabulary-drift check even when another
        # application on the same land decides the state.
        return best[1], unmapped or best[2], best[3]

    def restate_status(self, df: pd.DataFrame) -> pd.DataFrame | None:
        """Each stored row's state recomputed from its own `raw` under the current status map."""
        states: list[str] = []
        rules: list[str] = []
        for payload in df["raw"].tolist():
            try:
                row = json.loads(str(payload)) if payload is not None else {}
            except (TypeError, ValueError):
                row = {}
            state, rule, _ = self._state(row if isinstance(row, dict) else {})
            states.append(state)
            rules.append(rule)
        return pd.DataFrame({"lifecycle_state": states, "status_rule": rules}, index=df.index)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        states = [self._state(r) for r in rows]
        counties = [deq.county_for_point(r.get("Longitude"), r.get("Latitude")) for r in rows]
        numbers = [[a["Application"] for a in r["applications"]] for r in rows]
        subdivisions = [
            next((a["FIRST_Subdivision"] for a in apps if a["FIRST_Subdivision"]), None)
            for apps in (r["applications"] for r in rows)
        ]
        names = [site_name(n, s) for n, s in zip(numbers, subdivisions, strict=True)]
        df = pd.DataFrame(
            {
                "source_record_id": [r["site_key"] for r in rows],
                "source_url": [
                    record_url(r.get("published_numbers") or n) for r, n in zip(rows, numbers, strict=True)
                ],
                "kind": "load",
                "name_canonical": names,
                "name_norm": [norm_name(n) for n in names],
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": "VA",
                "county": [name for _, name in counties],
                "county_norm": [norm_county(name) for _, name in counties],
                "lifecycle_state": [s for s, _, _ in states],
                "status_raw": [raw_status for _, _, raw_status in states],
                "status_rule": [rule for _, rule, _ in states],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": pd.NaT,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [r["held_link"] or "" for r in rows],
            },
            index=range(len(rows)),
        )
        return self.finalize(df, rows, raw)
