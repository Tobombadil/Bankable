"""`POST /v1/alerts/unsubscribe` and its one-click `GET` twin (US-908 AC1, US-502 AC3;
`services/api/unsubscribe_routes.py`). Mounts the router onto the shared `app` the way
`web/test_auth.py` mounts `auth_router` -- a guarded `include_router` so importing this module
twice (pytest collection) never double-registers the route.
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.alerts.evaluate import run_alert_cycle
from services.api.app import app
from services.api.auth import ResendEmailAdapter
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.api.ratelimit import default_limiter
from services.api.unsubscribe_routes import router as unsubscribe_router
from services.db.models import Alert, Event, SavedSearch
from tests.conftest import make_account, make_user

UTC = dt.UTC

if not any(getattr(r, "path", None) == "/v1/alerts/unsubscribe" for r in app.routes):
    # `services/api/pro.py` already registers `GET /v1/alerts/{alert_id}` on this shared `app`.
    # Starlette matches routes in registration order, so a plain `include_router` (which appends)
    # would let that parameterised route swallow `/v1/alerts/unsubscribe` first, treating
    # "unsubscribe" as an `alert_id` and answering `401` instead of reaching this router (verified
    # by running this suite without the prepend below). The coordinator's real mount must put
    # `unsubscribe_routes.router` on `app` *before* `pro.router` for the same reason; this test
    # module reproduces that ordering by prepending its own routes rather than appending them.
    _before = list(app.router.routes)
    app.include_router(unsubscribe_router)
    _added = [r for r in app.router.routes if r not in _before]
    for _r in _added:
        app.router.routes.remove(_r)
    app.router.routes[0:0] = _added


def _make_saved_search(
    db: Session, account, user, *, channels: list[str] | None = None, status: str = "active"
) -> SavedSearch:
    from services.ids import public_id

    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="Texas storage",
        entity="proposal",
        query={"kind": "storage"},
        query_hash="x",
        channels=channels if channels is not None else ["email"],
        status=status,
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _seed_alert(db: Session, *, channels: list[str] | None = None) -> Alert:
    account = make_account(db)
    user = make_user(db, account)
    search = _make_saved_search(db, account, user, channels=channels)
    alert = Alert(
        public_id="alr_test",
        saved_search_id=search.id,
        user_id=user.id,
        channel="email",
        mode="daily",
        window_start=dt.datetime.now(UTC) - dt.timedelta(days=1),
        window_end=dt.datetime.now(UTC),
        event_seqs=[],
        recipient=user.email,
        subject="1 new match(es)",
        status="sent",
        unsubscribe_token="ut_test_token",
    )
    db.add(alert)
    db.commit()
    return alert


# ------------------------------------------------------------------------------------- POST 200
def test_post_unsubscribe_removes_email_channel_and_writes_event(client: TestClient, db: Session):
    alert = _seed_alert(db, channels=["email", "rss"])

    resp = client.post("/v1/alerts/unsubscribe", json={"token": "ut_test_token"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["unsubscribed"] is True
    assert body["saved_search_name"] == "Texas storage"

    search = db.get(SavedSearch, alert.saved_search_id)
    assert search.channels == ["rss"]
    assert search.status == "active"  # still has a channel left, so not paused

    event = db.query(Event).filter(Event.event_type == "alert_unsubscribed").one()
    assert event.subject_type == "saved_search"
    assert event.subject_id == search.id
    assert event.actor_type == "system"
    assert event.reason == "unsubscribe via token"
    assert event.before["channels"] == ["email", "rss"]
    assert event.after["channels"] == ["rss"]


def test_post_unsubscribe_pauses_search_when_channels_becomes_empty(client: TestClient, db: Session):
    alert = _seed_alert(db, channels=["email"])

    resp = client.post("/v1/alerts/unsubscribe", json={"token": "ut_test_token"})

    assert resp.status_code == 200
    search = db.get(SavedSearch, alert.saved_search_id)
    assert search.channels == []
    assert search.status == "paused"

    event = db.query(Event).filter(Event.event_type == "alert_unsubscribed").one()
    assert event.before["status"] == "active"
    assert event.after["status"] == "paused"
    assert "status" in event.changed_keys


# ------------------------------------------------------------------------------- GET one-click
def test_get_unsubscribe_one_click_also_works(client: TestClient, db: Session):
    alert = _seed_alert(db, channels=["email"])

    resp = client.get("/v1/alerts/unsubscribe", params={"token": "ut_test_token"})

    assert resp.status_code == 200
    assert resp.json()["unsubscribed"] is True
    search = db.get(SavedSearch, alert.saved_search_id)
    assert search.channels == []


# ----------------------------------------------------------------------------------- idempotent
def test_second_use_of_same_token_is_idempotent(client: TestClient, db: Session):
    _seed_alert(db, channels=["email"])

    first = client.post("/v1/alerts/unsubscribe", json={"token": "ut_test_token"})
    second = client.post("/v1/alerts/unsubscribe", json={"token": "ut_test_token"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    # Only one event was ever written -- the second call found `email` already gone and did
    # nothing further (module docstring decision 2).
    assert db.query(Event).filter(Event.event_type == "alert_unsubscribed").count() == 1


# ---------------------------------------------------------------------------------------- 404
def test_unknown_token_is_a_generic_404(client: TestClient, db: Session):
    resp = client.post("/v1/alerts/unsubscribe", json={"token": "ut_does_not_exist"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"
    assert body["title"] == "Not found"
    # Generic: no hint distinguishing "never existed" from any other reason (module docstring).
    assert "does_not_exist" not in body.get("detail", "")


def test_empty_token_is_also_a_generic_404(client: TestClient, db: Session):
    resp = client.post("/v1/alerts/unsubscribe", json={})

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# --------------------------------------------------------------------------------- rate limit
def test_rate_limit_returns_429_after_twenty_requests_from_one_ip(client: TestClient, db: Session):
    default_limiter.reset()
    for _ in range(20):
        resp = client.post("/v1/alerts/unsubscribe", json={"token": "ut_missing"})
        assert resp.status_code == 404

    resp = client.post("/v1/alerts/unsubscribe", json={"token": "ut_missing"})
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "rate_limited"
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------- end-to-end: stops within one cycle
def test_unsubscribe_stops_the_next_alert_cycle_for_that_search(client: TestClient, db: Session):
    """US-502 AC3: "unsubscribing stops that alert within one delivery cycle" -- proved through
    the real `run_alert_cycle`, not just the saved-search row."""
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    make_event(db, prop, src)
    _make_saved_search(db, account, user, channels=["email"])
    db.commit()

    email_port = ResendEmailAdapter()
    first_cycle = run_alert_cycle(db, email_port=email_port)
    db.commit()
    assert len(first_cycle) == 1
    assert len(email_port.sent) == 1
    token = first_cycle[0].unsubscribe_token

    resp = client.post("/v1/alerts/unsubscribe", json={"token": token})
    assert resp.status_code == 200
    db.commit()

    # A second matching event arrives after unsubscribing.
    prop2 = make_visible_proposal(db, src, public_id_suffix="2")
    prop2.kind = "storage"
    make_event(db, prop2, src)
    db.commit()

    second_cycle = run_alert_cycle(db, email_port=email_port)
    db.commit()

    assert second_cycle == [], "the saved search is paused; no new alert is created for it"
    assert len(email_port.sent) == 1, "no additional email was sent"


# -------------------------------------------------------------------- digest body contents
def test_digest_body_carries_the_unsubscribe_link_and_sender_identity(client: TestClient, db: Session):
    account = make_account(db)
    user = make_user(db, account)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.kind = "storage"
    make_event(db, prop, src)
    _make_saved_search(db, account, user, channels=["email"])
    db.commit()

    email_port = ResendEmailAdapter()
    created = run_alert_cycle(db, email_port=email_port)
    db.commit()

    assert len(created) == 1
    body = email_port.sent[0].body
    token = created[0].unsubscribe_token
    assert f"https://infraque.com/unsubscribe?token={token}" in body
    assert "alerts@infraque.com" in body
