"""Tests for placement grades (ADR 0008, docs/21 §3.7): `GET /v1/proposals/geo`'s `region`
features, the `placement` and `county_fips` filters on `GET /v1/proposals` and
`GET /v1/proposals/geo`, and `country_centroid` as a legal `location.precision` value end to end.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from services.api.app import PLACEMENT_REGION_PRECISIONS
from services.api.conftest import make_location, make_open_licence, make_public_source, make_visible_proposal
from services.api.geo import REGION_PRECISIONS
from tests.test_api_contract import assert_valid

_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
WORLD_BBOX = "-179,-85,179,85"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_region_precision_constants_agree():
    assert set(PLACEMENT_REGION_PRECISIONS) == set(REGION_PRECISIONS)


# ------------------------------------------------------------------------ region features (geo)
def test_county_centroid_proposals_group_into_one_region_feature(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    loc1 = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    loc2 = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    make_visible_proposal(db, src, public_id_suffix="1", location=loc1, lifecycle_state="filed")
    make_visible_proposal(db, src, public_id_suffix="2", location=loc2, lifecycle_state="built")
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "GeoResponse", body)
    features = body["data"]["features"]
    assert len(features) == 1
    region = features[0]["properties"]
    assert region["feature_kind"] == "region"
    assert region["region_level"] == "county"
    assert region["region_id"] == "48453"
    assert region["count"] == 2
    assert region["lifecycle_state_counts"] == {"filed": 1, "built": 1}
    # totals still cover every visible record, region grouping aside.
    assert body["data"]["totals"]["records"] == 2
    assert body["meta"]["unplaced_count"] == 0


def test_state_and_country_centroid_proposals_are_separate_region_features(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    state_loc = make_location(
        db, src, lic, geom=(-99.9, 31.9), precision="state_centroid", county_name=None, state_code="US-TX"
    )
    country_loc = make_location(
        db, src, lic, geom=(-98.6, 39.8), precision="country_centroid", county_name=None, state_code=None
    )
    make_visible_proposal(db, src, public_id_suffix="1", location=state_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=country_loc)
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    features = resp.json()["data"]["features"]
    assert len(features) == 2
    by_level = {f["properties"]["region_level"]: f["properties"] for f in features}
    assert by_level["state"]["region_id"] == "US-TX"
    assert by_level["country"]["region_id"] == "US"


def test_exact_and_region_features_coexist_in_one_response(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    exact_loc = make_location(db, src, lic, geom=(-100.0, 32.0), precision="exact")
    region_loc = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    make_visible_proposal(db, src, public_id_suffix="1", location=exact_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=region_loc)
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    kinds = {f["properties"]["feature_kind"] for f in resp.json()["data"]["features"]}
    assert kinds == {"proposal", "region"}


# ----------------------------------------------------------------------------- placement filter
def test_placement_filter_exact_only_drops_region_features(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    exact_loc = make_location(db, src, lic, geom=(-100.0, 32.0), precision="exact")
    region_loc = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    make_visible_proposal(db, src, public_id_suffix="1", location=exact_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=region_loc)
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "placement": "exact"})
    body = resp.json()["data"]
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["feature_kind"] == "proposal"
    # placement never changes totals/unplaced (docs/23 §3.1).
    assert body["totals"]["records"] == 2


def test_placement_filter_never_changes_unplaced_count(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    exact_loc = make_location(db, src, lic, geom=(-100.0, 32.0), precision="exact")
    unplaced_loc = make_location(
        db, src, lic, geom=None, precision="unknown", county_name=None, state_code=None
    )
    make_visible_proposal(db, src, public_id_suffix="1", location=exact_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=unplaced_loc)
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "placement": "exact"})
    assert resp.json()["meta"]["unplaced_count"] == 1

    resp2 = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "placement": "none"})
    assert resp2.json()["meta"]["unplaced_count"] == 1
    assert resp2.json()["data"]["features"] == []


def test_placement_filter_on_plain_proposal_list(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    exact_loc = make_location(db, src, lic, geom=(-100.0, 32.0), precision="exact")
    unplaced_loc = make_location(
        db, src, lic, geom=None, precision="unknown", county_name=None, state_code=None
    )
    make_visible_proposal(db, src, public_id_suffix="1", location=exact_loc)
    make_visible_proposal(db, src, public_id_suffix="2", location=unplaced_loc)
    db.commit()

    # No default on the plain list: both records are returned when `placement` is omitted.
    resp = client.get("/v1/proposals")
    assert len(resp.json()["data"]) == 2

    resp2 = client.get("/v1/proposals", params={"placement": "none"})
    data = resp2.json()["data"]
    assert len(data) == 1
    assert data[0]["public_id"].endswith("2") is False  # sanity: still a valid public id
    assert data[0]["location"]["precision"] == "unknown"


def test_invalid_placement_value_is_400(client, db):
    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "placement": "bogus"})
    assert resp.status_code == 400


# ------------------------------------------------------------------------------- county_fips filter
def test_county_fips_filter_on_proposals_geo(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    travis = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    anderson = make_location(db, src, lic, geom=(-95.6, 31.8), county_name="Anderson", county_fips="48001")
    make_visible_proposal(db, src, public_id_suffix="1", location=travis)
    make_visible_proposal(db, src, public_id_suffix="2", location=anderson)
    db.commit()

    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "county_fips": "48453"})
    body = resp.json()["data"]
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["region_id"] == "48453"
    # county_fips is a real narrowing filter -- unlike placement, it does shrink totals.
    assert body["totals"]["records"] == 1


def test_county_fips_filter_on_proposal_list(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    travis = make_location(db, src, lic, geom=(-97.7, 30.3), county_name="Travis", county_fips="48453")
    anderson = make_location(db, src, lic, geom=(-95.6, 31.8), county_name="Anderson", county_fips="48001")
    make_visible_proposal(db, src, public_id_suffix="1", location=travis)
    make_visible_proposal(db, src, public_id_suffix="2", location=anderson)
    db.commit()

    resp = client.get("/v1/proposals", params={"county_fips": "48001"})
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["location"]["county_name"] == "Anderson"
