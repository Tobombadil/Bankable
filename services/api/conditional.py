"""`ETag` and `If-None-Match` on public GETs (docs/23 §1; api/openapi.yaml `components/headers/ETag`:
"Entity tag on public GETs; honour `If-None-Match` for `304`. Absent on Pro/API responses").
Backend audit 2026-10-07, API-2: no response carried one, so a map pan or a feed poll re-downloaded
the same 150-400 KB.

A pure ASGI middleware, mounted inside the response compression (`services/api/app.py`), so the tag
is computed over the uncompressed body and one tag names the representation whatever the transfer
coding (a weak tag, `W/"..."`, because the gzip and identity bytes differ, RFC 9110 §8.8.1).

Which responses: `GET` on `/v1/*` and `/feeds/*` (not `/feeds/saved/*`, a private feed), status 200,
a JSON, GeoJSON, JSON Feed or RSS body, and no credential on the request (`Authorization` or a session
cookie: those responses are `private, no-store` and carry no tag). Streams (NDJSON bulk, CSV) are
never buffered.

What the tag covers: the body with the three per-request values of the envelope blanked --
`meta.request_id`, `meta.generated_at` and `meta.data_as_of` (which is "now" minus the zero lag) --
because they change on every response while the data does not. Two bodies that differ only there are
the same representation for revalidation; any other byte change is a new tag.

A matching `If-None-Match` (weak comparison, `*` included) gets `304 Not Modified` with the tag and
no body; `services/api/app.py::standard_headers` still adds `Cache-Control`, `Vary` and the rate-limit
headers, and the request is still counted.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Body types that are tagged; anything else (problem+json, NDJSON, CSV, HTML) passes through.
TAGGED_TYPES = frozenset(
    {"application/json", "application/geo+json", "application/feed+json", "application/rss+xml"}
)
_VOLATILE = re.compile(rb'"(?:request_id|generated_at|data_as_of)":"[^"]*"')


def entity_tag(body: bytes) -> str:
    digest = hashlib.blake2b(_VOLATILE.sub(b"", body), digest_size=16).hexdigest()
    return f'W/"{digest}"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def none_match(header: str | None, tag: str) -> bool:
    """RFC 9110 §13.1.2 with weak comparison: true when `If-None-Match` names `tag` or is `*`."""
    if not header:
        return False
    wanted = _opaque(tag)
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or _opaque(candidate) == wanted:
            return True
    return False


def _eligible_request(scope: Scope) -> bool:
    if scope["type"] != "http" or scope["method"] != "GET":
        return False
    path: str = scope["path"]
    if not (path.startswith("/v1/") or (path.startswith("/feeds/") and not path.startswith("/feeds/saved"))):
        return False
    headers = Headers(scope=scope)
    if headers.get("authorization"):
        return False
    cookie = headers.get("cookie") or ""
    return not any(part.strip().startswith("session=") for part in cookie.split(";"))


class ConditionalGetMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _eligible_request(scope):
            await self.app(scope, receive, send)
            return
        if_none_match = Headers(scope=scope).get("if-none-match")
        start: Message | None = None
        passthrough = False
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                ctype = Headers(raw=message["headers"]).get("content-type", "")
                if message["status"] != 200 or ctype.split(";")[0].strip().lower() not in TAGGED_TYPES:
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if passthrough or start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            tag = entity_tag(body)
            if none_match(if_none_match, tag):
                not_modified: dict[str, Any] = {"type": "http.response.start", "status": 304, "headers": []}
                headers = MutableHeaders(scope=not_modified)
                kept = Headers(raw=start["headers"])
                for name in ("cache-control", "vary", "x-request-id"):
                    if (value := kept.get(name)) is not None:
                        headers[name] = value
                headers["etag"] = tag
                await send(not_modified)
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            MutableHeaders(scope=start)["etag"] = tag
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, wrapped)


__all__ = ["TAGGED_TYPES", "ConditionalGetMiddleware", "entity_tag", "none_match"]
