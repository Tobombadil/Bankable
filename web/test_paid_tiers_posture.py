"""Paid tiers out of the way under the noncommercial posture (owner, 2026-10-10: a private beta
opens with paid tiers off, and pricing and Pro must not distract it; docs/26 §3 precondition (i)).

Under `noncommercial`: "Pricing" is nowhere in the header of any page (each of the four Jinja
environments that render `base.html`: the site, auth, legal and pricing), no page offers an "in Pro"
call to action or a "see what the plans include" link, `/docs/api` does not send the reader to the
plans, and `/pricing` still answers by URL with its posture notice. Under `commercial` every one of
those is as it was, except that Pricing lives in the account menu only (Navigation v2, docs/30 §11.2).

Same wiring as `web/test_pricing.py`: `web.app.app` driven in-process against the real
`services.api.app.app` on a fresh SQLite database, with `GET /v1/health` answering the posture the
way a host with `PLATFORM_POSTURE` set does (`_patch_posture`, duplicated rather than imported:
no `web/test_*.py` imports another).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter
from services.api.auth_routes import get_email_port
from services.api.auth_routes import router as auth_router
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from tests.web_alerts_support import memory_sessionmaker
from web.api_client import ApiClient
from web.app import app as web_app

if not any(getattr(r, "path", None) == "/v1/auth/register" for r in api_app.routes):
    api_app.include_router(auth_router)

NAV_LINK = '<a href="/pricing">Pricing</a>'
#: One page from each Jinja environment that renders `base.html`, plus the pages that carried a
#: paid-tier prompt before 2026-10-10.
PAGES = (
    "/",  # web/page.py
    "/proposals",
    "/opportunities",
    "/about",
    "/docs/api",
    "/organizations",
    "/login",  # web/auth.py
    "/privacy",  # web/legal.py
    "/pricing",  # web/pricing.py
)
#: Words that would ask a beta reader to buy something.
UPSELL = ("in Pro", "Upgrade", "upgrade to", "See what the plans include", "says which plans include")


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # The shared helper, so this module imports nothing from services.db (infra/importlinter.ini).
    factory: sessionmaker[Session] = memory_sessionmaker()

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

    api_app.dependency_overrides[get_db] = _override_get_db
    api_app.dependency_overrides[get_email_port] = lambda: ResendEmailAdapter()
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()
    for key in ("api_client", "lag_days_default", "coverage_data", "build_info"):
        web_app.state.__dict__.pop(key, None)


def _patch_posture(monkeypatch: pytest.MonkeyPatch, value: str, *, free_alerts: bool | None = None) -> None:
    """`GET /v1/health` reports `value` (and its sentence), as a host with `PLATFORM_POSTURE` set
    would. `free_alerts=False` drops the free-alert offer from the answer, so a page cannot lean on
    it to choose its call to action: the posture alone must keep "in Pro" off the page."""
    from services.posture import posture_statement

    real_get = ApiClient.get

    def _with_posture(self: ApiClient, path: str, **kwargs: Any) -> dict[str, Any]:
        body = real_get(self, path, **kwargs)
        if path == "/v1/health":
            body = {**body, "posture": value, "posture_statement": posture_statement(value)}
            if free_alerts is False:
                body.pop("free_alerts", None)
        return body

    monkeypatch.setattr(ApiClient, "get", _with_posture)


def _header(html: str) -> str:
    """Everything the shell draws above `<main>`: the top bar, its account menu, the phone Menu and the
    section bar (Navigation v2, docs/30 §11)."""
    return html.split('<a class="skip-link"', 1)[1].split('<main id="main"', 1)[0]


def _hrefs(fragment: str) -> list[str]:
    return re.findall(r'<a href="([^"]+)"(?: data-signin)?(?: aria-current="(?:page|true)")?>', fragment)


def _top_sections(html: str) -> str:
    return html.split('aria-label="Site sections">', 1)[1].split("</nav>", 1)[0]


def _account_menus(html: str) -> list[str]:
    """The account menu of the top bar and the account rows of the phone Menu."""
    top = re.findall(r'<details class="account-menu">(.*?)</details>', html, re.S)
    rows = html.split('<ul class="site-menu__utils">', 1)[1].split("</ul>", 1)[0]
    return [*top, rows]


@pytest.mark.parametrize("path", PAGES)
def test_noncommercial_leaves_pricing_out_of_the_navigation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _patch_posture(monkeypatch, "noncommercial")
    resp = client.get(path)
    assert resp.status_code == 200, path
    header = _header(resp.text)
    assert 'href="/pricing"' not in header, path
    assert NAV_LINK not in resp.text, path
    # The rest of the navigation is unchanged (docs/30 §11.2): signed out, the top bar holds Market (My
    # pipeline is signed-in only, Funding waits for its page), then Alerts and a plain "Sign in", with no
    # account menu to open; About & sources is a utility, not a section.
    assert _hrefs(_top_sections(resp.text)) == ["/"], path
    assert '<details class="account-menu">' not in header, path
    assert re.search(
        r'<a class="top-bar__link" href="/login" data-signin(?: aria-current="page")?>Sign in</a>', header
    )
    assert 'href="/about"' not in _top_sections(resp.text), path


@pytest.mark.parametrize("path", PAGES)
def test_commercial_keeps_pricing_in_the_account_menu_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _patch_posture(monkeypatch, "commercial")
    resp = client.get(path)
    assert resp.status_code == 200, path
    header = _header(resp.text)
    # Pricing is never a section or a section-bar page (docs/30 §11.2): only the account menu (the top
    # bar's, and the phone Menu's account rows) holds it. On /pricing itself it is marked as the page.
    assert 'href="/pricing"' not in _top_sections(resp.text), path
    bar = header.split('<nav class="section-bar"', 1)[1] if '<nav class="section-bar"' in header else ""
    assert 'href="/pricing"' not in bar, path
    menus = _account_menus(header)
    assert len(menus) == 2, path
    for menu in menus:
        assert re.search(r'<a href="/pricing"(?: aria-current="page")?>Pricing</a>', menu), path
    assert header.count('href="/pricing"') == 2, path


@pytest.mark.parametrize("posture", ["commercial", "noncommercial"])
def test_signed_in_the_account_menu_holds_account_and_sign_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, posture: str
) -> None:
    """Signed in, the account slot opens Account, Pricing (only under `commercial`) and Sign out."""
    _patch_posture(monkeypatch, posture)
    client.cookies.set("session", "opaque")
    header = _header(client.get("/proposals").text)
    [menu] = re.findall(r'<details class="account-menu">(.*?)</details>', header, re.S)
    assert '<summary class="top-bar__link account-menu__toggle">Account</summary>' in menu
    assert _hrefs(menu) == (["/account", "/pricing"] if posture == "commercial" else ["/account"])
    assert '<form method="post" action="/logout"><button type="submit">Sign out</button></form>' in menu
    assert ">Sign in<" not in header


@pytest.mark.parametrize("path", [p for p in PAGES if p != "/pricing"])
def test_noncommercial_pages_carry_no_upgrade_prompt_even_without_the_free_alert_offer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The tier notice used to fall back to "Alerts and API in Pro" whenever `/v1/health` did not
    report free alerts; under `noncommercial` it now says nothing about plans at all."""
    _patch_posture(monkeypatch, "noncommercial", free_alerts=False)
    body = client.get(path).text
    for words in UPSELL:
        assert words not in body, (path, words)
    assert "Pro &rarr;" not in body and 'href="/about#tiers">Alerts and API' not in body


def test_commercial_list_page_still_offers_the_paid_plan_where_free_alerts_are_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_posture(monkeypatch, "commercial", free_alerts=False)
    body = client.get("/proposals").text
    assert "Alerts and API in Pro" in body


def test_about_does_not_sell_the_paid_tier_in_its_opening_line(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_posture(monkeypatch, "noncommercial")
    assert "The paid tier is workflow" not in client.get("/about").text
    _patch_posture(monkeypatch, "commercial")
    assert "The paid tier is workflow" in client.get("/about").text


def test_pricing_stays_reachable_by_url_with_its_posture_notice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_posture(monkeypatch, "noncommercial")
    resp = client.get("/pricing")
    assert resp.status_code == 200
    assert "Paid tiers are not currently offered; every published record is free to read." in resp.text
    assert 'data-posture="noncommercial"' in resp.text
    assert "<h1>Pricing</h1>" in resp.text


def test_api_docs_say_keys_belong_to_the_paid_tiers_without_sending_the_reader_to_them(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_posture(monkeypatch, "noncommercial")
    noncommercial = client.get("/docs/api").text
    assert "API keys belong to the paid tiers, which are not offered" in noncommercial
    assert 'href="/pricing"' not in noncommercial
    _patch_posture(monkeypatch, "commercial")
    commercial = client.get("/docs/api").text
    assert '<a href="/pricing">Pricing</a> says which plans include API access.' in commercial


def test_the_health_answer_is_read_once_per_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The navigation, the tier notice and the free-alert line all ask the posture of one page
    view; `/v1/health` is still read once for all of them (`web/page.py::_request_health`)."""
    calls: list[str] = []
    real_get = ApiClient.get

    def _counting(self: ApiClient, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        return real_get(self, path, **kwargs)

    monkeypatch.setattr(ApiClient, "get", _counting)
    client.get("/proposals")  # warms the per-process caches (lag days, build line)
    calls.clear()
    assert client.get("/proposals").status_code == 200
    assert calls.count("/v1/health") == 1, calls
