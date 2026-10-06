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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.alerts.matching import event_matches_query, matches_query
from services.alerts.visibility import event_with_visible_subject_filter
from services.api.common import ensure_aware
from services.api.visibility import gated_opportunity, gated_proposal
from services.db.models import Account, Event, Opportunity, Proposal, WebhookDelivery, WebhookEndpoint
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


def _endpoint_entitlement(db: Session, endpoint: WebhookEndpoint) -> str:
    account = db.get(Account, endpoint.account_id)
    return account.entitlement if account is not None else "public"


def event_visible_for_endpoint(db: Session, endpoint: WebhookEndpoint, event: Event) -> bool:
    """The visibility predicate for the endpoint's own tier — the event's *and* its subject
    record's (`services/alerts/visibility.py`; docs/50 §3.1) — evaluated as SQL against this one
    event, so a hidden or restricted record's transition is never enqueued or, if it was hidden
    after enqueueing, never posted (`attempt_delivery`). This is "the payload is tier-filtered and
    licence-gated exactly like a GET on the same key" (docs/23 §9.1) enforced here rather than
    assumed of the caller."""
    entitlement = _endpoint_entitlement(db, endpoint)
    stmt = select(Event.id).where(Event.id == event.id, *event_with_visible_subject_filter(entitlement))
    return db.scalar(stmt) is not None


def _event_matches_endpoint(
    db: Session, endpoint: WebhookEndpoint, event: Event, *, visibility_checked: bool = False
) -> bool:
    """`visibility_checked` is for a caller whose candidate query already applied
    `event_with_visible_subject_filter` at the endpoint's tier, so the check is not repeated per
    event; every other caller has it evaluated here."""
    if webhook_type_for_event(event) not in endpoint.types:
        return False
    if not visibility_checked and not event_visible_for_endpoint(db, endpoint, event):
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
    return subject is not None and matches_query(
        endpoint.entity, subject, endpoint.query, _endpoint_entitlement(db, endpoint)
    )


def _new_delivery(db: Session, endpoint: WebhookEndpoint, event: Event) -> WebhookDelivery:
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
    return delivery


#: Events considered per endpoint per tick. A backlog larger than this drains over several ticks,
#: oldest first, each tick's batch committed with its watermark.
ENQUEUE_BATCH_SIZE = 1000


def enqueue_new_deliveries(db: Session, *, batch_size: int = ENQUEUE_BATCH_SIZE) -> int:
    """The alert tick's enqueue step (backend audit 2026-09-30 F2: before it, nothing in production
    created a delivery for a real change). For every active endpoint: lock its row, read the events
    above its `watermark_seq` that are visible at its owner's tier (the same predicate a GET on that
    key applies, `event_with_visible_subject_filter`), create one `pending` delivery per event its
    `types`/`entity`/`query` match, and advance the watermark. The caller commits once, so the
    deliveries and the watermark move together: a tick that fails leaves both unchanged and the next
    one does the same work again; a tick that commits is never repeated for those events. The
    endpoint's row lock (`FOR UPDATE`; a no-op on SQLite) makes a second, concurrent tick wait and
    then read the advanced watermark rather than enqueue the same events twice.

    The watermark passes events that are not visible or do not match, as a saved search's does:
    a record hidden when its change was recorded is not announced later. Returns the number of
    deliveries created. The payload is built at delivery time from the served view
    (`attempt_delivery`, `_event_payload`), which also re-checks visibility."""
    head = db.scalar(select(func.max(Event.seq))) or 0
    endpoint_ids = list(
        db.scalars(
            select(WebhookEndpoint.id).where(WebhookEndpoint.status == "active").order_by(WebhookEndpoint.id)
        ).all()
    )
    created = 0
    for endpoint_id in endpoint_ids:
        endpoint = db.scalar(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.id == endpoint_id, WebhookEndpoint.status == "active")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if endpoint is None or endpoint.watermark_seq >= head:
            continue
        entitlement = _endpoint_entitlement(db, endpoint)
        events = list(
            db.scalars(
                select(Event)
                .where(
                    Event.seq > endpoint.watermark_seq,
                    Event.seq <= head,
                    *event_with_visible_subject_filter(entitlement),
                )
                .order_by(Event.seq.asc())
                .limit(batch_size)
            ).all()
        )
        for event in events:
            if _event_matches_endpoint(db, endpoint, event, visibility_checked=True):
                _new_delivery(db, endpoint, event)
                created += 1
        # A full batch stops at its last event; a short one has seen everything up to `head`.
        endpoint.watermark_seq = events[-1].seq if len(events) == batch_size else head
        db.flush()
    return created


def enqueue_deliveries_for_event(db: Session, event: Event) -> list[WebhookDelivery]:
    """One `WebhookDelivery` row (`status = "pending"`, `attempt = 1`) per active endpoint on the
    event's account whose `types`/`query` match and for which the event *and its subject record*
    pass the visibility predicate at the endpoint's tier (`event_visible_for_endpoint`) — "the
    payload is tier-filtered and licence-gated exactly like a GET on the same key" (docs/23 §9.1),
    enforced here rather than left to the caller (docs/50 §3.1). It does not move any endpoint's
    watermark; the scheduled path is `enqueue_new_deliveries`."""
    endpoints = list(db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.status == "active")).all())
    created = []
    for endpoint in endpoints:
        if not _event_matches_endpoint(db, endpoint, event):
            continue
        created.append(_new_delivery(db, endpoint, event))
    return created


def _event_payload(db: Session, event: Event, entitlement: str = "public") -> dict[str, Any]:
    """The subject's name is its served name at the endpoint owner's tier (`GatedRecord`)."""
    subject: dict[str, Any] = {"public_id": str(event.subject_id)}
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p is not None:
            name = gated_proposal(p, entitlement).name_canonical
            subject = {"public_id": p.public_id, "name_canonical": name}
    elif event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o is not None:
            title = gated_opportunity(o, entitlement).title
            subject = {"public_id": o.public_id, "name_canonical": title}
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
    if event is not None and not event_visible_for_endpoint(db, endpoint, event):
        # Hidden (or its licence restricted) between enqueue and delivery: the payload would
        # name the record, so it is not posted. Terminal for this delivery, not counted against
        # the endpoint's failure streak — the endpoint did nothing wrong.
        delivery.status = "failed"
        delivery.error_class = "SubjectNotVisible"
        endpoint.last_delivery_at = now
        db.flush()
        return delivery
    payload = (
        _event_payload(db, event, _endpoint_entitlement(db, endpoint))
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
    except Exception as exc:  # any transport failure is a delivery failure, not a 500
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
    delivery with a new delivery id (consumers dedupe on `data.event.id`, per the spec). The
    candidate query is visibility-gated at the endpoint's tier (docs/50 §3.1: before this it was
    an ungated `seq > since` scan, so a replay shipped hidden and restricted records)."""
    entitlement = _endpoint_entitlement(db, endpoint)
    events = list(
        db.scalars(
            select(Event)
            .where(Event.seq > since_seq, *event_with_visible_subject_filter(entitlement))
            .order_by(Event.seq.asc())
        ).all()
    )
    created = []
    for event in events:
        if not _event_matches_endpoint(db, endpoint, event):
            continue
        created.append(_new_delivery(db, endpoint, event))
    return created


def deliver_pending(
    db: Session,
    *,
    transport: Transport,
    secret_for: Callable[[WebhookEndpoint], str],
    now: dt.datetime | None = None,
    after_attempt: Callable[[WebhookDelivery], None] | None = None,
) -> list[WebhookDelivery]:
    """One worker tick (no Procrastinate integration this sprint — a scheduled job calling this
    on an interval is the intended production shape, documented as an open decision in
    services/README.md): every `pending` delivery is attempted; every `retrying` delivery whose
    `next_attempt_at` has arrived is retried. `secret_for` looks up the endpoint's signing secret
    (never stored in the delivery row itself, matching `api_key.key_hash`'s "the secret is never
    stored" pattern — only its hash is). `after_attempt` runs after each attempt; the worker
    commits there, so a delivery already posted is recorded before the next one is tried and a
    later failure in the tick cannot send it again."""
    now = now or dt.datetime.now(dt.UTC)
    stmt = select(WebhookDelivery).where(WebhookDelivery.status.in_(("pending", "retrying")))
    due = []
    for delivery in db.scalars(stmt).all():
        if delivery.status == "retrying":
            # SQLite returns the stored instant naive (`ensure_aware`); comparing it with an aware
            # `now` raised, and the whole delivery step failed on the dev store.
            if delivery.next_attempt_at is None or ensure_aware(delivery.next_attempt_at) > now:
                continue
            retry_delivery(delivery)
        due.append(delivery)
    for delivery in due:
        attempt_delivery(db, delivery, transport=transport, secret=secret_for(delivery.endpoint), now=now)
        if after_attempt is not None:
            after_attempt(delivery)
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
