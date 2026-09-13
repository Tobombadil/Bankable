"""The admin shell's operator guard (coordinator-owned contract for the admin page modules)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.api_client import ApiClient
from web.app import app as web_app


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
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
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session], *, role: str) -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db, account, email=f"{role}@example.com", role=role, password="correct horse battery staple"
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": f"{role}@example.com", "password": "correct horse battery staple"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text


def test_admin_root_redirects_anonymous_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin"


def test_admin_root_is_forbidden_for_a_member(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker, role="member")
    resp = web_client.get("/admin")
    assert resp.status_code == 403
    assert "Operator role required" in resp.text


def test_admin_root_sends_an_operator_to_source_health(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sources"
