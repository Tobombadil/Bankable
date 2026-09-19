"""Admin Engagement screen: the pivot and the page over seeded `ui_event` rows."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import UiEvent
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.admin.engagement import pivot_weeks
from web.admin.engagement import router as engagement_router
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC


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
    if not any(getattr(r, "path", None) == "/admin/engagement" for r in web_app.routes):
        web_app.include_router(engagement_router)
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db,
            account,
            email="operator@example.com",
            role="operator",
            password="correct horse battery staple",
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": "operator@example.com", "password": "correct horse battery staple"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text


def test_pivot_fills_missing_cells_with_zero_and_sorts_newest_first() -> None:
    grid = pivot_weeks(
        [
            {"week": "2026-W36", "name": "map.layer_toggled", "count": 3, "count_on": 2, "count_off": 1},
            {"week": "2026-W37", "name": "alert.created", "count": 1},
        ]
    )
    assert [g["week"] for g in grid] == ["2026-W37", "2026-W36"]
    assert grid[1]["cells"]["map.layer_toggled"] == {"count": 3, "count_on": 2, "count_off": 1}
    assert grid[0]["cells"]["map.layer_toggled"]["count"] == 0
    assert grid[0]["cells"]["alert.created"]["count"] == 1


def test_anonymous_is_redirected_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin/engagement")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/engagement"


def test_engagement_page_renders_seeded_counts(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    with db_sessionmaker() as db:
        db.add_all(
            [
                UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": True}),
                UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": False}),
                UiEvent(name="auth.registered", props={"layers": "plants"}),
            ]
        )
        db.commit()
    resp = web_client.get("/admin/engagement")
    assert resp.status_code == 200, resp.text
    week = dt.datetime.now(UTC).strftime("%G-W%V")
    assert week in resp.text
    assert "Plants layer toggled" in resp.text
    assert "(on 1, off 1)" in resp.text
    assert "Registrations" in resp.text


def test_engagement_page_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/engagement?weeks=2")
    assert resp.status_code == 200, resp.text
    assert "No interaction counts in the last 2 weeks" in resp.text
