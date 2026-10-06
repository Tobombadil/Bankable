"""Saved-search create and update validate their bodies (QA audit 2026-09-30, finding QA-6;
`services/api/pro.py::validate_saved_search_body`): no silent no-op, no 500 from a database CHECK, no
corrupted channel list; and a pause actually stops delivery."""

from __future__ import annotations

import pytest

from services.alerts.evaluate import run_alert_cycle
from services.alerts.mail import ResendAlertMailer
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import SavedSearch
from tests.conftest import login, make_account, make_user


@pytest.fixture()
def pro(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    return account, user


def _create(client, **fields):
    body = {"name": "TX storage", "entity": "proposal", "query": {"kind": "storage"}}
    body.update(fields)
    return client.post("/v1/saved-searches", json=body)


def _assert_400(resp, field: str) -> None:
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["field"] == field


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"delivery_mode": "hourly"}, "delivery_mode"),
        ({"channels": "email"}, "channels"),
        ({"channels": ["sms"]}, "channels"),
        ({"channels": []}, "channels"),
        ({"channels": ["email", "email"]}, "channels"),
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 121}, "name"),
        ({"name": 7}, "name"),
        ({"entity": "watchlist"}, "entity"),
        ({"query": "kind=storage"}, "query"),
        ({"paused": True}, "paused"),
        ({"status": "paused"}, "status"),
    ],
)
def test_create_refuses_bad_input_naming_the_field(client, db, pro, fields, field) -> None:
    _assert_400(_create(client, **fields), field)
    assert db.query(SavedSearch).count() == 0


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"paused": True}, "paused"),
        ({"status": "banana"}, "status"),
        ({"delivery_mode": "hourly"}, "delivery_mode"),
        ({"channels": "email"}, "channels"),
        ({"channels": ["sms"]}, "channels"),
        ({"channels": []}, "channels"),
        ({"name": ""}, "name"),
        ({}, "body"),
    ],
)
def test_update_refuses_bad_input_and_changes_nothing(client, db, pro, body, field) -> None:
    created = _create(client).json()["data"]
    sid = created["saved_search_id"]
    _assert_400(client.patch(f"/v1/saved-searches/{sid}", json=body), field)
    after = client.get(f"/v1/saved-searches/{sid}").json()["data"]
    for key in ("name", "status", "delivery_mode", "channels"):
        assert after[key] == created[key]


def test_the_paused_hint_names_the_real_field(client, db, pro) -> None:
    sid = _create(client).json()["data"]["saved_search_id"]
    resp = client.patch(f"/v1/saved-searches/{sid}", json={"paused": True})
    assert '{"status": "paused"}' in resp.json()["errors"][0]["message"]


def test_renaming_to_a_name_already_used_is_409(client, db, pro) -> None:
    _create(client, name="one")
    sid = _create(client, name="two").json()["data"]["saved_search_id"]
    resp = client.patch(f"/v1/saved-searches/{sid}", json={"name": "one"})
    assert resp.status_code == 409 and resp.json()["code"] == "conflict"
    assert client.patch(f"/v1/saved-searches/{sid}", json={"name": "two"}).status_code == 200, (
        "own name is fine"
    )


def test_valid_channels_are_stored_as_a_list(client, db, pro) -> None:
    data = _create(client, channels=["email", "rss"]).json()["data"]
    assert data["channels"] == ["email", "rss"]
    patched = client.patch(f"/v1/saved-searches/{data['saved_search_id']}", json={"channels": ["email"]})
    assert patched.json()["data"]["channels"] == ["email"]


def test_a_paused_search_is_not_delivered_and_a_resumed_one_is(client, db, pro) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    db.commit()
    sid = _create(client, query={}).json()["data"]["saved_search_id"]
    assert client.patch(f"/v1/saved-searches/{sid}", json={"status": "paused"}).status_code == 200

    prop = make_visible_proposal(db, src)
    make_event(db, prop, src)
    db.commit()
    mailer = ResendAlertMailer(api_key="")
    assert run_alert_cycle(db, email_port=mailer) == []
    assert mailer.sent == []

    db.expire_all()
    assert client.patch(f"/v1/saved-searches/{sid}", json={"status": "active"}).status_code == 200
    db.expire_all()
    created = run_alert_cycle(db, email_port=mailer)
    assert len(created) == 1 and len(mailer.sent) == 1
    assert prop.name_canonical in mailer.sent[0].body
