"""Playwright end-to-end for a tracked item's data room (docs/19 §3, "Data rooms, as built"; lane
data-rooms, 2026-10-23): register and verify, add an item, open its Data room tab, upload a file into
a folder, see its row, download it (the browser names the file from the link's `download` attribute,
since the response's `Content-Disposition` carries no name), read the access log, and delete it. At
360px (the responsive floor, decision row 2026-10-22) and at 1440px: nothing scrolls sideways.

Same pattern as `web/test_e2e_alerts.py` (a real uvicorn subprocess, in-process API, driven by
Chromium), with its own server: `MY_PIPELINE=on`, `DATA_ROOMS=on`, a local object store under
`web/.data/` and a test key; no `RESEND_API_KEY`, so the verification link comes from the account
page's development notice. Port: `E2E_DATA_ROOM_PORT`, else two above `E2E_PORT`. Screenshots go to
`E2E_DATA_ROOM_SHOTS` when it is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, sync_playwright

from tests.web_alerts_support import init_empty_database
from web.test_e2e import E2E_PORT, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_DATA_ROOM_PORT") or str(E2E_PORT + 2))
BASE_URL = f"http://127.0.0.1:{PORT}"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-data-room.db"
STORE = REPO_ROOT / "web" / ".data" / "e2e-data-room-store"
SHOTS = Path(os.environ["E2E_DATA_ROOM_SHOTS"]) if os.environ.get("E2E_DATA_ROOM_SHOTS") else None
#: base64 of 32 test bytes: obviously not a deployment's key.
TEST_KEY = "ZTJlLWRhdGEtcm9vbS10ZXN0LWtleS0zMi1ieXRlcyE="  # base64 of 32 test bytes  # gitleaks:allow
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


@pytest.fixture(scope="module")
def room_server() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    shutil.rmtree(STORE, ignore_errors=True)
    database_url = f"sqlite+pysqlite:///{DB_PATH}"
    init_empty_database(database_url)
    env = dict(
        os.environ,
        DATABASE_URL=database_url,
        PLATFORM_POSTURE="noncommercial",
        MY_PIPELINE="on",
        DATA_ROOMS="on",
        DATA_ROOM_STORE="local",
        DATA_ROOM_LOCAL_DIR=str(STORE),
        DATA_ROOM_KEY=TEST_KEY,
    )
    for name in ("API_BASE_URL", "RESEND_API_KEY", "DATA_ROOM_SCANNER", "ENVIRONMENT"):
        env.pop(name, None)
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
        shutil.rmtree(STORE, ignore_errors=True)


def _shot(page: Page, name: str) -> None:
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _no_sideways_scroll(page: Page) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"page scrolls sideways by {overflow}px"


def _tap_targets_fit(page: Page, selector: str) -> None:
    for box in (page.locator(selector).nth(i).bounding_box() for i in range(page.locator(selector).count())):
        assert box is not None and box["height"] >= 43.5, box


@pytest.mark.parametrize(("label", "viewport"), [("phone", (360, 800)), ("desktop", (1440, 900))])
def test_upload_list_download_log_and_delete(
    room_server: Any, label: str, viewport: tuple[int, int], tmp_path: Path
) -> None:
    email = f"e2e-room-{label}@example.com"
    file_name = f"Site lease {label} with a long name that has to wrap on a phone.pdf"
    upload = tmp_path / file_name
    upload.write_bytes(PDF)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}, accept_downloads=True
        )
        page = context.new_page()
        page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        page.route("https://fonts.gstatic.com/**", lambda route: route.abort())

        page.goto(BASE_URL + "/register?next=/account")
        page.fill("#register-email", email)
        page.fill("#register-password", "correct horse battery")
        page.click("form.auth-form button[type=submit]")
        page.wait_for_url("**/account")
        page.get_by_role("button", name="Resend verification email").click()
        page.locator("a[href*='/verify?token=']").first.click()
        page.wait_for_url("**/verify?token=*")

        page.goto(BASE_URL + "/my/items/new?type=project")
        page.fill("#item-title", "North Ridge Solar")
        page.locator("form[action='/my/items'] button[type=submit]").first.click()
        page.wait_for_url("**/my/items/prj_*")
        page.get_by_role("link", name="Data room").click()
        page.wait_for_url("**/documents")
        assert page.locator(".my-room-folder").count() == 7
        assert "not scanned for viruses" in page.locator(".my-room-scan").inner_text()
        _no_sideways_scroll(page)
        if label == "phone":
            _tap_targets_fit(page, ".my-room-folder a")
        _shot(page, f"{label}-01-data-room")

        page.select_option("#upload-folder", label="Permits")
        page.set_input_files("#upload-file", str(upload))
        page.get_by_role("button", name="Upload").click()
        page.wait_for_url("**/documents/folders/*?done=uploaded")
        row = page.locator(".my-file")
        assert row.count() == 1
        assert file_name in row.inner_text() and "Not scanned" in row.inner_text()
        _no_sideways_scroll(page)
        _shot(page, f"{label}-02-folder")

        with page.expect_download() as caught:
            row.get_by_role("link", name=file_name).click()
        download = caught.value
        assert download.suggested_filename == file_name
        saved = tmp_path / "saved.pdf"
        download.save_as(str(saved))
        assert saved.read_bytes() == PDF

        page.goto(page.url.split("/documents/")[0] + "/documents/log")
        log = page.locator(".my-log__line")
        assert log.count() == 3  # downloaded, viewed (the folder), uploaded
        assert "downloaded" in log.first.inner_text()
        _no_sideways_scroll(page)
        _shot(page, f"{label}-03-access-log")

        page.go_back()
        page.get_by_role("button", name=f"Delete {file_name}").click()
        page.wait_for_url("**?done=file_deleted")
        assert page.locator(".my-file").count() == 0
        assert page.get_by_text("No files in this folder yet.").is_visible()
        _no_sideways_scroll(page)
        context.close()
        browser.close()
