"""`GET`/`POST /privacy/request` (`web/legal.py`) — the human-facing side of item 4 of the
2026-09-18 blockers sprint, and the privacy notice's updated §3 text. Wires the web app to the
API app the way `web/test_legal.py` does, with `services.api.privacy_routes.router` mounted on the
API side because the coordinator's real mount has not happened yet."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.privacy_routes import router as privacy_router
from services.api.ratelimit import default_limiter
from services.db.models import PrivacyRequest
from services.db.session import get_engine, get_sessionmaker, init_db
from web.api_client import ApiClient
from web.app import app as web_app
from web.legal import router as legal_router

if not any(getattr(r, "path", None) == "/v1/privacy/requests" for r in api_app.routes):
    api_app.include_router(privacy_router)
if not any(getattr(r, "path", None) == "/privacy/request" for r in web_app.routes):
    web_app.include_router(legal_router)

ORIGIN = {"origin": "http://testserver"}
FORM = {
    "kind": "correction",
    "record_public_id": "prop_01J8ZK3M9QWERTY2345",
    "contact_email": "filer@example.com",
    "message": "The contact name on this record is out of date.",
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
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_privacy_notice_links_the_request_form_and_names_the_suppression_rule(web_client: TestClient) -> None:
    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    assert 'href="/privacy/request"' in resp.text
    assert "POST /v1/privacy/requests" in resp.text
    assert "suppression list" in resp.text


def test_get_form_renders_without_calling_the_api(web_client: TestClient) -> None:
    resp = web_client.get("/privacy/request")
    assert resp.status_code == 200
    assert 'name="record_public_id"' in resp.text
    assert 'name="contact_email"' in resp.text
    assert "nothing is sent automatically" in resp.text


def test_post_records_the_request_and_shows_its_id(web_client: TestClient, db_sessionmaker) -> None:
    resp = web_client.post("/privacy/request", data=FORM, headers=ORIGIN)
    assert resp.status_code == 200
    assert "recorded" in resp.text
    assert "prq_" in resp.text
    with db_sessionmaker() as db:
        row = db.scalar(select(PrivacyRequest))
        assert row is not None
        assert row.kind == "correction"
        assert row.contact_email == "filer@example.com"


def test_post_rejects_cross_origin_before_touching_the_api(web_client: TestClient, db_sessionmaker) -> None:
    resp = web_client.post("/privacy/request", data=FORM, headers={"origin": "http://evil.example"})
    assert resp.status_code == 403
    with db_sessionmaker() as db:
        assert db.query(PrivacyRequest).count() == 0


def test_post_re_renders_the_form_with_the_api_problem_on_a_bad_field(web_client: TestClient) -> None:
    resp = web_client.post(
        "/privacy/request", data={**FORM, "contact_email": "not-an-address"}, headers=ORIGIN
    )
    assert resp.status_code == 400
    assert "contact_email" in resp.text
    assert 'value="prop_01J8ZK3M9QWERTY2345"' in resp.text, "the form keeps what was typed"
