"""`/alerts` on the public site (owner decision 2026-09-30; `web/alerts.py`): save a view as an
alert, list, pause, resume and delete; the sign-in prompt; the free-alert wording under the
noncommercial posture; and the page-view counter on detail pages.

Drives `services.api.app.app` in-process through the site, the way `web/test_auth.py` does. The
posture is switched by setting `services.billing.router.PAID_TIERS_ACTIVE`, the one constant the
free-alert rule reads (`services/api/alert_plan.py`).
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import alert_plan
from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter
from services.api.auth_routes import get_email_port
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.billing import router as billing_router
from tests.web_alerts_support import memory_sessionmaker, seed_proposal, ui_event_props
from web.alerts import filter_rows, saved_search_query, suggested_name
from web.api_client import ApiClient
from web.app import app as web_app

ORIGIN = {"origin": "http://testserver"}
SITE_TOKEN = "web-alerts-test-token"
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return memory_sessionmaker()


@pytest.fixture()
def web_client(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setenv("API_INTERNAL_TOKEN", SITE_TOKEN)
    api_app.dependency_overrides[get_db] = _override_get_db
    api_app.dependency_overrides[get_email_port] = ResendEmailAdapter
    web_app.state.api_client = ApiClient(
        TestClient(api_app, base_url="http://api-internal", headers={"X-Internal-Token": SITE_TOKEN})
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


@pytest.fixture()
def noncommercial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", False)


def _register(client: TestClient, email: str = "reader@example.com", *, verify: bool = True) -> None:
    resp = client.post(
        "/register", data={"email": email, "password": PASSWORD, "next": "/account"}, headers=ORIGIN
    )
    assert resp.status_code == 200, resp.text
    if verify:
        resent = client.post("/account/resend", headers=ORIGIN)
        token = resent.text.split("/verify?token=")[1].split('"')[0]
        assert client.get(f"/verify?token={token}").status_code == 200


def _save(client: TestClient, query: str) -> TestClient:
    page = client.get(f"/alerts/new?{query}")
    assert page.status_code == 200, page.text
    hidden_query = page.text.split('name="query" value="')[1].split('"')[0].replace("&amp;", "&")
    entity = page.text.split('name="entity" value="')[1].split('"')[0]
    name = page.text.split('id="alert-name" name="name" type="text" maxlength="120" required value="')[
        1
    ].split('"')[0]
    return client.post(
        "/alerts",
        data={"name": name, "entity": entity, "query": hidden_query, "delivery_mode": "daily"},
        headers=ORIGIN,
        follow_redirects=False,
    )


# ------------------------------------------------------------------------------------ anonymous
def test_anonymous_gets_a_sign_in_prompt_that_comes_back(web_client: TestClient) -> None:
    resp = web_client.get("/alerts")
    assert resp.status_code == 200
    assert "Sign in to save and manage alerts." in resp.text
    assert 'href="/login?next=%2Falerts"' in resp.text
    assert 'href="/register?next=%2Falerts"' in resp.text
    assert "noindex" in resp.text

    new = web_client.get("/alerts/new?entity=proposal&technology=storage")
    assert "Sign in to save and manage alerts." in new.text
    assert "next=%2Falerts%2Fnew%3Fentity%3Dproposal%26technology%3Dstorage" in new.text


def test_mutations_need_the_same_origin(web_client: TestClient) -> None:
    assert web_client.post("/alerts", data={"name": "x"}).status_code == 403
    assert web_client.post("/alerts/ss_1/pause").status_code == 403
    assert web_client.post("/alerts/ss_1/delete").status_code == 403


# ---------------------------------------------------------------------------------- commercial
def test_under_commercial_a_free_account_is_told_alerts_are_paid(web_client: TestClient) -> None:
    _register(web_client)
    page = web_client.get("/alerts")
    assert "Email alerts are part of the paid plans." in page.text
    assert "Save alert" not in web_client.get("/alerts/new?entity=proposal").text
    listing = web_client.get("/proposals")
    assert "Alerts and API in Pro" in listing.text
    assert "Free email alerts" not in listing.text


# ------------------------------------------------------------------------------- noncommercial
def test_list_page_offers_to_save_the_view_and_the_banner_points_to_free_alerts(
    web_client: TestClient, noncommercial: None
) -> None:
    page = web_client.get("/proposals?technology=bess_li_ion&jurisdiction=US-TX")
    assert page.status_code == 200
    assert '<a href="/alerts">Free email alerts &rarr;</a>' in page.text
    assert "Alerts and API in Pro" not in page.text
    href = page.text.split('<p class="save-alert"><a href="')[1].split('"')[0].replace("&amp;", "&")
    assert href.startswith("/alerts/new?")
    params = parse_qs(urlsplit(href).query)
    assert params["entity"] == ["proposal"]
    assert params["technology"] == ["bess_li_ion"] and params["jurisdiction"] == ["US-TX"]
    assert "Free: a daily or weekly email" in page.text

    opportunities = web_client.get("/opportunities?kind=grant")
    assert "entity=opportunity" in opportunities.text


def test_the_map_links_to_save_its_view(web_client: TestClient, noncommercial: None) -> None:
    page = web_client.get("/?kind=load")
    assert 'id="mf-save-alert"' in page.text
    href = page.text.split('id="mf-save-alert"')[0].rsplit('href="', 1)[1].split('"')[0].replace("&amp;", "&")
    assert parse_qs(urlsplit(href).query) == {"entity": ["proposal"], "origin": ["map"], "kind": ["load"]}


def test_save_list_pause_resume_and_delete(web_client: TestClient, noncommercial: None) -> None:
    _register(web_client)
    empty = web_client.get("/alerts")
    assert "No alerts yet." in empty.text
    assert '0</span> of <span class="tnum">10</span> alerts used' in empty.text
    assert "Free while Infraque operates as a noncommercial service" in empty.text

    new = web_client.get("/alerts/new?entity=proposal&technology=bess_li_ion&jurisdiction=US-TX")
    assert "Proposals this alert watches" in new.text
    assert "Active (announced through under construction)" in new.text
    assert 'value="daily" checked' in new.text and 'value="weekly"' in new.text
    assert 'value="immediate"' not in new.text, "immediate delivery is not on the free plan"

    saved = _save(web_client, "entity=proposal&technology=bess_li_ion&jurisdiction=US-TX")
    assert saved.status_code == 303 and saved.headers["location"] == "/alerts?done=created"

    listing = web_client.get("/alerts?done=created")
    assert "Alert saved." in listing.text
    assert "Bess li ion in US-TX" in listing.text
    assert "Daily digest" in listing.text and "not run yet" in listing.text
    assert '1</span> of <span class="tnum">10</span> alerts used' in listing.text
    sid = listing.text.split('action="/alerts/')[1].split("/")[0]

    paused = web_client.post(f"/alerts/{sid}/pause", headers=ORIGIN)
    assert "Alert paused." in paused.text
    assert f'action="/alerts/{sid}/resume"' in paused.text
    assert "chip--neutral" in paused.text

    resumed = web_client.post(f"/alerts/{sid}/resume", headers=ORIGIN)
    assert "Alert resumed." in resumed.text and f'action="/alerts/{sid}/pause"' in resumed.text

    deleted = web_client.post(f"/alerts/{sid}/delete", headers=ORIGIN)
    assert "Alert deleted." in deleted.text and "No alerts yet." in deleted.text


def test_an_unverified_reader_is_asked_to_verify_first(web_client: TestClient, noncommercial: None) -> None:
    _register(web_client, verify=False)
    page = web_client.get("/alerts/new?entity=proposal")
    assert "Verify your email address first." in page.text
    assert "Save alert" not in page.text


def test_the_cap_shows_before_the_api_refuses(
    web_client: TestClient, noncommercial: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alert_plan, "FREE_ALERT_CAP", 1)
    _register(web_client)
    assert _save(web_client, "entity=proposal&kind=storage").status_code == 303
    listing = web_client.get("/alerts")
    assert "You have used every alert on your plan." in listing.text
    new = web_client.get("/alerts/new?entity=proposal&kind=load")
    assert "You have used all" in new.text and "Save alert" not in new.text
    # A stale form posted anyway gets the API's own refusal, re-rendered.
    refused = web_client.post(
        "/alerts",
        data={"name": "second", "entity": "proposal", "query": "kind=load", "delivery_mode": "daily"},
        headers=ORIGIN,
    )
    assert refused.status_code == 403
    assert "Free alert limit reached" in refused.text


def test_a_duplicate_name_is_explained(web_client: TestClient, noncommercial: None) -> None:
    _register(web_client)
    body = {"name": "Same", "entity": "proposal", "query": "kind=storage", "delivery_mode": "daily"}
    assert web_client.post("/alerts", data=body, headers=ORIGIN, follow_redirects=False).status_code == 303
    again = web_client.post("/alerts", data=body, headers=ORIGIN)
    assert again.status_code == 409 and "An alert with this name already exists" in again.text
    blank = web_client.post("/alerts", data={**body, "name": "  "}, headers=ORIGIN)
    assert blank.status_code == 400 and "Enter a name of 1 to 120 characters." in blank.text


def test_account_and_pricing_mention_free_alerts(web_client: TestClient, noncommercial: None) -> None:
    _register(web_client)
    account = web_client.get("/account")
    assert '<a href="/alerts">Manage alerts</a>' in account.text and "(free)" in account.text
    pricing = web_client.get("/pricing")
    assert 'Email alerts on up to <span class="tnum">10</span> saved searches' in pricing.text
    assert 'href="/alerts">Alerts</a>' in pricing.text, "the nav links to alerts"


# ------------------------------------------------------------------------------- view -> query
def test_saved_search_query_mirrors_the_list() -> None:
    query = saved_search_query(
        "proposal",
        {
            "technology": "solar",
            "cursor": "x",
            "sort": "-capacity_mw",
            "updated_since": "2026-01-01",
            "layers": "plants",
        },
    )
    assert query["technology"] == "solar"
    assert query["lifecycle_state"].startswith("announced")
    assert not {"cursor", "sort", "updated_since", "layers"} & set(query)
    with_withdrawn = saved_search_query("proposal", {"include_withdrawn": "1"})
    assert "withdrawn" in with_withdrawn["lifecycle_state"]
    assert saved_search_query("opportunity", {"kind": "grant"}) == {"kind": "grant", "status": "open"}


def test_save_alert_href_drops_the_page_cursor() -> None:
    from starlette.datastructures import QueryParams

    from web.page import save_alert_href

    href = save_alert_href("proposal", QueryParams("technology=solar&cursor=abc&q=a b"))
    assert href == "/alerts/new?entity=proposal&technology=solar&q=a+b"


def test_the_maps_default_placement_is_not_a_watch() -> None:
    assert "placement" not in saved_search_query("proposal", {"placement": "exact,region"}, origin="map")
    assert saved_search_query("proposal", {"placement": "exact"}, origin="map")["placement"] == "exact"
    assert saved_search_query("proposal", {"placement": "exact,region"})["placement"] == "exact,region"


def test_suggested_names_and_filter_rows() -> None:
    assert suggested_name("proposal", {"kind": "storage", "jurisdiction": "US-TX"}) == "Storage in US-TX"
    assert (
        suggested_name("proposal", {"capacity_mw[gte]": "50", "capacity_mw[lte]": "500"})
        == "Proposals, 50-500 MW"
    )
    assert suggested_name("opportunity", {"status": "open"}) == "Opportunities"
    assert ("Capacity at least (MW)", "50") in filter_rows({"capacity_mw[gte]": "50"})


# ---------------------------------------------------------------------------------- page views
def test_a_detail_page_view_is_counted_without_identifiers(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    slug = seed_proposal(factory)
    assert web_client.get(f"/proposals/{slug}").status_code == 200
    assert ui_event_props(factory, "page.viewed") == [{"page_type": "proposal"}]


def test_crawlers_and_missing_pages_are_not_counted(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    slug = seed_proposal(factory)
    web_client.get(f"/proposals/{slug}", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    web_client.get(f"/proposals/{slug}", headers={"purpose": "prefetch"})
    web_client.get("/proposals/no-such-proposal")
    assert ui_event_props(factory, "page.viewed") == []


def test_a_browser_cannot_post_a_page_view_through_the_relay(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    resp = web_client.post("/api/ui-events", json={"name": "page.viewed", "props": {"page_type": "asset"}})
    assert resp.status_code == 202
    assert web_client.post("/api/ui-events", json={"name": "alert.created", "props": {}}).status_code == 202
    assert ui_event_props(factory, "page.viewed") == []
    assert ui_event_props(factory, "alert.created") == []


def test_about_tiers_describe_alerts_truthfully(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    about = web_client.get("/about")
    assert "does not expose them yet" not in about.text
    assert 'Saved searches and alerts are managed at <a href="/alerts">Alerts</a>' in about.text
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", False)
    about = web_client.get("/about")
    assert "email alerts are free to any registered reader" in about.text


# ------------------------------------------------------------- lockout recovery pages (QA-5)
def test_login_offers_recovery(web_client: TestClient) -> None:
    page = web_client.get("/login")
    assert 'href="/forgot-password"' in page.text
    assert 'name="sign_out_other_sessions"' in page.text


def test_forgot_and_reset_password_through_the_site(web_client: TestClient) -> None:
    _register(web_client, "forgetful@example.com", verify=False)
    web_client.cookies.clear()
    sent = web_client.post("/forgot-password", data={"email": "forgetful@example.com"}, headers=ORIGIN)
    assert sent.status_code == 200 and "a reset link is on its way" in sent.text
    link = sent.text.split('<a href="/reset-password?token=')[1].split('"')[0]
    form = web_client.get(f"/reset-password?token={link}")
    assert 'name="token"' in form.text
    done = web_client.post(
        "/reset-password", data={"token": link, "password": "a brand new passphrase"}, headers=ORIGIN
    )
    assert "Your password is changed" in done.text
    again = web_client.post(
        "/reset-password", data={"token": link, "password": "another new passphrase"}, headers=ORIGIN
    )
    assert again.status_code == 400 and 'href="/forgot-password"' in again.text
    signed_in = web_client.post(
        "/login",
        data={"email": "forgetful@example.com", "password": "a brand new passphrase", "next": "/account"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert signed_in.status_code == 303


def test_account_signs_out_other_sessions(web_client: TestClient) -> None:
    _register(web_client, "devices@example.com", verify=False)
    first = web_client.cookies.get("session")
    web_client.cookies.clear()
    web_client.post(
        "/login",
        data={"email": "devices@example.com", "password": PASSWORD, "next": "/account"},
        headers=ORIGIN,
    )
    page = web_client.post("/account/sign-out-others", headers=ORIGIN)
    assert 'Signed out of <span class="tnum">1</span> other session.' in page.text
    web_client.cookies.clear()
    web_client.cookies.set("session", first)
    assert web_client.get("/account", follow_redirects=False).status_code == 303, "the other session is gone"
