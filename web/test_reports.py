""" "Report a problem with this record" (designer audit 2026-09-30 D-6; `web/reports.py`).

Before 2026-10-06 the link was `mailto:?subject=...` with no recipient. These tests drive the site
against the real API in-process (`services.api.app.app` on an in-memory store, the
`web/test_alerts_pages.py` pattern), so a sent report is the `report` task `POST /v1/reports`
creates, and every error state is the API's own answer rendered per docs/31 §6.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from tests.web_alerts_support import memory_sessionmaker, seed_proposal
from web.api_client import ApiClient, ApiResult
from web.app import app as web_app

ORIGIN = {"origin": "http://testserver"}
FRAGMENT = {**ORIGIN, "x-report-fragment": "1"}
SITE_TOKEN = "web-reports-test-token"


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return memory_sessionmaker()


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every JSON body the site POSTs to the API, so the relay itself can be checked."""
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
    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(
        TestClient(api_app, base_url="http://api-internal", headers={"X-Internal-Token": SITE_TOKEN})
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _public_id(client: TestClient, slug: str) -> str:
    page = client.get(f"/proposals/{slug}").text
    return page.split('name="public_id" value="')[1].split('"')[0]


def _form(public_id: str, slug: str, **overrides: str) -> dict[str, str]:
    data = {
        "public_id": public_id,
        "return_path": f"/proposals/{slug}",
        "record_name": "Test Solar",
        "issue_type": "wrong_status",
        "description": "The queue lists this as withdrawn since August; see the CAISO report.",
        "email": "",
        "website": "",
    }
    data.update(overrides)
    return data


def _tasks(factory: sessionmaker[Session]) -> list[Any]:
    with factory() as session:
        return list(
            session.execute(
                text(
                    "SELECT type, subject_type, issue_type, description, contact"
                    " FROM task ORDER BY created_at"
                )
            )
        )


def test_the_record_page_has_a_form_not_a_mailto(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    slug = seed_proposal(factory)
    body = web_client.get(f"/proposals/{slug}").text

    assert "mailto:" not in body
    form = body.split('<details class="report-problem" id="report-problem">')[1].split("</details>")[0]
    assert "Something wrong with this record? Report a problem" in form
    assert 'method="post" action="/report"' in form
    # Every issue type the API accepts, as words, each a labelled radio in a legend'd group.
    for value, words in (
        ("wrong_merge", "Two different projects were merged into this record"),
        ("wrong_status", "The status is wrong"),
        ("wrong_sponsor", "The sponsor or owner is wrong"),
        ("other", "Something else"),
    ):
        assert f'<label for="report-issue-{value}"><input type="radio" id="report-issue-{value}"' in form
        assert words in form
    assert "<legend>What is wrong with" in form
    assert '<textarea id="report-description" name="description" required maxlength="2000"' in form
    assert 'aria-describedby="report-description-hint"' in form
    # The intake's honeypot, hidden from people and from assistive technology.
    assert (
        '<div class="report-problem__trap" aria-hidden="true">' in form
        and 'name="website" tabindex="-1"' in form
    )
    # No captcha widget without a configured site key.
    assert "cf-turnstile" not in form
    assert '<script src="/static/js/report.js' in body


def test_a_report_creates_a_review_task_and_says_what_happens_next(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    slug = seed_proposal(factory)
    public_id = _public_id(web_client, slug)

    resp = web_client.post("/report", data=_form(public_id, slug, email="reader@example.com"), headers=ORIGIN)

    assert resp.status_code == 200
    assert 'role="status"' in resp.text and "Thanks. Your report has reached our editors." in resp.text
    assert "we don&rsquo;t email you unless you gave an address" in resp.text
    assert f'href="/proposals/{slug}"' in resp.text  # the way back, on the no-JavaScript page
    assert 'name="robots" content="noindex, follow"' in resp.text
    rows = _tasks(factory)
    assert len(rows) == 1
    kind, subject, issue, description, contact = rows[0]
    assert (kind, subject, issue) == ("report", "proposal", "wrong_status")
    assert description.startswith("The queue lists this as withdrawn")
    assert "reader@example.com" in str(contact)
    assert sent[0]["website"] == "" and "captcha_token" not in sent[0]


def test_the_script_gets_only_the_fragment(web_client: TestClient, factory: sessionmaker[Session]) -> None:
    slug = seed_proposal(factory)
    resp = web_client.post("/report", data=_form(_public_id(web_client, slug), slug), headers=FRAGMENT)

    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<details") and "<html" not in resp.text
    assert 'id="report-status" role="status" tabindex="-1"' in resp.text


def test_an_invalid_report_keeps_what_was_typed_and_names_the_field(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    slug = seed_proposal(factory)
    resp = web_client.post(
        "/report",
        data=_form(_public_id(web_client, slug), slug, description="", email="not-an-address"),
        headers=FRAGMENT,
    )

    assert resp.status_code == 200
    assert 'role="alert"' in resp.text and "Invalid request." in resp.text
    assert 'Request <span class="mono">' in resp.text  # docs/31 §6: the request id, not a trace
    assert 'id="report-description-error"' in resp.text and 'aria-invalid="true"' in resp.text
    assert 'value="not-an-address"' in resp.text  # nothing the reader typed is lost
    assert 'value="wrong_status" required checked' in resp.text
    assert _tasks(factory) == []


def test_the_honeypot_is_relayed_and_a_bot_is_refused(
    web_client: TestClient, factory: sessionmaker[Session], sent: list[dict[str, Any]]
) -> None:
    slug = seed_proposal(factory)
    resp = web_client.post(
        "/report",
        data=_form(_public_id(web_client, slug), slug, website="http://spam.example"),
        headers=FRAGMENT,
    )

    assert sent[0]["website"] == "http://spam.example"
    assert 'role="alert"' in resp.text and _tasks(factory) == []


def test_the_intake_rate_limit_applies_and_reads_as_words(
    web_client: TestClient, factory: sessionmaker[Session]
) -> None:
    slug = seed_proposal(factory)
    public_id = _public_id(web_client, slug)
    for _ in range(5):
        assert "Thanks." in web_client.post("/report", data=_form(public_id, slug), headers=FRAGMENT).text

    resp = web_client.post("/report", data=_form(public_id, slug), headers=FRAGMENT)

    assert resp.status_code == 429
    assert "Too many reports have come from your connection this hour" in resp.text
    assert len(_tasks(factory)) == 5


def test_a_record_that_is_gone_says_so(web_client: TestClient, factory: sessionmaker[Session]) -> None:
    slug = seed_proposal(factory)
    resp = web_client.post("/report", data=_form("prop_DOESNOTEXIST", slug), headers=FRAGMENT)

    assert resp.status_code == 404
    assert "no longer published" in resp.text


def test_a_cross_site_post_is_refused(web_client: TestClient, factory: sessionmaker[Session]) -> None:
    slug = seed_proposal(factory)
    resp = web_client.post("/report", data=_form(_public_id(web_client, slug), slug))

    assert resp.status_code == 403
    assert _tasks(factory) == []


def test_the_way_back_never_leaves_the_record(web_client: TestClient, factory: sessionmaker[Session]) -> None:
    slug = seed_proposal(factory)
    public_id = _public_id(web_client, slug)
    resp = web_client.post(
        "/report", data=_form(public_id, slug, return_path="https://evil.example/phish"), headers=ORIGIN
    )

    assert "evil.example" not in resp.text
    assert f'href="/proposals/{public_id}"' in resp.text


def test_a_configured_captcha_renders_and_its_token_is_relayed(
    web_client: TestClient,
    factory: sessionmaker[Session],
    sent: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "0x4AAAA-site-key")
    slug = seed_proposal(factory)
    page = web_client.get(f"/proposals/{slug}").text
    assert '<div class="cf-turnstile" data-sitekey="0x4AAAA-site-key"></div>' in page
    assert 'src="https://challenges.cloudflare.com/turnstile/v0/api.js"' in page

    web_client.post(
        "/report",
        data={**_form(_public_id(web_client, slug), slug), "cf-turnstile-response": "token-from-widget"},
        headers=FRAGMENT,
    )

    assert sent[0]["captcha_token"] == "token-from-widget"
