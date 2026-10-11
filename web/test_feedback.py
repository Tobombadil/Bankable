"""The private beta's feedback link and form (`web/beta.py`, `web/feedback.py`; docs/65).

The site runs against the real API in-process on an in-memory store (the `web/test_reports.py`
pattern), with the visitor-address hook production installs (`web/api_client.py`), so a sent form
is the `report` task `POST /v1/reports` creates, the operator reads it through the real admin
screens, and the rate limiter sees the visitor's address exactly as it would behind Caddy.

What the brief asks to be proved, each by name below:
  - the link shows only in beta mode (`SITE_ACCESS=basic`), on every Jinja environment that
    renders `base.html`, and the form answers 404 outside it;
  - a submission reaches the admin review queue, labelled as beta feedback;
  - no IP address is stored, although the API's rate limiter saw it;
  - the page the reader came from is captured as a path, without its query string;
  - the form is axe-clean (in Chromium when Playwright's browser and an axe-core build are present,
    `AXE_CORE_JS`; the structural rules axe applies are also checked without a browser).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, sessionmaker

import services.api.admin_posts as admin_posts
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from tests.conftest import make_account, make_user
from tests.web_alerts_support import memory_sessionmaker
from web.admin.tasks import router as tasks_router
from web.api_client import ApiClient, ApiResult, _attach_visitor_ip
from web.app import app as web_app
from web.beta import came_from, feedback_href, site_path

ORIGIN = {"origin": "http://testserver"}
SITE_TOKEN = "web-feedback-test-token"
VISITOR_IP = "203.0.113.77"  # TEST-NET-3 (RFC 5737): an address that can be nobody's
PASSWORD = "correct horse battery staple"

if not any(getattr(r, "path", None) == "/admin/tasks" for r in web_app.routes):
    web_app.include_router(tasks_router)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return memory_sessionmaker()


@pytest.fixture()
def beta(web_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beta mode, set after `web_client` has cleared whatever the environment held."""
    monkeypatch.setenv("SITE_ACCESS", "basic")


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every JSON body the site POSTs to `/v1/reports`, so the relay itself can be checked."""
    calls: list[dict[str, Any]] = []
    original = ApiClient.post

    def spy(self: ApiClient, path: str, **kwargs: Any) -> ApiResult:
        if path == "/v1/reports":
            calls.append(dict(kwargs.get("json") or {}))
        return original(self, path, **kwargs)

    monkeypatch.setattr(ApiClient, "post", spy)
    return calls


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
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SITE_ACCESS", raising=False)
    api_app.dependency_overrides[get_db] = _override_get_db
    transport = TestClient(api_app, base_url="http://api-internal", headers={"X-Internal-Token": SITE_TOKEN})
    # The hook production's client carries (web/api_client.py::build_client): the visitor's address
    # rides on every call as X-Visitor-IP, which the API believes beside the internal token.
    transport.event_hooks = {"request": [_attach_visitor_ip], "response": []}
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    with TestClient(web_app, client=(VISITOR_IP, 50000), follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "description": "Find every 345 kV substation in ERCOT with more than 1 GW queued.",
        "what_happened": "The grid points list has no voltage filter, so I gave up.",
        "email": "",
        "page_path": "/interconnection-points",
        "website": "",
    }
    data.update(overrides)
    return data


def _tasks(factory: sessionmaker[Session]) -> list[Any]:
    with factory() as session:
        return list(
            session.execute(
                text(
                    "SELECT public_id, type, subject_type, subject_id, issue_type, description, contact,"
                    " pending_record FROM task ORDER BY created_at"
                )
            )
        )


def _every_stored_value(factory: sessionmaker[Session]) -> str:
    """Every row of every table, as text: what "stored" means for the no-IP test."""
    with factory() as session:
        bind = session.get_bind()
        out = []
        for table in inspect(bind).get_table_names():
            out.extend(str(tuple(row)) for row in session.execute(text(f'SELECT * FROM "{table}"')))  # noqa: S608
        return "\n".join(out)


def _hidden_page_path(body: str) -> str:
    match = re.search(r'<input type="hidden" name="page_path" value="([^"]*)">', body)
    assert match, "the form carries no page_path field"
    return match.group(1)


def _beta_link(body: str) -> str | None:
    match = re.search(r'<a class="beta-bar__link" href="([^"]*)"', body)
    return match.group(1).replace("&amp;", "&") if match else None


# ================================================================ link shown only in beta mode
#: One page per Jinja environment that renders base.html: web/page.py, web/legal.py, web/auth.py,
#: web/pricing.py. Each registers the link's global on its own.
PAGES_BY_ENVIRONMENT = ("/about", "/privacy", "/login", "/pricing")


@pytest.mark.parametrize("access", [None, "open", "OPEN", ""])
@pytest.mark.parametrize("path", PAGES_BY_ENVIRONMENT)
def test_no_link_and_no_form_outside_beta_mode(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch, access: str | None, path: str
) -> None:
    if access is not None:
        monkeypatch.setenv("SITE_ACCESS", access)

    page = web_client.get(path)

    assert page.status_code == 200
    assert "beta-bar" not in page.text and "/feedback" not in page.text
    assert web_client.get("/feedback").status_code == 404
    assert web_client.post("/feedback", data=_form(), headers=ORIGIN).status_code == 404


@pytest.mark.usefixtures("beta")
@pytest.mark.parametrize("path", PAGES_BY_ENVIRONMENT)
def test_in_beta_mode_every_page_links_to_the_form_with_its_own_path(
    web_client: TestClient, path: str
) -> None:
    body = web_client.get(path).text

    href = _beta_link(body)
    assert href == f"/feedback?from=%2F{path[1:]}", href
    assert "Tell us what you were trying to do</a>" in body
    # In the header (the banner landmark), above the masthead, so it shows at 400px too.
    header = body.split('<header class="site-header">')[1].split("</header>")[0]
    assert header.index('class="beta-bar"') < header.index('class="masthead')


def test_the_signal_is_the_shared_login_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    request = Request(
        {"type": "http", "method": "GET", "path": "/proposals", "query_string": b"q=x", "headers": []}
    )
    for value, shown in (("basic", True), (" Basic ", True), ("open", False), ("", False), ("basic2", False)):
        monkeypatch.setenv("SITE_ACCESS", value)
        assert (feedback_href(request) is not None) is shown, value
    monkeypatch.delenv("SITE_ACCESS")
    assert feedback_href(request) is None


# ======================================================== path captured without its query string
@pytest.mark.usefixtures("beta")
def test_the_link_carries_the_path_and_never_the_query(web_client: TestClient) -> None:
    body = web_client.get("/about?q=acme+power&utm_source=newsletter#sources").text

    href = _beta_link(body)
    assert href is not None
    query = parse_qs(urlsplit(href).query)
    assert query == {"from": ["/about"]}
    assert "acme" not in href and "utm" not in href


@pytest.mark.usefixtures("beta")
@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("/proposals", "/proposals"),
        ("/proposals?q=wind&state=TX", "/proposals"),
        ("/organizations/acme-power#pipeline", "/organizations/acme-power"),
        ("/reset-password?token=secret", "/reset-password"),
        ("https://evil.example/phish", ""),
        ("//evil.example/phish", ""),
        ("/\\evil.example", ""),
        ("/feedback", ""),
        ("/" + "a" * 600, ""),
    ],
)
def test_the_form_keeps_only_a_bare_site_path(web_client: TestClient, given: str, kept: str) -> None:
    body = web_client.get("/feedback", params={"from": given}).text

    assert _hidden_page_path(body) == kept
    if kept:
        assert f'<span class="submit-ref">{kept}</span>' in body  # the reader is told what is kept
    else:
        assert 'id="feedback-page"' not in body


@pytest.mark.usefixtures("beta")
def test_without_a_link_the_same_origin_referer_path_is_used(web_client: TestClient) -> None:
    same = web_client.get(
        "/feedback", headers={"referer": "http://testserver/assets?asset_type=lng_terminal"}
    )
    other = web_client.get("/feedback", headers={"referer": "https://elsewhere.example/assets"})

    assert _hidden_page_path(same.text) == "/assets"
    assert _hidden_page_path(other.text) == ""


@pytest.mark.usefixtures("beta")
def test_a_query_string_slipped_into_the_post_is_cut_before_it_is_sent(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    resp = web_client.post(
        "/feedback", data=_form(page_path="/proposals?q=sunrise+storage&sponsor_id=org_X"), headers=ORIGIN
    )

    assert resp.status_code == 200
    assert sent[0]["page_path"] == "/proposals"
    stored = _every_stored_value(factory)
    assert "sunrise" not in stored and "sponsor_id" not in stored


def test_site_path_and_came_from_units() -> None:
    from starlette.requests import Request

    assert site_path("/a/b?c#d") == "/a/b"
    assert site_path("/") == "/"
    assert site_path("") is None and site_path(None) is None
    assert site_path("/a b") is None and site_path("/a\x00b") is None
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("beta.example", 443),
            "path": "/feedback",
            "query_string": b"from=%2Fsites%2Fsite_1%3Fx%3D1",
            "headers": [(b"host", b"beta.example")],
        }
    )
    assert came_from(request) == "/sites/site_1"


# ======================================================= submission reaches the admin review queue
def _sign_in_operator(client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(db, account, email="operator@example.com", role="operator", password=PASSWORD)
        db.commit()
    resp = client.post("/login", data={"email": "operator@example.com", "password": PASSWORD}, headers=ORIGIN)
    assert resp.status_code == 303, resp.text


@pytest.mark.usefixtures("beta")
def test_a_submission_reaches_the_admin_queue_labelled_as_beta_feedback(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    resp = web_client.post("/feedback", data=_form(email="tester@example.com"), headers=ORIGIN)

    assert resp.status_code == 200
    assert 'role="status"' in resp.text and "Thanks. Your feedback has reached the team." in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert 'href="/interconnection-points">Back to the page you were on' in resp.text
    # The relay: the report pipeline's endpoint, no record, the beta issue type.
    assert sent == [
        {
            "issue_type": "beta_feedback",
            "description": "Find every 345 kV substation in ERCOT with more than 1 GW queued.",
            "what_happened": "The grid points list has no voltage filter, so I gave up.",
            "email": "tester@example.com",
            "page_path": "/interconnection-points",
            "website": "",
        }
    ]
    [row] = _tasks(factory)
    task_id, kind, subject_type, subject_id, issue, description, contact, pending = row
    assert (kind, subject_type, subject_id, issue) == ("report", None, None, "beta_feedback")
    assert description.startswith("Find every 345 kV substation")
    assert "tester@example.com" in str(contact)
    assert "/interconnection-points" in str(pending) and "no voltage filter" in str(pending)
    assert f'Reference <span class="submit-ref">{task_id}</span>' in resp.text

    # The operator's view: the existing task queue, labelled, and a detail page that reads as feedback.
    _sign_in_operator(web_client, factory)
    queue = web_client.get("/admin/tasks", params={"type": "report", "status": "open"})
    assert queue.status_code == 200
    row_html = queue.text.split(f'href="/admin/tasks/{task_id}"')[0].rsplit("<tr>", 1)[1]
    assert "Beta feedback" in row_html
    assert (
        '<span class="mono">/interconnection-points</span> <span class="admin-muted">(page)</span>'
        in row_html
    )
    detail = web_client.get(f"/admin/tasks/{task_id}")
    assert detail.status_code == 200
    assert "<h1>Beta feedback" in detail.text
    assert "<dt>What they were trying to do</dt><dd>Find every 345 kV substation" in detail.text
    assert 'id="feedback-what-happened">The grid points list has no voltage filter' in detail.text
    assert 'id="feedback-page" class="mono">/interconnection-points</dd>' in detail.text
    assert "None: feedback about the site, not a record" in detail.text
    assert "<h2>Pending record</h2>" not in detail.text
    assert "tester@example.com" in detail.text  # under "Contact (personal data)", admin only


# ======================================================================== privacy: no IP stored
@pytest.mark.usefixtures("beta")
def test_no_ip_address_is_stored_although_the_rate_limiter_saw_it(
    web_client: TestClient,
    factory: sessionmaker[Session],
    sent: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    original = admin_posts.rate_limit_address

    def spy(request: Any) -> str:
        seen.append(original(request))
        return seen[-1]

    monkeypatch.setattr(admin_posts, "rate_limit_address", spy)

    resp = web_client.post("/feedback", data=_form(), headers=ORIGIN)

    assert resp.status_code == 200 and len(_tasks(factory)) == 1
    assert seen == [VISITOR_IP]  # it reached the API, so its absence below is not vacuous
    assert VISITOR_IP not in _every_stored_value(factory)
    assert "testclient" not in _every_stored_value(factory)


@pytest.mark.usefixtures("beta")
def test_an_email_is_stored_only_when_given(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    web_client.post("/feedback", data=_form(email="", what_happened="", page_path=""), headers=ORIGIN)

    assert sent == [{"issue_type": "beta_feedback", "description": _form()["description"], "website": ""}]
    with factory() as session:
        [(contact, pending)] = list(session.execute(text("SELECT contact, pending_record FROM task")))
    # SQL NULL or a JSON null (how the JSON column stores Python's None), never an empty object.
    assert contact in (None, "null") and pending in (None, "null")


# =================================================================== protection, as /report has
@pytest.mark.usefixtures("beta")
def test_the_shared_intake_rate_limit_applies_and_reads_as_words(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    for _ in range(5):
        assert "Thanks." in web_client.post("/feedback", data=_form(), headers=ORIGIN).text

    resp = web_client.post("/feedback", data=_form(), headers=ORIGIN)

    assert resp.status_code == 429
    assert "Too many messages have come from your connection this hour" in resp.text
    assert "Find every 345 kV substation" in resp.text  # what was typed is kept
    assert len(_tasks(factory)) == 5


@pytest.mark.usefixtures("beta")
def test_a_cross_site_post_is_refused(web_client: TestClient, factory: sessionmaker[Session]) -> None:
    resp = web_client.post("/feedback", data=_form(), headers={"origin": "https://evil.example"})

    assert resp.status_code == 403
    assert _tasks(factory) == []


@pytest.mark.usefixtures("beta")
def test_the_honeypot_is_relayed_and_a_bot_is_refused(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    resp = web_client.post("/feedback", data=_form(website="http://spam.example"), headers=ORIGIN)

    assert sent[0]["website"] == "http://spam.example"
    assert resp.status_code == 422 and 'role="alert"' in resp.text
    assert _tasks(factory) == []


@pytest.mark.usefixtures("beta")
def test_a_configured_captcha_renders_is_required_and_its_token_is_relayed(
    web_client: TestClient, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "0x4AAAA-site-key")
    page = web_client.get("/feedback").text
    assert '<div class="cf-turnstile" data-sitekey="0x4AAAA-site-key"></div>' in page
    assert 'src="https://challenges.cloudflare.com/turnstile/v0/api.js"' in page

    missing = web_client.post("/feedback", data=_form(), headers=ORIGIN)
    assert missing.status_code == 422 and sent == []
    assert 'id="feedback-captcha-error"' in missing.text

    web_client.post(
        "/feedback", data={**_form(), "cf-turnstile-response": "token-from-widget"}, headers=ORIGIN
    )
    assert sent[0]["captcha_token"] == "token-from-widget"


@pytest.mark.usefixtures("beta")
def test_the_page_checks_answers_before_spending_a_request(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    resp = web_client.post(
        "/feedback",
        data=_form(description="   ", email="not-an-address", what_happened="x" * 2001),
        headers=ORIGIN,
    )

    assert resp.status_code == 422 and sent == [] and _tasks(factory) == []
    assert "Nothing was sent: 3 answers need changing." in resp.text
    for fid in ("feedback-description", "feedback-what-happened", "feedback-email"):
        assert f'<li><a href="#{fid}">' in resp.text
        assert f'aria-describedby="{fid}-hint {fid}-error" aria-invalid="true"' in resp.text
    assert 'value="not-an-address"' in resp.text  # nothing the reader typed is lost


# ============================================================================ accessibility
@pytest.mark.usefixtures("beta")
def test_the_form_meets_the_structural_rules_without_a_browser(web_client: TestClient) -> None:
    """What axe checks from markup alone: one h1, every control labelled, every described-by id
    present, ids unique, no inline style (the CSP forbids it), the honeypot out of the a11y tree."""
    body = web_client.get("/feedback", params={"from": "/proposals"}).text

    assert body.count("<h1>") == 1 and '<html lang="en">' in body
    assert "<title>Tell us what you were trying to do — Infraque</title>" in body
    assert 'name="robots" content="noindex, nofollow"' in body
    assert "style=" not in body
    ids = re.findall(r'\bid="([^"]+)"', body)
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
    for control in re.findall(r"<(?:input|textarea|select)\b[^>]*>", body):
        if 'type="hidden"' in control or 'type="search"' in control:
            continue
        control_id = re.search(r'\bid="([^"]+)"', control)
        assert control_id and f'<label for="{control_id.group(1)}"' in body, control
    for described in re.findall(r'aria-describedby="([^"]+)"', body):
        for target in described.split():
            assert f'id="{target}"' in body, target
    assert '<div class="intake-form__trap" aria-hidden="true">' in body
    assert 'aria-current="page">Tell us what you were trying to do</a>' in body
    assert '<textarea id="feedback-description" name="description" rows="5" maxlength="2000" required' in body


#: As web/test_e2e.py: the sandbox's pre-installed Chromium, else the one `playwright install` put in place.
CHROMIUM_PATH = "/opt/pw-browsers/chromium"


@pytest.mark.usefixtures("beta")
@pytest.mark.parametrize("state", ["empty", "errors", "sent"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_form_is_axe_clean_in_chromium(
    web_client: TestClient, state: str, scheme: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """axe-core's WCAG 2.0-2.2 A/AA rules over the rendered page, light and dark, at 400px, in each
    state a reader sees. The page is loaded with the site's own stylesheet so contrast is real.
    Skipped without Playwright or without an axe-core build (`AXE_CORE_JS`, a path to `axe.min.js`):
    the repository vendors no copy of axe."""
    sync_api = pytest.importorskip("playwright.sync_api")
    axe_path = Path(os.environ.get("AXE_CORE_JS", "") or "/nonexistent")
    if not axe_path.is_file():
        pytest.skip("set AXE_CORE_JS to an axe-core axe.min.js to run axe here")
    if state == "empty":
        body = web_client.get("/feedback", params={"from": "/proposals"}).text
    elif state == "errors":
        body = web_client.post("/feedback", data=_form(description="", email="nope"), headers=ORIGIN).text
    else:
        body = web_client.post("/feedback", data=_form(), headers=ORIGIN).text
    css = web_client.get("/static/css/styles.css").text
    # Inline the stylesheet and drop scripts and font preloads: the page is checked offline.
    page_html = re.sub(r'<link rel="(?:stylesheet|preload)"[^>]*>', "", body)
    page_html = re.sub(r"<script\b[^>]*src=[^>]*></script>", "", page_html)
    page_html = page_html.replace("</head>", f"<style>{css}</style></head>")

    with sync_api.sync_playwright() as pw:
        launch = {"executable_path": CHROMIUM_PATH} if os.path.exists(CHROMIUM_PATH) else {}
        browser = pw.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": 400, "height": 900}, color_scheme=scheme)
            page.set_content(page_html, wait_until="load")
            page.add_script_tag(path=str(axe_path))
            result = page.evaluate(
                "async () => (await axe.run(document, {runOnly: {type: 'tag', values:"
                " ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']}})).violations"
                ".map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target.join(' '))}))"
            )
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        finally:
            browser.close()
    assert result == [], result
    assert overflow is False, "the page scrolls sideways at 400px"
