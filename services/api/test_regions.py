"""Tests for `services/api/regions.py` (`GET /v1/geo/regions`, ADR 0008). Uses a tiny fixture
directory via `REGIONS_DIR` rather than the real ~1.6 MB vendored `us_counties.geojson`
(task brief: "Tests must not depend on the vendored files")."""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from services.api import regions as regions_module
from tests.test_api_contract import assert_valid

_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_SQUARE = [[[0, 0], [1, 0], [1, 1], [0, 0]]]


def _feature(**properties: str) -> dict:
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "MultiPolygon", "coordinates": [_SQUARE]},
    }


def _collection(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


@pytest.fixture()
def regions_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    directory = tmp_path / "regions"
    directory.mkdir()
    (directory / "us_counties.geojson").write_text(
        json.dumps(
            _collection(
                _feature(region_id="48453", name="Travis", level="county", state_code="US-TX"),
                _feature(region_id="48001", name="Anderson", level="county", state_code="US-TX"),
            )
        )
    )
    (directory / "us_states.geojson").write_text(
        json.dumps(_collection(_feature(region_id="US-TX", name="Texas", level="state")))
    )
    (directory / "countries.geojson").write_text(
        json.dumps(_collection(_feature(region_id="US", name="United States", level="country")))
    )
    monkeypatch.setenv("REGIONS_DIR", str(directory))
    regions_module._reset_regions_cache()
    yield directory
    regions_module._reset_regions_cache()


def test_county_level_returns_all_when_ids_omitted(client, db, regions_dir, spec):
    resp = client.get("/v1/geo/regions", params={"level": "county"})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "RegionsResponse", body)
    assert len(body["data"]["features"]) == 2


def test_county_level_filters_by_ids(client, db, regions_dir):
    resp = client.get("/v1/geo/regions", params={"level": "county", "ids": "48453"})
    assert resp.status_code == 200
    features = resp.json()["data"]["features"]
    assert len(features) == 1
    assert features[0]["properties"]["region_id"] == "48453"
    assert features[0]["properties"]["name"] == "Travis"


def test_state_level(client, db, regions_dir):
    resp = client.get("/v1/geo/regions", params={"level": "state", "ids": "US-TX"})
    assert resp.status_code == 200
    features = resp.json()["data"]["features"]
    assert len(features) == 1
    assert features[0]["properties"]["region_id"] == "US-TX"


def test_country_level(client, db, regions_dir):
    resp = client.get("/v1/geo/regions", params={"level": "country", "ids": "US"})
    assert resp.status_code == 200
    features = resp.json()["data"]["features"]
    assert len(features) == 1


def test_missing_level_is_400(client, db, regions_dir):
    resp = client.get("/v1/geo/regions")
    assert resp.status_code == 400


def test_invalid_level_is_400(client, db, regions_dir):
    resp = client.get("/v1/geo/regions", params={"level": "planet"})
    assert resp.status_code == 400


def test_too_many_ids_is_400(client, db, regions_dir):
    ids = ",".join(f"{i:05d}" for i in range(501))
    resp = client.get("/v1/geo/regions", params={"level": "county", "ids": ids})
    assert resp.status_code == 400


def test_missing_vendored_file_is_404_with_clear_detail(client, db, regions_dir):
    (regions_dir / "us_states.geojson").unlink()
    regions_module._reset_regions_cache()
    resp = client.get("/v1/geo/regions", params={"level": "state"})
    assert resp.status_code == 404
    body = resp.json()
    assert "REGIONS_DIR" in body["detail"]


def test_unknown_query_parameter_is_400(client, db, regions_dir):
    resp = client.get("/v1/geo/regions", params={"level": "county", "bogus": "x"})
    assert resp.status_code == 400
