"""The content security policy, ENFORCED, over the site's main pages in Chromium (docs/60 §2).

The policy is read from infra/compose/Caddyfile (the string `security_headers` passes to
`csp_report`/`csp_enforce`, with the basemap URL filled in) and sent as `Content-Security-Policy`
by a thin ASGI wrapper around the real site, which runs as its own uvicorn process on a port the
operating system picks, over a small store built here (the evaluation fixture, sites, one operator).
Each page is loaded with its scripts running; every `securitypolicyviolation` event, every
"Refused to ..." console message and every page error fails the test, with the list.

What the browser cannot reach here is answered offline, as in web/test_e2e.py: MapLibre's two
pinned files from the recorded copies in tests/fixtures/cdn, everything else on another origin
aborted. An aborted request is a network error, not a violation, so third-party scripts that never
run here (pmtiles, the Protomaps basemap, Cloudflare Turnstile) are checked only as far as the
policy allowing their URLs. Skipped without Playwright's Chromium.

The last test proves the report path end to end: a page that breaks the policy reports to
`/csp-report`, and the server logs the directive, the blocked origin and the page's route template.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, Route, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "infra" / "compose" / "Caddyfile"
CHROMIUM_PATH = "/opt/pw-browsers/chromium"
TILE_URL = "https://tiles.example.invalid/basemap.pmtiles"
#: Cloudflare's documented always-pass test site key, so /submit renders the Turnstile widget.
TURNSTILE_TEST_SITE_KEY = "1x00000000000000000000AA"
RECORDED_CDN = {
    "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js": (
        "maplibre-gl-5.24.0.js.gz",
        "45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb",
        "application/javascript",
    ),
    "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css": (
        "maplibre-gl-5.24.0.css.gz",
        "ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732",
        "text/css",
    ),
}
#: Recorded in the page before any of its own scripts run (Playwright injects it past the policy).
COLLECT = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.__csp.push({directive: e.effectiveDirective, blocked: e.blockedURI, source: e.sourceFile,
                     line: e.lineNumber, sample: e.sample, disposition: e.disposition});
});
"""


def policy(tile_url: str = TILE_URL) -> str:
    """The Caddyfile's policy, as Caddy sends it with `MAP_TILE_URL=tile_url`."""
    match = re.search(r'^\s*import csp_\{\$CSP_MODE:report\} "([^"]+)"$', CADDYFILE.read_text(), re.M)
    if match is None:
        raise AssertionError("the policy line moved in infra/compose/Caddyfile")
    return match.group(1).replace("{$MAP_TILE_URL}", tile_url)


SERVER = r"""
import asyncio, logging, os, socket, sys
import uvicorn
from infra.logging_config import configure_logging
configure_logging("INFO", stream=sys.stderr)  # stdout carries the port
from web.app import app as site

POLICY = os.environ["CSP_TEST_POLICY"].encode()
DROP = (b"content-security-policy", b"content-security-policy-report-only", b"reporting-endpoints")

async def enforced(scope, receive, send):
    async def with_policy(message):
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in DROP]
            headers += [(b"content-security-policy", POLICY), (b"reporting-endpoints", b'csp="/csp-report"')]
            message = {**message, "headers": headers}
        await send(message)
    await site(scope, receive, with_policy if scope["type"] == "http" else send)

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1], flush=True)
server = uvicorn.Server(uvicorn.Config(enforced, log_level="warning", access_log=False))
asyncio.run(server.serve(sockets=[sock]))
"""


def _chromium() -> dict[str, Any]:
    """The sandbox's Chromium where it exists, else Playwright's own (CI), as web/test_e2e.py."""
    if os.environ.get("E2E_USE_PLAYWRIGHT_CHROMIUM") == "1":
        return {}
    return {"executable_path": CHROMIUM_PATH} if os.path.exists(CHROMIUM_PATH) else {}


def _have_chromium() -> bool:
    if _chromium():
        return True
    with sync_playwright() as pw:
        return os.path.exists(pw.chromium.executable_path)


def _build_store(path: pathlib.Path) -> dict[str, str]:
    """The evaluation fixture (web/data_loading.py), its sites and one operator with a session.
    `CSP_BROWSER_DB` names an already-loaded SQLite file to use instead (a copy: sites and the
    operator are written into it)."""
    import sqlalchemy as sa

    from services.api.auth import create_session
    from services.db.models import Account, Organization, Proposal, User
    from services.db.session import get_engine, get_sessionmaker, init_db
    from services.ids import public_id
    from services.sites.build import run as build_sites
    from web.data_loading import load_test_database

    engine = get_engine(f"sqlite+pysqlite:///{path}")
    init_db(engine)
    factory = get_sessionmaker(engine)
    if not os.environ.get("CSP_BROWSER_DB"):
        with factory() as session:
            load_test_database(session)
            session.commit()
    build_sites(factory)
    with factory() as session:
        account = Account(public_id="", name="Ops", kind="organization", entitlement="admin")
        session.add(account)
        session.flush()
        account.public_id = public_id("acc", account.id)
        user = User(
            public_id="",
            account_id=account.id,
            email="operator@example.test",
            password_hash="not-a-password",
            name="Operator",
            role="operator",
            auth_provider="password",
        )
        session.add(user)
        session.flush()
        user.public_id = public_id("usr", user.id)
        _row, cookie = create_session(session, user)
        proposal = session.execute(
            sa.select(Proposal.slug).where(Proposal.slug != "").order_by(Proposal.id).limit(1)
        ).scalar_one()
        organization = session.execute(
            sa.select(Organization.public_id).order_by(Organization.id).limit(1)
        ).scalar_one()
        site_id = session.execute(sa.text("SELECT public_id FROM site ORDER BY id LIMIT 1")).scalar()
        session.commit()
    engine.dispose()
    return {"cookie": cookie, "proposal": proposal, "organization": organization, "site": site_id or ""}


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not _have_chromium():
        pytest.skip("no Chromium for Playwright")
    tmp = tmp_path_factory.mktemp("csp-browser")
    db = tmp / "store.db"
    if os.environ.get("CSP_BROWSER_DB"):
        db.write_bytes(pathlib.Path(os.environ["CSP_BROWSER_DB"]).read_bytes())
    facts = _build_store(db)
    log = tmp / "server.log"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{db}",
        "MAP_TILE_URL": TILE_URL,
        "TURNSTILE_SITE_KEY": TURNSTILE_TEST_SITE_KEY,
        "CSP_TEST_POLICY": policy(),
        "PYTHONPATH": str(ROOT),
    }
    env.pop("API_BASE_URL", None)
    with log.open("w") as log_file:
        process = subprocess.Popen(  # noqa: S603 -- fixed interpreter and code, no shell
            [sys.executable, "-c", SERVER],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=log_file,
            text=True,
        )
    try:
        assert process.stdout is not None
        port = int(process.stdout.readline().strip() or 0)
        if not port:
            raise AssertionError(f"the site did not start: {log.read_text()[-3000:]}")
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        import httpx

        while True:
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"the site did not answer: {log.read_text()[-3000:]}")
            time.sleep(0.2)
        yield {"base": base, "log": log, **facts}
    finally:
        process.terminate()
        process.wait(timeout=10)


def _recorded(url: str) -> tuple[bytes, str]:
    name, sha256, content_type = RECORDED_CDN[url]
    body = gzip.decompress((ROOT / "tests" / "fixtures" / "cdn" / name).read_bytes())
    if hashlib.sha256(body).hexdigest() != sha256:
        raise AssertionError(f"recorded copy of {url} changed")
    return body, content_type


def _offline(page: Page, base: str) -> None:
    def handle(route: Route) -> None:
        url = route.request.url
        if url.startswith(base):
            route.continue_()
        elif url in RECORDED_CDN:
            body, content_type = _recorded(url)
            route.fulfill(
                status=200, body=body, content_type=content_type, headers={"Access-Control-Allow-Origin": "*"}
            )
        else:
            route.abort()

    page.route("**/*", handle)


def _violations(page: Page, console: list[str], errors: list[str]) -> list[str]:
    found = [json.dumps(v, sort_keys=True) for v in page.evaluate("() => window.__csp || []")]
    found += [line for line in console if "Content Security Policy" in line or line.startswith("Refused to")]
    found += [f"pageerror: {e}" for e in errors]
    return found


@contextlib.contextmanager
def _browser(site: dict[str, Any], *, operator: bool = False) -> Iterator[Any]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**_chromium())
        context = browser.new_context(base_url=site["base"], viewport={"width": 1440, "height": 900})
        if operator:
            context.add_cookies([{"name": "session", "value": site["cookie"], "url": site["base"]}])
        context.add_init_script(COLLECT)
        try:
            yield context
        finally:
            browser.close()


def _visit(context: Any, base: str, path: str, *, map_page: bool = False) -> list[str]:
    page = context.new_page()
    console: list[str] = []
    errors: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    _offline(page, base)
    response = page.goto(path, wait_until="load")
    if response is None or response.status >= 400:
        raise AssertionError(f"{path}: {response.status if response else 'no response'}")
    if map_page:
        page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()", timeout=20000)
    page.wait_for_timeout(750)
    found = _violations(page, console, errors)
    page.close()
    return found


def _public_pages(site: dict[str, Any]) -> list[tuple[str, bool]]:
    pages = [
        ("/", True),
        ("/proposals", False),
        (f"/proposals/{site['proposal']}", False),
        (f"/organizations/{site['organization']}", False),
        ("/alerts", False),
        ("/submit", False),
        ("/docs/api", False),
        ("/pricing", False),
        ("/login", False),
        ("/organizations", False),
        ("/assets", False),
        ("/privacy", False),
    ]
    if site["site"]:
        pages.append((f"/sites/{site['site']}", False))
    return pages


def test_the_public_pages_break_no_rule_of_the_enforced_policy(site: dict[str, Any]) -> None:
    failures: dict[str, list[str]] = {}
    with _browser(site) as context:
        for path, map_page in _public_pages(site):
            found = _visit(context, site["base"], path, map_page=map_page)
            if found:
                failures[path] = found
    if failures:
        raise AssertionError(json.dumps(failures, indent=2))


def test_the_admin_pages_break_no_rule_of_the_enforced_policy(site: dict[str, Any]) -> None:
    failures: dict[str, list[str]] = {}
    with _browser(site, operator=True) as context:
        page = context.new_page()
        page.goto("/admin", wait_until="load")
        links = page.eval_on_selector_all(
            "a[href^='/admin']", "els => [...new Set(els.map(e => e.getAttribute('href')))]"
        )
        page.close()
        admin = sorted(href for href in links if "?" not in href and href.count("/") <= 3)
        if len(admin) < 5:
            raise AssertionError(f"the operator did not see the admin navigation: {links}")
        for path in ["/admin", *admin]:
            found = _visit(context, site["base"], path)
            if found:
                failures[path] = found
    if failures:
        raise AssertionError(json.dumps(failures, indent=2))


def _report_uri_only(page: Page, base: str, path: str) -> None:
    """Serve `path` with the policy minus `report-to`, so Chromium reports through `report-uri` at
    once. Through `report-to` it batches: none arrived within 90 s here (2026-10-10), too slow for a
    test; that format is covered by web/test_csp_reports.py with the Reporting API's payload."""

    def handle(route: Route) -> None:
        response = route.fetch()
        headers = dict(response.headers)
        headers["content-security-policy"] = headers["content-security-policy"].replace("; report-to csp", "")
        headers.pop("reporting-endpoints", None)
        route.fulfill(response=response, headers=headers)

    page.route(f"{base}{path}", handle)


def test_a_violation_reaches_the_report_endpoint_and_is_logged_without_the_url(site: dict[str, Any]) -> None:
    """The browser's own report of a script the policy refuses, to the real route, as one log line:
    the directive, the blocked origin and the page's route template, not the URLs."""
    path = "/docs/api?secret=do-not-log"
    with _browser(site) as context:
        page = context.new_page()
        _offline(page, site["base"])
        _report_uri_only(page, site["base"], path)  # registered last, so it is matched first
        page.goto(path, wait_until="load")
        page.evaluate(
            "() => { const s = document.createElement('script'); s.src = 'https://evil.example/x.js?k=1';"
            " document.head.appendChild(s); }"
        )
        deadline = time.monotonic() + 20
        line = None
        while line is None and time.monotonic() < deadline:
            for raw in site["log"].read_text().splitlines():
                if '"csp_directive"' in raw:
                    line = json.loads(raw)
            page.wait_for_timeout(250)  # not time.sleep: route handlers (the report's own) run only here
        page.close()
    if line is None:
        raise AssertionError("no report reached /csp-report: " + site["log"].read_text()[-2000:])
    expected = {
        "csp_directive": "script-src-elem",
        "csp_blocked": "https://evil.example",
        "csp_page": "/docs/api",
        "csp_disposition": "enforce",
    }
    if {k: line.get(k) for k in expected} != expected:
        raise AssertionError(line)
    if "do-not-log" in json.dumps(line) or "x.js" in json.dumps(line) or "127.0.0.1" in json.dumps(line):
        raise AssertionError(f"the log line keeps more than the origin and the template: {line}")


def test_the_policy_names_the_inline_script_it_allows() -> None:
    """The hash in the policy is the hash of base.html's one inline script (also derived in
    infra/test_caddyfile.py); here as the browser computes it, over the exact bytes."""
    body = re.search(r"<script>(.*?)</script>", (ROOT / "web" / "templates" / "base.html").read_text(), re.S)
    assert body is not None
    digest = base64.b64encode(hashlib.sha256(body.group(1).encode()).digest()).decode()
    assert f"'sha256-{digest}'" in policy()
