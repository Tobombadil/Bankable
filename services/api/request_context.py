"""Per-request plumbing: one request id, commit before the response, and problem+json for any
unhandled error (backend audit 2026-09-30 F3, F13).

**Commit before the response (F3).** `services/api/deps.py::get_db` is a yield dependency, and
FastAPI runs a yield dependency's teardown after the response has been sent. A write committed
there could fail (a serialization failure, a dropped connection, a deferred constraint) while the
client already held a 2xx and the id of a row that was never stored, and a provider webhook
(Stripe, Attio) was told "done" and never retried. `CommitBeforeResponseMiddleware` closes the
gap with one mechanism for every route: every ORM `Session` that begins a transaction or gains an
object while a request is being served is recorded, and when the route emits
`http.response.start` the middleware commits each one that is still open, before that message is
forwarded. A commit that fails raises from there, so the dependency's teardown rolls back and
the client receives a 500 problem+json instead of the route's status. Nothing has been sent yet.

Recording happens through SQLAlchemy session events, not inside `get_db`, so it holds for the
test suite's `dependency_overrides[get_db]` sessions exactly as for production's. A route that
streams its body (bulk NDJSON, CSV exports) keeps its session: committing ends the transaction,
the next read autobegins a new one, and the teardown commits and closes it as before. Row locks
taken by a write (an API key's `last_used_at`, F14) are released at the commit, before the body
streams, rather than after it.

The middleware sits innermost (`services/api/app.py` adds it first), so it sees the route's own
status line before GZip or the rate-limit middleware buffers anything.

**One request id (F13).** `RequestContextMiddleware` is outermost. It mints the id once, keeps it
in `services.api.common.REQUEST_ID` (read by `meta.request_id` and problem bodies) and in
`request.state.request_id`, and stamps `X-Request-Id` on every response it forwards. A 500 is
produced by Starlette's outermost error middleware, outside this one, so
`unhandled_exception_handler` sets the header itself from `request.state`.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.api.common import API_HOST, REQUEST_ID, new_request_id
from services.api.idempotency import (
    STATE_KEY,
    PendingRecord,
    conflict_body,
    is_idempotency_conflict,
    store_response,
)

logger = logging.getLogger(__name__)

#: The sessions the current request has opened a transaction on, or `None` outside a request (and
#: after the response has started: a session first used by a streaming body or a background task
#: commits itself or in the dependency teardown, as before). A list, not a set: `Session` is not
#: meant to be hashed by value, and identity is what matters.
_REQUEST_SESSIONS: ContextVar[list[Session] | None] = ContextVar("request_sessions", default=None)


def _track(session: Session) -> None:
    tracked = _REQUEST_SESSIONS.get()
    if tracked is not None and not any(s is session for s in tracked):
        tracked.append(session)


@event.listens_for(Session, "after_begin")
def _track_on_begin(session: Session, _transaction: Any, _connection: Any) -> None:
    _track(session)


@event.listens_for(Session, "before_attach")
def _track_on_attach(session: Session, _instance: Any) -> None:
    # A route that only `add()`s before returning has not begun a transaction yet.
    _track(session)


def _needs_commit(session: Session) -> bool:
    return session.in_transaction() or bool(session.new or session.dirty or session.deleted)


def commit_request_sessions(tracked: list[Session]) -> None:
    """Commits every recorded session that is still open; the first failure propagates."""
    for session in tracked:
        if _needs_commit(session):
            session.commit()


def _store_and_commit(tracked: list[Session], pending: PendingRecord, start: Message, body: bytes) -> bool:
    """Commits the request's sessions with its idempotency record. False when the record's
    `(scope, key)` already exists: a concurrent request with the same key committed first, so this
    request's transaction, its write included, is rolled back."""
    if _needs_commit(pending.session):
        store_response(pending, int(start["status"]), list(start.get("headers", [])), body)
    try:
        commit_request_sessions(tracked)
    except IntegrityError as exc:
        if not is_idempotency_conflict(exc):
            raise
        for session in tracked:
            session.rollback()
        return False
    return True


class CommitBeforeResponseMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracked: list[Session] = []
        token = _REQUEST_SESSIONS.set(tracked)
        started = False
        #: With an `Idempotency-Key` to honour, the whole response is held until it can be stored
        #: with the write (services/api/idempotency.py); responses to mutating calls are small.
        held: list[Message] = []
        holding = False

        async def send_after_commit(message: Message) -> None:
            nonlocal started, holding
            if message["type"] == "http.response.start" and not started:
                started = True
                pending_record = scope.get("state", {}).get(STATE_KEY)
                if isinstance(pending_record, PendingRecord) and int(message["status"]) < 500:
                    holding = True
                    held.append(message)
                    return
                pending = list(tracked)
                tracked.clear()
                if pending:
                    # Synchronous I/O, so off the event loop, like the route that used the session.
                    await anyio.to_thread.run_sync(commit_request_sessions, pending)
            elif holding and message["type"] == "http.response.body":
                held.append(message)
                if message.get("more_body", False):
                    return
                holding = False
                await _flush_held(held, scope, send, tracked)
                return
            await send(message)

        try:
            await self.app(scope, receive, send_after_commit)
        finally:
            _REQUEST_SESSIONS.reset(token)


async def _flush_held(held: list[Message], scope: Scope, send: Send, tracked: list[Session]) -> None:
    start, body = held[0], b"".join(m.get("body", b"") for m in held[1:])
    pending_record = scope["state"][STATE_KEY]
    sessions = list(tracked)
    tracked.clear()
    stored = await anyio.to_thread.run_sync(_store_and_commit, sessions, pending_record, start, body)
    if stored:
        for message in held:
            await send(message)
        return
    request_id = scope.get("state", {}).get("request_id") or new_request_id()
    payload = json.dumps(conflict_body(request_id, scope.get("path", ""))).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 409,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = REQUEST_ID.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            REQUEST_ID.reset(token)


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", None) or new_request_id()


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no other handler took: `500 internal_error` as RFC 9457 problem+json, with the
    request's id in the body and the header, and nothing about the failure itself (api/openapi.yaml
    `Problem`: "No stack traces, internal ids or source payloads ever appear here"). The exception
    is logged with the id so a customer's support request can be found; Starlette re-raises it
    afterwards for the server's own log."""
    request_id = request_id_of(request)
    logger.error(
        "unhandled_error request_id=%s path=%s error_type=%s",
        request_id,
        request.url.path,
        type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        media_type="application/problem+json",
        headers={"X-Request-Id": request_id},
        content={
            "type": f"{API_HOST}/errors/internal_error",
            "title": "Internal error",
            "status": 500,
            "code": "internal_error",
            "detail": "The request failed. Quote the request id when contacting support.",
            "request_id": request_id,
            "instance": request.url.path,
        },
    )
