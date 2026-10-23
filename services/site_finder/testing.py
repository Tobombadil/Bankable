"""A small, exactly known world for the site finder's tests (the job, the API and the page), built
offline: four lines, three substations, plants, requests, a one-state FEMA flood build, a TIGER-shaped
county file, a stand-in prospect sampler and a land parcels build of the synthetic county 48999
(`write_parcels`, from `pipeline/context/fixtures/txgio_parcels/synthetic_county.geojson`, laid out as a
pull would leave it; no owner value reaches the build's output).

Geometry (all on the WGS84 mean sphere, `geometry.M_PER_DEG` metres per degree of latitude):

- **Line A**, 138 kV, east-west along latitude 31.0 from -98.0 to -97.975 (1.48 mi): three segments of
  about 0.49 mi. Substation **S1** sits exactly 1 mile due north of A's western end, so segment 0 is
  1.000 mi from it. The SFHA fixture is everything north of latitude 31.0 near A, so each of A's bands
  is exactly half floodplain at every half-width. Retired plant **P1** (500 MW) is 2 miles due south of
  A's middle; retired plant **P2** is 7 miles away (outside the 5-mile context radius).
- **Line B**, 345 kV, along latitude 30.0 from -97.0 to -96.99 (0.60 mi, one segment). Substation **S2**
  is 3 miles north of its western end. No SFHA near it; a shaded X piece touches its 1-mile band. A
  data-centre request (**L1**, exact location) is 3 miles north and a queue request (200 MW) naming
  grid connection point **POI** 1 mile south. `queue_request` adds more requests (the API tests put
  one naming POI 200 miles away, which keeps POI visible while the near one is hidden).
- **Line C**, voltage not stated, along latitude 32.0 from -99.0 to -98.99: outside the flood build's
  area of interest (flood data unavailable, not computed) and outside the county file (the vendored
  1:20m counties name it). Substation **S3** is 0.5 miles north. The sampler reports it mostly on GAP
  1-2 protected land.
- **Line D**, 34.5 kV: under the 69 kV floor, never a candidate.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from services.db.models import Asset, InterconnectionPoint, Licence, Location, Proposal, Source
from services.ids import public_id, slugify
from services.site_finder.geometry import M_PER_DEG, METRES_PER_MILE

UTC = dt.UTC
RETRIEVED = dt.datetime(2026, 10, 3, 15, 21, 49, tzinfo=UTC)
#: One mile of latitude, degrees.
MILE_LAT = METRES_PER_MILE / M_PER_DEG
FLOOD_VERSION = "20261023T0000Z"


def _licence(db: Session, id_: str) -> Licence:
    lic = Licence(
        id=id_,
        name=f"{id_} terms",
        reuse_class="open",
        attribution_required=True,
        attribution_text="© OpenStreetMap contributors" if "osm" in id_ else None,
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        share_alike="osm" in id_,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=RETRIEVED,
        classified_by="test",
    )
    db.add(lic)
    db.flush()
    return lic


def _source(db: Session, id_: str, lic: Licence, name: str) -> Source:
    src = Source(
        id=id_,
        name=name,
        jurisdiction="US",
        category="registry",
        operator=name,
        url="https://example.org/" + id_,
        access="bulk_file",
        cadence="monthly",
        licence_id=lic.id,
        publish_state="public",
        manifest_version="2026-10-23",
        manifest_hash="0" * 64,
    )
    db.add(src)
    db.flush()
    return src


def _asset(
    db: Session,
    src: Source,
    *,
    key: str,
    asset_type: str,
    name: str,
    geom: tuple[float, float],
    line: list[tuple[float, float]] | None = None,
    attributes: dict[str, Any] | None = None,
    status: str = "operating",
    capacity_mw: float | None = None,
) -> Asset:
    wkt = "MULTILINESTRING((" + ", ".join(f"{x} {y}" for x, y in line) + "))" if line else None
    a = Asset(
        public_id="",
        slug="",
        asset_type=asset_type,
        source_asset_id=key,
        name=name,
        status=status,
        technology="gas" if asset_type == "power_plant" else None,
        technologies={},
        capacity_mw=capacity_mw,
        geom=geom,
        geom_line=wkt,
        attributes=attributes or {},
        state_code="US-TX",
        country="US",
        source_id=src.id,
        source_url=f"https://www.openstreetmap.org/{key}" if src.id == "us.osm.power" else src.url,
        retrieved_at=RETRIEVED,
        licence_id=src.licence_id,
    )
    db.add(a)
    db.flush()
    a.public_id = public_id("asset", a.id)
    a.slug = slugify(name) + "-" + key.replace("/", "-")
    db.flush()
    return a


def _line_attrs(kv: float | None, osm_id: int, operator: str | None = None) -> dict[str, Any]:
    return {
        "voltage_kv": kv,
        "voltages_kv": [kv] if kv else [],
        "voltage_unknown": kv is None,
        "voltage_class": None,
        "operator": operator,
        "osm_type": "way",
        "osm_id": osm_id,
        "region": "US-TX",
        "states_crossed": ["US-TX"],
        "scope_regions": ["US-TX"],
        "coverage_regions": ["US-TX"],
        "underground": False,
    }


@dataclass
class World:
    line_a: Asset
    line_b: Asset
    line_c: Asset
    line_d: Asset
    s1: Asset
    s2: Asset
    s3: Asset
    p1: Asset
    p2: Asset
    load: Proposal
    poi_proposal: Proposal
    point: InterconnectionPoint
    data_root: pathlib.Path


def _proposal(
    db: Session,
    src: Source,
    *,
    suffix: str,
    kind: str,
    name: str,
    geom: tuple[float, float],
    capacity_mw: float,
    point: InterconnectionPoint | None = None,
) -> Proposal:
    loc = Location(
        kind="point",
        geom=geom,
        precision="exact",
        state_code="US-TX",
        country="US",
        source_id=src.id,
        source_url=src.url,
        retrieved_at=RETRIEVED,
        licence_id=src.licence_id,
    )
    db.add(loc)
    db.flush()
    p = Proposal(
        public_id=f"prop_sf{suffix}",
        slug=f"sf-{suffix}",
        kind=kind,
        name_canonical=name,
        technology="load" if kind == "load" else "solar",
        capacity_mw=capacity_mw,
        jurisdiction="US-TX",
        iso="ERCOT",
        location_id=loc.id,
        lifecycle_state="filed",
        publish_state="public",
        published_at=RETRIEVED,
        public_at=RETRIEVED,
        min_reuse_class="open",
        source_count=1,
        interconnection_point_id=point.id if point is not None else None,
    )
    db.add(p)
    db.flush()
    from services.db.models import ProposalSource

    db.add(
        ProposalSource(
            proposal_id=p.id,
            source_id=src.id,
            source_record_id=f"sf-{suffix}",
            source_url=src.url,
            retrieved_at=RETRIEVED,
            licence_id=src.licence_id,
            raw={},
            link_method="deterministic_key",
            active=True,
        )
    )
    db.flush()
    return p


def queue_request(
    db: Session,
    world: World,
    *,
    suffix: str,
    geom: tuple[float, float],
    capacity_mw: float,
    kind: str = "generation",
    point: InterconnectionPoint | None = None,
) -> Proposal:
    """One more exactly-located request from the fixture's queue source, naming `point` (POI unless a
    load): the tests hide it, delay it or withhold its location afterwards."""
    src = db.get(Source, "us.iso.ercot.gen_queue")
    assert src is not None  # noqa: S101 - seeded by `seed`
    named = point if point is not None else (None if kind == "load" else world.point)
    p = _proposal(
        db,
        src,
        suffix=suffix,
        kind=kind,
        name=f"Request {suffix}",
        geom=geom,
        capacity_mw=capacity_mw,
        point=named,
    )
    db.commit()
    return p


def line_asset(
    db: Session, *, key: str, name: str, kv: float | None, coords: list[tuple[float, float]]
) -> Asset:
    """One more OpenStreetMap line (the tests add long ones, and many short ones for paging)."""
    src = db.get(Source, "us.osm.power")
    assert src is not None  # noqa: S101 - seeded by `seed`
    mid = coords[len(coords) // 2]
    return _asset(
        db,
        src,
        key=key,
        asset_type="transmission_line",
        name=name,
        geom=mid,
        line=coords,
        attributes=_line_attrs(kv, int(key.rsplit("/", 1)[-1])),
    )


def substation_asset(db: Session, *, key: str, name: str, geom: tuple[float, float], kv: float) -> Asset:
    src = db.get(Source, "us.osm.power")
    assert src is not None  # noqa: S101 - seeded by `seed`
    attrs = {"voltage_kv": kv, "substation_type": "transmission", "osm_type": "node"}
    return _asset(db, src, key=key, asset_type="substation", name=name, geom=geom, attributes=attrs)


def plant_asset(
    db: Session, *, key: str, name: str, geom: tuple[float, float], mw: float, status: str = "retired"
) -> Asset:
    src = db.get(Source, "us.eia.860m")
    assert src is not None  # noqa: S101 - seeded by `seed`
    return _asset(
        db, src, key=key, asset_type="power_plant", name=name, geom=geom, status=status, capacity_mw=mw
    )


def seed(db: Session, data_root: pathlib.Path) -> World:
    """The store rows of the world (module docstring) and its files under `data_root`."""
    osm = _source(db, "us.osm.power", _licence(db, "osm-lic"), "OpenStreetMap power network")
    eia = _source(db, "us.eia.860m", _licence(db, "eia-lic"), "EIA-860M")
    queue = _source(db, "us.iso.ercot.gen_queue", _licence(db, "ercot-lic"), "ERCOT queue")
    line_a = _asset(
        db,
        osm,
        key="way/1",
        asset_type="transmission_line",
        name="Alpha 138 kV",
        geom=(-97.9875, 31.0),
        line=[(-98.0, 31.0), (-97.975, 31.0)],
        attributes=_line_attrs(138.0, 1, "Oncor"),
    )
    line_b = _asset(
        db,
        osm,
        key="way/2",
        asset_type="transmission_line",
        name="Bravo 345 kV",
        geom=(-96.995, 30.0),
        line=[(-97.0, 30.0), (-96.99, 30.0)],
        attributes=_line_attrs(345.0, 2, "CenterPoint"),
    )
    line_c = _asset(
        db,
        osm,
        key="way/3",
        asset_type="transmission_line",
        name="Charlie (voltage not stated)",
        geom=(-98.995, 32.0),
        line=[(-99.0, 32.0), (-98.99, 32.0)],
        attributes=_line_attrs(None, 3),
    )
    line_d = _asset(
        db,
        osm,
        key="way/4",
        asset_type="transmission_line",
        name="Delta 34.5 kV",
        geom=(-97.5, 30.5),
        line=[(-97.5, 30.5), (-97.49, 30.5)],
        attributes=_line_attrs(34.5, 4),
    )
    sub_attrs = {"voltage_kv": 138.0, "substation_type": "transmission", "osm_type": "node"}
    s1 = _asset(
        db,
        osm,
        key="node/11",
        asset_type="substation",
        name="Alpha North",
        geom=(-98.0, 31.0 + MILE_LAT),
        attributes={**sub_attrs, "osm_id": 11},
    )
    s2 = _asset(
        db,
        osm,
        key="node/12",
        asset_type="substation",
        name="Bravo Switch",
        geom=(-97.0, 30.0 + 3 * MILE_LAT),
        attributes={**sub_attrs, "voltage_kv": 345.0, "osm_id": 12},
    )
    s3 = _asset(
        db,
        osm,
        key="node/13",
        asset_type="substation",
        name="Charlie Tap",
        geom=(-99.0, 32.0 + 0.5 * MILE_LAT),
        attributes={**sub_attrs, "voltage_kv": None, "osm_id": 13},
    )
    p1 = _asset(
        db,
        eia,
        key="eia/1",
        asset_type="power_plant",
        name="Old Mill Station",
        geom=(-97.9875, 31.0 - 2 * MILE_LAT),
        status="retired",
        capacity_mw=500.0,
    )
    p2 = _asset(
        db,
        eia,
        key="eia/2",
        asset_type="power_plant",
        name="Far Plant",
        geom=(-97.9875, 31.0 - 7 * MILE_LAT),
        status="retiring",
        capacity_mw=300.0,
    )
    _asset(
        db,
        eia,
        key="eia/3",
        asset_type="power_plant",
        name="Running Plant",
        geom=(-97.98, 31.0 - MILE_LAT),
        status="operating",
        capacity_mw=100.0,
    )
    point = InterconnectionPoint(
        public_id="poi_sf1",
        operator="ERCOT",
        name_display="Bravo 345kV",
        name_key="bravo|345",
        key_rule="v1",
        voltage_kv=345,
        kind="substation",
        jurisdiction="US-TX",
        source_id=queue.id,
        source_url=queue.url,
        retrieved_at=RETRIEVED,
        licence_id=queue.licence_id,
    )
    db.add(point)
    db.flush()
    load = _proposal(
        db,
        queue,
        suffix="load",
        kind="load",
        name="Hyperscale Campus",
        geom=(-97.0, 30.0 + 3 * MILE_LAT),
        capacity_mw=300.0,
    )
    poi_prop = _proposal(
        db,
        queue,
        suffix="poi",
        kind="generation",
        name="Bravo Solar",
        geom=(-96.995, 30.0 - MILE_LAT),
        capacity_mw=200.0,
        point=point,
    )
    db.commit()
    write_flood(data_root)
    write_counties(data_root)
    write_parcels(data_root)
    return World(line_a, line_b, line_c, line_d, s1, s2, s3, p1, p2, load, poi_prop, point, data_root)


PARCELS_VERSION = "v1/20261001-20261023T0900Z"
#: The fixture county's box (`write_counties`): the parcel layer's coverage of 48999.
FIXTURE_COUNTY_BOX = [-98.5, 29.5, -96.5, 31.5]


def write_parcels(data_root: pathlib.Path, fixture_name: str = "synthetic_county.geojson") -> dict[str, Any]:
    """The TxGIO builder's synthetic county (`pipeline/context/fixtures/txgio_parcels/`) built and laid
    out as a server's pull leaves it, beside line A: Delta's 632-acre ranch south of the line, Alpha's
    three-parcel holding and Bravo's two north of it, and the rest (`pipeline/context/test_txgio_parcels.py`
    describes each). The county's box stands in for its outline, as a real county's parcels fill it.
    `fixture_name` builds another fixture of that folder as the same county instead (the stacked
    outlines, near -97.5, 30.5)."""
    from pipeline.context import txgio_parcels as tp

    fixture = pathlib.Path(tp.__file__).parent / "fixtures" / "txgio_parcels" / fixture_name
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    frame = tp.frame_from_features(doc["features"], "48999", "Fixture County")
    frame.source_url = (
        "https://data.geographic.texas.gov/1d33dafb/resources/stratmap26-landparcels_48999_lp.zip"
    )
    frame.retrieved_at = "2026-10-23T09:00:00Z"
    root = data_root / "normalized" / "context" / tp.SOURCE_ID
    summary = tp.build_county(frame, root / PARCELS_VERSION)
    summary["bbox"] = FIXTURE_COUNTY_BOX
    files = {
        kind: {**spec, "local": f"{PARCELS_VERSION}/{spec['path']}"}
        for kind, spec in summary["files"].items()
    }
    current = {
        "source_id": tp.SOURCE_ID,
        "version": PARCELS_VERSION,
        "vintage": {
            "collection_id": "1d33dafb-6e9c-40ac-887d-f4577909ed9b",
            "publication_date": "2026-10-01",
        },
        "counties": {"48999": {**summary, "version": PARCELS_VERSION, "files": files}},
        "group_distance_m": tp.GROUP_DISTANCE_M,
    }
    (root / tp.CURRENT_NAME).write_text(json.dumps(current), encoding="utf-8")
    return current


def write_flood(data_root: pathlib.Path) -> dict[str, Any]:
    """A one-state FEMA build in the published layout, mirrored under `data_root`."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    from pipeline.context.fema_nfhl import AOI_SCHEMA, COVERAGE_SCHEMA, ZONE_SCHEMA, write_geoparquet

    prefix = f"data/normalized/context/us.fema.nfhl/{FLOOD_VERSION}"
    out = data_root / "normalized" / "context" / "us.fema.nfhl" / FLOOD_VERSION
    prov = {
        "nfhl_product": "NFHL_48_20261013",
        "source_id": "us.fema.nfhl",
        "source_url": "https://hazards.fema.gov/nfhlv2/output/State/NFHL_48_20261013.zip",
        "retrieved_at": "2026-10-23T03:00:00Z",
        "licence": "US Government work",
        "raw": "{}",
    }
    area = shapely.box(-98.3, 29.8, -96.7, 31.3)
    write_geoparquet(
        out / "aoi.parquet",
        [
            {
                "state": "TX",
                "buffer_miles": 2.0,
                "margin_m": 200.0,
                "min_kv": 69.0,
                "line_count": 2,
                "area_m2": 1.0,
                "source_id": "us.osm.power",
                "source_url": "https://tiles.example/grid",
                "retrieved_at": "2026-10-03T15:21:49Z",
                "licence": "ODbL",
            }
        ],
        [area],
        AOI_SCHEMA,
    )
    piece = {
        "zone": "AE",
        "zone_subtype": None,
        "sfha_tf": "T",
        "fld_ar_id": "1",
        "dfirm_id": "48X",
        "version_id": "1",
        "firm_panel": "48453C0100K",
        "panel_effective_date": "2020-01-01",
        "state": "TX",
        "area_m2": 1.0,
        **prov,
    }
    write_geoparquet(
        out / "sfha" / "TX.parquet",
        [{**piece, "piece_id": "TX:1:a:0", "category": "sfha", "sfha": True}],
        [shapely.box(-98.1, 31.0, -97.9, 31.2)],
        ZONE_SCHEMA,
    )
    write_geoparquet(
        out / "zones_other" / "TX.parquet",
        [
            {
                **piece,
                "piece_id": "TX:2:b:0",
                "category": "shaded_x",
                "sfha": False,
                "zone": "X",
                "zone_subtype": "0.2 PCT ANNUAL CHANCE FLOOD HAZARD",
                "sfha_tf": "F",
            }
        ],
        [shapely.box(-96.996, 29.99, -96.994, 29.995)],
        ZONE_SCHEMA,
    )
    write_geoparquet(
        out / "coverage" / "TX.parquet",
        [
            {
                "firm_panel": "48453C0100K",
                "panel_type": "Countywide, Panel Printed",
                "covers": True,
                "panel_effective_date": "2020-01-01",
                "dfirm_id": "48X",
                "version_id": "1",
                "state": "TX",
                "area_m2": 1.0,
                **prov,
            }
        ],
        [area],
        COVERAGE_SCHEMA,
    )
    files = {
        layer: {"key": f"{prefix}/{layer}/TX.parquet", "path": f"{layer}/TX.parquet"}
        for layer in ("sfha", "zones_other", "coverage")
    }
    manifest = {
        "source_id": "us.fema.nfhl",
        "version": FLOOD_VERSION,
        "built_at": "2026-10-23T03:00:00Z",
        "r2_prefix": prefix,
        "states": {
            "TX": {
                "state": "TX",
                "version": FLOOD_VERSION,
                "prefix": prefix,
                "product": {"name": "NFHL_48_20261013", "url": prov["source_url"]},
                "files": files,
                "aoi": {"key": f"{prefix}/aoi.parquet", "state_row": "TX"},
                "tolerance_m": 10.0,
            }
        },
    }
    (data_root / "normalized" / "context" / "us.fema.nfhl" / "latest.json").write_text(json.dumps(manifest))
    return manifest


def write_counties(data_root: pathlib.Path) -> None:
    """One TIGER-shaped county, "Fixture" (48999), over lines A and B but not C."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    from services.site_finder.counties import SCHEMA, SOURCE_ID, URL, output_path

    path = output_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "geoid": ["48999"],
            "name": ["Fixture"],
            "namelsad": ["Fixture County"],
            "state_fips": ["48"],
            "state_code": ["US-TX"],
            "geometry": [shapely.to_wkb(shapely.box(-98.5, 29.5, -96.5, 31.5))],
            "source_id": [SOURCE_ID],
            "source_url": [URL],
            "retrieved_at": ["2026-10-23T02:47:30Z"],
            "licence": ["US Government work, public domain (17 U.S.C. §105)"],
            "vintage": ["TIGER/Line Shapefiles 2026"],
        },
        schema=SCHEMA,
    )
    pq.write_table(table, path)


class FakeSampler:
    """`services.context_sample.Sampler`'s corridor answer, canned: west of -98.5 (line C) the band is
    60% GAP 1-2 protected land; elsewhere none. Solar GHI rises eastward so a band's mean is checkable."""

    directory = pathlib.Path("/nonexistent")

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> list[str]:
        return ["protected_lands", "solar_ghi"]

    def entry(self, layer_id: str) -> dict[str, Any] | None:
        if layer_id not in self.available():
            return None
        return {
            "provenance": {
                "source_id": "us.usgs.padus"
                if layer_id == "protected_lands"
                else "us.nrel.nsrdb.psm3_annual",
                "source_url": "https://example.org/" + layer_id,
                "retrieved_at": "2026-10-22T18:41:51Z",
                "licence": "Public domain",
                "attribution": "test credit",
            }
        }

    def sample_line(
        self, geometry: Any, buffer_mi: float, layers: list[str], *, spacing_m: float | None = None
    ) -> dict[str, Any]:
        self.calls += 1
        lon = float(geometry["coordinates"][0][0])
        hard = 0.6 if lon < -98.5 else 0.0
        out_layers: list[dict[str, Any]] = []
        for lid in layers:
            if lid == "protected_lands":
                out_layers.append(
                    {
                        "layer": lid,
                        "status": "ok",
                        "share": hard,
                        "hard_constraint_share": hard,
                        "coverage_share": 1.0,
                        "features": [{"unit_name": "Fixture Preserve", "share": hard, "gap_status": "1"}]
                        if hard
                        else [],
                    }
                )
            elif lid == "solar_ghi":
                ghi = round(5.0 + (lon + 100.0) * 0.1, 3)
                out_layers.append(
                    {
                        "layer": lid,
                        "status": "ok",
                        "summary": {"mean": ghi, "min": ghi, "max": ghi, "coverage_share": 1.0},
                    }
                )
            else:
                out_layers.append({"layer": lid, "status": "unavailable"})
        return {"buffer_mi": buffer_mi, "layers": out_layers}
