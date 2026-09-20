"""Line assets on the API (2026-09-19, owner option (a)): `GET /v1/assets/geo` line features,
line-aware detail and nearby-proposals, organisation asset roles/totals/hierarchy and
`GET /v1/organizations/{id}/nearby-proposals` (`services/api/assets.py`, `services/api/lines.py`).

`asset.geom_line` is set directly on rows built by `services.api.conftest.make_asset` (the factory
predates the column); the SQLite test target stores it as WKT (`services/db/types.py::
GeographyLine`), so a `MultiLineString` is exercised through `parse_line_parts` directly and
through a WKT string written to the column, which the same decoder hands back verbatim.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import pathlib
import random
import time

import pytest
import sqlalchemy as sa
import yaml

from services.api import assets as assets_module
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.api.lines import (
    parse_line_parts,
    parts_intersect_bbox,
    point_to_parts_km,
    simplify_part,
    tolerance_for_zoom,
)
from services.db.models import Asset, Licence
from tests.test_api_contract import assert_valid

UTC = dt.UTC
_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
WORLD_BBOX = "-180,-85,180,85"
CONUS_BBOX = "-125,24,-66,50"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _reset_asset_index_cache() -> None:
    assets_module._reset_asset_index_cache()


def _make_line(
    db,
    src,
    lic,
    coords: list[tuple[float, float]],
    *,
    source_asset_id: str,
    name: str = "Test Pipeline",
    asset_type: str = "gas_pipeline",
    attributes: dict | None = None,
    geom: tuple[float, float] | None = None,
) -> Asset:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    asset = make_asset(
        db,
        src,
        lic,
        source_asset_id=source_asset_id,
        asset_type=asset_type,
        name=name,
        technology=None,
        capacity_mw=None,
        geom=geom or ((min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2),
        state_code="US-CO",
        county_name=None,
    )
    asset.geom_line = coords
    asset.attributes = attributes or {"operator": "Test Midstream LLC", "interstate": True, "diameter_in": 30}
    asset.capacity_value = 1200.0
    asset.capacity_unit = "MMcf/d"
    db.flush()
    return asset


def _make_raw_forbidden_licence(db) -> Licence:
    lic = Licence(
        id="derived-only-lic",
        name="Derived-only Licence",
        reuse_class="attribution",
        attribution_required=True,
        attribution_text="Source: Test Registry",
        requires_link_back=True,
        allows_derived_publication=True,
        allows_raw_publication=False,
        allows_api_redistribution=True,
        allows_bulk_export=False,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms3",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


# ------------------------------------------------------------------------------ pure geometry
def test_parse_line_parts_accepts_every_stored_and_bound_form():
    pairs = [(-100.0, 32.0), (-99.0, 32.5), (-98.0, 32.0)]
    assert parse_line_parts(pairs) == (tuple(pairs),)
    assert parse_line_parts("LINESTRING(-100 32, -99 32.5, -98 32)") == (tuple(pairs),)
    multi = parse_line_parts("MULTILINESTRING((-100 32, -99 32.5), (-98 32, -97 32))")
    assert multi == (((-100.0, 32.0), (-99.0, 32.5)), ((-98.0, 32.0), (-97.0, 32.0)))
    assert parse_line_parts([[(-100.0, 32.0), (-99.0, 32.5)], [(-98.0, 32.0), (-97.0, 32.0)]]) == multi
    geojson = {"type": "MultiLineString", "coordinates": [[[-100, 32], [-99, 32.5]], [[-98, 32], [-97, 32]]]}
    assert parse_line_parts(geojson) == multi
    assert parse_line_parts(json.dumps({"type": "LineString", "coordinates": [[-100, 32], [-98, 32]]})) == (
        ((-100.0, 32.0), (-98.0, 32.0)),
    )
    assert parse_line_parts(None) is None
    assert parse_line_parts([]) is None
    assert parse_line_parts("") is None
    with pytest.raises(ValueError):
        parse_line_parts("POINT(1 2)")


def test_simplify_keeps_endpoints_and_drops_sub_tolerance_wiggle():
    part = tuple((x / 10.0, 0.0001 * ((-1) ** i)) for i, x in enumerate(range(0, 101)))
    simplified = simplify_part(part, tolerance_for_zoom(4))
    assert simplified[0] == part[0] and simplified[-1] == part[-1]
    assert len(simplified) == 2
    assert simplify_part(part, 0.0) == part
    assert tolerance_for_zoom(14) == 0.0
    assert tolerance_for_zoom(4) > tolerance_for_zoom(8) > 0


def test_parts_intersect_bbox_detects_segment_crossing_with_no_vertex_inside():
    diagonal = (((-10.0, -10.0), (10.0, 10.0)),)
    assert parts_intersect_bbox(diagonal, (-1.0, -1.0, 1.0, 1.0))
    # Bbox overlaps the line's envelope but the geometry passes wide of it.
    assert not parts_intersect_bbox(diagonal, (5.0, -9.0, 9.0, -5.0))


def test_point_to_parts_km_measures_to_the_segment_not_the_vertices():
    line = (((-100.0, 32.0), (-98.0, 32.0)),)
    # 0.05 deg of latitude above the midpoint: ~5.5 km to the line, ~48 km to either the centroid
    # (-99, 32) measured as a lone vertex or the endpoints.
    d = point_to_parts_km(-98.5, 32.05, line)
    assert 5.4 < d < 5.7
    assert point_to_parts_km(-98.5, 32.05, (((-99.0, 32.0),),)) > 47


# ------------------------------------------------------------------------------- geo: lines
def test_geo_returns_line_features_alongside_points(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="p1", geom=(-104.7, 40.2))
    line = _make_line(
        db,
        src,
        lic,
        [(-108.7, 40.4), (-106.9, 40.1), (-104.8, 40.6), (-102.3, 40.9)],
        source_asset_id="l1",
        name="Rockies Test Pipeline",
        attributes={
            "operator": "Test Midstream LLC",
            "interstate": True,
            "length_miles": 638.2,
            "states": ["CO"],
        },
    )
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetGeoResponse", body)
    fc = body["data"]
    kinds = sorted(f["properties"]["feature_kind"] for f in fc["features"])
    assert kinds == ["asset", "asset_line"]
    feat = next(f for f in fc["features"] if f["properties"]["feature_kind"] == "asset_line")
    assert feat["id"] == line.public_id
    assert feat["geometry"]["type"] == "LineString"
    assert feat["geometry"]["coordinates"][0] == [-108.7, 40.4]
    props = feat["properties"]
    assert props["asset_type"] == "gas_pipeline"
    assert props["length_miles"] == 638.2  # the registry's stated value wins over the geodesic one
    assert props["attributes"]["states"] == ["CO"]
    assert props["source"]["source_id"] == src.id
    assert props["source"]["licence_id"] == lic.id
    assert fc["totals"]["clustered"] is False
    assert fc["totals"]["line_count"] == 1 and fc["totals"]["lines_shown"] == 1
    assert fc["totals"]["line_tolerance_deg"] == tolerance_for_zoom(5)
    assert fc["totals"]["asset_type_counts"] == {"gas_pipeline": 1, "power_plant": 1}
    assert fc["totals"]["records"] == 2
    assert {s["source_id"] for s in body["licence_summary"]["sources"]} == {src.id}


def test_geo_line_membership_is_intersection_not_centroid(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    # Crosses the viewport (-1..1 square) but every vertex, and the centroid's representative
    # point, lies outside it.
    crossing = _make_line(
        db, src, lic, [(-10.0, -10.0), (10.0, 10.0)], source_asset_id="x", geom=(-9.0, -9.0)
    )
    # Envelope overlaps the viewport, geometry does not.
    passing = _make_line(db, src, lic, [(-10.0, -10.0), (10.0, 10.0)], source_asset_id="y", geom=(0.0, 0.0))
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": "-1,-1,1,1", "zoom": 8})
    ids = {f["id"] for f in resp.json()["data"]["features"]}
    assert crossing.public_id in ids

    resp = client.get("/v1/assets/geo", params={"bbox": "5,-9,9,-5", "zoom": 8})
    ids = {f["id"] for f in resp.json()["data"]["features"]}
    assert passing.public_id not in ids and crossing.public_id not in ids


def test_geo_lines_are_never_clustered_and_do_not_count_toward_the_split(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(assets_module.SPLIT_THRESHOLD + 1):
        make_asset(
            db, src, lic, source_asset_id=f"p{i}", geom=(-100.0 + (i % 50) * 0.1, 30.0 + (i // 50) * 0.1)
        )
    for i in range(3):
        _make_line(db, src, lic, [(-100.0, 35.0 + i), (-95.0, 35.0 + i)], source_asset_id=f"l{i}")
    db.commit()

    resp = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 4})
    fc = resp.json()["data"]
    assert fc["totals"]["clustered"] is True
    kinds = [f["properties"]["feature_kind"] for f in fc["features"]]
    assert kinds.count("asset_line") == 3
    assert "asset_cluster" in kinds and "asset" not in kinds
    assert fc["totals"]["records"] == assets_module.SPLIT_THRESHOLD + 1 + 3


def test_geo_line_simplification_depends_on_zoom(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    wiggle = [(-100.0 + i * 0.01, 35.0 + 0.0005 * ((-1) ** i)) for i in range(200)]
    line = _make_line(db, src, lic, wiggle, source_asset_id="w")
    db.commit()

    coarse = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 4}).json()["data"]
    fine = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 14}).json()["data"]
    coarse_feat = next(f for f in coarse["features"] if f["id"] == line.public_id)
    fine_feat = next(f for f in fine["features"] if f["id"] == line.public_id)
    assert len(coarse_feat["geometry"]["coordinates"]) == 2
    assert len(fine_feat["geometry"]["coordinates"]) == 200
    assert fine["totals"]["line_tolerance_deg"] == 0.0


def test_geo_filters_select_lines_by_type_and_exclude_them_by_technology(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="p1", geom=(-100.0, 32.0))
    _make_line(db, src, lic, [(-100.0, 35.0), (-95.0, 35.0)], source_asset_id="l1")
    db.commit()

    only_lines = client.get(
        "/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5, "asset_type": "gas_pipeline"}
    ).json()["data"]
    assert [f["properties"]["feature_kind"] for f in only_lines["features"]] == ["asset_line"]
    assert only_lines["totals"]["asset_type_counts"] == {"gas_pipeline": 1}

    by_tech = client.get(
        "/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5, "technology": "wind"}
    ).json()["data"]
    assert [f["properties"]["feature_kind"] for f in by_tech["features"]] == ["asset"]
    assert by_tech["totals"]["line_count"] == 0

    plants = client.get("/v1/context/plants/geo", params={"bbox": CONUS_BBOX, "zoom": 5}).json()["data"]
    assert [f["properties"]["feature_kind"] for f in plants["features"]] == ["asset"]


def test_geo_line_cap_keeps_the_longest_and_reports_both_counts(client, db, monkeypatch):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    short = _make_line(db, src, lic, [(-100.0, 35.0), (-99.5, 35.0)], source_asset_id="s")
    medium = _make_line(db, src, lic, [(-100.0, 36.0), (-98.0, 36.0)], source_asset_id="m")
    long_ = _make_line(db, src, lic, [(-100.0, 37.0), (-94.0, 37.0)], source_asset_id="l")
    db.commit()
    monkeypatch.setattr(assets_module, "LINE_FEATURE_CAP", 2)

    fc = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5}).json()["data"]
    ids = [f["id"] for f in fc["features"]]
    assert ids == [long_.public_id, medium.public_id]
    assert short.public_id not in ids
    assert fc["totals"]["line_count"] == 3 and fc["totals"]["lines_shown"] == 2


def test_geo_pipeline_row_without_line_geometry_is_a_point_and_multilinestring_is_served(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    point_only = make_asset(
        db,
        src,
        lic,
        source_asset_id="p",
        asset_type="gas_pipeline",
        technology=None,
        capacity_mw=None,
        geom=(-100.0, 32.0),
    )
    multi = _make_line(db, src, lic, [(-100.0, 35.0), (-99.0, 35.0)], source_asset_id="mm")
    db.commit()
    # `GeographyLine` (services/db/types.py, the data lane's) binds LineString only today; the geo
    # index reads the column as text, so a MultiLineString written past the decoder is served.
    db.execute(
        sa.text("UPDATE asset SET geom_line = :wkt WHERE id = :id"),
        {"wkt": "MULTILINESTRING((-100 35, -99 35), (-98 35, -97 35.5))", "id": str(multi.id)},
    )
    db.commit()

    body = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 6}).json()
    assert_valid(spec, "AssetGeoResponse", body)
    by_id = {f["id"]: f for f in body["data"]["features"]}
    assert by_id[point_only.public_id]["properties"]["feature_kind"] == "asset"
    assert by_id[point_only.public_id]["geometry"]["type"] == "Point"
    assert by_id[multi.public_id]["geometry"]["type"] == "MultiLineString"
    assert len(by_id[multi.public_id]["geometry"]["coordinates"]) == 2


def test_geo_features_carry_slug_and_url_and_miles_is_the_stated_length(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    plant = make_asset(db, src, lic, source_asset_id="p1", geom=(-104.7, 40.2))
    line = _make_line(
        db,
        src,
        lic,
        [(-100.0, 35.0), (-98.0, 35.0)],
        source_asset_id="l1",
        # What `pipeline/context/eia_atlas.py` records: `miles`, not `length_miles`.
        attributes={
            "pipeline_type": "interstate",
            "miles": 481.5,
            "states_crossed": ["US-TX"],
            "part_count": 3,
        },
    )
    db.commit()

    by_id = {
        f["id"]: f
        for f in client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5}).json()["data"][
            "features"
        ]
    }
    assert by_id[plant.public_id]["properties"]["slug"] == plant.slug
    assert by_id[plant.public_id]["properties"]["url"].endswith(f"/assets/{plant.slug}")
    props = by_id[line.public_id]["properties"]
    assert props["slug"] == line.slug and props["url"].endswith(f"/assets/{line.slug}")
    assert props["length_miles"] == 481.5
    assert props["attributes"] == {
        "pipeline_type": "interstate",
        "miles": 481.5,
        "states_crossed": ["US-TX"],
        "part_count": 3,
    }
    assert client.get(f"/v1/assets/{line.public_id}").json()["data"]["length_miles"] == 481.5


def test_geo_organization_filter_returns_its_points_and_lines_without_a_bbox(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Tallgrass Test Energy")
    other = make_org(db, "Someone Else")
    line = _make_line(db, src, lic, [(-100.0, 35.0), (-98.0, 35.0)], source_asset_id="l1")
    plant = make_asset(db, src, lic, source_asset_id="p1", geom=(-104.7, 40.2))
    foreign_line = _make_line(db, src, lic, [(-90.0, 40.0), (-89.0, 40.0)], source_asset_id="l2")
    make_asset(db, src, lic, source_asset_id="p2", geom=(-90.0, 41.0))  # nobody's
    make_asset_owner(db, line, org, src, lic, role="operator", share_pct=None)
    make_asset_owner(db, plant, org, src, lic, role="owner", share_pct=100.0)
    make_asset_owner(db, foreign_line, other, src, lic, role="operator", share_pct=None)
    db.commit()

    resp = client.get("/v1/assets/geo", params={"organization": org.public_id})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "AssetGeoResponse", body)
    fc = body["data"]
    assert {f["id"] for f in fc["features"]} == {line.public_id, plant.public_id}
    assert fc["bbox"] == [-180.0, -90.0, 180.0, 90.0]
    assert fc["totals"]["records"] == 2 and fc["totals"]["asset_type_counts"] == {
        "gas_pipeline": 1,
        "power_plant": 1,
    }
    assert fc["totals"]["line_tolerance_deg"] == tolerance_for_zoom(
        assets_module.ORGANIZATION_GEO_DEFAULT_ZOOM
    )
    assert body["licence_summary"]["sources"][0]["record_count"] == 2

    # A bbox and zoom still apply when given; an unknown organisation matches nothing.
    scoped = client.get(
        "/v1/assets/geo", params={"organization": org.public_id, "bbox": "-101,34,-97,36", "zoom": 8}
    )
    assert [f["id"] for f in scoped.json()["data"]["features"]] == [line.public_id]
    empty = client.get("/v1/assets/geo", params={"organization": "org_doesnotexist"}).json()["data"]
    assert empty["features"] == [] and empty["totals"]["records"] == 0
    assert client.get("/v1/assets/geo").status_code == 400
    assert client.get("/v1/context/plants/geo", params={"organization": org.public_id}).status_code == 400


def test_geo_skips_an_empty_line_geometry(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    empty = _make_line(db, src, lic, [(-100.0, 35.0), (-98.0, 35.0)], source_asset_id="e")
    db.commit()
    db.execute(
        sa.text("UPDATE asset SET geom_line = 'MULTILINESTRING EMPTY' WHERE id = :id"), {"id": str(empty.id)}
    )
    db.commit()

    fc = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5}).json()["data"]
    assert fc["features"] == [] and fc["totals"]["line_count"] == 0
    assert parse_line_parts("MULTILINESTRING EMPTY") is None


def test_simplify_drops_parts_below_tolerance_but_keeps_the_longest():
    from services.api.lines import simplify_parts

    big = ((-100.0, 35.0), (-99.0, 35.2), (-98.0, 35.0))
    speck = ((-97.0, 35.0), (-97.0005, 35.0002))
    tol = tolerance_for_zoom(4)
    assert simplify_parts((big, speck), tol) == (big,)
    assert simplify_parts((speck,), tol) == (speck,)  # never empties a geometry
    assert simplify_parts((big, speck), 0.0) == (big, speck)


def test_subsidiary_roll_ups_across_assets_geo_detail_and_nearby(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    parent = make_org(db, "Tallgrass Test Energy")
    sub = make_org(db, "Rockies Test Express")
    sub.parent_org_id = parent.id
    line = _make_line(db, src, lic, [(-100.0, 32.0), (-98.0, 32.0)], source_asset_id="l1")
    make_asset_owner(db, line, sub, src, lic, role="operator", share_pct=None)
    make_visible_proposal(
        db,
        src,
        public_id_suffix="1",
        location=make_location(db, src, lic, geom=(-98.5, 32.05), precision="exact"),
    )
    db.commit()

    detail = client.get(f"/v1/organizations/{parent.public_id}").json()
    assert_valid(spec, "OrganizationDetailResponse", detail)
    assert detail["data"]["asset_counts"]["assets"] == 0
    assert detail["data"]["group_asset_counts"]["by_role_and_type"] == {"operator": {"gas_pipeline": 1}}

    direct = client.get(f"/v1/organizations/{parent.public_id}/assets").json()
    assert direct["data"] == [] and direct["totals"]["assets"] == 0
    grouped = client.get(
        f"/v1/organizations/{parent.public_id}/assets", params={"include_subsidiaries": "true"}
    ).json()
    assert_valid(spec, "OrganizationAssetsResponse", grouped)
    assert [r["held_by"]["public_id"] for r in grouped["data"]] == [sub.public_id]
    assert grouped["totals"]["assets"] == 1

    geo = client.get(
        "/v1/assets/geo", params={"organization": parent.public_id, "include_subsidiaries": "true"}
    ).json()["data"]
    assert [f["id"] for f in geo["features"]] == [line.public_id]
    assert (
        client.get("/v1/assets/geo", params={"organization": parent.public_id}).json()["data"]["features"]
        == []
    )

    near = client.get(
        f"/v1/organizations/{parent.public_id}/nearby-proposals", params={"include_subsidiaries": "true"}
    ).json()
    assert near["totals"]["assets_considered"] == 1 and len(near["data"]) == 1
    assert (
        client.get(
            f"/v1/organizations/{parent.public_id}/assets", params={"include_subsidiaries": "maybe"}
        ).status_code
        == 400
    )


def test_geo_inline_spec_example_validates_against_the_response_schema(spec):
    """The checker in `api/check_story_coverage.py` validates named component examples only; the
    line-layer example is inline on the operation, so this test is its validation."""
    op = spec["paths"]["/v1/assets/geo"]["get"]
    example = op["responses"]["200"]["content"]["application/json"]["examples"]["lines"]["value"]
    assert_valid(spec, "AssetGeoResponse", example)
    kinds = {f["properties"]["feature_kind"] for f in example["data"]["features"]}
    assert kinds == {"asset_line", "asset"}


# ---------------------------------------------------------------------------------- detail
def test_detail_line_asset_carries_geometry_length_attributes_and_owner_roles(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    coords = [(-100.0, 35.0), (-99.0, 35.0), (-98.0, 35.0)]
    line = _make_line(db, src, lic, coords, source_asset_id="l1", attributes={"interstate": True})
    op = make_org(db, "Tallgrass Test Operator")
    make_asset_owner(db, line, op, src, lic, role="operator", share_pct=None)
    db.commit()

    body = client.get(f"/v1/assets/{line.public_id}").json()
    assert_valid(spec, "AssetDetailResponse", body)
    data = body["data"]
    assert data["geometry"]["type"] == "LineString"
    assert data["geometry"]["coordinates"] == [
        [-100.0, 35.0],
        [-98.0, 35.0],
    ]  # collinear middle simplified away
    # ~2 degrees of longitude at 35N ~ 182 km ~ 113 miles, computed since the registry stated none.
    assert 112 < data["length_miles"] < 115
    assert data["attributes"] == {"interstate": True}
    assert [(o["role"], o["organization"]["public_id"]) for o in data["owners"]] == [
        ("operator", op.public_id)
    ]
    assert body["redactions"] == []


def test_detail_point_asset_geometry_is_a_point_and_list_rows_omit_geometry(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    plant = make_asset(db, src, lic, source_asset_id="p1", geom=(-104.7, 40.2))
    db.commit()

    body = client.get(f"/v1/assets/{plant.public_id}").json()
    assert_valid(spec, "AssetDetailResponse", body)
    assert body["data"]["geometry"] == {"type": "Point", "coordinates": [-104.7, 40.2]}
    assert body["data"]["length_miles"] is None

    rows = client.get("/v1/assets").json()["data"]
    assert "geometry" not in rows[0]


# --------------------------------------------------------------------------------- nearby
def test_nearby_proposals_measure_to_the_line_not_the_centroid(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    line = _make_line(db, src, lic, [(-100.0, 32.0), (-98.0, 32.0)], source_asset_id="l1")
    assert line.geom == (-99.0, 32.0)
    near_line_loc = make_location(db, src, lic, geom=(-98.5, 32.05), precision="exact")
    far_loc = make_location(db, src, lic, geom=(-98.5, 33.0), precision="exact")
    make_visible_proposal(db, src, public_id_suffix="1", location=near_line_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=far_loc)
    db.commit()

    body = client.get(f"/v1/assets/{line.public_id}/nearby-proposals", params={"radius_km": 10}).json()
    assert_valid(spec, "NearbyProposalsResponse", body)
    assert len(body["data"]) == 1
    assert 5.4 < body["data"][0]["distance_km"] < 5.7  # ~48 km from the centroid; would be absent


def test_geometry_gate_is_generic_for_a_raw_forbidding_licence(client, db, spec):
    lic = _make_raw_forbidden_licence(db)
    src = make_public_source(db, lic)
    line = _make_line(db, src, lic, [(-100.0, 32.0), (-98.0, 32.0)], source_asset_id="l1")
    plant = make_asset(db, src, lic, source_asset_id="p1", geom=(-104.7, 40.2))
    loc = make_location(db, src, lic, geom=(-98.5, 32.05), precision="exact")
    make_visible_proposal(db, src, public_id_suffix="1", location=loc)
    db.commit()

    fc = client.get("/v1/assets/geo", params={"bbox": CONUS_BBOX, "zoom": 5}).json()["data"]
    assert fc["features"] == [] and fc["totals"]["records"] == 0

    body = client.get(f"/v1/assets/{line.public_id}").json()
    assert_valid(spec, "AssetDetailResponse", body)
    assert body["data"]["geometry"] is None
    assert body["data"]["length_miles"] is not None  # derived, still served
    assert body["redactions"][0]["field"] == "geometry" and body["redactions"][0]["reason"] == "licence"
    assert client.get(f"/v1/assets/{plant.public_id}").json()["data"]["geometry"] is None

    nearby = client.get(f"/v1/assets/{line.public_id}/nearby-proposals").json()
    assert nearby["data"] == [] and nearby["redactions"][0]["field"] == "geometry"
    assert len(client.get("/v1/assets").json()["data"]) == 2  # list rows stay


# ------------------------------------------------------------------------ organisations
def test_organization_assets_role_annotation_filters_and_totals(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Tallgrass Test Energy")
    for i in range(3):
        line = _make_line(db, src, lic, [(-100.0, 35.0 + i), (-95.0, 35.0 + i)], source_asset_id=f"l{i}")
        make_asset_owner(db, line, org, src, lic, role="operator", share_pct=None)
    plant_a = make_asset(db, src, lic, source_asset_id="pa", name="Plant A", geom=(-100.0, 32.0))
    plant_b = make_asset(db, src, lic, source_asset_id="pb", name="Plant B", geom=(-101.0, 32.0))
    make_asset_owner(db, plant_a, org, src, lic, role="owner", share_pct=100.0)
    make_asset_owner(db, plant_b, org, src, lic, role="owner", share_pct=50.0)
    make_asset_owner(db, plant_b, org, src, lic, role="operator", share_pct=None)  # owns and operates
    db.commit()

    body = client.get(f"/v1/organizations/{org.public_id}/assets").json()
    assert_valid(spec, "OrganizationAssetsResponse", body)
    assert len(body["data"]) == 6
    assert body["totals"] == {
        "assets": 5,
        "by_role": {"operator": 4, "owner": 2},
        "by_type": {"gas_pipeline": 3, "power_plant": 2},
        "by_role_and_type": {"operator": {"gas_pipeline": 3, "power_plant": 1}, "owner": {"power_plant": 2}},
    }
    row = next(r for r in body["data"] if r["public_id"] == plant_b.public_id and r["role"] == "owner")
    assert row["share_pct"] == 50.0 and row["asset_type"] == "power_plant" and "geometry" not in row

    ops = client.get(f"/v1/organizations/{org.public_id}/assets", params={"role": "operator"}).json()
    assert {r["role"] for r in ops["data"]} == {"operator"} and len(ops["data"]) == 4
    assert ops["totals"]["assets"] == 5  # totals ignore the filter

    pipes = client.get(
        f"/v1/organizations/{org.public_id}/assets", params={"asset_type": "gas_pipeline"}
    ).json()
    assert len(pipes["data"]) == 3 and all(r["length_miles"] > 0 for r in pipes["data"])

    assert (
        client.get(f"/v1/organizations/{org.public_id}/assets", params={"role": "lessee"}).status_code == 400
    )


def test_organization_detail_asset_counts_parent_and_subsidiaries(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    parent = make_org(db, "Tallgrass Test Holdings")
    org = make_org(db, "Tallgrass Test Energy")
    sub_b = make_org(db, "Tallgrass Test Sub B")
    sub_a = make_org(db, "Tallgrass Test Sub A")
    org.parent_org_id = parent.id
    sub_a.parent_org_id = org.id
    sub_b.parent_org_id = org.id
    line = _make_line(db, src, lic, [(-100.0, 35.0), (-95.0, 35.0)], source_asset_id="l1")
    make_asset_owner(db, line, org, src, lic, role="operator", share_pct=None)
    db.commit()

    body = client.get(f"/v1/organizations/{org.public_id}").json()
    assert_valid(spec, "OrganizationDetailResponse", body)
    data = body["data"]
    assert data["asset_counts"]["by_role_and_type"] == {"operator": {"gas_pipeline": 1}}
    assert data["parent"]["public_id"] == parent.public_id
    assert [s["public_id"] for s in data["subsidiaries"]] == [sub_a.public_id, sub_b.public_id]
    assert data["subsidiary_count"] == 2

    top = client.get(f"/v1/organizations/{parent.public_id}").json()["data"]
    assert top["parent"] is None and top["subsidiary_count"] == 1
    assert top["asset_counts"] == {"assets": 0, "by_role": {}, "by_type": {}, "by_role_and_type": {}}


def test_organization_nearby_proposals_dedupes_and_names_the_nearest_asset(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Tallgrass Test Energy")
    other = make_org(db, "Someone Else")
    pipe = _make_line(db, src, lic, [(-100.0, 32.0), (-98.0, 32.0)], source_asset_id="l1", name="Pipe")
    plant = make_asset(db, src, lic, source_asset_id="p1", name="Plant", geom=(-98.0, 32.3))
    foreign = make_asset(db, src, lic, source_asset_id="p2", name="Foreign", geom=(-90.0, 40.0))
    make_asset_owner(db, pipe, org, src, lic, role="operator", share_pct=None)
    make_asset_owner(db, plant, org, src, lic, role="owner", share_pct=100.0)
    make_asset_owner(db, foreign, other, src, lic, role="owner", share_pct=100.0)
    # Near both the pipe (5.5 km) and the plant (~22 km): reported once, nearest = pipe.
    make_visible_proposal(
        db,
        src,
        public_id_suffix="1",
        location=make_location(db, src, lic, geom=(-98.5, 32.05), precision="exact"),
    )
    # Near the plant only (~5.5 km).
    make_visible_proposal(
        db,
        src,
        public_id_suffix="2",
        location=make_location(db, src, lic, geom=(-98.0, 32.35), precision="exact"),
    )
    # Near the other organisation's asset only.
    make_visible_proposal(
        db,
        src,
        public_id_suffix="3",
        location=make_location(db, src, lic, geom=(-90.0, 40.02), precision="exact"),
    )
    db.commit()

    body = client.get(f"/v1/organizations/{org.public_id}/nearby-proposals", params={"radius_km": 25}).json()
    assert_valid(spec, "OrganizationNearbyProposalsResponse", body)
    rows = body["data"]
    assert [r["nearest_asset"]["public_id"] for r in rows] == [pipe.public_id, plant.public_id]
    assert rows[0]["distance_km"] < rows[1]["distance_km"]
    assert body["totals"] == {
        "assets_considered": 2,
        "proposals_within_radius": 2,
        "proposals_within_radius_unfiltered": 2,
    }
    assert body["page"]["has_more"] is False

    owned_only = client.get(
        f"/v1/organizations/{org.public_id}/nearby-proposals", params={"radius_km": 25, "role": "owner"}
    ).json()
    # Only the plant is measured from: proposal 1 is ~47 km from it, so it drops out.
    assert [r["nearest_asset"]["public_id"] for r in owned_only["data"]] == [plant.public_id]
    assert owned_only["totals"]["assets_considered"] == 1

    limited = client.get(f"/v1/organizations/{org.public_id}/nearby-proposals", params={"limit": 1}).json()
    assert len(limited["data"]) == 1 and limited["page"]["has_more"] is True
    assert client.get("/v1/organizations/org_missing/nearby-proposals").status_code == 404


# ------------------------------------------------------------------ measured: synthetic national
def _synthetic_lines(n: int, vertices: int, seed: int = 42) -> list[list[tuple[float, float]]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture, not security
    out = []
    for _ in range(n):
        lon = rng.uniform(-123.0, -70.0)
        lat = rng.uniform(26.0, 48.0)
        heading = rng.uniform(0, 6.283)
        coords = [(lon, lat)]
        for _ in range(vertices - 1):
            heading += rng.uniform(-0.6, 0.6)
            step = rng.uniform(0.02, 0.12)
            lon = min(-67.0, max(-124.0, lon + step * 1.2 * math.cos(heading)))
            lat = min(49.0, max(25.0, lat + step * math.sin(heading)))
            coords.append((round(lon, 5), round(lat, 5)))
        out.append(coords)
    return out


def test_national_and_regional_views_over_3000_synthetic_lines_are_small_and_warm_fast(client, db):
    """The measurement the 2026-09-19 brief asks for, on a synthetic 3,000-line, 60-vertex-each
    fixture (no midstream parquet in `data/normalized/context/` yet): payload bytes at zoom 4
    (national, CONUS bbox) and zoom 8 (a ~5-degree regional window), and warm response time at
    each. Printed with `-s`; the asserted bounds are loose so a slow CI box does not fail them."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    lines = _synthetic_lines(3000, 60)
    for i, coords in enumerate(lines):
        _make_line(db, src, lic, coords, source_asset_id=f"syn{i}", name=f"Synthetic Pipeline {i}")
    db.commit()

    def timed(params: dict) -> tuple[float, int, dict]:
        t0 = time.perf_counter()
        resp = client.get("/v1/assets/geo", params=params)
        elapsed = time.perf_counter() - t0
        assert resp.status_code == 200
        return elapsed, len(resp.content), resp.json()["data"]

    national = {"bbox": CONUS_BBOX, "zoom": 4, "asset_type": "gas_pipeline"}
    regional = {"bbox": "-100,33,-95,38", "zoom": 8, "asset_type": "gas_pipeline"}
    cold_4, _, _ = timed(national)
    warm_4, bytes_4, fc4 = timed(national)
    cold_8, _, _ = timed(regional)
    warm_8, bytes_8, fc8 = timed(regional)
    verts_4 = sum(len(f["geometry"]["coordinates"]) for f in fc4["features"])
    gz_4 = len(gzip.compress(client.get("/v1/assets/geo", params=national).content))
    gz_8 = len(gzip.compress(client.get("/v1/assets/geo", params=regional).content))
    print(  # noqa: T201 - the measurement the brief asks for, shown with -s
        f"\nline layer, 3,000 synthetic lines x 60 vertices: zoom 4 national {bytes_4 / 1024:.0f} KB raw / "
        f"{gz_4 / 1024:.0f} KB gzip, {fc4['totals']['lines_shown']}/{fc4['totals']['line_count']} lines, "
        f"{verts_4} vertices, cold {cold_4 * 1000:.0f} ms, warm {warm_4 * 1000:.0f} ms; zoom 8 regional "
        f"{bytes_8 / 1024:.0f} KB raw / {gz_8 / 1024:.0f} KB gzip, {fc8['totals']['lines_shown']} lines, "
        f"cold {cold_8 * 1000:.0f} ms, warm {warm_8 * 1000:.0f} ms"
    )
    assert (
        fc4["totals"]["line_count"] == 3000 and fc4["totals"]["lines_shown"] == assets_module.LINE_FEATURE_CAP
    )
    assert fc4["totals"]["clustered"] is False
    assert bytes_4 < 1_800_000, f"national line payload {bytes_4} bytes raw"
    assert gz_4 < 350_000, f"national line payload {gz_4} bytes gzipped"
    assert bytes_8 < 400_000, f"regional line payload {bytes_8} bytes raw"
    assert warm_4 < 2.0 and warm_8 < 2.0
    # The app compresses every JSON response over 1 KB for a client that accepts gzip.
    resp = client.get("/v1/assets/geo", params=national, headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"


def test_assets_geo_records_total_covers_the_whole_filter_match_not_the_viewport(client, db):
    """Contract pin (docs/23, `AssetGeoTotals.records`): `totals.records` counts every asset
    matching the filters, not the ones the viewport shows. A map labelling "in view" must count
    the returned features instead -- reading this field for that reported 1,536 assets in view
    beside eight rows (web/static/js/map.js, 2026-09-19)."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_asset(db, src, lic, source_asset_id="near", geom=(-100.0, 32.0))
    make_asset(db, src, lic, source_asset_id="far", geom=(10.0, 50.0))
    db.commit()

    narrow = client.get("/v1/assets/geo", params={"bbox": "-101,31,-99,33", "zoom": 8})
    assert narrow.status_code == 200
    body = narrow.json()["data"]
    assert len(body["features"]) == 1, "the viewport shows one asset"
    assert body["totals"]["records"] == 2, "but the total covers both, by design"

    world = client.get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": 2})
    assert world.json()["data"]["totals"]["records"] == 2


# ------------------------------------- organisation nearby-proposals: the technology filter
def _org_with_pipeline_and_neighbours(db):
    """A one-pipeline operator with four exact-grade proposals along the line: two gas, one solar,
    one storage. The shape the company page's default relevance filter is built for."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Midstream Test Co")
    line = _make_line(db, src, lic, [(-100.0, 32.0), (-98.0, 32.0)], source_asset_id="l1")
    make_asset_owner(db, line, org, src, lic, role="operator", share_pct=None)
    for suffix, technology in (("1", "gas_ct"), ("2", "gas_cc"), ("3", "solar"), ("4", "storage")):
        make_visible_proposal(
            db,
            src,
            public_id_suffix=suffix,
            technology=technology,
            location=make_location(db, src, lic, geom=(-99.0 - int(suffix) / 100, 32.02), precision="exact"),
        )
    db.commit()
    return org


def test_organization_nearby_proposals_technology_filter_and_both_totals(client, db, spec):
    org = _org_with_pipeline_and_neighbours(db)
    path = f"/v1/organizations/{org.public_id}/nearby-proposals"

    unfiltered = client.get(path).json()
    assert_valid(spec, "OrganizationNearbyProposalsResponse", unfiltered)
    assert unfiltered["totals"]["proposals_within_radius"] == 4
    # No filter given: the two counts agree, so a caller never has to special-case the absence.
    assert unfiltered["totals"]["proposals_within_radius_unfiltered"] == 4

    filtered = client.get(path, params={"technology": "gas_ct,gas_cc"}).json()
    assert_valid(spec, "OrganizationNearbyProposalsResponse", filtered)
    assert {p["technology"] for p in filtered["data"]} == {"gas_ct", "gas_cc"}
    # "N of M": the filtered count and the count it was taken from, so the caller can say both.
    assert filtered["totals"]["proposals_within_radius"] == 2
    assert filtered["totals"]["proposals_within_radius_unfiltered"] == 4
    assert filtered["totals"]["assets_considered"] == 1
    # Every row keeps the provenance quartet and its nearest asset (CLAUDE.md: the API never
    # returns a record without them).
    for row in filtered["data"]:
        quartet = row["provenance"][0]
        assert quartet["source_id"] and quartet["source_url"] and quartet["retrieved_at"]
        assert quartet["licence_id"] and quartet["reuse_class"]
        assert row["nearest_asset"]["public_id"].startswith("asset_")

    # A technology nothing nearby carries is an honest empty list under the same denominator,
    # never a silent fallback to everything.
    empty = client.get(path, params={"technology": "nuclear"}).json()
    assert empty["data"] == []
    assert empty["totals"] == {
        "assets_considered": 1,
        "proposals_within_radius": 0,
        "proposals_within_radius_unfiltered": 4,
    }


def test_organization_nearby_proposals_rejects_an_unknown_technology(client, db):
    org = _org_with_pipeline_and_neighbours(db)

    resp = client.get(
        f"/v1/organizations/{org.public_id}/nearby-proposals", params={"technology": "unobtanium"}
    )
    assert resp.status_code == 400
    assert "unobtanium" in resp.json()["detail"]


def test_organization_nearby_proposals_technology_composes_with_limit(client, db):
    """`limit` pages the filtered list, and `totals` stays the count of the whole filtered set --
    the company page states counts from `totals`, never from the rows it rendered."""
    org = _org_with_pipeline_and_neighbours(db)

    body = client.get(
        f"/v1/organizations/{org.public_id}/nearby-proposals",
        params={"technology": "gas_ct,gas_cc", "limit": 1},
    ).json()

    assert len(body["data"]) == 1 and body["page"]["has_more"] is True
    assert body["totals"]["proposals_within_radius"] == 2
    assert body["totals"]["proposals_within_radius_unfiltered"] == 4
