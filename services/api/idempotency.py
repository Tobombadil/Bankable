"""`Idempotency-Key` on mutating calls (api/openapi.yaml `IdempotencyKey`; docs/23 §1, §8;
docs/04 API-9; backend audit 2026-09-30 F12).

Before, the header was read only by intake and admin sources: two `POST /v1/webhooks` with the
same key created two endpoints. Now, for any `POST`/`PUT`/`PATCH`/`DELETE` from a signed-in
account (session or API key) that carries the header:

- The key is scoped to the account and bound to a SHA-256 of the method, path, query string and
  body. A repeat within 24 hours with the same hash returns the stored response, status, body and
  headers, with `Idempotent-Replayed: true`, and the route does not run again. The same key with a
  different request is `409 conflict`, as the spec says.
- The response is stored in the same transaction as the write it describes
  (`services/api/request_context.py::CommitBeforeResponseMiddleware` adds the `idempotency_record`
  row to the request's own session just before its commit). So a stored response always describes
  a committed write, a request that fails stores nothing and can be retried, and two concurrent
  requests with one key cannot both commit: the second hits the `(scope, idempotency_key)` unique
  constraint, its whole transaction (its write included) rolls back, and it answers `409 conflict`;
  its retry then receives the first request's response.
- An error response raised by the route is not stored: its transaction was rolled back, so a retry
  runs again, which is safe because nothing was written.

Deliberate difference from the spec, recorded there: the header is not yet *required* on API-key
calls. Rejecting keyless writes would break every current integration at once; it is honoured
whenever present. Anonymous calls are not covered here (they have no account to scope the key to);
the public intake routes keep their own key handling.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from services.api.auth import resolve_request_auth
from services.api.common import utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, validation_error
from services.db.models import IdempotencyRecord

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
TTL = dt.timedelta(hours=24)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Key on `request.state` (and `scope["state"]`) for the record the response should be stored as.
STATE_KEY = "idempotency"
#: Provider webhooks carry the provider's own event id, which is their idempotency key.
_EXEMPT_PREFIXES = ("/webhooks/",)
#: Response headers not replayed: recomputed per response, or per-caller state.
_NOT_STORED = frozenset({"content-length", "set-cookie", "x-request-id", "date", "server"})


@dataclass
class PendingRecord:
    """What `CommitBeforeResponseMiddleware` needs to store the response: the request's session
    (the one the route wrote through) and the record's identity."""

    session: Session
    scope: str
    key: str
    request_hash: str
    method: str
    path: str


class IdempotentReplay(Exception):
    def __init__(self, record: IdempotencyRecord) -> None:
        super().__init__(record.idempotency_key)
        self.status_code = record.status_code
        self.headers = dict(record.response_headers or {})
        self.body = bytes(record.response_body)


async def replay_handler(_request: Request, exc: Exception) -> Response:
    if not isinstance(exc, IdempotentReplay):  # pragma: no cover - registration guarantees this
        raise exc
    headers = {k: v for k, v in exc.headers.items() if k.lower() not in _NOT_STORED}
    headers[REPLAYED_HEADER] = "true"
    return Response(content=exc.body, status_code=exc.status_code, headers=headers)


def request_hash(method: str, path: str, query: str, body: bytes) -> str:
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query.encode()):
        digest.update(part + b"\n")
    digest.update(body)
    return digest.hexdigest()


def _lookup(request: Request, db: Session, key: str, digest: str) -> PendingRecord | None:
    ctx = resolve_request_auth(
        request,
        db,
        session_cookie=request.cookies.get("session"),
        authorization=request.headers.get("authorization"),
    )
    if ctx.account is None:
        return None
    scope = f"account:{ctx.account.id}"
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == key
        )
    )
    if record is not None:
        expires_at = (
            record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=dt.UTC)
        )
        if expires_at <= utcnow():
            db.delete(record)
            db.flush()
        elif record.request_hash != digest:
            raise ProblemError(
                "conflict",
                "Idempotency key reused with a different request",
                detail="This Idempotency-Key was used within the last 24 hours for another request.",
                instance=request.url.path,
            )
        else:
            raise IdempotentReplay(record)
    return PendingRecord(
        session=db,
        scope=scope,
        key=key,
        request_hash=digest,
        method=request.method,
        path=request.url.path,
    )


async def idempotency_guard(request: Request, db: Annotated[Session, Depends(get_db)]) -> None:
    """App-wide dependency: replays or refuses a repeated key before the route runs, otherwise
    marks the request so its response is stored with its write."""
    key = request.headers.get(HEADER)
    if key is None or request.method not in MUTATING_METHODS or request.url.path.startswith(_EXEMPT_PREFIXES):
        return
    if not 8 <= len(key) <= 128 or not key.isprintable():
        raise validation_error(
            HEADER, "Idempotency-Key must be 8 to 128 printable characters", request.url.path
        )
    body = await request.body()
    digest = request_hash(request.method, request.url.path, request.url.query, body)
    pending = await run_in_threadpool(_lookup, request, db, key, digest)
    if pending is not None:
        setattr(request.state, STATE_KEY, pending)


def store_response(
    pending: PendingRecord, status_code: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> None:
    """Adds the record to the request's session; the caller commits it with the write."""
    now = utcnow()
    stored = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in headers
        if name.decode("latin-1").lower() not in _NOT_STORED
    }
    pending.session.add(
        IdempotencyRecord(
            scope=pending.scope,
            idempotency_key=pending.key,
            method=pending.method,
            path=pending.path,
            request_hash=pending.request_hash,
            status_code=status_code,
            response_headers=stored,
            response_body=body,
            created_at=now,
            expires_at=now + TTL,
        )
    )


def is_idempotency_conflict(exc: BaseException) -> bool:
    text = str(exc)
    return "uq_idempotency_record_scope_key" in text or "idempotency_record.scope" in text


def conflict_body(request_id: str, instance: str) -> dict[str, Any]:
    from services.api.common import API_HOST

    return {
        "type": f"{API_HOST}/errors/conflict",
        "title": "A request with this Idempotency-Key is already being processed",
        "status": 409,
        "code": "conflict",
        "detail": "Retry with the same key to receive the first request's response.",
        "request_id": request_id,
        "instance": instance,
    }
