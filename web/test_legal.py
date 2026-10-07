"""`/privacy` and `/unsubscribe` (`web/legal.py`) -- the web-side half of US-908 AC1's two hard
blockers. Mounts `web.legal.router` onto `web_app` and `services.api.unsubscribe_routes.router`
onto `api_app`, exactly as `web/test_auth.py` mounts `auth_router` -- neither is mounted by
`web/app.py` yet (the coordinator does that mount; out of this task's writable scope).
"""

from __future__ import annotations

import datetime as dt
import re
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
    for heading in (
        "Privacy notice",
        "Who is responsible",
        "If you are named in a record we index",
        "If you use the site",
        "Who processes it for us",
        "How long we keep it",
        "Your rights",
        "Complaints",
        "California residents",
    ):
        assert heading in body, heading
    assert "privacy@infraque.com" in body
    assert "Last updated" in body


def test_privacy_page_names_the_controller_from_config(web_client: TestClient, monkeypatch) -> None:
    """L-7: the controller and address come from `SENDER_LEGAL_NAME`/`SENDER_POSTAL_ADDRESS`
    (owner decision 2026-09-18 (5)); invented values here."""
    monkeypatch.setenv("SENDER_LEGAL_NAME", "Example Holdings LLC (test)")
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "1 Test Street, Testville, TS 00000")
    body = web_client.get("/privacy").text
    assert "Example Holdings LLC (test)" in body
    assert "1 Test Street, Testville, TS 00000" in body


def test_privacy_page_says_plainly_when_the_controller_is_unset(web_client: TestClient, monkeypatch) -> None:
    """L-7: no template token, no 'placeholder' badge; an honest sentence instead."""
    monkeypatch.delenv("SENDER_LEGAL_NAME", raising=False)
    monkeypatch.delenv("SENDER_POSTAL_ADDRESS", raising=False)
    body = web_client.get("/privacy").text
    assert "{{" not in body and "POSTAL_ADDRESS" not in body and "placeholder -- not yet set" not in body
    assert "has not yet been named on this page" in body
    assert "Not yet published on this page" in body


def test_privacy_page_covers_record_subjects_retention_rights_and_cites_no_repo_paths(
    web_client: TestClient,
) -> None:
    body = web_client.get("/privacy").text
    # A repository path ("docs/13-legal-...md"), not the site's own `/docs/api` page in the footer.
    assert not re.search(r"docs/\d", body) and ".md" not in body, (
        "internal repository paths are not for the public"
    )
    assert "legitimate interests" in body
    assert "We did not collect this from you" in body
    assert "35 days" in body and "8 weeks" in body, "backup retention (docs/60 §8)"
    assert "24 months" in body
    assert "ico.org.uk" in body and "edpb.europa.eu" in body and "cppa.ca.gov" in body
    assert "We do not sell or share personal information" in body
    assert "within 30 days" in body


def test_reuse_conditions_page_summarises_each_sources_licence_and_is_not_a_contract(
    web_client: TestClient,
) -> None:
    """L-3: `meta.terms_url` points here; the page states what the register holds and says it is
    not a contract."""
    resp = web_client.get("/legal/reuse")
    assert resp.status_code == 200
    body = resp.text
    assert "not a contract" in body
    assert "Supported by National Energy SO Open Data" in body, "NESO's exact statement"
    assert "Source: California ISO" in body, "CAISO's credit"
    assert "Say what was changed" in body, "the CC BY statement of changes"
    assert "Noncommercial use only" in body and "unaltered" in body, "the RRC's condition"
    assert "Do not republish the source" in body, "derived-only sources"


def test_the_old_api_licence_url_leads_to_the_reuse_conditions(web_client: TestClient) -> None:
    resp = web_client.get("/legal/api-licence", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/legal/reuse"


def test_footer_links_to_privacy_on_every_page(web_client: TestClient) -> None:
    resp = web_client.get("/about")
    assert resp.status_code == 200
    assert 'href="/privacy"' in resp.text


def test_footer_links_to_the_reuse_conditions(web_client: TestClient) -> None:
    resp = web_client.get("/about")
    assert resp.status_code == 200
    assert 'href="/legal/reuse"' in resp.text


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
