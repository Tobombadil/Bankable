"""The grid layers on the asset API (lane grid-osm, 2026-10-22): substations as map points with their
voltage, the voltage fields on line features and the asset page, `voltage_kv[gte]` with
`include_voltage_unknown`, `source=`, the map's line-source preference (OpenStreetMap over LBNL's
subset where OpenStreetMap covers the line's states, docs/25 §2.7), the credit the licence summary
carries, and substations in `nearby-grid`."""

from __future__ import annotations

import math
import pathlib
from typing import Any

import pytest
import yaml

from services.api import assets as assets_module
from services.api.conftest import make_asset, make_attribution_licence, make_open_licence, make_public_source
from tests.test_api_contract import assert_valid

_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
TEXAS_BBOX = "-107,25,-93,37"
OSM = "us.osm.power"
LBNL = "us.lbnl.ferc_hifld_transmission_lines"
OSM_CREDIT = "© OpenStreetMap contributors. Power lines, cables and substations filtered by Inframation"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _reset_asset_index_cache() -> None:
    assets_module._reset_asset_index_cache()


def _line(db, src, lic, sid: str, name: str, coords: list[tuple[float, float]], attributes: dict[str, Any]):
    asset = make_asset(
        db,
        src,
        lic,
        source_asset_id=sid,
        asset_type="transmission_line",
        name=name,
        technology=None,
        capacity_mw=None,
        geom=coords[0],
    )
    asset.geom_line = coords
    asset.attributes = attributes
    return asset


def _sub(db, src, lic, sid: str, name: str, point: tuple[float, float], attributes: dict[str, Any]):
    asset = make_asset(
        db,
        src,
        lic,
        source_asset_id=sid,
        asset_type="substation",
        name=name,
        technology=None,
        capacity_mw=None,
        geom=point,
    )
    asset.attributes = attributes
    return asset


@pytest.fixture()
def grid(db) -> dict[str, Any]:
    osm_lic = make_attribution_licence(db, "osm-lic")
    osm_lic.attribution_text = OSM_CREDIT
    lbnl_lic = make_attribution_licence(db, "lbnl-lic")
    lbnl_lic.attribution_text = "Yin, Nait Belaid & Heleno (2026), Lawrence Berkeley National Laboratory"
    plant_lic = make_open_licence(db)
    osm = make_public_source(db, osm_lic, OSM)
    lbnl = make_public_source(db, lbnl_lic, LBNL)
    plants = make_public_source(db, plant_lic, "us.eia.860m")
    tx = {
        "region": "US-TX",
        "states_crossed": ["US-TX"],
        "scope_regions": ["US-TX"],
        "coverage_regions": ["US-TX"],
    }
    out = {
        "osm_345": _line(
            db,
            osm,
            osm_lic,
            "way/1",
            "Limestone - Gibbons Creek 345kV",
            [(-96.2, 31.0), (-96.1, 30.7)],
            {**tx, "voltage_kv": 345.0, "voltages_kv": [345.0], "voltage_unknown": False, "power": "line"},
        ),
        "osm_unknown": _line(
            db,
            osm,
            osm_lic,
            "way/2",
            "Power line, voltage not stated (OSM way 2)",
            [(-97.0, 31.0), (-97.1, 31.2)],
            {**tx, "voltage_kv": None, "voltages_kv": [], "voltage_unknown": True, "power": "line"},
        ),
        "lbnl_tx": _line(
            db,
            lbnl,
            lbnl_lic,
            "100",
            "Limestone - Gibbons Creek 345 kV",
            [(-96.2, 31.0), (-96.1, 30.69)],
            {"voltage_kv": 345.0, "states_crossed": ["US-TX"], "owner": "Texas Municipal Power"},
        ),
        "lbnl_border": _line(
            db,
            lbnl,
            lbnl_lic,
            "101",
            "Valliant - Lydia 345 kV",
            [(-95.4, 33.9), (-95.0, 34.2)],
            {"voltage_kv": 345.0, "states_crossed": ["US-OK", "US-TX"]},
        ),
        "sub_138": _sub(
            db,
            osm,
            osm_lic,
            "node/10",
            "Gibbons Creek substation",
            (-96.11, 30.71),
            {
                "voltage_kv": 345.0,
                "voltages_kv": [345.0, 138.0],
                "voltage_unknown": False,
                "substation_type": "transmission",
                "region": "US-TX",
                "coverage_regions": ["US-TX"],
                "outline": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-96.111, 30.709], [-96.109, 30.709], [-96.109, 30.711], [-96.111, 30.709]]
                    ],
                },
            },
        ),
        "sub_unknown": _sub(
            db,
            osm,
            osm_lic,
            "way/11",
            "Substation (OSM way 11)",
            (-96.3, 30.8),
            {
                "voltage_kv": None,
                "voltages_kv": [],
                "voltage_unknown": True,
                "substation_type": None,
                "region": "US-TX",
                "coverage_regions": ["US-TX"],
            },
        ),
        "plant": make_asset(
            db, plants, plant_lic, source_asset_id="P1", name="Gibbons Creek Plant", geom=(-96.08, 30.72)
        ),
    }
    db.commit()
    return out


def _geo(client, **params: str) -> dict[str, Any]:
    resp = client.get("/v1/assets/geo", params={"bbox": TEXAS_BBOX, "zoom": "7", **params})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _names(body: dict[str, Any], kind: str) -> set[str]:
    return {
        f["properties"]["name"] for f in body["data"]["features"] if f["properties"]["feature_kind"] == kind
    }


def test_substations_are_map_points_with_voltage_and_subtype(client, grid, spec):
    body = _geo(client, asset_type="substation,power_plant")
    assert_valid(spec, "AssetGeoResponse", body)
    props = {f["properties"]["name"]: f["properties"] for f in body["data"]["features"]}
    sub = props["Gibbons Creek substation"]
    assert sub["asset_type"] == "substation" and sub["substation_type"] == "transmission"
    assert (sub["voltage_kv"], sub["voltages_kv"], sub["voltage_unknown"]) == (345.0, [345.0, 138.0], False)
    assert props["Substation (OSM way 11)"]["voltage_unknown"] is True
    plant = props["Gibbons Creek Plant"]  # a plant marker is unchanged: no voltage keys
    assert "voltage_kv" not in plant and "substation_type" not in plant
    credits = [s["attribution_text"] for s in body["licence_summary"]["sources"]]
    assert any("© OpenStreetMap contributors" in (c or "") for c in credits)


def test_the_map_draws_osm_where_it_covers_and_lbnl_elsewhere(client, grid, spec):
    body = _geo(client, asset_type="transmission_line")
    assert_valid(spec, "AssetGeoResponse", body)
    lines = _names(body, "asset_line")
    # The Texas-only LBNL line gives way to OpenStreetMap; the one crossing into Oklahoma (no OSM
    # extract there) is still drawn.
    assert lines == {
        "Limestone - Gibbons Creek 345kV",
        "Power line, voltage not stated (OSM way 2)",
        "Valliant - Lydia 345 kV",
    }
    assert body["data"]["totals"]["asset_type_counts"] == {"transmission_line": 3}
    line = next(f for f in body["data"]["features"] if f["properties"]["name"].endswith("345kV"))
    assert line["properties"]["attributes"]["voltages_kv"] == [345.0]
    assert line["properties"]["source"]["attribution_text"] == OSM_CREDIT


def test_a_border_way_in_a_texas_load_does_not_make_its_other_state_covered(client, grid, db):
    """Review 2026-10-22: a US-TX load holds the ways across the Red River, labelled Oklahoma by
    their geometry. Coverage is the load's (`coverage_regions`), not the rows' labels, so LBNL's
    lines wholly in Oklahoma keep being drawn. (`extract_region` is what the first build wrote; it no
    longer decides anything.)"""
    osm_src, osm_lic = grid["osm_345"].source, grid["osm_345"].licence
    lbnl_src, lbnl_lic = grid["lbnl_tx"].source, grid["lbnl_tx"].licence
    _line(
        db,
        osm_src,
        osm_lic,
        "way/3",
        "Red River crossing 345 kV line (OSM way 3)",
        [(-97.5, 33.6), (-97.5, 34.4)],
        {
            "region": "US-OK",
            "extract_region": "US-OK",
            "states_crossed": ["US-OK", "US-TX"],
            "scope_regions": ["US-OK", "US-TX"],
            "coverage_regions": ["US-TX"],
            "voltage_kv": 345.0,
        },
    )
    _line(
        db,
        lbnl_src,
        lbnl_lic,
        "102",
        "Ardmore - Lawton 138 kV",
        [(-97.1, 34.2), (-98.4, 34.6)],
        {"voltage_kv": 138.0, "states_crossed": ["US-OK"]},
    )
    db.commit()
    assets_module._reset_asset_index_cache()
    lines = _names(_geo(client, asset_type="transmission_line"), "asset_line")
    assert {"Red River crossing 345 kV line (OSM way 3)", "Ardmore - Lawton 138 kV"} <= lines
    assert "Limestone - Gibbons Creek 345 kV" not in lines  # Texas is covered


def test_naming_a_source_draws_its_lines_everywhere(client, grid):
    body = _geo(client, asset_type="transmission_line", source=LBNL)
    assert _names(body, "asset_line") == {"Limestone - Gibbons Creek 345 kV", "Valliant - Lydia 345 kV"}
    assert {s["source_id"] for s in body["licence_summary"]["sources"]} == {LBNL}
    both = _geo(client, asset_type="transmission_line", source=f"{OSM},{LBNL}")
    assert len(_names(both, "asset_line")) == 4


def test_a_source_none_of_whose_lines_are_drawn_is_not_credited(client, grid, db):
    grid["lbnl_border"].attributes = {"voltage_kv": 345.0, "states_crossed": ["US-TX"]}
    db.commit()
    assets_module._reset_asset_index_cache()
    body = _geo(client, asset_type="transmission_line")
    assert {s["source_id"] for s in body["licence_summary"]["sources"]} == {OSM}
    assert body["data"]["totals"]["records"] == 2


def test_voltage_floor_keeps_stated_voltages_and_unknown_only_on_request(client, grid, spec):
    floor = _geo(client, **{"voltage_kv[gte]": "69"})
    assert_valid(spec, "AssetGeoResponse", floor)
    assert _names(floor, "asset_line") == {"Limestone - Gibbons Creek 345kV", "Valliant - Lydia 345 kV"}
    assert _names(floor, "asset") == {"Gibbons Creek substation"}  # no plant: plants state no voltage
    with_unknown = _geo(client, **{"voltage_kv[gte]": "69", "include_voltage_unknown": "true"})
    assert "Power line, voltage not stated (OSM way 2)" in _names(with_unknown, "asset_line")
    assert "Substation (OSM way 11)" in _names(with_unknown, "asset")
    high = _geo(client, **{"voltage_kv[gte]": "500"})
    assert _names(high, "asset_line") == set() and _names(high, "asset") == set()
    for bad in ({"voltage_kv[gte]": "high"}, {"voltage_kv[gte]": "5000"}, {"include_voltage_unknown": "yes"}):
        resp = client.get("/v1/assets/geo", params={"bbox": TEXAS_BBOX, "zoom": "7", **bad})
        assert resp.status_code == 400, bad


def test_asset_page_serves_a_substation_with_its_voltage_and_outline(client, grid, spec):
    resp = client.get(f"/v1/assets/{grid['sub_138'].public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetDetailResponse", body)
    data = body["data"]
    assert data["asset_type"] == "substation"
    assert (data["voltage_kv"], data["voltages_kv"], data["voltage_unknown"]) == (
        345.0,
        [345.0, 138.0],
        False,
    )
    assert data["attributes"]["outline"]["type"] == "Polygon"
    assert data["geometry"] == {"type": "Point", "coordinates": [-96.11, 30.71]}
    assert body["licence_summary"]["sources"][0]["attribution_text"].startswith(
        "© OpenStreetMap contributors"
    )
    line = client.get(f"/v1/assets/{grid['lbnl_tx'].public_id}").json()["data"]
    assert (line["voltage_kv"], line["voltages_kv"]) == (345.0, [345.0])  # LBNL lines stay on their pages
    plant = client.get(f"/v1/assets/{grid['plant'].public_id}").json()["data"]
    assert "voltage_kv" not in plant


def test_nearby_grid_lists_substations_and_does_not_double_list_a_superseded_line(client, grid, spec):
    resp = client.get(f"/v1/assets/{grid['plant'].public_id}/nearby-grid", params={"radius_km": "30"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "NearbyGridResponse", body)
    data = body["data"]
    assert [s["name"] for s in data["substations"]] == ["Gibbons Creek substation", "Substation (OSM way 11)"]
    first = data["substations"][0]
    assert (
        first["voltage_kv"] == 345.0
        and first["substation_type"] == "transmission"
        and first["distance_km"] < 5
    )
    assert [ln["name"] for ln in data["transmission_lines"]] == ["Limestone - Gibbons Creek 345kV"]
    own = client.get(f"/v1/assets/{grid['sub_138'].public_id}/nearby-grid").json()["data"]
    assert [s["name"] for s in own["substations"]] == ["Substation (OSM way 11)"]


def test_nearby_grid_finds_a_substation_due_east_at_the_edge_of_the_radius(client, grid, db):
    """Review 2026-10-22: the candidate box was padded by `radius_km / 100` degrees both ways, which
    at 30.7 N is 28.7 km east-west for a 30 km radius, so a substation 29.5 km due east was dropped
    before the distance test. The box now follows the sphere (`_padded_bbox`)."""
    plant_lon, plant_lat = grid["plant"].geom
    km_per_deg_lon = assets_module.EARTH_RADIUS_KM * math.radians(1.0) * math.cos(math.radians(plant_lat))
    east = _sub(
        db,
        grid["sub_138"].source,
        grid["sub_138"].licence,
        "node/12",
        "Due east substation",
        (plant_lon + 29.5 / km_per_deg_lon, plant_lat),
        {"voltage_kv": 138.0, "voltages_kv": [138.0], "voltage_unknown": False, "region": "US-TX"},
    )
    db.commit()
    assets_module._reset_asset_index_cache()
    distance = assets_module._haversine_km(plant_lon, plant_lat, *east.geom)
    assert 29.0 < distance < 30.0
    resp = client.get(f"/v1/assets/{grid['plant'].public_id}/nearby-grid", params={"radius_km": "30"})
    near = {s["name"]: s["distance_km"] for s in resp.json()["data"]["substations"]}
    assert near["Due east substation"] == pytest.approx(distance, abs=0.01)


@pytest.mark.parametrize(("lat", "radius_km"), [(0.0, 30.0), (30.7, 30.0), (45.0, 100.0), (64.8, 100.0)])
def test_padded_bbox_holds_every_point_within_the_radius(lat, radius_km):
    """Every compass point at exactly `radius_km` from the geometry is inside the padded box."""
    lon = -96.0
    box = assets_module._padded_bbox((((lon, lat),),), radius_km)
    d = radius_km / assets_module.EARTH_RADIUS_KM
    p1, l1 = math.radians(lat), math.radians(lon)
    for bearing in range(0, 360, 5):
        b = math.radians(bearing)
        p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
        l2 = l1 + math.atan2(
            math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
        )
        x, y = math.degrees(l2), math.degrees(p2)
        assert box[0] <= x <= box[2] and box[1] <= y <= box[3], (bearing, x, y, box)
