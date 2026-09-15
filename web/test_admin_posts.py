"""Tests for the admin posts screens (`web/admin/posts.py`, Sprint 3 item 3): the social review
queue, post detail (body length/limit, credit line, disclosure), the edit/approve/reject/schedule
forms and the per-channel auto-publish switch, over the real `/admin/v1/posts*` and
`/admin/v1/channels/*` API mounted in-process.

Fixtures copied from `web/test_admin_shell.py`; posts are seeded by inserting `Post` rows against
an `Event` from `services.api.conftest.make_event` (screen brief), not through any endpoint --
there is no draft-generation worker in this sprint (`services/api/admin_posts.md` "Deferred").
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import Event, Post, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from tests.conftest import make_account, make_user
from web.admin.posts import router as posts_router
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC

if not any(getattr(r, "path", None) == "/admin/posts" for r in web_app.routes):
    web_app.include_router(posts_router)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session], *, role: str = "operator") -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db, account, email=f"{role}@example.com", role=role, password="correct horse battery staple"
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": f"{role}@example.com", "password": "correct horse battery staple"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text


def _seed_event(db: Session, *, publish_state: str = "public") -> tuple[Proposal, Event]:
    lic = make_open_licence(db)
    source = make_public_source(db, lic)
    proposal = make_visible_proposal(db, source)
    proposal.publish_state = publish_state
    db.flush()
    event = make_event(db, proposal, source)
    return proposal, event


def _make_post(
    db: Session,
    event: Event,
    *,
    channel: str = "bluesky",
    state: str = "draft",
    gate_checked_at: dt.datetime | None = None,
    body: str | None = None,
    scheduled_for: dt.datetime | None = None,
) -> Post:
    link_url = "https://example.org/proposals/test-1"
    credit_line = "Source: Test Public Source"
    post = Post(
        public_id="",
        channel=channel,
        event_id=event.id,
        subject_type="proposal",
        subject_id=event.subject_id,
        template_id="status_change_v1",
        template_version="1",
        body=body or f"Filed: Test Storage Project 1. {credit_line} {link_url}",
        link_url=link_url,
        credit_line=credit_line,
        state=state,
        gate_checked_at=gate_checked_at or dt.datetime.now(UTC),
        scheduled_for=scheduled_for,
    )
    db.add(post)
    db.flush()
    post.public_id = public_id("post", post.id)
    db.flush()
    return post


def _seed_post(
    db_sessionmaker: sessionmaker[Session], *, publish_state: str = "public", state: str = "draft"
) -> str:
    with db_sessionmaker() as db:
        _proposal, event = _seed_event(db, publish_state=publish_state)
        post = _make_post(db, event, state=state)
        db.commit()
        return post.public_id


# =================================================================================================== auth
def test_posts_list_redirects_anonymous_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin/posts")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/posts"


def test_post_approve_without_origin_is_forbidden(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker)
    post_id = _seed_post(db_sessionmaker)
    resp = web_client.post(f"/admin/posts/{post_id}/approve")
    assert resp.status_code == 403


# =================================================================================================== list
def test_posts_list_empty_state_names_the_filter(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/posts?channel=x")
    assert resp.status_code == 200
    assert "No posts match" in resp.text
    assert "channel=x" in resp.text
    assert "Clear all filters" in resp.text


def test_posts_list_renders_rows_and_filters(web_client: TestClient, db_sessionmaker) -> None:
    _seed_post(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.get("/admin/posts")
    assert resp.status_code == 200
    assert "bluesky" in resp.text
    assert "Test Storage Project 1" in resp.text
    assert "Source: Test Public Source" in resp.text

    filtered = web_client.get("/admin/posts?channel=linkedin")
    assert filtered.status_code == 200
    assert "No posts match" in filtered.text


# ================================================================================================= detail
def test_post_detail_shows_body_count_and_limit(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.get(f"/admin/posts/{post_id}")
    assert resp.status_code == 200
    assert "/ 300 characters" in resp.text
    assert "Source: Test Public Source" in resp.text


# ================================================================================================ approve
def test_post_approve_flips_state(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(f"/admin/posts/{post_id}/approve", headers={"origin": "http://testserver"})
    assert resp.status_code == 303
    detail = web_client.get(resp.headers["location"])
    assert "approved" in detail.text


def test_post_approve_on_unpublished_subject_renders_gate_unmet(
    web_client: TestClient, db_sessionmaker
) -> None:
    post_id = _seed_post(db_sessionmaker, publish_state="pending_review")
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(f"/admin/posts/{post_id}/approve", headers={"origin": "http://testserver"})
    assert resp.status_code == 422
    assert "gate" in resp.text.lower() or "public" in resp.text.lower()


# ================================================================================================= reject
def test_post_reject_requires_a_reason(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/posts/{post_id}/reject", data={"reason": ""}, headers={"origin": "http://testserver"}
    )
    assert resp.status_code == 400


def test_post_reject_with_valid_reason_closes_it(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/posts/{post_id}/reject", data={"reason": "style"}, headers={"origin": "http://testserver"}
    )
    assert resp.status_code == 303
    detail = web_client.get(resp.headers["location"])
    assert "rejected" in detail.text


# =============================================================================================== schedule
def test_post_schedule_with_past_time_renders_400(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker, state="approved")
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/posts/{post_id}/schedule",
        data={"scheduled_for": "2020-01-01T00:00", "reason": "queued"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 400


def test_post_schedule_with_future_time_succeeds(web_client: TestClient, db_sessionmaker) -> None:
    post_id = _seed_post(db_sessionmaker, state="approved")
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/posts/{post_id}/schedule",
        data={"scheduled_for": "2099-01-01T10:00", "reason": "queued"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail = web_client.get(resp.headers["location"])
    assert "scheduled" in detail.text


# ================================================================================================ channels
def test_channels_page_is_read_only_for_an_operator(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/posts/channels")
    assert resp.status_code == 200
    assert "read-only" in resp.text

    put_resp = web_client.post(
        "/admin/posts/channels/bluesky/auto-publish",
        data={"auto_publish": "1", "disclosure_label_confirmed": "1", "reason": "graduation"},
        headers={"origin": "http://testserver"},
    )
    assert put_resp.status_code == 403


def test_channels_owner_put_refused_without_disclosure(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker, role="owner")
    resp = web_client.post(
        "/admin/posts/channels/bluesky/auto-publish",
        data={"auto_publish": "1", "reason": "graduation"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 422
    assert "disclosure" in resp.text.lower()


def test_channels_owner_put_succeeds_with_disclosure(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker, role="owner")
    resp = web_client.post(
        "/admin/posts/channels/bluesky/auto-publish",
        data={"auto_publish": "1", "disclosure_label_confirmed": "1", "reason": "graduation"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/posts/channels?flash=")

    detail = web_client.get(resp.headers["location"])
    assert detail.status_code == 200
    assert "auto-publish on" in detail.text
