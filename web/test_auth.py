"""Web-side login/registration surface (Sprint 3 "login and registration surface", first wave):
`web/auth.py` mounted on `web.app.app` (already done by that module's own `app.include_router`
call), driving `services.api.app.app` -- with `services/api/auth_routes.py` mounted onto it, the
same guarded pattern `tests/test_api_auth.py` uses -- in-process, exactly as
`tests/test_web_provenance.py` does for the read-only surfaces. No Playwright; plain
`TestClient(web_app)` requests.

The `auth.registered` tests below drive the real `POST /v1/ui-events`
(`services/api/ui_events.py`, mounted on `services.api.app.app`) rather than a fake -- it landed
in a parallel lane while this was built (`docs/CHANGELOG.md` 2026-09-15 backend-developer) -- and
assert on the `UiEvent` row it wrote.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter
from services.api.auth_routes import get_email_port
from services.api.auth_routes import router as auth_router
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import UiEvent
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.api_client import ApiClient
from web.app import app as web_app

if not any(getattr(r, "path", None) == "/v1/auth/register" for r in api_app.routes):
    api_app.include_router(auth_router)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


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


# ---------------------------------------------------------------------------------- form pages
def test_login_page_renders_a_labelled_form(web_client: TestClient) -> None:
    resp = web_client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    assert '<label for="login-email">Email</label>' in body
    assert '<label for="login-password">Password</label>' in body
    assert 'action="/login"' in body


def test_register_page_renders_a_labelled_form(web_client: TestClient) -> None:
    resp = web_client.get("/register")
    assert resp.status_code == 200
    body = resp.text
    assert '<label for="register-email">Email</label>' in body
    assert '<label for="register-password">Password</label>' in body
    assert 'action="/register"' in body


def test_nav_shows_sign_in_when_signed_out_and_account_when_signed_in(
    web_client: TestClient,
) -> None:
    anon = web_client.get("/about")
    assert 'href="/login"' in anon.text
    assert 'href="/account"' not in anon.text

    reg = web_client.post(
        "/register",
        data={"email": "nav@example.com", "password": "correct horse battery", "next": "/account"},
        headers=ORIGIN,
    )
    assert reg.status_code == 200  # followed the 303 to /account

    signed_in = web_client.get("/about")
    assert 'href="/account"' in signed_in.text
    assert 'href="/login"' not in signed_in.text


# --------------------------------------------------------------------------------------- register
def test_post_register_redirects_and_sets_session_cookie(web_client: TestClient) -> None:
    resp = web_client.post(
        "/register",
        data={"email": "gina@example.com", "password": "correct horse battery", "next": "/account"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"
    assert "session" in resp.cookies


def test_post_without_same_origin_header_is_refused(web_client: TestClient) -> None:
    resp = web_client.post(
        "/register",
        data={"email": "hank@example.com", "password": "correct horse battery"},
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------------------------------ login
def test_login_wrong_password_rerenders_form_with_error_text(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as session:
        account = make_account(session, entitlement="public")
        make_user(session, account, email="ivan@example.com", password="correct horse battery staple")
        session.commit()

    resp = web_client.post(
        "/login",
        data={"email": "ivan@example.com", "password": "wrong-password", "next": "/account"},
        headers=ORIGIN,
    )
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text


# ---------------------------------------------------------------------------------------- account
def test_account_renders_tier_and_unverified_state(web_client: TestClient) -> None:
    web_client.post(
        "/register",
        data={"email": "jan@example.com", "password": "correct horse battery"},
        headers=ORIGIN,
    )
    resp = web_client.get("/account")
    assert resp.status_code == 200
    assert "public" in resp.text  # account.entitlement default
    assert "Not verified" in resp.text


def test_account_without_a_cookie_redirects_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/account", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/account"


def test_account_resend_shows_the_dev_link(web_client: TestClient) -> None:
    web_client.post(
        "/register",
        data={"email": "kim@example.com", "password": "correct horse battery"},
        headers=ORIGIN,
    )
    resp = web_client.post("/account/resend", headers=ORIGIN)
    assert resp.status_code == 200
    assert "development only" in resp.text.lower()
    assert "/verify?token=" in resp.text


def test_verify_completes_via_the_dev_link(web_client: TestClient) -> None:
    reg = web_client.post(
        "/register",
        data={"email": "liam@example.com", "password": "correct horse battery"},
        headers=ORIGIN,
    )
    assert reg.status_code == 200
    resend = web_client.post("/account/resend", headers=ORIGIN)
    start = resend.text.index("/verify?token=")
    end = resend.text.index("</a>", start)
    verify_path = resend.text[start:end].split('"')[0]

    resp = web_client.get(verify_path)
    assert resp.status_code == 200
    assert "verified" in resp.text.lower()


# ----------------------------------------------------------------------------------------- logout
def test_logout_clears_cookie_and_account_then_redirects_to_login(web_client: TestClient) -> None:
    web_client.post(
        "/register",
        data={"email": "mona@example.com", "password": "correct horse battery"},
        headers=ORIGIN,
    )
    logout_resp = web_client.post("/logout", headers=ORIGIN, follow_redirects=False)
    assert logout_resp.status_code == 303

    account_resp = web_client.get("/account", follow_redirects=False)
    assert account_resp.status_code == 303
    assert account_resp.headers["location"] == "/login?next=/account"


# -------------------------------------------------------------------------- auth.registered event
def test_register_posts_auth_registered_event_with_layers_from_next(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    """Map task item 5: a sign-up started from the map page carries `?layers=plants` on `next`
    (written onto the header "Sign in" link by `map.js`'s `writeFilters`); `register_submit` reads
    it back off the now-validated `next_path` and posts `auth.registered {layers}` after redirect
    cookies are set, without blocking or altering the redirect itself. `POST /v1/ui-events`
    (`services/api/ui_events.py`) is real and mounted on `api_app` here -- this drives it for
    real rather than a fake, and asserts on the `UiEvent` row it wrote (in-memory SQLite is
    pinned to one shared connection, `services/db/session.py::get_engine`, so the register
    request and this read see the same database)."""
    resp = web_client.post(
        "/register",
        data={
            "email": "layers@example.com",
            "password": "correct horse battery",
            "next": "/account?layers=plants",
        },
        headers=ORIGIN,
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/account?layers=plants"
    with db_sessionmaker() as session:
        events = list(session.scalars(select(UiEvent).where(UiEvent.name == "auth.registered")))
    assert len(events) == 1
    assert events[0].props == {"layers": "plants"}


def test_register_without_layers_posts_empty_layers_string(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    web_client.post(
        "/register",
        data={"email": "nolayers@example.com", "password": "correct horse battery", "next": "/account"},
        headers=ORIGIN,
        follow_redirects=False,
    )

    with db_sessionmaker() as session:
        events = list(session.scalars(select(UiEvent).where(UiEvent.name == "auth.registered")))
    assert len(events) == 1
    assert events[0].props == {"layers": ""}
