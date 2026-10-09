"""Playwright end-to-end for free alerts (owner decision 2026-09-30; `web/alerts.py`): register,
verify, save a list view as an alert, then list, pause, resume and delete it -- at desktop width and
at 400px, where the page must not scroll sideways (docs/31 §3, D-32).

Same pattern as `web/test_e2e.py` (a real uvicorn subprocess driven by Chromium), with its own
server: `PLATFORM_POSTURE=noncommercial`, an empty store (the flow needs no records), and no
`RESEND_API_KEY`, so every email is a dry run and the verification link comes from the account
page's development notice. Port: `E2E_ALERTS_PORT`, else one below `E2E_PORT`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import sync_playwright

from tests.web_alerts_support import init_empty_database
from web.test_e2e import E2E_PORT, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_ALERTS_PORT") or str(E2E_PORT - 1))
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-alerts.db"
SHOTS = Path(os.environ["E2E_ALERTS_SHOTS"]) if os.environ.get("E2E_ALERTS_SHOTS") else None


@pytest.fixture(scope="module")
def alerts_server() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    database_url = f"sqlite+pysqlite:///{DB_PATH}"
    init_empty_database(database_url)
    env = dict(os.environ, DATABASE_URL=database_url, PLATFORM_POSTURE="noncommercial")
    for name in ("API_BASE_URL", "RESEND_API_KEY", "FREE_ALERT_CAP"):
        env.pop(name, None)
    # The server's log goes to a file, never an unread pipe: a full 64 KiB pipe buffer blocks the
    # server's next write, so every later request (and SIGTERM's graceful shutdown) hangs.
    server_log = DB_PATH.with_suffix(".server.log").open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv; the port is an int
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(BASE_URL + "/health")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server_log.close()


def _shot(page: Any, name: str) -> None:
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _no_sideways_scroll(page: Any) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"page scrolls sideways by {overflow}px"


@pytest.mark.parametrize(("label", "viewport"), [("desktop", (1440, 900)), ("narrow", (400, 850))])
def test_register_save_list_pause_resume_delete(
    alerts_server: Any, label: str, viewport: tuple[int, int]
) -> None:
    email = f"e2e-{label}@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        page.route("https://fonts.gstatic.com/**", lambda route: route.abort())

        page.goto(BASE_URL + "/alerts")
        assert page.get_by_text("Sign in to save and manage alerts.").is_visible()
        _no_sideways_scroll(page)
        _shot(page, f"{label}-01-alerts-signed-out")

        page.goto(BASE_URL + "/register?next=/account")
        page.fill("#register-email", email)
        page.fill("#register-password", "correct horse battery")
        page.click("form.auth-form button[type=submit]")
        page.wait_for_url("**/account")
        page.get_by_role("button", name="Resend verification email").click()
        page.locator("a[href*='/verify?token=']").first.click()
        page.wait_for_url("**/verify?token=*")

        page.goto(BASE_URL + "/proposals?kind=storage&jurisdiction=US-TX")
        assert page.get_by_role("link", name="Free email alerts").is_visible()
        page.get_by_role("link", name="Save this search as an alert").click()
        page.wait_for_url("**/alerts/new?*")
        assert page.get_by_text("Proposals this alert watches").is_visible()
        assert page.locator("#alert-name").input_value() == "Storage in US-TX"
        _no_sideways_scroll(page)
        _shot(page, f"{label}-02-alerts-new")
        page.get_by_label("Weekly digest").check()
        page.get_by_role("button", name="Save alert").click()
        page.wait_for_url("**/alerts?done=created")

        card = page.locator(".alert-card")
        assert card.count() == 1
        assert "Storage in US-TX" in card.inner_text() and "Weekly digest" in card.inner_text()
        _no_sideways_scroll(page)
        _shot(page, f"{label}-03-alerts-list")

        page.get_by_role("button", name="Pause Storage in US-TX").click()
        page.wait_for_url("**/alerts?done=paused")
        assert "Paused" in page.locator(".alert-card .chip").inner_text()  # chips print words
        _shot(page, f"{label}-04-alerts-paused")
        page.get_by_role("button", name="Resume Storage in US-TX").click()
        page.wait_for_url("**/alerts?done=resumed")
        assert "Active" in page.locator(".alert-card .chip").inner_text()

        page.get_by_role("button", name="Delete Storage in US-TX").click()
        page.wait_for_url("**/alerts?done=deleted")
        assert page.locator(".alert-card").count() == 0
        assert page.get_by_text("No alerts yet.").is_visible()
        _shot(page, f"{label}-05-alerts-empty")
        browser.close()
