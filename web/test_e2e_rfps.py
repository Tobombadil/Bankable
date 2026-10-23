"""Playwright smoke path for RFPs & funding when nothing is open (lane rfp-view, 2026-10-23).

The store mirrors production on 2026-10-23 (16 utility RFPs: 14 closed, 1 announced, 1 unknown):
one announced notice, one of unknown status, twelve closed notices with a deadline in the last
year and two closed before that. The default view shows the announced notice, marked "Not open
yet", and below it "Recently closed (last 12 months)" with ten of the twelve; the Due heading still
sorts; "All closed notices" opens the full closed list. Then every notice is moved out of both
views and the page shows its plain empty state. Checked at 1440, 390 and 360px: nothing scrolls
sideways, the list rows and the closed rows are two lines on a phone.

Same pattern as `web/test_e2e_companies.py` (a real uvicorn subprocess, in-process API, driven by
Chromium), with its own small store. Port: `E2E_RFPS_PORT`, else three below `E2E_PORT`.
Screenshots go to `E2E_RFPS_SHOTS` when it is set.
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
import sqlalchemy as sa
from playwright.sync_api import Page, expect, sync_playwright

from services.api.conftest import make_attribution_licence, make_public_source, make_visible_opportunity
from services.db.models import Opportunity
from services.db.session import get_engine, get_sessionmaker, init_db
from web.test_e2e import E2E_PORT, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_RFPS_PORT") or str(E2E_PORT - 3))
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-rfps.db"
SHOTS = Path(os.environ["E2E_RFPS_SHOTS"]) if os.environ.get("E2E_RFPS_SHOTS") else None
RECENT_CLOSED = 12


def _build_store(database_url: str) -> None:
    engine = get_engine(database_url)
    init_db(engine)
    now = dt.datetime.now(dt.UTC)
    with get_sessionmaker(engine)() as session:
        source = make_public_source(session, make_attribution_licence(session), id_="us.test.rfps")
        source.name, source.url = "Test Utility RFP Register", "https://example.org/rfps"
        announced = make_visible_opportunity(
            session, source, public_id_suffix="announced", status="announced"
        )
        announced.title, announced.due_at = "2026 Zero Carbon Draft RFP", None
        announced.open_at = (now + dt.timedelta(days=20)).date()
        unknown = make_visible_opportunity(session, source, public_id_suffix="unknown", status="unknown")
        unknown.title, unknown.due_at = "2024 RFPs", None
        for i in range(RECENT_CLOSED):
            notice = make_visible_opportunity(
                session, source, public_id_suffix=f"closed{i:02d}", status="closed"
            )
            notice.title = f"Closed All-Source RFP {i + 1:02d}"
            notice.due_at = now - dt.timedelta(days=10 + 25 * i)
        for i in range(2):
            notice = make_visible_opportunity(session, source, public_id_suffix=f"old{i}", status="closed")
            notice.title = f"Older Capacity RFP {i + 1}"
            notice.due_at = now - dt.timedelta(days=400 + 30 * i)
        session.commit()
    engine.dispose()


@pytest.fixture(scope="module")
def rfps_server() -> Any:
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
        yield database_url
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


def _is_two_lines(page: Page, first: str, second: str) -> None:
    """`second` starts a line below `first` at the left edge of the row: the row reads as two lines
    (inline cells, so the first box may itself reach into the second line)."""
    a, b = page.locator(first).first.bounding_box(), page.locator(second).first.bounding_box()
    assert a is not None and b is not None
    assert b["y"] >= a["y"] + 12 and abs(b["x"] - a["x"]) < 2, (a, b)


def _check_default_view(page: Page) -> None:
    page.goto(BASE_URL + "/opportunities")
    rows = page.locator("#result-rows table.record-table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Announced")
    expect(rows.first).to_contain_text("Not open yet: opens")
    section = page.locator("section.recent-closed")
    expect(section.locator("h2")).to_have_text("Recently closed (last 12 months)")
    closed_rows = section.locator(".recent-closed__row")
    expect(closed_rows).to_have_count(10)
    expect(closed_rows.first).to_contain_text("Closed All-Source RFP 01")  # the latest deadline first
    expect(section).to_contain_text("10 of 12 closed in the last 12 months.")
    expect(section).not_to_contain_text("Older Capacity RFP")
    assert section.locator(".chip__label", has_text="Open").count() == 0
    assert page.locator("table.record-table").get_by_text("Closed All-Source RFP").count() == 0
    _no_sideways_scroll(page)


def test_the_rfps_page_is_never_blank(rfps_server: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _check_default_view(page)
            expect(page.locator("#f-status")).to_have_value("open,announced")
            # From 720px a closed row is one line: issuer, deadline and source beside the title.
            lead = page.locator(".recent-closed__lead").first.bounding_box()
            meta = page.locator(".recent-closed__meta").first.bounding_box()
            assert lead is not None and meta is not None
            assert (
                meta["x"] > lead["x"] + lead["width"] - 1 and abs(meta["y"] - lead["y"]) < lead["height"]
            ), (
                lead,
                meta,
            )
            _shot(page, "rfps-1440")

            # The Due heading still sorts the list, and the section stays under it.
            page.locator("th.sortable a", has_text="Due").click()
            page.wait_for_url(re.compile(r"sort=-due_at"))
            expect(page.locator("th.sortable", has_text="Due")).to_have_attribute("aria-sort", "descending")
            expect(page.locator("section.recent-closed .recent-closed__row")).to_have_count(10)

            # The full closed list: every closed notice, the latest deadline first, no section.
            page.locator("section.recent-closed a", has_text="All closed notices").click()
            page.wait_for_url(re.compile(r"status=closed"))
            assert "sort=-due_at" in page.url
            rows = page.locator("#result-rows table.record-table tbody tr")
            expect(rows).to_have_count(RECENT_CLOSED + 2)
            expect(rows.first).to_contain_text("Closed All-Source RFP 01")
            expect(page.locator("section.recent-closed")).to_have_count(0)
            expect(page.locator("#f-status")).to_have_value("closed")
            page.close()

            # Phones, the narrower one in the dark theme.
            for width, height, scheme in ((390, 844, "light"), (360, 800, "dark")):
                phone = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
                _check_default_view(phone)
                # Two-line rows: the list's (status and title, then the rest) and the closed ones.
                _is_two_lines(phone, "#result-rows td.cell-title", "#result-rows td.cell-meta:not(.is-empty)")
                _is_two_lines(phone, ".recent-closed__lead", ".recent-closed__meta")
                expect(phone.locator("#filters")).to_be_hidden()  # behind the Filters button
                _shot(phone, f"rfps-{width}-{scheme}")
                phone.close()

            # Nothing open, announced or closed in the last year: the plain empty state.
            engine = get_engine(rfps_server)
            with engine.begin() as conn:
                old = dt.datetime.now(dt.UTC) - dt.timedelta(days=500)
                conn.execute(
                    sa.update(Opportunity)
                    .where(Opportunity.status.in_(("announced", "closed")))
                    .values(status="closed", due_at=old)
                )
            engine.dispose()
            for width, height in ((1440, 900), (360, 800)):
                empty = browser.new_page(viewport={"width": width, "height": height})
                empty.goto(BASE_URL + "/opportunities")
                box = empty.locator(".empty-state.rfp-empty")
                expect(box).to_contain_text("No open RFPs from the issuers we read right now.")
                for label in ("Closed notices", "The issuers we read", "Get an alert when one opens"):
                    expect(box.get_by_role("link", name=label)).to_be_visible()
                expect(empty.locator("section.recent-closed")).to_have_count(0)
                _no_sideways_scroll(empty)
                _shot(empty, f"rfps-empty-{width}")
                empty.close()
        finally:
            browser.close()
