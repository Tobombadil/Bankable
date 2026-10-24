"""Playwright smoke path for Navigation v2 lane V7 (docs/30 §11.4, §11.10): search a company by name and
open its top hit, move between kinds by tab and open a kind's full list; read the glossary and follow an
entry to its list; follow an About figure to its register table; move between the Coverage & methodology
tabs. Then, at 360px, the same pages without sideways scroll (docs/04 D-32, the 360px floor), a Sources
table cell's lines stacked under its label, and the methodology tabs in one row.

Same pattern as `web/test_e2e.py` (a real uvicorn subprocess, in-process API, driven by Chromium), with
its own small store: one company sponsoring sixty projects queued on a public register, and one RFP. Port:
`E2E_REFERENCE_PORT`, else five above `E2E_PORT`.
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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_attribution_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.api.org_stats_refresh import refresh as refresh_org_stats
from tests.web_alerts_support import init_empty_database
from web.test_e2e import E2E_PORT, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_REFERENCE_PORT") or str(E2E_PORT + 5))
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-reference.db"
SHOTS = Path(os.environ["E2E_REFERENCE_SHOTS"]) if os.environ.get("E2E_REFERENCE_SHOTS") else None
PROJECTS = 60


def _build_store(database_url: str) -> None:
    # The schema comes from the shared test helper and the rows go in through a plain SQLAlchemy
    # session, so this module imports nothing from `services.db` (infra/importlinter.ini).
    init_empty_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        licence = make_attribution_licence(session)
        queue = make_public_source(session, licence, id_="us.iso.ercot.gen_queue")
        queue.name, queue.operator = "ERCOT GIS Report", "ERCOT"
        notices = make_public_source(session, licence, id_="us.test.notices")
        notices.name, notices.category, notices.jurisdiction = "Test Notice Register", "procurement", "US"
        sponsor = make_org(session, "Bluebonnet Power LLC")
        for n in range(1, PROJECTS + 1):
            project = make_visible_proposal(
                session, queue, public_id_suffix=str(n), sponsor=sponsor, lifecycle_state="studied"
            )
            project.name_canonical = f"Bluebonnet Solar {n}"
        notice = make_visible_opportunity(session, notices, public_id_suffix="rfp")
        notice.title = "Bluebonnet storage RFP"
        notice.open_at = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        session.commit()
        refresh_org_stats(session)
        session.commit()
    engine.dispose()


@pytest.fixture(scope="module")
def reference_server() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    database_url = f"sqlite+pysqlite:///{DB_PATH}"
    _build_store(database_url)
    env = dict(os.environ, DATABASE_URL=database_url, WEB_DEV_PREVIEW="1", PLATFORM_POSTURE="noncommercial")
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
    assert width <= page.viewport_size["width"], (page.url, width)  # type: ignore[index]


def test_search_glossary_about_and_methodology_tabs(reference_server: Any) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            # Search: the company's name is the top hit, its one primary action opens it.
            page.goto(BASE_URL + "/search?q=bluebonnet+power")
            tabs = page.locator("nav.search-tabs")
            expect(tabs.locator('a[aria-current="page"]')).to_have_text("All")
            expect(tabs).to_contain_text(f"Projects {PROJECTS}")
            hit = page.locator("section.top-hit")
            expect(hit).to_contain_text("Bluebonnet Power LLC")
            _shot(page, "search-desktop")

            # A kind's tab: fifty rows and the full list, which counts exactly what the tab counted.
            page.goto(BASE_URL + "/search?q=bluebonnet")
            page.locator("nav.search-tabs a", has_text="Projects").click()
            page.wait_for_url(re.compile(r"type=projects"))
            expect(page.locator("li.search-row")).to_have_count(50)
            page.get_by_role("link", name=f"Open all {PROJECTS} in Projects").click()
            page.wait_for_url(re.compile(r"/proposals\?q=bluebonnet&lifecycle_state="))
            expect(page.locator("main")).to_contain_text(f"{PROJECTS}")

            hit_page = browser.new_page(viewport={"width": 1440, "height": 900})
            hit_page.goto(BASE_URL + "/search?q=bluebonnet+power")
            hit_page.locator("a.top-hit__open").click()
            hit_page.wait_for_url(re.compile(r"/organizations/bluebonnet-power-llc"))
            hit_page.close()

            # The glossary: an anchored entry is on screen under the sticky top bar, and its link opens
            # the list that shows it.
            page.goto(BASE_URL + "/glossary#interconnection-queue")
            entry = page.locator("#interconnection-queue")
            expect(entry).to_be_in_viewport()
            top = entry.bounding_box()
            assert top is not None and top["y"] >= 48, top  # not under the 48px top bar
            entry.get_by_role("link", name="Projects filed or in study").click()
            page.wait_for_url(re.compile(r"/proposals\?lifecycle_state=filed(%2C|,)studied"))
            expect(page.locator("main")).to_contain_text("Bluebonnet Solar")

            # About: a region's figure opens exactly the table it counts.
            page.goto(BASE_URL + "/about")
            expect(page.locator("h1")).to_have_text("About Inframation")
            figure = page.locator("a.about-reach__figure").first
            counted = figure.locator(".tnum").inner_text()
            figure.click()
            page.wait_for_url(re.compile(r"/about/sources#united-states"))
            table = page.locator("section.sources-region", has=page.locator("#united-states"))
            expect(table.locator("table.sources-table tbody tr")).to_have_count(int(counted))
            expect(page.locator('nav.section-bar a[aria-current="page"]')).to_have_text("Sources & licences")

            # Coverage & methodology: three URL tabs, each marked on its own page.
            page.goto(BASE_URL + "/methodology")
            page.locator("nav.ref-tabs a", has_text="What is not here").click()
            page.wait_for_url(re.compile(r"/methodology/gaps$"))
            expect(page.locator('nav.ref-tabs a[aria-current="page"]')).to_have_text("What is not here")
            page.locator("nav.ref-tabs a", has_text="Status words").click()
            page.wait_for_url(re.compile(r"/methodology/status-words$"))
            expect(page.locator("#state-studied")).to_contain_text("records")

            # An old one-page link lands where its content moved (site.js, docs/30 §11.10), a family of
            # ids by its prefix; the footer and the About bar name the new pages.
            for old, new in (
                ("/about#sources", "/about/sources"),
                ("/methodology#withheld", "/methodology/gaps#withheld"),
                ("/methodology#lifecycle", "/methodology/status-words#lifecycle"),
                ("/methodology#note-solar_queue", "/methodology/gaps#note-solar_queue"),
                ("/methodology#state-studied", "/methodology/status-words#state-studied"),
            ):
                page.goto(BASE_URL + old)
                page.wait_for_url(BASE_URL + new)
            page.goto(BASE_URL + "/methodology#vintage")  # Release dates stayed on this tab
            expect(page.locator("#vintage")).to_be_in_viewport()
            assert page.url == BASE_URL + "/methodology#vintage"
            footer = page.locator("footer.site-footer")
            expect(footer.get_by_role("link", name="Sources & licences")).to_have_attribute(
                "href", "/about/sources"
            )
            page.goto(BASE_URL + "/glossary")
            expect(page.locator('nav.section-bar a[aria-current="page"]')).to_have_text("Glossary")
            page.close()

            # 360px, the floor: nothing scrolls sideways; the search tabs scroll inside their own row.
            phone = browser.new_page(viewport={"width": 360, "height": 800})
            for path in (
                "/search?q=bluebonnet",
                "/search?q=bluebonnet&type=projects",
                "/glossary",
                "/about",
                "/about/sources",
                "/methodology",
                "/methodology/gaps",
                "/methodology/status-words",
            ):
                phone.goto(BASE_URL + path)
                _no_sideways_scroll(phone)
            _shot(phone, "glossary-360")

            # Sources & licences at 360px: below 720px a table cell is a label-and-value row, so a
            # register's name, operator and id stack under the label instead of squeezing side by side and
            # breaking the id mid-word (review of 2026-10-23).
            phone.goto(BASE_URL + "/about/sources")
            cell = phone.locator("table.sources-table td.sources-table__stack").first
            code = cell.locator("code.sources-table__id")
            name_box = cell.locator("a").first.bounding_box()
            id_box = code.bounding_box()
            assert name_box is not None and id_box is not None
            assert id_box["y"] >= name_box["y"] + name_box["height"] - 1, (name_box, id_box)
            size = float(code.evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)"))
            assert id_box["height"] < 2.5 * size, (id_box, size)  # one line

            # Coverage & methodology's tabs keep one row, as the search tabs do: none left alone below.
            phone.goto(BASE_URL + "/methodology")
            boxes = [a.bounding_box() for a in phone.locator("nav.ref-tabs a").all()]
            assert len({round(box["y"]) for box in boxes if box}) == 1, boxes
            _no_sideways_scroll(phone)
            phone.close()
        finally:
            browser.close()
