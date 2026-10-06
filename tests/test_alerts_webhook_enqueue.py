"""Webhooks fire for real changes: event -> alert tick -> delivery (backend audit 2026-09-30 F2).

Before, `enqueue_deliveries_for_event` was called only from tests, so a customer who registered a
webhook received nothing but the test ping and manual replays. The tick now enqueues from each
endpoint's `watermark_seq`, in one transaction with the watermark's advance, then delivers.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from services.alerts import webhooks
from services.alerts.worker import run_alert_tick
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Event, WebhookDelivery, WebhookEndpoint
from tests.conftest import make_account, make_user

UTC = dt.UTC


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RecordingTransport:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _Response:
        self.posts.append({"url": url, "body": json.loads(content), "headers": headers})
        return _Response(self.status_code)


class _NoMail:
    dry_run = True

    def send(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - no saved searches here
        raise AssertionError("no email expected")


def _tick(factory: sessionmaker[Session], transport: RecordingTransport) -> Any:
    return run_alert_tick(factory, email_port=_NoMail(), transport=transport)


@pytest.fixture()
def world(db_sessionmaker: sessionmaker[Session], client: Any) -> dict[str, Any]:
    """An API-tier account that registers a webhook through the API, and a visible proposal."""
    from tests.conftest import login

    with db_sessionmaker() as db:
        account = make_account(db, entitlement="api")
        user = make_user(db, account)
        source = make_public_source(db, make_open_licence(db))
        prop = make_visible_proposal(db, source)
        make_event(db, prop, source, event_type="created")  # history before the endpoint exists
        db.commit()
        login(client, db, user)
        ids = {"source": source.id, "proposal": prop.id}
    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "types": ["event.published"], "entity": "event"},
    )
    assert resp.status_code == 201, resp.text
    return {**ids, "webhook_id": resp.json()["data"]["webhook_id"]}


def _new_change(
    factory: sessionmaker[Session], world: dict[str, Any], event_type: str = "status_change"
) -> int:
    from services.db.models import Proposal, Source

    with factory() as db:
        prop = db.get(Proposal, world["proposal"])
        source = db.get(Source, world["source"])
        assert prop is not None and source is not None
        ev = make_event(db, prop, source, event_type=event_type)
        db.commit()
        return ev.seq


def _deliveries(factory: sessionmaker[Session]) -> list[WebhookDelivery]:
    with factory() as db:
        return list(db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.event_seq)).all())


def test_a_change_after_registration_is_delivered_once(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    seq = _new_change(db_sessionmaker, world)
    transport = RecordingTransport()

    report = _tick(db_sessionmaker, transport)
    assert report.errors == ()
    assert report.deliveries_enqueued == 1
    assert report.deliveries_delivered == 1
    assert [p["body"]["data"]["event"]["seq"] for p in transport.posts] == [seq]
    # The served view names the record (W3's GatedRecord), not a bare uuid.
    assert transport.posts[0]["body"]["data"]["subject"]["name_canonical"].startswith("Test Storage Project")

    # The history before the endpoint existed is not sent; a second tick sends nothing new.
    again = _tick(db_sessionmaker, transport)
    assert again.deliveries_enqueued == 0 and again.deliveries_attempted == 0
    assert len(transport.posts) == 1
    assert [d.event_seq for d in _deliveries(db_sessionmaker)] == [seq]


def test_a_failed_enqueue_moves_nothing_and_the_retry_does_not_duplicate(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _new_change(db_sessionmaker, world)
    second = _new_change(db_sessionmaker, world, event_type="field_changed")
    real = webhooks._new_delivery
    calls = {"n": 0}

    def flaky(db: Session, endpoint: WebhookEndpoint, event: Event) -> WebhookDelivery:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure after the first delivery row")
        return real(db, endpoint, event)

    monkeypatch.setattr(webhooks, "_new_delivery", flaky)
    transport = RecordingTransport()
    failed = _tick(db_sessionmaker, transport)
    assert any(e.startswith("webhook_enqueue") for e in failed.errors)
    assert _deliveries(db_sessionmaker) == []
    with db_sessionmaker() as db:
        endpoint = db.scalar(select(WebhookEndpoint))
        assert endpoint is not None and endpoint.watermark_seq < first

    monkeypatch.setattr(webhooks, "_new_delivery", real)
    ok = _tick(db_sessionmaker, transport)
    assert ok.errors == ()
    assert [d.event_seq for d in _deliveries(db_sessionmaker)] == [first, second]
    assert sorted(p["body"]["data"]["event"]["seq"] for p in transport.posts) == [first, second]


def test_a_failed_post_is_retried_on_the_same_delivery_not_a_new_one(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    _new_change(db_sessionmaker, world)
    down = RecordingTransport(status_code=503)
    _tick(db_sessionmaker, down)
    later = dt.datetime.now(UTC) + dt.timedelta(seconds=webhooks.BACKOFF_SCHEDULE_SECONDS[0] + 1)
    up = RecordingTransport()
    report = run_alert_tick(db_sessionmaker, email_port=_NoMail(), transport=up, now=later)
    assert report.deliveries_enqueued == 0
    rows = _deliveries(db_sessionmaker)
    assert len(rows) == 1 and rows[0].status == "delivered" and rows[0].attempt == 2
    assert (
        down.posts[0]["headers"]["X-Platform-Delivery-Id"] == up.posts[0]["headers"]["X-Platform-Delivery-Id"]
    )


def test_a_hidden_record_change_is_not_enqueued(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    from services.db.models import Proposal

    _new_change(db_sessionmaker, world)
    with db_sessionmaker() as db:
        prop = db.get(Proposal, world["proposal"])
        assert prop is not None
        prop.publish_state = "unpublished"
        db.commit()
    transport = RecordingTransport()
    report = _tick(db_sessionmaker, transport)
    assert report.deliveries_enqueued == 0
    assert transport.posts == []
    with db_sessionmaker() as db:
        endpoint = db.scalar(select(WebhookEndpoint))
        head = db.scalar(select(func.max(Event.seq)))
        assert endpoint is not None and endpoint.watermark_seq == head


def test_a_backlog_drains_in_batches_oldest_first(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    seqs = [
        _new_change(db_sessionmaker, world, event_type=t)
        for t in ("status_change", "field_changed", "cod_changed")
    ]
    with db_sessionmaker() as db:
        assert webhooks.enqueue_new_deliveries(db, batch_size=2) == 2
        db.commit()
        assert webhooks.enqueue_new_deliveries(db, batch_size=2) == 1
        db.commit()
        assert webhooks.enqueue_new_deliveries(db, batch_size=2) == 0
    assert [d.event_seq for d in _deliveries(db_sessionmaker)] == seqs


def test_a_paused_endpoint_is_not_enqueued(
    db_sessionmaker: sessionmaker[Session], world: dict[str, Any]
) -> None:
    _new_change(db_sessionmaker, world)
    with db_sessionmaker() as db:
        endpoint = db.scalar(select(WebhookEndpoint))
        assert endpoint is not None
        endpoint.status = "paused"
        db.commit()
    assert _tick(db_sessionmaker, RecordingTransport()).deliveries_enqueued == 0
