"""Playwright smoke path for the companies index and the RFPs & funding value column (lane pages,
2026-10-23): find a company, sort by a figure, open what a figure counts, and sort notices by value,
at desktop width and at 390px, where nothing scrolls sideways (docs/04 D-32).

Same pattern as `web/test_e2e.py` (a real uvicorn subprocess, in-process API, driven by Chromium),
with its own small store: the ownership fixture group of `services/api/test_org_portfolio.py`
(`Store`), its figures computed by the stats refresh, and two notices whose values the value pass
states, one in euros (converted at the vendored ECB table) and one in dollars. Port:
`E2E_COMPANIES_PORT`, else one above `E2E_PORT`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from services.api.conftest import make_attribution_licence, make_public_source, make_visible_opportunity
from services.api.org_stats_refresh import refresh as refresh_org_stats
from services.api.test_org_portfolio import Store
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.opportunity_value import value_pass
from web.test_e2e import E2E_PORT, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_COMPANIES_PORT") or str(E2E_PORT + 1))
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-companies.db"
SHOTS = Path(os.environ["E2E_COMPANIES_SHOTS"]) if os.environ.get("E2E_COMPANIES_SHOTS") else None


def _build_store(database_url: str) -> None:
    engine = get_engine(database_url)
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        Store(session)  # commits the fixture group
        source = make_public_source(session, make_attribution_licence(session), id_="us.test.notices")
        source.name, source.url = "Test Notice Register", "https://example.org/notices"
        opened = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        for suffix, amount, currency in (("eur", 300_000_000.0, "EUR"), ("usd", 120_000_000.0, "USD")):
            notice = make_visible_opportunity(session, source, public_id_suffix=suffix)
            notice.title = f"Grid tender in {currency}"
            notice.budget_amount, notice.budget_currency, notice.open_at = amount, currency, opened
        session.flush()
        value_pass(session, refresh=False)
        session.commit()
        refresh_org_stats(session)
        session.commit()
    engine.dispose()


@pytest.fixture(scope="module")
def companies_server() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    database_url = f"sqlite+pysqlite:///{DB_PATH}"
    _build_store(database_url)
    env = dict(os.environ, DATABASE_URL=database_url, WEB_DEV_PREVIEW="1")
    env.pop("API_BASE_URL", None)  # in-process API mount, backed by the same SQLite file
    # The server's log goes to a file, never an unread pipe (web/test_e2e.py says why).
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


def _shot(page: Page, name: str) -> None:
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _no_sideways_scroll(page: Page) -> None:
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= page.viewport_size["width"], width  # type: ignore[index]


def _company_names(page: Page) -> list[str]:
    return [n.strip() for n in page.locator("table.org-table tbody a.row-link").all_inner_texts()]


def test_find_sort_and_open_a_company_figure_then_sort_notices_by_value(companies_server: Any) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            # The index opens on whole groups, largest operating fleet first.
            page.goto(BASE_URL + "/organizations")
            heading = page.locator("th.sortable", has_text="Operating GW")
            expect(heading).to_have_attribute("aria-sort", "descending")
            names = _company_names(page)
            assert names[0] == "Fleet Holdings", names
            _shot(page, "companies-desktop")

            # A heading click sorts by that figure and keeps the rest of the view.
            page.locator("th.sortable a", has_text="Owned GW").click()
            page.wait_for_url(re.compile(r"sort=-owned_mw"))
            expect(page.locator("th.sortable", has_text="Owned GW")).to_have_attribute(
                "aria-sort", "descending"
            )

            # The name box searches by name, ignoring case and spaces, and keeps the chosen order.
            page.fill("#f-org-q", "FLEET generation")
            page.press("#f-org-q", "Enter")
            page.wait_for_url(re.compile(r"q=FLEET\+generation"))
            assert "sort=-owned_mw" in page.url
            expect(page.locator("table.org-table tbody tr")).to_have_count(2)
            assert set(_company_names(page)) == {"Fleet Generation A", "Fleet Generation B"}

            # A figure opens the company page's assets, narrowed to what it counted.
            page.goto(BASE_URL + "/organizations")
            first = page.locator("table.org-table tbody tr").first
            link = first.locator("a.figure-link").first
            counted = link.inner_text().strip()
            link.click()
            page.wait_for_url(re.compile(r"/organizations/fleet-holdings\?status="))
            expect(page.locator("#asset-filter-notice")).to_contain_text(f"Showing only the {counted} ")

            # A hand-typed or stale link with values the API refuses shows the list without them.
            response = page.goto(BASE_URL + "/organizations?role=boss&has_pipeline=maybe&min_operating_mw=-5")
            assert response is not None and response.status == 200
            expect(page.locator("table.org-table tbody tr").first).to_be_visible()

            # RFPs & funding: the Value heading sorts by the US-dollar equivalent, largest first.
            page.goto(BASE_URL + "/opportunities?relevance=all")
            page.locator("th.sortable a", has_text="Value").click()
            page.wait_for_url(re.compile(r"sort=-value"))
            first_value = page.locator("table.record-table tbody tr").first.locator("td.cell-value")
            expect(first_value).to_contain_text("€300M")
            expect(first_value).to_contain_text(re.compile(r"≈\s*\$3\d\dM"))  # above the $120M notice
            expect(page.locator(".attribution-line--list")).to_contain_text("European Central Bank")
            page.locator("table.record-table tbody tr").first.locator("a.row-link").click()
            expect(page.locator("#opportunity-value-usd")).to_contain_text(
                "at the ECB euro reference rate of"
            )
            expect(page.locator("#opportunity-value-credit")).to_contain_text("European Central Bank")
            page.close()

            # 390px: two-line rows, the Sort by row, the search box on screen, no sideways scroll.
            phone = browser.new_page(viewport={"width": 390, "height": 844})
            phone.goto(BASE_URL + "/organizations")
            expect(phone.locator("#f-org-q")).to_be_visible()
            expect(phone.locator("#filters")).to_be_hidden()  # behind the Filters button
            row = phone.locator("table.org-table tbody tr").first
            expect(row).to_contain_text("GW operating")
            _no_sideways_scroll(phone)
            _shot(phone, "companies-390")
            phone.goto(BASE_URL + "/opportunities?relevance=all&sort=-value")
            _no_sideways_scroll(phone)
            phone.close()
        finally:
            browser.close()
