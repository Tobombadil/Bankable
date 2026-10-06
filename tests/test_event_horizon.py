"""Watermark readers never pass an event a still-open transaction can commit later (backend audit
2026-09-30 F5; architect A5, second half; `services/db/event_horizon.py`; docs/21 §3.10).

On Postgres the sequence hands out seqs in allocation order, so a lower seq can commit after a
higher one; `tests/test_postgres.py` reproduces that with two real transactions and pins the
trigger-based bound. SQLite cannot produce the race (one writer at a time, `MAX(seq)+1` inside the
writer's transaction, and every watermark reader also writes, so it waits for an open writer): the
first test below pins that, with two interleaved sessions. The second recreates the Postgres
ordering on SQLite with interleaved writer sessions and explicit seqs. An uncommitted row is
invisible to a reader on Postgres, which on SQLite is the row not yet written; the bound is
replaced by what the Postgres bound returns at each step. It shows that every reader (webhook
enqueue, saved-search alerts, social drafts, `/v1/events?since=`) stops at the bound and picks the
late event up once it commits. On the base tree each reader moved to 5 and never delivered 4.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.alerts import evaluate as evaluate_module
from services.alerts import webhooks as webhooks_module
from services.alerts.evaluate import evaluate_saved_search
from services.alerts.webhooks import enqueue_new_deliveries
from services.api import resource_queries
from services.api.app import app
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.event_horizon import stable_event_seq
from services.db.models import Account, Event, Proposal, SavedSearch, Source, WebhookDelivery, WebhookEndpoint
from services.db.session import get_sessionmaker, init_db
from services.ids import public_id
from services.social import worker as social_worker
from tests.conftest import make_account, make_user

UTC = dt.UTC


@pytest.fixture()
def file_db(tmp_path: pathlib.Path) -> Iterator[sessionmaker[Session]]:
    """A file database, so two sessions are two connections with SQLite's real locking (the suite's
    in-memory database shares one connection between sessions)."""
    engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'horizon.db'}", connect_args={"timeout": 0.2})
    init_db(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


def _world(factory: sessionmaker[Session]) -> dict[str, Any]:
    """A visible proposal with three committed events, an API-tier webhook endpoint and a saved
    search over events, both starting at seq 0."""
    with factory() as db:
        account = make_account(db, entitlement="api")
        user = make_user(db, account)
        source = make_public_source(db, make_open_licence(db))
        prop = make_visible_proposal(db, source)
        for i in range(3):
            _event(db, prop, source, f"history-{i}")
        endpoint = WebhookEndpoint(
            public_id="",
            account_id=account.id,
            created_by_user_id=user.id,
            url="https://example.com/hook",
            types=["event.published"],
            entity="event",
            query={},
            secret="s",
            watermark_seq=0,
        )
        search = SavedSearch(
            public_id="",
            user_id=user.id,
            account_id=account.id,
            name="every event",
            entity="event",
            query={},
            query_hash="0" * 64,
            delivery_mode="immediate",
            channels=["email"],
            watermark_seq=0,
        )
        db.add_all([endpoint, search])
        db.flush()
        endpoint.public_id = public_id("whe", endpoint.id)
        search.public_id = public_id("ss", search.id)
        db.commit()
        return {"proposal": prop.id, "source": source.id, "endpoint": endpoint.id, "search": search.id}


def _event(db: Session, prop: Proposal, source: Source, key: str, *, seq: int | None = None) -> Event:
    ev = make_event(db, prop, source)
    ev.idempotency_key = f"horizon:{key}"
    if seq is not None:
        ev.seq = seq
    db.flush()
    return ev


def _write(
    factory: sessionmaker[Session], world: dict[str, Any], key: str, *, seq: int | None = None
) -> Session:
    """Opens a writer session, inserts one event and leaves the transaction open."""
    db = factory()
    prop, source = db.get(Proposal, world["proposal"]), db.get(Source, world["source"])
    assert prop is not None and source is not None
    _event(db, prop, source, key, seq=seq)
    return db


def test_sqlite_serialises_writers_so_its_head_is_max_seq(file_db: sessionmaker[Session]) -> None:
    world = _world(file_db)
    writer_a = _write(file_db, world, "a")  # seq 4, uncommitted
    try:
        with file_db() as reader:
            assert stable_event_seq(reader) == 3
        assert _api_seqs(file_db, since=0) == [1, 2, 3]
        # Neither a second writer nor a watermark reader (it writes deliveries and its watermark)
        # can proceed while A's transaction is open, so no seq can commit out of order.
        with pytest.raises(sa.exc.OperationalError, match="locked"):
            _write(file_db, world, "b").close()
        with file_db() as reader, pytest.raises(sa.exc.OperationalError, match="locked"):
            enqueue_new_deliveries(reader)
            reader.commit()
        writer_a.commit()
    finally:
        writer_a.close()
    with file_db() as reader:
        assert stable_event_seq(reader) == 4
        assert enqueue_new_deliveries(reader) == 4
        reader.commit()
        seqs = sorted(reader.scalars(sa.select(WebhookDelivery.event_seq)).all())
    assert seqs == [1, 2, 3, 4]


class _NoMail:
    dry_run = True

    def send(self, *args: Any, **kwargs: Any) -> None:
        return None


def _patch_bound(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """What the Postgres bound returns at this step of the interleaving (module docstring)."""
    for module in (webhooks_module, evaluate_module, social_worker, resource_queries):
        monkeypatch.setattr(module, "stable_event_seq", lambda _db, v=value: v)


def _api_seqs(factory: sessionmaker[Session], since: int) -> list[int]:
    def _override() -> Iterator[Session]:
        with factory() as s:
            yield s
            s.commit()

    app.dependency_overrides[get_db] = _override
    try:
        default_limiter.reset()
        with TestClient(app) as client:
            resp = client.get(f"/v1/events?since={since}&sort=seq&limit=50")
        assert resp.status_code == 200, resp.text
        return [row["seq"] for row in resp.json()["data"]]
    finally:
        app.dependency_overrides.clear()


def _read_all(factory: sessionmaker[Session], world: dict[str, Any]) -> dict[str, Any]:
    """Runs every watermark reader once and reports where each one stands."""
    with factory() as db:
        enqueue_new_deliveries(db)
        search = db.get(SavedSearch, world["search"])
        assert search is not None
        account = db.get(Account, search.account_id)
        assert account is not None
        matched = evaluate_saved_search(db, search, account)
        db.commit()
        endpoint = db.get(WebhookEndpoint, world["endpoint"])
        assert endpoint is not None
        state = {
            "webhook_watermark": endpoint.watermark_seq,
            "delivered_seqs": sorted(db.scalars(sa.select(WebhookDelivery.event_seq)).all()),
            "alert_watermark": search.watermark_seq,
            "alerted_seqs": [item.event_seq for item in matched],
        }
    report = social_worker.draft_posts_tick(factory)
    state["social_watermark"] = report.watermark_seq
    return state


def test_every_reader_stops_at_the_bound_and_picks_up_a_late_commit(
    file_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seq 4 is taken by transaction A before transaction B takes seq 5, and B commits first."""
    world = _world(file_db)
    _read_all(file_db, world)  # every watermark at 3

    # Transaction A has taken seq 4 and is still open (its row invisible); B takes 5 and commits.
    writer_b = _write(file_db, world, "b", seq=5)
    writer_b.commit()
    writer_b.close()
    _patch_bound(monkeypatch, 3)  # A's advertised lock holds the bound at 3
    during = _read_all(file_db, world)
    assert during["webhook_watermark"] == 3
    assert during["alert_watermark"] == 3
    assert during["social_watermark"] == 3
    assert during["delivered_seqs"] == [1, 2, 3]
    assert _api_seqs(file_db, since=3) == []

    writer_a = _write(file_db, world, "a", seq=4)  # A commits, after B
    writer_a.commit()
    writer_a.close()
    _patch_bound(monkeypatch, 5)  # nothing open any more
    after = _read_all(file_db, world)
    assert after["delivered_seqs"] == [1, 2, 3, 4, 5]
    assert after["alerted_seqs"] == [4, 5]
    assert after["webhook_watermark"] == after["alert_watermark"] == after["social_watermark"] == 5
    assert _api_seqs(file_db, since=3) == [4, 5]


def test_a_new_webhook_and_saved_search_start_at_the_bound(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not at `MAX(seq)`: an event an open transaction commits later with a lower seq is still due."""
    from services.api import pro
    from tests.conftest import login

    source = make_public_source(db, make_open_licence(db))
    prop = make_visible_proposal(db, source)
    for i in range(5):
        _event(db, prop, source, f"start-{i}")
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    monkeypatch.setattr(pro, "stable_event_seq", lambda _db: 3)
    resp = client.post("/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]})
    assert resp.status_code == 201, resp.text
    endpoint = db.scalar(sa.select(WebhookEndpoint))
    assert endpoint is not None and endpoint.watermark_seq == 3
