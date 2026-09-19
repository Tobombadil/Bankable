"""`/privacy` and `/unsubscribe` (`web/legal.py`) -- the web-side half of US-908 AC1's two hard
blockers. Mounts `web.legal.router` onto `web_app` and `services.api.unsubscribe_routes.router`
onto `api_app`, exactly as `web/test_auth.py` mounts `auth_router` -- neither is mounted by
`web/app.py` yet (the coordinator does that mount; out of this task's writable scope).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter
from services.api.auth_routes import get_email_port
from services.api.auth_routes import router as auth_router
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.api.unsubscribe_routes import router as unsubscribe_router
from services.db.models import Alert, SavedSearch
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.api_client import ApiClient
from web.app import app as web_app
from web.legal import router as legal_router

UTC = dt.UTC

if not any(getattr(r, "path", None) == "/v1/auth/register" for r in api_app.routes):
    api_app.include_router(auth_router)

if not any(getattr(r, "path", None) == "/v1/alerts/unsubscribe" for r in api_app.routes):
    # `services/api/pro.py` already registers `GET /v1/alerts/{alert_id}` on this shared `app`.
    # Starlette matches in registration order, so a plain (appending) `include_router` would let
    # that parameterised route swallow `/v1/alerts/unsubscribe` first (an `alert_id` of literally
    # "unsubscribe") and answer `401` instead of reaching this router -- the same hazard
    # `tests/test_api_unsubscribe.py` documents and works around. The coordinator's real mount
    # must put `unsubscribe_routes.router` before `pro.router` for the same reason.
    _before = list(api_app.router.routes)
    api_app.include_router(unsubscribe_router)
    _added = [r for r in api_app.router.routes if r not in _before]
    for _r in _added:
        api_app.router.routes.remove(_r)
    api_app.router.routes[0:0] = _added

if not any(getattr(r, "path", None) == "/privacy" for r in web_app.routes):
    web_app.include_router(legal_router)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
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
def email_port() -> ResendEmailAdapter:
    return ResendEmailAdapter()


@pytest.fixture()
def web_client(
    db_sessionmaker: sessionmaker[Session], email_port: ResendEmailAdapter
) -> Iterator[TestClient]:
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
    api_app.dependency_overrides[get_email_port] = lambda: email_port
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


ORIGIN = {"origin": "http://testserver"}


def _seed_alert(db: Session, *, channels: list[str] | None = None) -> Alert:
    account = make_account(db)
    user = make_user(db, account)
    from services.ids import public_id

    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="Texas storage",
        entity="proposal",
        query={"kind": "storage"},
        query_hash="x",
        channels=channels if channels is not None else ["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    alert = Alert(
        public_id="alr_test",
        saved_search_id=search.id,
        user_id=user.id,
        channel="email",
        mode="daily",
        window_start=dt.datetime.now(UTC) - dt.timedelta(days=1),
        window_end=dt.datetime.now(UTC),
        event_seqs=[],
        recipient=user.email,
        subject="1 new match(es)",
        status="sent",
        unsubscribe_token="ut_web_test_token",
    )
    db.add(alert)
    db.commit()
    return alert


# ------------------------------------------------------------------------------------- privacy
def test_privacy_page_renders_the_required_sections(web_client: TestClient) -> None:
    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    body = resp.text
    assert "Privacy notice" in body
    assert "What we store" in body
    assert "Accounts" in body
    assert "Alerts" in body
    assert "Intake and reports" in body
    assert "CRM" in body
    assert "Why we use it" in body
    assert "Retention and deletion" in body
    assert "US-910" in body
    assert "privacy@infraque.com" in body
    assert "{{POSTAL_ADDRESS}}" in body
    assert "Last updated" in body


def test_footer_links_to_privacy_on_every_page(web_client: TestClient) -> None:
    resp = web_client.get("/about")
    assert resp.status_code == 200
    assert 'href="/privacy"' in resp.text


# --------------------------------------------------------------------------------- unsubscribe
def test_get_unsubscribe_with_valid_token_renders_confirmation(web_client: TestClient, db: Session) -> None:
    _seed_alert(db, channels=["email"])

    resp = web_client.get("/unsubscribe", params={"token": "ut_web_test_token"})

    assert resp.status_code == 200
    assert "no longer receive email alerts" in resp.text
    assert "Texas storage" in resp.text


def test_get_unsubscribe_with_invalid_token_renders_not_found_text(web_client: TestClient) -> None:
    resp = web_client.get("/unsubscribe", params={"token": "ut_bogus"})

    assert resp.status_code == 404
    assert "not recognized" in resp.text or "invalid" in resp.text.lower()


def test_get_unsubscribe_without_a_token_renders_the_entry_form(web_client: TestClient) -> None:
    resp = web_client.get("/unsubscribe")

    assert resp.status_code == 200
    assert 'action="/unsubscribe"' in resp.text
    assert '<label for="unsubscribe-token">' in resp.text


def test_post_unsubscribe_confirm_button_works(web_client: TestClient, db: Session) -> None:
    _seed_alert(db, channels=["email"])

    resp = web_client.post("/unsubscribe", data={"token": "ut_web_test_token"}, headers=ORIGIN)

    assert resp.status_code == 200
    assert "no longer receive email alerts" in resp.text


def test_post_unsubscribe_without_origin_is_403(web_client: TestClient, db: Session) -> None:
    _seed_alert(db, channels=["email"])

    resp = web_client.post("/unsubscribe", data={"token": "ut_web_test_token"})

    assert resp.status_code == 403
