"""Item 4 of the 2026-09-18 blockers sprint: the filer-deletion route the privacy notice promised
(`services/api/privacy_routes.py`; docs/50-audit-2026-09-18.md §3.1; US-910).

Builds a standalone app with only this router (the "option B" `tests/test_api_admin_people.py`
uses), so the suite passes before the coordinator mounts the router on `services.api.app.app`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.audit import hash_identifier
from services.api.auth import create_session
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.api.privacy_routes import router
from services.api.ratelimit import default_limiter
from services.db.models import Event, PrivacyRequest
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user

UTC = dt.UTC
VALID = {
    "kind": "erasure",
    "record_public_id": "prop_01J8ZK3M9QWERTY2345",
    "contact_email": "filer@example.com",
    "message": "I am the contact named on this filing; please remove my name.",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-not-a-secret")
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(router)

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _login_operator(client: TestClient, db: Session, *, role: str = "operator") -> None:
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email=f"{role}@example.com", role=role)
    _row, cookie = create_session(db, user)
    db.commit()
    client.cookies.set("session", cookie)


# ------------------------------------------------------------------------------------ public
def test_public_post_stores_the_request_and_returns_its_id_without_personal_data(client, db):
    resp = client.post("/v1/privacy/requests", json=VALID)

    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["public_id"].startswith("prq_")
    assert data["kind"] == "erasure"
    assert data["record_public_id"] == VALID["record_public_id"]
    assert data["status"] == "open"
    assert "contact_email" not in data and "message" not in data
    assert resp.json()["meta"]["tier"] == "public"

    row = db.scalar(select(PrivacyRequest))
    assert row is not None
    assert row.contact_email == "filer@example.com"
    assert row.message == VALID["message"]
    assert db.query(Event).count() == 0, "no audit event on intake: nothing to erase later"


def test_public_post_needs_no_auth_and_does_not_confirm_record_existence(client):
    unknown = {**VALID, "record_public_id": "prop_ZZZZZZZZZZZZZZZZ"}
    assert client.post("/v1/privacy/requests", json=unknown).status_code == 202


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "delete-everything"),
        ("kind", None),
        ("record_public_id", "not a public id"),
        ("record_public_id", ""),
        ("contact_email", "no-at-sign"),
        ("contact_email", None),
        ("message", 42),
        ("message", "x" * 4001),
    ],
)
def test_public_post_validates_each_field(client, db, field, value):
    body = {**VALID, field: value}
    resp = client.post("/v1/privacy/requests", json=body)
    assert resp.status_code == 400
    problem = resp.json()
    assert problem["code"] == "validation_error"
    assert any(e["field"] == field for e in problem["errors"])
    assert db.query(PrivacyRequest).count() == 0


def test_public_post_is_rate_limited_per_ip(client):
    for _ in range(20):
        assert client.post("/v1/privacy/requests", json=VALID).status_code == 202
    resp = client.post("/v1/privacy/requests", json=VALID)
    assert resp.status_code == 429
    assert resp.json()["code"] == "rate_limited"
    assert "Retry-After" in resp.headers


# ------------------------------------------------------------------------------------- admin
def test_admin_list_requires_an_operator(client, db):
    assert client.get("/admin/v1/privacy-requests").status_code == 401
    account = make_account(db, entitlement="pro")
    member = make_user(db, account, email="member@example.com", role="member")
    _row, cookie = create_session(db, member)
    db.commit()
    client.cookies.set("session", cookie)
    assert client.get("/admin/v1/privacy-requests").status_code == 403


def test_admin_list_and_get_show_the_request_oldest_first_with_contact(client, db):
    client.post("/v1/privacy/requests", json=VALID)
    client.post("/v1/privacy/requests", json={**VALID, "kind": "correction", "message": None})
    _login_operator(client, db)

    listed = client.get("/admin/v1/privacy-requests")
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert [r["kind"] for r in rows] == ["erasure", "correction"]
    assert rows[0]["contact_email"] == "filer@example.com"
    assert rows[0]["message"] == VALID["message"]
    assert rows[1]["message"] is None

    filtered = client.get("/admin/v1/privacy-requests", params={"kind": "correction"}).json()["data"]
    assert [r["kind"] for r in filtered] == ["correction"]
    assert client.get("/admin/v1/privacy-requests", params={"bogus": "1"}).status_code == 400

    one = client.get(f"/admin/v1/privacy-requests/{rows[0]['public_id']}")
    assert one.status_code == 200
    assert one.json()["data"]["public_id"] == rows[0]["public_id"]
    assert client.get("/admin/v1/privacy-requests/prq_nope").status_code == 404


def test_admin_close_clears_the_contact_and_audits_only_its_hash(client, db):
    created = client.post("/v1/privacy/requests", json=VALID).json()["data"]
    _login_operator(client, db)

    no_reason = client.patch(f"/admin/v1/privacy-requests/{created['public_id']}", json={"status": "done"})
    assert no_reason.status_code == 400

    resp = client.patch(
        f"/admin/v1/privacy-requests/{created['public_id']}",
        json={"status": "done", "reason": "record redacted by hand"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "done"
    assert data["contact_email"] is None
    assert data["completed_at"] is not None

    row = db.scalar(select(PrivacyRequest))
    assert row.contact_email is None
    event = db.scalar(select(Event).where(Event.subject_type == "privacy_request"))
    assert event is not None
    assert event.before["contact_email_hash"] == hash_identifier("filer@example.com")
    assert "filer@example.com" not in str(event.before) + str(event.after)
    assert event.after["contact_email"] == "cleared"


def test_admin_in_progress_keeps_the_contact(client, db):
    created = client.post("/v1/privacy/requests", json=VALID).json()["data"]
    _login_operator(client, db)
    resp = client.patch(
        f"/admin/v1/privacy-requests/{created['public_id']}",
        json={"status": "in_progress", "reason": "assigned"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["contact_email"] == "filer@example.com"
    bad = client.patch(
        f"/admin/v1/privacy-requests/{created['public_id']}", json={"status": "weird", "reason": "x"}
    )
    assert bad.status_code == 400


# ------------------------------------------------------------- legal audit 2026-09-30, L-6
def test_closing_clears_the_message_too_and_audits_only_its_hash(client, db):
    """L-6 (a): the free text usually names the person and can give their address; it must not
    outlive the request any more than the contact address does."""
    body = {**VALID, "message": "I am Margaret Fictitia of 12 Nowhere Lane; please remove my name."}
    created = client.post("/v1/privacy/requests", json=body).json()["data"]
    _login_operator(client, db)
    resp = client.patch(
        f"/admin/v1/privacy-requests/{created['public_id']}",
        json={"status": "rejected", "reason": "not ours"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["message"] is None
    db.expire_all()
    row = db.scalar(select(PrivacyRequest))
    assert row.message is None
    event = db.scalar(select(Event).where(Event.subject_type == "privacy_request"))
    assert event.before["message_hash"] == hash_identifier(body["message"])
    assert "Nowhere Lane" not in str(event.before) + str(event.after)
    assert event.after["message"] == "cleared"


def test_every_request_is_due_thirty_days_after_receipt_and_overdue_is_filterable(client, db):
    """L-6 (d): docs/13 §5.4 rule 6's 30-day clock, stored and shown, with an overdue view."""
    created = client.post("/v1/privacy/requests", json=VALID).json()["data"]
    received = dt.datetime.fromisoformat(created["created_at"].replace("Z", "+00:00"))
    due = dt.datetime.fromisoformat(created["due_at"].replace("Z", "+00:00"))
    assert due - received == dt.timedelta(days=30)
    assert created["overdue"] is False

    late = client.post("/v1/privacy/requests", json={**VALID, "kind": "correction"}).json()["data"]
    row = db.scalar(select(PrivacyRequest).where(PrivacyRequest.public_id == late["public_id"]))
    row.created_at = dt.datetime.now(UTC) - dt.timedelta(days=45)
    row.due_at = dt.datetime.now(UTC) - dt.timedelta(days=15)
    db.commit()

    _login_operator(client, db)
    overdue = client.get("/admin/v1/privacy-requests", params={"overdue": "true"}).json()["data"]
    assert [r["public_id"] for r in overdue] == [late["public_id"]]
    assert overdue[0]["overdue"] is True
    on_time = client.get("/admin/v1/privacy-requests", params={"overdue": "false"}).json()["data"]
    assert [r["public_id"] for r in on_time] == [created["public_id"]]
    assert client.get("/admin/v1/privacy-requests", params={"overdue": "maybe"}).status_code == 400

    client.patch(
        f"/admin/v1/privacy-requests/{late['public_id']}", json={"status": "done", "reason": "redacted"}
    )
    assert client.get("/admin/v1/privacy-requests", params={"overdue": "true"}).json()["data"] == []


def test_the_checklist_lists_every_record_that_names_the_subject(client, db):
    """L-6 (b): an erasure starting from one proposal id finds the sponsor, the other proposal it
    sponsors, a proposal that carries the name, its ownership edge and alias, and says what is left."""
    from services.api.conftest import (
        make_asset,
        make_asset_owner,
        make_open_licence,
        make_org,
        make_public_source,
        make_visible_proposal,
    )

    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    person = make_org(db, name="Margaret Fictitia")
    other = make_org(db, name="Unrelated Wind LLC")
    first = make_visible_proposal(db, source, public_id_suffix="1", sponsor=person)
    second = make_visible_proposal(db, source, public_id_suffix="2", sponsor=person)
    named = make_visible_proposal(db, source, public_id_suffix="3", sponsor=other)
    named.name_canonical = "Margaret Fictitia Barn Solar"
    unrelated = make_visible_proposal(db, source, public_id_suffix="4", sponsor=other)
    asset = make_asset(db, source, lic, name="Fictitia Farm Turbine")
    make_asset_owner(db, asset, person, source, lic, share_pct=12.5)
    db.commit()
    assert person.personal_data is True

    created = client.post("/v1/privacy/requests", json={**VALID, "record_public_id": first.public_id}).json()[
        "data"
    ]
    _login_operator(client, db)
    resp = client.get(f"/admin/v1/privacy-requests/{created['public_id']}/checklist")
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_kind: dict[str, set[str]] = {}
    for item in data["items"]:
        by_kind.setdefault(item["kind"], set()).add(item["public_id"])
    assert by_kind["organization"] == {person.public_id}
    assert by_kind["proposal"] == {first.public_id, second.public_id, named.public_id}
    assert unrelated.public_id not in by_kind["proposal"]
    assert by_kind["asset_owner"] == {asset.public_id}
    assert data["remaining"] == len(data["items"])  # nothing is done yet
    assert data["request"]["due_at"]
    org_item = next(i for i in data["items"] if i["kind"] == "organization")
    assert org_item["personal_data"] is True

    person.publish_state = "unpublished"
    db.commit()
    after = client.get(f"/admin/v1/privacy-requests/{created['public_id']}/checklist").json()["data"]
    done = {i["kind"] for i in after["items"] if i["done"]}
    assert {"organization", "asset_owner"} <= done
    assert after["remaining"] < data["remaining"]

    unknown = client.post(
        "/v1/privacy/requests", json={**VALID, "record_public_id": "org_ZZZZZZZZZZZZZZZZ"}
    ).json()["data"]
    items = client.get(f"/admin/v1/privacy-requests/{unknown['public_id']}/checklist").json()["data"]["items"]
    assert [i["kind"] for i in items] == ["unresolved"]
    assert client.get("/admin/v1/privacy-requests/prq_nope/checklist").status_code == 404
