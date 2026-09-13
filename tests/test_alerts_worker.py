"""The scheduled alert-and-delivery tick: `run_alert_tick` (task brief: "the scheduled alert
cycle and delivery worker"; docs/20-architecture.md §4.1/§4.2 job type `alert`). Reuses the
fixtures and entity factories `tests/test_alerts_evaluate.py` and `tests/test_alerts_webhooks.py`
already use (`services.api.conftest`, `tests.conftest`) rather than duplicating them.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from services.alerts.webhooks import BACKOFF_SCHEDULE_SECONDS
from services.alerts.worker import (
    AlertTickReport,
    DryRunTransport,
    HttpxTransport,
    main,
    run_alert_tick,
)
from services.api.auth import ResendEmailAdapter, SentEmail
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Alert, SavedSearch, WebhookDelivery, WebhookEndpoint
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from tests.conftest import make_account, make_user

UTC = dt.UTC


# ------------------------------------------------------------------------------------- fixtures
@pytest.fixture()
def session_factory():
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def _make_saved_search(db, account, user, *, query=None) -> SavedSearch:
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="test search",
        entity="proposal",
        query=query or {"kind": "storage"},
        query_hash="x",
        channels=["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _make_endpoint(db, account, user, *, secret="whsec_test") -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        url="https://example.com/hook",
        types=["event.published"],
        entity="proposal",
        query={},
        secret=secret,
        status="active",
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("whe", endpoint.id)
    db.flush()
    return endpoint


def _seed(db):
    """One account/user with a saved search that matches a seeded event, plus a webhook endpoint
    with a pending delivery — the scenario the task brief names."""
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    event = make_event(db, prop, src)
    search = _make_saved_search(db, account, user)
    endpoint = _make_endpoint(db, account, user)
    db.commit()

    from services.alerts.webhooks import enqueue_deliveries_for_event

    deliveries = enqueue_deliveries_for_event(db, event)
    db.commit()
    return {
        "account": account,
        "user": user,
        "source": src,
        "proposal": prop,
        "event": event,
        "search": search,
        "endpoint": endpoint,
        "delivery": deliveries[0] if deliveries else None,
    }


class RecordingEmailPort:
    def __init__(self) -> None:
        self.sent: list[SentEmail] = []

    def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        record = SentEmail(to=to, subject=subject, body=body, provider_message_id="rec_1", dry_run=True)
        self.sent.append(record)
        return record


class RaisingEmailPort:
    def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        raise RuntimeError("email provider down")


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeTransport:
    def __init__(self, responses: list[int | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        self.calls.append({"url": url, "content": content, "headers": headers})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


# ------------------------------------------------------------------------------- happy path tick
def test_one_tick_evaluates_creates_alert_and_delivers_pending(session_factory):
    with session_factory() as db:
        seeded = _seed(db)
        search_id = seeded["search"].id

    email_port = RecordingEmailPort()
    transport = FakeTransport([200])
    now = dt.datetime.now(UTC)

    report = run_alert_tick(session_factory, email_port=email_port, transport=transport, now=now)

    assert isinstance(report, AlertTickReport)
    assert report.started_at == now
    assert report.finished_at >= report.started_at
    assert report.searches_evaluated == 1
    assert report.alerts_created == 1
    assert report.emails_sent == 1
    assert report.deliveries_attempted == 1
    assert report.deliveries_delivered == 1
    assert report.deliveries_failed == 0
    assert report.errors == ()

    assert len(email_port.sent) == 1

    with session_factory() as db:
        search = db.get(SavedSearch, search_id)
        assert search.watermark_seq > 0
        assert db.query(Alert).count() == 1
        assert db.query(Alert).one().status == "sent"
        delivery = db.query(WebhookDelivery).one()
        assert delivery.status == "delivered"


def test_second_tick_is_a_no_op_on_both_halves(session_factory):
    with session_factory() as db:
        _seed(db)

    now = dt.datetime.now(UTC)
    first = run_alert_tick(
        session_factory, email_port=RecordingEmailPort(), transport=FakeTransport([200]), now=now
    )
    assert first.alerts_created == 1
    assert first.deliveries_attempted == 1

    second_email = RecordingEmailPort()
    # No responses queued: a second `.post()` call would raise IndexError from `.pop(0)` — proof
    # that a delivered delivery is never re-attempted.
    second_transport = FakeTransport([])
    second = run_alert_tick(session_factory, email_port=second_email, transport=second_transport, now=now)

    assert second.searches_evaluated == 1
    assert second.alerts_created == 0
    assert second.emails_sent == 0
    assert second.deliveries_attempted == 0
    assert second.deliveries_delivered == 0
    assert second.deliveries_failed == 0
    assert second.errors == ()
    assert second_email.sent == []

    with session_factory() as db:
        assert db.query(Alert).count() == 1
        assert db.query(WebhookDelivery).count() == 1


def test_failed_delivery_is_retrying_with_next_attempt_at_in_the_future(session_factory):
    with session_factory() as db:
        seeded = _seed(db)
        delivery_id = seeded["delivery"].id

    now = dt.datetime.now(UTC)
    report = run_alert_tick(
        session_factory, email_port=RecordingEmailPort(), transport=FakeTransport([500]), now=now
    )

    assert report.deliveries_attempted == 1
    assert report.deliveries_delivered == 0
    assert report.deliveries_failed == 1

    with session_factory() as db:
        delivery = db.get(WebhookDelivery, delivery_id)
        assert delivery.status == "retrying"
        next_attempt_at = delivery.next_attempt_at
        assert next_attempt_at is not None
        # SQLite does not round-trip tzinfo through storage (unlike the in-memory object the
        # other suites assert against without a re-fetch); normalise to UTC before comparing.
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=UTC)
        assert next_attempt_at == now + dt.timedelta(seconds=BACKOFF_SCHEDULE_SECONDS[0])
        assert next_attempt_at > now


# --------------------------------------------------------------------------- failure containment
def test_email_port_raising_leaves_delivery_half_running_and_records_error(session_factory):
    with session_factory() as db:
        _seed(db)

    report = run_alert_tick(session_factory, email_port=RaisingEmailPort(), transport=FakeTransport([200]))

    # The active-search count is taken before `run_alert_cycle` runs, so it survives the
    # rollback that follows — only the cycle's own writes (the alert row, the watermark) do not.
    assert report.searches_evaluated == 1
    assert report.alerts_created == 0
    assert report.emails_sent == 0
    assert len(report.errors) == 1
    assert "alert_cycle" in report.errors[0]
    assert "RuntimeError" in report.errors[0]
    # The delivery half still ran, undisturbed by the alert half's rollback.
    assert report.deliveries_attempted == 1
    assert report.deliveries_delivered == 1

    with session_factory() as db:
        # The alert half's transaction was rolled back: no alert row, watermark unmoved.
        assert db.query(Alert).count() == 0
        assert db.query(WebhookDelivery).one().status == "delivered"


def test_email_port_raising_never_leaks_the_exception(session_factory):
    with session_factory() as db:
        _seed(db)

    # Must not raise: every exception is caught (task brief).
    report = run_alert_tick(session_factory, email_port=RaisingEmailPort(), transport=FakeTransport([200]))
    assert isinstance(report, AlertTickReport)


def test_delivery_half_raising_leaves_alert_half_committed_and_records_error(session_factory, monkeypatch):
    """`webhooks.attempt_delivery` deliberately swallows every exception a `Transport.post` call
    raises (its own docstring: "any transport failure is a delivery failure, not a 500"), so a
    literally-raising `Transport` never reaches this module's own exception boundary — see
    `services/alerts/README.md` "found in webhooks.py" for why. This proves the boundary itself
    (any failure anywhere in the delivery half) by making the lower-level call raise directly,
    which is the scenario `webhooks.py`'s own design cannot absorb (e.g. a dropped DB connection)."""
    with session_factory() as db:
        _seed(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("delivery machinery down")

    monkeypatch.setattr("services.alerts.worker.deliver_pending", _boom)

    report = run_alert_tick(session_factory, email_port=RecordingEmailPort(), transport=FakeTransport([200]))

    assert report.alerts_created == 1
    assert report.emails_sent == 1
    assert report.deliveries_attempted == 0
    assert report.deliveries_delivered == 0
    assert report.deliveries_failed == 0
    assert len(report.errors) == 1
    assert "webhook_delivery" in report.errors[0]
    assert "RuntimeError" in report.errors[0]

    with session_factory() as db:
        # The alert half committed even though the delivery half blew up afterwards.
        assert db.query(Alert).count() == 1


def test_transport_post_raising_is_recorded_as_a_failed_delivery_not_a_tick_error(session_factory):
    """Documents actual behaviour (services/alerts/README.md): `webhooks.attempt_delivery` catches
    a raising `Transport.post` itself and marks the delivery `retrying`/`failed` — it never
    reaches this module's `errors` list."""
    with session_factory() as db:
        _seed(db)

    report = run_alert_tick(
        session_factory,
        email_port=RecordingEmailPort(),
        transport=FakeTransport([ConnectionError("refused")]),
    )

    assert report.errors == ()
    assert report.deliveries_attempted == 1
    assert report.deliveries_delivered == 0
    assert report.deliveries_failed == 1


# ------------------------------------------------------------------------------------- defaults
def test_default_email_port_is_dry_run_and_default_transport_handles_no_pending_work(
    session_factory, monkeypatch
):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import services.alerts.worker as worker_module

    worker_module._default_email_port = None  # force the lazy singleton to rebuild
    with session_factory() as db:
        account = make_account(db)
        user = make_user(db, account)
        lic = make_open_licence(db)
        src = make_public_source(db, lic)
        prop = make_visible_proposal(db, src, public_id_suffix="1")
        prop.kind = "storage"
        make_event(db, prop, src)
        _make_saved_search(db, account, user)
        db.commit()

    # transport=None with no pending webhook deliveries never has to construct a real connection.
    report = run_alert_tick(session_factory)

    assert report.errors == ()
    assert report.alerts_created == 1
    assert report.emails_sent == 1
    assert report.deliveries_attempted == 0

    assert isinstance(worker_module._default_email_port, ResendEmailAdapter)
    assert worker_module._default_email_port.dry_run is True
    assert len(worker_module._default_email_port.sent) == 1
    assert worker_module._default_email_port.sent[0].provider_message_id.startswith("dryrun_")


# ------------------------------------------------------------------------------- HttpxTransport
def test_httpx_transport_success_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpxTransport(client=client)

    response = transport.post("https://example.com/hook", content=b"{}", headers={"X-Test": "1"})

    assert response.status_code == 202
    transport.close()  # no-op: this instance did not build the client


def test_httpx_transport_connection_error_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpxTransport(client=client)

    with pytest.raises(httpx.ConnectError):
        transport.post("https://example.com/hook", content=b"{}", headers={})


def test_httpx_transport_end_to_end_through_deliver_pending(session_factory):
    """The real `HttpxTransport` (not a `FakeTransport`) driving an actual delivery, wired to
    `httpx.MockTransport` so no live network is used."""
    with session_factory() as db:
        seeded = _seed(db)
        delivery_id = seeded["delivery"].id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpxTransport(client=client)

    report = run_alert_tick(session_factory, email_port=RecordingEmailPort(), transport=transport)

    assert report.deliveries_delivered == 1
    with session_factory() as db:
        assert db.get(WebhookDelivery, delivery_id).status == "delivered"


def test_httpx_transport_owns_and_closes_a_client_it_builds_itself():
    transport = HttpxTransport(timeout=1.0)
    assert transport._owns_client is True
    transport.close()
    assert transport._client.is_closed is True


def test_dry_run_transport_records_without_sending():
    transport = DryRunTransport()

    response = transport.post("https://example.com/hook", content=b"{}", headers={"X-Test": "1"})

    assert response.status_code == 200
    assert transport.calls == [{"url": "https://example.com/hook", "headers": {"X-Test": "1"}}]


# ----------------------------------------------------------------------------------------- CLI
def test_cli_main_runs_one_tick_and_prints_a_summary(session_factory, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    exit_code = main(["--dry-run"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("alert_tick ")
    assert "searches_evaluated=0" in out
    assert "errors=0" in out


def test_cli_dry_run_forces_dry_run_email_and_recording_transport(session_factory, monkeypatch):
    db_url = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("DATABASE_URL", db_url)
    # Pretend a real key is configured; --dry-run must still force a dry-run send.
    monkeypatch.setenv("RESEND_API_KEY", "re_should_not_be_used")

    captured: dict = {}
    original_run_alert_tick = run_alert_tick

    def _spy(session_factory_arg, *, email_port=None, transport=None, now=None):
        captured["email_port"] = email_port
        captured["transport"] = transport
        return original_run_alert_tick(
            session_factory_arg, email_port=email_port, transport=transport, now=now
        )

    monkeypatch.setattr("services.alerts.worker.run_alert_tick", _spy)

    exit_code = main(["--dry-run"])

    assert exit_code == 0
    assert isinstance(captured["email_port"], ResendEmailAdapter)
    assert captured["email_port"].dry_run is True
    assert isinstance(captured["transport"], DryRunTransport)


def test_cli_exit_code_is_nonzero_when_the_tick_recorded_an_error(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

    def _failing_tick(*args, **kwargs):
        return AlertTickReport(
            started_at=dt.datetime.now(UTC),
            finished_at=dt.datetime.now(UTC),
            searches_evaluated=0,
            alerts_created=0,
            emails_sent=0,
            deliveries_attempted=0,
            deliveries_delivered=0,
            deliveries_failed=0,
            errors=("alert_cycle: RuntimeError: boom",),
        )

    monkeypatch.setattr("services.alerts.worker.run_alert_tick", _failing_tick)

    exit_code = main([])

    assert exit_code == 1
    assert "errors=1" in capsys.readouterr().out
