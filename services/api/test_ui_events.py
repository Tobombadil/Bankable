"""Tests for `services/api/ui_events.py`: `POST /v1/ui-events` and
`GET /admin/v1/ui-events/summary` (docs/00-PLAN.md decision 2026-09-14; docs/21 §3.21).

Uses this directory's own `client`/`db` fixtures (`services/api/conftest.py`, which resets the
shared rate limiter per-test) plus `tests/conftest.py`'s `login`/`make_account`/`make_user` for the
admin-only summary endpoint.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import yaml
from sqlalchemy import select

from services.db.models import UiEvent
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC
_OPENAPI_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with _OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ------------------------------------------------------------------------- POST /v1/ui-events
def test_post_each_event_name_is_202_with_empty_body(client, db):
    happy_bodies = [
        {"name": "map.layer_toggled", "props": {"layer": "plants", "on": True}},
        {"name": "map.region_jumped", "props": {"region": "us"}},
        {"name": "map.basemap_failed", "props": {}},
        {"name": "auth.registered", "props": {"layers": "plants"}},
        {"name": "alert.created", "props": {}},
    ]
    for body in happy_bodies:
        resp = client.post("/v1/ui-events", json=body)
        assert resp.status_code == 202, body
        assert resp.content == b""

    stored = list(db.scalars(select(UiEvent)).all())
    assert {r.name for r in stored} == {b["name"] for b in happy_bodies}


def test_post_missing_props_defaults_to_empty_object(client, db):
    resp = client.post("/v1/ui-events", json={"name": "alert.created"})
    assert resp.status_code == 202


def test_post_unknown_name_is_400(client, db):
    resp = client.post("/v1/ui-events", json={"name": "totally.unknown", "props": {}})
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_post_unallowed_prop_key_is_400(client, db):
    resp = client.post(
        "/v1/ui-events", json={"name": "map.region_jumped", "props": {"region": "us", "extra": "x"}}
    )
    assert resp.status_code == 400


def test_post_wrong_type_is_400(client, db):
    resp = client.post(
        "/v1/ui-events", json={"name": "map.layer_toggled", "props": {"layer": "plants", "on": "yes"}}
    )
    assert resp.status_code == 400


def test_post_too_long_string_is_400(client, db):
    resp = client.post("/v1/ui-events", json={"name": "map.region_jumped", "props": {"region": "x" * 17}})
    assert resp.status_code == 400


def test_post_too_many_props_is_400(client, db):
    resp = client.post(
        "/v1/ui-events",
        json={"name": "map.basemap_failed", "props": {f"k{i}": "v" for i in range(9)}},
    )
    assert resp.status_code == 400


def test_post_email_like_value_is_400(client, db):
    resp = client.post(
        "/v1/ui-events", json={"name": "map.region_jumped", "props": {"region": "a@example.com"}}
    )
    assert resp.status_code == 400


def test_post_uuid_like_value_is_400(client, db):
    resp = client.post(
        "/v1/ui-events",
        json={"name": "auth.registered", "props": {"layers": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}},
    )
    assert resp.status_code == 400


def test_post_ipv4_like_value_is_400(client, db):
    resp = client.post("/v1/ui-events", json={"name": "map.region_jumped", "props": {"region": "10.0.0.1"}})
    assert resp.status_code == 400


def test_post_token_like_value_is_400(client, db):
    resp = client.post(
        "/v1/ui-events",
        json={"name": "auth.registered", "props": {"layers": "abcdefghijklmnopqrstuvwxyz012345"}},
    )
    assert resp.status_code == 400


def test_post_never_stores_extra_fields(client, db):
    resp = client.post("/v1/ui-events", json={"name": "map.region_jumped", "props": {"region": "us"}})
    assert resp.status_code == 202
    row = db.scalar(select(UiEvent).where(UiEvent.name == "map.region_jumped"))
    assert row is not None
    assert row.props == {"region": "us"}


def test_rate_limit_429_after_ceiling(client, db):
    last = None
    for _ in range(61):
        last = client.post("/v1/ui-events", json={"name": "alert.created", "props": {}})
    assert last is not None
    assert last.status_code == 429
    assert last.json()["code"] == "rate_limited"
    assert "Retry-After" in last.headers


# ------------------------------------------------------------------ GET /admin/v1/ui-events/summary
def _operator(db):
    account = make_account(db, entitlement="admin", name="Ops")
    return make_user(db, account, email=f"op{id(account)}@example.com", role="operator")


def _viewer(db):
    account = make_account(db, entitlement="public", name="Viewer")
    return make_user(db, account, email=f"viewer{id(account)}@example.com", role="viewer")


def test_summary_requires_auth(client, db):
    resp = client.get("/admin/v1/ui-events/summary")
    assert resp.status_code == 401


def test_summary_requires_operator_role(client, db):
    viewer = _viewer(db)
    db.commit()
    login(client, db, viewer)
    resp = client.get("/admin/v1/ui-events/summary")
    assert resp.status_code == 403


def test_summary_week_buckets_with_layer_toggle_split(client, db, spec):
    now = dt.datetime.now(UTC)
    db.add(UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": True}, occurred_at=now))
    db.add(UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": True}, occurred_at=now))
    db.add(UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": False}, occurred_at=now))
    db.add(UiEvent(name="alert.created", props={}, occurred_at=now))
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/ui-events/summary", params={"weeks": 8})
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "UiEventsSummaryResponse", body)
    rows = body["data"]
    iso_year, iso_week, _ = now.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"

    toggled = next(r for r in rows if r["name"] == "map.layer_toggled" and r["week"] == week_label)
    assert toggled["count"] == 3
    assert toggled["count_on"] == 2
    assert toggled["count_off"] == 1

    created = next(r for r in rows if r["name"] == "alert.created" and r["week"] == week_label)
    assert created["count"] == 1
    assert "count_on" not in created


def test_summary_excludes_events_outside_window(client, db):
    old = dt.datetime.now(UTC) - dt.timedelta(weeks=20)
    db.add(UiEvent(name="alert.created", props={}, occurred_at=old))
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/ui-events/summary", params={"weeks": 1})
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_summary_unknown_query_parameter_is_400(client, db):
    operator = _operator(db)
    db.commit()
    login(client, db, operator)
    resp = client.get("/admin/v1/ui-events/summary", params={"bogus": "x"})
    assert resp.status_code == 400
