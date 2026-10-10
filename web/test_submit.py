""" "Submit a project" (`web/submit.py`; US-1001, docs/30 §4.5).

Two halves. Most tests drive the site against a fake `Transport` (the `web/test_head_requests.py`
pattern, duplicated because no `web/test_*.py` imports another), so every API answer the page must
render -- 202, a 400 validation problem, a captcha refusal, 429, an unreachable API -- is exact and
the body the page relays can be read back. The last tests drive the real API in-process on an
in-memory store (the `web/test_reports.py` pattern), so the body is proven against the endpoint
itself: it creates one private `intake_proposal` task and nothing public, the honeypot refuses a
bot, and the shared intake limit turns into a 429 page that keeps what was typed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from services.api import admin_posts
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from tests.web_alerts_support import memory_sessionmaker
from web import submit
from web.api_client import ApiClient
from web.app import app as web_app
from web.sitemaps import SITEMAP_STATIC_PATHS

REPO_ROOT = Path(__file__).resolve().parent.parent
ORIGIN = {"origin": "http://testserver"}
SITE_KEY = "0x4AAAA-test-site-key"
INTERNAL_TOKEN = "web-submit-test-token"

HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}, {"value": "unknown"}, {"value": "gas_cc"}, {"value": "other"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}
ACCEPTED: dict[str, Any] = {
    "task_id": "task_01JC0FFEE0000000000000000",
    "status": "pending_review",
    "status_url": "https://example.invalid/status/task_01JC0FFEE0000000000000000",
    "privacy_notice_url": "https://example.invalid/legal/privacy",
    "request_id": "req_accepted",
}


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


class FakeTransport:
    """Canned responses keyed by path; every call is recorded. `raise_on` makes one path behave
    like an API that does not answer at all."""

    def __init__(self, responses: Mapping[str, tuple[int, Any]], raise_on: str | None = None) -> None:
        self.responses = dict(responses)
        self.raise_on = raise_on
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url == self.raise_on:
            raise httpx.ConnectError("connection refused")
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass

    def intake_posts(self) -> list[dict[str, Any]]:
        return [dict(call[2]) for call in self.calls if call[:2] == ("POST", "/v1/intake/proposals")]


def _transport(intake: tuple[int, Any] = (202, ACCEPTED), raise_on: str | None = None) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals": (200, _list_env([])),
            "/v1/opportunities": (200, _list_env([])),
            "/v1/intake/proposals": intake,
        },
        raise_on=raise_on,
    )


@pytest.fixture(autouse=True)
def _no_site_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as test_client:
        yield test_client
    for key in ("api_client", "lag_days_default"):
        web_app.state.__dict__.pop(key, None)


def _install(transport: FakeTransport) -> FakeTransport:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _form(**overrides: str) -> dict[str, str]:
    """A complete, valid submission, as the browser posts it."""
    data = {
        "project_name": "Red Mesa Solar",
        "sponsor_name": "Mesa Renewables LLC",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": "150",
        "storage_mwh": "",
        "lifecycle_state": "filed",
        "description": "Fenced site west of the substation.\r\nQueue position since March.",
        "jurisdiction": "us-tx",
        "state": "Texas",
        "county": "Pecos",
        "identifiers.queue_ids.iso": "ERCOT",
        "identifiers.queue_ids.id": "26INR0123",
        "identifiers.eia_plant_id": "",
        "identifiers.ferc_dockets": "",
        "contact.name": "Dana Whitfield",
        "contact.email": "dana@example.com",
        "consent": "true",
        "website": "",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _main(html: str) -> str:
    return html.split('<main id="main">')[1].split("</main>")[0]


def _schema() -> dict[str, Any]:
    spec = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    schemas: dict[str, Any] = spec["components"]["schemas"]
    return schemas


# ============================================================================== the empty form
def test_the_form_renders_every_field_labelled_and_indexable(client: TestClient) -> None:
    _install(_transport())

    resp = client.get("/submit")

    assert resp.status_code == 200
    body = resp.text
    assert "<title>Submit a project — Infraque</title>" in body
    assert '<meta name="robots" content="index, follow">' in body
    assert '<link rel="canonical" href="http://testserver/submit">' in body
    main = _main(body)
    assert '<form class="intake-form" method="post" action="/submit" novalidate>' in main
    for name in (*submit.TEXT_FIELDS, *submit.CHECKBOX_FIELDS):
        field_id = submit.FIELD_IDS[name]
        assert f'name="{name}"' in main, name
        assert f'<label for="{field_id}">' in main, name
        assert f'id="{field_id}"' in main, name
    # Required answers are marked for assistive technology; optional ones say so in the label.
    assert 'name="project_name" type="text" value="" required' in main
    assert '<label for="submit-county">County <span class="intake-form__optional">(optional)</span>' in main
    # Hints are tied to their inputs.
    assert 'aria-describedby="submit-jurisdiction-hint"' in main
    # The honeypot the API checks, hidden from people and assistive technology, out of the tab order.
    trap = main.split('<div class="intake-form__trap" aria-hidden="true">')[1].split("</div>")[0]
    assert 'name="website" tabindex="-1" autocomplete="off"' in trap
    # Consent links the privacy notice that exists on this site.
    assert 'I agree to the <a href="/privacy">privacy notice</a>' in main
    # Status definitions are one link away; "unknown" is offered last for a submitter who cannot say.
    assert 'href="/methodology#lifecycle"' in main
    assert '<option value="unknown">Unknown</option>' in main
    # No error or status state on an empty form.
    assert 'role="alert"' not in main and 'role="status"' not in main


def test_technology_options_come_from_the_register_and_survive_an_unanswered_api(client: TestClient) -> None:
    _install(_transport())
    main = _main(client.get("/submit").text)
    select = main.split('name="technology"')[1].split("</select>")[0]
    assert '<option value="">Not stated</option>' in select
    assert '<option value="gas_cc">Gas, combined cycle</option>' in select
    assert 'value="unknown"' not in select  # the empty choice already says "Not stated"
    assert select.index('value="solar"') < select.index('value="other"')  # Other last

    broken = _transport()
    broken.responses["/v1/meta/vocabularies"] = (503, {"title": "Service unavailable"})
    _install(broken)
    resp = client.get("/submit")
    assert resp.status_code == 200
    assert '<option value="other">Other</option>' in _main(resp.text)


def test_field_names_and_choices_match_the_intake_schema() -> None:
    schemas = _schema()
    request = schemas["IntakeProposalRequest"]
    properties = request["properties"]
    nested = {
        "contact": schemas["IntakeContact"]["properties"],
        "identifiers": schemas["ProposalIdentifiers"]["properties"],
    }
    for name in (*submit.TEXT_FIELDS, *submit.CHECKBOX_FIELDS):
        head, _, rest = name.partition(".")
        assert head in properties, name
        if rest:
            assert rest.split(".")[0] in nested[head], name
    # Every required key is either a field on the form or one the page fills in itself.
    rendered = {name.split(".")[0] for name in (*submit.TEXT_FIELDS, *submit.CHECKBOX_FIELDS)}
    assert set(request["required"]) <= rendered | {submit.CAPTCHA_FIELD}
    # The choices are the schema's enums and the API's own tuples, nothing more or less.
    assert list(submit.PROPOSAL_KINDS) == schemas["ProposalKind"]["enum"] == list(admin_posts.PROPOSAL_KINDS)
    assert set(submit.LIFECYCLE_CHOICES) == set(schemas["LifecycleState"]["enum"])
    assert set(submit.LIFECYCLE_CHOICES) == set(admin_posts.LIFECYCLE_STATES)
    assert submit.JURISDICTION_RE.pattern == properties["jurisdiction"]["pattern"]
    assert submit.EMAIL_RE.pattern == admin_posts._EMAIL_RE.pattern
    assert (
        submit.NAME_MAX == properties["project_name"]["maxLength"] == properties["sponsor_name"]["maxLength"]
    )
    assert submit.NAME_MAX == schemas["IntakeContact"]["properties"]["name"]["maxLength"]
    assert submit.DESCRIPTION_MAX == properties["description"]["maxLength"]
    iso_param = yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))[
        "components"
    ]["parameters"]["Iso"]["description"]
    for token in submit.QUEUE_OPERATORS:
        assert f"`{token}`" in iso_param, token


def test_the_turnstile_widget_appears_only_with_a_site_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(_transport())
    without = _main(client.get("/submit").text)
    assert "cf-turnstile" not in without and "challenges.cloudflare.com" not in without

    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE_KEY)
    with_key = _main(client.get("/submit").text)
    assert f'<div class="cf-turnstile" data-sitekey="{SITE_KEY}"></div>' in with_key
    assert (
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
        in with_key
    )
    # The widget sits inside the form, so its token is posted with the answers.
    form = with_key.split('<form class="intake-form"')[1].split("</form>")[0]
    assert "cf-turnstile" in form


def test_the_list_pages_link_the_form_and_the_sitemap_lists_it(client: TestClient) -> None:
    _install(_transport())
    for path in ("/proposals", "/opportunities"):
        header = client.get(path).text.split('<div class="page-header">')[1].split("</div>")[0]
        assert '<a href="/submit">Submit it for review</a>' in header, path
    assert "/submit" in SITEMAP_STATIC_PATHS


# ===================================================================================== success
def test_a_valid_submission_is_relayed_as_the_schema_body_and_says_what_happens_next(
    client: TestClient,
) -> None:
    transport = _install(_transport())

    resp = client.post("/submit", data=_form(), headers=ORIGIN)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    [sent] = transport.intake_posts()
    assert sent == {
        "project_name": "Red Mesa Solar",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 150.0,
        "storage_mwh": None,
        "jurisdiction": "US-TX",  # upper-cased to the schema's pattern
        "state": "Texas",
        "county": "Pecos",
        "lifecycle_state": "filed",
        "sponsor_name": "Mesa Renewables LLC",
        "contact": {"name": "Dana Whitfield", "email": "dana@example.com"},
        # CRLF from the browser becomes one line break, as the browser counted it.
        "description": "Fenced site west of the substation.\nQueue position since March.",
        "public_opt_in": False,
        "consent": True,
        "consent_version": submit.CONSENT_VERSION,
        "captcha_token": submit.NO_WIDGET_TOKEN,  # no widget rendered, and the API requires one
        "website": "",
        "identifiers": {"queue_ids": [{"iso": "ERCOT", "id": "26INR0123"}]},
    }
    body = resp.text
    assert "<title>Submission received — Infraque</title>" in body
    assert '<meta name="robots" content="noindex, follow">' in body
    main = _main(body)
    assert 'role="status"' in main and "Thanks. Your submission is with our editors." in main
    assert '<span class="submit-ref">task_01JC0FFEE0000000000000000</span>' in main
    assert "Until then it appears nowhere" in main
    assert "You did not allow publication, so the record stays private" in main
    assert "<form" not in main  # the form is replaced by the next steps
    # Nothing the API returned that this site does not serve is linked.
    assert "/status/" not in main and "/legal/privacy" not in main


def test_opting_in_says_an_editor_may_publish(client: TestClient) -> None:
    transport = _install(_transport())

    resp = client.post("/submit", data=_form(public_opt_in="true"), headers=ORIGIN)

    assert transport.intake_posts()[0]["public_opt_in"] is True
    assert "You allowed publication, so an editor may then make the record public" in resp.text


def test_identifiers_are_shaped_as_the_resolver_reads_them(client: TestClient) -> None:
    transport = _install(_transport())

    client.post(
        "/submit",
        data=_form(
            **{
                "identifiers.queue_ids.iso": "",
                "identifiers.queue_ids.id": "Q-1187",
                "identifiers.eia_plant_id": "57701",
                "identifiers.ferc_dockets": "ER24-1234-000",
            }
        ),
        headers=ORIGIN,
    )
    client.post(
        "/submit",
        data=_form(**{"identifiers.queue_ids.iso": "", "identifiers.queue_ids.id": ""}),
        headers=ORIGIN,
    )

    first, second = transport.intake_posts()
    assert first["identifiers"] == {
        "queue_ids": [{"iso": "", "id": "Q-1187"}],  # an id without an operator keeps an empty iso
        "eia_plant_id": "57701",
        "ferc_dockets": ["ER24-1234-000"],
    }
    assert "identifiers" not in second


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("87.5", 87.5), ("0", 0.0), (" 1200 ", 1200.0), (".5", 0.5)],
)
def test_capacity_numbers_are_sent_as_numbers(client: TestClient, typed: str, expected: float) -> None:
    transport = _install(_transport())
    client.post("/submit", data=_form(capacity_mw=typed), headers=ORIGIN)
    assert transport.intake_posts()[0]["capacity_mw"] == expected


# ======================================================================== validation, in page
def test_every_problem_is_listed_at_once_and_nothing_typed_is_lost(client: TestClient) -> None:
    transport = _install(_transport())
    form = _form(
        project_name="",
        capacity_mw="1,200 MW",
        storage_mwh="-5",
        jurisdiction="Texas",
        **{"identifiers.eia_plant_id": "EIA-57701", "contact.email": "dana at example"},
        consent="",
    )

    resp = client.post("/submit", data=form, headers=ORIGIN)

    assert resp.status_code == 422
    assert transport.intake_posts() == []  # the hourly intake allowance is not spent on this
    body = resp.text
    assert "<title>Check your answers: submit a project — Infraque</title>" in body
    main = _main(body)
    summary = main.split('id="submit-status"')[1].split("</div>")[0]
    assert 'role="alert"' in main.split("<form")[0]
    assert "Nothing was sent: 7 answers need changing." in summary
    links = re.findall(r'<li><a href="#([a-z-]+)">', summary)
    assert links == [  # page order, each linked to its input
        "submit-project-name",
        "submit-capacity-mw",
        "submit-storage-mwh",
        "submit-jurisdiction",
        "submit-identifiers-eia-plant-id",
        "submit-contact-email",
        "submit-consent",
    ]
    # Each field says what is wrong in words, tied to the input and marked invalid.
    assert (
        '<p class="field__error" id="submit-capacity-mw-error"><span class="visually-hidden">Error: </span>'
        in main
    )
    assert 'aria-describedby="submit-capacity-mw-hint submit-capacity-mw-error" aria-invalid="true"' in main
    assert "Enter a number of megawatts, like 150 or 87.5, without units or commas." in main
    assert "Enter a country code, or a country and state code, like US or US-TX." in main
    consent = main.split('id="submit-consent" name="consent"')[1].split(">")[0]
    assert 'aria-describedby="submit-consent-error" aria-invalid="true"' in consent
    # What was typed comes back, valid or not.
    assert 'value="1,200 MW"' in main and 'value="Texas"' in main and 'value="dana at example"' in main
    assert 'value="Mesa Renewables LLC"' in main and 'value="26INR0123"' in main
    assert '<option value="generation" selected>' in main and '<option value="filed" selected>' in main
    assert '<option value="ERCOT" selected>ERCOT</option>' in main
    assert (
        "Fenced site west of the substation." in main.split('name="description"')[1].split("</textarea>")[0]
    )
    # The honeypot is never echoed back.
    assert 'id="submit-website" name="website" tabindex="-1" autocomplete="off">' in main


def test_long_answers_are_refused_with_their_length(client: TestClient) -> None:
    transport = _install(_transport())
    resp = client.post(
        "/submit",
        data=_form(project_name="x" * 201, description="y" * 2001),
        headers=ORIGIN,
    )
    assert resp.status_code == 422 and transport.intake_posts() == []
    assert "Shorten this to 200 characters or fewer (it has 201)." in resp.text
    assert "Shorten the description to 2,000 characters or fewer (it has 2,001)." in resp.text


def test_a_2000_character_description_with_line_breaks_is_accepted(client: TestClient) -> None:
    transport = _install(_transport())
    line = "z" * 99
    description = "\r\n".join([line] * 20)  # 20 x 99 + 19 breaks = 1,999 as the browser counts it
    resp = client.post("/submit", data=_form(description=description), headers=ORIGIN)
    assert resp.status_code == 200
    assert len(transport.intake_posts()[0]["description"]) == 1999


def test_a_queue_chosen_without_its_id_is_asked_for(client: TestClient) -> None:
    transport = _install(_transport())
    resp = client.post(
        "/submit",
        data=_form(**{"identifiers.queue_ids.id": "", "identifiers.queue_ids.iso": "ISONE"}),
        headers=ORIGIN,
    )
    assert resp.status_code == 422 and transport.intake_posts() == []
    assert "Enter the project&#39;s ISO-NE queue position ID" in resp.text


def test_a_tampered_choice_is_refused_in_page(client: TestClient) -> None:
    transport = _install(_transport())
    resp = client.post("/submit", data=_form(kind="spaceport", lifecycle_state="vibes"), headers=ORIGIN)
    assert resp.status_code == 422 and transport.intake_posts() == []
    assert (
        "Choose what kind of project it is." in resp.text and "Choose the project&#39;s status." in resp.text
    )


# ========================================================================== the API's answers
def test_an_api_validation_problem_lands_on_its_field_with_the_request_id(client: TestClient) -> None:
    problem = {
        "type": "https://example.invalid/problems/validation_error",
        "title": "Invalid request",
        "status": 400,
        "detail": "must look like 'US' or 'US-TX'",
        "errors": [{"field": "jurisdiction", "message": "must look like 'US' or 'US-TX'"}],
        "request_id": "req_badjur",
    }
    _install(_transport(intake=(400, problem)))

    resp = client.post("/submit", data=_form(), headers=ORIGIN)

    assert resp.status_code == 422
    main = _main(resp.text)
    assert "Invalid request. Nothing was sent: 1 answer needs changing." in main
    assert '<li><a href="#submit-jurisdiction">Jurisdiction: Enter a country code' in main
    assert 'Request <span class="submit-ref">req_badjur</span>' in main
    assert 'id="submit-jurisdiction" name="jurisdiction" type="text" value="us-tx"' in main
    assert 'aria-invalid="true"' in main
    assert 'value="Red Mesa Solar"' in main and 'value="dana@example.com"' in main


def test_a_field_the_form_does_not_render_is_still_said(client: TestClient) -> None:
    problem = {
        "title": "Invalid request",
        "errors": [{"field": "sponsor_org_id", "message": "unknown organization"}],
    }
    _install(_transport(intake=(400, problem)))
    main = _main(client.post("/submit", data=_form(), headers=ORIGIN).text)
    assert '<li><a href="#submit-status">Submission: unknown organization</a></li>' in main


def test_a_refused_captcha_keeps_the_answers_and_asks_for_the_check_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE_KEY)
    problem = {
        "title": "Invalid request",
        "errors": [{"field": "captcha_token", "message": "captcha verification failed"}],
        "request_id": "req_captcha",
    }
    transport = _install(_transport(intake=(400, problem)))

    resp = client.post(
        "/submit", data={**_form(), "cf-turnstile-response": "token-from-the-widget"}, headers=ORIGIN
    )

    assert transport.intake_posts()[0]["captcha_token"] == "token-from-the-widget"
    assert resp.status_code == 422
    main = _main(resp.text)
    summary = main.split('id="submit-status"')[1].split("</div>")[0]
    assert '<li><a href="#submit-captcha">Check that you are not a robot: The check that you are' in summary
    assert '<p class="field__error" id="submit-captcha-error">' in main
    # A fresh widget, since a Turnstile token is single-use; and every answer still there.
    assert f'<div class="cf-turnstile" data-sitekey="{SITE_KEY}"></div>' in main
    assert 'value="Red Mesa Solar"' in main and '<option value="solar" selected>' in main
    assert 'name="consent" type="checkbox" value="true" required checked' in main


def test_a_missing_widget_token_is_caught_before_the_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE_KEY)
    transport = _install(_transport())

    resp = client.post("/submit", data=_form(), headers=ORIGIN)

    assert resp.status_code == 422 and transport.intake_posts() == []
    assert "Complete the check that you are not a robot, then send again." in resp.text


def test_the_rate_limit_is_a_429_page_that_keeps_the_answers(client: TestClient) -> None:
    problem = {
        "title": "Rate limit exceeded",
        "detail": "More than 5 submissions in the current hour from this IP.",
        "request_id": "req_limited",
    }
    _install(_transport(intake=(429, problem)))

    resp = client.post("/submit", data=_form(public_opt_in="true"), headers=ORIGIN)

    assert resp.status_code == 429
    assert resp.headers["cache-control"] == "no-store"
    main = _main(resp.text)
    status = main.split('id="submit-status"')[1].split("</div>")[0]
    assert "Rate limit exceeded." in status
    assert "Too many submissions have come from your connection this hour, so nothing was sent." in status
    assert 'Request <span class="submit-ref">req_limited</span>' in status
    assert 'value="Red Mesa Solar"' in main and 'value="26INR0123"' in main
    assert 'name="public_opt_in" type="checkbox" value="true" checked' in main


def test_an_unreachable_api_is_a_502_page_that_keeps_the_answers(client: TestClient) -> None:
    _install(_transport(raise_on="/v1/intake/proposals"))

    resp = client.post("/submit", data=_form(), headers=ORIGIN)

    assert resp.status_code == 502
    main = _main(resp.text)
    assert "The submission service is unavailable." in main
    assert "Nothing was sent. What you typed is kept below" in main
    assert 'value="Red Mesa Solar"' in main
    assert "Traceback" not in resp.text


def test_a_cross_site_post_is_refused(client: TestClient) -> None:
    transport = _install(_transport())
    resp = client.post("/submit", data=_form())
    assert resp.status_code == 403
    assert transport.intake_posts() == []


# ================================================================= against the real endpoint
@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return memory_sessionmaker()


@pytest.fixture()
def real_client(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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

    default_limiter.reset()
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.setenv("API_INTERNAL_TOKEN", INTERNAL_TOKEN)
    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(
        TestClient(api_app, base_url="http://api-internal", headers={"X-Internal-Token": INTERNAL_TOKEN})
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()
    web_app.state.__dict__.pop("api_client", None)
    default_limiter.reset()


def _tasks(factory: sessionmaker[Session]) -> list[Any]:
    with factory() as session:
        return list(
            session.execute(text("SELECT type, status, contact, pending_record, public_opt_in FROM task"))
        )


def test_the_real_endpoint_takes_the_body_and_makes_one_private_task(
    real_client: TestClient, factory: sessionmaker[Session]
) -> None:
    resp = real_client.post(
        "/submit", data=_form(public_opt_in="true", **{"identifiers.eia_plant_id": "57701"}), headers=ORIGIN
    )

    assert resp.status_code == 200, resp.text
    assert "Thanks. Your submission is with our editors." in resp.text
    reference = re.search(r'<span class="submit-ref">(task_[A-Za-z0-9]+)</span>', resp.text)
    assert reference is not None
    [(kind, status, contact, pending, opted_in)] = _tasks(factory)
    assert (kind, status, bool(opted_in)) == ("intake_proposal", "open", True)
    assert "dana@example.com" in str(contact)
    pending_text = str(pending)
    assert "Red Mesa Solar" in pending_text and "57701" in pending_text and "US-TX" in pending_text
    # Nothing public exists until an editor approves it (US-1001 AC2).
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM proposal")).scalar() == 0


def test_the_real_honeypot_refuses_a_bot(real_client: TestClient, factory: sessionmaker[Session]) -> None:
    resp = real_client.post("/submit", data=_form(website="http://spam.example"), headers=ORIGIN)

    assert resp.status_code == 422
    assert "Submission: This submission could not be accepted." in resp.text
    assert _tasks(factory) == []


def test_the_real_intake_limit_turns_into_a_429_page(
    real_client: TestClient, factory: sessionmaker[Session]
) -> None:
    for n in range(admin_posts.INTAKE_RATE_LIMIT):
        assert (
            real_client.post("/submit", data=_form(project_name=f"Project {n}"), headers=ORIGIN).status_code
            == 200
        )

    resp = real_client.post("/submit", data=_form(project_name="One too many"), headers=ORIGIN)

    assert resp.status_code == 429
    assert "Too many submissions have come from your connection this hour" in resp.text
    assert 'value="One too many"' in resp.text
    assert len(_tasks(factory)) == admin_posts.INTAKE_RATE_LIMIT
