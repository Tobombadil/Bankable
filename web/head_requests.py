"""`HEAD` on every `GET` route of the public site, in one place.

FastAPI's `APIRoute` registers only the methods it is given, so until 2026-10-07 every page answered
`HEAD` with `405 Method Not Allowed` (link checkers, social unfurlers and uptime monitors send
`HEAD`). The fix is this one ASGI middleware rather than `HEAD` added to each route: a `HEAD` is
served as the `GET` it stands for, and the body is dropped on the way out. The status and every
header, `Content-Length` included, are therefore the `GET`'s own by construction (RFC 9110 §9.3.2).

Why not add `HEAD` to the routes' `methods`: on FastAPI 0.141 an included router is matched through
internal `_IncludedRouter` contexts, so walking the routes is version-fragile, and a route that lists
`HEAD` also appears in `app.openapi()` as a `HEAD` operation, which the blocking spec-inventory step
in `.github/workflows/ci.yml` would report as undocumented.

What does not change:
  - a path with no `GET` route still refuses: `HEAD` on a POST-only route is routed as `GET` and
    gets the router's own `405` with `Allow: POST`;
  - `NO_HEAD_PATHS` keeps its `405`: those `GET` handlers spend a one-time token through the API, and
    a mail scanner's `HEAD` must not verify an address or unsubscribe a reader;
  - the page-view counter (`web/page.py::count_page_view`) still skips `HEAD`, through
    `is_head_request`, because the handler sees `GET`.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Set on the scope of a `HEAD` request that is being served as `GET`.
HEAD_SCOPE_KEY = "web.head_request"

#: `GET` routes whose handler changes state through the API (`web/auth.py::verify` spends an email
#: verification token; `web/legal.py::unsubscribe` spends an unsubscribe token). `HEAD` stays `405`
#: there. Both pages are `noindex` and absent from the sitemap, so no crawler needs them.
NO_HEAD_PATHS = frozenset({"/verify", "/unsubscribe"})

#: Server extensions that let a response skip `http.response.body` messages (a file sent by path).
#: Removed from the rewritten scope, so every byte passes through `_without_body` and is dropped.
_BODY_BYPASS_EXTENSIONS = ("http.response.pathsend", "http.response.zerocopysend")


class HeadAsGetMiddleware:
    """Serve `HEAD` as `GET` with the body removed. Add once, on the site's app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD" or scope["path"] in NO_HEAD_PATHS:
            await self.app(scope, receive, send)
            return
        extensions = {
            name: value
            for name, value in (scope.get("extensions") or {}).items()
            if name not in _BODY_BYPASS_EXTENSIONS
        }
        get_scope: Scope = {**scope, "method": "GET", "extensions": extensions, HEAD_SCOPE_KEY: True}

        async def _without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app(get_scope, receive, _without_body)


def is_head_request(request: Request) -> bool:
    """True for a `HEAD`, whether or not it is being served as `GET`."""
    return request.method == "HEAD" or bool(request.scope.get(HEAD_SCOPE_KEY))
