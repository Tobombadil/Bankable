"""Tests for `services/api/context_routes.py` (`GET /v1/context/plants/geo`): the
built-infrastructure context layer (docs/00-PLAN.md decision 2026-09-14; docs/21 §3.20).

Uses this directory's own `client`/`db` fixtures (`services/api/conftest.py`) plus its licence and
source factories; `BuiltPlant` rows are seeded with a small local factory since no shared one
exists yet (`services/db/models.py::BuiltPlant` has no counterpart to
`services.api.conftest.make_visible_proposal`)."""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import yaml

from services.api import context_routes
from services.api.conftest import make_open_licence, make_public_source
from services.db.models import BuiltPlant
from tests.test_api_contract import assert_valid

UTC = dt.UTC
_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _reset_plant_index_cache() -> None:
    """`services.api.context_routes._index_cache` is one process-wide instance (its own docstring)
    keyed in part on `id(db.get_bind())`, which already isolates this file's per-test in-memory
    engines from each other in practice — this reset removes any dependence on `id()` reuse
    dynamics being safe across the whole suite, the same reasoning `services/api/conftest.py`'s
    `_reset_rate_limiter` gives for `default_limiter`."""
    context_routes._reset_plant_index_cache()


def make_built_plant(
    session,
    source,
    licence,
    *,
    plant_id_suffix: str = "1",
    geom: tuple[float, float] | None = (-100.0, 32.0),
    technology: str | None = "wind",
    capacity_mw: float | None = 100.0,
    country: str = "US",
    state_code: str | None = "US-TX",
    county_name: str | None = "Nolan",
) -> BuiltPlant:
    plant = BuiltPlant(
        source_plant_id=f"P{plant_id_suffix}",
        name=f"Test Plant {plant_id_suffix}",
        operator_name="Test Operator LLC",
        technology=technology,
        technology_raw="Onshore Wind Turbine" if technology == "wind" else technology,
        technologies={"Onshore Wind Turbine": capacity_mw} if capacity_mw else {},
        capacity_mw=capacity_mw,
        generator_count=3,
        earliest_operating_year=2015,
        geom=geom,
        state_code=state_code,
        county_name=county_name,
        country=country,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=licence.id,
    )
    session.add(plant)
    session.flush()
    return plant


WORLD_BBOX = "-180,-85,180,85"


def test_individual_features_below_split_threshold(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_built_plant(db, src, lic, plant_id_suffix="1", geom=(-100.0, 32.0))
    make_built_plant(db, src, lic, plant_id_suffix="2", geom=(-99.0, 31.0))
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ContextPlantsGeoResponse", body)
    fc = body["data"]
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    assert fc["totals"]["clustered"] is False
    assert fc["totals"]["records"] == 2
    kinds = {f["properties"]["feature_kind"] for f in fc["features"]}
    assert kinds == {"plant"}
    feature = next(f for f in fc["features"] if f["properties"]["name"] == "Test Plant 1")
    props = feature["properties"]
    assert props["technology"] == "wind"
    assert props["capacity_mw"] == 100.0
    assert props["state_code"] == "US-TX"
    assert props["county_name"] == "Nolan"
    assert props["source"]["source_id"] == src.id
    assert props["source"]["licence_id"] == lic.id


def test_individual_feature_id_is_plant_uuid(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    plant = make_built_plant(db, src, lic)
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    fc = resp.json()["data"]
    assert fc["features"][0]["id"] == str(plant.id)


def test_clusters_above_split_threshold(client, db, spec):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(600):
        make_built_plant(db, src, lic, plant_id_suffix=str(i), geom=(-100.0 + (i % 5) * 0.01, 32.0))
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "ContextPlantsGeoResponse", body)
    fc = body["data"]
    assert fc["totals"]["clustered"] is True
    assert fc["totals"]["records"] == 600
    assert len(fc["features"]) < 600
    assert len(fc["features"]) <= 2000
    assert all(f["properties"]["feature_kind"] == "plant_cluster" for f in fc["features"])
    total_clustered = sum(f["properties"]["count"] for f in fc["features"])
    assert total_clustered == 600


def test_technology_filter(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_built_plant(db, src, lic, plant_id_suffix="wind", technology="wind", geom=(-100.0, 32.0))
    make_built_plant(db, src, lic, plant_id_suffix="solar", technology="solar", geom=(-101.0, 33.0))
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "technology": "solar"})
    assert resp.status_code == 200
    fc = resp.json()["data"]
    assert fc["totals"]["records"] == 1
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["technology"] == "solar"
    assert fc["totals"]["technology_counts"] == {"solar": 1}


def test_unknown_technology_is_400(client, db):
    """The task brief describes this as "422", but `services/api/errors.py::validation_error`
    (the shared helper used, unchanged, per this task's read-only scope) is hard-coded to 400 for
    every caller in this codebase (see `ERROR_CODES["validation_error"] = 400` and e.g.
    `test_unknown_query_parameter_is_400` in `services/api/test_routes.py`) — followed here rather
    than introduced as a one-off inconsistent status, and called out in the backend report."""
    resp = client.get(
        "/v1/context/plants/geo",
        params={"bbox": WORLD_BBOX, "zoom": 4, "technology": "not_a_real_technology"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_licence_summary_present(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_built_plant(db, src, lic)
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    body = resp.json()
    assert body["licence_summary"]["sources"]
    row = body["licence_summary"]["sources"][0]
    assert row["source_id"] == src.id
    assert row["licence_id"] == lic.id
    assert row["record_count"] == 1


def test_bbox_scoping(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_built_plant(db, src, lic, plant_id_suffix="in", geom=(-100.0, 32.0))
    make_built_plant(db, src, lic, plant_id_suffix="out", geom=(50.0, 10.0))
    db.commit()

    resp = client.get("/v1/context/plants/geo", params={"bbox": "-106.6,25.8,-93.5,36.5", "zoom": 4})
    assert resp.status_code == 200
    fc = resp.json()["data"]
    # Both plants match the (empty) filter set, so totals count both...
    assert fc["totals"]["records"] == 2
    # ...but only the one inside the requested viewport becomes a feature.
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["name"] == "Test Plant in"


def test_unknown_query_parameter_is_400(client, db):
    resp = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4, "bogus": "x"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"


def test_missing_bbox_or_zoom_is_400(client, db):
    resp = client.get("/v1/context/plants/geo", params={"zoom": 4})
    assert resp.status_code == 400
    resp2 = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX})
    assert resp2.status_code == 400


# ------------------------------------------------------------- process-local index cache (perf)
def test_index_cache_invalidates_after_row_update(client, db):
    """Coordinator follow-up (2026-09-15): the cache is keyed on `(row_count, max(updated_at))`
    (`services.api.context_routes._plant_index_cache_key`) — a row's field changing, with no
    change in row count, must still bust it."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    plant = make_built_plant(db, src, lic, plant_id_suffix="1", technology="wind", geom=(-100.0, 32.0))
    db.commit()

    resp1 = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp1.json()["data"]["totals"]["technology_counts"] == {"wind": 1}

    # Change the field the response depends on, and bump `updated_at` by a whole day so the
    # assertion below can never be a false pass from wall-clock resolution happening to not have
    # advanced between the two requests.
    plant.technology = "solar"
    plant.updated_at = plant.updated_at + dt.timedelta(days=1)
    db.commit()

    resp2 = client.get("/v1/context/plants/geo", params={"bbox": WORLD_BBOX, "zoom": 4})
    assert resp2.json()["data"]["totals"]["technology_counts"] == {"solar": 1}
    feature = resp2.json()["data"]["features"][0]
    assert feature["properties"]["technology"] == "solar"


def test_cached_and_uncached_index_paths_agree(client, db, monkeypatch):
    """A second request with the same underlying data must (a) serve the *identical* feature
    collection as the first (proving the cached path and the cold-rebuild path agree byte for
    byte) and (b) never re-hit `_rebuild_plant_index` (proving it actually was cached, not
    silently rebuilt every time by a key-computation bug) — both requirements from the
    coordinator's follow-up. 700 plants (above `SPLIT_THRESHOLD`) so this exercises the clustered
    path the coordinator's own zoom=4/zoom=6 measurements used."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(700):
        make_built_plant(
            db,
            src,
            lic,
            plant_id_suffix=str(i),
            geom=(-100.0 + (i % 7) * 0.01, 32.0 + (i % 5) * 0.01),
        )
    db.commit()

    calls = {"count": 0}
    original_rebuild = context_routes._rebuild_plant_index

    def _counting_rebuild(db_):
        calls["count"] += 1
        return original_rebuild(db_)

    monkeypatch.setattr(context_routes, "_rebuild_plant_index", _counting_rebuild)

    params = {"bbox": WORLD_BBOX, "zoom": 3}
    resp1 = client.get("/v1/context/plants/geo", params=params)
    resp2 = client.get("/v1/context/plants/geo", params=params)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["data"] == resp2.json()["data"]
    assert resp1.json()["data"]["totals"]["clustered"] is True
    assert calls["count"] == 1, "second request should have served the cached index, not rebuilt it"
