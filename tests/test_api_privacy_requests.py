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
