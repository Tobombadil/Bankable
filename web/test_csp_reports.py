"""`POST /csp-report` (web/csp_reports.py): both report formats, what one log line keeps and what it
never keeps, the bounds and the rate limit. And FastAPI's own docs pages, off outside development
on both apps (docs/60 §2, 2026-10-10)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from web import csp_reports
from web.app import app

ROOT = Path(__file__).resolve().parents[1]
#: What Chromium sends to `report-uri` (captured from web/test_csp_browser.py's run, 2026-10-10).
CSP_REPORT = {
    "csp-report": {
        "document-uri": "https://example.test/proposals/solar-farm-prop_ABC123?state=US-TX&token=secret",
        "referrer": "https://example.test/?q=private-search",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "original-policy": "default-src 'self'; report-uri /csp-report",
        "disposition": "enforce",
        "blocked-uri": "https://evil.example/x.js?k=private",
        "line-number": 12,
        "source-file": "https://example.test/static/js/map.js?v=abc",
        "status-code": 200,
        "script-sample": "alert('private')",
    }
}
#: The Reporting API's batch (https://www.w3.org/TR/reporting-1/, CSP3 `CSPViolationReportBody`).
REPORTS_JSON = [
    {
        "type": "csp-violation",
        "age": 10,
        "url": "https://admin.example.test/admin/users/usr_PRIVATE1?x=1",
        "user_agent": "Mozilla/5.0 (private)",
        "body": {
            "documentURL": "https://admin.example.test/admin/users/usr_PRIVATE1?x=1",
            "effectiveDirective": "style-src-attr",
            "blockedURL": "inline",
            "disposition": "report",
            "sample": "color:red",
            "originalPolicy": "default-src 'self'",
            "statusCode": 200,
        },
    },
    {"type": "deprecation", "url": "https://example.test/", "body": {"id": "x"}},
    {
        "type": "csp-violation",
        "url": "https://example.test/nowhere/abc@example.com",
        "body": {
            "documentURL": "https://example.test/nowhere/abc@example.com",
            "effectiveDirective": "img-src",
            "blockedURL": "data",
            "disposition": "report",
        },
    },
]
PRIVATE = (
    "secret",
    "private",
    "token",
    "PRIVATE1",
    "abc@example.com",
    "x.js",
    "ABC123",
    "testclient",
    "alert(",
)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(csp_reports, "BUDGET", csp_reports.ReportBudget())
    with TestClient(app) as c:
        c.cookies.set("session", "a-session-cookie-never-logged")
        yield c


def _post(client: TestClient, body: Any, content_type: str) -> Any:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post(csp_reports.REPORT_PATH, content=raw, headers={"content-type": content_type})


def _lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "web.csp_reports" and hasattr(r, "csp_directive")]


def _everything(record: logging.LogRecord) -> str:
    return json.dumps({k: str(v) for k, v in vars(record).items()}) + record.getMessage()


def test_a_report_uri_report_is_one_line_with_the_directive_the_origin_and_the_route(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="web.csp_reports")
    response = _post(client, CSP_REPORT, "application/csp-report")
    assert response.status_code == 204 and response.content == b""
    [line] = _lines(caplog)
    assert line.levelno == logging.WARNING
    assert (line.csp_directive, line.csp_blocked, line.csp_page, line.csp_disposition) == (  # type: ignore[attr-defined]
        "script-src-elem",
        "https://evil.example",
        "/proposals/{slug}",
        "enforce",
    )
    text = _everything(line)
    for private in (*PRIVATE, "a-session-cookie-never-logged", "referrer", "user_agent"):
        assert private not in text, private


def test_a_reporting_api_batch_logs_each_csp_violation_and_nothing_else(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="web.csp_reports")
    response = _post(client, REPORTS_JSON, "application/reports+json")
    assert response.status_code == 204
    lines = _lines(caplog)
    got = [(r.csp_directive, r.csp_blocked, r.csp_page, r.csp_disposition) for r in lines]  # type: ignore[attr-defined]
    assert got == [
        ("style-src-attr", "inline", "/admin/users/{user_id}", "report"),
        ("img-src", "data", "unmatched", "report"),
    ]
    for line in lines:
        text = _everything(line)
        for private in PRIVATE:
            assert private not in text, private


@pytest.mark.parametrize(
    ("blocked", "logged"),
    [
        ("inline", "inline"),
        ("eval", "eval"),
        ("", "none"),
        ("https://cdn.example.com/a/b.js?token=1#x", "https://cdn.example.com"),
        ("https://user:pw@Host.Example:8443/p?q", "https://host.example:8443"),
        ("wss://socket.example/feed", "wss://socket.example"),
        ("data:image/png;base64,AAAA", "data:"),
        ("blob:https://example.test/uuid", "blob:"),
        ("chrome-extension://abcdef/content.js", "chrome-extension:"),
        ("<script>alert(1)</script>", "other"),
    ],
)
def test_the_blocked_resource_is_reduced_to_its_origin_or_keyword(blocked: str, logged: str) -> None:
    assert csp_reports.blocked_origin(blocked) == logged


def test_a_csp2_violated_directive_is_cut_to_its_name() -> None:
    assert csp_reports.directive_name("style-src 'self' https://x") == "style-src"
    assert csp_reports.directive_name("<b>") == "unknown"


def test_the_route_template_prefers_the_literal_route(client: TestClient) -> None:
    class _Req:
        app = client.app

    req: Any = _Req()
    assert (
        csp_reports.page_template(req, "https://admin.example.test/admin/sources/new") == "/admin/sources/new"
    )
    assert (
        csp_reports.page_template(req, "https://admin.example.test/admin/sources/x.y")
        == "/admin/sources/{source_id}"
    )
    assert csp_reports.page_template(req, "https://example.test/") == "/"
    assert csp_reports.page_template(req, "not a url") == "unmatched"


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (CSP_REPORT, "application/json", 415),
        (CSP_REPORT, "text/plain", 415),
        (b"{not json", "application/csp-report", 400),
        (b"x" * (csp_reports.MAX_BODY_BYTES + 1), "application/csp-report", 413),
        ({"unexpected": 1}, "application/csp-report", 204),
        ({"type": "csp-violation"}, "application/reports+json", 204),
    ],
)
def test_what_is_not_a_report_logs_nothing(
    client: TestClient, caplog: pytest.LogCaptureFixture, body: Any, content_type: str, status: int
) -> None:
    caplog.set_level(logging.INFO, logger="web.csp_reports")
    assert _post(client, body, content_type).status_code == status
    assert _lines(caplog) == []


def test_a_batch_is_read_up_to_its_limit(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="web.csp_reports")
    batch = [REPORTS_JSON[0]] * (csp_reports.MAX_REPORTS_PER_REQUEST + 5)
    assert _post(client, batch, "application/reports+json").status_code == 204
    assert len(_lines(caplog)) == csp_reports.MAX_REPORTS_PER_REQUEST


def test_one_client_is_limited_per_minute_and_others_still_report() -> None:
    now = [0.0]
    budget = csp_reports.ReportBudget(per_client=3, total=5, window_s=60, clock=lambda: now[0])
    assert [budget.take("198.51.100.7") for _ in range(4)] == [(True, False)] * 3 + [(False, True)]
    assert budget.take("198.51.100.7") == (False, False), "one warning a window"
    assert budget.take("203.0.113.9") == (True, False) and budget.take("203.0.113.9") == (True, False)
    assert budget.take("192.0.2.1") == (False, False), "the total is spent"
    now[0] = 61.0
    assert budget.take("198.51.100.7") == (True, False), "a new window"
    assert all("198.51" not in key for key in budget._counts), "addresses are not kept"


def test_over_the_limit_the_answer_is_still_204_with_one_warning(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csp_reports, "BUDGET", csp_reports.ReportBudget(per_client=2, total=100))
    caplog.set_level(logging.INFO, logger="web.csp_reports")
    statuses = {_post(client, CSP_REPORT, "application/csp-report").status_code for _ in range(5)}
    assert statuses == {204}
    assert len(_lines(caplog)) == 2
    warnings = [r for r in caplog.records if "rate limit" in r.getMessage()]
    assert len(warnings) == 1


def test_the_route_is_not_part_of_the_site_schema() -> None:
    assert csp_reports.REPORT_PATH not in app.openapi()["paths"]


def test_the_caddyfile_policy_reports_to_this_route() -> None:
    caddyfile = (ROOT / "infra" / "compose" / "Caddyfile").read_text()
    assert f"report-uri {csp_reports.REPORT_PATH}; report-to csp" in caddyfile
    assert f'`csp="{csp_reports.REPORT_PATH}"`' in caddyfile


# ------------------------------------------------------------------ FastAPI's own docs pages
_PROBE = """
import json
from fastapi.testclient import TestClient
from services.api.app import app as api
from web.app import app as site
out = {}
for name, application in (("api", api), ("web", site)):
    with TestClient(application) as client:
        out[name] = {path: client.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")}
print(json.dumps(out))
"""


def _docs_status(environment: str) -> dict[str, dict[str, int]]:
    env = {**os.environ, "ENVIRONMENT": environment, "DATABASE_URL": "sqlite+pysqlite:///:memory:"}
    env.update(
        SESSION_SECRET="test-only-session-secret-0123456789abcdef",
        PLATFORM_POSTURE="noncommercial",
        API_INTERNAL_TOKEN="test-only-internal-token",
        PYTHONPATH=str(ROOT),
    )
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and code, no shell
        [sys.executable, "-c", _PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    status: dict[str, dict[str, int]] = json.loads(result.stdout.strip().splitlines()[-1])
    return status


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_outside_development_neither_app_serves_the_framework_docs_pages(environment: str) -> None:
    """They load an unpinned script and Google Fonts the policy refuses, and describe routes, not
    the contract. The API keeps `/openapi.json`, which /docs/api links; the site serves none."""
    status = _docs_status(environment)
    assert status["api"] == {"/docs": 404, "/redoc": 404, "/openapi.json": 200}, status
    assert status["web"] == {"/docs": 404, "/redoc": 404, "/openapi.json": 404}, status


def test_in_development_both_apps_still_serve_them() -> None:
    status = _docs_status("dev")
    assert status["api"] == {"/docs": 200, "/redoc": 200, "/openapi.json": 200}, status
    assert status["web"] == {"/docs": 200, "/redoc": 200, "/openapi.json": 200}, status
