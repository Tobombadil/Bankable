"""Restricted-precision rule at the API boundary (docs/04 D-9; docs/21 §8 `precise_geo`;
2026-09-18 audit, docs/50 §3.1 "exact coordinates publish under licences that forbid raw"): a
`location` stored `exact` whose licence has `allows_raw_publication = false` is served at its
region grade with `precision_reason = licence`, and the stored coordinate never appears -- on the
proposal list and detail, the proposals map (feature and placement filter), and
`nearby-proposals`. Region grade is the county centroid, else the state centroid, from the same
vendored gazetteer the loader uses.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from services.api.conftest import (
    make_asset,
    make_attribution_licence,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.ingest.geocode import geocode
from tests.test_api_contract import assert_valid

UTC = dt.UTC
WORLD_BBOX = "-180,-85,180,85"
#: A coordinate that is not any gazetteer centroid, so its presence in a body is unambiguous.
SECRET_POINT = (-97.123456, 30.654321)
SECRET_TOKENS = ("-97.123456", "30.654321")


def _raw_withheld_licence(db):
    lic = make_attribution_licence(db, id_="attr-raw-withheld")
    lic.allows_raw_publication = False
    db.flush()
    return lic


def _seed(db, *, county: str | None = "Travis", county_fips: str | None = "48453"):
    lic = _raw_withheld_licence(db)
    src = make_public_source(db, lic, id_="us.test.derived_only_source")
    loc = make_location(
        db,
        src,
        lic,
        geom=SECRET_POINT,
        precision="exact",
        county_name=county,
        county_fips=county_fips,
        state_code="US-TX",
    )
    loc.kind = "point"
    # `make_location` keys FIPS only on `county_centroid` rows; a loader-produced exact row carries
    # it whenever the source names a real county (services/ingest/loader.py::_get_or_create_location).
    loc.county_fips = county_fips
    prop = make_visible_proposal(db, src, public_id_suffix="7", location=loc)
    db.commit()
    return lic, src, loc, prop


def _assert_no_secret(body) -> None:
    text = json.dumps(body)
    for token in SECRET_TOKENS:
        assert token not in text, f"withheld coordinate {token} leaked: {text[:400]}"


def test_detail_and_list_serve_the_county_centroid_with_licence_reason(client, db, spec_contract):
    _lic, _src, _loc, prop = _seed(db)
    expected_point, expected_precision = geocode("TX", "Travis")
    assert expected_precision == "county_centroid"

    detail = client.get(f"/v1/proposals/{prop.public_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert_valid(spec_contract, "ProposalDetailResponse", body)
    loc = body["data"]["location"]
    assert loc["precision"] == "county_centroid"
    assert loc["precision_reason"] == "licence"
    assert loc["geom"] == {"type": "Point", "coordinates": list(expected_point)}
    assert loc["raw_place"] is None
    assert body["redactions"] == [
        {
            "public_id": prop.public_id,
            "field": "location.geom",
            "reason": "licence_precision",
            "source_id": "us.test.derived_only_source",
            "note": "exact coordinate withheld under the source licence; region centroid returned",
        }
    ]
    _assert_no_secret(body)

    listing = client.get("/v1/proposals").json()
    assert_valid(spec_contract, "ProposalListResponse", listing)
    row = next(p for p in listing["data"] if p["public_id"] == prop.public_id)
    assert row["location"]["precision"] == "county_centroid"
    assert row["location"]["precision_reason"] == "licence"
    _assert_no_secret(listing)


def test_state_centroid_when_no_county_resolves(client, db):
    _lic, _src, _loc, prop = _seed(db, county=None, county_fips=None)
    expected_point, expected_precision = geocode("TX", None)
    assert expected_precision == "state_centroid"

    body = client.get(f"/v1/proposals/{prop.public_id}").json()
    loc = body["data"]["location"]
    assert loc["precision"] == "state_centroid"
    assert loc["precision_reason"] == "licence"
    assert loc["geom"]["coordinates"] == list(expected_point)
    _assert_no_secret(body)


def test_geo_serves_a_region_feature_never_the_point(client, db, spec_contract):
    _lic, _src, _loc, prop = _seed(db)
    resp = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec_contract, "GeoResponse", body)
    kinds = [f["properties"]["feature_kind"] for f in body["data"]["features"]]
    assert kinds == ["region"], kinds
    region = body["data"]["features"][0]["properties"]
    assert region["region_level"] == "county"
    assert region["region_id"] == "48453"
    assert region["count"] == 1
    assert prop.public_id not in json.dumps(body)  # no individual proposal feature
    _assert_no_secret(body)

    # A properly-licensed exact proposal in the same viewport still draws as a point.
    open_lic = make_open_licence(db)
    open_src = make_public_source(db, open_lic, id_="us.test.open_source")
    open_loc = make_location(db, open_src, open_lic, geom=(-96.5, 31.5), precision="exact")
    open_prop = make_visible_proposal(db, open_src, public_id_suffix="8", location=open_loc)
    db.commit()
    body = client.get("/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4}).json()
    by_kind = {f["properties"]["feature_kind"]: f for f in body["data"]["features"]}
    assert set(by_kind) == {"region", "proposal"}
    assert by_kind["proposal"]["properties"]["public_id"] == open_prop.public_id
    assert by_kind["proposal"]["properties"]["precision_reason"] is None
    _assert_no_secret(body)


def test_placement_filter_judges_the_served_grade(client, db):
    _lic, _src, _loc, prop = _seed(db)
    open_lic = make_open_licence(db)
    open_src = make_public_source(db, open_lic, id_="us.test.open_source")
    open_loc = make_location(db, open_src, open_lic, geom=(-96.5, 31.5), precision="exact")
    open_prop = make_visible_proposal(db, open_src, public_id_suffix="8", location=open_loc)
    db.commit()

    exact = client.get("/v1/proposals", params={"placement": "exact"}).json()
    assert [p["public_id"] for p in exact["data"]] == [open_prop.public_id]

    region = client.get("/v1/proposals", params={"placement": "region"}).json()
    assert [p["public_id"] for p in region["data"]] == [prop.public_id]
    _assert_no_secret(region)

    geo_exact = client.get(
        "/v1/proposals/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "placement": "exact"}
    ).json()
    assert [f["properties"]["feature_kind"] for f in geo_exact["data"]["features"]] == ["proposal"]
    assert geo_exact["data"]["features"][0]["properties"]["public_id"] == open_prop.public_id


def test_nearby_proposals_excludes_licence_downgraded_points(client, db):
    _lic, _src, _loc, prop = _seed(db)
    open_lic = make_open_licence(db)
    open_src = make_public_source(db, open_lic, id_="us.test.open_source")
    # The asset sits on the withheld coordinate exactly: a naive query would return the
    # downgraded proposal at distance 0 and disclose the point by implication.
    asset = make_asset(db, open_src, open_lic, source_asset_id="1", name="Plant", geom=SECRET_POINT)
    open_loc = make_location(db, open_src, open_lic, geom=(-97.12, 30.66), precision="exact")
    open_prop = make_visible_proposal(db, open_src, public_id_suffix="8", location=open_loc)
    db.commit()

    body = client.get(f"/v1/assets/{asset.public_id}/nearby-proposals", params={"radius_km": 50}).json()
    ids = [p["public_id"] for p in body["data"]]
    assert ids == [open_prop.public_id]
    assert prop.public_id not in ids


def test_stored_row_is_unchanged_by_serving(client, db):
    """The rule is applied at the boundary; nothing rewrites the row (admin surfaces and a future
    licence clearance still see the stored coordinate)."""
    _lic, _src, loc, prop = _seed(db)
    client.get(f"/v1/proposals/{prop.public_id}")
    db.expire_all()
    from services.db.models import Location

    stored = db.get(Location, loc.id)
    assert stored.precision == "exact"
    assert tuple(stored.geom) == SECRET_POINT


@pytest.fixture(scope="module")
def spec_contract():
    import pathlib

    import yaml

    path = pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
