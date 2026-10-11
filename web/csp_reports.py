"""`POST /csp-report`: where browsers send Content-Security-Policy violation reports (docs/60 §2).

The policy is Caddy's (infra/compose/Caddyfile `security_headers`, report-only or enforced by
`CSP_MODE`) and names this path twice: `report-uri /csp-report` for browsers without the Reporting
API (they POST `application/csp-report`, one report per request) and `report-to csp` with
`Reporting-Endpoints: csp="/csp-report"` for the rest (`application/reports+json`, a batch).

Why the site and not the API: every document the policy governs is a site page (the API serves no
HTML once its own docs pages are off outside development), so a relative path is same-origin on the
site and admin hosts, and Caddy sends it here from the API host too. And on the API every route
spends the caller's public read window (`services/api/ratelimit.py::meter_request`) and is held to
`api/openapi.yaml`; a page full of violations would eat a tester's budget for nothing.

What is kept, and only in the log: one structured line per report naming the directive, the
blocked resource reduced to its origin (or a keyword such as `inline`), the page as its route
template (`/proposals/{slug}`, so no record or account identifier and never a query string), and the
disposition (`enforce` or `report`). Nothing else from the report is read: no full URL, no sample of
the blocked code, no referrer, no user agent. The request's cookies are never read, and its address
only keys the rate limit below, through a keyed hash that lives in memory only.

Bounds: a body over `MAX_BODY_BYTES` is refused unread (413), at most `MAX_REPORTS_PER_REQUEST`
reports of a batch are read, every logged value is cut to a fixed length, and the lines written
are limited per client and in total per minute (`ReportBudget`). A browser ignores the answer, so a
report over the limit is still answered 204, and not logged; one warning per minute says that
reports were dropped. Gate: `/csp-report` is outside the private-beta login (Caddyfile `gate_basic`),
because a browser may send a report without the stored credentials.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import Response

logger = logging.getLogger("web.csp_reports")

router = APIRouter()

REPORT_PATH = "/csp-report"
#: The `report-uri` format (one report) and the Reporting API's (a batch).
CSP_REPORT_TYPE = "application/csp-report"
REPORTS_JSON_TYPE = "application/reports+json"
#: A report is under 2 KB (most of it the policy itself); a batch of a page's reports, a few times that.
MAX_BODY_BYTES = 64 * 1024
MAX_REPORTS_PER_REQUEST = 20
#: Lines logged per minute for one client, and in total. A page that breaks sends one report per
#: blocked resource, so these leave room for a few page views a minute from each tester.
PER_CLIENT_PER_MINUTE = 30
TOTAL_PER_MINUTE = 300
#: The longest directive name is under 30 characters; an origin, under 100; a route template, short.
_MAX_FIELD = 120
_KEYWORD = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_DIRECTIVE = re.compile(r"^[a-z][a-z-]{0,39}$")


class ReportBudget:
    """Fixed one-minute windows: a count per client and one in total. Clients are keyed by an HMAC
    of their address under a key drawn at start-up and never written anywhere, so the table holds
    no address and is meaningless after a restart. It is cleared when the window turns."""

    def __init__(
        self,
        per_client: int = PER_CLIENT_PER_MINUTE,
        total: int = TOTAL_PER_MINUTE,
        window_s: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.per_client, self.total, self.window_s, self.clock = per_client, total, window_s, clock
        self._key = os.urandom(32)
        self._lock = threading.Lock()
        self._window = -1
        self._counts: dict[str, int] = {}
        self._spent = 0
        self._warned = False

    def _client_key(self, address: str) -> str:
        return hmac.new(self._key, address.encode(), hashlib.sha256).hexdigest()[:16]

    def take(self, address: str) -> tuple[bool, bool]:
        """`(allowed, first_refusal_this_window)` for one report from `address`."""
        key = self._client_key(address)
        with self._lock:
            window = int(self.clock() // self.window_s)
            if window != self._window:
                self._window, self._counts, self._spent, self._warned = window, {}, 0, False
            if self._spent >= self.total or self._counts.get(key, 0) >= self.per_client:
                first = not self._warned
                self._warned = True
                return False, first
            self._counts[key] = self._counts.get(key, 0) + 1
            self._spent += 1
            return True, False


BUDGET = ReportBudget()


def _cut(value: str) -> str:
    return value[:_MAX_FIELD]


def blocked_origin(value: object) -> str:
    """The blocked resource as an origin (`https://cdn.example`), a keyword the browser uses
    (`inline`, `eval`, `data`, `blob`), or a bare scheme (`chrome-extension:`). Never a path or query."""
    text = str(value or "").strip()
    if not text:
        return "none"
    lowered = text.lower()
    if _KEYWORD.match(lowered):
        return lowered
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme in ("http", "https", "ws", "wss") and parts.hostname:
        port = f":{parts.port}" if parts.port else ""
        return _cut(f"{scheme}://{parts.hostname.lower()}{port}")
    if scheme and re.match(r"^[a-z][a-z0-9+.-]{0,31}$", scheme):
        return f"{scheme}:"
    return "other"


def directive_name(value: object) -> str:
    """The directive alone. A CSP2 browser's `violated-directive` carries its sources too."""
    first = str(value or "").strip().split(" ", 1)[0].lower()
    return first if _DIRECTIVE.match(first) else "unknown"


@functools.lru_cache(maxsize=4)
def _page_templates(app: Any) -> tuple[tuple[re.Pattern[str], str, int], ...]:
    """Every `GET` route of the site as `(pattern, template, literal characters)`, from
    `app.openapi()` (cached by FastAPI): the one stable list of route templates on this FastAPI
    version, where `app.routes` holds included routers rather than their routes (web/head_requests.py)."""
    out = []
    for template, operations in app.openapi()["paths"].items():
        if "get" not in operations:
            continue
        parts = re.split(r"(\{[^}/]+\})", template)
        pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts)
        literal = sum(len(part) for part in parts if not part.startswith("{"))
        out.append((re.compile(f"^{pattern}$"), template, literal))
    return tuple(out)


def page_template(request: Request, document_url: object) -> str:
    """The route the violating page was served from, as its template, so a record or account id
    in the path is never logged; `unmatched` for a path the site does not route (a 404 page). The
    most literal template wins (`/admin/sources/new` over `/admin/sources/{source_id}`)."""
    path = urlsplit(str(document_url or "")).path or "/"
    best: tuple[int, str] | None = None
    for pattern, template, literal in _page_templates(request.app):
        if pattern.match(path) and (best is None or literal > best[0]):
            best = (literal, template)
    return _cut(best[1]) if best else "unmatched"


def _reports(content_type: str, payload: Any) -> Iterable[Mapping[str, Any]]:
    """Each report's fields under the `report-uri` names, whichever format it came in."""
    if content_type == CSP_REPORT_TYPE:
        body = payload.get("csp-report") if isinstance(payload, dict) else None
        if isinstance(body, dict):
            yield body
        return
    if not isinstance(payload, list):
        return
    for item in payload[:MAX_REPORTS_PER_REQUEST]:
        if not isinstance(item, dict) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if isinstance(body, dict):
            yield {
                "effective-directive": body.get("effectiveDirective"),
                "blocked-uri": body.get("blockedURL"),
                "document-uri": body.get("documentURL") or item.get("url"),
                "disposition": body.get("disposition"),
            }


async def _read_bounded(request: Request) -> bytes | None:
    """The body, or None once it passes `MAX_BODY_BYTES` (the rest is not read)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(REPORT_PATH, include_in_schema=False)
async def csp_report(request: Request) -> Response:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in (CSP_REPORT_TYPE, REPORTS_JSON_TYPE):
        return Response(status_code=415)
    body = await _read_bounded(request)
    if body is None:
        return Response(status_code=413)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    address = request.client.host if request.client else ""
    for report in _reports(content_type, payload):
        allowed, first_refusal = BUDGET.take(address)
        if not allowed:
            if first_refusal:
                logger.warning("csp reports over the rate limit; the rest of this minute's are dropped")
            break
        fields = {
            "csp_directive": directive_name(
                report.get("effective-directive") or report.get("violated-directive")
            ),
            "csp_blocked": blocked_origin(report.get("blocked-uri")),
            "csp_page": page_template(request, report.get("document-uri")),
            "csp_disposition": "enforce" if str(report.get("disposition")) == "enforce" else "report",
        }
        logger.warning(
            "csp violation: %s blocked %s on %s (%s)",
            fields["csp_directive"],
            fields["csp_blocked"],
            fields["csp_page"],
            fields["csp_disposition"],
            extra=fields,
        )
    return Response(status_code=204)
