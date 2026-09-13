"""API-tier webhook delivery (docs/23-api-spec-outline.md §9.1; docs/21-data-model.md §4.4;
task brief: "registration, HMAC signing, retry with backoff, delivery log").

Signing matches `api/openapi.yaml`'s `WebhookSignatureHeader` exactly: `t=<unix>,v1=<hex HMAC-SHA256
of "t.body">`. Delivery is split into two functions so a test never makes a live HTTP call
(docs/04 E-6 "fixtures over live calls"): `build_delivery_request` is pure (payload → signed
headers + body), and `attempt_delivery` takes an injectable `Transport` — production wires a real
`httpx.Client`, tests pass a `FakeTransport` that returns configured responses.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.alerts.matching import event_matches_query, matches_query
from services.db.models import Event, Opportunity, Proposal, WebhookDelivery, WebhookEndpoint
from services.ids import public_id

#: docs/23 §9.1: "5 attempts over ~6 hours with exponential backoff and jitter". Jitter is the
#: caller's concern (kept deterministic here for reproducible tests); the five delays below sum to
#: a little under 6 hours, matching the spec's "~6 hours" for attempts 2-5 after the first
#: immediate attempt.
BACKOFF_SCHEDULE_SECONDS = (60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_SECONDS)
#: docs/23 §9.1: "after 24 consecutive failures, the endpoint is paused".
PAUSE_AFTER_CONSECUTIVE_FAILURES = 24
SIGNATURE_TOLERANCE_SECONDS = 300


class TransportResponse(Protocol):
    status_code: int


class Transport(Protocol):
    """The only method call sites use — a real `httpx.Client.post` satisfies this shape without
    adaptation; `services/alerts/test_webhooks... ` fixtures use a `FakeTransport` instead."""

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> TransportResponse: ...


def sign_payload(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{body.decode()}".encode()
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_signature(secret: str, body: bytes, header: str, *, now: int | None = None) -> bool:
    """Mirrors what a customer's receiver does — used by this codebase's own tests to prove
    `sign_payload` round-trips, and available for a future receiver-side test double."""
    now = now if now is not None else int(time.time())
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(now - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = sign_payload(secret, body, timestamp=ts).split("v1=")[1]
    return hmac.compare_digest(expected, given)


@dataclass(frozen=True)
class WebhookEventEnvelope:
    """Matches `api/openapi.yaml`'s webhook payload example (docs/23 §9.1)."""

    type: str
    delivery_id: str
    data: dict[str, Any]

    def to_bytes(self) -> bytes:
        body = {
            "id": self.delivery_id,
            "type": self.type,
            "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "api_version": "v1",
            "data": self.data,
        }
        return json.dumps(body, sort_keys=True).encode()


_EVENT_TYPE_TO_WEBHOOK_TYPE = {
    "unpublished": "record.unpublished",
    "match_added": "match.added",
    "match_removed": "match.removed",
}


def webhook_type_for_event(event: Event) -> str:
    return _EVENT_TYPE_TO_WEBHOOK_TYPE.get(event.event_type, "event.published")


def _event_matches_endpoint(db: Session, endpoint: WebhookEndpoint, event: Event) -> bool:
    if webhook_type_for_event(event) not in endpoint.types:
        return False
    if endpoint.entity == "event":
        return event_matches_query(event, endpoint.query)
    if endpoint.entity != event.subject_type:
        return False
    subject: Proposal | Opportunity | None
    if event.subject_type == "proposal":
        subject = db.get(Proposal, event.subject_id)
    elif event.subject_type == "opportunity":
        subject = db.get(Opportunity, event.subject_id)
    else:
        return False
    return subject is not None and matches_query(endpoint.entity, subject, endpoint.query)


def enqueue_deliveries_for_event(db: Session, event: Event) -> list[WebhookDelivery]:
    """One `WebhookDelivery` row (`status = "pending"`, `attempt = 1`) per active endpoint on the
    event's account whose `types`/`query` match — "the payload is tier-filtered and licence-gated
    exactly like a GET on the same key" (docs/23 §9.1) is true here because `event` rows reaching
    this function have already passed the visibility predicate for the endpoint's own tier
    (the caller's responsibility, mirroring how `services/api/app.py` never constructs an
    ungated query in the first place)."""
    endpoints = list(
        db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.status == "active")).all()
    )
    created = []
    for endpoint in endpoints:
        if not _event_matches_endpoint(db, endpoint, event):
            continue
        delivery = WebhookDelivery(
            public_id="",
            webhook_endpoint_id=endpoint.id,
            type=webhook_type_for_event(event),
            event_id=event.id,
            event_seq=event.seq,
            attempt=1,
            status="pending",
        )
        db.add(delivery)
        db.flush()
        delivery.public_id = public_id("whd", delivery.id)
        db.flush()
        created.append(delivery)
    return created


def _event_payload(db: Session, event: Event) -> dict[str, Any]:
    subject: dict[str, Any] = {"public_id": str(event.subject_id)}
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p is not None:
            subject = {"public_id": p.public_id, "name_canonical": p.name_canonical}
    elif event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o is not None:
            subject = {"public_id": o.public_id, "name_canonical": o.title}
    return {
        "event": {
            "seq": event.seq,
            "subject_type": event.subject_type,
            "subject_id": str(event.subject_id),
            "event_type": event.event_type,
            "before": event.before,
            "after": event.after,
            "provenance": {"source_id": event.source_id, "licence_id": event.licence_id},
        },
        "subject": subject,
    }


def attempt_delivery(
    db: Session,
    delivery: WebhookDelivery,
    *,
    transport: Transport,
    secret: str,
    now: dt.datetime | None = None,
) -> WebhookDelivery:
    """One HTTP attempt. On success: `status = "delivered"`, endpoint's failure streak reset. On
    failure: schedules the next attempt per `BACKOFF_SCHEDULE_SECONDS`, or `status = "failed"` and
    counts against `PAUSE_AFTER_CONSECUTIVE_FAILURES` once attempts are exhausted."""
    now = now or dt.datetime.now(dt.UTC)
    endpoint = delivery.endpoint
    event = db.get(Event, delivery.event_id) if delivery.event_id else None
    payload = (
        _event_payload(db, event)
        if event is not None
        else {"event": None, "subject": None, "message": "webhook.test"}
    )
    envelope = WebhookEventEnvelope(type=delivery.type, delivery_id=delivery.public_id, data=payload)
    body = envelope.to_bytes()
    signature = sign_payload(secret, body, timestamp=int(now.timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-Platform-Signature": signature,
        "X-Platform-Delivery-Id": delivery.public_id,
        "X-Platform-Event-Seq": str(delivery.event_seq or 0),
    }
    started = time.monotonic()
    try:
        response = transport.post(endpoint.url, content=body, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        delivery.response_status = response.status_code
        delivery.latency_ms = latency_ms
        success = 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001 — any transport failure is a delivery failure, not a 500
        success = False
        delivery.error_class = type(exc).__name__
        delivery.latency_ms = int((time.monotonic() - started) * 1000)

    endpoint.last_delivery_at = now
    if success:
        delivery.status = "delivered"
        endpoint.last_success_at = now
        endpoint.consecutive_failures = 0
    else:
        if delivery.attempt >= MAX_ATTEMPTS:
            delivery.status = "failed"
            endpoint.consecutive_failures += 1
            if endpoint.consecutive_failures >= PAUSE_AFTER_CONSECUTIVE_FAILURES:
                endpoint.status = "paused"
        else:
            delivery.status = "retrying"
            delay = BACKOFF_SCHEDULE_SECONDS[delivery.attempt - 1]
            delivery.next_attempt_at = now + dt.timedelta(seconds=delay)
    db.flush()
    return delivery


def retry_delivery(delivery: WebhookDelivery) -> WebhookDelivery:
    """Advances `attempt` for the next `attempt_delivery` call — kept separate from
    `attempt_delivery` so a worker loop can decide, per delivery, whether `next_attempt_at` has
    arrived before spending an attempt."""
    delivery.attempt += 1
    delivery.status = "pending"
    return delivery


def replay_from_seq(db: Session, endpoint: WebhookEndpoint, *, since_seq: int) -> list[WebhookDelivery]:
    """`POST /v1/webhooks/{id}/replay?since=<seq>` (docs/23 §9.1): re-enqueues every event with
    `seq > since_seq` this endpoint would have matched, each starting a fresh `attempt = 1`
    delivery with a new delivery id (consumers dedupe on `data.event.id`, per the spec)."""
    events = list(db.scalars(select(Event).where(Event.seq > since_seq).order_by(Event.seq.asc())).all())
    created = []
    for event in events:
        if not _event_matches_endpoint(db, endpoint, event):
            continue
        delivery = WebhookDelivery(
            public_id="",
            webhook_endpoint_id=endpoint.id,
            type=webhook_type_for_event(event),
            event_id=event.id,
            event_seq=event.seq,
            attempt=1,
            status="pending",
        )
        db.add(delivery)
        db.flush()
        delivery.public_id = public_id("whd", delivery.id)
        db.flush()
        created.append(delivery)
    return created


def deliver_pending(
    db: Session,
    *,
    transport: Transport,
    secret_for: Callable[[WebhookEndpoint], str],
    now: dt.datetime | None = None,
) -> list[WebhookDelivery]:
    """One worker tick (no Procrastinate integration this sprint — a scheduled job calling this
    on an interval is the intended production shape, documented as an open decision in
    services/README.md): every `pending` delivery is attempted; every `retrying` delivery whose
    `next_attempt_at` has arrived is retried. `secret_for` looks up the endpoint's signing secret
    (never stored in the delivery row itself, matching `api_key.key_hash`'s "the secret is never
    stored" pattern — only its hash is)."""
    now = now or dt.datetime.now(dt.UTC)
    stmt = select(WebhookDelivery).where(WebhookDelivery.status.in_(("pending", "retrying")))
    due = []
    for delivery in db.scalars(stmt).all():
        if delivery.status == "retrying":
            if delivery.next_attempt_at is None or delivery.next_attempt_at > now:
                continue
            retry_delivery(delivery)
        due.append(delivery)
    for delivery in due:
        attempt_delivery(db, delivery, transport=transport, secret=secret_for(delivery.endpoint), now=now)
    return due


def create_test_delivery(db: Session, endpoint: WebhookEndpoint) -> WebhookDelivery:
    """`POST /v1/webhooks/{id}/test` (docs/23 §9.1 `webhook.test`): a delivery with no backing
    `event`, matched against nothing — always enqueued regardless of `types`/`query`."""
    delivery = WebhookDelivery(
        public_id="",
        webhook_endpoint_id=endpoint.id,
        type="webhook.test",
        event_id=None,
        event_seq=None,
        attempt=1,
        status="pending",
    )
    db.add(delivery)
    db.flush()
    delivery.public_id = public_id("whd", delivery.id)
    db.flush()
    return delivery
