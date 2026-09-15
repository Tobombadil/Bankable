"""Webhook mechanics: HMAC signing/verification, matching, retry with backoff, and the delivery
log (task brief item 4; docs/23-api-spec-outline.md §9.1)."""

from __future__ import annotations

import datetime as dt

from services.alerts.webhooks import (
    BACKOFF_SCHEDULE_SECONDS,
    MAX_ATTEMPTS,
    PAUSE_AFTER_CONSECUTIVE_FAILURES,
    attempt_delivery,
    deliver_pending,
    enqueue_deliveries_for_event,
    replay_from_seq,
    retry_delivery,
    sign_payload,
    verify_signature,
    webhook_type_for_event,
)
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import WebhookEndpoint
from tests.conftest import make_account, make_user

UTC = dt.UTC


def _make_endpoint(
    db, account, user, *, types=None, entity="event", query=None, secret="whsec_test"
) -> WebhookEndpoint:
    from services.ids import public_id

    endpoint = WebhookEndpoint(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        url="https://example.com/hook",
        types=types or ["event.published"],
        entity=entity,
        query=query or {},
        secret=secret,
        status="active",
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("whe", endpoint.id)
    db.flush()
    return endpoint


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeTransport:
    """Records every call; `responses` is consumed in order, `Exception` entries raise instead
    of returning (docs/04 E-6: no live HTTP call in a test)."""

    def __init__(self, responses: list[int | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        self.calls.append({"url": url, "content": content, "headers": headers})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


# ------------------------------------------------------------------------------------- signing
def test_sign_and_verify_round_trip():
    body = b'{"a":1}'
    header = sign_payload("whsec_abc", body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_signature("whsec_abc", body, header, now=1_700_000_010) is True


def test_verify_rejects_wrong_secret():
    body = b"payload"
    header = sign_payload("whsec_abc", body, timestamp=1_700_000_000)
    assert verify_signature("whsec_wrong", body, header, now=1_700_000_010) is False


def test_verify_rejects_stale_timestamp():
    body = b"payload"
    header = sign_payload("whsec_abc", body, timestamp=1_700_000_000)
    assert verify_signature("whsec_abc", body, header, now=1_700_000_000 + 301) is False


def test_verify_rejects_malformed_header():
    assert verify_signature("secret", b"x", "garbage") is False
    assert verify_signature("secret", b"x", "t=notanumber,v1=abc") is False


# ---------------------------------------------------------------------------------- matching
def test_enqueue_matches_active_endpoint_by_type_and_query(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.jurisdiction = "US-TX"
    event = make_event(db, prop, src)
    db.flush()

    matching_endpoint = _make_endpoint(db, account, user, entity="proposal", query={"jurisdiction": "US-TX"})
    non_matching_endpoint = _make_endpoint(
        db, account, user, entity="proposal", query={"jurisdiction": "US-CA"}
    )
    wrong_type_endpoint = _make_endpoint(
        db, account, user, types=["match.added"], entity="proposal", query={}
    )
    db.commit()

    deliveries = enqueue_deliveries_for_event(db, event)
    endpoint_ids = {d.webhook_endpoint_id for d in deliveries}
    assert matching_endpoint.id in endpoint_ids
    assert non_matching_endpoint.id not in endpoint_ids
    assert wrong_type_endpoint.id not in endpoint_ids
    assert all(d.status == "pending" and d.attempt == 1 for d in deliveries)


def test_paused_endpoint_never_matches(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user, entity="proposal", query={})
    endpoint.status = "paused"
    db.commit()

    assert enqueue_deliveries_for_event(db, event) == []


def test_webhook_type_for_event_maps_special_types():
    class Fake:
        event_type = "unpublished"

    assert webhook_type_for_event(Fake()) == "record.unpublished"

    class Fake2:
        event_type = "status_change"

    assert webhook_type_for_event(Fake2()) == "event.published"


# --------------------------------------------------------------------------- delivery + retry
def test_attempt_delivery_success_resets_failure_streak(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user)
    endpoint.consecutive_failures = 3
    db.commit()
    deliveries = enqueue_deliveries_for_event(db, event)
    db.commit()
    delivery = deliveries[0]

    transport = FakeTransport([200])
    attempt_delivery(db, delivery, transport=transport, secret=endpoint.secret)

    assert delivery.status == "delivered"
    assert delivery.response_status == 200
    assert endpoint.consecutive_failures == 0
    assert endpoint.last_success_at is not None
    # The signature header on the wire round-trips against the endpoint's own secret.
    sig = transport.calls[0]["headers"]["X-Platform-Signature"]
    assert verify_signature(endpoint.secret, transport.calls[0]["content"], sig) is True


def test_attempt_delivery_failure_schedules_backoff(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user)
    db.commit()
    delivery = enqueue_deliveries_for_event(db, event)[0]
    db.commit()

    transport = FakeTransport([500])
    now = dt.datetime.now(UTC)
    attempt_delivery(db, delivery, transport=transport, secret=endpoint.secret, now=now)

    assert delivery.status == "retrying"
    assert delivery.attempt == 1
    assert delivery.next_attempt_at == now + dt.timedelta(seconds=BACKOFF_SCHEDULE_SECONDS[0])


def test_attempt_delivery_exhausts_after_max_attempts_and_counts_toward_pause(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user)
    db.commit()
    delivery = enqueue_deliveries_for_event(db, event)[0]
    delivery.attempt = MAX_ATTEMPTS
    db.commit()

    transport = FakeTransport([503])
    attempt_delivery(db, delivery, transport=transport, secret=endpoint.secret)

    assert delivery.status == "failed"
    assert endpoint.consecutive_failures == 1


def test_endpoint_pauses_after_enough_consecutive_failures(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user)
    endpoint.consecutive_failures = PAUSE_AFTER_CONSECUTIVE_FAILURES - 1
    db.commit()
    delivery = enqueue_deliveries_for_event(db, event)[0]
    delivery.attempt = MAX_ATTEMPTS
    db.commit()

    attempt_delivery(db, delivery, transport=FakeTransport([500]), secret=endpoint.secret)
    assert endpoint.status == "paused"


def test_transport_exception_counts_as_a_failed_attempt(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    endpoint = _make_endpoint(db, account, user)
    db.commit()
    delivery = enqueue_deliveries_for_event(db, event)[0]
    db.commit()

    attempt_delivery(
        db, delivery, transport=FakeTransport([ConnectionError("refused")]), secret=endpoint.secret
    )
    assert delivery.status == "retrying"
    assert delivery.error_class == "ConnectionError"


def test_deliver_pending_retries_only_when_due(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    event = make_event(db, prop, src)
    _endpoint = _make_endpoint(db, account, user)
    db.commit()
    delivery = enqueue_deliveries_for_event(db, event)[0]
    db.commit()

    now = dt.datetime.now(UTC)
    # First attempt fails, scheduling a retry in the future.
    processed = deliver_pending(db, transport=FakeTransport([500]), secret_for=lambda e: e.secret, now=now)
    assert len(processed) == 1
    assert delivery.status == "retrying"

    # Not due yet: a tick right now finds nothing to do.
    still_none = deliver_pending(db, transport=FakeTransport([200]), secret_for=lambda e: e.secret, now=now)
    assert still_none == []

    # Once due, the retry succeeds.
    due_time = delivery.next_attempt_at
    retried = deliver_pending(db, transport=FakeTransport([200]), secret_for=lambda e: e.secret, now=due_time)
    assert len(retried) == 1
    assert delivery.status == "delivered"
    assert delivery.attempt == 2


def test_retry_delivery_advances_attempt_and_resets_status():
    from services.db.models import WebhookDelivery

    d = WebhookDelivery(
        public_id="whd_x",
        webhook_endpoint_id="ignored",
        type="event.published",
        attempt=2,
        status="retrying",
    )
    retry_delivery(d)
    assert d.attempt == 3
    assert d.status == "pending"


# ---------------------------------------------------------------------------------------- replay
def test_replay_from_seq_only_resends_matching_later_events(db):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    old_event = make_event(db, prop, src)
    db.flush()
    new_event = make_event(db, prop, src, event_type="status_change")
    endpoint = _make_endpoint(db, account, user, entity="proposal", query={})
    db.commit()

    deliveries = replay_from_seq(db, endpoint, since_seq=old_event.seq)
    event_ids = {d.event_id for d in deliveries}
    assert new_event.id in event_ids
    assert old_event.id not in event_ids
