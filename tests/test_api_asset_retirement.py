"""Retired and retiring power plants on the asset API (lane R1, 2026-10-06): the `status` and
`retirement_year[gte|lte]` filters, the detail's `retirement_changes`, `GET /v1/assets/{id}/nearby-grid`,
and asset events staying off the global feed."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api import assets as assets_module
from services.api.conftest import (
    make_asset,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.visibility import event_visibility_filter
from services.db.models import Event, InterconnectionPoint
from services.ids import public_id

UTC = dt.UTC
NOW = dt.datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh_index() -> Iterator[None]:
    assets_module._reset_asset_index_cache()
    yield
    assets_module._reset_asset_index_cache()


@pytest.fixture()
def world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    spec = {
        "operating": ("10", "Running Plant", "operating", (-100.0, 32.0)),
        "retiring": ("20", "Rockport", "retiring", (-87.03, 37.93)),
        "minority": ("30", "Cardinal", "operating", (-80.65, 40.25)),
        "retired": ("40", "Homer City", "retired", (-79.2, 40.51)),
        "old": ("50", "Old Unit Site", "retired", (-90.0, 35.0)),
    }
    plants = {
        key: make_asset(db, src, lic, source_asset_id=sid, name=name, status=status, geom=geom)
        for key, (sid, name, status, geom) in spec.items()
    }
    plants["retiring"].retirement_year = 2028
    plants["minority"].retirement_year = 2030
    plants["retired"].retirement_year = 2024
    plants["old"].retirement_year = 2009
    plants["retiring"].attributes = {
        "retirement": {
            "status_rule": "majority_mw_retiring",
            "retiring_mw": 2600.0,
            "next_planned": "2028-12",
        },
        "grid": {
            "nerc_region": "RFC",
            "grid_voltage_kv": [765.0],
            "transmission_owner": "Indiana Michigan Power Co",
        },
    }
    db.commit()
    return {"lic": lic, "src": src, **plants}


def _ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {row["public_id"] for row in resp.json()["data"]}


def test_status_filter_lists_retired_and_retiring(client: TestClient, world: dict[str, Any]) -> None:
    got = _ids(client.get("/v1/assets", params={"status": "retired,retiring"}))
    assert got == {world[k].public_id for k in ("retiring", "retired", "old")}
    assert _ids(client.get("/v1/assets", params={"status": "retiring"})) == {world["retiring"].public_id}


def test_unknown_status_is_a_400(client: TestClient, world: dict[str, Any]) -> None:
    resp = client.get("/v1/assets", params={"status": "mothballed"})
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"


def test_retirement_year_range(client: TestClient, world: dict[str, Any]) -> None:
    window = {"retirement_year[gte]": "2024", "retirement_year[lte]": "2030"}
    assert _ids(client.get("/v1/assets", params=window)) == {
        world[k].public_id for k in ("retiring", "minority", "retired")
    }
    # A minority retirement stays `operating` but matches the year; the status narrows it out.
    assert _ids(client.get("/v1/assets", params={**window, "status": "retiring,retired"})) == {
        world["retiring"].public_id,
        world["retired"].public_id,
    }
    assert client.get("/v1/assets", params={"retirement_year[gte]": "1066"}).status_code == 400
    assert client.get("/v1/assets", params={"retirement_year[lte]": "soon"}).status_code == 400


def test_sort_by_retirement_year_puts_undated_plants_last(client: TestClient, world: dict[str, Any]) -> None:
    resp = client.get("/v1/assets", params={"sort": "retirement_year"})
    names = [row["name"] for row in resp.json()["data"]]
    assert names[:4] == ["Old Unit Site", "Homer City", "Rockport", "Cardinal"]
    assert names[-1] == "Running Plant"
    assert resp.json()["data"][0]["retirement_year"] == 2009


def test_geo_status_filter(client: TestClient, world: dict[str, Any]) -> None:
    params = {
        "bbox": "-125,24,-66,50",
        "zoom": "4",
        "asset_type": "power_plant",
        "status": "retired,retiring",
    }
    resp = client.get("/v1/assets/geo", params=params)
    assert resp.status_code == 200, resp.text
    feats = resp.json()["data"]["features"]
    assert {f["properties"]["status"] for f in feats} == {"retired", "retiring"}
    assert {f["properties"]["retirement_year"] for f in feats} == {2028, 2024, 2009}
    assert client.get("/v1/assets/geo", params={**params, "status": "bogus"}).status_code == 400
    # The plants alias keeps its own, smaller vocabulary.
    alias = client.get(
        "/v1/context/plants/geo", params={"bbox": "-125,24,-66,50", "zoom": "4", "status": "retired"}
    )
    assert alias.status_code == 400 and alias.json()["code"] == "unknown_parameter"


def _asset_event(
    db: Session, world: dict[str, Any], *, event_type: str, key: str, public: bool = True
) -> Event:
    ev = Event(
        subject_type="asset",
        subject_id=world["retiring"].id,
        event_type=event_type,
        observed_at=NOW - dt.timedelta(days=3),
        published_at=NOW - dt.timedelta(days=1),
        public_at=(NOW - dt.timedelta(days=1)) if public else (NOW + dt.timedelta(days=5)),
        source_id=world["src"].id,
        source_url="https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx",
        retrieved_at=NOW - dt.timedelta(days=3),
        licence_id=world["lic"].id,
        before={"units": [{"generator_id": "1", "date": "2028-12"}]},
        after={
            "units": [
                {"generator_id": "1", "technology": "Conventional Steam Coal", "capacity_mw": 1300.0,
                 "date_before": "2028-12", "date_after": "2030-06"}
            ],
            "capacity_mw": 1300.0,
            "plant_status": "retiring",
        },
        changed_keys=["retirement"],
        reason="Planned retirement date moved: 1 unit, 1,300.0 MW (unit 1 2028-12 -> 2030-06).",
        idempotency_key=key,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    return ev


def test_detail_carries_retirement_and_its_changes(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _asset_event(db, world, event_type="retirement_date_changed", key="k1")
    _asset_event(db, world, event_type="retired", key="k2", public=False)  # not yet public
    db.commit()
    data = client.get(f"/v1/assets/{world['retiring'].public_id}").json()["data"]
    assert data["status"] == "retiring" and data["retirement_year"] == 2028
    assert data["attributes"]["retirement"]["retiring_mw"] == 2600.0
    assert data["attributes"]["grid"]["grid_voltage_kv"] == [765.0]
    changes = data["retirement_changes"]
    assert [c["event_type"] for c in changes] == ["retirement_date_changed"]
    assert changes[0]["units"][0]["date_after"] == "2030-06"
    assert changes[0]["provenance"]["source_id"] == world["src"].id
    other = client.get(f"/v1/assets/{world['operating'].public_id}").json()["data"]
    assert other["retirement_changes"] == [] and other["retirement_year"] is None


def test_asset_events_never_reach_the_global_feed(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    _asset_event(db, world, event_type="retired", key="k3")
    db.commit()
    assert db.scalar(select(func.count()).select_from(Event).where(*event_visibility_filter("public"))) == 0
    assert client.get("/v1/events").json()["data"] == []


def test_nearby_grid_lists_lines_and_the_points_queue_requests_name(
    client: TestClient, db: Session, world: dict[str, Any]
) -> None:
    src, lic, plant = world["src"], world["lic"], world["retiring"]
    line: dict[str, Any] = {"asset_type": "transmission_line", "technology": None, "capacity_mw": None}
    near = make_asset(
        db, src, lic, source_asset_id="L1", name="Rockport - Jefferson 765 kV", geom=(-87.0, 37.95), **line
    )
    near.geom_line = [(-87.04, 37.94), (-86.5, 38.3)]
    near.attributes = {"voltage_kv": 765.0}
    far = make_asset(db, src, lic, source_asset_id="L2", name="Far Away 345 kV", geom=(-80.0, 40.0), **line)
    far.geom_line = [(-80.0, 40.0), (-79.5, 40.2)]
    point = InterconnectionPoint(
        public_id="",
        operator="MISO",
        name_display="Rockport 765kV",
        name_key="sub:rockport|765",
        key_rule="t",
        kind="substation",
        voltage_kv=765.0,
        source_id=src.id,
        source_url=src.url,
        retrieved_at=NOW,
        licence_id=lic.id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    loc = make_location(
        db, src, lic, geom=(-87.05, 37.9), precision="exact", county_name="Spencer", state_code="US-IN"
    )
    prop = make_visible_proposal(db, src, public_id_suffix="7", location=loc, jurisdiction="US-IN")
    prop.interconnection_point_id = point.id
    db.commit()

    resp = client.get(f"/v1/assets/{plant.public_id}/nearby-grid")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["radius_km"] == 25.0
    assert [r["name"] for r in data["transmission_lines"]] == ["Rockport - Jefferson 765 kV"]
    assert data["transmission_lines"][0]["voltage_kv"] == 765.0
    assert data["transmission_lines"][0]["distance_km"] < 2
    assert [p["name"] for p in data["interconnection_points"]] == ["Rockport 765kV"]
    assert data["interconnection_points"][0]["proposals_nearby"] == 1
    assert resp.json()["licence_summary"]
    assert (
        client.get(f"/v1/assets/{plant.public_id}/nearby-grid", params={"radius_km": "500"}).status_code
        == 400
    )
    assert client.get(f"/v1/assets/{plant.public_id}/nearby-grid", params={"zoom": "3"}).status_code == 400
    assert client.get("/v1/assets/asset_nope/nearby-grid").status_code == 404
    # A line asset has no point to measure from: empty, with the reason.
    line = client.get(f"/v1/assets/{near.public_id}/nearby-grid").json()
    assert line["data"]["transmission_lines"] == [] and line["data"]["interconnection_points"] == []
