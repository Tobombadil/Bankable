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
from services.billing.fake import InMemoryBilling
from services.crm.fake import InMemoryCrm
from services.db.models import UiEvent, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.sor.wiring import get_billing_port, get_crm_port
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


def test_safe_next_rejects_backslash_and_host_bearing_paths() -> None:
    """Web audit 2026-09-18: browsers normalise `/\\host` to `//host` when following a redirect."""
    from web.auth import _safe_next

    assert _safe_next("/account?tab=alerts") == "/account?tab=alerts"
    bad_values = (
        "/\\evil.com",
        "/\\\\evil.com/x",
        "//evil.com",
        "https://evil.com",
        "/ok\r\nLocation: x",
        "",
        None,
    )
    for bad in bad_values:
        assert _safe_next(bad) == "/account"


# ------------------------------------------------------------ delete one's own account (2026-10-09)
@pytest.fixture()
def fake_crm() -> Iterator[InMemoryCrm]:
    """The deletion asks the CRM first; a fresh in-memory fake per test (the `web_client` teardown
    clears every override, this one included)."""
    crm = InMemoryCrm()
    api_app.dependency_overrides[get_crm_port] = lambda: crm
    api_app.dependency_overrides[get_billing_port] = lambda: InMemoryBilling()
    yield crm


def _register_and_stay(web_client: TestClient, email: str) -> None:
    resp = web_client.post(
        "/register", data={"email": email, "password": "correct horse battery"}, headers=ORIGIN
    )
    assert resp.status_code == 200


def _stored_user(db_sessionmaker: sessionmaker[Session], public_id: str) -> User:
    with db_sessionmaker() as session:
        user = session.scalar(select(User).where(User.public_id == public_id))
        assert user is not None
        session.expunge(user)
        return user


def _public_id_of(db_sessionmaker: sessionmaker[Session], email: str) -> str:
    with db_sessionmaker() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.public_id


def test_account_page_links_to_the_deletion_step(web_client: TestClient) -> None:
    _register_and_stay(web_client, "olga@example.com")
    resp = web_client.get("/account")
    assert 'href="/account/delete"' in resp.text


def test_the_confirmation_step_says_what_goes_and_asks_for_the_password(
    web_client: TestClient, fake_crm: InMemoryCrm
) -> None:
    signed_out = web_client.get("/account/delete", follow_redirects=False)
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login?next=/account/delete"

    _register_and_stay(web_client, "pia@example.com")
    resp = web_client.get("/account/delete")
    assert resp.status_code == 200
    assert "pia@example.com" in resp.text
    assert "cannot be undone" in resp.text
    assert "What is deleted" in resp.text and "What we keep" in resp.text
    assert 'action="/account/delete"' in resp.text
    assert '<label for="delete-password">Your password</label>' in resp.text
    assert 'autocomplete="current-password"' in resp.text
    assert fake_crm.tasks == {}, "showing the confirmation step deletes nothing"


def test_deleting_needs_the_same_origin(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    _register_and_stay(web_client, "quinn@example.com")
    resp = web_client.post("/account/delete", data={"password": "correct horse battery"})
    assert resp.status_code == 403
    cross_site = web_client.post(
        "/account/delete",
        data={"password": "correct horse battery"},
        headers={"origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403
    user = _stored_user(db_sessionmaker, _public_id_of(db_sessionmaker, "quinn@example.com"))
    assert user.status == "active"
    assert fake_crm.tasks == {}


def test_a_wrong_password_rerenders_the_step_and_keeps_the_account(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    _register_and_stay(web_client, "rosa@example.com")
    resp = web_client.post("/account/delete", data={"password": "not the password"}, headers=ORIGIN)
    assert resp.status_code == 400
    assert "does not match this account" in resp.text
    assert 'action="/account/delete"' in resp.text, "the form is shown again"
    user = _stored_user(db_sessionmaker, _public_id_of(db_sessionmaker, "rosa@example.com"))
    assert user.status == "active" and user.email == "rosa@example.com"
    assert web_client.get("/account").status_code == 200, "still signed in"
    assert fake_crm.tasks == {}


def test_deleting_signs_out_and_confirms(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    _register_and_stay(web_client, "sven@example.com")
    public_id = _public_id_of(db_sessionmaker, "sven@example.com")

    resp = web_client.post(
        "/account/delete", data={"password": "correct horse battery"}, headers=ORIGIN, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account/deleted"
    assert "session" not in web_client.cookies, "the browser's cookie is cleared"

    page = web_client.get("/account/deleted")
    assert page.status_code == 200
    assert "Your account is deleted" in page.text
    assert 'href="/login"' in page.text, "the header offers sign-in again"
    assert web_client.get("/account", follow_redirects=False).headers["location"] == "/login?next=/account"

    user = _stored_user(db_sessionmaker, public_id)
    assert user.status == "anonymised" and user.email is not None and user.email.endswith("@erased.invalid")
    assert user.name is None and user.password_hash is None
    assert [t.email for t in fake_crm.tasks.values()] == ["sven@example.com"]
    signed_in_again = web_client.post(
        "/login", data={"email": "sven@example.com", "password": "correct horse battery"}, headers=ORIGIN
    )
    assert signed_in_again.status_code == 401


def test_the_deleted_page_sends_a_signed_in_browser_to_its_account(web_client: TestClient) -> None:
    _register_and_stay(web_client, "tara@example.com")
    resp = web_client.get("/account/deleted", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/account"


def test_staff_are_told_why_there_is_no_form(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    with db_sessionmaker() as session:
        account = make_account(session, entitlement="admin", name="Ops")
        make_user(
            session, account, email="ops@example.com", role="operator", password="correct horse battery"
        )
        session.commit()
    login = web_client.post(
        "/login", data={"email": "ops@example.com", "password": "correct horse battery"}, headers=ORIGIN
    )
    assert login.status_code == 200
    page = web_client.get("/account/delete")
    assert page.status_code == 200
    assert "cannot be deleted here" in page.text
    assert 'action="/account/delete"' not in page.text
    refused = web_client.post("/account/delete", data={"password": "correct horse battery"}, headers=ORIGIN)
    assert refused.status_code == 409, "the API refuses even a hand-made post"
    assert fake_crm.tasks == {}
